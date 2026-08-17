"""What can be done to one profile, declared once for every surface.

An operator meets the roster in three places — the setup wizard, the
``forumboard profile`` tree, and the served enrolment page — and each of them
wants the same short list of verbs against the same listing. Declaring that
list here rather than in each surface is what keeps a verb from existing in the
terminal and not in the browser, which is how the wizard came to offer only
enrolment while the page had buttons for both.

The acts are a union answering through its members, in the arrangement
:class:`lup.runtime.models.TurnBlock` uses: display is data each kind carries,
``perform`` is abstract so a kind that cannot do anything cannot be built, and
``applies`` declines by default so a kind that forgets to say who it is for is
offered to nobody. Omission is safe in the direction that matters — a new act
appears in no menu until it says whose, rather than appearing in all of them.

Nothing here prints. A surface asks which acts a profile's state offers, draws
them however it draws things, and hands back the one that was chosen.
"""

import logging
import shutil
from abc import abstractmethod
from pathlib import Path

from pydantic import BaseModel

from forumboard.claudeai.browser import context_dir, login_interactive
from forumboard.profiles import (
    ProfileState,
    profile_dir,
    profile_states,
    read_roster,
)

logger = logging.getLogger(__name__)


class ProfileTarget(BaseModel, frozen=True):
    """One profile, and the two roots every act needs to reach it.

    Both roots are carried rather than derived so a test can point a whole
    editing session at a scratch workspace, which is the same reason
    :func:`forumboard.profiles.profile_states` takes them.
    """

    project_root: Path
    profiles_root: Path
    name: str

    def session_dir(self) -> Path:
        """Where this profile's claude.ai session is stored."""
        return context_dir(self.profiles_root, self.name)

    def directory(self) -> Path:
        """This profile's whole directory — both credentials, not one."""
        return profile_dir(self.profiles_root, self.name)

    def state(self) -> ProfileState:
        """This profile as the listing shows it.

        Answers for a name the listing has never seen too, as all three facts
        false — which is exactly what somebody being added is, so a surface
        adding a person asks this rather than assembling a state of its own.
        """
        listed = profile_states(self.project_root, self.profiles_root)
        found = next((state for state in listed if state.name == self.name), None)
        return found or ProfileState(name=self.name, enrolled=False, signed_in=False)


class ActOutcome(BaseModel, frozen=True):
    """What an act did, in the words the surface that ran it will show.

    ``changed`` is separate from the message because a caller sometimes has to
    act on the difference: ``profile login`` exits non-zero when no session was
    captured, and a refused confirmation must not read as a deletion.
    """

    changed: bool
    message: str


class ProfileAct(BaseModel, frozen=True):
    """One thing an operator can do to one profile.

    A kind of act declares how it is named and what it costs, says whose state
    it is offered for, and does it. Everything a surface needs to draw the act
    is a field, so a menu row and a button are two renderings of one
    declaration rather than two lists that drift.
    """

    slug: str
    """Stable identity, so a URL or a stored answer can name this act."""

    label: str
    """The verb, as a menu row or a button shows it."""

    consequence: str
    """One line saying what it costs, shown beside the label rather than after."""

    asks: str = ""
    """What to ask for before running, where the act needs one value."""

    answer: str = ""
    """The value that was asked for.

    One field for the one value any act needs: the note for an enrolment, the
    typed-back name for a destructive one. No act needs both, which is why
    there is one field and not a shape per kind.
    """

    destructive: bool = False
    """Whether this deletes something, and so must have its name typed back."""

    def applies(self, state: ProfileState) -> bool:
        """Whether this act is offered for a profile in this state.

        Declines, so an act that does not answer is offered to nobody. That is
        the safe direction: a verb missing from a menu is noticed by whoever
        wanted it, while a verb offered against a state it cannot handle is an
        instruction the system will refuse.
        """
        return False

    @abstractmethod
    async def perform(self, target: ProfileTarget) -> ActOutcome:
        """Do it, and say what happened."""

    def confirmed(self, target: ProfileTarget) -> bool:
        """Whether a destructive act was confirmed by naming its profile.

        Checked here rather than by whichever surface drew the prompt, so the
        guard holds for every caller — including one that never drew anything.
        """
        return self.answer == target.name


class SignIn(ProfileAct, frozen=True):
    """Open a browser on this machine so somebody can sign in to claude.ai."""

    slug: str = "sign-in"
    label: str = "Sign in to claude.ai"
    consequence: str = (
        "opens a browser here; for somebody at another machine "
        "`forumboard serve` streams the same window to theirs"
    )

    def applies(self, state: ProfileState) -> bool:
        """Always: signing in again is how an expired session is replaced."""
        return True

    async def perform(self, target: ProfileTarget) -> ActOutcome:
        directory = target.session_dir()
        if await login_interactive(directory):
            return ActOutcome(
                changed=True, message=f"Signed in — session stored at {directory}"
            )
        return ActOutcome(changed=False, message="No session was captured.")


class Enrol(ProfileAct, frozen=True):
    """Record that somebody agreed to have their conversations read."""

    slug: str = "enrol"
    label: str = "Enrol for syncing"
    consequence: str = "recorded in .lup/config/roster.json, which is gitignored"
    asks: str = "Who arranged this, or what was agreed"

    def applies(self, state: ProfileState) -> bool:
        return not state.enrolled

    async def perform(self, target: ProfileTarget) -> ActOutcome:
        roster = read_roster(target.project_root)
        if roster.holds(target.name):
            return ActOutcome(
                changed=False, message=f"{target.name} is already enrolled."
            )
        recorded = roster.with_profile(target.name, self.answer).write(
            target.project_root
        )
        return ActOutcome(
            changed=True, message=f"Enrolled {target.name} — recorded in {recorded}"
        )


class Withdraw(ProfileAct, frozen=True):
    """Stop reading somebody, without touching what was already published."""

    slug: str = "withdraw"
    label: str = "Withdraw from syncing"
    consequence: str = "future passes stop reading them; published pages stay up"

    def applies(self, state: ProfileState) -> bool:
        return state.enrolled

    async def perform(self, target: ProfileTarget) -> ActOutcome:
        roster = read_roster(target.project_root)
        if not roster.holds(target.name):
            return ActOutcome(changed=False, message=f"{target.name} is not enrolled.")
        roster.without_profile(target.name).write(target.project_root)
        return ActOutcome(
            changed=True,
            message=(f"Withdrew {target.name}. Nothing already published was changed."),
        )


class SetNote(ProfileAct, frozen=True):
    """Say something else about what somebody agreed to."""

    slug: str = "set-note"
    label: str = "Edit the note"
    consequence: str = "changes what was arranged; the enrolment date stays"
    asks: str = "Who arranged this, or what was agreed"

    def applies(self, state: ProfileState) -> bool:
        return state.enrolled

    async def perform(self, target: ProfileTarget) -> ActOutcome:
        roster = read_roster(target.project_root)
        if not roster.holds(target.name):
            return ActOutcome(changed=False, message=f"{target.name} is not enrolled.")
        recorded = roster.with_note(target.name, self.answer).write(target.project_root)
        return ActOutcome(
            changed=True, message=f"Noted against {target.name} in {recorded}"
        )


class ForgetSession(ProfileAct, frozen=True):
    """Drop a stored claude.ai session, keeping everything else about them."""

    slug: str = "forget-session"
    label: str = "Forget the claude.ai session"
    consequence: str = (
        "deletes the stored cookies only; the account this machine keeps "
        "for the agent is untouched, and signing in again restores the sync"
    )
    asks: str = "Type the profile name to confirm"
    destructive: bool = True

    def applies(self, state: ProfileState) -> bool:
        return state.signed_in

    async def perform(self, target: ProfileTarget) -> ActOutcome:
        if not self.confirmed(target):
            return ActOutcome(
                changed=False,
                message=f"That is not {target.name} — no session was deleted.",
            )
        directory = target.session_dir()
        if not directory.is_dir():
            return ActOutcome(
                changed=False, message=f"{target.name} has no stored session."
            )
        shutil.rmtree(directory)
        logger.info("Deleted the stored claude.ai session for %s", target.name)
        return ActOutcome(
            changed=True,
            message=(
                f"Forgot {target.name}'s claude.ai session. Sign in again to resume."
            ),
        )


class DeleteProfile(ProfileAct, frozen=True):
    """Remove a profile's whole directory, so the listing stops naming it.

    Offered only once somebody has been withdrawn. The directory holds two
    credentials — the claude.ai session and the Claude Code login the agent
    itself runs under — so deleting it is more than ending a sync, and doing
    that as a side effect of leaving the roster would be exactly the kind of
    conflation the roster exists to prevent. Withdraw first, then delete: two
    steps, both of which somebody chose.
    """

    slug: str = "delete-profile"
    label: str = "Delete the profile directory"
    consequence: str = (
        "deletes the whole directory, so this also signs the account out of "
        "the agent harness; the profile leaves the listing entirely"
    )
    asks: str = "Type the profile name to confirm"
    destructive: bool = True

    def applies(self, state: ProfileState) -> bool:
        return state.kept and not state.enrolled

    async def perform(self, target: ProfileTarget) -> ActOutcome:
        if not self.confirmed(target):
            return ActOutcome(
                changed=False,
                message=f"That is not {target.name} — nothing was deleted.",
            )
        directory = target.directory()
        if not directory.is_dir():
            return ActOutcome(
                changed=False, message=f"Nothing is kept here for {target.name}."
            )
        shutil.rmtree(directory)
        logger.info("Deleted the profile directory for %s", target.name)
        return ActOutcome(
            changed=True,
            message=f"Deleted {directory}. {target.name} is gone from here.",
        )


PROFILE_ACTS: list[ProfileAct] = [
    SignIn(),
    Enrol(),
    SetNote(),
    Withdraw(),
    ForgetSession(),
    DeleteProfile(),
]
"""Every act, in the order a menu should offer them.

Ordered by how much each one costs — signing in and enrolling first, deleting
last — because that is the order somebody reads a list of verbs in, and a
destructive one sitting where a routine one was expected gets chosen by
muscle memory.
"""


class ProfileActs(BaseModel, frozen=True):
    """The acts a surface offers, and how it finds one.

    What a renderer holds. The roster of acts is a field so a caller can offer
    a different set — a page that never deletes anything, say — without
    editing this module.
    """

    acts: list[ProfileAct] = PROFILE_ACTS

    def offered(self, state: ProfileState) -> list[ProfileAct]:
        """Every act that applies to a profile in this state, in order."""
        return [act for act in self.acts if act.applies(state)]

    def named(self, slug: str) -> ProfileAct | None:
        """The act a stored answer names, or None where it names nothing."""
        return next((act for act in self.acts if act.slug == slug), None)
