"""Collectors package."""

from .base import Collector, RobotsPolicy, fetch_robots
from .bluesky import BlueskyCollector
from .bluesky_search import BlueskySearchCollector, BlueskySearchSource
from .claudecoworkcourse import ClaudeCoworkCourseCollector
from .github import GitHubCollector, GitHubSource
from .hackernews import HackerNewsCollector
from .lemmy import LemmyCollector
from .mastodon import MastodonCollector, MastodonSource
from .oneparrainage import OneParrainageCollector
from .reddit import RedditCollector, RedditSource
from .telegram_preview import TelegramPreviewCollector
from .web_search import WebSearchCollector, WebSearchSource

__all__ = [
    "BlueskyCollector",
    "BlueskySearchCollector",
    "BlueskySearchSource",
    "ClaudeCoworkCourseCollector",
    "Collector",
    "GitHubCollector",
    "GitHubSource",
    "HackerNewsCollector",
    "LemmyCollector",
    "MastodonCollector",
    "MastodonSource",
    "OneParrainageCollector",
    "RedditCollector",
    "RedditSource",
    "RobotsPolicy",
    "TelegramPreviewCollector",
    "WebSearchCollector",
    "WebSearchSource",
    "fetch_robots",
]
