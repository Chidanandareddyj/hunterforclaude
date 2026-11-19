"""Shared fixtures: config, temp DB, and realistic RawItems."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hunter.config import load_config  # noqa: E402
from hunter.db import Database  # noqa: E402
from hunter.models import RawItem  # noqa: E402


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def cfg(tmp_path):
    config_path = Path(__file__).parent / "fixtures" / "config_test.yaml"
    cfg = load_config(config_path)
    cfg.db_path = tmp_path / "test.db"
    return cfg


@pytest.fixture
async def db(cfg):
    database = Database(cfg.db_path)
    await database.connect()
    yield database
    await database.close()


def make_item(
    *,
    source: str = "lemmy",
    author: str = "alice",
    text: str = "fresh pass: https://claude.ai/referral/ABC123XY",
    urls: list[str] | None = None,
    minutes_ago: float = 2.0,
    permalink: str = "https://example.com/p/1",
    now_dt: datetime | None = None,
    item_id: str = "1",
) -> RawItem:
    n = now_dt or datetime.now(UTC)
    posted = n - timedelta(minutes=minutes_ago)
    return RawItem(
        source=source,
        item_id=item_id,
        author=author,
        text=text,
        urls=urls if urls is not None else [],
        permalink=permalink,
        posted_at=posted,
        fetched_at=n,
    )
