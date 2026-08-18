"""What a first pass reads, and what every pass after it reads.

A lookback window is the right rule for a profile that has synced before: it
asks for what changed, and the cursor says where changed starts. It is the
wrong rule for one that never has. An account quieter than the window answers
it with nothing, and a first pass that publishes nothing cannot be told apart
from an expired session or a misresolved organisation — which is the confusion
these pin shut.

The fake answers with a payload rather than with conversations, so the real
filter runs. A fake that did the filtering itself would pass whatever the
window rule became.
"""

from datetime import datetime, timedelta, timezone

import pytest

from lup.types import JsonValue

from forumboard.agent.config import settings
from forumboard.claudeai.client import (
    ClaudeCredentials,
    ClaudeWebClient,
    ConversationMeta,
    PathBuilder,
    StaticCredentials,
)
from forumboard.pipeline.sync import to_read

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
FLOOR = NOW - timedelta(days=7)


def aged(days: int) -> str:
    """A timestamp that many days before the floor, as the listing spells it."""
    return (FLOOR - timedelta(days=days)).isoformat()


def fresh(hours: int) -> str:
    """A timestamp that many hours before now, well inside the window."""
    return (NOW - timedelta(hours=hours)).isoformat()


class Listing(ClaudeWebClient):
    """A client answering every request with one fixed conversation listing."""

    def __init__(self, *stamps: str) -> None:
        super().__init__(StaticCredentials(ClaudeCredentials(cookie="sessionKey=x")))
        self.payload: JsonValue = [
            ConversationMeta(uuid=f"c{index}", updated_at=stamp).model_dump()
            for index, stamp in enumerate(stamps)
        ]

    async def fetch(self, request: PathBuilder) -> JsonValue:
        return self.payload


@pytest.fixture(autouse=True)
def seed_of_three(monkeypatch: pytest.MonkeyPatch) -> None:
    """A seed small enough to count, and a listing cap above it."""
    monkeypatch.setattr(settings, "sync_seed_conversations", 3)
    monkeypatch.setattr(settings, "sync_max_conversations", 20)


async def test_a_later_pass_reads_only_the_window() -> None:
    """A cursor means the window is the whole rule, however little it holds."""
    client = Listing(aged(60), aged(50), aged(40))
    assert await to_read(client, FLOOR, seeding=False) == []


async def test_a_first_pass_seeds_from_an_account_quieter_than_the_window() -> None:
    """The case that reported nothing: everything real, all of it older."""
    client = Listing(aged(60), aged(1), aged(30))
    seeded = await to_read(client, FLOOR, seeding=True)
    assert [meta.uuid for meta in seeded] == ["c1", "c2", "c0"]


async def test_a_first_pass_seeds_the_newest_rather_than_the_listing_order() -> None:
    """The head of the listing is taken by age, not by the order it arrived."""
    client = Listing(aged(90), aged(80), aged(2), aged(70), aged(1))
    seeded = await to_read(client, FLOOR, seeding=True)
    assert [meta.uuid for meta in seeded] == ["c4", "c2", "c3"]


async def test_a_first_pass_with_a_full_window_does_not_reach_past_it() -> None:
    """A window already holding the seed count is the answer on its own."""
    client = Listing(fresh(1), fresh(2), fresh(3), aged(40))
    seeded = await to_read(client, FLOOR, seeding=True)
    assert [meta.uuid for meta in seeded] == ["c0", "c1", "c2"]


async def test_seeding_never_reads_less_than_the_window_would() -> None:
    """A partial window is topped up, not replaced by the newest few."""
    client = Listing(fresh(1), aged(10), aged(20), aged(30))
    seeded = await to_read(client, FLOOR, seeding=True)
    assert {meta.uuid for meta in seeded} >= {"c0", "c1"}
    assert len(seeded) == 3


async def test_a_zero_seed_leaves_the_window_as_the_only_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opting out restores the behaviour a lookback alone gives."""
    monkeypatch.setattr(settings, "sync_seed_conversations", 0)
    client = Listing(aged(60), aged(50))
    assert await to_read(client, FLOOR, seeding=True) == []


async def test_an_undated_conversation_seeds_last_rather_than_being_dropped() -> None:
    """A listing entry with no timestamp is still a conversation."""
    client = Listing("", aged(40), aged(30))
    seeded = await to_read(client, FLOOR, seeding=True)
    assert [meta.uuid for meta in seeded] == ["c0", "c2", "c1"]
