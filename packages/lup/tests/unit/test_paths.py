"""Lazy path resolution, configure() tolerance, and access-control helpers.

Resolution must be lazy (importing lup.workspace.paths never auto-detects) and
configure() must accept roots that are not real projects: a missing
pyproject.toml falls back to version "0.0.0", and an explicit version
skips reading the file entirely.
"""

from pathlib import Path

import pytest

from lup.workspace import paths
from lup.workspace.paths import extract_glob_dir, path_is_under


@pytest.fixture
def isolated_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear cached path state; auto-detection must not run during the test."""

    def fail_autodetect() -> Path:
        raise AssertionError("find_project_root must not run — configure() came first")

    monkeypatch.setattr(paths.state, "config", None)
    monkeypatch.setattr(paths, "find_project_root", fail_autodetect)


@pytest.mark.usefixtures("isolated_state")
def test_configure_before_first_access_skips_autodetection(tmp_path: Path) -> None:
    paths.configure(root=tmp_path, version="9.9.9")

    assert paths.project_root() == tmp_path
    assert paths.agent_version() == "9.9.9"
    assert paths.notes_path() == tmp_path / "notes"
    assert paths.runtime_logs_path() == tmp_path / "logs"
    assert paths.sessions_dir() == tmp_path / "notes" / "traces" / "9.9.9" / "sessions"
    assert paths.outputs_dir() == tmp_path / "notes" / "traces" / "9.9.9" / "outputs"
    assert paths.trace_logs_dir() == tmp_path / "notes" / "traces" / "9.9.9" / "logs"
    assert paths.feedback_path() == tmp_path / "notes" / "feedback_loop"


@pytest.mark.usefixtures("isolated_state")
def test_configure_root_without_pyproject_defaults_version(tmp_path: Path) -> None:
    paths.configure(root=tmp_path)
    assert paths.agent_version() == "0.0.0"
    assert paths.project_root() == tmp_path


@pytest.mark.usefixtures("isolated_state")
def test_configure_root_reads_version_from_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.lup]\nagent_version = "3.1.4"\n', encoding="utf-8"
    )
    paths.configure(root=tmp_path)
    assert paths.agent_version() == "3.1.4"


@pytest.mark.usefixtures("isolated_state")
def test_partial_overrides_keep_other_values(tmp_path: Path) -> None:
    paths.configure(root=tmp_path, version="1.0.0")
    paths.configure(notes_dir=tmp_path / "elsewhere")

    assert paths.notes_path() == tmp_path / "elsewhere"
    assert paths.project_root() == tmp_path
    assert paths.agent_version() == "1.0.0"
    assert (
        paths.sessions_dir() == tmp_path / "elsewhere" / "traces" / "1.0.0" / "sessions"
    )


@pytest.mark.usefixtures("isolated_state")
def test_explicit_version_and_dirs_never_touch_pyproject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_read(_root: Path) -> str:
        raise AssertionError("pyproject must not be read when version is explicit")

    monkeypatch.setattr(paths, "read_agent_version", fail_read)
    paths.configure(
        root=tmp_path / "ghost",
        version="2.0.0",
        notes_dir=tmp_path / "n",
        logs_dir=tmp_path / "l",
    )

    assert paths.agent_version() == "2.0.0"
    assert paths.notes_path() == tmp_path / "n"
    assert paths.runtime_logs_path() == tmp_path / "l"


def test_explicit_version_argument_overrides_per_call(tmp_lup_project: Path) -> None:
    assert paths.sessions_dir("7.7.7") == (
        tmp_lup_project / "notes" / "traces" / "7.7.7" / "sessions"
    )


def test_path_is_under_rejects_outside(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    assert path_is_under(allowed / "child.json", [allowed])
    assert path_is_under(allowed / "deep" / "child.json", [allowed])
    assert not path_is_under(tmp_path / "sibling.json", [allowed])


def test_path_is_under_blocks_traversal(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    escape = allowed / ".." / "secret.json"
    assert not path_is_under(escape, [allowed])


def test_autodetection_answers_about_the_project_the_command_runs_against(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Installed as a dependency the library sits outside every project it serves.

    Where it is installed is then a fact about the environment, so the fallback
    that reads it must never win over the working directory.
    """
    project = tmp_path / "adopter"
    (project / "src").mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        '[project]\nname = "adopter"\n\n[tool.lup]\nagent_version = "3.1.4"\n'
    )
    elsewhere = tmp_path / "shared-venv"
    elsewhere.mkdir()

    def installed_outside_any_project() -> Path:
        raise AssertionError(
            "the working directory answered; the fallback must not run"
        )

    monkeypatch.setattr(paths.state, "config", None)
    monkeypatch.setattr(paths, "find_project_root", installed_outside_any_project)
    monkeypatch.chdir(project / "src")

    assert paths.project_root() == project
    assert paths.agent_version() == "3.1.4"


def test_extract_glob_dir() -> None:
    assert extract_glob_dir("/tmp/foo/**/*.py") == "/tmp/foo"
    assert extract_glob_dir("**/*.py") == ""
    assert extract_glob_dir("/tmp/foo/bar") == "/tmp/foo/bar"
    assert extract_glob_dir("/a/b/*.txt") == "/a/b"
    assert extract_glob_dir("/a/[abc].py") == "/a"
