"""Metrics collection behavior, including the cross-process file mode."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from lup.telemetry.metrics import (
    collector,
    configure_metrics,
    get_metrics_summary,
    metrics_path,
    read_metrics_summary,
    reset_metrics,
)


@pytest.fixture(autouse=True)
def clean_collector() -> Iterator[None]:
    reset_metrics()
    yield
    configure_metrics(None)
    reset_metrics()


class TestFileMode:
    def test_records_flush_through_to_disk(self, tmp_path: Path) -> None:
        configure_metrics(metrics_path(tmp_path))

        collector.record("search", 12.5)
        collector.record("search", 7.5, is_error=True)

        summary = read_metrics_summary(tmp_path)
        assert summary is not None
        assert summary["total_tool_calls"] == 2
        assert summary["total_errors"] == 1
        assert summary["by_tool"]["search"]["call_count"] == 2

    def test_corrupt_flush_file_reads_as_none(self, tmp_path: Path) -> None:
        metrics_path(tmp_path).write_text("{broken", encoding="utf-8")

        assert read_metrics_summary(tmp_path) is None

    def test_absent_file_reads_as_none(self, tmp_path: Path) -> None:
        assert read_metrics_summary(tmp_path) is None


class TestInProcessSummary:
    def test_error_rate_reflects_recorded_calls(self) -> None:
        collector.record("fetch", 10.0)
        collector.record("fetch", 10.0, is_error=True)

        summary = get_metrics_summary()
        assert summary["by_tool"]["fetch"]["error_rate"] == "50.0%"

    def test_reset_clears_state(self) -> None:
        collector.record("fetch", 10.0)
        reset_metrics()

        assert get_metrics_summary()["total_tool_calls"] == 0
