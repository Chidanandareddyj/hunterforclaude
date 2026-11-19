"""Lemmy collector: public search API, no auth (DESIGN.md §4.1).

Searches posts and comments for the referral substring, sorted by new.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..models import RawItem
from .base import Collector


class LemmyCollector(Collector):
    name = "lemmy"

    def __init__(self, db, cfg, client=None) -> None:
        super().__init__(db, client)
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.interval_seconds = cfg.interval_seconds
        self.trust = cfg.trust

    async def poll(self) -> list[RawItem]:
        client = await self.client()
        now = datetime.now(UTC)
        items: list[RawItem] = []
        page = 1
        seen_newest = await self.load_cursor()
        newest_id = seen_newest or ""

        for type_ in ("Posts", "Comments"):
            resp = await client.get(
                f"{self.cfg.base_url}/api/v3/search",
                params={
                    "q": self.cfg.search_term,
                    "type_": type_,
                    "sort": "New",
                    "listing_type": "All",
                    "community_name": self.cfg.community or None,
                    "page": page,
                    "limit": 50,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            # SearchResponse shape: {"posts": [PostView], "comments": [CommentView], ...}
            hits = data.get("posts", []) if type_ == "Posts" else data.get("comments", [])
            for hit in hits if isinstance(hits, list) else []:
                if not isinstance(hit, dict):
                    continue
                item = self._to_raw(hit, type_, now)
                if item is None:
                    continue
                items.append(item)
                if item.item_id > newest_id:
                    newest_id = item.item_id

        if items and newest_id != seen_newest:
            await self.save_cursor(newest_id)
        return self._only_new(items, seen_newest)

    def _to_raw(self, hit: dict, type_: str, now) -> RawItem | None:
        if type_ == "Posts":
            post = hit.get("post") or {}
            creator = (hit.get("creator") or {}).get("name", "")
            content = post.get("body") or post.get("name") or ""
            url = post.get("url") or ""
            permalink = f"{self.cfg.base_url}/post/{post.get('id', '')}"
            item_id = str(post.get("id", ""))
            published = post.get("published")
            if not item_id or not creator:
                return None
        else:
            comment = hit.get("comment") or {}
            creator = (hit.get("creator") or {}).get("name", "")
            content = comment.get("content") or ""
            permalink = f"{self.cfg.base_url}/comment/{comment.get('id', '')}"
            item_id = str(comment.get("id", ""))
            published = comment.get("published")
            if not item_id or not creator:
                return None
        posted = None
        if published:
            try:
                posted = datetime.fromisoformat(published.replace("Z", "+00:00"))
            except ValueError:
                posted = None
        return RawItem(
            source=self.name,
            item_id=f"{type_}/{item_id}",
            author=creator,
            text=content,
            urls=[url] if url else [],
            permalink=permalink,
            posted_at=posted,
            fetched_at=now,
        )

    def _only_new(self, items: list[RawItem], seen_newest: str | None) -> list[RawItem]:
        if not seen_newest:
            # First run: take only the last page's worth to avoid re-alerting history.
            return items[-10:]
        # Lemmy sorts New first, so keep everything until we pass the cursor.
        out = []
        for it in items:
            if it.item_id > seen_newest:
                out.append(it)
        return out
