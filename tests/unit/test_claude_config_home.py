"""Per-session Claude configuration homes, and the trust they record."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lup.adapters.claude.config_home import (
    CLAUDE_BACKUP_DIR,
    CLAUDE_CONFIG_FILE,
    CLAUDE_HOME_LAYOUT,
    ClaudeConfigUnreadable,
    load_document,
    record_trust,
    restorable_backups,
    restoration_advice,
    save_document,
    selected_config_home,
    trusts,
    untrusted_degradation,
    workspace_config_environment,
)
from lup.adapters.claude.login import CLAUDE_CONFIG_DIR
from lup.runtime.session_home import SessionHomes

SEEDED_PROJECT = "/already/trusted"


def shared_home(root: Path) -> Path:
    """A configuration home holding what a real profile carries."""
    home = root / "profile"
    (home / "plugins").mkdir(parents=True)
    (home / "settings.json").write_text('{"model": "opus"}', encoding="utf-8")
    (home / ".credentials.json").write_text('{"token": "shared"}', encoding="utf-8")
    save_document(
        home / CLAUDE_CONFIG_FILE,
        {"projects": {SEEDED_PROJECT: {"hasTrustDialogAccepted": True}}},
    )
    return home


def homes_under(root: Path) -> SessionHomes:
    return SessionHomes(shared_home(root), CLAUDE_HOME_LAYOUT)


def test_a_derived_home_shares_everything_but_the_document(tmp_path: Path) -> None:
    """Isolation is of the racing file alone, never of the whole home."""
    derived = homes_under(tmp_path).derive(tmp_path / "lease-a")

    assert (derived / "settings.json").is_symlink()
    assert (derived / "plugins").is_symlink()
    assert (derived / ".credentials.json").is_symlink()
    assert not (derived / CLAUDE_CONFIG_FILE).exists()


def test_a_refreshed_login_reaches_every_derived_home(tmp_path: Path) -> None:
    """One file for every session, because a rotation invalidates copies."""
    homes = homes_under(tmp_path)
    first = homes.derive(tmp_path / "lease-a")
    second = homes.derive(tmp_path / "lease-b")

    (first / ".credentials.json").write_text('{"token": "rotated"}', encoding="utf-8")

    rotated = '{"token": "rotated"}'
    assert (homes.shared / ".credentials.json").read_text(encoding="utf-8") == rotated
    assert (second / ".credentials.json").read_text(encoding="utf-8") == rotated


def test_an_entry_made_later_reaches_a_home_derived_before_it(tmp_path: Path) -> None:
    """A runtime makes directories the first time it needs one."""
    homes = homes_under(tmp_path)
    workspace = tmp_path / "lease-a"
    homes.derive(workspace)

    (homes.shared / "session-env").mkdir()

    assert (homes.derive(workspace) / "session-env").is_symlink()


def test_two_leases_sharing_a_basename_do_not_share_a_home(tmp_path: Path) -> None:
    """A run names every lease for its concern, under a root per run."""
    homes = homes_under(tmp_path)

    left = homes.derive(tmp_path / "run-a" / "notes-gate")
    right = homes.derive(tmp_path / "run-b" / "notes-gate")

    assert left != right


def test_a_worker_phase_of_twenty_eight_corrupts_no_document(tmp_path: Path) -> None:
    """The failing run opened one session per leased concern at once.

    Each start reads the whole document and writes it back, so sessions
    sharing one file read each other's partial writes and die on a truncated
    parse. This asserts what removes that: a document per workspace, every
    one of them still parsing and still carrying what it was seeded from.
    """
    home = shared_home(tmp_path)
    leases = [tmp_path / "leases" / f"concern-{number}" for number in range(28)]

    def open_session(workspace: Path) -> Path:
        environment = workspace_config_environment(
            {CLAUDE_CONFIG_DIR: str(home)}, workspace, trust=True
        )
        document = Path(environment[CLAUDE_CONFIG_DIR]) / CLAUDE_CONFIG_FILE
        for version in range(20):
            content = load_document(document)
            content["migrationVersion"] = version
            save_document(document, content)
        return document

    with ThreadPoolExecutor(max_workers=len(leases)) as pool:
        documents = list(pool.map(open_session, leases))

    assert len(set(documents)) == len(leases)
    for document, workspace in zip(documents, leases, strict=True):
        content = load_document(document)
        assert trusts(content, workspace)
        assert SEEDED_PROJECT in json.dumps(content)


def test_trust_is_recorded_for_the_lease_and_not_the_operator(tmp_path: Path) -> None:
    """The exception to project-level-only settings stays this narrow."""
    home = shared_home(tmp_path)
    before = (home / CLAUDE_CONFIG_FILE).read_bytes()
    lease = tmp_path / "lease"
    source = tmp_path / "source"

    def document_for(workspace: Path, trust: bool) -> Path:
        environment = workspace_config_environment(
            {CLAUDE_CONFIG_DIR: str(home)}, workspace, trust=trust
        )
        return Path(environment[CLAUDE_CONFIG_DIR]) / CLAUDE_CONFIG_FILE

    assert trusts(load_document(document_for(lease, True)), lease)
    assert not trusts(load_document(document_for(source, False)), source)
    assert (home / CLAUDE_CONFIG_FILE).read_bytes() == before


def test_an_unnamed_home_reads_the_document_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset, Claude reads ``~/.claude.json`` rather than an entry in the home."""
    monkeypatch.setenv("HOME", str(tmp_path))

    selected = selected_config_home({})

    assert selected.directory == tmp_path / ".claude"
    assert selected.document == tmp_path / ".claude.json"


def test_a_named_home_reads_the_document_inside_it(tmp_path: Path) -> None:
    selected = selected_config_home({CLAUDE_CONFIG_DIR: str(tmp_path / "account")})

    assert selected.directory == tmp_path / "account"
    assert selected.document == tmp_path / "account" / CLAUDE_CONFIG_FILE


def test_an_untrusted_workspace_names_what_it_is_dropping(tmp_path: Path) -> None:
    """Untrusted-and-degraded is stated once rather than warned per session."""
    workspace = tmp_path / "repo"
    (workspace / ".claude").mkdir(parents=True)
    (workspace / ".claude" / "settings.json").write_text(
        json.dumps(
            {"permissions": {"allow": ["Bash(git status:*)", "Read(//tmp/**)"]}}
        ),
        encoding="utf-8",
    )
    document = tmp_path / CLAUDE_CONFIG_FILE
    save_document(document, {})

    message = untrusted_degradation(workspace, document)
    assert message is not None
    assert "2 permissions.allow entries" in message

    record_trust(document, workspace)
    assert untrusted_degradation(workspace, document) is None


def test_a_workspace_declaring_nothing_loses_nothing(tmp_path: Path) -> None:
    document = tmp_path / CLAUDE_CONFIG_FILE
    save_document(document, {})

    assert untrusted_degradation(tmp_path / "repo", document) is None


def test_a_truncated_document_is_refused_rather_than_emptied(tmp_path: Path) -> None:
    """``Unexpected EOF`` must not read as a profile trusting nothing."""
    document = tmp_path / CLAUDE_CONFIG_FILE
    document.write_text('{"projects": {', encoding="utf-8")

    with pytest.raises(ClaudeConfigUnreadable):
        load_document(document)


def test_an_absent_document_is_not_an_error(tmp_path: Path) -> None:
    assert load_document(tmp_path / CLAUDE_CONFIG_FILE) == {}


def backup(home: Path, name: str, content: str) -> Path:
    """One file where Claude Code copies a document it could not read."""
    written = home / CLAUDE_BACKUP_DIR / f"{CLAUDE_CONFIG_FILE}.backup.{name}"
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text(content, encoding="utf-8")
    return written


def test_a_backup_that_cannot_restore_is_not_offered(tmp_path: Path) -> None:
    """The hint named a zero-byte file, and following it emptied a healed one."""
    backup(tmp_path, "empty", "")
    backup(tmp_path, "flags", json.dumps({"migrationVersion": 13}))

    assert restorable_backups(tmp_path) == []
    assert "No backup under" in restoration_advice(tmp_path)


def test_the_newest_backup_carrying_projects_is_the_one_named(
    tmp_path: Path,
) -> None:
    """Newest by when it was written, so restoring loses the least."""
    older = backup(tmp_path, "older", json.dumps({"projects": {"/a": {}}}))
    newer = backup(tmp_path, "newer", json.dumps({"projects": {"/b": {}}}))
    backup(tmp_path, "truncated", '{"projects": {')
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    assert restorable_backups(tmp_path) == [newer, older]
    assert str(newer) in restoration_advice(tmp_path)


def test_an_unreadable_document_stops_a_run_before_it_leases(
    tmp_path: Path,
) -> None:
    """Every derived home is seeded from this one, so nothing can start."""
    home = selected_config_home({CLAUDE_CONFIG_DIR: str(tmp_path)})
    home.document.write_text('{"projects": {', encoding="utf-8")
    restorable = backup(tmp_path, "whole", json.dumps({"projects": {"/a": {}}}))

    fault = home.configuration_fault()
    assert fault is not None
    assert "does not parse" in fault
    assert str(restorable) in fault


def test_a_readable_document_is_no_fault(tmp_path: Path) -> None:
    home = selected_config_home({CLAUDE_CONFIG_DIR: str(tmp_path)})
    save_document(home.document, {})

    assert home.configuration_fault() is None
