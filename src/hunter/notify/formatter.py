"""Telegram message formatter (DESIGN.md §4.6).

HTML parse mode, link preview disabled, properly escaped.
"""

from __future__ import annotations

import html

from ..models import Alert

_TAG_EMOJI = {"green": "🟢", "yellow": "🟡", "orange": "🟠"}


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
    parts = [
        f"🎟 <b>Referral spotted</b> {_TAG_EMOJI.get(alert.confidence, '🟠')}",
        f"Link: <a href=\"{html.escape(link)}\">{code}</a>",
        f"Age: {_age_text(alert.age_seconds)}",
    ]
    src = alert.item
    if src.permalink:
        parts.append(f"Source: <a href=\"{html.escape(src.permalink)}\">{html.escape(src.source)}</a>")
    else:
        parts.append(f"Source: {html.escape(src.source)}")
    if alert.n_sources > 1:
        parts.append(f"Corroborated by {alert.n_sources} sources")
    return "\n".join(parts)


def format_sample() -> str:
    return (
        "🎟 <b>Referral spotted</b> 🟢\n"
        'Link: <a href="https://claude.ai/referral/EXAMPLEKEY12">EXAMPLEKEY12</a>\n'
        "Age: 12s old\n"
        "Source: test\n"
        "This is a --send-test sample alert."
    )
