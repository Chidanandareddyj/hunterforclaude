"""Offline tests for the Telegram formatter.

Checks that alert rendering stays consistent with the Alert model (including
reliability_tier / validation_status), escapes HTML, never renders ``None``
ages, and only emits tags/entities Telegram's HTML parse mode accepts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

import pytest

from hunter.models import Alert, RawItem
from hunter.notify.formatter import (
    _age_text,
    _reliability_text,
    format_alert,
    format_sample,
)

# https://core.telegram.org/bots/api#html-style
_TELEGRAM_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a",
    "code", "pre", "span", "tg-spoiler",
}
_TELEGRAM_NAMED_ENTITIES = {"lt", "gt", "amp", "quot"}


class _TelegramHTMLCollector(HTMLParser):
    """Collect tags/entities without decoding them (so we can validate them)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.start_tags: list[str] = []
        self.end_tags: list[str] = []
        self.named_entities: list[str] = []
        self.numeric_entities: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.start_tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        self.end_tags.append(tag)

    def handle_entityref(self, name: str) -> None:
        self.named_entities.append(name)

    def handle_charref(self, name: str) -> None:
        self.numeric_entities.append(name)


def _assert_valid_telegram_html(text: str) -> None:
    parser = _TelegramHTMLCollector()
    parser.feed(text)
    parser.close()
    assert set(parser.start_tags) <= _TELEGRAM_TAGS
    assert set(parser.end_tags) <= _TELEGRAM_TAGS
    for tag in set(parser.start_tags) | set(parser.end_tags):
        assert parser.start_tags.count(tag) == parser.end_tags.count(tag)
    assert set(parser.named_entities) <= _TELEGRAM_NAMED_ENTITIES
    # Telegram accepts all numeric character references; check they are well formed.
    for ref in parser.numeric_entities:
        assert ref.isdigit() or (ref[:1].lower() == "x" and ref[1:] != "")


def _make_alert() -> Alert:
    now = datetime.now(UTC)
    item = RawItem(
        source="bluesky",
        item_id="1",
        author="alice",
        text="fresh pass https://claude.ai/referral/ABC123XY",
        permalink="https://bsky.app/profile/alice/post/1",
        posted_at=now - timedelta(seconds=10),
        fetched_at=now,
    )
    return Alert(
        code="ABC123XY",
        url="https://claude.ai/referral/ABC123XY",
        item=item,
        age_seconds=10.0,
        confidence="green",
        reliability_tier="realtime_firehose",
        validation_status="verified_fresh",
    )


def _format(alert: Alert) -> str:
    return format_alert(alert, "https://claude.ai/referral")


def test_format_alert_contains_all_model_fields() -> None:
    text = _format(_make_alert())

    assert "🎟 <b>Referral spotted</b> 🟢" in text
    assert 'Link: <a href="https://claude.ai/referral/ABC123XY">ABC123XY</a>' in text
    assert "Age: 10s old ⚡ (Fresh)" in text
    assert 'Source: <a href="https://bsky.app/profile/alice/post/1">bluesky</a>' in text
    assert "Reliability: ⚡ High (Real-time Firehose)" in text
    assert "Validation: ✅ Verified (fresh)" in text
    assert "Corroborated by" not in text
    assert "None" not in text
    _assert_valid_telegram_html(text)


def test_format_alert_escapes_special_characters() -> None:
    base = _make_alert()
    item = base.item.model_copy(
        update={
            "source": 'A&B "Lemmy" <test>',
            "permalink": 'https://example.com/a?x=1&y="2"',
        }
    )
    alert = base.model_copy(
        update={"code": 'AB&C"D<E>', "item": item, "validation_status": "<b>weird</b>"}
    )

    text = _format(alert)

    assert "A&amp;B &quot;Lemmy&quot; &lt;test&gt;" in text
    assert "x=1&amp;y=&quot;2&quot;" in text
    assert "AB&amp;C&quot;D&lt;E&gt;" in text
    assert "&lt;b&gt;weird&lt;/b&gt;" in text
    assert "<b>weird</b>" not in text
    _assert_valid_telegram_html(text)


def test_format_alert_without_permalink_and_with_corroboration() -> None:
    base = _make_alert()
    item = base.item.model_copy(update={"permalink": None})
    alert = base.model_copy(update={"item": item, "n_sources": 3})

    text = _format(alert)

    assert "Source: bluesky" in text
    assert "Source: <a href" not in text
    assert "Corroborated by 3 sources" in text
    _assert_valid_telegram_html(text)


def test_format_alert_unknown_age_has_no_none_or_fresh_suffix() -> None:
    base = _make_alert()
    item = base.item.model_copy(update={"posted_at": None})
    alert = base.model_copy(update={"item": item, "age_seconds": None})

    text = _format(alert)

    assert "Age: age unknown" in text
    assert "⚡ (Fresh)" not in text
    assert "None" not in text
    _assert_valid_telegram_html(text)


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (0.0, "0s old"),
        (12.9, "12s old"),
        (90.0, "1m 30s old"),
        (3700.0, "1h 01m old"),
    ],
)
def test_age_text_buckets(age: float, expected: str) -> None:
    assert _age_text(age) == expected


@pytest.mark.parametrize(
    ("age", "fresh"),
    [(119.0, True), (120.0, False), (None, False)],
)
def test_fresh_suffix_threshold(age: float | None, fresh: bool) -> None:
    text = _format(_make_alert().model_copy(update={"age_seconds": age}))
    assert ("⚡ (Fresh)" in text) is fresh


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        ("realtime_firehose", "⚡ High (Real-time Firehose)"),
        ("community_search", "🔍 Medium (Community Search)"),
        ("public_directory", "⚠️ Low (Public Directory)"),
        ("channel_preview", "📢 Moderate (Channel Preview)"),
        ("unknown", "❓ Unrated (Unknown Source)"),
        ("some_new_tier", "❓ Unrated"),
    ],
)
def test_reliability_tier_rendered(tier: str, expected: str) -> None:
    assert _reliability_text(tier) == expected
    assert f"Reliability: {expected}" in _format(
        _make_alert().model_copy(update={"reliability_tier": tier})
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("verified_fresh", "✅ Verified (fresh)"),
        ("candidate_valid", "✅ Valid candidate"),
        ("verified", "✅ Verified"),
        ("invalid_format", "⚠️ Invalid format"),
        ("troll_code", "⚠️ Flagged as troll"),
        ("low_entropy", "⚠️ Low entropy"),
        ("stale", "⚠️ Stale"),
    ],
)
def test_validation_status_rendered(status: str, expected: str) -> None:
    text = _format(_make_alert().model_copy(update={"validation_status": status}))
    assert f"Validation: {expected}" in text
    assert "None" not in text
    _assert_valid_telegram_html(text)


def test_format_sample_matches_alert_shape() -> None:
    text = format_sample()

    assert text.startswith("🎟 <b>Referral spotted</b> 🟢\n")
    assert "Age: 12s old ⚡ (Fresh)" in text
    assert "Source: test" in text
    assert "Reliability: ⚡ High (Real-time Firehose)" in text
    assert "Validation: ✅ Verified (fresh)" in text
    assert "--send-test sample alert" in text
    assert "None" not in text
    _assert_valid_telegram_html(text)
