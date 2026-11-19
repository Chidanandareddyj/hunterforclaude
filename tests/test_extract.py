"""Extraction tests: referral matching, host rules, lookalikes, userinfo tricks."""

from __future__ import annotations

from datetime import UTC, datetime

from hunter.extract import compile_pattern, extract_urls, match_referral
from hunter.models import RawItem


def _item(text: str = "", urls: list[str] | None = None) -> RawItem:
    return RawItem(
        source="test", item_id="1", author="a", text=text,
        urls=urls or [], fetched_at=datetime.now(UTC),
    )


PATTERN = compile_pattern("https://claude\\.ai/referral/[A-Za-z0-9_]{6,20}")
PREFIX = "https://claude.ai/referral/"


def test_extracts_bare_url_from_text():
    item = _item(text="grab it https://claude.ai/referral/ABC123XYZ9 now")
    urls = extract_urls(item)
    assert "https://claude.ai/referral/ABC123XYZ9" in urls


def test_extracts_markdown_link():
    item = _item(text="[pass](https://claude.ai/referral/ABC123XYZ9)")
    assert match_referral(extract_urls(item), PATTERN, PREFIX) == (
        "ABC123XYZ9", "https://claude.ai/referral/ABC123XYZ9"
    )


def test_extracts_href_link():
    item = _item(text='<a href="https://claude.ai/referral/ABC123XYZ9">go</a>')
    assert match_referral(extract_urls(item), PATTERN, PREFIX) is not None


def test_match_returns_code_and_url():
    hit = match_referral(["https://claude.ai/referral/ABC_123xyz"], PATTERN, PREFIX)
    assert hit == ("ABC_123xyz", "https://claude.ai/referral/ABC_123xyz")


def test_rejects_lookalike_domain():
    urls = ["https://claude.ai.evil.com/referral/ABC123XYZ9"]
    assert match_referral(urls, PATTERN, PREFIX) is None


def test_rejects_unicode_confusable_host():
    # "claude.ai" with a Cyrillic 'а' (U+0430) in place of Latin 'a'.
    urls = ["https://clаude.ai/referral/ABC123XYZ9"]
    assert match_referral(urls, PATTERN, PREFIX) is None


def test_rejects_userinfo_trick():
    urls = ["https://claude.ai@evil.com/referral/ABC123XYZ9"]
    assert match_referral(urls, PATTERN, PREFIX) is None


def test_rejects_subdomain_of_lookalike():
    urls = ["https://claude.ai.attacker.io/referral/ABC123XYZ9"]
    assert match_referral(urls, PATTERN, PREFIX) is None


def test_accepts_with_tracking_params():
    urls = ["https://claude.ai/referral/ABC123XYZ9?utm_source=x"]
    hit = match_referral(urls, PATTERN, PREFIX)
    assert hit is not None and hit[0] == "ABC123XYZ9"


def test_rejects_non_referral_path():
    urls = ["https://claude.ai/login"]
    assert match_referral(urls, PATTERN, PREFIX) is None
