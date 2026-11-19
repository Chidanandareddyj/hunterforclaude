"""DB and migration tests."""

from __future__ import annotations

import os
from pathlib import Path

from hunter.db import Database


async def test_migrations_idempotent(tmp_path):
    db = Database(tmp_path / "a.db")
    await db.connect()
    # connect() runs migrate(); calling again must not fail (idempotent re-run).
    await db.migrate()
    await db.close()


async def test_fresh_clone_creates_schema(tmp_path):
    db = Database(tmp_path / "b.db")
    await db.connect()
    async with db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ) as cur:
        tables = {r["name"] for r in await cur.fetchall()}
    expected = {"schema_version", "sources", "authors", "links", "sightings", "alerts", "votes", "settings"}
    assert expected <= tables
    await db.close()


async def test_cursor_survives_reconnect(tmp_path):
    path = tmp_path / "c.db"
    db = Database(path)
    await db.connect()
    await db.upsert_source("lemmy")
    await db.save_cursor("lemmy", "Posts/7")
    await db.close()

    db2 = Database(path)
    await db2.connect()
    assert await db2.get_cursor("lemmy") == "Posts/7"
    await db2.close()


async def test_missing_secret_clear_error(tmp_path, monkeypatch):
    import pytest

    from hunter.config import ConfigError, load_config

    monkeypatch.delenv("HUNTER_TELEGRAM_BOT_TOKEN", raising=False)
    # Run from a directory without .env so the real one can't satisfy the check.
    monkeypatch.chdir(tmp_path)
    config_path = Path(__file__).parent / "fixtures" / "config_test.yaml"
    with pytest.raises(ConfigError) as exc:
        load_config(config_path, require_token=True)
    assert "HUNTER_TELEGRAM_BOT_TOKEN" in str(exc.value)


def test_env_file_loading(tmp_path, monkeypatch):
    from hunter.config import load_env_file

    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "HUNTER_TELEGRAM_BOT_TOKEN=123:abc\n"
        "HUNTER_GITHUB_TOKEN=\"ghp_quoted\"\n"
        "\n"
        "NO_EQUALS_SIGN\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("HUNTER_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("HUNTER_GITHUB_TOKEN", raising=False)
    load_env_file(env_file)
    assert os.environ["HUNTER_TELEGRAM_BOT_TOKEN"] == "123:abc"
    assert os.environ["HUNTER_GITHUB_TOKEN"] == "ghp_quoted"
    # Existing environment values must NOT be overwritten.
    monkeypatch.setenv("HUNTER_TELEGRAM_BOT_TOKEN", "real-token")
    load_env_file(env_file)
    assert os.environ["HUNTER_TELEGRAM_BOT_TOKEN"] == "real-token"
