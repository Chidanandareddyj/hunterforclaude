"""URL extraction and referral-pattern matching.

Strict, config-driven pattern (DESIGN.md §4.2). Extraction is pure and has
no network access, so it is trivially testable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .models import RawItem


def extract_urls(item: RawItem) -> list[str]:
    """All candidate URLs mentioned in the item (text, url fields, simple markdown)."""
    found: list[str] = []
    found.extend(item.urls)
    # Bare URLs in text
    for m in re.finditer(r"https?://[^\s<>\"'()]+", item.text):
        found.append(m.group(0).rstrip(".,;:!?"))
    # Markdown/HTML links: [text](url) and href="..."
    for m in re.finditer(r"\]\((https?://[^)\s]+)\)", item.text):
        found.append(m.group(1))
    for m in re.finditer(r"""href=["'](https?://[^"']+)["']""", item.text, re.I):
        found.append(m.group(1))
    # De-duplicate preserving order
    seen: set[str] = set()
    out: list[str] = []
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def match_referral(
    urls: Iterable[str], pattern: re.Pattern[str], canonical_prefix: str
) -> tuple[str, str] | None:
    """Return (code, url) for the first URL that is a *direct* referral link.

    Strict rules (DESIGN.md §4.3):
    - the URL itself must match the referral pattern, not just contain it
      behind a redirect, shortener or wrapper;
    - host must be exactly the host in the canonical prefix (no lookalikes,
      no unicode-confusable IDN homoglyphs);
    - userinfo tricks (`user@host`) are rejected;
    - any fragment or tracking query is tolerated at match time (normalizer
      strips it), but the path must be exactly /referral/<code>.
    """
    canon = canonical_prefix.rstrip("/")
    canon_host = re.match(r"https?://([^/]+)/referral", canon + "/referral")
    if not canon_host:
        return None
    expected_host = canon_host.group(1).lower()

    for url in urls:
        # Split off query/fragment so pattern fullmatch applies to the path part;
        # the host check below still runs on the full URL.
        base = url.strip().split("?", 1)[0].split("#", 1)[0]
        m = pattern.fullmatch(base)
        if not m:
            continue
        # Host check on the parsed URL, not the raw string.
        try:
            from urllib.parse import urlsplit

            parts = urlsplit(url)
        except ValueError:
            continue
        host = (parts.hostname or "").lower()
        if host != expected_host:
            continue
        # IDN homoglyph check: encoding the ASCII host is a no-op; anything
        # else means unicode confusables were used.
        try:
            if host.encode("idna").decode("ascii") != host:
                continue
        except UnicodeError:
            continue
        # Userinfo trick: https://claude.ai@evil.com/...
        if parts.username or parts.password:
            continue
        code = m.groupdict().get("code") or m.group(1)
        if not code:
            continue
        return code, url
    return None


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile the configured referral pattern, adding a named `code` group if absent."""
    if "(?P<code>" not in pattern:
        pattern = pattern.replace("[A-Za-z0-9_]{6,20}", "(?P<code>[A-Za-z0-9_]{6,20})")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid referral url_pattern in config: {pattern!r} ({exc})") from exc
