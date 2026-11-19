"""claudecoworkcourse directory collector (DESIGN.md §4.1).

Polls the public directory page. robots.txt is checked programmatically at
startup and the collector disables itself if disallowed. Relative timestamps
("3h ago") are converted to approximate absolute times with a margin, and the
source is marked low-confidence since directory codes may already be exhausted.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from ..logging_setup import get_logger
from ..models import RawItem
from .base import Collector, fetch_robots

log = get_logger(component="collector.claudecoworkcourse")

_UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "week": 604800,
    "weeks": 604800,
}

_REL_TIME_RE = re.compile(r"\b(\d+)\s*([a-z]+)\s*ago\b", re.I)
_CODE_RE = re.compile(r"claude\.ai/referral/([A-Za-z0-9_]{6,20})")


class ClaudeCoworkCourseCollector(Collector):
    name = "claudecoworkcourse"

    def __init__(self, db, cfg, client=None) -> None:
        super().__init__(db, client)
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.interval_seconds = cfg.interval_seconds
        self.trust = cfg.trust
        self._robots_checked = False
        # Approximation margin: relative times can be stale by a minute or two.
        self.age_margin_minutes = 5.0

    async def poll(self) -> list[RawItem]:
        client = await self.client()
        if not self._robots_checked:
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
        now = now.astimezone(UTC)
        # Each directory entry is some block containing a link and a relative time.
        for node in tree.css("a"):
            href = node.attributes.get("href") or ""
            m = _CODE_RE.search(href)
            if not m:
                text = node.text() if node.text() else ""
                m2 = _CODE_RE.search(text)
                if not m2:
                    continue
                code = m2.group(1)
            else:
                code = m.group(1)
            # Find the closest ancestor card and its relative-time text.
            rel_text = ""
            ancestor = node.parent
            for _ in range(4):
                if ancestor is None:
                    break
                found = _REL_TIME_RE.search(ancestor.text() or "")
                if found:
                    rel_text = found.group(0)
                    break
                ancestor = ancestor.parent
            posted = self._rel_to_abs(rel_text, now)
            item_id = f"{code}/{int(posted.timestamp()) if posted else 'unknown'}"
            items.append(
                RawItem(
                    source=self.name,
                    item_id=item_id,
                    author=self.name,
                    text=f"{href}",
                    urls=[f"https://claude.ai/referral/{code}"],
                    permalink=self.cfg.url,
                    posted_at=posted,
                    fetched_at=now,
                )
            )
        return items

    def _rel_to_abs(self, rel_text: str, now: datetime) -> datetime | None:
        m = _REL_TIME_RE.search(rel_text)
        if not m:
            return None
        amount = int(m.group(1))
        unit = m.group(2).lower()
        seconds = _UNIT_SECONDS.get(unit)
        if seconds is None:
            return None
        # Convert with a margin so we never over-trust the freshness.
        absolute = now - timedelta(seconds=amount * seconds)
        return absolute - timedelta(minutes=self.age_margin_minutes)
