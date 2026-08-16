"""What "everything left to implement" is, and where a written one lives.

Declaration only. The command surface computes one of these from the scans
that already answer each topic, and the report skill writes one down after a
session — so the two halves share a shape instead of each describing
outstanding work in its own words, which is the difference between one report
mechanism and two that never met.

Nothing here scans, renders a CLI, or imports one. That is what lets the
skill's own prose recite the topic roster from the same declaration the
command fills in, the way the sub-app roster is recited rather than restated.
"""

from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

DEFAULT_REPORT_PATH = Path("tmp/report.md")
"""Where a written report goes unless a composition says otherwise.

Scratch, so it is gitignored and never reaches a diff, a reviewer, or a
commit — which is the whole reason a report may be written at all. A backlog
file parks work where no workflow will surface it again; this is rewritten
whole from the surfaces every time, so a stale one cannot outlive what it
describes. A default rather than a rule: a project keeping its scratch
somewhere else passes its own.
"""


class ReportTopic(BaseModel, frozen=True):
    """One question a report answers, and what a reader does about it."""

    title: str
    guidance: str


NOTES = ReportTopic(
    title="Open notes",
    guidance="Review feedback still asking for something, at the site it concerns.",
)
DEFERRALS = ReportTopic(
    title="Deferred work",
    guidance="Work parked at its own site, with the gate that would wake it.",
)
CLAIMS = ReportTopic(
    title="Unverified claims",
    guidance="Notes claimed solved, awaiting the pass that retires one.",
)
DRIFT = ReportTopic(
    title="Stale artifacts",
    guidance="Generated trees the typed source has moved out from under.",
)
UNLANDED = ReportTopic(
    title="Unlanded branches",
    guidance="Work committed where the integration branch has not taken it.",
)
LEASES = ReportTopic(
    title="Resolver leases",
    guidance="Concerns a run is holding, which nothing else should pick up.",
)

REPORT_TOPICS = (NOTES, DEFERRALS, CLAIMS, DRIFT, UNLANDED, LEASES)
"""Every topic this report accounts for, in the order it reads them."""


class ReportItem(BaseModel, frozen=True):
    """One outstanding thing: where it is, what it says, and any gate on it.

    One shape for every topic rather than one per surface, because a report
    is read rather than dispatched on, and each surface keeps its own command
    for the detail this leaves out.
    """

    where: str
    what: str
    gate: str = ""

    def line(self) -> str:
        """This item as one Markdown bullet's worth of text."""
        gate = f" [{self.gate}]" if self.gate else ""
        return f"`{self.where}`{gate} — {self.what}"


class ReportPart(BaseModel, frozen=True):
    """One topic together with what the surfaces found for it."""

    topic: ReportTopic
    items: list[ReportItem]

    def markdown(self) -> str:
        """This topic as its own section, empty or not.

        An empty topic still prints, because "nothing outstanding here" is an
        answer and a section that vanishes reads as one nobody looked at.
        """
        heading = (
            f"## {self.topic.title} ({len(self.items)})\n\n{self.topic.guidance}\n"
        )
        if not self.items:
            return f"{heading}\nNothing outstanding.\n"
        return heading + "\n" + "".join(f"- {item.line()}\n" for item in self.items)


class Report(BaseModel, frozen=True):
    """Everything there still is to implement, as the surfaces can see it."""

    parts: list[ReportPart]

    def outstanding(self) -> int:
        """How many items stand open across every topic."""
        return sum(len(part.items) for part in self.parts)

    def markdown(self) -> str:
        """The whole report, as a written one holds it."""
        header = (
            "# What is left to implement\n\n"
            f"{self.outstanding()} outstanding item(s) across "
            f"{len(self.parts)} topic(s).\n\n"
        )
        return header + "\n".join(part.markdown() for part in self.parts)


def topic_bullets(topics: Sequence[ReportTopic] = REPORT_TOPICS) -> str:
    """Format a topic roster as Markdown bullet lines with their guidance."""
    return "".join(f"- **{topic.title}** — {topic.guidance}\n" for topic in topics)
