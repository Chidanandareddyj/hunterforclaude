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
        all_items: list[RawItem] = []
        seen_newest = await self.load_cursor()
        try:
            cursor_ts = float(seen_newest) if seen_newest else 0.0
        except (TypeError, ValueError):
            cursor_ts = 0.0

        for type_ in ("Posts", "Comments"):
            for hit in await self._search(client, type_):
                if not isinstance(hit, dict):
                    continue
                item = self._to_raw(hit, type_, now)
                if item is not None:
                    all_items.append(item)

        # Cursor is a timestamp, not an item_id string. Item ids are prefixed
        # with "Posts/"/"Comments/" and compared lexically previously, which
        # meant comment results were dropped forever once a post raised the
        # cursor (and "Posts/9" sorted above "Posts/10").
        newest_ts = 0.0
        for item in all_items:
            if item.posted_at is not None:
                newest_ts = max(newest_ts, item.posted_at.timestamp())

        if seen_newest is None:
            # First run: surface the freshest items and seed the cursor at the
            # newest one so history is never re-alerted on subsequent polls.
            returned = sorted(
                all_items,
                key=lambda it: it.posted_at.timestamp() if it.posted_at else 0.0,
                reverse=True,
            )[:10]
        else:
            returned = [
                it for it in all_items
                if it.posted_at is not None and it.posted_at.timestamp() > cursor_ts
            ]

        if newest_ts and newest_ts > cursor_ts:
            await self.save_cursor(str(newest_ts))
        return returned

    async def _search(self, client, type_: str) -> list:
        key = "posts" if type_ == "Posts" else "comments"
        params = {
            "q": self.cfg.search_term,
            "type_": type_,
            "sort": "New",
            "listing_type": "All",
            "community_name": self.cfg.community or None,
            "page": 1,
            "limit": 50,
        }
        url = f"{self.cfg.base_url}/api/v3/search"
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        hits = resp.json().get(key, [])
        # A configured community may not exist on this instance (the default
        # "claudeai" community yields zero posts on lemmy.world). Fall back to
        # an instance-wide search so real referral links still flow.
        if self.cfg.community and not hits:
            params.pop("community_name", None)
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            hits = resp.json().get(key, [])
        return hits if isinstance(hits, list) else []

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
