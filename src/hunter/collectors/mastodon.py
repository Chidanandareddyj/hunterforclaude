"""Mastodon collector (public APIs, no auth).

Reads public hashtag timelines and (where the instance allows it) unauthenticated
status search across a small configurable set of instances, looking for the
``claude.ai/referral`` substring in public, non-reblog statuses.

The collector is deliberately defensive: a single bad instance, endpoint or
status never aborts the rest of the poll.  ``fetch_items`` is public and DB-free
so it can be exercised by a live probe; ``poll`` only adds cursor persistence.
"""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from ..logging_setup import get_logger
from ..models import RawItem
from .base import Collector

log = get_logger(component="collector.mastodon")

# Strip HTML tags but keep the visible text (Mastodon renders links as
# <a href="URL">URL</a>, so the bare URL survives in the visible text).
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_BARE_URL_RE = re.compile(r"""https?://[^\s<>"']+""")


class MastodonSource(BaseModel):
    """Standalone config for the Mastodon collector.

    Kept independent of ``..config`` so the collector can be imported and used
    (including by a DB-free live probe) on its own.
    """

    enabled: bool = True
    interval_seconds: float = 60.0
    trust: float = 0.6
    instances: list[str] = Field(
        default_factory=lambda: ["mastodon.social", "fosstodon.org", "techhub.social"]
    )
    hashtags: list[str] = Field(default_factory=lambda: ["ClaudeCode", "ClaudeAI"])
    query: str = "claude.ai/referral"


class MastodonCollector(Collector):
    name = "mastodon"

    def __init__(self, db, cfg, client=None) -> None:
        super().__init__(db, client)
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.interval_seconds = cfg.interval_seconds
        self.trust = cfg.trust
        # Updated by fetch_items so poll() can persist the per-instance cursor.
        self._last_cursor: str | None = None

    # -- public, DB-free API --------------------------------------------------

    async def fetch_items(self, client, cursor: str | None = None) -> list[RawItem]:
        """Fetch and parse matching statuses. No DB access, safe for a live probe.

        ``cursor`` is a JSON object mapping instance -> newest status id seen.
        The updated cursor is exposed via ``self._last_cursor`` for ``poll``.
        """
        cursors = self._load_cursor_map(cursor)
        now = datetime.now(UTC)
        items: list[RawItem] = []
        newest: dict[str, str] = {}

        for instance in self.cfg.instances:
            try:
                inst_items, max_id = await self._fetch_instance(
                    client, instance, cursors.get(instance), now
                )
                items.extend(inst_items)
                if max_id is not None and (
                    instance not in newest or self._id_gt(max_id, newest[instance])
                ):
                    newest[instance] = max_id
            except Exception as exc:  # one bad instance must not abort the poll
                log.warning("mastodon instance failed", instance=instance, error=str(exc)[:200])

        # Merge newly observed ids into the existing cursor (never move it back).
        for instance, mid in newest.items():
            if instance not in cursors or self._id_gt(mid, cursors[instance]):
                cursors[instance] = mid
        self._last_cursor = json.dumps(cursors, separators=(",", ":")) if cursors else None
        return items

    # -- Collector interface --------------------------------------------------

    async def poll(self) -> list[RawItem]:
        client = await self.client()
        cursor = await self.load_cursor()
        items = await self.fetch_items(client, cursor)
        new_cursor = self._last_cursor
        if new_cursor and new_cursor != cursor:
            await self.save_cursor(new_cursor)
        return items

    # -- internals ------------------------------------------------------------

    async def _fetch_instance(
        self, client, instance: str, cursor_id: str | None, now: datetime
    ) -> tuple[list[RawItem], str | None]:
        """Fetch all configured endpoints for one instance.

        Returns the matching items and the newest status id observed (across
        every fetched status, so non-matching posts still advance the cursor).
        """
        instance = instance.strip().rstrip("/")
        if not instance:
            return [], None

        requests: list[tuple[str, dict]] = []
        for hashtag in self.cfg.hashtags:
            tag = str(hashtag).strip().lstrip("#")
            if tag:
                requests.append(
                    (
                        f"https://{instance}/api/v1/timelines/tag/{tag}",
                        {"limit": 40},
                    )
                )
        if self.cfg.query:
            requests.append(
                (
                    f"https://{instance}/api/v2/search",
                    {"q": self.cfg.query, "type": "statuses", "resolve": "false"},
                )
            )

        items: list[RawItem] = []
        seen_ids: set[str] = set()
        max_id: str | None = None

        for url, params in requests:
            for status in await self._get_statuses(client, url, params):
                try:
                    sid = str(status.get("id", "")).strip()
                    if not sid:
                        continue
                    if max_id is None or self._id_gt(sid, max_id):
                        max_id = sid
                    if cursor_id and not self._id_gt(sid, cursor_id):
                        continue
                    if sid in seen_ids:
                        continue
                    seen_ids.add(sid)
                    item = self._to_raw(status, instance, sid, now)
                    if item is not None:
                        items.append(item)
                except Exception as exc:  # skip a single malformed status
                    log.debug("mastodon status skipped", instance=instance, error=str(exc)[:120])
        return items, max_id

    async def _get_statuses(self, client, url: str, params: dict) -> list[dict]:
        """GET one endpoint and normalise the response to a list of status dicts."""
        try:
            resp = await client.get(url, params=params)
            # Rate limited or transient server error: nothing to do this round.
            if resp.status_code != 200:
                return []
            data = resp.json()
        except Exception as exc:
            log.debug("mastodon request failed", url=url, error=str(exc)[:120])
            return []

        if isinstance(data, list):
            return [s for s in data if isinstance(s, dict)]
        if isinstance(data, dict):
            statuses = data.get("statuses")
            if isinstance(statuses, list):
                return [s for s in statuses if isinstance(s, dict)]
        return []

    def _to_raw(
        self, status: dict, instance: str, sid: str, now: datetime
    ) -> RawItem | None:
        """Convert one status to a RawItem, or None if it must be skipped."""
        # Skip boosts/reblogs and anything that is explicitly not public.
        if status.get("reblog"):
            return None
        visibility = status.get("visibility")
        if visibility and visibility != "public":
            return None

        content = html.unescape(str(status.get("content") or ""))
        query = str(self.cfg.query or "").lower()
        if query and query not in content.lower():
            return None

        # Visible text (tags stripped, URLs kept) plus the raw unescaped HTML so
        # the pipeline's bare-URL and href extractors both see the referral link.
        visible = _WS_RE.sub(" ", _TAG_RE.sub(" ", content)).strip()
        text = (visible + "\n" + content).strip() if visible != content else content

        # Also surface the direct URLs; extraction from text still applies.
        # Mastodon repeats the URL as both href and visible text, so dedupe.
        urls = list(dict.fromkeys(u.rstrip(".,;:!?") for u in _BARE_URL_RE.findall(content)))

        account = status.get("account")
        if not isinstance(account, dict):
            account = {}
        author = str(account.get("acct") or account.get("username") or "")

        created = status.get("created_at")
        posted: datetime | None = None
        if created:
            try:
                posted = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            except ValueError:
                posted = None

        permalink = status.get("url") or None
        return RawItem(
            source=self.name,
            item_id=f"{instance}/{sid}",
            author=author,
            text=text,
            urls=urls,
            permalink=str(permalink) if permalink else None,
            posted_at=posted,
            fetched_at=now,
        )

    # -- cursor helpers -------------------------------------------------------

    @staticmethod
    def _load_cursor_map(cursor: str | None) -> dict[str, str]:
        if not cursor:
            return {}
        try:
            data = json.loads(cursor)
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}

    @staticmethod
    def _id_gt(a: str, b: str) -> bool:
        """True when status id ``a`` is newer than ``b`` (numeric when possible)."""
        try:
            return int(a) > int(b)
        except (TypeError, ValueError):
            return str(a) > str(b)
