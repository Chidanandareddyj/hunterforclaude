"""Telegram public-preview collector (t.me/s/<handle>, no account needed).

Empty seed list by default (DESIGN.md §4.1): the owner adds channel handles
in config.yaml. Rate: 15s per channel with jitter, small channel count assumed.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx

from ..models import RawItem
from .base import Collector

# t.me/s pages embed messages in <div class="tgme_widget_message ..."> blocks
# with a datetime attribute and permalinks like t.me/<handle>/<id>.
_MSG_SPLIT_RE = re.compile(r'(?=<div class="tgme_widget_message[ "])')
_DATETIME_RE = re.compile(r'datetime="(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"')
_PERMALINK_RE = re.compile(r"t\.me/(\w+)/(\d+)")
_TEXT_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " "}


class TelegramPreviewCollector(Collector):
    name = "telegram_preview"

    def __init__(self, db, cfg, client=None) -> None:
        super().__init__(db, client)
        self.cfg = cfg
        self.enabled = cfg.enabled and bool(cfg.channels)
        self.interval_seconds = cfg.interval_seconds
        self.trust = cfg.trust
        self._seen: set[str] = set()

    async def poll(self) -> list[RawItem]:
        if not self.cfg.channels:
            return []
        client = await self.client()
        now = datetime.now(UTC)
        items: list[RawItem] = []
        for handle in self.cfg.channels:
            items.extend(await self._poll_channel(client, handle.strip(), now))
        return items

    async def _poll_channel(self, client: httpx.AsyncClient, handle: str, now: datetime) -> list[RawItem]:
        url = f"https://t.me/s/{handle}"
        try:
            resp = await client.get(url)
        except httpx.HTTPError:
            return []
        if resp.status_code != 200:
            # Private or missing channel: nothing to parse; health stays ok.
            return []
        return self.parse(handle, resp.text, now=now)

    def parse(self, handle: str, html: str, *, now: datetime) -> list[RawItem]:
        items: list[RawItem] = []
        for chunk in _MSG_SPLIT_RE.split(html):
            idm = _PERMALINK_RE.search(chunk)
            if not idm:
                continue
            _, msg_id = idm.group(1), idm.group(2)
            item_id = f"{handle}/{msg_id}"
            if item_id in self._seen:
                continue
            self._seen.add(item_id)
            text = ""
            tm = _TEXT_RE.search(chunk)
            if tm:
                text = tm.group(1)
                text = _TAG_RE.sub("", text)
                for k, v in _HTML_ENTITIES.items():
                    text = text.replace(k, v)
            posted = None
            dm = _DATETIME_RE.search(chunk)
            if dm:
                try:
                    posted = datetime.fromisoformat(dm.group(1)).replace(tzinfo=UTC)
                except ValueError:
                    posted = None
            items.append(
                RawItem(
                    source=self.name,
                    item_id=item_id,
                    author=handle,
                    text=text,
                    urls=[],
                    permalink=f"https://t.me/{handle}/{msg_id}",
                    posted_at=posted,
                    fetched_at=now,
                )
            )
        return items
