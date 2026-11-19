"""Standalone live probe: find currently-public Claude referral links.

This does NOT need the Hunter config, DB, or Telegram bot. It queries the same
public sources the collectors use and prints any referral links it finds.

Usage:
    .venv\\Scripts\\python tools\\find_links.py
    .venv\\Scripts\\python tools\\find_links.py --write data/found_links.jsonl

Remember: guest passes are first-come, first-served and often already claimed.
This tool only observes already-public posts; it never opens or redeems links.
"""

from __future__ import annotations

import argparse
import contextlib
import html
import json
import math
import re
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

CODE_RE = re.compile(r"https://claude\.ai/referral/([A-Za-z0-9_-]{6,60})\b", re.I)
# Obvious joke/troll submissions seen on public directories.
TROLL_RE = re.compile(
    r"(?:mom|poor|test|notareferral|gay|sex|dick|freecode|givemefree|discord|"
    r"thoughtyou|airdrop|scam|nigga|monopoly|ambient|homeless|gift)",
    re.I,
)
AGE_RE = re.compile(r"\b(\d+)\s*(s|sec|m|min|h|hour|d|day|w|week|mo|month)\b", re.I)
_UNITS = {
    "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hour": 3600,
    "d": 86400, "day": 86400, "w": 604800, "week": 604800,
    "mo": 2592000, "month": 2592000,
}


@dataclass(slots=True)
class Found:
    source: str
    code: str
    url: str
    age_seconds: float | None
    context: str


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    return -sum((c / len(s)) * math.log2(c / len(s)) for c in
                (s.count(ch) / len(s) for ch in set(s)))


def plausible(code: str) -> bool:
    """Reject obvious troll/low-entropy submissions from public directories."""
    if TROLL_RE.search(code):
        return False
    return not (len(code) >= 10 and _entropy(code) < 2.2)


def _age_text(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


async def probe_hn(client: httpx.AsyncClient, now: datetime) -> list[Found]:
    out: list[Found] = []
    resp = await client.get(
        "https://hn.algolia.com/api/v1/search_by_date",
        params={"query": "claude.ai/referral", "tags": "comment", "hitsPerPage": 100},
    )
    resp.raise_for_status()
    for hit in resp.json().get("hits", []):
        text = html.unescape(hit.get("comment_text") or "")
        created = hit.get("created_at")
        age = None
        if created:
            try:
                posted = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age = max(0.0, (now - posted).total_seconds())
            except ValueError:
                pass
        for m in CODE_RE.finditer(text):
            out.append(
                Found(
                    source="hackernews",
                    code=m.group(1),
                    url=m.group(0),
                    age_seconds=age,
                    context=re.sub(r"<[^>]+>", " ", text)[:160].strip(),
                )
            )
    return out


async def probe_bluesky(client: httpx.AsyncClient, now: datetime) -> list[Found]:
    out: list[Found] = []
    resp = await client.get(
        "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts",
        params={"q": "claude.ai/referral", "sort": "latest", "limit": 100},
    )
    resp.raise_for_status()
    for post in resp.json().get("posts", []):
        record = post.get("record") or {}
        text = str(record.get("text") or "")
        created = record.get("createdAt") or post.get("indexedAt")
        age = None
        if created:
            try:
                posted = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age = max(0.0, (now - posted).total_seconds())
            except ValueError:
                pass
        handle = (post.get("author") or {}).get("handle", "unknown")
        for m in CODE_RE.finditer(text):
            out.append(
                Found(
                    source="bluesky_search",
                    code=m.group(1),
                    url=m.group(0),
                    age_seconds=age,
                    context=f"@{handle}: {text[:140]}",
                )
            )
    return out


async def probe_reddit(client: httpx.AsyncClient, now: datetime) -> list[Found]:
    out: list[Found] = []
    base = "https://api.pullpush.io/reddit/search"
    for kind in ("comment", "submission"):
        try:
            resp = await client.get(
                f"{base}/{kind}/",
                params={"q": "claude.ai/referral", "sort": "desc", "size": 100},
            )
            resp.raise_for_status()
        except Exception:  # noqa: BLE001 - probe should survive one dead endpoint
            continue
        for post in resp.json().get("data", []):
            text = " ".join(
                str(post.get(k) or "") for k in ("title", "selftext", "body")
            )
            ts = post.get("created_utc")
            age = None
            if ts:
                with contextlib.suppress(ValueError, TypeError, OSError):
                    age = max(0.0, (now - datetime.fromtimestamp(float(ts), UTC)).total_seconds())
            author = str(post.get("author") or "unknown")
            permalink = "https://www.reddit.com" + str(post.get("permalink") or "")
            for m in CODE_RE.finditer(text):
                out.append(
                    Found(
                        source="reddit",
                        code=m.group(1),
                        url=m.group(0),
                        age_seconds=age,
                        context=f"u/{author} {permalink}"[:160],
                    )
                )
    return out


async def probe_coworkcourse(client: httpx.AsyncClient, now: datetime) -> list[Found]:
    out: list[Found] = []
    resp = await client.get("https://claudecoworkcourse.com/claude-guest-passes")
    resp.raise_for_status()
    page = html.unescape(resp.text)
    for m in CODE_RE.finditer(page):
        # Look for a relative age in the ~200 chars around the link.
        window = page[max(0, m.start() - 200): m.end() + 200]
        age = None
        am = AGE_RE.search(window)
        if am:
            age = int(am.group(1)) * _UNITS.get(am.group(2).lower(), 60)
        out.append(
            Found(
                source="claudecoworkcourse",
                code=m.group(1),
                url=m.group(0),
                age_seconds=age,
                context=re.sub(r"<[^>]+>", " ", window)[:160].strip(),
            )
        )
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", type=Path, default=None, help="also write findings as JSONL")
    ap.add_argument("--max-age-hours", type=float, default=24.0)
    ap.add_argument("--top", type=int, default=15, help="max links to print (default 15)")
    args = ap.parse_args()

    now = datetime.now(UTC)
    found: list[Found] = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        for name, probe in (
            ("Hacker News", probe_hn),
            ("Bluesky search", probe_bluesky),
            ("Reddit/PullPush", probe_reddit),
            ("claudecoworkcourse", probe_coworkcourse),
        ):
            try:
                got = await probe(client, now)
                print(f"[{name}] {len(got)} link(s) found", file=sys.stderr)
                found.extend(got)
            except Exception as exc:  # noqa: BLE001 - standalone probe, report and continue
                print(f"[{name}] probe failed: {exc}", file=sys.stderr)

    # Dedupe by code, keep the freshest/earliest sighting.
    by_code: dict[str, Found] = {}
    for f in found:
        prev = by_code.get(f.code)
        if prev is None or (f.age_seconds or 0) < (prev.age_seconds or 0):
            by_code[f.code] = f

    fresh = [
        f for f in by_code.values()
        if plausible(f.code)
        and (f.age_seconds is None or f.age_seconds <= args.max_age_hours * 3600)
    ]
    fresh.sort(key=lambda f: (f.age_seconds if f.age_seconds is not None else 1e18))
    shown = fresh[: args.top]

    if not fresh:
        print("No recent referral links found right now. Try again later; these are time-sensitive.")
        return 0

    print(f"\n{len(fresh)} plausible recent referral link(s) "
          f"(showing {len(shown)}; {len(by_code)} raw found):\n")
    for f in shown:
        print(f"  {f.url}")
        print(f"    source: {f.source}   age: {_age_text(f.age_seconds)}")

    if args.write:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        with args.write.open("w", encoding="utf-8") as fh:
            for f in fresh:
                fh.write(json.dumps(asdict(f)) + "\n")
        print(f"\nWrote {len(fresh)} finding(s) to {args.write}")
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
