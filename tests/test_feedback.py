"""Feedback handler tests: votes record even without a pipeline-created link row."""

from __future__ import annotations

from hunter.db import Database
from hunter.feedback import FeedbackHandler


class FakeQuery:
    """Minimal stand-in for a telegram CallbackQuery."""

    def __init__(self, data: str) -> None:
        self.data = data
        self.answered = False
        self.message = None

    async def answer(self, *args, **kwargs) -> None:
        self.answered = True


async def test_vote_records_without_existing_link_row(db: Database):
    state = type("S", (), {})()  # simple namespace-like state
    state.db = db
    state.cfg = None
    handler = FeedbackHandler(state)  # type: ignore[arg-type]

    query = FakeQuery("vote:worked:SYNTHCODE99")
    await handler.handle(query)

    assert query.answered
    votes = await db.conn.execute_fetchall("SELECT code, vote FROM votes")
    assert [(v["code"], v["vote"]) for v in votes] == [("SYNTHCODE99", "worked")]
    # The defensive upsert created the link row.
    link = await db.get_link("SYNTHCODE99")
    assert link is not None
    assert link.status == "worked"


async def test_vote_updates_source_score_and_author_streak(db: Database):
    state = type("S", (), {})()
    state.db = db
    state.cfg = None
    handler = FeedbackHandler(state)  # type: ignore[arg-type]

    # Simulate a real sighting first.
    await db.conn.execute(
        "INSERT INTO links(code, first_seen, last_seen, status) "
        "VALUES ('REALCODE12', '2026-11-19T00:00:00+00:00', "
        "'2026-11-19T00:00:00+00:00', 'alerted')"
    )
    await db.conn.execute(
        "INSERT INTO sightings(code, source, author, permalink, posted_at, fetched_at) "
        "VALUES ('REALCODE12', 'lemmy', 'badactor', 'https://x/1', NULL, "
        "'2026-11-19T00:00:00+00:00')"
    )
    await db.conn.commit()

    query = FakeQuery("vote:dead:REALCODE12")
    await handler.handle(query)

    streak = await db.conn.execute_fetchall(
        "SELECT consecutive_dead FROM authors WHERE handle = 'badactor'"
    )
    assert streak[0]["consecutive_dead"] == 1
    link = await db.get_link("REALCODE12")
    assert link is not None and link.status == "dead"
