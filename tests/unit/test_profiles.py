"""What the profile listing answers, and what the setup wizard offers.

Enrolment is two independent facts — a stored claude.ai session and a roster
entry — and every surface that reports them derives the pair from
:func:`profile_states`. These pin the disagreements: an account kept but never
enrolled, and an enrolment that has not signed in yet. The second is the one
that matters, because it is the case where somebody believes they are being
synced and is not, and a listing built from directories alone cannot show it.

A third fact rides along in the listing without belonging to enrolment: whether
a directory is kept here at all. It is what separates two rows that both read
as unsynced — one with credentials on this machine, one with nothing — and so
what decides whether there is anything an operator could remove.

The wizard tests pin which tree ``setup`` mounts. A profile in this
application is a person whose conversations are read; the harness tree that
curates which Claude account the *agent* runs under answers a different
question and belongs to ``lup-devtools harness profile``.
"""

from pathlib import Path

from forumboard.devtools.setup import INTEGRATIONS, profiles_status
from forumboard.devtools.setup import app as setup_app
from forumboard.profiles import profile_states, profiles_root, read_roster
from tests.unit.conftest import enrolled, keep_account, sign_in


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


def test_a_kept_directory_is_the_third_fact_the_pair_cannot_carry(
    tmp_path: Path,
) -> None:
    """Kept but never signed in, against enrolled with nothing on disk.

    Both read as "not signed in" and neither syncs, but only one of them has
    credentials here to remove — which is the question ``kept`` answers.
    """
    keep_account(tmp_path, "bo")
    enrolled(tmp_path, "ana")
    ana, bo = profile_states(tmp_path, profiles_root(tmp_path))

    assert bo.kept and not bo.signed_in
    assert ana.enrolled and not ana.kept


def test_a_note_is_replaced_without_moving_the_enrolment_date(tmp_path: Path) -> None:
    """When somebody agreed is not changed by writing down what they agreed to."""
    enrolled(tmp_path, "ana")
    original = read_roster(tmp_path)
    [before] = original.entries

    original.with_note("ana", "asked in standup").write(tmp_path)
    [after] = read_roster(tmp_path).entries

    assert after.note == "asked in standup"
    assert after.enrolled_at == before.enrolled_at


def test_a_note_for_somebody_not_enrolled_changes_nothing(tmp_path: Path) -> None:
    """There is nowhere to put one, and enrolling them to make room is consent."""
    enrolled(tmp_path, "ana")
    roster = read_roster(tmp_path).with_note("bo", "arranged by nobody")

    assert roster.names() == ["ana"]
    assert [entry.note for entry in roster.entries] == [""]


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
