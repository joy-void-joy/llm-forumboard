"""Behavior tests for `lup-devtools dev init`'s manifest rewrite.

The scaffold flag is what lets one rule read two ways: `# lup: template:`
markers are inventory in the scaffold that ships them and outstanding work in
the repository that adopted it. Clearing the flag is the moment that flips,
so these pin that adoption actually clears it — against this repository's own
`pyproject.toml`, because a flag the real manifest declares and the real
command fails to clear is the failure nobody would see until it had already
shipped into somebody else's repo.
"""

from pathlib import Path

import tomlkit

from lup.workspace.paths import is_template_scaffold
from lup_template.devtools.dev.init import clear_scaffold_flag

REPO_PYPROJECT = Path("pyproject.toml")


def test_this_repository_declares_itself_the_scaffold() -> None:
    assert is_template_scaffold(Path.cwd())


def test_adoption_clears_the_flag_this_repository_actually_declares(
    tmp_path: Path,
) -> None:
    """The real manifest through the real command — the two cannot drift apart."""
    adopted = tmp_path / "pyproject.toml"
    adopted.write_text(REPO_PYPROJECT.read_text(), encoding="utf-8")

    changes = clear_scaffold_flag(adopted, dry_run=False)

    assert changes, "adoption reported no change against a manifest that declares it"
    assert not is_template_scaffold(tmp_path)
    # The prose explaining the flag goes with it; a comment about a key that is
    # no longer there reads downstream as an instruction nobody can act on.
    assert "inventory rather than outstanding work" not in adopted.read_text()


def test_clearing_keeps_the_rest_of_the_table(tmp_path: Path) -> None:
    """Only the flag goes; a sibling key in the same table is untouched."""
    adopted = tmp_path / "pyproject.toml"
    adopted.write_text(REPO_PYPROJECT.read_text(), encoding="utf-8")
    before = tomlkit.parse(adopted.read_text())["tool"]["lup"]["agent_version"]  # pyright: ignore[reportIndexIssue]

    clear_scaffold_flag(adopted, dry_run=False)

    after = tomlkit.parse(adopted.read_text())["tool"]["lup"]["agent_version"]  # pyright: ignore[reportIndexIssue]
    assert after == before


def test_a_downstream_manifest_is_left_alone(tmp_path: Path) -> None:
    """Clearing an already-adopted manifest reports nothing and writes nothing."""
    adopted = tmp_path / "pyproject.toml"
    adopted.write_text('[tool.lup]\nagent_version = "0.2.0"\n', encoding="utf-8")
    before = adopted.read_text()

    assert clear_scaffold_flag(adopted, dry_run=False) == []
    assert adopted.read_text() == before


def test_dry_run_reports_without_writing(tmp_path: Path) -> None:
    adopted = tmp_path / "pyproject.toml"
    adopted.write_text(REPO_PYPROJECT.read_text(), encoding="utf-8")
    before = adopted.read_text()

    assert clear_scaffold_flag(adopted, dry_run=True)
    assert adopted.read_text() == before
    assert is_template_scaffold(tmp_path)
