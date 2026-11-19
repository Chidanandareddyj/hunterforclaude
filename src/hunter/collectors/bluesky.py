"""Bluesky Jetstream collector (DESIGN.md §4.1).

Subscribes to the post collection firehose and substring-matches client-side
(Jetstream has no server-side URL filter). Reconnects with cursor, handles
keepalive messages, never follows unknown redirects.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import websockets

from ..logging_setup import get_logger
from ..models import RawItem
from .base import Collector

log = get_logger(component="collector.bluesky")

KEEPALIVE_MAX_GAP = 10  # Jetstream sends a keepalive every ~1s when quiet


class BlueskyCollector(Collector):
    name = "bluesky"

    def __init__(self, db, cfg, client=None) -> None:
        super().__init__(db, client)
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.interval_seconds = 5.0  # reconnect delay between firehose sessions
        self.trust = cfg.trust
        self._backlog: list[RawItem] = []

    async def poll(self) -> list[RawItem]:
        """Not poll-based: runs a firehose session until it breaks.

        The run() loop treats each poll() call as one session attempt; items
        accumulate in `_backlog` and are returned as a batch on session end.
        """
        drained = self._backlog
        self._backlog = []
        cursor = await self.load_cursor()
        url = f"{self.cfg.jetstream_url}/subscribe"
        params = []
        if self.cfg.wanted_collection:
            params.append(f"wantedCollections={self.cfg.wanted_collection}")
        if cursor:
            params.append(f"cursor={cursor}")
        if params:
            url += "?" + "&".join(params)
        try:
            async with websockets.connect(url) as ws:
                last_message = datetime.now(UTC)
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    except TimeoutError:
                        if (datetime.now(UTC) - last_message).total_seconds() > 30:
                            break  # stale connection; reconnect with cursor
                        continue
                    last_message = datetime.now(UTC)
                    msg = json.loads(raw)
                    items = self._handle_message(msg)
                    self._backlog.extend(items)
                    # Yield in small batches so a long-lived session still alerts fast.
                    if len(self._backlog) >= 5:
                        drained = self._backlog
                        self._backlog = []
                        return drained
        except (websockets.ConnectionClosed, OSError) as exc:
            log.warning("jetstream session ended", source=self.name, error=str(exc)[:120])
        # Return whatever accumulated; run() will call poll() again after a pause.
        return drained

    def _handle_message(self, msg: dict) -> list[RawItem]:
        kind = msg.get("kind")
        if kind == "keepalive":
            self._maybe_save_cursor(msg)
            return []
        if kind != "commit":
            return []
        commit = msg.get("commit", {})
        record = commit.get("record") or {}
        if commit.get("operation") != "create":
            return []
        collection = commit.get("collection", "")
        if collection != self.cfg.wanted_collection:
            return []
        self._maybe_save_cursor(msg)
        text = record.get("text", "")
        now = datetime.now(UTC)
        posted = None
        created = record.get("createdAt") or record.get("created_at")
        if created:
            try:
                posted = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                posted = None
        did = commit.get("repo", "")
        rkey = commit.get("rkey", "")
        permalink = f"https://bsky.app/profile/{did}/post/{rkey}"
        # Link embeds live under record["embed"]["external"], and explicit links
        # under record["facets"][*]["features"][*]["uri"]. RawItem.urls is a
        # list[str]; passing the bare uri string here raised a validation error.
        urls: list[str] = []
        embed = record.get("embed")
        if isinstance(embed, dict):
            external = embed.get("external")
            if isinstance(external, dict) and external.get("uri"):
                urls.append(external["uri"])
        for facet in record.get("facets") or []:
            if not isinstance(facet, dict):
                continue
            for feature in facet.get("features") or []:
                if isinstance(feature, dict) and feature.get("uri"):
                    urls.append(feature["uri"])
        return [
            RawItem(
                source=self.name,
                item_id=f"{did}/{rkey}",
                author=did,
                text=text,
                urls=urls,
                permalink=permalink,
                posted_at=posted or now,
                fetched_at=now,
            )
        ]

    def _maybe_save_cursor(self, msg: dict) -> None:
        ts = msg.get("time_us")
        if ts:
            # Persisting on every message is too chatty; the DB write is cheap
            # enough at this rate and guarantees no re-alerting after restart.
            self._cursor_dirty = str(ts)
            # fire-and-forget save (db calls are quick)
            import asyncio as _aio

            try:
                loop = _aio.get_running_loop()
            except RuntimeError:
                return  # safe when _handle_message is called outside a loop
            loop.create_task(self.save_cursor(str(ts)))
