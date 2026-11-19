"""SQLite layer: connection, migrations, and small typed accessors.

Every DB write from the pipeline happens inside one transaction (M1 task 6).
"""

from __future__ import annotations

import importlib.resources
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from .logging_setup import get_logger

log = get_logger(component="db")


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _migration_sql(version: int) -> str:
    name = f"{version:03d}_init.sql" if version == 1 else f"{version:03d}.sql"
    root = importlib.resources.files("hunter") / "migrations"
    return (root / name).read_text(encoding="utf-8")


# Number of numbered migration scripts shipped with the package.
MIGRATION_COUNT = 1


@dataclass(slots=True)
class MuteState:
    """Mute/author state snapshot used by the filter stage."""

    source_muted: bool = False
    source_score: float = 1.0
    author_muted: bool = False
    author_score: float = 1.0


@dataclass(slots=True)
class LinkState:
    code: str
    status: str
    first_seen: datetime
    last_seen: datetime
    seen_sources: int


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self.migrate()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected; call connect() first")
        return self._conn

    # -- migrations ---------------------------------------------------------

    async def migrate(self) -> None:
        async with self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ) as cur:
            exists = await cur.fetchone()
        current = 0
        if exists:
            async with self.conn.execute(
                "SELECT MAX(version) AS v FROM schema_version"
            ) as cur:
                row = await cur.fetchone()
            current = int(row["v"] or 0) if row else 0
        for version in range(current + 1, MIGRATION_COUNT + 1):
            sql = _migration_sql(version)
            log.info("applying migration", version=version)
            await self.conn.executescript(sql)
            await self.conn.execute(
                "INSERT INTO schema_version(version) VALUES (?)", (version,)
            )
            await self.conn.commit()

    async def migrate_dry(self) -> bool:
        """Idempotency check for tests: run migrate twice, expect no error."""
        await self.migrate()
        return True

    # -- sources ------------------------------------------------------------

    async def upsert_source(self, name: str, enabled: bool = True) -> None:
        await self.conn.execute(
            "INSERT INTO sources(name, enabled) VALUES (?, ?) "
            "ON CONFLICT(name) DO NOTHING",
            (name, int(enabled)),
        )
        await self.conn.commit()

    async def get_cursor(self, name: str) -> str | None:
        async with self.conn.execute(
            "SELECT cursor FROM sources WHERE name = ?", (name,)
        ) as cur:
            row = await cur.fetchone()
        return row["cursor"] if row else None

    async def save_cursor(self, name: str, cursor: str | None) -> None:
        await self.conn.execute(
            "UPDATE sources SET cursor = ?, last_poll = ? WHERE name = ?",
            (cursor, iso(utcnow()), name),
        )
        await self.conn.commit()

    async def record_error(self, name: str, error: str) -> None:
        await self.conn.execute(
            "UPDATE sources SET error_count = error_count + 1, last_error = ? "
            "WHERE name = ?",
            (error[:500], name),
        )
        await self.conn.commit()

    async def record_success(self, name: str) -> None:
        await self.conn.execute(
            "UPDATE sources SET error_count = 0, last_error = NULL, "
            "last_poll = ? WHERE name = ?",
            (iso(utcnow()), name),
        )
        await self.conn.commit()

    async def record_item_seen(self, name: str) -> None:
        await self.conn.execute(
            "UPDATE sources SET last_item_at = ? WHERE name = ?",
            (iso(utcnow()), name),
        )
        await self.conn.commit()

    async def source_health(self) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT name, enabled, last_poll, error_count, last_error, muted, "
            "last_item_at FROM sources ORDER BY name"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def set_source_muted(self, name: str, muted: bool) -> None:
        await self.conn.execute(
            "UPDATE sources SET muted = ? WHERE name = ?", (int(muted), name)
        )
        await self.conn.commit()

    async def adjust_source_score(self, name: str, delta: float) -> float:
        async with self.conn.execute(
            "UPDATE sources SET score = MAX(0.0, MIN(1.0, score + ?)) "
            "WHERE name = ? RETURNING score",
            (delta, name),
        ) as cur:
            row = await cur.fetchone()
        await self.conn.commit()
        return float(row["score"]) if row else 1.0

    # -- authors ------------------------------------------------------------

    async def author(self, source: str, handle: str) -> MuteState | None:
        async with self.conn.execute(
            "SELECT muted, score FROM authors WHERE source = ? AND handle = ?",
            (source, handle),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return MuteState(
            source_muted=False,
            author_muted=bool(row["muted"]),
            author_score=float(row["score"]),
        )

    async def upsert_author(self, source: str, handle: str) -> None:
        await self.conn.execute(
            "INSERT INTO authors(source, handle) VALUES (?, ?) "
            "ON CONFLICT(source, handle) DO NOTHING",
            (source, handle),
        )
        await self.conn.commit()

    async def adjust_author_score(self, source: str, handle: str, delta: float) -> float:
        await self.conn.execute(
            "INSERT INTO authors(source, handle) VALUES (?, ?) "
            "ON CONFLICT(source, handle) DO NOTHING",
            (source, handle),
        )
        async with self.conn.execute(
            "UPDATE authors SET score = MAX(0.0, MIN(1.0, score + ?)) "
            "WHERE source = ? AND handle = ? RETURNING score",
            (delta, source, handle),
        ) as cur:
            row = await cur.fetchone()
        await self.conn.commit()
        return float(row["score"]) if row else 1.0

    async def bump_author_consecutive_dead(self, source: str, handle: str, reset: bool) -> int:
        await self.conn.execute(
            "INSERT INTO authors(source, handle) VALUES (?, ?) "
            "ON CONFLICT(source, handle) DO NOTHING",
            (source, handle),
        )
        value = "consecutive_dead + 1" if not reset else "0"
        async with self.conn.execute(
            f"UPDATE authors SET consecutive_dead = {value} "
            "WHERE source = ? AND handle = ? RETURNING consecutive_dead",
            (source, handle),
        ) as cur:
            row = await cur.fetchone()
        await self.conn.commit()
        return int(row["consecutive_dead"]) if row else 0

    async def set_author_muted(self, source: str, handle: str, muted: bool) -> None:
        await self.conn.execute(
            "INSERT INTO authors(source, handle, muted) VALUES (?, ?, ?) "
            "ON CONFLICT(source, handle) DO UPDATE SET muted = excluded.muted",
            (source, handle, int(muted)),
        )
        await self.conn.commit()

    async def unmute_author(self, handle: str) -> int:
        async with self.conn.execute(
            "UPDATE authors SET muted = 0, consecutive_dead = 0 WHERE handle = ?",
            (handle,),
        ) as cur:
            count = cur.rowcount
        await self.conn.commit()
        return count if count and count > 0 else 0

    async def unmute_source(self, name: str) -> int:
        async with self.conn.execute(
            "UPDATE sources SET muted = 0 WHERE name = ?", (name,)
        ) as cur:
            count = cur.rowcount
        await self.conn.commit()
        return count if count and count > 0 else 0

    async def muted_sources(self) -> list[str]:
        async with self.conn.execute(
            "SELECT name FROM sources WHERE muted = 1"
        ) as cur:
            return [r["name"] for r in await cur.fetchall()]

    async def muted_authors(self) -> list[tuple[str, str]]:
        async with self.conn.execute(
            "SELECT source, handle FROM authors WHERE muted = 1"
        ) as cur:
            return [(r["source"], r["handle"]) for r in await cur.fetchall()]

    async def source_votes(self, name: str) -> tuple[int, int]:
        """(votes, dead_votes) for the source across all its sightings."""
        async with self.conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN v.vote = 'dead' THEN 1 ELSE 0 END) AS dead "
            "FROM votes v JOIN sightings s ON s.code = v.code "
            "WHERE s.source = ?",
            (name,),
        ) as cur:
            row = await cur.fetchone()
        total = int(row["total"] or 0) if row else 0
        dead = int(row["dead"] or 0) if row else 0
        return total, dead

    # -- links / sightings ---------------------------------------------------

    async def get_link(self, code: str) -> LinkState | None:
        async with self.conn.execute(
            "SELECT code, status, first_seen, last_seen FROM links WHERE code = ?",
            (code,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        n_sources = await self._count_sources(code)
        return LinkState(
            code=row["code"],
            status=row["status"],
            first_seen=parse_iso(row["first_seen"]),  # type: ignore[arg-type]
            last_seen=parse_iso(row["last_seen"]),  # type: ignore[arg-type]
            seen_sources=n_sources,
        )

    async def _count_sources(self, code: str) -> int:
        async with self.conn.execute(
            "SELECT COUNT(DISTINCT source) AS n FROM sightings WHERE code = ?",
            (code,),
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def set_link_status(self, code: str, status: str, feedback: str | None = None) -> None:
        await self.conn.execute(
            "UPDATE links SET status = ?, feedback = COALESCE(?, feedback) "
            "WHERE code = ?",
            (status, feedback, code),
        )
        await self.conn.commit()

    # -- alerts ---------------------------------------------------------------

    async def latest_alert(self, code: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM alerts WHERE code = ? ORDER BY sent_at DESC LIMIT 1",
            (code,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def alert_message_id(self, code: str) -> int | None:
        alert = await self.latest_alert(code)
        return int(alert["message_id"]) if alert and alert["message_id"] else None

    async def set_alert_message_id(self, alert_id: int, message_id: int) -> None:
        await self.conn.execute(
            "UPDATE alerts SET message_id = ? WHERE id = ?", (message_id, alert_id)
        )
        await self.conn.commit()

    async def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        async with self.conn.execute("SELECT COUNT(*) AS n FROM links") as cur:
            r = await cur.fetchone()
            out["links"] = int(r["n"]) if r else 0
        async with self.conn.execute("SELECT COUNT(*) AS n FROM alerts") as cur:
            r = await cur.fetchone()
            out["alerts"] = int(r["n"]) if r else 0
        async with self.conn.execute("SELECT COUNT(*) AS n FROM votes") as cur:
            r = await cur.fetchone()
            out["votes"] = int(r["n"]) if r else 0
        async with self.conn.execute(
            "SELECT vote, COUNT(*) AS n FROM votes GROUP BY vote"
        ) as cur:
            out["votes_by_kind"] = {r["vote"]: r["n"] for r in await cur.fetchall()}
        async with self.conn.execute(
            "SELECT s.source AS source, COUNT(v.id) AS votes, "
            "SUM(CASE WHEN v.vote='dead' THEN 1 ELSE 0 END) AS dead "
            "FROM sightings s JOIN votes v ON v.code = s.code "
            "GROUP BY s.source"
        ) as cur:
            out["per_source"] = [
                {"source": r["source"], "votes": r["votes"], "dead": r["dead"]}
                for r in await cur.fetchall()
            ]
        return out

    # -- settings (chat lock) --------------------------------------------------

    async def get_setting(self, key: str) -> str | None:
        async with self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()
