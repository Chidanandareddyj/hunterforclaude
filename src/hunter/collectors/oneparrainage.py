"""1parrainage collector — GATED (DESIGN.md §4.1, §8).

Ships disabled by default. **Do not enable until you have manually read its
robots.txt and ToS.** The constructor refuses to enable unless the user sets
`gates_ok: true` in config for this source after that manual review.
Date-only timestamps: age is unknown, so confidence is capped by the scorer.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from ..logging_setup import get_logger
from ..models import RawItem
from .base import Collector

log = get_logger(component="collector.oneparrainage")

_CODE_RE = re.compile(r"claude\.ai/referral/([A-Za-z0-9_]{6,20})")
_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


class OneParrainageCollector(Collector):
    name = "oneparrainage"

    def __init__(self, db, cfg, client=None, *, gates_ok: bool = False) -> None:
        super().__init__(db, client)
        self.cfg = cfg
        self.gates_ok = gates_ok
        self.enabled = cfg.enabled and gates_ok  # never enabled without manual clearance
        self.interval_seconds = cfg.interval_seconds
        self.trust = cfg.trust
        self._robots_checked = False

    async def poll(self) -> list[RawItem]:
        client = await self.client()
        if not self._robots_checked:
            from .base import fetch_robots

            policy = await fetch_robots(client, self.cfg.url)
            self._robots_checked = True
            if not policy.allowed:
                log.warning("robots.txt disallows this page; disabling collector",
                            source=self.name, note=policy.note)
                self.enabled = False
                return []
        resp = await client.get(self.cfg.url)
        resp.raise_for_status()
        return self.parse(resp.text, now=datetime.now(UTC))

    def parse(self, html: str, *, now: datetime) -> list[RawItem]:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        items: list[RawItem] = []
        seen: set[str] = set()
        for node in tree.css("a,div,span,li"):
            text = node.text() or ""
            m = _CODE_RE.search(text) or _CODE_RE.search(node.attributes.get("href") or "")
            if not m:
                continue
            code = m.group(1)
            if code in seen:
                continue
            seen.add(code)
            dm = _DATE_RE.search(text)
            posted = None
            if dm:
                try:
                    posted = datetime(
                        int(dm.group(1)), int(dm.group(2)), int(dm.group(3)),
                        tzinfo=UTC,
                    )
                except ValueError:
                    posted = None
            items.append(
                RawItem(
                    source=self.name,
                    item_id=f"{code}/{dm.group(0) if dm else 'undated'}",
                    author=self.name,
                    text=text[:2000],
                    urls=[f"https://claude.ai/referral/{code}"],
                    permalink=self.cfg.url,
                    posted_at=posted,  # date-only: hour/min unknown → capped confidence
                    fetched_at=now,
                )
            )
        return items
