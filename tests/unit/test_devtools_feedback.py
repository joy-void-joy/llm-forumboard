"""Behavior tests for `lup-devtools feedback` against a tmp project.

Pins: status reports session counts from disk, uncommitted-session
discovery survives paths with spaces and staged renames (porcelain -z),
and `collect` rejects the contradictory --since + --all-time combination.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lup.devtools.feedback import commits
from lup_template.devtools.main import app
from tests.unit.repos import initialized_repo

from tests.unit.conftest import LUP_PROJECT_VERSION

runner = CliRunner()


def make_session(root: Path, session_id: str, stamp: str) -> None:
    session_dir = (
        root / "notes" / "traces" / LUP_PROJECT_VERSION / "sessions" / session_id
    )
    session_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": "2026-01-01T12:00:00",
        "output": {"summary": f"summary for {session_id}"},
    }
    (session_dir / f"{stamp}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_status_reports_session_counts(tmp_lup_project: Path) -> None:
    for i in range(10):
        make_session(tmp_lup_project, f"sess-{i:02d}", f"20260101_1200{i:02d}")

    result = runner.invoke(app, ["feedback", "status", "-v", LUP_PROJECT_VERSION])

    assert result.exit_code == 0, result.output
    assert "Session directories: 10" in result.output
    assert "Unanalyzed: 10" in result.output


def test_collect_rejects_since_with_all_time(tmp_lup_project: Path) -> None:
    result = runner.invoke(
        app, ["feedback", "collect", "--since", "2026-01-01", "--all-time"]
    )

    assert result.exit_code == 1
    assert "mutually exclusive" in result.stderr


def session_path(repo: Path, session_id: str) -> Path:
    return repo / "notes" / "traces" / LUP_PROJECT_VERSION / "sessions" / session_id


def test_uncommitted_session_ids_handle_spaces_and_renames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    git = initialized_repo(repo, tmp_path / "no-hooks")

    committed = session_path(repo, "old-name")
    committed.mkdir(parents=True)
    (committed / "result.json").write_text("{}", encoding="utf-8")
    git("add", "notes")
    git("commit", "-m", "data(sessions): seed")

    spaced = session_path(repo, "sess with space")
    spaced.mkdir(parents=True)
    (spaced / "result.json").write_text("{}", encoding="utf-8")

    git("mv", str(committed), str(session_path(repo, "renamed-session")))

    monkeypatch.chdir(repo)
    session_ids = commits.get_uncommitted_session_ids()

    assert sorted(session_ids) == ["renamed-session", "sess with space"]
