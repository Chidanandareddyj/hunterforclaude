"""Bluesky public AppView *search* collector.

Complements the Jetstream firehose collector: searchPosts catches posts that
were published while the app was offline. Uses the unauthenticated public
AppView XRPC endpoint and follows its opaque ``cursor`` between polls.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from pydantic import BaseModel

from ..logging_setup import get_logger
from ..models import RawItem
from .base import Collector

log = get_logger(component="collector.bluesky_search")

REFERRAL_SUBSTRING = "claude.ai/referral"

# public.api.bsky.app is geo-blocked from some networks (BunnyCDN 403). The
# same public AppView XRPC method is also served by api.bsky.app; it is used as
# a drop-in fallback so the source keeps working with the documented default.
FALLBACK_ENDPOINT = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; HunterBot/1.0)",
    "Accept": "application/json",
}


class BlueskySearchSource(BaseModel):
    """Standalone config so importing this module never creates a cycle.

    The scheduler/config layer may import this class directly, or build one
    from its own settings object via ``model_validate``.
    """

    enabled: bool = True
    interval_seconds: float = 60.0
    trust: float = 0.7
    endpoint: str = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"
    query: str = "claude.ai/referral"


class BlueskySearchCollector(Collector):
    name = "bluesky_search"

    def __init__(self, db, cfg, client=None) -> None:
        super().__init__(db, client)
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.interval_seconds = cfg.interval_seconds
        self.trust = cfg.trust
        # Set by fetch_posts(); poll() persists it. Kept off the DB path so
        # fetch_posts() can be called directly (e.g. by a live probe).
        self.next_cursor: str | None = None

    async def poll(self) -> list[RawItem]:
        client = await self.client()
        cursor = await self.load_cursor()
        items = await self.fetch_posts(client, cursor)
        if self.next_cursor:
            await self.save_cursor(self.next_cursor)
        return items

    async def fetch_posts(self, client, cursor=None) -> list[RawItem]:
        """Fetch one search page and convert referral posts to RawItems.

        No DB access: safe to call directly for probing. Any HTTP/JSON failure
        is swallowed (logged) so one bad poll never breaks the run loop.
        """
        self.next_cursor = None
        # The AppView rejects *paginated* search requests (a `cursor` query
        # param gets a 403 even when the cursor came from its own response).
        # Fetch the newest page every poll and keep progress as a client-side
        # timestamp cursor instead, so the request shape stays identical.
        params: dict = {"q": self.cfg.query, "sort": "latest", "limit": 100}

        try:
            resp = await self._get(client, params)
        except httpx.HTTPError as exc:
            log.warning("bluesky search request failed", error=str(exc)[:200])
            return []

        try:
            data = resp.json()
        except (ValueError, UnicodeDecodeError) as exc:
            log.warning("bluesky search non-JSON response", error=str(exc)[:200])
            return []
        if not isinstance(data, dict):
            return []

        posts = data.get("posts")
        if not isinstance(posts, list):
            return []

        # Persist the newest post timestamp, never the opaque API cursor.
        self.next_cursor = self._fallback_cursor(posts)
        if self.next_cursor and cursor:
            try:
                newest = datetime.fromisoformat(self.next_cursor.replace("Z", "+00:00"))
                previous = datetime.fromisoformat(str(cursor).replace("Z", "+00:00"))
                if newest <= previous:
                    self.next_cursor = str(cursor)  # never move the cursor backwards
            except ValueError:
                pass

        now = datetime.now(UTC)
        items: list[RawItem] = []
        for post in posts:
            try:
                item = self._to_raw(post, now)
            except Exception as exc:  # one malformed record must not drop the batch
                log.warning("bluesky search bad record", error=str(exc)[:200])
                continue
            if item is not None:
                items.append(item)

        # Client-side dedup against the saved timestamp cursor. An unparseable
        # (legacy API) cursor is ignored; the DB still dedups by referral code.
        if cursor:
            try:
                cutoff = datetime.fromisoformat(str(cursor).replace("Z", "+00:00"))
            except ValueError:
                cutoff = None
            if cutoff is not None:
                items = [
                    item for item in items
                    if item.posted_at is None or item.posted_at > cutoff
                ]
        return items

    async def _get(self, client, params: dict) -> httpx.Response:
        """Try the configured endpoint, then the public AppView fallback."""
        endpoints = [self.cfg.endpoint]
        if FALLBACK_ENDPOINT not in endpoints:
            endpoints.append(FALLBACK_ENDPOINT)
        last_exc: httpx.HTTPError | None = None
        for url in endpoints:
            try:
                resp = await client.get(url, params=params, headers=HTTP_HEADERS)
                resp.raise_for_status()
                return resp
            except httpx.HTTPError as exc:
                last_exc = exc
                log.warning(
                    "bluesky search endpoint failed",
                    url=url,
                    status=getattr(exc.response, "status_code", None)
                    if isinstance(exc, httpx.HTTPStatusError)
                    else None,
                )
                continue
        assert last_exc is not None  # at least one endpoint was attempted
        raise last_exc

    @staticmethod
    def _fallback_cursor(posts: list) -> str | None:
        """When the API omits ``cursor``, use the newest post timestamp."""
        newest: datetime | None = None
        newest_raw: str | None = None
        for post in posts:
            if not isinstance(post, dict):
                continue
            record = post.get("record")
            created = record.get("createdAt") if isinstance(record, dict) else None
            raw = created or post.get("indexedAt")
            if not isinstance(raw, str) or not raw:
                continue
            try:
                ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if newest is None or ts > newest:
                newest, newest_raw = ts, raw
        return newest_raw

    def _to_raw(self, post, now: datetime) -> RawItem | None:
        if not isinstance(post, dict):
            return None
        uri = post.get("uri")
        if not isinstance(uri, str) or not uri:
            return None
        record = post.get("record")
        if not isinstance(record, dict):
            return None

        text = record.get("text")
        text = text if isinstance(text, str) else ""
        urls: list[str] = []

        # Link cards carry the untruncated referral URL (post text is often
        # clipped with "..."). Include the card text and all link URIs.
        embed = record.get("embed")
        if isinstance(embed, dict):
            external = embed.get("external")
            if isinstance(external, dict):
                for key in ("uri", "title", "description"):
                    value = external.get(key)
                    if isinstance(value, str) and value:
                        if key == "uri":
                            urls.append(value)
                        text = f"{text} {value}".strip()
        for facet in record.get("facets") or []:
            if not isinstance(facet, dict):
                continue
            for feature in facet.get("features") or []:
                if isinstance(feature, dict) and isinstance(feature.get("uri"), str):
                    urls.append(feature["uri"])

        combined = " ".join([text, *urls]).strip()
        lowered = combined.lower()
        query = str(self.cfg.query).lower()
        if REFERRAL_SUBSTRING not in lowered and (query and query not in lowered):
            return None

        record_key = uri.rstrip("/").split("/")[-1]
        author_obj = post.get("author")
        if not isinstance(author_obj, dict):
            author_obj = {}
        handle = author_obj.get("handle") or author_obj.get("did") or ""
        if not record_key or not handle:
            return None
        handle = str(handle).lstrip("@")
        permalink = f"https://bsky.app/profile/{handle}/post/{record_key}"

        posted: datetime | None = None
        created = record.get("createdAt") or post.get("indexedAt")
        if isinstance(created, str) and created:
            try:
                posted = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                posted = None

        return RawItem(
            source=self.name,
            item_id=uri,
            author=handle,
            text=combined,
            urls=urls,
            permalink=permalink,
            posted_at=posted,
            fetched_at=now,
        )
