"""Reddit collector tests: fully mocked HTTP, no live network."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from hunter.collectors.reddit import RedditCollector, RedditSource

REFERRAL = "https://claude.ai/referral/REDDITCODE1"


class FakeDB:
    """Minimal cursor store; poll() only needs these two methods."""

    def __init__(self) -> None:
        self.cursors: dict[str, str | None] = {}

    async def get_cursor(self, name: str) -> str | None:
        return self.cursors.get(name)

    async def save_cursor(self, name: str, cursor: str | None) -> None:
        self.cursors[name] = cursor


def make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def make_collector(handler, cfg=None, db=None):
    db = db if db is not None else FakeDB()
    client = make_client(handler)
    return RedditCollector(db, cfg or RedditSource(), client), db, client


def comment(**overrides):
    rec = {
        "id": "c1",
        "author": "alice",
        "body": f"take mine {REFERRAL}",
        "created_utc": 1700000000,
        "permalink": "/r/ClaudeCode/comments/1/x/",
        "subreddit": "ClaudeCode",
    }
    rec.update(overrides)
    return rec


# -- config / constructor ------------------------------------------------------


def test_reddit_source_defaults():
    cfg = RedditSource()
    assert cfg.enabled is True
    assert cfg.interval_seconds == 60.0
    assert cfg.trust == 0.7
    assert cfg.pullpush_url == "https://api.pullpush.io/reddit/search"
    assert cfg.subreddits == ["ClaudeCode", "ClaudeAI", "anthropic"]
    assert cfg.query == "claude.ai/referral"


def test_collector_uses_cfg_values():
    cfg = RedditSource(enabled=False, interval_seconds=123.0, trust=0.25)
    col = RedditCollector(FakeDB(), cfg)
    assert col.name == "reddit"
    assert col.enabled is False
    assert col.interval_seconds == 123.0
    assert col.trust == 0.25


# -- PullPush parsing ----------------------------------------------------------


@pytest.mark.asyncio
async def test_pullpush_parses_comments_and_submissions():
    submission = {
        "id": "s1",
        "author": "bob",
        "title": "free guest pass",
        "selftext": f"here you go {REFERRAL}",
        "created_utc": 1700000100,
        "permalink": "/r/ClaudeAI/comments/2/y/",
        "subreddit": "ClaudeAI",
    }
    urls = []

    def handler(request):
        urls.append(str(request.url))
        if "/reddit/search/comment/" in str(request.url):
            return httpx.Response(200, json={"data": [comment()]}, request=request)
        if "/reddit/search/submission/" in str(request.url):
            return httpx.Response(200, json={"data": [submission]}, request=request)
        return httpx.Response(200, json={"data": []}, request=request)

    col, _, client = make_collector(handler)
    items = await col.fetch_items(client)  # no DB required
    by_id = {item.item_id: item for item in items}
    assert set(by_id) == {"comment/c1", "submission/s1"}
    c = by_id["comment/c1"]
    assert c.source == "reddit"
    assert c.author == "alice"
    assert REFERRAL in c.text
    assert c.urls == [REFERRAL]
    assert c.permalink == "https://www.reddit.com/r/ClaudeCode/comments/1/x/"
    assert c.posted_at == datetime.fromtimestamp(1700000000, UTC)
    assert by_id["submission/s1"].item_id == "submission/s1"
    assert REFERRAL in by_id["submission/s1"].text
    assert all("after=" not in url for url in urls)


@pytest.mark.asyncio
async def test_pullpush_client_side_subreddit_filter_and_dedup():
    def handler(request):
        if "/reddit/search/comment/" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "data": [
                        comment(
                            id="foreign",
                            subreddit="NotConfigured",
                            permalink="/r/NotConfigured/comments/1/",
                        ),
                        comment(id="ok"),
                    ]
                },
                request=request,
            )
        return httpx.Response(200, json={"data": []}, request=request)

    col, _, client = make_collector(handler)
    items = await col.fetch_items(client)
    assert [item.item_id for item in items] == ["comment/ok"]


@pytest.mark.asyncio
async def test_poll_saves_cursor_and_passes_after():
    def handler(request):
        if "/reddit/search/comment/" in str(request.url):
            return httpx.Response(200, json={"data": [comment()]}, request=request)
        return httpx.Response(200, json={"data": []}, request=request)

    col, db, client = make_collector(handler)
    items = await col.poll()
    assert len(items) == 1
    assert db.cursors["reddit"] == "1700000000"

    seen = []

    def empty_handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": []}, request=request)

    col2, _, client2 = make_collector(empty_handler, db=db)
    assert await col2.poll() == []
    assert any("after=1700000000" in url for url in seen)


# -- fallbacks / robustness ----------------------------------------------------


@pytest.mark.asyncio
async def test_reddit_json_fallback_maps_kinds_and_uses_browser_ua():
    ua_seen = []

    def handler(request):
        url = str(request.url)
        if "/reddit/search/" in url:  # PullPush
            return httpx.Response(521, text="cloudflare down", request=request)
        if "/search.json" in url:  # Reddit JSON fallback
            ua_seen.append(request.headers.get("User-Agent", ""))
            return httpx.Response(
                200,
                json={
                    "data": {
                        "children": [
                            {
                                "kind": "t1",
                                "data": {
                                    "id": "jc1",
                                    "author": "u1",
                                    "body": REFERRAL,
                                    "created_utc": 1700000500,
                                    "permalink": "/r/ClaudeCode/comments/1/a/",
                                    "subreddit": "ClaudeCode",
                                },
                            },
                            {
                                "kind": "t3",
                                "data": {
                                    "id": "js1",
                                    "author": "u2",
                                    "selftext": REFERRAL,
                                    "created_utc": 1700000600,
                                    "permalink": "/r/ClaudeAI/comments/2/b/",
                                    "subreddit": "ClaudeAI",
                                },
                            },
                        ]
                    }
                },
                request=request,
            )
        return httpx.Response(200, json={"data": []}, request=request)

    col, _, client = make_collector(handler)
    items = await col.fetch_items(client)
    assert {item.item_id for item in items} == {"comment/jc1", "submission/js1"}
    assert ua_seen and all("Mozilla" in ua for ua in ua_seen)


@pytest.mark.asyncio
async def test_blocked_endpoints_do_not_crash():
    def handler(request):
        url = str(request.url)
        if "/reddit/search/" in url:
            return httpx.Response(521, text="down", request=request)
        if "/search.json" in url:
            return httpx.Response(403, text="blocked", request=request)
        if "arctic-shift" in url:
            return httpx.Response(429, json={"data": None}, request=request)
        return httpx.Response(200, json={"data": []}, request=request)

    col, _, client = make_collector(handler)
    assert await col.fetch_items(client) == []


@pytest.mark.asyncio
async def test_malformed_json_and_bad_records_are_skipped():
    def handler(request):
        if "/reddit/search/comment/" in str(request.url):
            return httpx.Response(200, text="{not json", request=request)
        if "/reddit/search/submission/" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"author": "x", "body": REFERRAL, "created_utc": 1700000000},  # no id
                        {
                            "id": "bad-ts",
                            "author": "y",
                            "body": REFERRAL,
                            "created_utc": "not-a-number",
                            "subreddit": "ClaudeCode",
                        },
                        {
                            "id": "good",
                            "author": "z",
                            "body": REFERRAL,
                            "created_utc": 1700000000,
                            "subreddit": "ClaudeCode",
                            "permalink": "/r/ClaudeCode/comments/9/g/",
                        },
                    ]
                },
                request=request,
            )
        return httpx.Response(200, json={"data": []}, request=request)

    col, _, client = make_collector(handler)
    items = await col.fetch_items(client)
    assert {item.item_id for item in items} == {"submission/bad-ts", "submission/good"}
    bad_ts = next(item for item in items if item.item_id == "submission/bad-ts")
    assert bad_ts.posted_at is None


@pytest.mark.asyncio
async def test_arctic_shift_fallback_finds_referral():
    def handler(request):
        url = str(request.url)
        if "/reddit/search/" in url:
            return httpx.Response(200, json={"data": []}, request=request)
        if "/search.json" in url:
            return httpx.Response(200, json={"data": {"children": []}}, request=request)
        if "arctic-shift" in url and "/posts/search" in url:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "a1",
                            "author": "z",
                            "selftext": f"grab {REFERRAL}",
                            "title": "Guest pass",
                            "created_utc": 1700000000,
                            "permalink": "/r/ClaudeCode/comments/3/z/",
                            "subreddit": "ClaudeCode",
                        }
                    ]
                },
                request=request,
            )
        return httpx.Response(200, json={"data": []}, request=request)

    col, _, client = make_collector(handler)
    items = await col.fetch_items(client)
    assert len(items) == 1
    assert items[0].item_id == "submission/a1"
    assert REFERRAL in items[0].text
