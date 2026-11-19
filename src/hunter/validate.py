"""Referral link validation and reliability rating.

Provides:
- Format/entropy validation and troll-phrase detection (the DROP decision)
- Source reliability tier classification for sources
- A shared freshness-status helper used by the scorer (no re-validation)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Candidate

# Standard Anthropic guest pass referral code format:
# Typically 8-16 characters, alphanumeric (plus _ or -), random distribution.
_VALID_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{6,60}$")

# Long, multi-word troll fragments. These are specific enough that they cannot
# plausibly appear inside a randomly generated token, so a substring match is
# safe here (unlike matching single words such as "mom" or "test" anywhere).
_TROLL_PHRASES = re.compile(
    r"(?:notareferral|freecode|givemefree|thoughtyou|freepro|plzgive|"
    r"homelessplz|needforsoftware|totallynot)",
    re.I,
)

# Single troll words. A single occurrence is NOT enough to reject a code: a
# legitimate random token can legitimately contain "mom", "sex", "test", ...
# as a substring. We only act on these when several appear as whole components
# of a digits-free, word-like code (i.e. an obvious phrase submission).
_TROLL_WORDS = frozenset(
    {
        "mom", "poor", "test", "gay", "sex", "dick", "discord", "airdrop",
        "scam", "homeless",
    }
)

# Split CamelCase / snake_case / kebab-case codes into word-like components.
_CAMEL_SPLIT_RE = re.compile(
    r"[_\-]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
)


def calculate_entropy(s: str) -> float:
    """Calculate Shannon entropy to measure randomness.

    Random hashes (like real referral codes) typically have entropy >= 2.8.
    Predictable English words or repetitive strings have lower entropy.
    """
    if not s:
        return 0.0
    prob = [float(s.count(c)) / len(s) for c in set(s)]
    return -sum(p * math.log2(p) for p in prob)


def _code_components(code: str) -> list[str]:
    """Return the lower-cased word components of a code."""
    return [part.lower() for part in _CAMEL_SPLIT_RE.split(code) if part]


def is_troll_code(code: str) -> bool:
    """Detect obvious troll *phrases* without punishing unlucky random codes.

    A code is only flagged when there is strong, phrase-like evidence:
    an explicit multi-word troll fragment, a clearly overlength submission,
    or several troll words forming the components of an all-letter code.
    """
    if len(code) > 60 or _TROLL_PHRASES.search(code):
        return True
    # Real referral tokens are alphanumeric/mixed-case; phrase submissions are
    # plain English letters. Requiring "no digits" makes the word rule safe.
    if any(char.isdigit() for char in code):
        return False
    words = set(_code_components(code))
    return len(words & _TROLL_WORDS) >= 2


@dataclass(slots=True)
class ValidationResult:
    is_valid: bool
    # verified_fresh | candidate_valid | invalid_format | troll_code | low_entropy
    status: str
    reason: str
    reliability_tier: str  # realtime_firehose, community_search, public_directory
    reliability_score: float  # 0.0 to 1.0


def get_source_reliability(source: str) -> tuple[str, float]:
    """Classify the operational reliability of a source.

    Real-time firehoses (Bluesky) provide near-instantaneous capture (<5s latency).
    Community search (Lemmy, HN) provides fast polling (<60s).
    Public directories (claudecoworkcourse, 1parrainage) have heavy public competition
    and often contain already-exhausted or retired codes.
    """
    if source == "bluesky":
        return "realtime_firehose", 0.9
    if source == "bluesky_search":
        return "realtime_firehose", 0.85
    if source in ("lemmy", "hackernews"):
        return "community_search", 0.7
    if source in ("reddit", "mastodon", "github", "web_search"):
        return "community_search", 0.6
    if source in ("claudecoworkcourse", "oneparrainage"):
        return "public_directory", 0.25
    if source == "telegram_preview":
        return "channel_preview", 0.5
    return "unknown", 0.4


def freshness_status(age_seconds: float | None) -> str:
    """Status for a candidate that already passed validation.

    Shared with the scorer so it does not need to re-run the full validation
    (format regex, troll and entropy checks) on every candidate.
    """
    if age_seconds is not None and age_seconds <= 300:
        return "verified_fresh"
    return "candidate_valid"


def validate_candidate(candidate: Candidate) -> ValidationResult:
    """Validate code shape and troll patterns; staleness is config-driven elsewhere."""
    code = candidate.code
    source = candidate.item.source
    tier, rel_score = get_source_reliability(source)

    # 1. Regex shape check
    if not _VALID_CODE_RE.match(code):
        return ValidationResult(
            is_valid=False,
            status="invalid_format",
            reason="Code does not match standard referral format",
            reliability_tier=tier,
            reliability_score=rel_score,
        )

    # 2. Troll phrase heuristic (conservative: no false positives on random codes)
    if is_troll_code(code):
        return ValidationResult(
            is_valid=False,
            status="troll_code",
            reason="Code flagged as troll or joke submission",
            reliability_tier=tier,
            reliability_score=rel_score,
        )

    # 3. Code length and entropy: real codes are 8-16 chars with mixed characters.
    # Very long codes (20+ chars) that read like English phrases have low entropy.
    if len(code) >= 12 and calculate_entropy(code) < 2.5:
        return ValidationResult(
            is_valid=False,
            status="low_entropy",
            reason="Code appears to be plain words rather than a generated referral token",
            reliability_tier=tier,
            reliability_score=rel_score,
        )

    # Staleness is deliberately NOT checked here. filter_candidate() applies
    # pipeline_cfg.staleness_cutoff_minutes so config stays the single source
    # of truth; this function only validates the code itself.
    return ValidationResult(
        is_valid=True,
        status=freshness_status(candidate.age_seconds),
        reason="Format and shape validated",
        reliability_tier=tier,
        reliability_score=rel_score,
    )
