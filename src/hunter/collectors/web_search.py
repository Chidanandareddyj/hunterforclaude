"""General web-search collector for publicly indexed Claude referral links.

Queries search engines for pages that expose ``https://claude.ai/referral/<code>``
links and emits each hit as a :class:`~hunter.models.RawItem`. Two engines are
implemented: the DuckDuckGo HTML endpoint (primary) and Bing (optional
fallback). Before each engine is queried, :func:`~.base.fetch_robots` is
consulted for that engine's endpoint and the engine is skipped when the path is
disallowed.

Design notes
------------
* **No cursor.** Search result sets are neither chronological nor stable between
  polls, so a cursor would be meaningless. The downstream pipeline de-duplicates
  by referral code (DB layer), so repeated results are harmless. The small
  in-memory ``seen`` set only de-duplicates targets within a single poll.
* **Fragile by nature.** HTML scraping is brittle and engines rate-limit or serve
  challenge pages, so the source ships disabled by default (``enabled=False``).
  Enable it only after a live probe confirms it works from your network.
* **Defensive parsing.** A blocked/malformed engine response or a single bad
  result never aborts the poll — each engine and each result is isolated in its
  own try/except.

DuckDuckGo result links are wrapped as ``//duckduckgo.com/l/?uddg=<encoded-url>``;
the real target lives in the ``uddg`` query parameter and is decoded with
``urllib.parse``. Bing results are parsed from ``li.b_algo`` blocks, and Bing's
``/ck/a?...&u=a1<base64>`` redirect links are decoded to their real target.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import html as html_lib
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from pydantic import BaseModel
from selectolax.parser import HTMLParser

from ..logging_setup import get_logger
from ..models import RawItem
from .base import Collector, fetch_robots

log = get_logger(component="collector.web_search")

_DDG_URL = "https://html.duckduckgo.com/html/"
_BING_URL = "https://www.bing.com/search"

_UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Codes are [A-Za-z0-9_]{6,20} in config.yaml; detection is deliberately a bit
# broader so downstream (which applies the authoritative pattern) decides.
_REFERRAL_RE = re.compile(
    r"(?:https?://)?claude\.ai/referral/([A-Za-z0-9_]{4,60})",
    re.IGNORECASE,
)
_AUTHOR = "web_search"


class WebSearchSource(BaseModel):
    """Config for this source. Self-contained (pydantic only) so the module can
    be imported and tested without touching ``..config``."""

    enabled: bool = False
    interval_seconds: float = 300.0
    trust: float = 0.5
    query: str = 'site:claude.ai/referral "claude.ai/referral"'


class WebSearchCollector(Collector):
    name = "web_search"

    def __init__(self, db, cfg, client=None) -> None:
        super().__init__(db, client)
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.interval_seconds = cfg.interval_seconds
        self.trust = cfg.trust

    async def poll(self) -> list[RawItem]:
        client = await self.client()
        return await self.fetch_items(client)

    async def fetch_items(self, client: httpx.AsyncClient) -> list[RawItem]:
        """Run both engines and return items. Public so a live probe can call it
        directly with an ``httpx.AsyncClient`` and no database."""
        now = datetime.now(UTC)
        items: list[RawItem] = []
        seen: set[str] = set()
        engines = (
            ("duckduckgo", _DDG_URL, self._parse_ddg),
            ("bing", _BING_URL, self._parse_bing),
        )
        for name, endpoint, parser in engines:
            items.extend(await self._fetch_engine(client, name, endpoint, parser, now, seen))
        return items

    async def _fetch_engine(self, client, name, endpoint, parser, now, seen) -> list[RawItem]:
        try:
            policy = await fetch_robots(client, endpoint)
            if not policy.allowed:
                log.info(
                    "robots.txt disallows search endpoint; skipping engine",
                    engine=name,
                    note=policy.note,
                )
                return []
            resp = await client.get(endpoint, params={"q": self.cfg.query}, headers=_UA_HEADERS)
            if resp.status_code != 200:
                log.info(
                    "search engine returned non-200; skipping",
                    engine=name,
                    status=resp.status_code,
                )
                return []
            return parser(HTMLParser(resp.text), now, seen, self.name)
        except Exception as exc:
            log.warning("search engine fetch/parse failed", engine=name, error=str(exc)[:200])
            return []

    # -- DuckDuckGo -----------------------------------------------------------

    @staticmethod
    def _parse_ddg(tree, now, seen, source) -> list[RawItem]:
        items: list[RawItem] = []
        for node in tree.css("a.result__a"):
            item = WebSearchCollector._ddg_result_to_item(node, now, seen, source)
            if item is not None:
                items.append(item)
        return items

    @staticmethod
    def _ddg_result_to_item(node, now, seen, source) -> RawItem | None:
        try:
            title = " ".join((node.text() or "").split())
            target = WebSearchCollector._decode_ddg_href(node.attributes.get("href") or "")
            snippet = WebSearchCollector._find_snippet(node)
            return WebSearchCollector._make_item(target, title, snippet, now, seen, source)
        except Exception:
            return None

    @staticmethod
    def _decode_ddg_href(href: str) -> str:
        """Return the real target from ``//duckduckgo.com/l/?uddg=<encoded>``."""
        if not href:
            return ""
        raw = html_lib.unescape(href)
        with contextlib.suppress(ValueError, TypeError):
            uddg = parse_qs(urlsplit(raw).query, keep_blank_values=True).get("uddg", [])
            if uddg:
                return unquote(uddg[0])
        if raw.startswith("//"):
            return f"https:{raw}"
        return raw

    # -- Bing -----------------------------------------------------------------

    @staticmethod
    def _parse_bing(tree, now, seen, source) -> list[RawItem]:
        items: list[RawItem] = []
        for node in tree.css("li.b_algo"):
            item = WebSearchCollector._bing_result_to_item(node, now, seen, source)
            if item is not None:
                items.append(item)
        return items

    @staticmethod
    def _bing_result_to_item(node, now, seen, source) -> RawItem | None:
        try:
            anchor = node.css_first("h2 a") or node.css_first("a")
            if anchor is None:
                return None
            title = " ".join((anchor.text() or "").split())
            target = WebSearchCollector._decode_bing_href(anchor.attributes.get("href") or "")
            snip = node.css_first(".b_caption p") or node.css_first("p")
            snippet = " ".join((snip.text() or "").split()) if snip is not None else ""
            return WebSearchCollector._make_item(target, title, snippet, now, seen, source)
        except Exception:
            return None

    @staticmethod
    def _decode_bing_href(href: str) -> str:
        """Bing hides targets behind ``/ck/a?...&u=a1<base64url>``; decode them."""
        if not href:
            return ""
        raw = html_lib.unescape(href)
        with contextlib.suppress(ValueError, TypeError):
            u = parse_qs(urlsplit(raw).query, keep_blank_values=True).get("u", [])
            if u and u[0].startswith("a1"):
                payload = u[0][2:]
                payload += "=" * (-len(payload) % 4)
                return base64.urlsafe_b64decode(payload.encode("ascii")).decode(
                    "utf-8", "replace"
                )
        return raw

    # -- shared ---------------------------------------------------------------

    @staticmethod
    def _find_snippet(node) -> str:
        """Snippets are siblings of ``a.result__a`` in DDG markup; climb a few
        ancestors and take the first snippet found within the result block."""
        ancestor = node.parent
        for _ in range(5):
            if ancestor is None or ancestor.tag in ("body", "html"):
                break
            snip = ancestor.css_first("a.result__snippet, .result__snippet")
            if snip is not None:
                return " ".join((snip.text() or "").split())
            ancestor = ancestor.parent
        return ""

    @staticmethod
    def _make_item(
        target: str,
        title: str,
        snippet: str,
        now: datetime,
        seen: set[str],
        source: str,
    ) -> RawItem | None:
        try:
            if not target or not _REFERRAL_RE.search(f"{target} {snippet} {title}"):
                return None
            item_id = hashlib.sha1(target.encode("utf-8")).hexdigest()
            if item_id in seen:
                return None
            seen.add(item_id)
            return RawItem(
                source=source,
                item_id=item_id,
                author=_AUTHOR,
                text=f"{title} {snippet} {target}",
                urls=[target],
                permalink=target,
                posted_at=None,  # search results rarely give reliable timestamps
                fetched_at=now,
            )
        except Exception:
            return None
