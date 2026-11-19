"""Core data models: RawItem, Candidate, Alert."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class RawItem(BaseModel):
    """One post/comment/message from any source, before processing."""

    source: str                      # collector name, e.g. "lemmy"
    item_id: str                     # source-unique id for cursoring/dedup
    author: str
    text: str = ""
    urls: list[str] = Field(default_factory=list)
    permalink: str | None = None
    posted_at: datetime | None = None
    fetched_at: datetime

    @property
    def age_seconds(self) -> float | None:
        if not self.posted_at:
            return None
        return max(0.0, (self.fetched_at - self.posted_at).total_seconds())


class Candidate(BaseModel):
    """A referral link extracted from a RawItem."""

    code: str
    url: str
    item: RawItem

    @property
    def age_seconds(self) -> float | None:
        return self.item.age_seconds


class Alert(BaseModel):
    """The single possible output of the pipeline for one candidate."""

    code: str
    url: str
    item: RawItem
    n_sources: int = 1
    score: float = 0.0
    silent: bool = False
    confidence: str = "orange"       # green | yellow | orange
    age_seconds: float | None = None
    sources: list[str] = Field(default_factory=list)
    row_id: int | None = None        # alerts table row, set by the pipeline
