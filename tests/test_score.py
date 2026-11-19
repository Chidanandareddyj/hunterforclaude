"""Scoring tests: monotonicity, corroboration, confidence, silent decision."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hunter.config import Scoring
from hunter.db import MuteState
from hunter.models import Candidate, RawItem
from hunter.score import (
    corroboration_boost,
    freshness_decay,
    score_candidate,
)

SCORING = Scoring()
PREFIX = "https://claude.ai/referral/"


def _cand(minutes_ago: float) -> Candidate:
    now = datetime.now(UTC)
    item = RawItem(
        source="lemmy", item_id="1", author="a", text="t",
        posted_at=now - timedelta(minutes=minutes_ago), fetched_at=now,
    )
    return Candidate(code="ABC123XYZ9", url="https://claude.ai/referral/ABC123XYZ9", item=item)


def test_freshness_decay_monotonic():
    fresh = freshness_decay(60, 20)
    older = freshness_decay(600, 20)
    assert fresh > older
    assert 0 < older < 1


def test_decay_halves_at_half_life():
    assert freshness_decay(20 * 60, 20) == 0.5


def test_corroboration_increases_and_caps():
    assert corroboration_boost(1, SCORING) == 1.0
    assert corroboration_boost(3, SCORING) > corroboration_boost(2, SCORING)
    assert corroboration_boost(100, SCORING) == SCORING.corroboration.max_boost


def test_score_older_means_lower():
    fresh = score_candidate(_cand(1), scoring=SCORING, staleness_cutoff_minutes=30)
    old = score_candidate(_cand(25), scoring=SCORING, staleness_cutoff_minutes=30)
    assert fresh.score > old.score


def test_score_more_sources_means_higher():
    one = score_candidate(_cand(2), scoring=SCORING, staleness_cutoff_minutes=30, n_sources=1)
    three = score_candidate(_cand(2), scoring=SCORING, staleness_cutoff_minutes=30, n_sources=3)
    assert three.score > one.score


def test_fresh_high_score_is_normal_not_silent():
    alert = score_candidate(_cand(1), scoring=SCORING, staleness_cutoff_minutes=30)
    assert not alert.silent
    assert alert.confidence == "green"


def test_old_low_score_is_silent():
    alert = score_candidate(_cand(29), scoring=SCORING, staleness_cutoff_minutes=30)
    assert alert.silent
    assert alert.confidence in ("yellow", "orange")


def test_author_history_lowers_score():
    bad = MuteState(author_score=0.2)
    good = score_candidate(_cand(2), scoring=SCORING, staleness_cutoff_minutes=30)
    poor = score_candidate(_cand(2), scoring=SCORING, staleness_cutoff_minutes=30, mute_state=bad)
    assert poor.score < good.score


def test_confidence_tags():
    now = datetime.now(UTC)
    for minutes, expected in ((2, "green"), (10, "yellow"), (20, "orange")):
        item = RawItem(
            source="s", item_id="1", author="a",
            posted_at=now - timedelta(minutes=minutes), fetched_at=now,
        )
        cand = Candidate(code="X", url="https://claude.ai/referral/ABC123XYZ9", item=item)
        alert = score_candidate(cand, scoring=SCORING, staleness_cutoff_minutes=30)
        assert alert.confidence == expected


def test_unknown_age_conservative():
    alert = score_candidate(
        Candidate(
            code="X", url="https://claude.ai/referral/ABC123XYZ9",
            item=RawItem(source="s", item_id="1", author="a", fetched_at=datetime.now(UTC)),
        ),
        scoring=SCORING, staleness_cutoff_minutes=30,
    )
    assert alert.confidence == "orange"
