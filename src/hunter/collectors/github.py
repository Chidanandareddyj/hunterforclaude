"""GitHub public REST search collector (issues + gists), no auth.

Searches issues, pull requests and gists for the referral substring via the
public search endpoints, which are available without a token (rate limited to
roughly 10 requests/minute for unauthenticated clients). We use one combined
query per endpoint and keep the collector disabled by default so a user can
opt in once they are comfortable with the rate limit.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel

from ..logging_setup import get_logger
from ..models import RawItem
from .base import Collector

log = get_logger(component="collector")

# Required by GitHub's REST API for unauthenticated (and authenticated) calls.
GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

REFERRAL_SUBSTRING = "claude.ai/referral"


class GitHubSource(BaseModel):
    """Standalone config for this source (defined here so the module can be
    imported/probed without touching the central config module)."""

    enabled: bool = False  # rate-limited: opt-in only
    interval_seconds: float = 300.0
    trust: float = 0.5
    api_url: str = "https://api.github.com/search"
    query: str = "claude.ai/referral"


def _parse_ts(value: object) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp into an aware datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _text(value: object) -> str:
    """Coerce an arbitrary JSON value to a usable text string."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


class GitHubCollector(Collector):
    name = "github"

    def __init__(self, db, cfg, client=None) -> None:
        super().__init__(db, client)
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.interval_seconds = cfg.interval_seconds
        self.trust = cfg.trust

    async def poll(self) -> list[RawItem]:
        """Fetch new items, advancing the cursor to the newest record seen."""
        client = await self.client()
        cursor = await self.load_cursor()
        items, newest = await self._fetch(client, cursor)
        if newest and newest != cursor:
            await self.save_cursor(newest)
        return items

    async def fetch_items(self, client, cursor: str | None = None) -> list[RawItem]:
        """Public, DB-free fetch used by live probes and tests."""
        items, _ = await self._fetch(client, cursor)
        return items

    def _search_query(self) -> str:
        """Return the exact-phrase query GitHub actually understands.

        GitHub tokenizes punctuation in issue search, so a bare
        ``claude.ai/referral`` matches thousands of unrelated issues (the
        tokens "claude", "ai" and "referral") and surfaces no real links.
        Quoting it forces an exact-phrase match, which is what finds actual
        referral URLs. The configured value stays human-readable.
        """
        q = _text(self.cfg.query).strip()
        if q and not (q.startswith('"') and q.endswith('"')):
            return f'"{q}"'
        return q

    async def _fetch(
        self, client, cursor: str | None = None
    ) -> tuple[list[RawItem], str | None]:
        """Query both search endpoints. Returns (items, newest_created_at)."""
        now = datetime.now(UTC)
        cursor_ts = _parse_ts(cursor) if cursor else None
        items: list[RawItem] = []
        newest_dt: datetime | None = None
        newest_str: str | None = None

        endpoints = (
            ("issues", self._build_issue),
            ("gists", self._build_gist),
        )
        for kind, builder in endpoints:
            url = f"{self.cfg.api_url}/{kind}"
            try:
                resp = await client.get(
                    url,
                    params={
                        "q": self._search_query(),
                        "sort": "created",
                        "order": "desc",
                        "per_page": 50,
                    },
                    headers=GITHUB_HEADERS,
                )
            except Exception as exc:  # defensive: never let a single request kill the poll
                log.warning(
                    "github request failed",
                    source=self.name,
                    endpoint=kind,
                    error=str(exc)[:200],
                )
                continue

            if resp.status_code in (403, 429):
                # Unauthenticated search is rate limited. Back off instead of
                # hammering the API; a later poll will retry.
                log.warning(
                    "github rate limited",
                    source=self.name,
                    endpoint=kind,
                    status=resp.status_code,
                )
                break
            if resp.status_code >= 400:
                log.warning(
                    "github bad status",
                    source=self.name,
                    endpoint=kind,
                    status=resp.status_code,
                )
                continue

            try:
                payload = resp.json()
            except Exception as exc:
                log.warning(
                    "github invalid json",
                    source=self.name,
                    endpoint=kind,
                    error=str(exc)[:200],
                )
                continue
            if not isinstance(payload, dict):
                continue
            records = payload.get("items")
            if not isinstance(records, list):
                continue

            for rec in records:
                try:
                    if not isinstance(rec, dict):
                        continue
                    created = rec.get("created_at")
                    posted = _parse_ts(created)
                    # Track the newest record across *all* results so the
                    # cursor advances even past items without a referral link.
                    if posted is not None and (newest_dt is None or posted > newest_dt):
                        newest_dt = posted
                        newest_str = created if isinstance(created, str) else None
                    if cursor_ts is not None and posted is not None and posted <= cursor_ts:
                        continue
                    item = builder(rec, now, posted)
                    if item is None:
                        continue
                    if REFERRAL_SUBSTRING not in item.text.lower():
                        continue
                    items.append(item)
                except Exception as exc:  # one malformed record must not drop the rest
                    log.warning(
                        "github bad record",
                        source=self.name,
                        endpoint=kind,
                        error=str(exc)[:200],
                    )
                    continue

        return items, newest_str

    def _build_issue(
        self, rec: dict, now: datetime, posted: datetime | None
    ) -> RawItem | None:
        html_url = rec.get("html_url")
        if not isinstance(html_url, str) or not html_url:
            return None
        user = rec.get("user") or {}
        author = _text(user.get("login")) if isinstance(user, dict) else ""
        title = _text(rec.get("title"))
        body = _text(rec.get("body"))
        text = f"{title}\n{body}\n{html_url}"
        return RawItem(
            source=self.name,
            item_id=f"issue/{html_url}",
            author=author,
            text=text,
            permalink=html_url,
            posted_at=posted,
            fetched_at=now,
        )

    def _build_gist(
        self, rec: dict, now: datetime, posted: datetime | None
    ) -> RawItem | None:
        html_url = rec.get("html_url")
        if not isinstance(html_url, str) or not html_url:
            return None
        owner = rec.get("owner") or {}
        author = _text(owner.get("login")) if isinstance(owner, dict) else ""
        description = _text(rec.get("description"))
        text = f"{description}\n{html_url}"
        gid = _text(rec.get("id")) or html_url
        return RawItem(
            source=self.name,
            item_id=f"gist/{gid}",
            author=author,
            text=text,
            permalink=html_url,
            posted_at=posted,
            fetched_at=now,
        )
