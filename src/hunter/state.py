"""Shared runtime state for the running monitor process."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .db import Database

if TYPE_CHECKING:
    from asyncio import Event

    from .config import Config
    from .feedback import FeedbackHandler
    from .notify.send_queue import SendQueue


@dataclass
class AppState:
    """Everything handlers and collectors may need at runtime."""

    token: str
    db: Database
    cfg: Config | None = None
    queue: SendQueue | None = None
    feedback: FeedbackHandler | None = None
    paused: bool = False
    dry_run: bool = False
    stop_event: Event | None = None
