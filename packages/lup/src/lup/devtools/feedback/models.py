"""Feedback data shapes: session JSON, domain results, and report rows.

This is a TEMPLATE script. Run ``/lup:init`` to customize it for your domain.
The loaders live in ``state``, aggregation in ``metrics``, presentation in
``reports``.
"""

from pathlib import Path
from typing import TypedDict

from pydantic import BaseModel

from lup.telemetry.metrics import MetricsSummary, ToolMetricsDict
from lup.types import JsonValue
from lup.workspace.history import SessionRecord


class AgentPrompt(BaseModel, frozen=True):
    """One application's assembled system prompt, as a health report reads it.

    The report weighs what a session actually receives, so the rendered text
    arrives already assembled rather than as a source file this side would
    have to know how to render.
    """

    sections: list[str]
    rendered: str
    source: Path | None = None
    """Where the sections are authored, when the application can point at it."""


# =============================================================================
# SESSION JSON TYPES
# =============================================================================


class LoadedSession(SessionRecord):
    """A session record plus load provenance for display.

    ``source_session_id`` is the session directory name and ``source_file``
    the JSON file it was read from — injected by the loaders, distinct from
    any ``session_id`` the payload itself carries.
    """

    source_session_id: str = ""
    source_file: str = ""


# =============================================================================
# lup: template: replace these models' fields with what your domain scores on
# =============================================================================


class SessionResult(BaseModel):
    """A session matched with its outcome/feedback.

    Replace ``outcome``/``metrics`` with the fields your domain scores on.
    This is the per-domain shape the whole feedback loop aggregates over
    (``/lup:init`` customization step 9); the generic fields below only
    carry sessions through unscored until you do.

    Customize this for your domain. Examples:

    For forecasting:
        question_id: int
        probability: float
        resolution: bool | None
        brier_score: float | None

    For coaching:
        conversation_id: str
        user_rating: int | None
        session_duration: float
        goals_addressed: list[str]

    For game playing:
        game_id: str
        outcome: str  # "win", "loss", "draw"
        moves_played: int
        opponent_strength: float
    """

    session_id: str
    timestamp: str
    agent_sdk: str | None = None
    outcome: JsonValue = None
    metrics: MetricsSummary | None = None


class FeedbackMetrics(BaseModel):
    """Aggregated metrics from sessions.

    Customize this for your domain.
    """

    collection_timestamp: str
    since_timestamp: str | None = None
    total_sessions: int
    sessions_with_outcomes: int
    # Open per-backend tally, keyed by whatever sdk ids appear.
    sessions_by_sdk: dict[str, int] = {}  # lup: ignore[dict-str-payload]
    results: list[SessionResult] = []


# =============================================================================
# JSON OUTPUT TYPES
# =============================================================================


class ToolUsageEntry(TypedDict):
    name: str
    calls: int
    errors: int
    error_rate: float
    avg_ms: float


class ErrorSessionEntry(TypedDict):
    session_id: str
    errors: int
    by_tool: dict[str, ToolMetricsDict]


class TrendEntry(TypedDict):
    date: str
    avg_calls: float
    error_rate: float
    avg_cost: float


class PromptSection(TypedDict):
    name: str
    lines: int
    characters: int


class PromptHealthReport(TypedDict):
    file: str
    rendered_characters: int
    estimated_tokens: int
    sections: list[PromptSection]


class BackendCostRow(TypedDict):
    """Per-backend rollup row for the costs command."""

    sessions: int
    cost_usd: float
    without_cost: int
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int


def empty_cost_row() -> BackendCostRow:
    return BackendCostRow(
        sessions=0,
        cost_usd=0.0,
        without_cost=0,
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=0,
    )
