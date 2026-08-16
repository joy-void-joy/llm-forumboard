"""Behavior tests for the notes/trace path layout.

Pins the seam that broke: setup_notes() must put the trace log at
notes/traces/<version>/logs/<session_id>/<timestamp>.md (the location
the trace/feedback devtools scan), with a parseable timestamp, and the
session/output dirs under the same version root.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from lup.workspace import paths
from lup.workspace.notes import setup_notes
from lup.workspace.paths import parse_timestamp


@pytest.fixture
def tmp_project(tmp_path: Path) -> Iterator[Path]:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.lup]\nagent_version = "1.2.3"\n', encoding="utf-8"
    )
    old_root = paths.project_root()
    paths.configure(root=tmp_path)
    yield tmp_path
    paths.configure(root=old_root)


def test_trace_log_lands_in_versioned_logs_dir(tmp_project: Path) -> None:
    notes = setup_notes("session-abc", "task-1")

    logs_root = tmp_project / "notes" / "traces" / "1.2.3" / "logs"
    assert notes.trace_log.parent == logs_root / "session-abc"
    parse_timestamp(notes.trace_log.name)


def test_session_and_output_dirs_share_the_version_root(tmp_project: Path) -> None:
    notes = setup_notes("session-abc", "task-1")

    version_root = tmp_project / "notes" / "traces" / "1.2.3"
    assert notes.session == version_root / "sessions" / "session-abc"
    assert notes.session.is_dir()
    assert notes.output.parent == version_root / "outputs" / "task-1"
    assert notes.output.is_dir()


@pytest.mark.usefixtures("tmp_project")
def test_trace_log_dir_is_created_but_not_writable_grant() -> None:
    notes = setup_notes("session-abc", "task-1")

    assert notes.trace_log.parent.is_dir()
    assert all(notes.trace_log.parent != rw for rw in notes.rw)
