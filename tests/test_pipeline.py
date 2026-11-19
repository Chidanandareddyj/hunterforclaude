"""End-to-end pipeline tests over the realistic fixture set (no network)."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import make_item
from hunter.config import PipelineCfg
from hunter.models import RawItem
from hunter.pipeline import Pipeline

FIXTURES = Path(__file__).parent / "fixtures" / "raw_items.jsonl"


def load_fixtures() -> list[RawItem]:
    items = []
    for line in FIXTURES.read_text(encoding="utf-8").splitlines():
        if line.strip():
            items.append(RawItem(**json.loads(line)))
    return items


async def test_fixture_set_produces_expected_alerts(cfg, db):
    pipeline = Pipeline(cfg, db)
    alerts = []
    rejections: dict[str, list[str]] = {}
    for item in load_fixtures():
        result = await pipeline.process(item)
        if result.alert:
            alerts.append(result.alert)
        elif result.rejected:
            rejections.setdefault(result.rejected, []).append(item.item_id)

    expected_ids = [
        f["item_id"]
        for f in json.loads((FIXTURES.parent / "expected.json").read_text())
    ]
    got_ids = [a.item.item_id for a in alerts]
    assert sorted(got_ids) == sorted(expected_ids), rejections


async def test_staleness_cutoff_is_configurable_end_to_end(cfg, db):
    old_item = make_item(source="lemmy", item_id="stale-config", minutes_ago=45)
    strict = cfg.model_copy(update={"pipeline": PipelineCfg(staleness_cutoff_minutes=30)})
    lenient = cfg.model_copy(update={"pipeline": PipelineCfg(staleness_cutoff_minutes=60)})

    rejected = await Pipeline(strict, db).process(old_item)
    assert rejected.alert is None
    assert rejected.rejected == "stale"

    accepted = await Pipeline(lenient, db).process(old_item)
    assert accepted.alert is not None
    assert accepted.alert.validation_status == "candidate_valid"


async def test_pipeline_dedupes_across_sources(cfg, db):
    pipeline = Pipeline(cfg, db)
    base = make_item(source="lemmy", item_id="dup-1")
    r1 = await pipeline.process(base)
    assert r1.alert is not None
    second = make_item(source="bluesky", item_id="dup-2")
    r2 = await pipeline.process(second)
    assert r2.alert is None  # already alerted within the window
    assert r2.dedup is not None and r2.dedup.n_sources == 2


async def test_muted_author_generates_no_alert(cfg, db):
    pipeline = Pipeline(cfg, db)
    item = make_item(source="lemmy", item_id="mute-1", author="spammer")
    await pipeline.process(item)
    await db.set_author_muted("lemmy", "spammer", True)
    item2 = make_item(source="lemmy", item_id="mute-2", author="spammer")
    result = await pipeline.process(item2)
    assert result.alert is None
    assert result.rejected == "muted"


async def test_auto_mute_after_three_dead(cfg, db):
    pipeline = Pipeline(cfg, db)
    for i in range(4):
        item = make_item(source="lemmy", item_id=f"dead-{i}", author="badactor")
        result = await pipeline.process(item)
        if i < 3:
            await db.bump_author_consecutive_dead("lemmy", "badactor", reset=False)
    await db.set_author_muted("lemmy", "badactor", True)
    result = await pipeline.process(
        make_item(source="lemmy", item_id="dead-4", author="badactor")
    )
    assert result.rejected == "muted"
