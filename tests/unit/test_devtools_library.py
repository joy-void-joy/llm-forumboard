"""Behavior tests for `lup-devtools dev library`: how a project obtains lup.

The mode is a property of pyproject.toml, so every assertion here reads the
rewritten file back rather than trusting the change log. What must hold is
that un-vendoring removes every path that stops resolving once the package is
gone, that vendoring restores exactly those, and that the round trip is the
identity — a project can move between modes without accumulating drift.
"""

import tomllib
from pathlib import Path

import pytest
import typer

from lup.types import JsonValue
from lup_template.devtools.dev import library

VENDORED_PYPROJECT = """\
[project]
name = "demo"
dependencies = ["fastapi>=0.139.0", "lup[claude,codex,docker]", "typer>=0.21.1"]

[tool.uv]
exclude-newer = "3 days"

[tool.uv.workspace]
members = ["packages/*"]

[tool.uv.sources]
lup = { workspace = true }

[tool.pytest.ini_options]
pythonpath = ["src", "packages/lup/src"]

[tool.pyright]
include = ["src", "packages/lup/src", "tests", "packages/lup/tests"]

[[tool.pyright.executionEnvironments]]
root = "packages/lup/src/lup/adapters/claude/assets"
extraPaths = [".claude/plugins/lup/hooks/runtime", "packages/lup/src/lup/policy/assets"]

[[tool.pyright.executionEnvironments]]
root = ".claude/plugins/lup/hooks/scripts"
extraPaths = [".claude/plugins/lup/hooks/runtime"]

[[tool.pyright.executionEnvironments]]
root = "packages/lup/src/lup/adapters/codex/assets"
extraPaths = [".codex/plugins/lup/hooks/runtime", "packages/lup/src/lup/policy/assets"]

[[tool.pyright.executionEnvironments]]
root = ".codex/plugins/lup/hooks/scripts"
extraPaths = [".codex/plugins/lup/hooks/runtime"]
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(VENDORED_PYPROJECT, encoding="utf-8")
    (tmp_path / library.VENDORED_ROOT).mkdir(parents=True)
    (tmp_path / library.VENDORED_ROOT / "pyproject.toml").write_text(
        '[project]\nname = "lup"\n', encoding="utf-8"
    )
    return tmp_path


def at(root: Path, *keys: str) -> JsonValue:
    """Walk a key path through pyproject.toml, or None where it stops."""
    with (root / "pyproject.toml").open("rb") as handle:
        value: JsonValue = tomllib.load(handle)
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def strings(root: Path, *keys: str) -> list[str]:
    """Read a list-of-strings setting, or an empty list where it is absent."""
    value = at(root, *keys)
    return [str(item) for item in value] if isinstance(value, list) else []


def environment_roots(root: Path) -> list[str]:
    """Name the root of every declared pyright execution environment."""
    match at(root, "tool", "pyright", "executionEnvironments"):
        case [*environments]:
            return [
                str(entry["root"])
                for entry in environments
                if isinstance(entry, dict) and "root" in entry
            ]
        case _:
            return []


def test_a_vendored_project_reads_as_local(project: Path) -> None:
    assert library.read_mode(project) is library.LibraryMode.LOCAL


def test_publishing_drops_every_path_that_stops_resolving(project: Path) -> None:
    library.set_mode(project, library.LibraryMode.PUBLISHED, version="0.3.0")

    assert library.read_mode(project) is library.LibraryMode.PUBLISHED
    assert "lup[claude,codex,docker]>=0.3.0" in strings(
        project, "project", "dependencies"
    )
    assert at(project, "tool", "uv", "workspace") is None
    assert strings(project, "tool", "pytest", "ini_options", "pythonpath") == ["src"]
    assert library.VENDORED_SRC not in strings(project, "tool", "pyright", "include")
    assert not [
        root for root in environment_roots(project) if library.VENDORED_ROOT in root
    ]


def test_publishing_keeps_the_generated_tree_environments(project: Path) -> None:
    library.set_mode(project, library.LibraryMode.PUBLISHED, version="0.3.0")

    assert environment_roots(project) == [
        ".claude/plugins/lup/hooks/scripts",
        ".codex/plugins/lup/hooks/scripts",
    ]


def test_linking_points_at_the_checkout_and_keeps_no_version_bound(
    project: Path, tmp_path: Path
) -> None:
    checkout = tmp_path / "elsewhere" / "packages" / "lup"
    checkout.mkdir(parents=True)

    library.set_mode(project, library.LibraryMode.LINKED, checkout=checkout)

    assert library.read_mode(project) is library.LibraryMode.LINKED
    assert library.read_linked_path(project) == checkout
    assert at(project, "tool", "uv", "sources", "lup", "editable") is True
    assert "lup[claude,codex,docker]" in strings(project, "project", "dependencies")


def test_un_vendoring_drops_the_tests_root_along_with_the_source_one(
    project: Path,
) -> None:
    library.set_mode(project, library.LibraryMode.PUBLISHED, version="0.3.0")

    # Both roots live inside the package that is about to be deleted, so a
    # pyright include naming either would point at nothing.
    assert strings(project, "tool", "pyright", "include") == ["src", "tests"]


def test_a_git_project_reads_as_git_and_names_where_it_resolves(project: Path) -> None:
    source = library.GitSource(
        url="https://github.com/joy-void-joy/lup", ref_kind="branch", ref="dev"
    )

    library.set_mode(project, library.LibraryMode.GIT, git=source)

    assert library.read_mode(project) is library.LibraryMode.GIT
    assert library.read_git_source(project) == source
    # The distribution sits inside the repository, not at its root.
    assert at(project, "tool", "uv", "sources", "lup", "subdirectory") == "packages/lup"
    # A source override supplies the version, so no bound is restated.
    assert "lup[claude,codex,docker]" in strings(project, "project", "dependencies")


def test_git_un_vendors_exactly_as_publishing_does(project: Path) -> None:
    library.set_mode(project, library.LibraryMode.GIT, git=library.GitSource(ref="dev"))

    assert at(project, "tool", "uv", "workspace") is None
    assert strings(project, "tool", "pytest", "ini_options", "pythonpath") == ["src"]
    assert environment_roots(project) == [
        ".claude/plugins/lup/hooks/scripts",
        ".codex/plugins/lup/hooks/scripts",
    ]


def test_each_kind_of_ref_is_written_under_its_own_key(project: Path) -> None:
    for kind in ("branch", "tag", "rev"):
        source = library.GitSource(ref_kind=kind, ref="something")
        library.set_mode(project, library.LibraryMode.GIT, git=source)

        assert at(project, "tool", "uv", "sources", "lup", kind) == "something"
        assert library.read_git_source(project) == source


def test_a_command_line_may_name_one_ref_and_no_more() -> None:
    assert library.git_source("u", branch="dev").ref == "dev"
    assert library.git_source("u", tag="v1").ref_kind == "tag"
    # No ref named at all falls back to the repository's default branch.
    assert library.git_source("u").ref == "main"

    with pytest.raises(typer.BadParameter, match="--branch and --tag"):
        library.git_source("u", branch="dev", tag="v1")


def test_git_mode_needs_somewhere_to_resolve_from(project: Path) -> None:
    with pytest.raises(ValueError, match="repository and ref"):
        library.set_mode(project, library.LibraryMode.GIT)


def test_moving_between_modes_and_back_settles_where_it_started(project: Path) -> None:
    """Table order in the file may shift; nothing may be lost, gained, or reordered."""
    before = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))

    library.set_mode(project, library.LibraryMode.PUBLISHED, version="0.3.0")
    library.set_mode(project, library.LibraryMode.LOCAL)

    assert tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8")) == (
        before
    )


def test_restating_a_settled_mode_changes_nothing(project: Path) -> None:
    assert library.set_mode(project, library.LibraryMode.LOCAL) == []


def test_vendoring_without_a_package_present_is_refused(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(VENDORED_PYPROJECT, encoding="utf-8")

    with pytest.raises(typer.BadParameter, match="no library to vendor"):
        library.set_mode(tmp_path, library.LibraryMode.LOCAL)


def test_a_dry_run_reports_without_writing(project: Path) -> None:
    before = (project / "pyproject.toml").read_text(encoding="utf-8")

    changes = library.set_mode(
        project, library.LibraryMode.PUBLISHED, version="0.3.0", dry_run=True
    )

    assert changes
    assert (project / "pyproject.toml").read_text(encoding="utf-8") == before


def test_an_unrenamed_template_refuses_to_un_vendor(project: Path) -> None:
    (project / "src" / "lup_template").mkdir(parents=True)

    with pytest.raises(typer.BadParameter, match="template itself"):
        library.guard_leaving_local(project, force=False)

    library.guard_leaving_local(project, force=True)
