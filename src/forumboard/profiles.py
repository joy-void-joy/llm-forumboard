"""Who is synced, and the deliberate step that says so.

A directory under ``.lup/profiles`` means somebody has a Claude account
configured here. It does not mean their conversations should be read. Those
are different facts, and conflating them would make enrolment a side effect of
setting up an agent account — which is how somebody ends up harvested by
accident.

So enrolment is its own record, and it is ignored rather than committed. The
roster carries when and by whose account, because "who agreed to this, and
when" is the question that gets asked later.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from forumboard.claudeai.browser import has_session

logger = logging.getLogger(__name__)

# lup: ignore[constant-declaration] — this repository's own layout
ROSTER_FILENAME = "roster.json"


class Enrolment(BaseModel, frozen=True):
    """One profile that has opted into being synced."""

    profile: str
    enrolled_at: datetime
    note: str = ""
    """Free text — who arranged it, or what they agreed to."""

    def with_note(self, note: str) -> "Enrolment":
        """This enrolment, saying something else about what was arranged.

        ``enrolled_at`` is deliberately untouched. Writing down more accurately
        what somebody agreed to is not them agreeing again, and the date is
        answering "when did they", which a correction does not change.
        """
        return self.model_copy(update={"note": note})


class Roster(BaseModel):
    """Every enrolled profile. A profile absent from here is never read."""

    entries: list[Enrolment] = []

    def names(self) -> list[str]:
        """Enrolled profile names, in enrolment order."""
        return [entry.profile for entry in self.entries]

    def holds(self, profile: str) -> bool:
        """Whether this profile is enrolled."""
        return any(entry.profile == profile for entry in self.entries)

    def with_profile(self, profile: str, note: str = "") -> "Roster":
        """This roster plus ``profile``, unchanged when it is already there."""
        if self.holds(profile):
            return self
        entry = Enrolment(
            profile=profile, enrolled_at=datetime.now(timezone.utc), note=note
        )
        return Roster(entries=[*self.entries, entry])

    def with_note(self, profile: str, note: str) -> "Roster":
        """This roster, with ``profile``'s note replaced.

        Unchanged for a profile it does not hold: a note is something an
        enrolment carries, so there is nowhere to put one for somebody who has
        not agreed, and quietly enrolling them to have somewhere would make a
        correction into consent.
        """
        return Roster(
            entries=[
                entry.with_note(note) if entry.profile == profile else entry
                for entry in self.entries
            ]
        )

    def without_profile(self, profile: str) -> "Roster":
        """This roster minus ``profile``.

        Removing somebody stops future reads. It deliberately does not touch
        what has already been published: unpublishing is a separate decision
        about material other people may have acted on, and it is not one to
        make silently as a side effect of ending a sync.
        """
        return Roster(entries=[e for e in self.entries if e.profile != profile])

    def write(self, project_root: Path) -> Path:
        """Record this roster, creating its directory on first enrolment."""
        path = roster_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.model_dump(mode="json"), indent=2) + "\n")
        return path


def roster_path(project_root: Path) -> Path:
    """Where the roster lives — gitignored, beside the profiles it names."""
    return project_root / ".lup" / "config" / ROSTER_FILENAME


def profiles_root(project_root: Path) -> Path:
    """Where profile directories live — gitignored, one per person.

    A profile holds two credentials that expire independently: the claude.ai
    browser session under ``claude-web``, and the Claude Code login the agent
    runs under. Same person, different authorisations.
    """
    return project_root / ".lup" / "profiles"


def profile_dir(profiles_root: Path, profile: str) -> Path:
    """One profile's directory, holding both of its credentials.

    :func:`forumboard.claudeai.browser.context_dir` names the ``claude-web``
    child of this, and the Claude Code login the agent runs under sits beside
    it. Anything addressing the person rather than one of their credentials —
    whether a directory is kept here at all, say — asks for this.
    """
    return profiles_root / profile


def read_roster(project_root: Path) -> Roster:
    """The roster, or an empty one where nobody has been enrolled yet."""
    path = roster_path(project_root)
    if not path.is_file():
        return Roster()
    try:
        return Roster.model_validate_json(path.read_text())
    except ValueError:
        logger.exception("the roster at %s is unreadable; treating it as empty", path)
        return Roster()


class ProfileState(BaseModel, frozen=True):
    """One profile as a listing shows it."""

    name: str
    enrolled: bool
    signed_in: bool
    kept: bool = False
    """Whether a directory is kept here for this name.

    Three facts rather than two, because the pair cannot tell an account kept
    here but never signed in from an enrolment with nothing on disk at all.
    Being signed in implies this; what needs it is deciding whether there is
    anything to delete, and how much of a person's credentials that would be.
    """

    def describe(self) -> str:
        """A line an operator can read at a glance."""
        match (self.enrolled, self.signed_in):
            case (True, True):
                return f"{self.name}: enrolled, signed in"
            case (True, False):
                return f"{self.name}: enrolled but NOT signed in — run `login`"
            case (False, True):
                return f"{self.name}: signed in, not enrolled — run `enrol`"
            case _:
                return f"{self.name}: not enrolled"

    def syncable(self) -> bool:
        """Whether a sync pass will read this profile."""
        return self.enrolled and self.signed_in


def profile_states(project_root: Path, profiles: Path) -> list[ProfileState]:
    """Every profile this checkout keeps, in name order.

    The listing the CLI, the enrolment page, and the setup wizard each show,
    derived once — so a profile cannot read as enrolled on one surface and not
    on another. Both roots are given rather than derived so a test can point
    the whole listing at a scratch workspace.

    Names come from the roster *and* from the directories, because the two
    disagree in both directions and each disagreement is worth seeing. A
    directory nobody enrolled is an account kept but not read; an enrolment
    with no directory is somebody who agreed and has not signed in yet, and
    listing only what is on disk would hide them entirely — which is the one
    case where a person believes they are being synced and is not.
    """
    roster = read_roster(project_root)
    folders = (
        [entry.name for entry in profiles.iterdir() if entry.is_dir()]
        if profiles.is_dir()
        else []
    )
    return [
        ProfileState(
            name=name,
            enrolled=roster.holds(name),
            signed_in=has_session(profiles, name),
            kept=name in folders,
        )
        for name in sorted({*folders, *roster.names()})
    ]
