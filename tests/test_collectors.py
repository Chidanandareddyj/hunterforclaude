"""Collector tests against recorded responses (no live network in unit tests)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from hunter.collectors.claudecoworkcourse import ClaudeCoworkCourseCollector
from hunter.collectors.hackernews import HackerNewsCollector
from hunter.collectors.lemmy import LemmyCollector
from hunter.collectors.telegram_preview import TelegramPreviewCollector
from hunter.config import (
    ClaudeCoworkCourseSource,
    HackerNewsSource,
    LemmySource,
    TelegramPreviewSource,
)


class RecordingTransport(httpx.AsyncBaseTransport):
    """Records requests and returns canned responses."""

    def __init__(self, responder) -> None:
        self.responder = responder
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)


def make_collector(cls, cfg, responder, tmp_path):
    transport = RecordingTransport(responder)
    client = httpx.AsyncClient(transport=transport, follow_redirects=False)
    from hunter.db import Database

    db = Database(tmp_path / "collector.db")
    return cls(db, cfg, client), db, transport


# -- Lemmy -------------------------------------------------------------------

LEMMY_SEARCH = {
    "Posts": {
        "id": 7,
        "name": "Guest pass",
        "body": "here https://claude.ai/referral/LEMMYCODE1",
        "url": "",
        "published": "2026-11-17T11:59:00Z",
        "creator_id": 1,
    },
    "creator": {"name": "giver"},
}


@pytest.mark.asyncio
async def test_lemmy_parses_posts(tmp_path):
    def responder(request):
        return httpx.Response(
            200,
            json={
                "posts": [{"post": LEMMY_SEARCH["Posts"], "creator": LEMMY_SEARCH["creator"]}],
                "comments": [],
            },
            request=request,
        )

    collector, db, _ = make_collector(
        LemmyCollector,
        LemmySource(enabled=True, base_url="https://lemmy.example", community="claudeai"),
        responder,
        tmp_path,
    )
    await db.connect()
    await db.upsert_source("lemmy")
    items = await collector.poll()
    assert len(items) == 1
    assert items[0].author == "giver"
    assert "LEMMYCODE1" in items[0].text
    assert items[0].posted_at is not None
    # Cursor saved; second poll without new data returns nothing new.
    cursor = await db.get_cursor("lemmy")
    assert cursor
    items2 = await collector.poll()
    assert items2 == []
    await db.close()


# -- HN ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hackernews_parses_hits_and_cursors(tmp_path):
    hits = [
        {
            "objectID": "42",
            "author": "hnuser",
            "comment_text": "take mine https://claude.ai/referral/HNCODE1234",
            "created_at_i": 1700000000,
        }
    ]

    def responder(request):
        return httpx.Response(200, json={"hits": hits}, request=request)

    collector, db, transport = make_collector(
        HackerNewsCollector,
        HackerNewsSource(enabled=True, algolia_url="https://hn.example/search"),
        responder,
        tmp_path,
    )
    await db.connect()
    await db.upsert_source("hackernews")
    items = await collector.poll()
    assert items[0].author == "hnuser"
    assert await db.get_cursor("hackernews") == "1700000000"
    # Second poll adds numericFilter above cursor
    await collector.poll()
    assert "numericFilters=created_at_i%3E1700000000" in str(transport.requests[-1].url)
    await db.close()


# -- claudecoworkcourse ------------------------------------------------------


@pytest.mark.asyncio
async def test_coworkcourse_robots_disallow_disables(tmp_path):
    def responder(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /claude-guest-passes", request=request)
        return httpx.Response(200, text="<html></html>", request=request)

    collector, db, _ = make_collector(
        ClaudeCoworkCourseCollector,
        ClaudeCoworkCourseSource(enabled=True, url="https://dir.example/claude-guest-passes"),
        responder,
        tmp_path,
    )
    await db.connect()
    await db.upsert_source("claudecoworkcourse")
    items = await collector.poll()
    assert items == []
    assert collector.enabled is False
    await db.close()


@pytest.mark.asyncio
async def test_coworkcourse_parses_codes_and_relative_times(tmp_path):
    page = """
    <html><body>
      <div class="card">
        <a href="https://claude.ai/referral/DIRCODE123">link</a>
        <span>3 hours ago</span>
      </div>
    </body></html>
    """

    def responder(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        return httpx.Response(200, text=page, request=request)

    collector, db, _ = make_collector(
        ClaudeCoworkCourseCollector,
        ClaudeCoworkCourseSource(enabled=True, url="https://dir.example/claude-guest-passes"),
        responder,
        tmp_path,
    )
    await db.connect()
    await db.upsert_source("claudecoworkcourse")
    items = collector.parse(page, now=datetime.now(UTC))
    assert len(items) == 1
    assert items[0].urls == ["https://claude.ai/referral/DIRCODE123"]
    assert items[0].posted_at is not None
    # Relative time is approximate: ~3h ago minus the safety margin.
    age = (items[0].fetched_at - items[0].posted_at).total_seconds()
    assert 3 * 3600 + 60 <= age <= 3 * 3600 + 8 * 60
    await db.close()


@pytest.mark.asyncio
async def test_coworkcourse_skips_retired_rows(tmp_path):
    page = """
    <html><body>
      <table>
        <tr class="gpx-row gpx-row--retired" hidden="">
          <td>https://claude.ai/referral/RETIRED123</td>
          <td>Retired</td>
          <td>100 / 100</td>
          <td>5mo ago</td>
          <td><a href="https://claude.ai/referral/RETIRED123">Try this</a></td>
        </tr>
      </table>
      <ul class="gpx-retired-list">
        <li class="gpx-retired-row">
          <span>kszk3100 clicks · added 23h ago</span>
          <a href="https://claude.ai/referral/RETIRED456">Try</a>
        </li>
      </ul>
      <div class="card">
        <a href="https://claude.ai/referral/LIVECODE789">link</a>
        <span>5m ago</span>
      </div>
    </body></html>
    """
    from hunter.db import Database
    db = Database(tmp_path / "test.db")
    collector = ClaudeCoworkCourseCollector(db, ClaudeCoworkCourseSource(enabled=True))
    items = collector.parse(page, now=datetime.now(UTC))
    assert len(items) == 1
    assert items[0].urls == ["https://claude.ai/referral/LIVECODE789"]


@pytest.mark.asyncio
async def test_coworkcourse_parses_months_and_concatenated_text(tmp_path):
    page = """
    <html><body>
      <div class="card">
        <span>added 2mo ago</span><a href="https://claude.ai/referral/MONTHCODE1">Try</a>
      </div>
    </body></html>
    """
    from hunter.db import Database
    db = Database(tmp_path / "test.db")
    collector = ClaudeCoworkCourseCollector(db, ClaudeCoworkCourseSource(enabled=True))
    items = collector.parse(page, now=datetime.now(UTC))
    assert len(items) == 1
    age = (items[0].fetched_at - items[0].posted_at).total_seconds()
    assert age > 50 * 86400  # at least ~50 days old


def test_bluesky_handle_message_camelcase():
    from hunter.collectors.bluesky import BlueskyCollector
    from hunter.config import BlueskySource

    col = BlueskyCollector(None, BlueskySource(enabled=True))
    msg = {
        "kind": "commit",
        "commit": {
            "operation": "create",
            "collection": "app.bsky.feed.post",
            "record": {
                "text": "here is a pass https://claude.ai/referral/ABC123XYZ9",
                "createdAt": "2026-11-19T18:00:00Z",
            },
            "repo": "did:plc:123",
            "rkey": "456",
        },
    }
    items = col._handle_message(msg)
    assert len(items) == 1
    assert items[0].posted_at is not None
    assert items[0].posted_at.year == 2026


# -- Telegram preview ---------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_preview_parses_messages(tmp_path):
    page = """
    <div class="tgme_widget_message_wrap">
      <div class="tgme_widget_message" data-view="1">
        <a href="https://t.me/somechannel/42"><time datetime="2026-11-17T11:59:00+00:00"></time></a>
        <div class="tgme_widget_message_text">Pass: https://claude.ai/referral/TGCODE1234</div>
      </div>
    </div>
    """

    def responder(request):
        return httpx.Response(200, text=page, request=request)

    collector, db, _ = make_collector(
        TelegramPreviewCollector,
        TelegramPreviewSource(enabled=True, channels=["somechannel"]),
        responder,
        tmp_path,
    )
    await db.connect()
    await db.upsert_source("telegram_preview")
    items = await collector.poll()
    assert len(items) == 1
    assert items[0].item_id == "somechannel/42"
    assert items[0].permalink == "https://t.me/somechannel/42"
    # Duplicate parse produces no repeat items.
    items2 = collector.parse("somechannel", page, now=datetime.now(UTC))
    assert items2 == []
    await db.close()


# -- Live-shape regressions ---------------------------------------------------


@pytest.mark.asyncio
async def test_hackernews_unescapes_algolia_html_entities(tmp_path):
    """Algolia escapes URL slashes in comment_text as &#x2F; — the collector
    must unescape them so the pipeline sees a real referral URL."""

    def responder(request):
        if request.url.params.get("tags") != "comment":
            return httpx.Response(200, json={"hits": []}, request=request)
        return httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "objectID": "44068282",
                        "author": "practal",
                        "comment_text": (
                            "Obligatory: <a href=&quot;https:&#x2F;&#x2F;claude.ai"
                            "&#x2F;referral&#x2F;HNESC123&quot;>link</a>"
                        ),
                        "created_at_i": 1747956728,
                    }
                ]
            },
            request=request,
        )

    collector, db, _ = make_collector(
        HackerNewsCollector,
        HackerNewsSource(enabled=True, algolia_url="https://hn.example/search"),
        responder,
        tmp_path,
    )
    await db.connect()
    items = await collector.poll()
    assert len(items) == 1
    assert "https://claude.ai/referral/HNESC123" in items[0].text
    await db.close()


@pytest.mark.asyncio
async def test_coworkcourse_live_rows_full_codes_and_undated(tmp_path):
    """The real page uses tr.gpx-row--unverified with full-length codes (some
    longer than 20 chars) and may have live rows without a visible age."""
    page = """
    <html><body><table>
      <tr class="gpx-row gpx-row--unverified">
        <td class="gpx-cell-url">https://claude.ai/referral/totallynotkridOnDiscord</td>
        <td>Unverified</td><td>—</td><td>1d ago</td>
        <td class="gpx-cell-action"><a href="https://claude.ai/referral/totallynotkridOnDiscord">Try</a></td>
      </tr>
      <tr class="gpx-row gpx-row--unverified">
        <td class="gpx-cell-url">https://claude.ai/referral/LIVEUNDATED</td>
        <td>Unverified</td><td>—</td><td>—</td>
        <td class="gpx-cell-action"><a href="https://claude.ai/referral/LIVEUNDATED">Try</a></td>
      </tr>
    </table></body></html>
    """
    from hunter.db import Database

    db = Database(tmp_path / "test.db")
    collector = ClaudeCoworkCourseCollector(db, ClaudeCoworkCourseSource(enabled=True))
    items = collector.parse(page, now=datetime.now(UTC))
    assert len(items) == 2
    assert items[0].urls == ["https://claude.ai/referral/totallynotkridOnDiscord"]  # not truncated
    assert items[0].posted_at is not None
    assert items[1].urls == ["https://claude.ai/referral/LIVEUNDATED"]
    assert items[1].posted_at is None  # undated live rows are kept, not dropped


@pytest.mark.asyncio
async def test_coworkcourse_skips_real_retired_table_row(tmp_path):
    page = """
    <html><body><table>
      <tr class="gpx-row gpx-row--retired" hidden="">
        <td class="gpx-cell-url">https://claude.ai/referral/DEADCODE1</td>
        <td>Retired</td><td>100 / 100</td><td>5mo ago</td>
        <td class="gpx-cell-action"><a href="https://claude.ai/referral/DEADCODE1">Try</a></td>
      </tr>
      <tr class="gpx-row gpx-row--unverified">
        <td>https://claude.ai/referral/LIVECODE1</td><td>Unverified</td><td>—</td><td>5m ago</td>
        <td><a href="https://claude.ai/referral/LIVECODE1">Try</a></td>
      </tr>
    </table></body></html>
    """
    from hunter.db import Database

    db = Database(tmp_path / "test.db")
    collector = ClaudeCoworkCourseCollector(db, ClaudeCoworkCourseSource(enabled=True))
    items = collector.parse(page, now=datetime.now(UTC))
    assert len(items) == 1
    assert items[0].urls == ["https://claude.ai/referral/LIVECODE1"]


def test_bluesky_external_embed_url_is_a_list():
    from hunter.collectors.bluesky import BlueskyCollector
    from hunter.config import BlueskySource

    col = BlueskyCollector(None, BlueskySource(enabled=True))
    msg = {
        "kind": "commit",
        "time_us": 1795110988494578,
        "commit": {
            "operation": "create",
            "collection": "app.bsky.feed.post",
            "record": {
                "text": "pass",
                "createdAt": "2026-11-19T18:00:00.123Z",
                "embed": {
                    "$type": "app.bsky.embed.external",
                    "external": {"uri": "https://claude.ai/referral/BSKYCODE1"},
                },
            },
            "repo": "did:plc:123",
            "rkey": "456",
        },
    }
    items = col._handle_message(msg)
    assert len(items) == 1
    assert items[0].urls == ["https://claude.ai/referral/BSKYCODE1"]
    assert items[0].posted_at.year == 2026


@pytest.mark.asyncio
async def test_telegram_parses_real_datetime_and_anchor_link(tmp_path):
    """Real t.me/s markup: data-post id, +00:00 datetime, referral link as an
    anchor href with generic visible text."""
    page = """
    <div class="tgme_widget_message_wrap">
      <div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="somechannel/55">
        <a class="tgme_widget_message_date" href="https://t.me/somechannel/55">
          <time datetime="2026-11-19T18:00:00+00:00"></time>
        </a>
        <div class="tgme_widget_message_text js-message_text" dir="auto">
          Grab it <a href="https://claude.ai/referral/TGREAL123">here</a>
        </div>
      </div>
    </div>
    """
    from hunter.db import Database

    db = Database(tmp_path / "test.db")
    collector = TelegramPreviewCollector(db, TelegramPreviewSource(enabled=True, channels=["somechannel"]))
    items = collector.parse("somechannel", page, now=datetime.now(UTC))
    assert len(items) == 1
    assert items[0].posted_at is not None
    assert items[0].posted_at.year == 2026
    assert "https://claude.ai/referral/TGREAL123" in items[0].urls


@pytest.mark.asyncio
async def test_lemmy_falls_back_to_unscoped_search(tmp_path):
    """The configured community can be empty on an instance (claudeai on
    lemmy.world). An empty community search must fall back to all posts."""
    calls = []

    def responder(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "posts": (
                    [] if "community_name" in request.url.params
                    else [{"post": LEMMY_SEARCH["Posts"], "creator": LEMMY_SEARCH["creator"]}]
                ),
                "comments": [],
            },
            request=request,
        )

    collector, db, _ = make_collector(
        LemmyCollector,
        LemmySource(enabled=True, base_url="https://lemmy.example", community="claudeai"),
        responder,
        tmp_path,
    )
    await db.connect()
    await db.upsert_source("lemmy")
    items = await collector.poll()
    assert len(items) == 1
    assert len(calls) >= 2  # community-scoped, then unscoped fallback
    assert "LEMMYCODE1" in items[0].text
    await db.close()

