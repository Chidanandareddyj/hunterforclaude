"""Bluesky public AppView search collector tests (mocked HTTP only)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from hunter.collectors.bluesky_search import (
    FALLBACK_ENDPOINT,
    BlueskySearchCollector,
    BlueskySearchSource,
)
from hunter.models import RawItem

SEARCH_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"


def make_client(responder) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(responder),
        follow_redirects=False,
    )


def post(
    *,
    rkey: str = "3n2gvlclcq22n",
    handle: str = "alice.bsky.social",
    did: str = "did:plc:alice",
    text: str = "Referral if anyone wants it claude.ai/referral/LIVECODE123",
    created: str = "2026-11-19T18:00:00.123Z",
    indexed: str = "2026-11-19T18:00:05.000Z",
    external_uri: str | None = None,
    facet_uri: str | None = None,
) -> dict:
    record: dict = {"$type": "app.bsky.feed.post", "text": text, "createdAt": created}
    if external_uri:
        record["embed"] = {
            "$type": "app.bsky.embed.external",
            "external": {
                "title": "Join Claude!",
                "description": "You've been invited to try Claude",
                "uri": external_uri,
            },
        }
    if facet_uri:
        record["facets"] = [
            {
                "index": {"byteStart": 0, "byteEnd": 1},
                "features": [{"$type": "app.bsky.richtext.facet#link", "uri": facet_uri}],
            }
        ]
    return {
        "uri": f"at://{did}/app.bsky.feed.post/{rkey}",
        "cid": "bafyreEXAMPLE",
        "author": {"did": did, "handle": handle},
        "record": record,
        "indexedAt": indexed,
    }


class FakeDB:
    """Minimal DB stub: poll() only touches get_cursor/save_cursor."""

    def __init__(self, cursor: str | None = None) -> None:
        self.cursor = cursor
        self.saved: list[str | None] = []

    async def get_cursor(self, name: str) -> str | None:
        return self.cursor

    async def save_cursor(self, name: str, cursor: str | None) -> None:
        self.saved.append(cursor)
        self.cursor = cursor


def test_config_defaults_are_self_contained():
    cfg = BlueskySearchSource()
    assert cfg.enabled is True
    assert cfg.interval_seconds == 60.0
    assert cfg.trust == 0.7
    assert cfg.endpoint == SEARCH_URL
    assert cfg.query == "claude.ai/referral"


@pytest.mark.asyncio
async def test_fetch_posts_parses_referral_posts_and_filters_others():
    good = post(
        external_uri="https://claude.ai/referral/FULLCODE99?s=claude_ai",
        facet_uri="https://claude.ai/referral/FULLCODE99",
    )
    # Truncated text plus a clear link card — still a valid referral post.
    clipped = post(
        rkey="rkey2",
        handle="bob.bsky.social",
        text="pass claude.ai/referral/CUT...",
        external_uri="https://claude.ai/referral/CUTCODE77",
        created="2026-11-19T17:00:00Z",
    )
    irrelevant = post(rkey="rkey3", text="just saying hello, no link here")

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"posts": [good, clipped, irrelevant], "cursor": "opaque-next-page"},
            request=request,
        )

    async with make_client(responder) as client:
        col = BlueskySearchCollector(None, BlueskySearchSource(), client=client)
        items = await col.fetch_posts(client)

    assert len(items) == 2
    assert all(isinstance(it, RawItem) for it in items)
    first = items[0]
    assert first.source == "bluesky_search"
    assert first.item_id == "at://did:plc:alice/app.bsky.feed.post/3n2gvlclcq22n"
    assert first.author == "alice.bsky.social"
    assert first.permalink == "https://bsky.app/profile/alice.bsky.social/post/3n2gvlclcq22n"
    assert "https://claude.ai/referral/FULLCODE99" in first.urls
    assert "Join Claude!" in first.text  # embed external text included
    assert first.posted_at is not None
    assert first.posted_at.year == 2026
    assert any("CUTCODE77" in u for u in items[1].urls)
    # The opaque API cursor is intentionally ignored: paginated requests get a
    # 403 from the AppView. Progress is tracked by newest post timestamp.
    assert col.next_cursor == "2026-11-19T18:00:00.123Z"


@pytest.mark.asyncio
async def test_fetch_posts_falls_back_to_createdat_cursor_when_absent():
    newest = post(rkey="newest", text="https://claude.ai/referral/NEWEST1", created="2026-11-19T20:00:00Z")
    older = post(rkey="older", text="https://claude.ai/referral/OLDER1", created="2026-11-18T09:00:00Z")

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"posts": [older, newest]}, request=request)

    async with make_client(responder) as client:
        col = BlueskySearchCollector(None, BlueskySearchSource(), client=client)
        items = await col.fetch_posts(client)

    assert len(items) == 2
    assert col.next_cursor == "2026-11-19T20:00:00Z"


@pytest.mark.asyncio
async def test_poll_uses_timestamp_cursor_without_paginating():
    calls: list[httpx.URL] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(
            200,
            json={
                "posts": [post(text="https://claude.ai/referral/POLLCODE1")],
                "cursor": "opaque-next-page",
            },
            request=request,
        )

    db = FakeDB()
    async with make_client(responder) as client:
        col = BlueskySearchCollector(db, BlueskySearchSource(), client=client)
        first = await col.poll()
        assert len(first) == 1
        assert db.saved == ["2026-11-19T18:00:00.123Z"]
        assert "cursor" not in calls[0].params

        # The same post is filtered out client-side on the next poll; the
        # request still has no `cursor` param (that would trigger a 403).
        second = await col.poll()
        assert second == []
        assert "cursor" not in calls[1].params


@pytest.mark.asyncio
async def test_fallback_endpoint_used_when_primary_is_blocked():
    seen: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "public.api.bsky.app":
            return httpx.Response(403, text="Forbidden", request=request)
        return httpx.Response(
            200,
            json={"posts": [post(text="https://claude.ai/referral/FALLBACK1")]},
            request=request,
        )

    async with make_client(responder) as client:
        col = BlueskySearchCollector(None, BlueskySearchSource(), client=client)
        items = await col.fetch_posts(client)

    assert len(items) == 1
    assert any(FALLBACK_ENDPOINT in url for url in seen)
    assert seen[0].startswith(SEARCH_URL)


@pytest.mark.asyncio
async def test_malformed_records_and_payloads_do_not_raise():
    def bad_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>", request=request)

    async with make_client(bad_json) as client:
        col = BlueskySearchCollector(None, BlueskySearchSource(), client=client)
        assert await col.fetch_posts(client) == []
        assert col.next_cursor is None

    def rate_limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "RateLimitExceeded"}, request=request)

    async with make_client(rate_limited) as client:
        col = BlueskySearchCollector(None, BlueskySearchSource(), client=client)
        assert await col.fetch_posts(client) == []

    def mixed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "posts": [
                    None,
                    "not a dict",
                    {},
                    {"uri": "at://did:plc:x/app.bsky.feed.post/1", "record": None},
                    post(rkey="good", text="https://claude.ai/referral/GOODCODE1"),
                ]
            },
            request=request,
        )

    async with make_client(mixed) as client:
        col = BlueskySearchCollector(None, BlueskySearchSource(), client=client)
        items = await col.fetch_posts(client)

    assert len(items) == 1
    assert "GOODCODE1" in items[0].text


@pytest.mark.asyncio
async def test_empty_posts_and_missing_handle_are_handled():
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"posts": [], "cursor": "cursor-x"}, request=request)

    async with make_client(responder) as client:
        col = BlueskySearchCollector(None, BlueskySearchSource(), client=client)
        assert await col.fetch_posts(client) == []
        assert col.next_cursor is None

    no_author = post(text="https://claude.ai/referral/NOAUTHOR1")
    no_author["author"] = {}

    def responder2(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"posts": [no_author]}, request=request)

    async with make_client(responder2) as client:
        col = BlueskySearchCollector(None, BlueskySearchSource(), client=client)
        assert await col.fetch_posts(client) == []


@pytest.mark.asyncio
async def test_posted_at_falls_back_to_indexed_at():
    p = post(text="https://claude.ai/referral/DATED1")
    p["record"].pop("createdAt")

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"posts": [p]}, request=request)

    async with make_client(responder) as client:
        col = BlueskySearchCollector(None, BlueskySearchSource(), client=client)
        items = await col.fetch_posts(client)

    assert items[0].posted_at == datetime(2026, 11, 19, 18, 0, 5, tzinfo=UTC)
