"""Telegram bot: chat lock, commands, inline buttons wiring.

- First run with no stored chat ID: the bot waits for /start from the owner,
  stores that chat ID, and from then on ignores every other chat.
- Commands: /pause, /resume, /stats, /sources, /unmute.
- Buttons: ✅ Worked, ❌ Dead, 🔇 Mute source (callbacks from locked chat only).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from ..logging_setup import get_logger
from .formatter import format_sample
from .send_queue import OutgoingMessage

if TYPE_CHECKING:
    from ..state import AppState

log = get_logger(component="telegram")

CHAT_ID_KEY = "owner_chat_id"

WELCOME = (
    "Hunter is listening. This chat is now the owner chat; messages from any "
    "other chat will be ignored."
)


class TelegramBot:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self.app: Application | None = None

    async def start(self) -> None:
        from ..state import AppState  # noqa: PLC0415 (avoid circular import at module load)

        assert isinstance(self.state, AppState)
        app = (
            Application.builder()
            .token(self.state.token)
            .concurrent_updates(False)
            .build()
        )
        app.add_handler(CommandHandler("start", self._cmd_start))
        app.add_handler(CommandHandler("pause", self._cmd_pause))
        app.add_handler(CommandHandler("resume", self._cmd_resume))
        app.add_handler(CommandHandler("stats", self._cmd_stats))
        app.add_handler(CommandHandler("sources", self._cmd_sources))
        app.add_handler(CommandHandler("unmute", self._cmd_unmute))
        app.add_handler(CallbackQueryHandler(self._on_callback))

        await app.initialize()
        await app.start()
        if app.updater:
            await app.updater.start_polling(allowed_updates=["message", "callback_query"])
        self.app = app

    async def stop(self) -> None:
        if not self.app:
            return
        if self.app.updater:
            await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
        self.app = None

    # -- chat lock -----------------------------------------------------------

    async def owner_chat_id(self) -> int | None:
        stored = await self.state.db.get_setting(CHAT_ID_KEY)
        return int(stored) if stored else None

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None:
            return
        chat_id = update.effective_chat.id
        stored = await self.state.db.get_setting(CHAT_ID_KEY)
        if stored and int(stored) != chat_id:
            # Locked to another chat: stay silent.
            return
        if not stored:
            await self.state.db.set_setting(CHAT_ID_KEY, str(chat_id))
            log.info("owner chat locked", chat_id=chat_id)
        if self.state.queue:
            self.state.queue.chat_id = chat_id
        if update.message:
            await update.message.reply_text(WELCOME, disable_web_page_preview=True)

    # -- commands -------------------------------------------------------------

    def _only_owner(self, update: Update) -> bool:
        return update.effective_chat is not None

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._only_owner(update):
            return
        self.state.paused = True
        if self.state.queue:
            self.state.queue.pause()
        if update.effective_message:
            await update.effective_message.reply_text("Paused. Alerts are queued, not sent.")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._only_owner(update):
            return
        self.state.paused = False
        if self.state.queue:
            self.state.queue.resume()
        if update.effective_message:
            await update.effective_message.reply_text("Resumed.")

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._only_owner(update):
            return
        stats = await self.state.db.stats()
        lines = [
            f"Links seen: {stats['links']}",
            f"Alerts sent: {stats['alerts']}",
            f"Votes: {stats['votes']} {stats['votes_by_kind']}",
        ]
        for row in stats["per_source"]:
            dead = row["dead"] or 0
            lines.append(f"  {row['source']}: {row['votes']} votes, {dead} dead")
        if update.effective_message:
            await update.effective_message.reply_text("\n".join(lines))

    async def _cmd_sources(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._only_owner(update):
            return
        rows = await self.state.db.source_health()
        lines = []
        for r in rows:
            status = (
                "muted" if r["muted"]
                else (f"errors: {r['error_count']}" if r["error_count"] else "ok")
            )
            lines.append(
                f"  {r['name']}: {status}, last poll {r['last_poll'] or 'never'}, "
                f"last item {r['last_item_at'] or 'never'}"
            )
        if update.effective_message:
            await update.effective_message.reply_text(
                "Sources:\n" + ("\n".join(lines) or "  none registered")
            )

    async def _cmd_unmute(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._only_owner(update):
            return
        if not context.args:
            muted_s = await self.state.db.muted_sources()
            muted_a = await self.state.db.muted_authors()
            lines = [f"sources: {muted_s or 'none'}"] + [
                f"authors: {a or 'none'}" for a in [muted_a]
            ]
            if update.effective_message:
                await update.effective_message.reply_text(
                    "Usage: /unmute source <name> | /unmute author <handle>\n"
                    + "\n".join(lines)
                )
            return
        kind, _, name = context.args[0].partition(" ")
        name = name.strip() or kind
        if kind == "source":
            n = await self.state.db.unmute_source(name)
        else:
            n = await self.state.db.unmute_author(name)
        if update.effective_message:
            await update.effective_message.reply_text("Unmuted." if n else "No matching muted entry.")

    # -- buttons ---------------------------------------------------------------

    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.data is None:
            return
        chat_id = update.effective_chat.id if update.effective_chat else None
        owner = await self.owner_chat_id()
        if owner is None or chat_id != owner:
            await query.answer()  # silently ignore non-owner presses
            return
        feedback = self.state.feedback
        if feedback is None:
            await query.answer()
            return
        await feedback.handle(query)

    # -- sending ----------------------------------------------------------------

    async def send_alert(
        self,
        chat_id: int,
        text: str,
        *,
        silent: bool,
        code: str,
        on_sent,
    ) -> None:
        assert self.state.queue is not None
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Worked", callback_data=f"vote:worked:{code}"),
                    InlineKeyboardButton("❌ Dead", callback_data=f"vote:dead:{code}"),
                ],
                [InlineKeyboardButton("🔇 Mute source", callback_data=f"mute:{code}")],
            ]
        )
        self.state.queue.enqueue(
            OutgoingMessage(
                chat_id=chat_id,
                text=text,
                silent=silent,
                reply_markup=markup,
                on_sent=on_sent,
            )
        )

    async def send_test(self, chat_id: int) -> None:
        assert self.state.queue is not None
        self.state.queue.enqueue(
            OutgoingMessage(chat_id=chat_id, text=format_sample(), silent=False)
        )
