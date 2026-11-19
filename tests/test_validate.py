"""Tests for referral link validation, troll filtering, and reliability ratings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hunter.config import PipelineCfg
from hunter.filters import filter_candidate
from hunter.models import Candidate, RawItem
from hunter.validate import (
    calculate_entropy,
    get_source_reliability,
    validate_candidate,
)

PREFIX = "https://claude.ai/referral/"


def _cand(code: str, source: str = "bluesky", minutes_ago: float = 2) -> Candidate:
    now = datetime.now(UTC)
    item = RawItem(
        source=source,
        item_id=f"test-{code}",
        author="author1",
        text=f"Check this: https://claude.ai/referral/{code}",
        posted_at=now - timedelta(minutes=minutes_ago),
        fetched_at=now,
    )
    return Candidate(code=code, url=f"https://claude.ai/referral/{code}", item=item)


def test_standard_codes_are_valid():
    for code in ("ABC123XYZ9", "DEF456GHIJ", "vDCiv2svIw", "taRcxYDUMg"):
        res = validate_candidate(_cand(code))
        assert res.is_valid
        assert res.status == "verified_fresh"


def test_troll_codes_are_rejected():
    trolls = [
        "GaySex",
        "mymomiskindahomelessplzgivemefreeclaudecodebecauseyesineedforsoftwaredevelopment",
        "youreallythoughtyouaregettingfreeproboygettheplanpooron",
        "ItsATestNotAReferral",
        "totallynotkridOnDiscord",
    ]
    for troll in trolls:
        res = validate_candidate(_cand(troll))
        assert not res.is_valid
        assert res.status in ("troll_code", "invalid_format", "low_entropy")


def test_stale_codes_are_rejected_via_configurable_cutoff():
    stale = _cand("ABC123XYZ9", minutes_ago=45)
    # Default 30-minute cutoff rejects the 45-minute-old candidate.
    assert filter_candidate(
        stale, canonical_prefix=PREFIX,
        pipeline_cfg=PipelineCfg(staleness_cutoff_minutes=30), mute_state=None,
    ) == "stale"
    # A larger, configured cutoff keeps the very same candidate: config is the
    # single source of truth, validation itself applies no hardcoded cutoff.
    assert filter_candidate(
        stale, canonical_prefix=PREFIX,
        pipeline_cfg=PipelineCfg(staleness_cutoff_minutes=60), mute_state=None,
    ) is None


def test_validation_does_not_hardcode_staleness():
    # Validation only judges the code itself; age is the filter's concern.
    res = validate_candidate(_cand("ABC123XYZ9", minutes_ago=45))
    assert res.is_valid
    assert res.status == "candidate_valid"


def test_codes_with_troll_substrings_are_not_dropped():
    # A random token may legitimately contain "mom"/"test"/"sex"/... as a
    # substring; a single occurrence must not cause a hard DROP.
    for code in ("MomABC123x", "TestXY987", "SeXX9abcQ", "GayQ7word", "DisCoRd123"):
        res = validate_candidate(_cand(code))
        assert res.is_valid, (code, res.status)
        assert res.status == "verified_fresh"


def test_entropy_detects_randomness():
    # Random hash
    random_hash = "vDCiv2svIw"
    assert calculate_entropy(random_hash) > 2.8

    # Repetitive / plain words
    low_entropy = "aaaaaaaaaaaa"
    assert calculate_entropy(low_entropy) == 0.0


def test_reliability_tiers():
    tier, score = get_source_reliability("bluesky")
    assert tier == "realtime_firehose"
    assert score >= 0.8

    tier, score = get_source_reliability("claudecoworkcourse")
    assert tier == "public_directory"
    assert score <= 0.3
