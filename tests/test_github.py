"""GitHub collector tests against canned responses (no live network)."""

from __future__ import annotations

import httpx
import pytest

from hunter.collectors.github import GITHUB_HEADERS, GitHubCollector, GitHubSource
from hunter.db import Database


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, responder) -> None:
        self.responder = responder
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)


def make_client(responder):
    transport = RecordingTransport(responder)
    client = httpx.AsyncClient(transport=transport, follow_redirects=False)
    return client, transport


ISSUE = {
    "html_url": "https://github.com/org/repo/issues/1",
    "number": 1,
    "title": "Free guest pass",
    "body": "here https://claude.ai/referral/ISSUECODE1",
    "user": {"login": "alice"},
    "created_at": "2026-11-19T10:00:00Z",
    "repository_url": "https://api.github.com/repos/org/repo",
}

GIST = {
    "html_url": "https://gist.github.com/abc123def456",
    "description": "code https://claude.ai/referral/GISTCODE2 here",
    "owner": {"login": "bob"},
    "created_at": "2026-11-19T11:00:00Z",
    "id": "abc123def456",
}


def _issues_gists_responder(issues, gists):
    def responder(request):
        if request.url.path.endswith("/issues"):
            return httpx.Response(200, json={"items": issues}, request=request)
        if request.url.path.endswith("/gists"):
            return httpx.Response(200, json={"items": gists}, request=request)
        return httpx.Response(404, json={}, request=request)

    return responder


def test_github_source_defaults():
    cfg = GitHubSource()
    assert cfg.enabled is False
    assert cfg.interval_seconds == 300.0
    assert cfg.trust == 0.5
    assert cfg.api_url == "https://api.github.com/search"
    assert cfg.query == "claude.ai/referral"


@pytest.mark.asyncio
async def test_github_fetch_items_parses_issues_and_gists():
    client, transport = make_client(_issues_gists_responder([ISSUE], [GIST]))
    col = GitHubCollector(None, GitHubSource(), client=client)

    items = await col.fetch_items(client)

    assert len(items) == 2
    issue, gist = items
    assert issue.item_id == f"issue/{ISSUE['html_url']}"
    assert issue.author == "alice"
    assert "ISSUECODE1" in issue.text
    assert issue.permalink == ISSUE["html_url"]
    assert issue.posted_at is not None and issue.posted_at.year == 2026
    assert gist.item_id == "gist/abc123def456"
    assert gist.author == "bob"
    assert "GISTCODE2" in gist.text
    assert gist.permalink == GIST["html_url"]

    # Required headers and combined query are sent on each request.
    assert len(transport.requests) == 2
    for req in transport.requests:
        assert req.headers["accept"] == GITHUB_HEADERS["Accept"]
        assert req.headers["x-github-api-version"] == GITHUB_HEADERS["X-GitHub-Api-Version"]
        # Quoted phrase so GitHub matches the exact URL instead of tokenizing it.
        assert req.url.params["q"] == '"claude.ai/referral"'
        assert req.url.params["sort"] == "created"
        assert req.url.params["order"] == "desc"
        assert req.url.params["per_page"] == "50"
    await client.aclose()


@pytest.mark.asyncio
async def test_github_poll_saves_and_filters_by_cursor(tmp_path):
    newer = {**ISSUE, "html_url": "https://github.com/org/repo/issues/9",
             "body": "https://claude.ai/referral/NEWCODE3",
             "created_at": "2026-11-19T12:00:00Z"}
    client, transport = make_client(_issues_gists_responder([ISSUE, newer], [GIST]))
    db = Database(tmp_path / "github.db")
    await db.connect()
    await db.upsert_source("github")
    col = GitHubCollector(db, GitHubSource(enabled=True), client=client)

    first = await col.poll()
    assert len(first) == 3
    assert await db.get_cursor("github") == "2026-11-19T12:00:00Z"

    # Second poll: everything is at or below the cursor, so nothing new.
    second = await col.poll()
    assert second == []

    # An intermediate cursor keeps only the strictly newer items.
    items = await col.fetch_items(client, cursor="2026-11-19T10:30:00Z")
    assert len(items) == 2
    await db.close()
    await client.aclose()


@pytest.mark.asyncio
async def test_github_rate_limit_is_graceful():
    calls: list[httpx.Request] = []

    def responder(request):
        calls.append(request)
        return httpx.Response(403, json={"message": "rate limit exceeded"}, request=request)

    client, _ = make_client(responder)
    col = GitHubCollector(None, GitHubSource(), client=client)
    assert await col.fetch_items(client) == []
    # Stop after the first rate-limited response instead of hammering the API.
    assert len(calls) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_github_skips_malformed_and_nonreferral_records():
    no_link = {**ISSUE, "html_url": "https://github.com/org/repo/issues/2",
               "body": "nothing useful here", "user": "not-a-dict"}
    bad_date = {**ISSUE, "html_url": "https://github.com/org/repo/issues/3",
                "body": "https://claude.ai/referral/BADDATE1",
                "created_at": "not-a-timestamp"}
    issues = [None, "junk", 42, {}, no_link, bad_date]

    client, _ = make_client(_issues_gists_responder(issues, []))
    col = GitHubCollector(None, GitHubSource(), client=client)
    items = await col.fetch_items(client)

    assert len(items) == 1
    assert "BADDATE1" in items[0].text
    assert items[0].posted_at is None
    await client.aclose()


@pytest.mark.asyncio
async def test_github_handles_bad_json_empty_and_server_errors():
    def responder(request):
        if request.url.path.endswith("/issues"):
            return httpx.Response(200, text="not json", request=request)
        return httpx.Response(500, json={}, request=request)

    client, _ = make_client(responder)
    col = GitHubCollector(None, GitHubSource(), client=client)
    assert await col.fetch_items(client) == []
    await client.aclose()

    client, _ = make_client(_issues_gists_responder([], []))
    col = GitHubCollector(None, GitHubSource(), client=client)
    assert await col.fetch_items(client) == []
    await client.aclose()
