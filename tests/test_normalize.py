"""Dedup, tracking-strip and re-alert rule tests."""

from __future__ import annotations

from datetime import timedelta

from conftest import make_item
from hunter.config import PipelineCfg
from hunter.db import Database
from hunter.models import Candidate
from hunter.normalize import record_sighting_and_decide, strip_tracking


def test_strip_tracking_params():
    assert strip_tracking("https://claude.ai/referral/ABC?utm_source=x&fbclid=y") == (
        "https://claude.ai/referral/ABC"
    )


async def test_first_sighting_alerts(db: Database):
    cfg = PipelineCfg()
    item = make_item()
    cand = Candidate(code="ABC123XYZ9", url="https://claude.ai/referral/ABC123XYZ9", item=item)
    result = await record_sighting_and_decide(db, cand, "https://claude.ai/referral/", cfg)
    assert result.should_alert and result.reason == "new_code"
    assert result.n_sources == 1


async def test_second_sighting_within_window_does_not_realert(db: Database):
    cfg = PipelineCfg()
    prefix = "https://claude.ai/referral/"
    cand = Candidate(code="ABC123XYZ9", url="https://claude.ai/referral/ABC123XYZ9", item=make_item(item_id="1"))
    await record_sighting_and_decide(db, cand, prefix, cfg)
    # Simulate an alert sent 1h ago
    from hunter.db import iso, utcnow

    await db.conn.execute(
        "INSERT INTO alerts(code, sent_at, silent, confidence) VALUES (?, ?, 0, 'green')",
        ("ABC123XYZ9", iso(utcnow() - timedelta(hours=1))),
    )
    await db.conn.commit()
    # Another source posts the same code 2 minutes later.
    item2 = make_item(source="bluesky", item_id="2")
    cand2 = Candidate(code="ABC123XYZ9", url="https://claude.ai/referral/ABC123XYZ9", item=item2)
    result = await record_sighting_and_decide(db, cand2, prefix, cfg)
    assert not result.should_alert
    assert result.reason == "already_alerted"
    assert result.n_sources == 2  # corroboration counted


async def test_realert_after_24h(db: Database):
    cfg = PipelineCfg()
    prefix = "https://claude.ai/referral/"
    cand = Candidate(code="ABC123XYZ9", url="https://claude.ai/referral/ABC123XYZ9", item=make_item(item_id="1"))
    await record_sighting_and_decide(db, cand, prefix, cfg)
    from hunter.db import iso, utcnow

    await db.conn.execute(
        "INSERT INTO alerts(code, sent_at, silent, confidence) VALUES (?, ?, 0, 'green')",
        ("ABC123XYZ9", iso(utcnow() - timedelta(hours=25))),
    )
    await db.conn.commit()
    result = await record_sighting_and_decide(db, cand, prefix, cfg)
    assert result.should_alert and result.reason == "realert_window"


async def test_dead_never_realerts(db: Database):
    cfg = PipelineCfg()
    prefix = "https://claude.ai/referral/"
    cand = Candidate(code="ABC123XYZ9", url="https://claude.ai/referral/ABC123XYZ9", item=make_item(item_id="1"))
    await record_sighting_and_decide(db, cand, prefix, cfg)
    await db.set_link_status("ABC123XYZ9", "dead")
    result = await record_sighting_and_decide(db, cand, prefix, cfg)
    assert not result.should_alert and result.reason == "dead_never_realert"


async def test_worked_never_realerts(db: Database):
    cfg = PipelineCfg()
    prefix = "https://claude.ai/referral/"
    cand = Candidate(code="ABC123XYZ9", url="https://claude.ai/referral/ABC123XYZ9", item=make_item(item_id="1"))
    await record_sighting_and_decide(db, cand, prefix, cfg)
    await db.set_link_status("ABC123XYZ9", "worked")
    result = await record_sighting_and_decide(db, cand, prefix, cfg)
    assert not result.should_alert and result.reason == "worked_never_realert"
