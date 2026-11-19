"""Normalization and dedup (DESIGN.md §4.4).

- Canonical key: referral code, case preserved.
- Tracking params are stripped: canonical URL is always prefix + code.
- Every sighting is stored, even when no alert is sent (corroboration counts).
- One alert per code; 24h re-alert only if not marked Dead; Worked = never again.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import PipelineCfg
from .db import Database
from .logging_setup import get_logger
from .models import Candidate

log = get_logger(component="normalize")

# Params that are never considered "tracking" for our canonical form: we simply
# strip *all* query params on referral links — claude.ai needs none of them.
TRACKING_PARAMS = (
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_src", "referrer", "source", "fbclid", "gclid", "mc_cid",
    "mc_eid", "igshid", "si",
)


def canonical_url(prefix: str, code: str) -> str:
    return f"{prefix.rstrip('/')}/{code}"


def strip_tracking(url: str) -> str:
    """Remove query params entirely (referral links need none) and fragments."""
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    drop = {k.lower() for k in TRACKING_PARAMS}
    kept = [(k, v) for k, v in kept if k.lower() not in drop]
    query = urlencode(kept)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


@dataclass(slots=True)
class DedupResult:
    code: str
    canonical: str
    n_sources: int
    should_alert: bool
    reason: str


async def record_sighting_and_decide(
    db: Database, candidate: Candidate, prefix: str, cfg: PipelineCfg
) -> DedupResult:
    """Record the sighting in one transaction and decide whether to alert.

    Rules:
    - unknown code: create link row, alert.
    - known, status new/alerted and last alert older than realert_after_hours:
      re-alert (the code may have been re-shared because it still works).
    - status dead: never re-alert.
    - status worked: never re-alert.
    - otherwise: sighting only.
    """
    code = candidate.code
    canon = strip_tracking(canonical_url(prefix, code))
    now = candidate.item.fetched_at

    link = await db.get_link(code)
    if link is None:
        # Single transaction for link + sighting.
        conn = db.conn
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute(
                "INSERT INTO links(code, first_seen, last_seen, status) "
                "VALUES (?, ?, ?, 'new') ON CONFLICT(code) DO UPDATE SET "
                "last_seen = excluded.last_seen",
                (code, now.isoformat(), now.isoformat()),
            )
            await conn.execute(
                "INSERT INTO sightings(code, source, author, permalink, posted_at, "
                "fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    code,
                    candidate.item.source,
                    candidate.item.author,
                    candidate.item.permalink,
                    candidate.item.posted_at.isoformat() if candidate.item.posted_at else None,
                    now.isoformat(),
                ),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        await db.record_item_seen(candidate.item.source)
        return DedupResult(code, canon, 1, True, "new_code")

    # Existing link: record sighting, bump last_seen, count sources.
    conn = db.conn
    await conn.execute("BEGIN IMMEDIATE")
    try:
        await conn.execute(
            "UPDATE links SET last_seen = ? WHERE code = ?", (now.isoformat(), code)
        )
        await conn.execute(
            "INSERT INTO sightings(code, source, author, permalink, posted_at, "
            "fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                code,
                candidate.item.source,
                candidate.item.author,
                candidate.item.permalink,
                candidate.item.posted_at.isoformat() if candidate.item.posted_at else None,
                now.isoformat(),
            ),
        )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await db.record_item_seen(candidate.item.source)

    # Distinct-source count AFTER this sighting was recorded.
    n_sources = await db._count_sources(code)
    if link.status == "dead":
        return DedupResult(code, canon, n_sources, False, "dead_never_realert")
    if link.status == "worked":
        return DedupResult(code, canon, n_sources, False, "worked_never_realert")

    # new or alerted: check re-alert window on the last alert.
    alert = await db.latest_alert(code)
    if alert is None:
        return DedupResult(code, canon, n_sources, True, "no_prior_alert")
    from .db import parse_iso

    last_sent = parse_iso(alert["sent_at"])
    if last_sent is None:
        return DedupResult(code, canon, n_sources, True, "no_prior_alert")
    elapsed = (now - last_sent).total_seconds()
    if elapsed >= cfg.realert_after_hours * 3600:
        return DedupResult(code, canon, n_sources, True, "realert_window")
    return DedupResult(code, canon, n_sources, False, "already_alerted")
