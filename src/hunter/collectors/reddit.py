"""Reddit collector: public PullPush archive with Reddit JSON fallback.

PullPush (no auth) is tried first for comments and submissions. If it is
unavailable (521/429/network) or yields nothing, the collector falls back to
Reddit's public ``search.json`` endpoint, and finally to the Arctic Shift
archive (a PullPush replacement). Every HTTP/JSON failure is contained per
source, so one broken endpoint never brings down the collector.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel

from ..logging_setup import get_logger
from ..models import RawItem
from .base import Collector

log = get_logger(component="collector.reddit")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REDDIT_BASE = "https://www.reddit.com"
ARCTIC_SHIFT_URL = "https://arctic-shift.photon-reddit.com/api"
# The scheme is optional in Reddit text; capture the code and the raw URL.
REFERRAL_RE = re.compile(r"(?:https?://)?claude\.ai/referral/([A-Za-z0-9_-]{4,})", re.I)


class RedditSource(BaseModel):
    """Standalone config so this module can be built/imported on its own."""

    enabled: bool = True
    interval_seconds: float = 60.0
    trust: float = 0.7
    pullpush_url: str = "https://api.pullpush.io/reddit/search"
    subreddits: list[str] = ["ClaudeCode", "ClaudeAI", "anthropic"]
    query: str = "claude.ai/referral"


class RedditCollector(Collector):
    name = "reddit"

    def __init__(self, db, cfg, client=None) -> None:
        super().__init__(db, client)
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.interval_seconds = cfg.interval_seconds
        self.trust = cfg.trust
        self._subs = {s.lower() for s in cfg.subreddits}
        self._headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    # -- public API -------------------------------------------------------------

    async def poll(self) -> list[RawItem]:
        """Load/save the cursor around the DB-free :meth:`fetch_items`."""
        client = await self.client()
        cursor = await self.load_cursor()
        items = await self.fetch_items(client, cursor=cursor)
        if items:
            cur_ts = self._cursor_ts(cursor)
            newest = max(
                (it.posted_at.timestamp() for it in items if it.posted_at is not None),
                default=0.0,
            )
            if newest > cur_ts:
                await self.save_cursor(str(int(newest)))
        return items

    async def fetch_items(self, client, cursor: str | None = None) -> list[RawItem]:
        """Fetch referral-bearing Reddit items.

        This is deliberately free of any DB dependency so a live probe can call
        it directly with just an ``httpx.AsyncClient``.
        """
        now = datetime.now(UTC)
        cur_ts = self._cursor_ts(cursor)
        collected: list[tuple[dict, str]] = []

        collected.extend(await self._pullpush(client, cur_ts))
        if not collected:
            collected.extend(await self._reddit_json(client))
        if not collected:
            collected.extend(await self._arctic_shift(client, cur_ts))

        items: list[RawItem] = []
        seen: set[str] = set()
        for rec, kind in collected:
            item = self._to_raw(rec, kind, now)
            if item is None or item.item_id in seen:
                continue
            seen.add(item.item_id)
            if cur_ts and (item.posted_at is None or item.posted_at.timestamp() <= cur_ts):
                continue
            items.append(item)
        return items

    # -- sources ---------------------------------------------------------------

    async def _pullpush(self, client, cur_ts: float) -> list[tuple[dict, str]]:
        """PullPush archive: search comments and submissions, scoped per sub."""
        out: list[tuple[dict, str]] = []
        subs = self.cfg.subreddits or [None]
        base = self.cfg.pullpush_url.rstrip("/")
        for kind in ("comment", "submission"):
            for sub in subs:
                params: dict = {"q": self.cfg.query, "sort": "desc", "size": 100}
                if cur_ts:
                    params["after"] = int(cur_ts)
                if sub:
                    params["subreddit"] = sub
                try:
                    resp = await client.get(
                        f"{base}/{kind}/",
                        params=params,
                        headers=self._headers,
                    )
                    resp.raise_for_status()
                    payload = resp.json()
                except (httpx.HTTPError, ValueError) as exc:
                    log.debug("pullpush unavailable", kind=kind, sub=sub, error=str(exc)[:120])
                    continue
                records = payload.get("data", []) if isinstance(payload, dict) else payload
                if not isinstance(records, list):
                    continue
                for rec in records:
                    if isinstance(rec, dict) and self._subreddit_allowed(rec.get("subreddit")):
                        out.append((rec, kind))
        return out

    async def _reddit_json(self, client) -> list[tuple[dict, str]]:
        """Fallback to Reddit's public search.json (browser-like UA)."""
        out: list[tuple[dict, str]] = []
        for sub in self.cfg.subreddits or ["all"]:
            try:
                resp = await client.get(
                    f"{REDDIT_BASE}/r/{sub}/search.json",
                    params={
                        "q": self.cfg.query,
                        "restrict_sr": 1,
                        "sort": "new",
                        "t": "hour",
                        "limit": 100,
                    },
                    headers=self._headers,
                )
                if resp.status_code in (403, 429):
                    log.debug("reddit json rate-limited", sub=sub, status=resp.status_code)
                    continue
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.debug("reddit json unavailable", sub=sub, error=str(exc)[:120])
                continue
            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            children = data.get("children", []) if isinstance(data, dict) else []
            if not isinstance(children, list):
                continue
            for child in children:
                if not isinstance(child, dict) or not isinstance(child.get("data"), dict):
                    continue
                kind = "comment" if child.get("kind") == "t1" else "submission"
                rec = child["data"]
                if self._subreddit_allowed(rec.get("subreddit")):
                    out.append((rec, kind))
        return out

    async def _arctic_shift(self, client, cur_ts: float) -> list[tuple[dict, str]]:
        """Last resort: Arctic Shift archive. Needs a subreddit plus a bounded
        time window, otherwise its full-text search times out server-side."""
        out: list[tuple[dict, str]] = []
        after = int(cur_ts) if cur_ts else int(datetime.now(UTC).timestamp()) - 30 * 86400
        queries = [self.cfg.query, "guest pass"]
        for sub in self.cfg.subreddits or ["ClaudeCode"]:
            for query in queries:
                try:
                    resp = await client.get(
                        f"{ARCTIC_SHIFT_URL}/posts/search",
                        params={
                            "query": query,
                            "subreddit": sub,
                            "after": after,
                            "sort": "desc",
                            "limit": 100,
                        },
                        headers=self._headers,
                        timeout=12.0,
                    )
                    resp.raise_for_status()
                    payload = resp.json()
                except (httpx.HTTPError, ValueError) as exc:
                    log.debug("arctic shift unavailable", sub=sub, error=str(exc)[:120])
                    continue
                records = payload.get("data", []) if isinstance(payload, dict) else []
                if not isinstance(records, list):
                    continue
                for rec in records:
                    if (
                        isinstance(rec, dict)
                        and self._has_referral(rec)
                        and self._subreddit_allowed(rec.get("subreddit"))
                    ):
                        out.append((rec, "submission"))
                if out:
                    return out  # stop early: we have fresh, referral-bearing data
        return out

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _cursor_ts(cursor: str | None) -> float:
        try:
            return float(cursor) if cursor else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _subreddit_allowed(self, sub: object) -> bool:
        if not self._subs or not sub:
            return True
        return str(sub).lower() in self._subs

    @staticmethod
    def _has_referral(rec: dict) -> bool:
        blob = " ".join(str(rec.get(k) or "") for k in ("body", "selftext", "title", "url"))
        return bool(REFERRAL_RE.search(html.unescape(blob)))

    def _to_raw(self, rec: dict, kind: str, now: datetime) -> RawItem | None:
        try:
            rid = rec.get("id") or rec.get("name")
            if not rid:
                return None
            body = " ".join(
                str(rec.get(key) or "")
                for key in ("body", "selftext", "title", "url")
                if rec.get(key)
            )
            text = html.unescape(body)
            if not REFERRAL_RE.search(text):
                return None
            urls: list[str] = []
            for match in REFERRAL_RE.finditer(text):
                url = match.group(0).rstrip(".,);]")
                if not url.startswith("http"):
                    url = "https://" + url
                if url not in urls:
                    urls.append(url)
            permalink: str | None = str(rec.get("permalink") or "")
            if permalink:
                if not permalink.startswith("http"):
                    permalink = REDDIT_BASE + (
                        permalink if permalink.startswith("/") else "/" + permalink
                    )
            else:
                permalink = None
            ts = rec.get("created_utc")
            posted: datetime | None = None
            if ts is not None:
                try:
                    posted = datetime.fromtimestamp(float(ts), UTC)
                except (TypeError, ValueError, OSError, OverflowError):
                    posted = None
            return RawItem(
                source=self.name,
                item_id=f"{kind}/{rid}",
                author=str(rec.get("author") or ""),
                text=text[:10000],
                urls=urls,
                permalink=permalink,
                posted_at=posted,
                fetched_at=now,
            )
        except Exception as exc:  # one malformed record must not stop the poll
            log.debug("reddit record skipped", error=str(exc)[:120])
            return None
