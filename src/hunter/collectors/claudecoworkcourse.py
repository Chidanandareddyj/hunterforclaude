"""claudecoworkcourse directory collector (DESIGN.md §4.1).

Polls the public directory page. robots.txt is checked programmatically at
startup and the collector disables itself if disallowed. Relative timestamps
("3h ago") are converted to approximate absolute times with a margin, and the
source is marked low-confidence since directory codes may already be exhausted.

The live page is a table of ``tr.gpx-row`` entries. Genuine live rows carry the
``gpx-row--unverified`` class (or no modifier at all) and a relative age in a
cell. Retired rows are marked with ``gpx-row--retired``/``gpx-retired-row``,
usually also ``hidden`` and with a "100 / 100" or "100 clicks" marker. Only
retired rows are skipped; live rows are kept even if their age is not shown
(the scorer caps confidence for unknown-age items).
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
    "mo": 2592000,
    "mon": 2592000,
    "month": 2592000,
    "months": 2592000,
    "y": 31536000,
    "yr": 31536000,
    "year": 31536000,
    "years": 31536000,
}

_REL_TIME_RE = re.compile(r"\b(\d+)\s*([a-z]+)\s*ago\b", re.I)
# Capture the *whole* code. The live directory contains codes longer than the
# config's 6..20 window (e.g. 23 chars); truncating them would emit a wrong
# code. Downstream matching against the configured pattern decides validity.
_CODE_RE = re.compile(r"claude\.ai/referral/([A-Za-z0-9_]{4,60})")
_RETIRED_MARKERS = ("retired", "100 / 100", "100 clicks")


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
        seen: set[str] = set()
        for node in tree.css("a"):
            href = node.attributes.get("href") or ""
            match = _CODE_RE.search(href) or _CODE_RE.search(node.text() or "")
            if not match:
                continue
            code = match.group(1)

            row = self._row_of(node)
            if self._is_retired(node, row):
                continue
            if code in seen:
                continue
            seen.add(code)

            # Look for a relative age on the row first, then walk a few
            # ancestors for card-like layouts that have no explicit row.
            rel_text = ""
            ancestor = row or node.parent
            for _ in range(5):
                # Never climb out of the row into the table: a sibling row's
                # age would otherwise be attributed to an undated live row.
                if ancestor is None or ancestor.tag in ("table", "tbody", "section", "body", "html"):
                    break
                found = _REL_TIME_RE.search(ancestor.text(separator=" ") or "")
                if found:
                    rel_text = found.group(0)
                    break
                ancestor = ancestor.parent

            posted = self._rel_to_abs(rel_text, now)  # None for undated rows
            items.append(
                RawItem(
                    source=self.name,
                    # Stable id: the code itself. Using the recomputed relative
                    # timestamp here changed every poll and caused repeat alerts.
                    item_id=code,
                    author=self.name,
                    text=href,
                    urls=[f"https://claude.ai/referral/{code}"],
                    permalink=self.cfg.url,
                    posted_at=posted,
                    fetched_at=now,
                )
            )
        return items

    @staticmethod
    def _row_of(node):
        """Closest row-like container for a link (live table row, retired
        list item, or generic card). ``None`` when there is no obvious row."""
        ancestor = node.parent
        while ancestor is not None:
            if ancestor.tag in ("body", "html", "table", "tbody", "section"):
                return None
            classes = (ancestor.attributes.get("class") or "").lower()
            if ancestor.tag in ("tr", "li") or "gpx-row" in classes or "card" in classes:
                return ancestor
            ancestor = ancestor.parent
        return None

    @staticmethod
    def _is_retired(node, row) -> bool:
        """Only genuinely retired/hidden rows are skipped."""
        ancestor = node
        while ancestor is not None:
            classes = (ancestor.attributes.get("class") or "").lower()
            if "retired" in classes or ancestor.attributes.get("hidden") is not None:
                return True
            if ancestor is row:
                break
            ancestor = ancestor.parent
        if row is not None:
            lowered = (row.text(separator=" ") or "").lower()
            if any(marker in lowered for marker in _RETIRED_MARKERS):
                return True
        return False

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
