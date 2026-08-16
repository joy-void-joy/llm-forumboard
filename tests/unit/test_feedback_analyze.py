"""Behavior tests for feedback analysis aggregation.

`gather_tool_health` must sum per-tool calls/errors across sessions and
derive rates without dividing by zero; `gather_error_patterns` must
surface only failing sessions, ranked by error count.
"""

import pytest

from lup.telemetry.metrics import MetricsSummary, ToolMetricsDict
from lup.devtools.feedback.analyze import (
    gather_error_patterns,
    gather_tool_health,
)
from lup.devtools.feedback.models import LoadedSession


def tool_metrics(call_count: int, error_count: int) -> ToolMetricsDict:
    return {
        "call_count": call_count,
        "error_count": error_count,
        "error_rate": "0%",
        "total_duration_ms": 0.0,
        "avg_duration_ms": 0.0,
        "min_duration_ms": 0.0,
        "max_duration_ms": 0.0,
    }


def session(session_id: str, by_tool: dict[str, ToolMetricsDict]) -> LoadedSession:
    summary: MetricsSummary = {
        "session_duration_seconds": 1.0,
        "total_tool_calls": sum(t["call_count"] for t in by_tool.values()),
        "total_errors": sum(t["error_count"] for t in by_tool.values()),
        "overall_error_rate": "0%",
        "total_tool_time_ms": 0.0,
        "tools_used": len(by_tool),
        "by_tool": by_tool,
    }
    return LoadedSession(source_session_id=session_id, tool_metrics=summary)


def test_tool_health_aggregates_across_sessions() -> None:
    health = gather_tool_health(
        [
            session("s1", {"Bash": tool_metrics(4, 1), "Read": tool_metrics(2, 0)}),
            session("s2", {"Bash": tool_metrics(6, 2)}),
        ]
    )

    assert [t["name"] for t in health] == ["Bash", "Read"]  # busiest first
    bash, read = health
    assert (bash["calls"], bash["errors"]) == (10, 3)
    assert bash["error_rate"] == pytest.approx(0.3)
    assert (read["calls"], read["errors"], read["error_rate"]) == (2, 0, 0.0)


def test_tool_health_zero_calls_has_zero_rate() -> None:
    health = gather_tool_health([session("s1", {"Ghost": tool_metrics(0, 0)})])
    assert health == [{"name": "Ghost", "calls": 0, "errors": 0, "error_rate": 0.0}]


def test_error_patterns_report_only_failing_sessions_ranked() -> None:
    patterns = gather_error_patterns(
        [
            session("clean", {"Read": tool_metrics(5, 0)}),
            session("worst", {"Bash": tool_metrics(4, 3), "Read": tool_metrics(2, 1)}),
            session("mild", {"Bash": tool_metrics(3, 1)}),
        ]
    )

    assert [p["session_id"] for p in patterns] == ["worst", "mild"]
    worst = patterns[0]
    assert worst["error_count"] == 4
    assert worst["total_calls"] == 6
    assert worst["top_errors"] == ["Bash: 3", "Read: 1"]  # worst tool first


def test_error_patterns_empty_without_sessions() -> None:
    assert gather_error_patterns([]) == []
    assert gather_tool_health([]) == []
