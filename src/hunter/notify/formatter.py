"""Telegram message formatter (DESIGN.md §4.6).

HTML parse mode, link preview disabled, properly escaped.
"""

from __future__ import annotations

import html

from ..models import Alert

_TAG_EMOJI = {"green": "🟢", "yellow": "🟡", "orange": "🟠"}

_RELIABILITY_LABELS = {
    "realtime_firehose": "⚡ High (Real-time Firehose)",
    "community_search": "🔍 Medium (Community Search)",
    "public_directory": "⚠️ Low (Public Directory)",
    "channel_preview": "📢 Moderate (Channel Preview)",
    "unknown": "❓ Unrated (Unknown Source)",
}

# Statuses produced by validate.validate_candidate plus the Alert model default.
_VALIDATION_LABELS = {
    "verified_fresh": "✅ Verified (fresh)",
    "candidate_valid": "✅ Valid candidate",
    "verified": "✅ Verified",
    "invalid_format": "⚠️ Invalid format",
    "troll_code": "⚠️ Flagged as troll",
    "low_entropy": "⚠️ Low entropy",
    "stale": "⚠️ Stale",
}


def _reliability_text(tier: str) -> str:
    return _RELIABILITY_LABELS.get(tier, "❓ Unrated")


def _validation_text(status: str) -> str:
    return _VALIDATION_LABELS.get(status, f"❓ {html.escape(status)}")


def _age_text(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "age unknown"
    m = int(age_seconds // 60)
    s = int(age_seconds % 60)
    if m == 0:
        return f"{s}s old"
    if m < 60:
        return f"{m}m {s:02d}s old"
    return f"{m // 60}h {m % 60:02d}m old"


def format_alert(alert: Alert, canonical_prefix: str) -> str:
    code = html.escape(alert.code)
    link = f"{canonical_prefix.rstrip('/')}/{alert.code}"
    age_str = _age_text(alert.age_seconds)
    if alert.age_seconds is not None and alert.age_seconds < 120:
        age_str += " ⚡ (Fresh)"

    parts = [
        f"🎟 <b>Referral spotted</b> {_TAG_EMOJI.get(alert.confidence, '🟠')}",
        f"Link: <a href=\"{html.escape(link)}\">{code}</a>",
        f"Age: {age_str}",
    ]
    src = alert.item
    if src.permalink:
        parts.append(f"Source: <a href=\"{html.escape(src.permalink)}\">{html.escape(src.source)}</a>")
    else:
        parts.append(f"Source: {html.escape(src.source)}")

    tier_label = _reliability_text(alert.reliability_tier)
    parts.append(f"Reliability: {tier_label}")
    parts.append(f"Validation: {_validation_text(alert.validation_status)}")

    if alert.n_sources > 1:
        parts.append(f"Corroborated by {alert.n_sources} sources")
    return "\n".join(parts)


def format_sample() -> str:
    return (
        "🎟 <b>Referral spotted</b> 🟢\n"
        'Link: <a href="https://claude.ai/referral/EXAMPLEKEY12">EXAMPLEKEY12</a>\n'
        "Age: 12s old ⚡ (Fresh)\n"
        "Source: test\n"
        "Reliability: ⚡ High (Real-time Firehose)\n"
        "Validation: ✅ Verified (fresh)\n"
        "This is a --send-test sample alert."
    )
