"""What the profile listing answers, and what the setup wizard offers.

Enrolment is two independent facts — a stored claude.ai session and a roster
entry — and every surface that reports them derives the pair from
:func:`profile_states`. These pin the disagreements: an account kept but never
enrolled, and an enrolment that has not signed in yet. The second is the one
that matters, because it is the case where somebody believes they are being
synced and is not, and a listing built from directories alone cannot show it.

The wizard tests pin which tree ``setup`` mounts. A profile in this
application is a person whose conversations are read; the harness tree that
curates which Claude account the *agent* runs under answers a different
question and belongs to ``lup-devtools harness profile``.
"""

from pathlib import Path

from forumboard.devtools.setup import INTEGRATIONS, profiles_status
from forumboard.devtools.setup import app as setup_app
from forumboard.profiles import Roster, profile_states, profiles_root


def enrolled(root: Path, *names: str) -> None:
    """Write a roster holding exactly ``names``."""
    roster = Roster()
    for name in names:
        roster = roster.with_profile(name)
    roster.write(root)


def sign_in(root: Path, name: str) -> None:
    """Leave what a completed claude.ai login leaves behind."""
    session = profiles_root(root) / name / "claude-web"
    session.mkdir(parents=True)
    (session / "Cookies").write_text("stored")


def keep_account(root: Path, name: str) -> None:
    """Make a profile directory without a claude.ai session in it."""
    (profiles_root(root) / name / "claude-config").mkdir(parents=True)


def test_listing_is_empty_before_anybody_is_kept_or_enrolled(tmp_path: Path) -> None:
    assert profile_states(tmp_path, profiles_root(tmp_path)) == []


def test_an_enrolment_without_a_directory_is_listed(tmp_path: Path) -> None:
    """The case a directory listing hides: agreed to, never signed in."""
    enrolled(tmp_path, "ana")
    [state] = profile_states(tmp_path, profiles_root(tmp_path))
    assert state.name == "ana"
    assert state.enrolled
    assert not state.signed_in
    assert not state.syncable()
    assert "NOT signed in" in state.describe()


def test_a_directory_without_an_enrolment_is_listed(tmp_path: Path) -> None:
    """An account kept here is not consent to read it."""
    keep_account(tmp_path, "bo")
    [state] = profile_states(tmp_path, profiles_root(tmp_path))
    assert state.name == "bo"
    assert not state.enrolled
    assert not state.syncable()


def test_a_session_directory_reads_as_signed_in(tmp_path: Path) -> None:
    sign_in(tmp_path, "cass")
    [state] = profile_states(tmp_path, profiles_root(tmp_path))
    assert state.signed_in
    assert not state.enrolled


def test_only_both_facts_make_a_profile_syncable(tmp_path: Path) -> None:
    sign_in(tmp_path, "dev")
    enrolled(tmp_path, "dev")
    [state] = profile_states(tmp_path, profiles_root(tmp_path))
    assert state.syncable()
    assert state.describe() == "dev: enrolled, signed in"


def test_the_listing_unions_both_sources_in_name_order(tmp_path: Path) -> None:
    keep_account(tmp_path, "bo")
    sign_in(tmp_path, "cass")
    enrolled(tmp_path, "ana", "cass")
    states = profile_states(tmp_path, profiles_root(tmp_path))
    assert [state.name for state in states] == ["ana", "bo", "cass"]
    assert [state.syncable() for state in states] == [False, False, True]


def test_status_is_red_until_somebody_is_both(tmp_lup_project: Path) -> None:
    """Everything else can be configured while the pipeline reads nobody."""
    assert not profiles_status({}).ok
    assert profiles_status({}).detail == "no profiles yet"

    enrolled(tmp_lup_project, "ana")
    status = profiles_status({})
    assert not status.ok
    assert status.detail == "0 of 1 enrolled and signed in"

    sign_in(tmp_lup_project, "ana")
    status = profiles_status({})
    assert status.ok
    assert status.detail == "1 of 1 enrolled and signed in"


def test_the_wizard_offers_an_enrolment_step() -> None:
    commands = [integration.command for integration in INTEGRATIONS]
    assert "profiles" in commands


def test_the_enrolment_step_writes_no_environment() -> None:
    """Enrolment belongs in the committed roster, not in .env.local."""
    [profiles] = [i for i in INTEGRATIONS if i.command == "profiles"]
    assert profiles.env_keys == []


def test_the_wizard_does_not_mount_the_harness_account_tree() -> None:
    """`setup profile` would answer the enrolment question with the wrong tree."""
    groups = [group.name for group in setup_app.registered_groups]
    assert "profile" not in groups
