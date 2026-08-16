"""What the sandbox records as it executes, and what a replay of it says.

The container is stood in for at the three calls that reach it — ``run_code``,
``run_file``, ``run_install`` — so the recording and replay legs run without a
daemon. What is under test is which cell each tool writes, that a replay takes
the same route back, and that a replay never reports a clean sequence from a
record it could not read.
"""

from pathlib import Path

import pytest

from lup.mcp import LupMcpTool, ToolError
from lup.replay.journal import JournalCell
from lup.sandbox.container import Sandbox
from lup.sandbox.models import (
    ExecuteCodeInput,
    ExecuteCodeResult,
    InstallPackageInput,
    InstallPackageResult,
    SandboxReplayInput,
)


class FakeRuns:
    """The container's three execution calls, answered from a failing set."""

    def __init__(self, failing: set[str]) -> None:
        self.failing = failing
        self.code: list[str] = []
        self.files: list[str] = []
        self.installs: list[list[str]] = []

    def outcome(self, source: str) -> int:
        return 1 if source in self.failing else 0

    def run_code(
        self, code: str, timeout_seconds: int | None = None
    ) -> ExecuteCodeResult:
        self.code.append(code)
        return ExecuteCodeResult(
            exit_code=self.outcome(code),
            stderr="ZeroDivisionError: division by zero" if self.outcome(code) else "",
        )

    def run_file(
        self, path: str, timeout_seconds: int | None = None
    ) -> ExecuteCodeResult:
        self.files.append(path)
        return ExecuteCodeResult(exit_code=self.outcome(path))

    def run_install(self, packages: list[str]) -> InstallPackageResult:
        self.installs.append(packages)
        return InstallPackageResult(
            exit_code=self.outcome(packages[0]), output="", packages=packages
        )


def wired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, failing: set[str] | None = None
) -> tuple[Sandbox, FakeRuns, dict[str, LupMcpTool]]:
    sandbox = Sandbox(session_id="journal-1", shared_dir=tmp_path, pre_install=None)
    runs = FakeRuns(failing or set())
    monkeypatch.setattr(sandbox, "ensure_started", lambda: None)
    monkeypatch.setattr(sandbox, "run_code", runs.run_code)
    monkeypatch.setattr(sandbox, "run_file", runs.run_file)
    monkeypatch.setattr(sandbox, "run_install", runs.run_install)
    return sandbox, runs, {tool.name: tool for tool in sandbox.create_tools()}


async def test_every_executed_cell_is_recorded_in_the_order_it_ran(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox, _, tools = wired(tmp_path, monkeypatch)

    await tools["execute_code"](ExecuteCodeInput(code="x = 1"))
    await tools["install_package"](InstallPackageInput(packages=["numpy", "pandas"]))
    await tools["execute_code"](ExecuteCodeInput(file="/shared/run.py"))

    assert [(cell.kind, cell.source) for cell in sandbox.journal.load().cells] == [
        ("code", "x = 1"),
        ("install", '["numpy","pandas"]'),
        ("file", "/shared/run.py"),
    ]


async def test_a_cell_that_failed_is_recorded_as_having_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox, _, tools = wired(tmp_path, monkeypatch, failing={"1 / 0"})

    await tools["execute_code"](ExecuteCodeInput(code="1 / 0"))

    assert [cell.ok for cell in sandbox.journal.load().cells] == [False]


async def test_a_replay_takes_each_cell_back_through_the_call_that_ran_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An install has to arrive back as the package list it named, not as one
    # string that happens to contain spaces.
    _, runs, tools = wired(tmp_path, monkeypatch)
    await tools["execute_code"](ExecuteCodeInput(code="x = 1"))
    await tools["install_package"](InstallPackageInput(packages=["numpy", "pandas"]))
    await tools["execute_code"](ExecuteCodeInput(file="/shared/run.py"))

    await tools["sandbox_replay"](SandboxReplayInput())

    assert runs.code == ["x = 1", "x = 1"]
    assert runs.installs == [["numpy", "pandas"], ["numpy", "pandas"]]
    assert runs.files == ["/shared/run.py", "/shared/run.py"]


async def test_a_replay_that_matches_the_record_reports_no_divergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, tools = wired(tmp_path, monkeypatch)
    await tools["execute_code"](ExecuteCodeInput(code="x = 1"))

    report = await tools["sandbox_replay"](SandboxReplayInput())

    assert report.reproduced
    assert report.cells_replayed == 1
    assert "no divergence" in report.finding


async def test_a_cell_that_goes_the_other_way_on_replay_is_the_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The sandbox claims no determinism, so this reads as what the result
    # depended on rather than as a broken promise.
    sandbox, runs, tools = wired(tmp_path, monkeypatch)
    await tools["execute_code"](ExecuteCodeInput(code="import requests"))
    runs.failing.add("import requests")

    report = await tools["sandbox_replay"](SandboxReplayInput())

    assert not report.reproduced
    assert report.divergences[0].index == 0
    assert "ZeroDivisionError" in report.divergences[0].detail
    assert "which is the finding" in report.finding
    assert not sandbox.journal.load().determinism_claimed


async def test_replaying_does_not_itself_lengthen_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Recording happens at the tool boundary, so a replay re-runs the cells
    # without appending them again — otherwise each replay would double the
    # journal and the next one would take twice as long.
    sandbox, _, tools = wired(tmp_path, monkeypatch)
    await tools["execute_code"](ExecuteCodeInput(code="x = 1"))

    await tools["sandbox_replay"](SandboxReplayInput())
    await tools["sandbox_replay"](SandboxReplayInput())

    assert len(sandbox.journal.load().cells) == 1


async def test_an_unreadable_record_stops_the_tools_rather_than_reading_as_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox, _, tools = wired(tmp_path, monkeypatch)
    sandbox.journal.record(JournalCell(source="x = 1", ok=True))
    sandbox.journal.path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ToolError, match="does not parse"):
        await tools["sandbox_replay"](SandboxReplayInput())
    with pytest.raises(ToolError, match="does not parse"):
        await tools["execute_code"](ExecuteCodeInput(code="x = 2"))
