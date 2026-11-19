"""Filter rule tests: shorteners, funnel heuristics, staleness, mutes."""

from __future__ import annotations

from datetime import UTC, datetime

from hunter.config import PipelineCfg
from hunter.db import MuteState
from hunter.filters import (
    filter_candidate,
    has_shortener,
    is_funnel_post,
    passes_host_filter,
)
from hunter.models import Candidate

PREFIX = "https://claude.ai/referral/"
CFG = PipelineCfg()


def _cand(text: str = "free pass", urls: list[str] | None = None, minutes_ago: float = 2) -> Candidate:
    now = datetime.now(UTC)
    from hunter.models import RawItem

    item = RawItem(
        source="lemmy", item_id="1", author="a", text=text,
        urls=urls or [], permalink="https://x/1",
        posted_at=now.replace(minute=now.minute), fetched_at=now,
    )
    item = item.model_copy(
        update={"posted_at": now.timestamp() - 0 and None}
    ) if False else item
    return Candidate(code="ABC123XYZ9", url="https://claude.ai/referral/ABC123XYZ9", item=item)


def _item(text: str, urls: list[str], minutes_ago: float = 2) -> object:
    from datetime import timedelta

    from hunter.models import RawItem

    now = datetime.now(UTC)
    return RawItem(
        source="lemmy", item_id="1", author="a", text=text, urls=urls,
        permalink="https://x/1", posted_at=now - timedelta(minutes=minutes_ago),
        fetched_at=now,
    )


def test_host_filter_rejects_lookalike():
    assert not passes_host_filter("https://claude.ai.evil.com/referral/X", PREFIX)
    assert passes_host_filter("https://claude.ai/referral/X", PREFIX)


def test_shortener_detected():
    assert has_shortener(["https://bit.ly/3xY"])
    assert not has_shortener(["https://claude.ai/referral/ABC123XYZ9"])


def test_funnel_payment_and_dm():
    assert is_funnel_post("Pay $5 and DM me for the pass")
    assert not is_funnel_post("Here is my extra pass, first come first served")


def test_staleness_rejects_old():
    cand = Candidate(
        code="ABC123XYZ9", url="https://claude.ai/referral/ABC123XYZ9",
        item=_item("pass", [], minutes_ago=45),  # type: ignore[arg-type]
    )
    assert filter_candidate(
        cand, canonical_prefix=PREFIX, pipeline_cfg=CFG, mute_state=None
    ) == "stale"


def test_mute_rejects_muted_source_or_author():
    cand = Candidate(
        code="ABC123XYZ9", url="https://claude.ai/referral/ABC123XYZ9",
        item=_item("pass", []),  # type: ignore[arg-type]
    )
    state = MuteState(source_muted=False, author_muted=True)
    assert filter_candidate(
        cand, canonical_prefix=PREFIX, pipeline_cfg=CFG, mute_state=state
    ) == "muted"
    state2 = MuteState(source_muted=True)
    assert filter_candidate(
        cand, canonical_prefix=PREFIX, pipeline_cfg=CFG, mute_state=state2
    ) == "muted"


def test_unknown_age_passes_filter():
    from hunter.models import RawItem

    now = datetime.now(UTC)
    item = RawItem(
        source="oneparrainage", item_id="1", author="a", text="pass",
        urls=[], posted_at=None, fetched_at=now,
    )
    cand = Candidate(code="ABC123XYZ9", url="https://claude.ai/referral/ABC123XYZ9", item=item)
    assert filter_candidate(
        cand, canonical_prefix=PREFIX, pipeline_cfg=CFG, mute_state=None
    ) is None
