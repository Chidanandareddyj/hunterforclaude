"""Configuration loading.

`config.yaml` holds non-secret settings; `.env` holds secrets. Loading fails
fast with a clear error when something is missing or malformed (M0 acceptance).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError


class ConfigError(Exception):
    """Raised when configuration is missing or invalid. Message is user-facing."""


class FreshnessDecay(BaseModel):
    half_life_minutes: float = 20.0


class Corroboration(BaseModel):
    extra_sources_weight: float = 0.15
    max_boost: float = 1.5


class ConfidenceTags(BaseModel):
    green_under: float = 5.0
    yellow_under: float = 15.0


class Scoring(BaseModel):
    normal_threshold: float = 0.55
    freshness_decay: FreshnessDecay = Field(default_factory=FreshnessDecay)
    corroboration: Corroboration = Field(default_factory=Corroboration)
    confidence_tags: ConfidenceTags = Field(default_factory=ConfidenceTags)


class AutoMute(BaseModel):
    author_consecutive_dead: int = 3
    source_min_votes: int = 5
    source_dead_rate: float = 0.7


class PipelineCfg(BaseModel):
    staleness_cutoff_minutes: float = 30.0
    realert_after_hours: float = 24.0


class LemmySource(BaseModel):
    enabled: bool = False
    interval_seconds: float = 30.0
    base_url: str = "https://lemmy.world"
    community: str = ""
    search_term: str = "claude.ai/referral"
    trust: float = 0.5


class BlueskySource(BaseModel):
    enabled: bool = False
    jetstream_url: str = "wss://jetstream2-firehose.fly.dev"
    wanted_collection: str = "app.bsky.feed.post"
    trust: float = 0.5


class HackerNewsSource(BaseModel):
    enabled: bool = False
    interval_seconds: float = 60.0
    algolia_url: str = "https://hn.algolia.com/api/v1/search_by_date"
    trust: float = 0.5


class ClaudeCoworkCourseSource(BaseModel):
    enabled: bool = False
    interval_seconds: float = 300.0
    url: str = "https://claudecoworkcourse.com/claude-guest-passes"
    trust: float = 0.3


class OneParrainageSource(BaseModel):
    enabled: bool = False
    interval_seconds: float = 300.0
    url: str = "https://1parrainage.com/claude"
    trust: float = 0.3


class TelegramPreviewSource(BaseModel):
    enabled: bool = False
    interval_seconds: float = 15.0
    channels: list[str] = Field(default_factory=list)
    trust: float = 0.5


class Sources(BaseModel):
    lemmy: LemmySource = Field(default_factory=LemmySource)
    bluesky: BlueskySource = Field(default_factory=BlueskySource)
    hackernews: HackerNewsSource = Field(default_factory=HackerNewsSource)
    claudecoworkcourse: ClaudeCoworkCourseSource = Field(default_factory=ClaudeCoworkCourseSource)
    oneparrainage: OneParrainageSource = Field(default_factory=OneParrainageSource)
    telegram_preview: TelegramPreviewSource = Field(default_factory=TelegramPreviewSource)


class NotifyTelegram(BaseModel):
    parse_mode: str = "HTML"


class Notify(BaseModel):
    telegram: NotifyTelegram = Field(default_factory=NotifyTelegram)


class Heartbeat(BaseModel):
    time: str = ""  # HH:MM local, empty disables


class Health(BaseModel):
    error_after_minutes: float = 10.0
    silent_after_hours: float = 24.0


class Config(BaseModel):
    url_pattern: str
    canonical_link_prefix: str = "https://claude.ai/referral/"
    pipeline: PipelineCfg = Field(default_factory=PipelineCfg)
    scoring: Scoring = Field(default_factory=Scoring)
    auto_mute: AutoMute = Field(default_factory=AutoMute)
    sources: Sources = Field(default_factory=Sources)
    notify: Notify = Field(default_factory=Notify)
    heartbeat: Heartbeat = Field(default_factory=Heartbeat)
    health: Health = Field(default_factory=Health)

    # Runtime paths, not user-facing config.
    db_path: Path = Field(default=Path("data/hunter.db"), exclude=True)


DEFAULT_CONFIG_PATH = Path("config.yaml")


def load_env_file(env_path: Path | None = None) -> None:
    """Load KEY=VALUE pairs from .env into os.environ (non-destructively).

    Only fills variables that are not already set in the process environment,
    so explicit exports always win. No external dependency needed.
    """
    path = env_path or Path(".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(config_path: Path | None = None, *, require_token: bool = False) -> Config:
    """Load and validate configuration. Raises ConfigError with a clear message."""
    load_env_file()
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}. "
            "Copy config.example.yaml to config.yaml and adjust it."
        )
    try:
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Config file {path} is not valid YAML: {exc}") from exc

    # DB path stays out of YAML: fixed relative to the working directory.
    raw.pop("db_path", None)

    try:
        cfg = Config(**raw)
        cfg.db_path = Path(os.environ.get("HUNTER_DB_PATH", "data/hunter.db"))
    except ValidationError as exc:
        raise ConfigError(f"Config file {path} is invalid:\n{exc}") from exc

    if require_token and not os.environ.get("HUNTER_TELEGRAM_BOT_TOKEN"):
        raise ConfigError(
            "HUNTER_TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and "
            "paste the token from @BotFather."
        )
    return cfg
