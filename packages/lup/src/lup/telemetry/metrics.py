"""Tool call metrics tracking.

MCP tools defined with :func:`lup.mcp.lup_tool` are tracked automatically —
every call's duration and error status is recorded into the module-level
collector, no decorator needed. Metrics are saved with each session for
feedback loop analysis.

Use the :func:`tracked` decorator for non-tool async functions (background
jobs, API helpers, sub-agent invocations) that should feed the same
metrics stream.

Examples:
    Record a non-tool helper alongside tool metrics::

        >>> @tracked("fetch_market_data")
        ... async def fetch_market_data(symbol: str) -> dict[str, float]:
        ...     return {"price": 101.5}

    Retrieve aggregated metrics at session end::

        >>> summary = get_metrics_summary()
        >>> summary["total_tool_calls"]
        15
        >>> summary["by_tool"]["search"]["avg_duration_ms"]
        42.5

    Reset metrics between sessions::

        >>> reset_metrics()
"""

import json
import logging
import time
from collections import defaultdict
from collections.abc import Callable, Coroutine
from functools import wraps
from pathlib import Path
from typing import TypedDict

from pydantic import BaseModel, TypeAdapter, ValidationError

from lup.channels.models import write_atomic

logger = logging.getLogger(__name__)

# lup: ignore[constant-declaration] — the metrics file's own name inside a
# session directory, which the writer and every reader must spell alike
METRICS_FILENAME = "metrics.json"


def metrics_path(session_dir: Path) -> Path:
    """Canonical location of flushed metrics for a session."""
    return session_dir / METRICS_FILENAME


class ToolMetricsDict(TypedDict):
    """Serialized metrics for a single tool."""

    call_count: int
    error_count: int
    error_rate: str
    total_duration_ms: float
    avg_duration_ms: float
    min_duration_ms: float
    max_duration_ms: float


class MetricsSummary(TypedDict):
    """Serialized summary of all tool metrics."""

    session_duration_seconds: float
    total_tool_calls: int
    total_errors: int
    overall_error_rate: str
    total_tool_time_ms: float
    tools_used: int
    by_tool: dict[str, ToolMetricsDict]


METRICS_SUMMARY_ADAPTER = TypeAdapter(MetricsSummary)
"""Validating reader for flushed metrics JSON."""


class ToolMetrics(BaseModel):
    """Metrics for a single tool."""

    call_count: int = 0
    error_count: int = 0
    total_duration_ms: float = 0.0
    min_duration_ms: float = float("inf")
    max_duration_ms: float = 0.0

    @property
    def avg_duration_ms(self) -> float:
        """Average duration per call in milliseconds."""
        if self.call_count == 0:
            return 0.0
        return self.total_duration_ms / self.call_count

    @property
    def error_rate(self) -> float:
        """Percentage of calls that resulted in errors."""
        if self.call_count == 0:
            return 0.0
        return self.error_count / self.call_count

    def record_call(self, duration_ms: float, is_error: bool = False) -> None:
        """Record a tool call."""
        self.call_count += 1
        self.total_duration_ms += duration_ms
        self.min_duration_ms = min(self.min_duration_ms, duration_ms)
        self.max_duration_ms = max(self.max_duration_ms, duration_ms)
        if is_error:
            self.error_count += 1

    def to_dict(self) -> ToolMetricsDict:
        """Convert to dictionary for serialization."""
        return ToolMetricsDict(
            call_count=self.call_count,
            error_count=self.error_count,
            error_rate=f"{self.error_rate:.1%}",
            total_duration_ms=round(self.total_duration_ms, 2),
            avg_duration_ms=round(self.avg_duration_ms, 2),
            min_duration_ms=(
                round(self.min_duration_ms, 2)
                if self.min_duration_ms != float("inf")
                else 0
            ),
            max_duration_ms=round(self.max_duration_ms, 2),
        )


class MetricsCollector:
    """Collects metrics for all tools.

    When ``flush_path`` is set (tool-serving subprocesses), the summary
    is written through to disk after every recorded call so the parent
    process can read it even if the subprocess is killed.
    """

    def __init__(self) -> None:
        self.metrics: dict[str, ToolMetrics] = defaultdict(ToolMetrics)
        self.session_start: float = time.time()
        self.flush_path: Path | None = None

    def record(
        self, tool_name: str, duration_ms: float, is_error: bool = False
    ) -> None:
        """Record a tool call."""
        self.metrics[tool_name].record_call(duration_ms, is_error)
        if self.flush_path is not None:
            self.flush()

    def flush(self) -> None:
        """Write the current summary to ``flush_path`` atomically.

        A kill mid-write must not corrupt the snapshot the parent reads,
        so the summary lands on a temp file in the same directory and is
        renamed onto the target (``Path.replace`` is atomic on POSIX).
        """
        if self.flush_path is None:
            return
        try:
            write_atomic(
                self.flush_path,
                json.dumps(self.get_summary(), indent=2).encode("utf-8"),
            )
        except OSError:
            logger.exception("Failed to flush metrics to %s", self.flush_path)

    def get_summary(self) -> MetricsSummary:
        """Get a summary of all metrics."""
        total_calls = sum(m.call_count for m in self.metrics.values())
        total_errors = sum(m.error_count for m in self.metrics.values())
        total_duration = sum(m.total_duration_ms for m in self.metrics.values())
        session_duration = time.time() - self.session_start

        return MetricsSummary(
            session_duration_seconds=round(session_duration, 2),
            total_tool_calls=total_calls,
            total_errors=total_errors,
            overall_error_rate=f"{total_errors / max(1, total_calls):.1%}",
            total_tool_time_ms=round(total_duration, 2),
            tools_used=len(self.metrics),
            by_tool={name: m.to_dict() for name, m in self.metrics.items()},
        )

    def log_summary(self, level: int = logging.INFO) -> None:
        """Log a summary of all metrics."""
        summary = self.get_summary()
        logger.log(
            level,
            "Tool Metrics: %d calls, %d errors, %.1fs session",
            summary["total_tool_calls"],
            summary["total_errors"],
            summary["session_duration_seconds"],
        )

    def reset(self) -> None:
        """Reset all metrics."""
        self.metrics.clear()
        self.session_start = time.time()


# Global metrics collector
collector = MetricsCollector()


def tracked[**P, T](
    tool_name: str | None = None,
) -> Callable[
    [Callable[P, Coroutine[object, object, T]]],
    Callable[P, Coroutine[object, object, T]],
]:
    """Decorator recording call metrics for non-tool async functions.

    **What:** Records each call's duration and error status (raised
    exceptions, or a returned dict carrying ``is_error``) into the same
    collector that :func:`lup.mcp.lup_tool` feeds automatically for MCP
    tools.

    **When:** Apply to async helpers that are not MCP tools — background
    jobs, API wrappers, sub-agent invocations — so their health shows up
    in :func:`get_metrics_summary` next to the tool metrics. Tools defined
    via ``lup_tool`` are already tracked; do not double-decorate them.

    **Why:** Session analysis reads a single metrics stream; work that
    happens outside tool handlers would otherwise be invisible to the
    feedback loop.

    Args:
        tool_name: Name to record metrics under. If None, uses function name.

    Example:
        @tracked("refresh_cache")
        async def refresh_cache(bucket: str) -> dict[str, int]:
            ...
    """

    def decorator(
        func: Callable[P, Coroutine[object, object, T]],
    ) -> Callable[P, Coroutine[object, object, T]]:
        name = tool_name or func.__name__

        @wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            start = time.perf_counter()
            is_error = False

            try:
                result = await func(*args, **kwargs)
                match result:
                    case {"is_error": flag} if flag:
                        is_error = True
                return result
            except BaseException:  # lup: ignore[except-baseexception] — mark + reraise
                is_error = True
                raise
            finally:
                duration_ms = (time.perf_counter() - start) * 1000
                collector.record(name, duration_ms, is_error)

        return wrapper

    return decorator


def configure_metrics(flush_path: Path | None) -> None:
    """Route metrics to a write-through flush file.

    Call in tool-serving subprocesses (with ``metrics_path(session_dir)``)
    so the parent process can read tool metrics after the run.
    """
    collector.flush_path = flush_path


def read_metrics_summary(session_dir: Path) -> MetricsSummary | None:
    """Read a flushed metrics summary, or None if absent or unreadable."""
    path = metrics_path(session_dir)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return METRICS_SUMMARY_ADAPTER.validate_python(raw)
    except (json.JSONDecodeError, OSError, ValidationError):
        logger.exception("Flushed metrics at %s are unreadable", path)
        return None


def log_metrics_summary() -> None:
    """Log a summary of all tool metrics."""
    collector.log_summary()


def get_metrics_summary() -> MetricsSummary:
    """Get a summary of all tool metrics."""
    return collector.get_summary()


def reset_metrics() -> None:
    """Reset all tool metrics."""
    collector.reset()
