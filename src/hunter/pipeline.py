"""Pipeline orchestrator: RawItem in, zero or one Alert out.

All DB writes for one item happen inside the dedup transaction; the filter
runs before any DB write so junk never touches storage beyond a sighting.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .db import Database, MuteState
from .extract import compile_pattern, extract_urls, match_referral
from .filters import filter_candidate
from .logging_setup import get_logger
from .models import Alert, Candidate, RawItem
from .normalize import DedupResult, record_sighting_and_decide
from .score import score_candidate

log = get_logger(component="pipeline")


@dataclass(slots=True)
class PipelineResult:
    alert: Alert | None
    rejected: str | None       # filter rejection reason
    dedup: DedupResult | None  # dedup decision (alerted or suppressed)
    candidate: Candidate | None


class Pipeline:
    def __init__(self, cfg: Config, db: Database) -> None:
        self.cfg = cfg
        self.db = db
        self.pattern = compile_pattern(cfg.url_pattern)

    async def process(self, item: RawItem) -> PipelineResult:
        # 1. Extract
        urls = extract_urls(item)
        hit = match_referral(urls, self.pattern, self.cfg.canonical_link_prefix)
        if hit is None:
            return PipelineResult(None, None, None, None)
        code, url = hit
        candidate = Candidate(code=code, url=url, item=item)

        # 2. Filter (contextual rules; host rules already applied at match time)
        mute_state = await self._mute_state(item)
        reason = filter_candidate(
            candidate,
            canonical_prefix=self.cfg.canonical_link_prefix,
            pipeline_cfg=self.cfg.pipeline,
            mute_state=mute_state,
        )
        if reason:
            log.debug("filtered", source=item.source, item_id=item.item_id, reason=reason)
            return PipelineResult(None, reason, None, candidate)

        # 3. Normalize + dedup (records sighting in one transaction)
        dedup = await record_sighting_and_decide(
            self.db, candidate, self.cfg.canonical_link_prefix, self.cfg.pipeline
        )

        # 4. Score (never drops; picks silent vs normal)
        alert = score_candidate(
            candidate,
            scoring=self.cfg.scoring,
            staleness_cutoff_minutes=self.cfg.pipeline.staleness_cutoff_minutes,
            mute_state=mute_state,
            n_sources=dedup.n_sources,
            source_trust=await self._source_trust(item.source),
        )
        alert.n_sources = dedup.n_sources
        alert.sources = sorted({item.source, *(s for s in alert.sources)})

        if not dedup.should_alert:
            return PipelineResult(None, None, dedup, candidate)

        # 5. Mark link alerted and record the alert attempt (dedup basis for
        #    later sightings, independent of actual delivery).
        from .db import iso, utcnow

        await self.db.set_link_status(code, "alerted")
        async with self.db.conn.execute(
            "INSERT INTO alerts(code, sent_at, silent, confidence) VALUES (?, ?, ?, ?)",
            (code, iso(utcnow()), int(alert.silent), alert.confidence),
        ) as cur:
            alert_row_id = cur.lastrowid
        await self.db.conn.commit()
        alert.row_id = alert_row_id  # set by caller for message-id updates
        log.info(
            "alert candidate",
            code=code,
            source=item.source,
            n_sources=dedup.n_sources,
            score=alert.score,
            silent=alert.silent,
            confidence=alert.confidence,
        )
        return PipelineResult(alert, None, dedup, candidate)

    async def _mute_state(self, item: RawItem) -> MuteState | None:
        async with self.db.conn.execute(
            "SELECT muted, score FROM sources WHERE name = ?", (item.source,)
        ) as cur:
            srow = await cur.fetchone()
        async with self.db.conn.execute(
            "SELECT muted, score FROM authors WHERE source = ? AND handle = ?",
            (item.source, item.author),
        ) as cur:
            arow = await cur.fetchone()
        return MuteState(
            source_muted=bool(srow["muted"]) if srow else False,
            source_score=float(srow["score"]) if srow else 1.0,
            author_muted=bool(arow["muted"]) if arow else False,
            author_score=float(arow["score"]) if arow else 1.0,
        )

    async def _source_trust(self, source: str) -> float:
        """Configured trust × live source score from feedback."""
        trust_by_name: dict[str, float] = {
            "lemmy": self.cfg.sources.lemmy.trust,
            "bluesky": self.cfg.sources.bluesky.trust,
            "hackernews": self.cfg.sources.hackernews.trust,
            "claudecoworkcourse": self.cfg.sources.claudecoworkcourse.trust,
            "oneparrainage": self.cfg.sources.oneparrainage.trust,
            "telegram_preview": self.cfg.sources.telegram_preview.trust,
        }
        trust = trust_by_name.get(source, 0.5)
        async with self.db.conn.execute(
            "SELECT score FROM sources WHERE name = ?", (source,)
        ) as cur:
            row = await cur.fetchone()
        live = float(row["score"]) if row else 1.0
        return max(0.0, min(1.0, trust * live))
