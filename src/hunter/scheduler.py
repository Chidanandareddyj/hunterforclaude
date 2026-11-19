"""Scheduler: run collectors concurrently, with failure isolation.

One collector failing never stops the others (cross-cutting rule); each
collector owns its own error handling and health tracking in base.run().
Also hosts the daily heartbeat and health-alert loops (M5).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from .collectors.base import Collector
from .config import Config
from .db import Database
from .logging_setup import get_logger
from .notify.send_queue import OutgoingMessage, SendQueue

log = get_logger(component="scheduler")


class Scheduler:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        on_items,
        queue: SendQueue | None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.on_items = on_items
        self.queue = queue
        self.collectors: list[Collector] = []
        self.stop = asyncio.Event()

    def build_collectors(self) -> None:
        import httpx

        from .collectors import (
            BlueskyCollector,
            BlueskySearchCollector,
            ClaudeCoworkCourseCollector,
            GitHubCollector,
            HackerNewsCollector,
            LemmyCollector,
            MastodonCollector,
            OneParrainageCollector,
            RedditCollector,
            TelegramPreviewCollector,
            WebSearchCollector,
        )

        client = httpx.AsyncClient(timeout=30, follow_redirects=False)
        s = self.cfg.sources
        self.collectors = [
            LemmyCollector(self.db, s.lemmy, client),
            BlueskyCollector(self.db, s.bluesky, client),
            HackerNewsCollector(self.db, s.hackernews, client),
            ClaudeCoworkCourseCollector(self.db, s.claudecoworkcourse, client),
            OneParrainageCollector(self.db, s.oneparrainage, client, gates_ok=False),
            TelegramPreviewCollector(self.db, s.telegram_preview, client),
            BlueskySearchCollector(self.db, s.bluesky_search, client),
            RedditCollector(self.db, s.reddit, client),
            MastodonCollector(self.db, s.mastodon, client),
            GitHubCollector(self.db, s.github, client),
            WebSearchCollector(self.db, s.web_search, client),
        ]

    async def run(self) -> None:
        self.build_collectors()
        tasks = [
            asyncio.create_task(c.run(self.on_items, self.stop), name=f"collector-{c.name}")
            for c in self.collectors
        ]
        monitor = asyncio.create_task(self._health_loop(), name="health")
        if self.cfg.heartbeat.time:
            hb = asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
            tasks.append(hb)
        try:
            await self.stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            monitor.cancel()
            await asyncio.gather(*tasks, monitor, return_exceptions=True)
        log.info("scheduler stopped")

    # -- health alerts ---------------------------------------------------------

    async def _health_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=300)
                return
            except TimeoutError:
                pass
            await self._check_health()

    async def _check_health(self) -> None:
        rows = await self.db.source_health()
        now = datetime.now(UTC)
        for r in rows:
            if r["muted"] or not r["enabled"]:
                continue
            msgs = []
            if r["last_poll"]:
                since = (now - datetime.fromisoformat(r["last_poll"])).total_seconds()
                if since > self.cfg.health.silent_after_hours * 3600:
                    msgs.append(f"silent for {since / 3600:.1f}h")
            if r["error_count"] >= 3 and r["last_poll"]:
                since = (now - datetime.fromisoformat(r["last_poll"])).total_seconds()
                if since > self.cfg.health.error_after_minutes * 60:
                    msgs.append(f"erroring for {since / 60:.0f}m: {r['last_error']}")
            if msgs and self.queue:
                text = f"⚠️ <b>Source health</b>\n{r['name']}: " + "; ".join(msgs)
                self.queue.enqueue(
                    OutgoingMessage(chat_id=await self._owner_chat(), text=text, silent=True)
                )

    async def _owner_chat(self) -> int:
        stored = await self.db.get_setting("owner_chat_id")
        return int(stored) if stored else 0

    # -- heartbeat --------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while not self.stop.is_set():
            try:
                delay = self._seconds_until(self.cfg.heartbeat.time)
                await asyncio.wait_for(self.stop.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            await self._send_heartbeat()

    def _seconds_until(self, hhmm: str) -> float:
        try:
            hh, mm = hhmm.split(":")
            target_h, target_m = int(hh), int(mm)
        except ValueError:
            log.warning("invalid heartbeat time", time=hhmm)
            return 86400
        now = datetime.now()
        target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def _send_heartbeat(self) -> None:
        stats = await self.db.stats()
        rows = await self.db.source_health()
        healthy = sum(1 for r in rows if r["error_count"] == 0 and not r["muted"])
        text = (
            "💓 <b>Hunter alive</b>\n"
            f"Links seen: {stats['links']}, alerts: {stats['alerts']}, "
            f"sources healthy: {healthy}/{len(rows)}"
        )
        if self.queue:
            self.queue.enqueue(
                OutgoingMessage(chat_id=await self._owner_chat(), text=text, silent=True)
            )
