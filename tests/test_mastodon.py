"""Mastodon collector tests against canned HTTP responses (no live network)."""

from __future__ import annotations

import json

import httpx
import pytest

from hunter.collectors.mastodon import MastodonCollector, MastodonSource


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, responder) -> None:
        self.responder = responder
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)


def make_client(responder) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=RecordingTransport(responder), follow_redirects=False
    )


def make_status(
    sid: str,
    code: str,
    *,
    acct: str = "alice@mastodon.social",
    visibility: str | None = "public",
    reblog=None,
    created: str = "2026-11-19T12:00:00Z",
    url: str | None = None,
    content: str | None = None,
) -> dict:
    if content is None:
        content = (
            '<p>Grab it <a href="https://claude.ai/referral/'
            f'{code}" target="_blank" rel="nofollow noopener">'
            f"https://claude.ai/referral/{code}</a></p>"
        )
    status = {
        "id": sid,
        "account": {"acct": acct, "username": acct.split("@")[0]},
        "content": content,
        "created_at": created,
        "url": url or f"https://mastodon.social/@{acct.split('@')[0]}/{sid}",
        "visibility": visibility,
        "reblog": reblog,
    }
    return status


def source(**kwargs) -> MastodonSource:
    base: dict = {"instances": ["mastodon.social"], "hashtags": ["ClaudeCode"]}
    base.update(kwargs)
    return MastodonSource(**base)


def respond_timeline(statuses, search=None):
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/search":
            return httpx.Response(
                200, json={"statuses": search or [], "accounts": []}, request=request
            )
        return httpx.Response(200, json=statuses, request=request)

    return responder


# -- config ------------------------------------------------------------------


def test_source_defaults():
    cfg = MastodonSource()
    assert cfg.enabled is True
    assert cfg.interval_seconds == 60.0
    assert cfg.trust == 0.6
    assert cfg.instances == ["mastodon.social", "fosstodon.org", "techhub.social"]
    assert cfg.hashtags == ["ClaudeCode", "ClaudeAI"]
    assert cfg.query == "claude.ai/referral"


# -- parsing -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_parses_hashtag_statuses():
    statuses = [make_status("100", "CODEAAA"), make_status("101", "CODEBBB")]
    client = make_client(respond_timeline(statuses))
    col = MastodonCollector(None, source(), client)

    items = await col.fetch_items(client)

    assert len(items) == 2
    first = items[0]
    assert first.source == "mastodon"
    assert first.item_id == "mastodon.social/100"
    assert first.author == "alice@mastodon.social"
    assert first.permalink == "https://mastodon.social/@alice/100"
    assert first.posted_at is not None and first.posted_at.year == 2026
    assert "https://claude.ai/referral/CODEAAA" in first.text
    assert "href=" in first.text  # raw content kept for href extraction
    assert "https://claude.ai/referral/CODEAAA" in first.urls


@pytest.mark.asyncio
async def test_html_entities_unescaped_in_text():
    status = make_status(
        "102",
        "ESCCODE1",
        content=(
            "<p>ref https:&#x2F;&#x2F;claude.ai&#x2F;referral&#x2F;ESCCODE1 "
            "&amp; more</p>"
        ),
    )
    client = make_client(respond_timeline([status]))
    col = MastodonCollector(None, source(), client)

    items = await col.fetch_items(client)

    assert len(items) == 1
    assert "https://claude.ai/referral/ESCCODE1" in items[0].text
    assert "&amp;" not in items[0].text


@pytest.mark.asyncio
async def test_skips_reblogs_non_public_and_non_referral():
    statuses = [
        make_status("200", "GOODCODE"),
        make_status("201", "BOOSTCOD", reblog={"id": "x"}),
        make_status("202", "PRIVCODE", visibility="private"),
        make_status("203", "UNLICODE", visibility="unlisted"),
        make_status("204", "NOPECODE", content="<p>just chatting</p>"),
    ]
    client = make_client(respond_timeline(statuses))
    col = MastodonCollector(None, source(), client)

    items = await col.fetch_items(client)

    assert len(items) == 1
    assert "GOODCODE" in items[0].urls[0]


@pytest.mark.asyncio
async def test_search_endpoint_statuses_parsed():
    search_results = [make_status("300", "SEARCHC1")]
    client = make_client(respond_timeline([], search=search_results))
    col = MastodonCollector(None, source(hashtags=[]), client)

    items = await col.fetch_items(client)

    assert len(items) == 1
    assert items[0].item_id == "mastodon.social/300"
    assert "SEARCHC1" in items[0].urls[0]


@pytest.mark.asyncio
async def test_search_request_uses_expected_params():
    client = make_client(respond_timeline([]))
    col = MastodonCollector(None, source(), client)
    await col.fetch_items(client)

    from typing import cast

    transport = cast(RecordingTransport, client._transport)
    search_requests = [r for r in transport.requests if r.url.path == "/api/v2/search"]
    assert len(search_requests) == 1
    params = search_requests[0].url.params
    assert params["q"] == "claude.ai/referral"
    assert params["type"] == "statuses"
    assert params["resolve"] == "false"


# -- cursor ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_skips_old_ids_and_persists_per_instance():
    statuses = [make_status("10", "OLDCODE1"), make_status("11", "NEWCODE1")]
    client = make_client(respond_timeline(statuses))
    col = MastodonCollector(None, source(), client)

    first = await col.fetch_items(client)
    assert len(first) == 2
    cursor = col._last_cursor
    assert cursor is not None
    assert json.loads(cursor) == {"mastodon.social": "11"}

    # Same data + cursor: both ids are <= cursor, so nothing is re-emitted.
    assert await col.fetch_items(client, cursor) == []

    # A genuinely newer status is emitted and advances the cursor.
    statuses.append(make_status("12", "FRESHCOD"))
    newer = await col.fetch_items(client, cursor)
    assert len(newer) == 1
    assert "FRESHCOD" in newer[0].urls[0]
    assert col._last_cursor is not None
    assert json.loads(col._last_cursor)["mastodon.social"] == "12"


@pytest.mark.asyncio
async def test_cursor_isolates_instances():
    def responder(request):
        if request.url.host == "fosstodon.org":
            return httpx.Response(200, json=[make_status("7", "FOSSCODE")], request=request)
        return httpx.Response(200, json=[make_status("99", "MASTCODE")], request=request)

    client = make_client(responder)
    cfg = source(instances=["mastodon.social", "fosstodon.org"], hashtags=["ClaudeCode"])
    col = MastodonCollector(None, cfg, client)

    items = await col.fetch_items(client)
    assert len(items) == 2
    assert col._last_cursor is not None
    assert json.loads(col._last_cursor) == {
        "mastodon.social": "99",
        "fosstodon.org": "7",
    }


@pytest.mark.asyncio
async def test_poll_loads_and_saves_cursor(tmp_path):
    from hunter.db import Database

    statuses = [make_status("55", "POLLCODE")]
    client = make_client(respond_timeline(statuses))
    db = Database(tmp_path / "mastodon.db")
    await db.connect()
    await db.upsert_source("mastodon")
    col = MastodonCollector(db, source(), client)

    try:
        items = await col.poll()
        assert len(items) == 1
        assert await db.get_cursor("mastodon") == '{"mastodon.social":"55"}'
        # Second poll with the persisted cursor yields no duplicates.
        assert await col.poll() == []
    finally:
        await db.close()


# -- defensiveness -----------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limited_and_malformed_responses_do_not_raise():
    client = make_client(lambda request: httpx.Response(429, text="slow down", request=request))
    col = MastodonCollector(None, source(), client)
    assert await col.fetch_items(client) == []

    client2 = make_client(lambda request: httpx.Response(200, text="not json", request=request))
    col2 = MastodonCollector(None, source(), client2)
    assert await col2.fetch_items(client2) == []

    client3 = make_client(lambda request: httpx.Response(500, json={}, request=request))
    col3 = MastodonCollector(None, source(), client3)
    assert await col3.fetch_items(client3) == []


@pytest.mark.asyncio
async def test_malformed_single_status_is_skipped():
    broken = {"id": "6", "account": "not-a-dict", "content": 12345}
    good = make_status("7", "OKCODE1")
    client = make_client(respond_timeline([broken, {"id": "5"}, good]))
    col = MastodonCollector(None, source(), client)

    items = await col.fetch_items(client)

    assert len(items) == 1
    assert "OKCODE1" in items[0].urls[0]
