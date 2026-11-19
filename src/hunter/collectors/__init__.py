"""Collectors package."""

from .base import Collector, RobotsPolicy, fetch_robots
from .bluesky import BlueskyCollector
from .claudecoworkcourse import ClaudeCoworkCourseCollector
from .hackernews import HackerNewsCollector
from .lemmy import LemmyCollector
from .oneparrainage import OneParrainageCollector
from .telegram_preview import TelegramPreviewCollector

__all__ = [
    "BlueskyCollector",
    "ClaudeCoworkCourseCollector",
    "Collector",
    "HackerNewsCollector",
    "LemmyCollector",
    "OneParrainageCollector",
    "RobotsPolicy",
    "TelegramPreviewCollector",
    "fetch_robots",
]
