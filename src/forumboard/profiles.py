"""Who is synced, and the deliberate step that says so.

A directory under ``.lup/profiles`` means somebody has a Claude account
configured here. It does not mean their conversations should be read. Those
are different facts, and conflating them would make enrolment a side effect of
setting up an agent account — which is how somebody ends up harvested by
accident.

So enrolment is its own record, and it is committed rather than ignored: who
is being read is exactly the kind of thing that should be visible in a diff
and reviewable by the people it names. The roster carries when and by whose
account, because "who agreed to this, and when" is the question that gets
asked later.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# lup: ignore[constant-declaration] — this repository's own layout
ROSTER_FILENAME = "roster.json"


class Enrolment(BaseModel, frozen=True):
    """One profile that has opted into being synced."""

    profile: str
    enrolled_at: datetime
    note: str = ""
    """Free text — who arranged it, or what they agreed to."""


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
    """Where the roster lives — committed, not ignored."""
    return project_root / "config" / ROSTER_FILENAME


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
