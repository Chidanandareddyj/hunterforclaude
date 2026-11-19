"""Scorer (DESIGN.md §4.5).

score = source_trust × freshness_decay × corroboration_boost × author_history

The score never drops a candidate that passed the filter. It decides:
- normal notification (score >= normal_threshold), or
- silent notification (disable_notification).
It also produces the age-based confidence tag.
"""

from __future__ import annotations

from .config import Scoring
from .db import MuteState
from .models import Alert, Candidate


def freshness_decay(age_seconds: float | None, half_life_minutes: float) -> float:
    if age_seconds is None:
        # Unknown age: conservative but not zero (e.g. date-only sources).
        return 0.5
    half_life = max(half_life_minutes, 0.1) * 60
    return 0.5 ** (age_seconds / half_life)


def corroboration_boost(n_sources: int, cfg: Scoring) -> float:
    boost = 1.0 + cfg.corroboration.extra_sources_weight * max(0, n_sources - 1)
    return min(boost, cfg.corroboration.max_boost)


def author_history(state: MuteState | None) -> float:
    if state is None:
        return 1.0
    return max(0.0, min(1.0, state.author_score))


def confidence_tag(age_seconds: float | None, cfg: Scoring, cutoff_minutes: float) -> str:
    if age_seconds is None:
        return "orange"
    minutes = age_seconds / 60
    if minutes < cfg.confidence_tags.green_under:
        return "green"
    if minutes < cfg.confidence_tags.yellow_under:
        return "yellow"
    return "orange"


def score_candidate(
    candidate: Candidate,
    *,
    scoring: Scoring,
    staleness_cutoff_minutes: float,
    mute_state: MuteState | None = None,
    n_sources: int = 1,
    source_trust: float = 1.0,
) -> Alert:
    age = candidate.age_seconds
    decay = freshness_decay(age, scoring.freshness_decay.half_life_minutes)
    boost = corroboration_boost(n_sources, scoring)
    history = author_history(mute_state)
    score = source_trust * decay * boost * history
    silent = score < scoring.normal_threshold
    from .validate import freshness_status, get_source_reliability

    # filter_candidate() already validated this candidate before it reached the
    # scorer. Reuse the cheap freshness-based status instead of re-running the
    # full validation (regex + troll + entropy checks) a second time.
    tier, _ = get_source_reliability(candidate.item.source)
    return Alert(
        code=candidate.code,
        url=candidate.url,
        item=candidate.item,
        n_sources=n_sources,
        score=round(score, 4),
        silent=silent,
        confidence=confidence_tag(age, scoring, staleness_cutoff_minutes),
        age_seconds=age,
        sources=[candidate.item.source],
        reliability_tier=tier,
        validation_status=freshness_status(age),
    )
