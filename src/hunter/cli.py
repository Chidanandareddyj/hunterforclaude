"""CLI entry point and run modes (M2 task 6).

Modes:
  hunter run [--dry-run] [--replay FILE] [--send-test] [-v]
  --dry-run        full pipeline, print alerts to console, never send
  --replay FILE    feed saved RawItems (JSONL) through the pipeline
  --send-test      send one sample alert to the locked chat and exit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .db import Database, iso, utcnow
from .logging_setup import get_logger, setup_logging
from .models import RawItem
from .notify.formatter import format_alert, format_sample
from .pipeline import Pipeline
from .state import AppState

log = get_logger(component="cli")


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hunter", description="Claude guest-pass referral monitor")
    p.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    p.add_argument("--dry-run", action="store_true", help="run pipeline, print alerts, never send")
    p.add_argument("--replay", type=Path, default=None, help="JSONL file of RawItems to replay")
    p.add_argument("--send-test", action="store_true", help="send one sample alert and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


async def cmd_run(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    try:
        cfg = load_config(args.config, require_token=not args.dry_run and args.replay is None)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    db = Database(cfg.db_path)
    await db.connect()
    pipeline = Pipeline(cfg, db)

    if args.replay:
        return await _run_replay(args, cfg, db, pipeline)

    if args.send_test:
        return await _run_send_test(args, cfg, db)

    if args.dry_run:
        return await _run_dry(cfg, db, pipeline)

    return await _run_live(cfg, db, pipeline)


async def _run_replay(args, cfg, db, pipeline) -> int:
    if not args.replay.exists():
        print(f"error: replay file not found: {args.replay}", file=sys.stderr)
        return 2
    sent = 0
    with args.replay.open(encoding="utf-8") as fh:
        async for line in _lines(fh):
            if not line.strip():
                continue
            try:
                item = RawItem(**json.loads(line))
            except Exception as exc:
                print(f"skipping malformed line: {exc}", file=sys.stderr)
                continue
            result = await pipeline.process(item)
            if result.alert:
                sent += 1
                text = format_alert(result.alert, cfg.canonical_link_prefix)
                silent = result.alert.silent
                prefix = "[SILENT] " if silent else ""
                if args.dry_run:
                    print(f"{prefix}{text}\n")
                else:
                    await _send_via_telegram(cfg, db, result.alert, text)
    print(f"replay done: {sent} alerts", file=sys.stderr)
    await db.close()
    return 0


async def _lines(fh):
    for line in fh:
        yield line


async def _run_dry(cfg, db, pipeline) -> int:
    """Dry-run live mode: collectors run, alerts print to console, nothing sends."""
    alerts: list = []

    async def on_items(items: list[RawItem]) -> None:
        for item in items:
            result = await pipeline.process(item)
            if result.alert:
                alerts.append(result.alert)
                print(format_alert(result.alert, cfg.canonical_link_prefix))
                print()

    from .scheduler import Scheduler

    sched = Scheduler(cfg, db, on_items, queue=None)
    print("hunter dry-run: collectors starting; Ctrl+C to stop.", file=sys.stderr)
    try:
        await sched.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        sched.stop.set()
    await db.close()
    return 0


async def _run_live(cfg, db, pipeline) -> int:
    import os

    token = os.environ["HUNTER_TELEGRAM_BOT_TOKEN"]
    from .feedback import FeedbackHandler
    from .notify.send_queue import SendQueue
    from .notify.telegram import TelegramBot
    from .scheduler import Scheduler

    state = AppState(token=token, db=db, dry_run=False)
    bot = TelegramBot(state)
    state.feedback = FeedbackHandler(state)
    state.feedback.mute_cfg = cfg.auto_mute
    state.queue = None

    alerts_sent = 0

    async def on_items(items: list[RawItem]) -> None:
        nonlocal alerts_sent
        for item in items:
            if state.paused:
                continue
            result = await pipeline.process(item)
            if not result.alert:
                continue
            alert = result.alert
            chat_id = await bot.owner_chat_id()
            if chat_id is None:
                log.warning("no owner chat yet; dropping alert", code=alert.code)
                continue
            if state.queue is None:
                continue

            async def on_sent(message_id: int, row_id=alert.row_id) -> None:
                if row_id:
                    await db.set_alert_message_id(row_id, message_id)

            text = format_alert(alert, cfg.canonical_link_prefix)
            await bot.send_alert(
                chat_id, text, silent=alert.silent, code=alert.code, on_sent=on_sent
            )
            alerts_sent += 1

    scheduler = Scheduler(cfg, db, on_items, queue=None)
    scheduler.stop = asyncio.Event()
    try:
        await bot.start()
        chat_id = await bot.owner_chat_id()
        queue = SendQueue(bot.app.bot if bot.app else None, chat_id)
        queue.start()
        state.queue = queue
        scheduler.queue = queue
        await scheduler.run()
    finally:
        if state.queue:
            await state.queue.stop()
        await bot.stop()
        await db.close()
    return 0


async def _send_via_telegram(cfg, db, alert, text) -> None:
    """Used by replay without --dry-run: real sends need the chat locked."""
    import os

    token = os.environ.get("HUNTER_TELEGRAM_BOT_TOKEN")
    if not token:
        print("error: real send needs HUNTER_TELEGRAM_BOT_TOKEN", file=sys.stderr)
        return
    import httpx

    chat = await db.get_setting("owner_chat_id")
    if not chat:
        print("error: no owner chat stored yet; run once and /start the bot", file=sys.stderr)
        return
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": int(chat),
                "text": text,
                "parse_mode": "HTML",
                "disable_notification": alert.silent,
                "link_preview_options": {"is_disabled": True},
            },
        )
        resp.raise_for_status()
    await db.conn.execute(
        "INSERT INTO alerts(code, sent_at, silent, confidence) VALUES (?, ?, ?, ?)",
        (alert.code, iso(utcnow()), int(alert.silent), alert.confidence),
    )
    await db.conn.commit()


async def _run_send_test(args, cfg, db) -> int:
    import os

    token = os.environ.get("HUNTER_TELEGRAM_BOT_TOKEN")
    if not token:
        print("error: HUNTER_TELEGRAM_BOT_TOKEN is not set (see .env.example)", file=sys.stderr)
        return 2
    import httpx

    chat = await db.get_setting("owner_chat_id")
    if chat:
        # Chat already locked: deliver directly, no bot polling needed.
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": int(chat),
                    "text": format_sample(),
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                },
            )
            resp.raise_for_status()
            print("Sample alert delivered.")
        await db.close()
        return 0

    # Bootstrap: no owner chat stored yet. Start the bot and wait for /start
    # (an /start sent earlier is still queued at Telegram and will arrive).
    from .feedback import FeedbackHandler
    from .notify.send_queue import SendQueue
    from .notify.telegram import TelegramBot
    from .state import AppState

    state = AppState(token=token, db=db, dry_run=True)
    state.feedback = FeedbackHandler(state)
    bot = TelegramBot(state)
    print(
        "Waiting for /start: open your bot in Telegram and send /start "
        "(if you already sent it, it will arrive shortly)."
    )
    await bot.start()
    queue = SendQueue(bot.app.bot if bot.app else None, None)
    queue.start()
    state.queue = queue
    try:
        for _ in range(120):  # wait up to 2 minutes
            chat = await db.get_setting("owner_chat_id")
            if chat:
                break
            await asyncio.sleep(1)
        else:
            print(
                "Timed out waiting for /start. Send /start to your bot, then run "
                "hunter --send-test again.",
                file=sys.stderr,
            )
            return 1
        await bot.send_test(int(chat))
        await queue.stop()  # drains the queue: the sample is sent before we return
        print("Sample alert delivered.")
        return 0
    finally:
        await bot.stop()


def main() -> int:
    # Windows consoles default to a legacy codepage that cannot encode the
    # emoji used in alert output; force UTF-8.
    import contextlib

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(OSError, ValueError):
                stream.reconfigure(encoding="utf-8", errors="replace")
    args = _build_arg_parser().parse_args()
    try:
        return asyncio.run(cmd_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
