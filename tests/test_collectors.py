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
