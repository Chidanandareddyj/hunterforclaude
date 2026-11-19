"""Collector base: cursor persistence, interval with jitter, exponential
backoff, per-source health, enable flag (DESIGN.md §4.1, M3 common base)."""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from ..db import Database
from ..logging_setup import get_logger

log = get_logger(component="collector")


@dataclass(slots=True)
class RobotsPolicy:
    """Simple robots.txt result for page-polling collectors."""

    allowed: bool
    note: str = ""


class Collector(ABC):
    name: str = "base"
    interval_seconds: float = 30.0
    enabled: bool = True
    trust: float = 0.5
    needs_http: bool = False

    def __init__(self, db: Database, client: httpx.AsyncClient | None = None) -> None:
        self.db = db
        self._client = client
        self._owns_client = client is None
        self.backoff = 1.0

    async def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30, follow_redirects=False)
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client:
            await self._client.aclose()
            self._client = None

    # -- lifecycle ------------------------------------------------------------

    async def run(
        self,
        on_items: Callable[[list], Awaitable[None]],
        stop: asyncio.Event,
    ) -> None:
        """Poll loop with jitter and exponential backoff. One failing collector
        never affects others: exceptions are caught here, and health recorded."""
        await self.db.upsert_source(self.name, enabled=self.enabled)
        if not self.enabled:
            log.info("collector disabled", source=self.name)
            return
        while not stop.is_set():
            try:
                items = await self.poll()
                self.backoff = 1.0
                await self.db.record_success(self.name)
                if items:
                    await self.db.record_item_seen(self.name)
                await on_items(items)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.db.record_error(self.name, str(exc))
                log.warning(
                    "collector error",
                    source=self.name,
                    error=str(exc)[:200],
                    backoff=round(self.backoff, 1),
                )
                await self._sleep(self.backoff, stop)
                self.backoff = min(self.backoff * 2, 600)
                continue
            await self._sleep(self.jittered_interval(), stop)

    async def _sleep(self, seconds: float, stop: asyncio.Event) -> None:
        import contextlib

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)

    def jittered_interval(self) -> float:
        return self.interval_seconds * random.uniform(0.9, 1.1)

    # -- interface ---------------------------------------------------------------

    @abstractmethod
    async def poll(self) -> list:
        """Fetch new items and persist the cursor. Returns RawItems."""

    # -- cursor helpers ------------------------------------------------------------

    async def load_cursor(self) -> str | None:
        return await self.db.get_cursor(self.name)

    async def save_cursor(self, cursor: str | None) -> None:
        await self.db.save_cursor(self.name, cursor)


async def fetch_robots(client: httpx.AsyncClient, page_url: str) -> RobotsPolicy:
    """Check robots.txt for the page's origin. Programmatic check per M3 task 4."""
    from urllib.parse import urlsplit

    parts = urlsplit(page_url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        resp = await client.get(robots_url)
    except httpx.HTTPError as exc:
        return RobotsPolicy(allowed=True, note=f"robots fetch failed ({exc.__class__.__name__}); allowing")
    if resp.status_code != 200:
        return RobotsPolicy(allowed=True, note=f"robots status {resp.status_code}")
    text = resp.text
    agent = None
    disallows: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line.startswith("user-agent:"):
            current = line.split(":", 1)[1].strip()
            agent = "*" if current == "*" else current
        elif line.startswith("disallow:") and agent == "*":
            path = line.split(":", 1)[1].strip()
            if path:
                disallows.append(path)
    from urllib.parse import urlsplit as _split

    path = _split(page_url).path or "/"
    for rule in disallows:
        if rule != "" and path.startswith(rule):
            return RobotsPolicy(allowed=False, note=f"disallowed by robots.txt rule {rule!r}")
    return RobotsPolicy(allowed=True)
