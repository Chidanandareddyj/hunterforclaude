"""Filter rules (DESIGN.md §4.3). Each rule is independently testable.

A candidate is alerted on only if every rule passes. `match_referral` already
enforces the host/userinfo rules at extraction; the filters here cover the
contextual rules.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .config import PipelineCfg
from .db import MuteState
from .models import Candidate

# Well-known URL shorteners and indirect wrappers. A referral link must never
# arrive through one of these: we do not follow redirects to unknown hosts.
SHORTENER_HOSTS: frozenset[str] = frozenset(
    {
        "bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly", "is.gd", "buff.ly",
        "rebrand.ly", "cutt.ly", "shorturl.at", "rb.gy", "t.ly", "s.id",
        "shorte.st", "adf.ly", "bc.vc", "ouo.io", "linktr.ee", "beacons.ai",
        "dub.co", "short.io", "v.gd", "tiny.cc", "soo.gd", "clck.ru",
    }
)

# Funnel heuristics: posts that want money, DMs or channel joins are not
# genuine free passes. Kept deliberately simple and testable.
PAYMENT_RE = re.compile(
    r"\b(?:pay|payment|price|sell(?:ing)?|buy(?:ing)?|\$\d+|€\d+|£\d+|usdt|btc|"
    r"bitcoin|crypto|venmo|paypal|cashapp|upi)\b",
    re.I,
)
DM_FUNNEL_RE = re.compile(
    r"\b(?:dm|dms|message me|inbox|subscribe|join (?:my|the) (?:channel|group)|"
    r"follow me|comment below|first \d+ people)\b",
    re.I,
)


def passes_host_filter(url: str, canonical_prefix: str) -> bool:
    """Exact-host check against the canonical prefix (defense in depth)."""
    try:
        expected = urlsplit(canonical_prefix).hostname or ""
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    return host.lower() == expected.lower()


def has_shortener(item_urls: list[str]) -> bool:
    for url in item_urls:
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            continue
        if host in SHORTENER_HOSTS:
            return True
    return False


def is_funnel_post(text: str) -> bool:
    """Payment ask or DM/channel funnel detected in the post text."""
    return bool(PAYMENT_RE.search(text) and DM_FUNNEL_RE.search(text)) or bool(
        PAYMENT_RE.search(text) and re.search(r"\b(?:for )?(?:\$|€|£)\s?\d", text, re.I)
    )


def within_staleness(candidate: Candidate, cfg: PipelineCfg, now_seconds: float) -> bool:
    """Age is within the cutoff. Unknown age (no posted_at) is kept but capped
    later by the scorer via a conservative default."""
    age = candidate.age_seconds
    if age is None:
        return True  # unknown-age items are not dropped here; see scorer
    return age <= cfg.staleness_cutoff_minutes * 60


def not_muted(candidate: Candidate, state: MuteState | None) -> bool:
    """Source and author mute checks."""
    if state is None:
        return True
    return not (state.source_muted or state.author_muted)


def filter_candidate(
    candidate: Candidate,
    *,
    canonical_prefix: str,
    pipeline_cfg: PipelineCfg,
    mute_state: MuteState | None,
    now_seconds: float | None = None,
) -> str | None:
    """Run all rules. Returns None when everything passes, else a rejection reason."""
    if not passes_host_filter(candidate.url, canonical_prefix):
        return "host_mismatch"
    if has_shortener(candidate.item.urls):
        return "shortener"
    if is_funnel_post(candidate.item.text):
        return "funnel"
    cutoff = pipeline_cfg.staleness_cutoff_minutes * 60
    age = candidate.age_seconds
    if age is not None and age > cutoff:
        return "stale"
    if not not_muted(candidate, mute_state):
        return "muted"
    return None
