"""Feedback analysis: structured report combining tool health, errors, and capability gaps.

Examples::

    $ uv run lup-devtools feedback analyze
    $ uv run lup-devtools feedback analyze --version 0.3.0
    $ uv run lup-devtools feedback analyze --output report.json
"""

from collections import defaultdict
from pathlib import Path
from collections.abc import Sequence
from typing import TypedDict

from lup.devtools.feedback.models import LoadedSession
from lup.devtools.feedback.state import load_sessions_for_versions
from lup.devtools.trace.traces import (
    CapabilityRequest,
    scan_for_capability_gaps,
)
from lup.telemetry.metrics import MetricsSummary
from lup.workspace.history import resolve_version


class ToolBucket(TypedDict):
    """One tool's call and error tallies."""

    calls: int
    errors: int


# Open per-tool aggregation buckets, keyed by whatever tools ran.
type ToolBuckets = dict[str, ToolBucket]


class ToolHealth(TypedDict):
    name: str
    calls: int
    errors: int
    error_rate: float


class ErrorPattern(TypedDict):
    session_id: str
    error_count: int
    total_calls: int
    error_rate: float
    top_errors: list[str]


class AnalysisReport(TypedDict):
    version: str | None
    tool_health: list[ToolHealth]
    error_patterns: list[ErrorPattern]
    capability_gaps: list[CapabilityRequest]


def gather_tool_health(sessions: Sequence[LoadedSession]) -> list[ToolHealth]:
    """Compute per-tool call counts, error counts, and error rates."""
    # Open per-tool aggregation buckets, keyed by whatever tools ran.
    tool_stats: ToolBuckets = defaultdict(lambda: {"calls": 0, "errors": 0})

    for s in sessions:
        if s.tool_metrics is None:
            continue
        for tool_name, data in s.tool_metrics["by_tool"].items():
            tool_stats[tool_name]["calls"] += data["call_count"]
            tool_stats[tool_name]["errors"] += data["error_count"]

    def health_row(name: str) -> ToolHealth:
        stats = tool_stats[name]
        calls = stats["calls"]
        errors = stats["errors"]
        return {
            "name": name,
            "calls": calls,
            "errors": errors,
            "error_rate": (errors / calls) if calls > 0 else 0.0,
        }

    return [
        health_row(name)
        for name in sorted(tool_stats, key=lambda t: -tool_stats[t]["calls"])
    ]


def gather_error_patterns(sessions: Sequence[LoadedSession]) -> list[ErrorPattern]:
    """Find sessions with high error rates, grouped by error type."""

    def error_pattern(session_id: str, metrics: MetricsSummary) -> ErrorPattern:
        total_errors = metrics["total_errors"]
        total_calls = metrics["total_tool_calls"]
        tool_errors = sorted(
            (
                (errs, tool_name)
                for tool_name, tool_data in metrics["by_tool"].items()
                if (errs := tool_data["error_count"]) > 0
            ),
            reverse=True,
        )
        return {
            "session_id": session_id,
            "error_count": total_errors,
            "total_calls": total_calls,
            "error_rate": (total_errors / total_calls) if total_calls > 0 else 0.0,
            "top_errors": [f"{name}: {count}" for count, name in tool_errors],
        }

    return sorted(
        (
            error_pattern(s.source_session_id, metrics)
            for s in sessions
            if (metrics := s.tool_metrics) is not None and metrics["total_errors"] > 0
        ),
        key=lambda p: -p["error_count"],
    )


def build_report(version: str | None, all_versions: bool) -> AnalysisReport:
    """Build a complete analysis report."""
    effective = resolve_version(version, all_versions).versions
    sessions = load_sessions_for_versions(effective)

    return {
        "version": version,
        "tool_health": gather_tool_health(sessions),
        "error_patterns": gather_error_patterns(sessions),
        "capability_gaps": scan_for_capability_gaps(effective),
    }


def analyze(version: str | None, all_versions: bool, output: Path | None) -> None:
    """Produce a JSON analysis report to stdout or a file."""
    import json

    import typer

    from lup.devtools.utils import output_json

    report = build_report(version, all_versions)

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
        typer.echo(f"Report written to {output}")
    else:
        output_json(report)
