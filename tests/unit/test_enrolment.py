"""What each surface may offer a profile, and what each act actually does.

The acts are declared once and rendered by the terminal editor and the served
page, so what is pinned here is mostly the offer: which verbs a state allows.
That is the half a surface cannot be trusted to get right on its own — a button
drawn for an act the state cannot honour is an instruction the system will
refuse, and a destructive one drawn a step too early is worse than that.

The rest pins the two acts that delete something. Both check the name typed
back at them rather than trusting whichever surface drew a confirmation, so a
caller that never drew one — a request straight at the socket — meets the same
refusal.
"""

from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from forumboard.enrolment import (
    DeleteProfile,
    Enrol,
    ForgetSession,
    ProfileActs,
    ProfileTarget,
    SetNote,
    SignIn,
    Withdraw,
)
from forumboard.environment.cli.__main__ import app
from forumboard.environment.web.app import create_app
from forumboard.profiles import ProfileState, profiles_root, read_roster
from tests.unit.conftest import enrolled, keep_account, sign_in

PLAIN_CONSOLE = {"FORCE_COLOR": None, "NO_COLOR": "1", "TERM": "dumb"}
"""Rich styles option tokens whenever it believes it is writing to a terminal."""

runner = CliRunner(env=PLAIN_CONSOLE)


def target(root: Path, name: str) -> ProfileTarget:
    """One profile of a scratch workspace, as an act reaches it."""
    return ProfileTarget(
        project_root=root, profiles_root=profiles_root(root), name=name
    )


def state(
    *, enrolled: bool = False, signed_in: bool = False, kept: bool = False
) -> ProfileState:
    """A profile in exactly the combination of facts named."""
    return ProfileState(name="ana", enrolled=enrolled, signed_in=signed_in, kept=kept)


def offered(**facts: bool) -> list[str]:
    """The slugs a profile in this state is offered, in menu order."""
    return [act.slug for act in ProfileActs().offered(state(**facts))]


def test_a_stranger_can_only_be_signed_in_or_enrolled() -> None:
    """Nothing is kept for them and nothing was agreed, so nothing else applies."""
    assert offered() == ["sign-in", "enrol"]


def test_withdrawing_and_noting_arrive_together_with_enrolment() -> None:
    """Both are things an enrolment carries, so neither exists without one."""
    assert offered(enrolled=True) == ["sign-in", "set-note", "withdraw"]


def test_forgetting_a_session_is_offered_only_where_there_is_one() -> None:
    assert "forget-session" not in offered(kept=True)
    assert "forget-session" in offered(kept=True, signed_in=True)


def test_deleting_a_profile_waits_until_they_have_been_withdrawn() -> None:
    """The directory holds the agent's own login, so it is never a side effect.

    Leaving the roster entry behind would also leave the sync reading somebody
    whose session has been deleted, which reports as an expired cookie rather
    than as what happened.
    """
    assert "delete-profile" not in offered(kept=True, signed_in=True, enrolled=True)
    assert "delete-profile" in offered(kept=True, signed_in=True)


def test_nothing_is_offered_for_a_directory_that_does_not_exist() -> None:
    """An enrolment with nothing on disk has nothing to delete."""
    assert "delete-profile" not in offered(enrolled=True)


async def test_enrolling_records_the_note_it_was_given(tmp_path: Path) -> None:
    outcome = await Enrol(answer="asked in standup").perform(target(tmp_path, "ana"))

    assert outcome.changed
    [entry] = read_roster(tmp_path).entries
    assert (entry.profile, entry.note) == ("ana", "asked in standup")


async def test_enrolling_somebody_already_enrolled_changes_nothing(
    tmp_path: Path,
) -> None:
    enrolled(tmp_path, "ana")
    outcome = await Enrol(answer="again").perform(target(tmp_path, "ana"))

    assert not outcome.changed
    assert [entry.note for entry in read_roster(tmp_path).entries] == [""]


async def test_withdrawing_leaves_the_stored_session_alone(tmp_path: Path) -> None:
    """Ending a sync is not revoking access, and revoking is its own act."""
    sign_in(tmp_path, "ana")
    enrolled(tmp_path, "ana")
    outcome = await Withdraw().perform(target(tmp_path, "ana"))

    assert outcome.changed
    assert read_roster(tmp_path).names() == []
    assert (profiles_root(tmp_path) / "ana" / "claude-web").is_dir()


async def test_a_note_reaches_the_roster(tmp_path: Path) -> None:
    enrolled(tmp_path, "ana")
    outcome = await SetNote(answer="agreed in writing").perform(target(tmp_path, "ana"))

    assert outcome.changed
    assert [entry.note for entry in read_roster(tmp_path).entries] == [
        "agreed in writing"
    ]


async def test_forgetting_a_session_keeps_the_account_beside_it(tmp_path: Path) -> None:
    """The two credentials expire independently, so removing one is not both."""
    sign_in(tmp_path, "ana")
    keep_account(tmp_path, "ana")
    outcome = await ForgetSession(answer="ana").perform(target(tmp_path, "ana"))

    assert outcome.changed
    assert not (profiles_root(tmp_path) / "ana" / "claude-web").exists()
    assert (profiles_root(tmp_path) / "ana" / "claude-config").is_dir()


async def test_a_mistyped_name_deletes_no_session(tmp_path: Path) -> None:
    sign_in(tmp_path, "ana")
    outcome = await ForgetSession(answer="ANA").perform(target(tmp_path, "ana"))

    assert not outcome.changed
    assert (profiles_root(tmp_path) / "ana" / "claude-web").is_dir()


async def test_deleting_a_profile_takes_the_whole_directory(tmp_path: Path) -> None:
    sign_in(tmp_path, "ana")
    keep_account(tmp_path, "ana")
    outcome = await DeleteProfile(answer="ana").perform(target(tmp_path, "ana"))

    assert outcome.changed
    assert not (profiles_root(tmp_path) / "ana").exists()


async def test_a_mistyped_name_deletes_no_directory(tmp_path: Path) -> None:
    keep_account(tmp_path, "ana")
    outcome = await DeleteProfile(answer="").perform(target(tmp_path, "ana"))

    assert not outcome.changed
    assert (profiles_root(tmp_path) / "ana" / "claude-config").is_dir()


async def test_a_name_the_listing_has_never_seen_has_a_state_anyway(
    tmp_path: Path,
) -> None:
    """Which is what lets adding somebody run through the same menu."""
    unheard = target(tmp_path, "nobody").state()

    assert unheard == ProfileState(name="nobody", enrolled=False, signed_in=False)
    assert SignIn().applies(unheard)


def test_the_editor_acts_on_a_profile_chosen_by_its_number(
    tmp_lup_project: Path,
) -> None:
    """The gap this closes: acting on a listed profile without retyping its name.

    ``1`` selects the only row, ``2`` is Enrol among what a kept-but-unenrolled
    profile is offered, the note is typed, and ``q`` finishes.
    """
    keep_account(tmp_lup_project, "ana")
    result = runner.invoke(app, ["profile", "edit"], input="1\n2\nasked them\nq\n")

    assert result.exit_code == 0, result.output
    [entry] = read_roster(tmp_lup_project).entries
    assert (entry.profile, entry.note) == ("ana", "asked them")


def test_the_editor_offers_nothing_a_state_disallows(tmp_lup_project: Path) -> None:
    """Withdraw is not among a stranger's verbs, so its number cannot reach it."""
    keep_account(tmp_lup_project, "ana")
    result = runner.invoke(app, ["profile", "edit"], input="1\nq\n")

    assert "Withdraw" not in result.output
    assert "Enrol for syncing" in result.output


def test_a_one_shot_verb_and_the_editor_write_the_same_roster(
    tmp_lup_project: Path,
) -> None:
    enrolled(tmp_lup_project, "ana")
    result = runner.invoke(app, ["profile", "note", "ana", "arranged by bo"])

    assert result.exit_code == 0, result.output
    assert [e.note for e in read_roster(tmp_lup_project).entries] == ["arranged by bo"]


def client(root: Path) -> TestClient:
    """The served page over a scratch workspace."""
    return TestClient(create_app(root, profiles_root(root)))


def test_the_page_carries_the_acts_each_row_allows(tmp_lup_project: Path) -> None:
    keep_account(tmp_lup_project, "bo")
    sign_in(tmp_lup_project, "cass")
    enrolled(tmp_lup_project, "cass")

    listing = client(tmp_lup_project).get("/api/profiles").json()
    rows = {
        row["name"]: [act["slug"] for act in row["acts"]] for row in listing["profiles"]
    }

    assert rows["bo"] == ["sign-in", "enrol", "delete-profile"]
    assert rows["cass"] == ["sign-in", "set-note", "withdraw", "forget-session"]


def test_the_page_enrols_a_name_that_was_typed_in(tmp_lup_project: Path) -> None:
    """A person with no directory yet becomes a row by being enrolled."""
    reply = (
        client(tmp_lup_project)
        .post("/api/profiles/ana/act", json={"slug": "enrol", "answer": "asked ana"})
        .json()
    )

    assert [row["name"] for row in reply["listing"]["profiles"]] == ["ana"]
    assert [e.note for e in read_roster(tmp_lup_project).entries] == ["asked ana"]


def test_the_page_refuses_an_act_the_state_does_not_offer(
    tmp_lup_project: Path,
) -> None:
    """What the browser was never shown must not be reachable by asking."""
    keep_account(tmp_lup_project, "ana")
    enrolled(tmp_lup_project, "ana")

    reply = (
        client(tmp_lup_project)
        .post(
            "/api/profiles/ana/act",
            json={"slug": "delete-profile", "answer": "ana"},
        )
        .json()
    )

    assert "cannot do that" in reply["message"]
    assert (profiles_root(tmp_lup_project) / "ana").is_dir()


def test_the_page_refuses_a_slug_that_names_nothing(tmp_lup_project: Path) -> None:
    enrolled(tmp_lup_project, "ana")
    reply = (
        client(tmp_lup_project)
        .post("/api/profiles/ana/act", json={"slug": "rm -rf", "answer": "ana"})
        .json()
    )

    assert "cannot do that" in reply["message"]
    assert read_roster(tmp_lup_project).names() == ["ana"]


def test_the_page_will_not_delete_on_a_mistyped_name(tmp_lup_project: Path) -> None:
    keep_account(tmp_lup_project, "ana")
    reply = (
        client(tmp_lup_project)
        .post(
            "/api/profiles/ana/act",
            json={"slug": "delete-profile", "answer": "not-ana"},
        )
        .json()
    )

    assert "nothing was deleted" in reply["message"]
    assert (profiles_root(tmp_lup_project) / "ana").is_dir()
