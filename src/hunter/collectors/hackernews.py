"""Hacker News collector via Algolia search_by_date (DESIGN.md §4.1)."""

from __future__ import annotations

from datetime import UTC, datetime

from ..models import RawItem
from .base import Collector


class HackerNewsCollector(Collector):
    name = "hackernews"

    def __init__(self, db, cfg, client=None) -> None:
        super().__init__(db, client)
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.interval_seconds = cfg.interval_seconds
        self.trust = cfg.trust

    async def poll(self) -> list[RawItem]:
        client = await self.client()
        cursor = await self.load_cursor()
        now = datetime.now(UTC)
        items: list[RawItem] = []
        newest_ts = int(cursor) if cursor and cursor.isdigit() else 0

        for tags in ("story", "comment"):
            params: dict = {
                "query": "claude.ai/referral",
                "tags": tags,
                "hitsPerPage": 50,
            }
            if newest_ts:
                params["numericFilters"] = f"created_at_i>{newest_ts}"
            resp = await client.get(self.cfg.algolia_url, params=params)
            resp.raise_for_status()
            for hit in resp.json().get("hits", []):
                item = self._to_raw(hit, now)
                if item:
                    items.append(item)
                    ts = int(hit.get("created_at_i", 0))
                    if ts > newest_ts:
                        newest_ts = ts

        if items:
            await self.save_cursor(str(newest_ts))
        return items

    def _to_raw(self, hit: dict, now: datetime) -> RawItem | None:
        item_id = hit.get("objectID")
        author = hit.get("author")
        if not item_id or not author:
            return None
        text = hit.get("comment_text") or hit.get("title") or hit.get("story_text") or ""
        url = hit.get("url") or ""
        object_id = hit["objectID"]
        if hit.get("comment_text") is not None:
            permalink = f"https://news.ycombinator.com/item?id={object_id}"
        else:
            permalink = f"https://news.ycombinator.com/item?id={object_id}"
        ts = int(hit.get("created_at_i", 0))
        posted = datetime.fromtimestamp(ts, UTC) if ts else None
        return RawItem(
            source=self.name,
            item_id=object_id,
            author=author,
            text=text,
            urls=[url] if url else [],
            permalink=permalink,
            posted_at=posted,
            fetched_at=now,
        )
