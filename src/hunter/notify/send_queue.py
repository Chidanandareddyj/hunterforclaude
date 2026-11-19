"""Serialized Telegram send queue with rate limiting, retry and backoff.

One background task drains the queue, so sends never interleave and rate
limits are respected. Message IDs are stored via `on_sent` callbacks so the
feedback handler can edit alerts later.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger

log = get_logger(component="send_queue")

# Telegram global limit ~30 msg/s; per chat ~1 msg/s. We serialize, so 1.05s
# spacing is safe and unobtrusive for a single-owner bot.
SEND_SPACING = 1.05
MAX_RETRIES = 4


@dataclass(slots=True)
class OutgoingMessage:
    chat_id: int
    text: str
    parse_mode: str = "HTML"
    silent: bool = False
    reply_markup: Any = None
    on_sent: Callable[[int], Awaitable[None]] | None = None  # receives message_id
    retries: int = field(default=0)


class SendQueue:
    def __init__(self, bot: Any, chat_id: int | None) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self._queue: asyncio.Queue[OutgoingMessage | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._last_sent = 0.0
        self._paused = asyncio.Event()
        self._paused.set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._drain(), name="send-queue")

    async def stop(self) -> None:
        if self._task:
            await self._queue.put(None)
            await self._task
            self._task = None

    def pause(self) -> None:
        self._paused.clear()

    def resume(self) -> None:
        self._paused.set()

    def enqueue(self, msg: OutgoingMessage) -> None:
        self._queue.put_nowait(msg)

    async def _drain(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            msg = await self._queue.get()
            if msg is None:
                return
            await self._paused.wait()
            if self.chat_id is not None and msg.chat_id != self.chat_id:
                log.warning("dropping send to non-owner chat", chat_id=msg.chat_id)
                continue
            # Rate limit spacing
            wait = SEND_SPACING - (loop.time() - self._last_sent)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                sent = await self._send_with_retry(msg)
            except Exception as exc:  # permanent failure after retries
                log.error("send failed permanently", error=str(exc))
                continue
            self._last_sent = loop.time()
            if msg.on_sent and sent is not None:
                try:
                    await msg.on_sent(int(sent))
                except Exception as exc:
                    log.error("on_sent callback failed", error=str(exc))

    async def _send_with_retry(self, msg: OutgoingMessage) -> Any:
        delay = 1.0
        for attempt in range(MAX_RETRIES + 1):
            try:
                return await self.bot.send_message(
                    chat_id=msg.chat_id,
                    text=msg.text,
                    parse_mode=msg.parse_mode,
                    disable_notification=msg.silent,
                    disable_web_page_preview=True,
                    reply_markup=msg.reply_markup,
                )
            except Exception as exc:
                if attempt >= MAX_RETRIES:
                    raise
                log.warning(
                    "send retry",
                    attempt=attempt + 1,
                    delay=delay,
                    error=str(exc)[:200],
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
        raise RuntimeError("unreachable")
