"""The personal Claude profile registry that selects the launched account.

A wrong resolution here launches the harness with the wrong Claude
account: these pin that the first added profile becomes active, removal
clears activeness only for the removed profile, resolution prefers
explicit name over active over the ``~/.claude`` default, and an unknown
name is a loud error instead of a silent fallback.

Reading and curating are separate capabilities over one file, so each test
holds whichever it exercises and both are pointed at the same registry.
"""

from pathlib import Path

import pytest

from lup.adapters.claude.profile_store import (
    AccountFile,
    ClaudeProfileNames,
    ClaudeProfileRegistrar,
)


@pytest.fixture
def accounts(tmp_path: Path) -> AccountFile:
    return AccountFile(tmp_path / "profiles.json")


@pytest.fixture
def names(accounts: AccountFile) -> ClaudeProfileNames:
    return ClaudeProfileNames(accounts)


@pytest.fixture
def registrar(accounts: AccountFile) -> ClaudeProfileRegistrar:
    return ClaudeProfileRegistrar(accounts)


def test_first_added_profile_becomes_active(
    names: ClaudeProfileNames, registrar: ClaudeProfileRegistrar
) -> None:
    registrar.add_profile("work", Path("/homes/work-claude"))
    registrar.add_profile("personal", Path("/homes/personal-claude"))

    assert names.active_profile() == "work"
    assert names.config_dir_for("personal") == Path("/homes/personal-claude")


def test_set_active_requires_a_registered_profile(
    names: ClaudeProfileNames, registrar: ClaudeProfileRegistrar
) -> None:
    registrar.add_profile("work", Path("/homes/work-claude"))

    with pytest.raises(KeyError):
        registrar.set_active("ghost")
    registrar.set_active("work")
    assert names.active_profile() == "work"


def test_removing_the_active_profile_clears_only_its_activeness(
    accounts: AccountFile,
    names: ClaudeProfileNames,
    registrar: ClaudeProfileRegistrar,
) -> None:
    registrar.add_profile("work", Path("/homes/work-claude"))
    registrar.add_profile("personal", Path("/homes/personal-claude"))
    registrar.set_active("personal")

    registrar.remove_profile("personal")

    registry = accounts.load_registry()
    assert registry.active is None
    assert list(registry.profiles) == ["work"]

    registrar.set_active("work")
    registrar.remove_profile("personal")  # absent name is a no-op
    assert names.active_profile() == "work"


def test_resolution_prefers_explicit_then_active_then_default(
    accounts: AccountFile, registrar: ClaudeProfileRegistrar
) -> None:
    assert accounts.resolve_config_dir() == Path.home() / ".claude"

    registrar.add_profile("work", Path("/homes/work-claude"))
    registrar.add_profile("personal", Path("/homes/personal-claude"))

    assert accounts.resolve_config_dir() == Path("/homes/work-claude")
    assert accounts.resolve_config_dir("personal") == Path("/homes/personal-claude")


def test_unknown_profile_resolution_is_a_loud_error(
    accounts: AccountFile, registrar: ClaudeProfileRegistrar
) -> None:
    registrar.add_profile("work", Path("/homes/work-claude"))

    with pytest.raises(KeyError, match="unknown Claude profile 'ghost'"):
        accounts.resolve_config_dir("ghost")


def test_registry_updates_replace_the_file_atomically(
    names: ClaudeProfileNames, registrar: ClaudeProfileRegistrar, tmp_path: Path
) -> None:
    registrar.add_profile("work", Path("~/work-claude"))

    assert names.config_dir_for("work") == Path.home() / "work-claude"
    leftovers = [path.name for path in tmp_path.iterdir()]
    assert leftovers == ["profiles.json"]


def test_the_two_capabilities_read_one_registry(
    names: ClaudeProfileNames, registrar: ClaudeProfileRegistrar
) -> None:
    """Splitting the powers must not split the file they answer for."""
    registrar.add_profile("work", Path("/homes/work-claude"))

    assert names.names() == ["work"]
