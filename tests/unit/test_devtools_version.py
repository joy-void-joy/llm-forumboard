# lup: ignore[bare-object, tuple-shape]
# Test fixtures and assertions construct these shapes deliberately.
"""Behavior tests for `lup-devtools version`: commit classification and bump.

`classify_commit` feeds the changelog's behavior/data/infrastructure split;
`bump_cmd` must compute the next semver, rewrite pyproject.toml, and tag —
or refuse cleanly on bad levels and non-semver versions.
"""

import json
from pathlib import Path

import pytest
import typer

from lup.workspace import paths
from lup.devtools import version


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("feat(agent): add a tool", "behavior"),
        ("Fix: crash on empty input", "behavior"),
        ("refactor(core): split module", "behavior"),
        ("data(outputs): session results", "data"),
        ("docs: update README", "infrastructure"),
        ("chore(deps): bump ruff", "infrastructure"),
        ("test: add coverage", "infrastructure"),
        ("meta(claude): tune hooks", "infrastructure"),
    ],
)
def test_classify_commit_buckets_by_conventional_prefix(
    message: str, category: str
) -> None:
    assert version.classify_commit(message) == category


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.lup]\nagent_version = "1.2.3"\n', encoding="utf-8"
    )
    monkeypatch.setattr(paths, "find_project_root", lambda: tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    ("level", "expected"),
    [("patch", "1.2.4"), ("minor", "1.3.0"), ("major", "2.0.0")],
)
def test_bump_dry_run_computes_next_version(
    project: Path,
    capsys: pytest.CaptureFixture[str],
    level: str,
    expected: str,
) -> None:
    version.bump_cmd(level, as_json=True, dry_run=True)

    result = json.loads(capsys.readouterr().out)
    assert result == {"old": "1.2.3", "new": expected, "tag": f"v{expected}"}
    # Dry run must leave the project untouched.
    assert 'agent_version = "1.2.3"' in (project / "pyproject.toml").read_text()


def test_bump_rejects_bad_or_missing_level(project: Path) -> None:
    with pytest.raises(typer.Exit):
        version.bump_cmd(None, as_json=False, dry_run=False)
    with pytest.raises(typer.Exit):
        version.bump_cmd("gigantic", as_json=False, dry_run=False)


def test_bump_rejects_non_semver_version(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / "pyproject.toml").write_text(
        '[tool.lup]\nagent_version = "one.two"\n', encoding="utf-8"
    )
    with pytest.raises(typer.Exit):
        version.bump_cmd("patch", as_json=False, dry_run=False)


class FakeGit:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def record(self, name: str) -> object:
        def run(*args: str) -> str:
            self.calls.append((name, *args))
            return ""

        return run

    def __getattr__(self, name: str) -> object:
        return self.record(name)


def test_bump_writes_pyproject_and_tags(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGit()
    monkeypatch.setattr(version, "git", fake)

    version.bump_cmd("minor", as_json=False, dry_run=False)

    assert 'agent_version = "1.3.0"' in (project / "pyproject.toml").read_text()
    ops = [call[0] for call in fake.calls]
    assert ops == ["add", "commit", "tag"]
    assert fake.calls[-1] == ("tag", "v1.3.0")
