"""Offline tests for the Telegram send path.

Every network boundary is faked: ``FakeBot`` records ``send_message`` calls and
the ``Update``/``CallbackQuery`` objects are simple stand-ins. No test polls
Telegram or sends a real message.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from telegram import Update

from hunter.db import Database
from hunter.notify import send_queue as send_queue_module
from hunter.notify.send_queue import OutgoingMessage, SendQueue
from hunter.notify.telegram import TelegramBot
from hunter.state import AppState


class FakeSentMessage:
    """Stand-in for telegram.Message; int(message) yields the message_id."""

    def __init__(self, message_id: int) -> None:
        self.message_id = message_id

    def __int__(self) -> int:
        return self.message_id


class FakeBot:
    """Records send_message calls; optionally fails the first N attempts."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_remaining = fail_times

    async def send_message(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.fail_remaining > 0:
            self.fail_remaining -= 1
            raise RuntimeError("transient failure")
        return FakeSentMessage(1000 + len(self.calls))


class _AsyncioWithFakeSleep:
    """Proxy that records sleep delays but yields instead of really waiting."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.sleeps: list[float] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        await self._real.sleep(0)


@pytest.fixture(autouse=True)
def _fast_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep drain tests instant; the rate-limit test opts back in."""
    monkeypatch.setattr(send_queue_module, "SEND_SPACING", 0.0)


# -- SendQueue ----------------------------------------------------------------


async def test_enqueue_drain_and_chat_lock() -> None:
    bot = FakeBot()
    queue = SendQueue(bot, chat_id=111)
    received: list[int] = []

    async def on_sent(message_id: int) -> None:
        received.append(message_id)

    queue.start()
    queue.enqueue(OutgoingMessage(chat_id=111, text="for-owner", on_sent=on_sent))
    queue.enqueue(OutgoingMessage(chat_id=999, text="for-intruder"))
    await queue.stop()

    assert len(bot.calls) == 1
    assert bot.calls[0]["chat_id"] == 111
    assert bot.calls[0]["parse_mode"] == "HTML"
    assert bot.calls[0]["disable_notification"] is False
    assert bot.calls[0]["disable_web_page_preview"] is True
    assert received == [1001]


async def test_bootstrap_queue_without_chat_id_sends() -> None:
    """During /start bootstrap the queue has no chat_id until /start locks it."""
    bot = FakeBot()
    queue = SendQueue(bot, chat_id=None)
    queue.start()
    queue.enqueue(OutgoingMessage(chat_id=111, text="sample"))
    await queue.stop()
    assert len(bot.calls) == 1


async def test_pause_holds_messages_and_resume_drains() -> None:
    bot = FakeBot()
    queue = SendQueue(bot, chat_id=111)
    queue.start()
    queue.pause()
    queue.enqueue(OutgoingMessage(chat_id=111, text="queued"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert bot.calls == []
    queue.resume()
    await queue.stop()
    assert len(bot.calls) == 1


async def test_stop_while_paused_does_not_hang() -> None:
    bot = FakeBot()
    queue = SendQueue(bot, chat_id=111)
    queue.start()
    queue.pause()
    await asyncio.sleep(0)
    await asyncio.wait_for(queue.stop(), timeout=1.0)
    assert bot.calls == []


async def test_rate_limit_spacing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(send_queue_module, "SEND_SPACING", 1.05)
    fake_asyncio = _AsyncioWithFakeSleep(asyncio)
    monkeypatch.setattr(send_queue_module, "asyncio", fake_asyncio)

    bot = FakeBot()
    queue = SendQueue(bot, chat_id=111)
    queue._last_sent = asyncio.get_running_loop().time()
    queue.start()
    queue.enqueue(OutgoingMessage(chat_id=111, text="one"))
    queue.enqueue(OutgoingMessage(chat_id=111, text="two"))
    await queue.stop()

    assert len(bot.calls) == 2
    assert len(fake_asyncio.sleeps) == 2
    assert all(1.0 <= delay <= 1.05 for delay in fake_asyncio.sleeps)


async def test_retry_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(send_queue_module, "MAX_RETRIES", 3)
    fake_asyncio = _AsyncioWithFakeSleep(asyncio)
    monkeypatch.setattr(send_queue_module, "asyncio", fake_asyncio)

    bot = FakeBot(fail_times=2)
    queue = SendQueue(bot, chat_id=111)
    received: list[int] = []

    async def on_sent(message_id: int) -> None:
        received.append(message_id)

    queue.start()
    queue.enqueue(OutgoingMessage(chat_id=111, text="retry", on_sent=on_sent))
    await queue.stop()

    assert len(bot.calls) == 3
    assert fake_asyncio.sleeps == [1.0, 2.0]
    assert received == [1003]


async def test_permanent_failure_skips_on_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(send_queue_module, "MAX_RETRIES", 1)
    fake_asyncio = _AsyncioWithFakeSleep(asyncio)
    monkeypatch.setattr(send_queue_module, "asyncio", fake_asyncio)

    bot = FakeBot(fail_times=99)
    queue = SendQueue(bot, chat_id=111)
    received: list[int] = []

    async def on_sent(message_id: int) -> None:
        received.append(message_id)

    queue.start()
    queue.enqueue(OutgoingMessage(chat_id=111, text="doomed", on_sent=on_sent))
    await queue.stop()

    assert len(bot.calls) == 2  # initial attempt + one retry
    assert received == []


# -- TelegramBot handlers (no polling, no network) ----------------------------


class FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, chat_id: int | None) -> None:
        self.effective_chat = FakeChat(chat_id) if chat_id is not None else None
        self.effective_message = FakeMessage()
        self.message = self.effective_message
        self.callback_query: Any = None


class FakeContext:
    def __init__(self, args: list[str] | None = None) -> None:
        self.args = args or []


class FakeCallbackQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answered = False

    async def answer(self) -> None:
        self.answered = True


class RecordingFeedback:
    def __init__(self) -> None:
        self.handled: list[str] = []

    async def handle(self, query: Any) -> None:
        self.handled.append(str(query.data))


def _update(update: FakeUpdate) -> Update:
    return cast(Update, update)


def _context(args: list[str] | None = None) -> Any:
    return cast(Any, FakeContext(args))


def _bot(state: AppState) -> TelegramBot:
    return TelegramBot(state)


async def test_owner_chat_id_reads_db_setting(db: Database) -> None:
    state = AppState(token="fake", db=db)
    bot = _bot(state)
    assert await bot.owner_chat_id() is None

    await db.set_setting("owner_chat_id", "987654")
    assert await bot.owner_chat_id() == 987654


async def test_start_locks_owner_chat_and_sets_queue_chat(db: Database) -> None:
    state = AppState(token="fake", db=db)
    bot = _bot(state)
    queue = SendQueue(FakeBot(), chat_id=None)
    state.queue = queue

    first = FakeUpdate(111)
    await bot._cmd_start(_update(first), _context())
    assert await db.get_setting("owner_chat_id") == "111"
    assert queue.chat_id == 111
    assert len(first.message.replies) == 1

    second = FakeUpdate(222)
    await bot._cmd_start(_update(second), _context())
    assert await db.get_setting("owner_chat_id") == "111"
    assert queue.chat_id == 111
    assert second.message.replies == []

    no_chat = FakeUpdate(None)
    await bot._cmd_start(_update(no_chat), _context())
    assert await db.get_setting("owner_chat_id") == "111"


async def test_commands_require_locked_owner_chat(db: Database) -> None:
    await db.set_setting("owner_chat_id", "111")
    state = AppState(token="fake", db=db)
    bot = _bot(state)
    queue = SendQueue(FakeBot(), chat_id=111)
    state.queue = queue

    outsider = FakeUpdate(222)
    await bot._cmd_pause(_update(outsider), _context())
    assert state.paused is False
    assert queue._paused.is_set()
    assert outsider.message.replies == []

    owner = FakeUpdate(111)
    await bot._cmd_pause(_update(owner), _context())
    assert state.paused is True
    assert not queue._paused.is_set()
    assert owner.message.replies == ["Paused. Alerts are queued, not sent."]

    owner2 = FakeUpdate(111)
    await bot._cmd_resume(_update(owner2), _context())
    assert state.paused is False
    assert queue._paused.is_set()
    assert owner2.message.replies == ["Resumed."]


async def test_commands_denied_before_owner_lock(db: Database) -> None:
    state = AppState(token="fake", db=db)
    bot = _bot(state)
    update = FakeUpdate(111)
    await bot._cmd_pause(_update(update), _context())
    assert state.paused is False
    assert update.message.replies == []


async def test_stats_and_sources_only_owner(db: Database) -> None:
    await db.set_setting("owner_chat_id", "111")
    state = AppState(token="fake", db=db)
    bot = _bot(state)

    outsider = FakeUpdate(222)
    await bot._cmd_stats(_update(outsider), _context())
    await bot._cmd_sources(_update(outsider), _context())
    assert outsider.message.replies == []

    stats = FakeUpdate(111)
    await bot._cmd_stats(_update(stats), _context())
    assert stats.message.replies and "Links seen:" in stats.message.replies[0]

    sources = FakeUpdate(111)
    await bot._cmd_sources(_update(sources), _context())
    assert sources.message.replies and "Sources:" in sources.message.replies[0]


async def test_unmute_uses_all_argument_tokens(db: Database) -> None:
    await db.set_setting("owner_chat_id", "111")
    await db.upsert_source("bluesky")
    await db.set_source_muted("bluesky", True)
    await db.set_author_muted("lemmy", "badactor", True)
    state = AppState(token="fake", db=db)
    bot = _bot(state)

    usage = FakeUpdate(111)
    await bot._cmd_unmute(_update(usage), _context())
    assert usage.message.replies and "Usage:" in usage.message.replies[0]

    by_source = FakeUpdate(111)
    await bot._cmd_unmute(_update(by_source), _context(["source", "bluesky"]))
    assert await db.muted_sources() == []
    assert by_source.message.replies == ["Unmuted."]

    by_author = FakeUpdate(111)
    await bot._cmd_unmute(_update(by_author), _context(["author", "badactor"]))
    assert await db.muted_authors() == []
    assert by_author.message.replies == ["Unmuted."]


async def test_callbacks_only_owner(db: Database) -> None:
    await db.set_setting("owner_chat_id", "111")
    state = AppState(token="fake", db=db)
    feedback = RecordingFeedback()
    state.feedback = cast(Any, feedback)
    bot = _bot(state)

    outsider = FakeUpdate(222)
    outsider.callback_query = FakeCallbackQuery("vote:worked:ABC")
    await bot._on_callback(_update(outsider), _context())
    assert feedback.handled == []
    assert outsider.callback_query.answered is True

    owner = FakeUpdate(111)
    owner.callback_query = FakeCallbackQuery("vote:worked:ABC")
    await bot._on_callback(_update(owner), _context())
    assert feedback.handled == ["vote:worked:ABC"]


async def test_send_alert_enqueues_with_buttons(db: Database) -> None:
    state = AppState(token="fake", db=db)
    queue = SendQueue(FakeBot(), chat_id=111)
    state.queue = queue
    bot = _bot(state)

    async def on_sent(message_id: int) -> None:
        return None

    await bot.send_alert(111, "text", silent=True, code="ABC", on_sent=on_sent)
    msg = queue._queue.get_nowait()
    assert msg is not None
    assert msg.chat_id == 111
    assert msg.silent is True
    assert msg.on_sent is on_sent
    assert msg.reply_markup is not None

    await bot.send_test(111)
    sample = queue._queue.get_nowait()
    assert sample is not None
    assert "Referral spotted" in sample.text
