"""Turning a name somebody said into a person Notion can notify.

Somebody writes "remind me to ping Jacob about the migration" and the editor
reports the name it heard. Only the workspace knows who Jacob is, and it may
know two — so resolution happens here, against the live directory, rather than
from a mapping of profiles to accounts that would go stale and would not have
covered Jacob anyway (he need never have used claude.ai).

Ambiguity drops the ping rather than guessing between two people. A ping that
reaches the wrong person is worse than one that does not arrive: the wrong
person is now holding somebody else's context, and the right person is still
waiting. Every drop is reported, so an operator can see what went unresolved
instead of wondering why nobody was tagged.
"""

from pydantic import BaseModel

from lup.types import JsonObject

from forumboard.notion.client import NotionPerson


class UnresolvedName(BaseModel, frozen=True):
    """A name that reached no single person, and what stopped it."""

    spoken: str
    reason: str


class NameOutcome(BaseModel, frozen=True):
    """What resolving one spoken name produced — exactly one of the two."""

    person: NotionPerson | None = None
    missed: UnresolvedName | None = None


class ResolvedPeople(BaseModel, frozen=True):
    """The outcome of resolving every name one pass reported."""

    matched: list[NotionPerson] = []
    unresolved: list[UnresolvedName] = []

    def mentions(self) -> list[JsonObject]:
        """The matched people as people-property entries."""
        return [person.mention() for person in self.matched]

    def describe(self) -> str:
        """One line for the log, naming what did not resolve."""
        if not self.unresolved:
            return f"{len(self.matched)} person(s) tagged"
        missed = ", ".join(entry.spoken for entry in self.unresolved)
        return f"{len(self.matched)} person(s) tagged; no match for {missed}"


class PeopleDirectory(BaseModel, frozen=True):
    """The workspace's members, and the name matching this project does.

    Built once per pass and passed down, so a conversation mentioning six
    people costs one listing rather than six.
    """

    members: list[NotionPerson] = []

    def resolve(self, spoken_names: list[str]) -> ResolvedPeople:
        """Every spoken name matched to a member, or recorded as unresolved.

        Matched people are keyed by id on the way out, so a conversation that
        names somebody twice tags them once.
        """
        outcomes = [self.outcome_for(spoken) for spoken in spoken_names]
        by_id = {
            outcome.person.id: outcome.person
            for outcome in outcomes
            if outcome.person is not None
        }
        return ResolvedPeople(
            matched=list(by_id.values()),
            unresolved=[
                outcome.missed for outcome in outcomes if outcome.missed is not None
            ],
        )

    def outcome_for(self, spoken: str) -> NameOutcome:
        """One spoken name resolved, or explained."""
        match self.candidates(spoken):
            case [only]:
                return NameOutcome(person=only)
            case []:
                return NameOutcome(
                    missed=UnresolvedName(
                        spoken=spoken, reason="nobody in the workspace matches"
                    )
                )
            case several:
                names = ", ".join(person.name for person in several)
                return NameOutcome(
                    missed=UnresolvedName(
                        spoken=spoken,
                        reason=f"could be any of {names}, so nobody was tagged",
                    )
                )

    def candidates(self, spoken: str) -> list[NotionPerson]:
        """Members a spoken name could mean, exact match winning outright.

        An exact match ends the question even when somebody else's name starts
        the same way: a workspace holding both "Jacob" and "Jacob Miller" reads
        "Jacob" as the person actually called that.
        """
        wanted = spoken.casefold()
        exact = [person for person in self.members if person.name.casefold() == wanted]
        if exact:
            return exact
        return [
            person
            for person in self.members
            if person.name.casefold().startswith(wanted)
        ]
