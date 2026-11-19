"""M4: Feedback loop — buttons update scores and mutes.

- ✅ Worked: never re-alert; author gets a small score bump.
- ❌ Dead: author consecutive-dead counter; 3 in a row auto-mutes the author.
- 🔇 Mute source: marks the source muted (its dead-rate rule also auto-mutes).
- A source is auto-muted at >= source_dead_rate dead over >= source_min_votes.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from .config import AutoMute
from .logging_setup import get_logger

if TYPE_CHECKING:
    from telegram import CallbackQuery

    from .state import AppState

log = get_logger(component="feedback")

AUTHOR_DEAD_PENALTY = -0.2
AUTHOR_WORKED_BONUS = +0.1
SOURCE_DEAD_PENALTY = -0.05
SOURCE_WORKED_BONUS = +0.03


class FeedbackHandler:
    def __init__(self, state: AppState) -> None:
        self.state = state
        from .config import AutoMute

        self.mute_cfg: AutoMute = state.cfg.auto_mute if state.cfg else AutoMute()

    async def handle(self, query: CallbackQuery) -> None:
        data = query.data or ""
        await query.answer()
        if data.startswith("vote:"):
            await self._handle_vote(query, data)
        elif data.startswith("mute:"):
            await self._handle_mute(query, data)

    async def _handle_vote(self, query: CallbackQuery, data: str) -> None:
        _, kind, code = data.split(":", 2)
        # Find author/source of the alert's first sighting for score updates.
        async with self.state.db.conn.execute(
            "SELECT source, author FROM sightings WHERE code = ? ORDER BY id LIMIT 1",
            (code,),
        ) as cur:
            row = await cur.fetchone()
        source = row["source"] if row else "unknown"
        author = row["author"] if row else "unknown"

        from .db import iso, utcnow

        # Defensive: ensure the link row exists so the vote always records
        # (the sample alert's synthetic code has no pipeline-created row).
        await self.state.db.conn.execute(
            "INSERT OR IGNORE INTO links(code, first_seen, last_seen, status) "
            "VALUES (?, ?, ?, 'alerted')",
            (code, iso(utcnow()), iso(utcnow())),
        )
        await self.state.db.conn.execute(
            "INSERT INTO votes(code, vote, voted_at) VALUES (?, ?, ?)",
            (code, kind, iso(utcnow())),
        )
        await self.state.db.conn.commit()
        await self.state.db.set_link_status(code, "worked" if kind == "worked" else "dead")

        if kind == "worked":
            await self.state.db.adjust_author_score(source, author, AUTHOR_WORKED_BONUS)
            await self.state.db.adjust_source_score(source, SOURCE_WORKED_BONUS)
            note = "✅ Marked worked. Never re-alerting this code."
        else:
            streak = await self.state.db.bump_author_consecutive_dead(source, author, reset=False)
            await self.state.db.adjust_author_score(source, author, AUTHOR_DEAD_PENALTY)
            score = await self.state.db.adjust_source_score(source, SOURCE_DEAD_PENALTY)
            note = f"❌ Marked dead. Author dead streak: {streak}."
            if streak >= self.mute_cfg.author_consecutive_dead:
                await self.state.db.set_author_muted(source, author, True)
                note += f" Author {author} auto-muted."
            # Source auto-mute rule
            votes, dead = await self.state.db.source_votes(source)
            if votes >= self.mute_cfg.source_min_votes and dead / votes >= self.mute_cfg.source_dead_rate:
                await self.state.db.set_source_muted(source, True)
                note += f" Source {source} auto-muted (dead rate {dead}/{votes})."
            _ = score

        await self._edit(query, note)

    async def _handle_mute(self, query: CallbackQuery, data: str) -> None:
        _, code = data.split(":", 1)
        async with self.state.db.conn.execute(
            "SELECT source FROM sightings WHERE code = ? ORDER BY id LIMIT 1",
            (code,),
        ) as cur:
            row = await cur.fetchone()
        source = row["source"] if row else None
        if source:
            await self.state.db.set_source_muted(source, True)
            await self._edit(query, f"🔇 Source {source} muted. /unmute source {source} to restore.")
        else:
            await self._edit(query, "🔇 Could not resolve the source for this alert.")

    async def _edit(self, query: CallbackQuery, note: str) -> None:
        from telegram import Message

        message = query.message
        if not isinstance(message, Message):
            return
        try:
            text = message.text or ""
        except Exception:
            text = ""
        # Trim the original alert to a compact line + outcome note.
        first_line = html.escape(text.splitlines()[0]) if text else "Alert"
        try:
            await message.edit_text(
                f"{first_line}\n\n<b>Outcome:</b> {html.escape(note)}",
                disable_web_page_preview=True,
            )
        except Exception as exc:  # message too old, identical text, etc.
            log.warning("edit failed", error=str(exc)[:200])
