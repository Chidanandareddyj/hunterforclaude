"""Tests for the general web-search collector (mocked HTTP only, no network)."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
import pytest

from hunter.collectors.web_search import WebSearchCollector, WebSearchSource

ALLOW_ROBOTS = "User-agent: *\nAllow: /\n"
DISALLOW_ROBOTS = "User-agent: *\nDisallow: /html/\nDisallow: /search\n"


class MockTransport(httpx.AsyncBaseTransport):
    """Tiny async transport that routes requests to a responder callable."""

    def __init__(self, responder) -> None:
        self.responder = responder
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)


def make_client(ddg_html: str = "", bing_html: str = "", *, ddg_status: int = 200,
                bing_status: int = 200, robots: str = ALLOW_ROBOTS):
    def responder(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text=robots, request=request)
        if path == "/html/":
            return httpx.Response(ddg_status, text=ddg_html, request=request)
        if path == "/search":
            return httpx.Response(bing_status, text=bing_html, request=request)
        return httpx.Response(404, text="", request=request)

    transport = MockTransport(responder)
    return httpx.AsyncClient(transport=transport), transport


def ddg_result(target: str, title: str = "Free pass", snippet: str | None = None) -> str:
    snippet = snippet or f"Grab it at {target} today"
    uddg = quote(target, safe="")
    return (
        '<div class="result results_links web-result">'
        f'<a class="result__a" href="//duckduckgo.com/l/?uddg={uddg}&amp;rut=abc">{title}</a>'
        f'<a class="result__snippet">{snippet}</a>'
        "</div>"
    )


def test_web_search_source_defaults():
    cfg = WebSearchSource()
    assert cfg.enabled is False
    assert cfg.interval_seconds == 300.0
    assert cfg.trust == 0.5
    assert cfg.query == 'site:claude.ai/referral "claude.ai/referral"'
    # Constructor works with all defaults and no DB/client.
    collector = WebSearchCollector(None, cfg)
    assert collector.name == "web_search"
    assert collector.enabled is False
    assert collector.interval_seconds == 300.0
    assert collector.trust == 0.5


@pytest.mark.asyncio
async def test_ddg_decodes_uddg_and_emits_item():
    target = "https://claude.ai/referral/DDGCODE123"
    html = f"<html><body>{ddg_result(target, title='Free Claude pass')}</body></html>"
    client, _ = make_client(ddg_html=html)
    collector = WebSearchCollector(None, WebSearchSource())

    items = await collector.fetch_items(client)
    await client.aclose()

    assert len(items) == 1
    item = items[0]
    assert item.source == "web_search"
    assert item.author == "web_search"
    assert item.item_id == hashlib.sha1(target.encode("utf-8")).hexdigest()
    assert item.urls == [target]
    assert item.permalink == target
    assert item.posted_at is None
    assert item.fetched_at.tzinfo == UTC
    assert "Free Claude pass" in item.text
    assert target in item.text


@pytest.mark.asyncio
async def test_bing_decodes_redirect_and_emits_item():
    target = "https://claude.ai/referral/BINGCODE123"
    encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    html = (
        "<html><body>"
        '<li class="b_algo">'
        f'<h2><a href="https://www.bing.com/ck/a?!&amp;&amp;u=a1{encoded}">Claude pass</a></h2>'
        '<div class="b_caption"><p>Share this guest pass</p></div>'
        "</li>"
        "</body></html>"
    )
    client, _ = make_client(bing_html=html)
    collector = WebSearchCollector(None, WebSearchSource())

    items = await collector.fetch_items(client)
    await client.aclose()

    assert len(items) == 1
    assert items[0].urls == [target]
    assert "Claude pass" in items[0].text


@pytest.mark.asyncio
async def test_referral_in_snippet_counts_even_if_target_unrelated():
    # DDG target is a forum thread, but the snippet carries the referral link.
    html = (
        "<html><body>"
        + ddg_result(
            "https://example.com/thread/1",
            title="thread",
            snippet="someone posted https://claude.ai/referral/SNIPPET123 here",
        )
        + "</body></html>"
    )
    client, _ = make_client(ddg_html=html)
    collector = WebSearchCollector(None, WebSearchSource())
    items = await collector.fetch_items(client)
    await client.aclose()

    assert len(items) == 1
    assert items[0].urls == ["https://example.com/thread/1"]
    assert "SNIPPET123" in items[0].text


@pytest.mark.asyncio
async def test_no_referral_yields_no_items():
    html = f"<html><body>{ddg_result('https://example.com/nope', title='nothing')}</body></html>"
    client, _ = make_client(ddg_html=html)
    collector = WebSearchCollector(None, WebSearchSource())
    items = await collector.fetch_items(client)
    await client.aclose()
    assert items == []


@pytest.mark.asyncio
async def test_robots_disallow_skips_engine_without_fetching_results():
    target = "https://claude.ai/referral/DISALLOWED"
    html = f"<html><body>{ddg_result(target)}</body></html>"
    client, transport = make_client(ddg_html=html, bing_html=html, robots=DISALLOW_ROBOTS)
    collector = WebSearchCollector(None, WebSearchSource())
    items = await collector.fetch_items(client)
    await client.aclose()

    assert items == []
    fetched_paths = [r.url.path for r in transport.requests]
    assert "/html/" not in fetched_paths
    assert "/search" not in fetched_paths
    assert fetched_paths.count("/robots.txt") == 2


@pytest.mark.asyncio
async def test_non_200_engine_response_is_skipped():
    target = "https://claude.ai/referral/BLOCKED1"
    # DDG serves a challenge page (202); Bing returns an empty result set.
    client, _ = make_client(
        ddg_html=f"<html><body>{ddg_result(target)}</body></html>",
        ddg_status=202,
    )
    collector = WebSearchCollector(None, WebSearchSource())
    items = await collector.fetch_items(client)
    await client.aclose()
    assert items == []


@pytest.mark.asyncio
async def test_malformed_result_and_duplicates_are_handled():
    target = "https://claude.ai/referral/DUPCODE123"
    good = ddg_result(target)
    malformed = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=%ZZnot-a-url">bad</a>'
    )
    html = f"<html><body>{good}{malformed}{good}</body></html>"
    client, _ = make_client(ddg_html=html)
    collector = WebSearchCollector(None, WebSearchSource())

    items = await collector.fetch_items(client)
    await client.aclose()

    # The duplicate target is emitted once; the malformed result is ignored.
    assert len(items) == 1
    assert items[0].urls == [target]


@pytest.mark.asyncio
async def test_fetched_at_is_populated_on_items():
    target = "https://claude.ai/referral/TIMECODE1"
    html = f"<html><body>{ddg_result(target)}</body></html>"
    client, _ = make_client(ddg_html=html)
    before = datetime.now(UTC)
    collector = WebSearchCollector(None, WebSearchSource())
    items = await collector.fetch_items(client)
    await client.aclose()
    assert len(items) == 1
    assert items[0].fetched_at >= before
