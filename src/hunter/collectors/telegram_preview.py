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

# Real t.me/s pages: <div class="tgme_widget_message_wrap"> containing
# <div class="tgme_widget_message ..." data-post="channel/123"> with a
# <time datetime="2026-11-17T11:59:00+00:00"> and a .tgme_widget_message_text.
_PERMALINK_RE = re.compile(r"t\.me/(\w+)/(\d+)")


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
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        items: list[RawItem] = []
        wraps = tree.css("div.tgme_widget_message_wrap")
        if not wraps:
            wraps = tree.css("div.tgme_widget_message")
        for wrap in wraps:
            msg = wrap.css_first("div.tgme_widget_message") or wrap

            # Prefer the canonical data-post attribute; fall back to the first
            # t.me/<channel>/<id> link in the block (older/minimal markup).
            msg_id = ""
            data_post = msg.attributes.get("data-post") or ""
            if "/" in data_post:
                msg_id = data_post.rsplit("/", 1)[-1]
            if not msg_id:
                match = _PERMALINK_RE.search(wrap.html or "")
                if not match:
                    continue
                msg_id = match.group(2)

            item_id = f"{handle}/{msg_id}"
            if item_id in self._seen:
                continue
            self._seen.add(item_id)

            text_node = msg.css_first(".tgme_widget_message_text")
            text = text_node.text(separator=" ") if text_node else ""

            # Preserve anchor hrefs: a referral link may be the link target
            # while the visible text is just "here" or "Try".
            urls: list[str] = []
            for anchor in msg.css("a[href]"):
                href = anchor.attributes.get("href") or ""
                if href.startswith(("http://", "https://")):
                    urls.append(href)

            posted = None
            time_node = msg.css_first("time[datetime]")
            raw_dt = time_node.attributes.get("datetime") if time_node else None
            if raw_dt:
                try:
                    posted = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
                    if posted.tzinfo is None:
                        posted = posted.replace(tzinfo=UTC)
                except ValueError:
                    posted = None

            items.append(
                RawItem(
                    source=self.name,
                    item_id=item_id,
                    author=handle,
                    text=text,
                    urls=urls,
                    permalink=f"https://t.me/{handle}/{msg_id}",
                    posted_at=posted,
                    fetched_at=now,
                )
            )
        return items
