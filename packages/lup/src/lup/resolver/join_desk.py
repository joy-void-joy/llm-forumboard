"""One join's files under a run directory, and the shapes written into them.

Separate from the verbs in :mod:`lup.resolver.join_tools` because the
readers are not the same set as the writers. The merger writes here through
the MCP tools; the status projection only reads, and answering "how far has
this join got" should not pull the tool layer, the orchestrator and an agent
transport in behind it.

The checkpoint is the reason the split earns its keep. It is written by
whoever landed the parent rather than by the orchestrator afterwards, so it
is current while a join turn is still running — which is exactly when
somebody is asking.
"""

from pathlib import Path

from pydantic import BaseModel

from lup.channels.models import utc_now
from lup.resolver.models import CarriedParent, VerificationCommand

JOIN_DIR = Path("join")
"""Where under a run directory the join plan and its progress are kept."""


class JoinTip(BaseModel, frozen=True):
    """One parent on the table, and what the merger needs to judge it."""

    commit: str
    concern_id: str
    summary: str = ""
    """What the concern behind this parent set out to do."""
    files: list[Path] = []
    """Every path this parent wrote, measured from where it forked.

    The whole point of handing the set over at once: overlap between two
    tips is visible here before either is merged, where the loop only ever
    revealed it by conflicting on the second.
    """


class JoinPlan(BaseModel, frozen=True):
    """Every parent one join has to land, written where a resume can read it.

    Persisted rather than passed, because the merger's session and the run
    process are separately mortal: a resumed run opens a session that
    remembers nothing, and the plan is what tells it which parents were on
    the table and which of them are already in.
    """

    concern_id: str
    worktree: Path
    base: str
    title: str
    purpose: str
    tips: list[JoinTip] = []
    carried: list[CarriedParent] = []
    """Parents already inside another, which land when their container does."""
    regeneration: list[str] = []
    verification: list[VerificationCommand] = []


class JoinLanding(BaseModel, frozen=True):
    """One parent as it actually landed, recorded by the verb that landed it."""

    commit: str
    head: str
    conflicted: bool = False
    rendered: bool = False
    """Whether regenerating the artifacts settled this join on its own."""
    merged: bool = True
    """Whether landing this parent performed a merge.

    A parent already contained in the tree is recorded without one, so a
    reader timing every landing would time merges that never ran: a resume
    sweeping the parents an earlier run landed records four of them inside
    twelve seconds, and a rate averaged over those describes no work the
    run ever did.
    """
    broke: list[str] = []
    at: str = ""


class JoinProgressRecord(BaseModel, frozen=True):
    """How far the merger has got, durable across its own session dying."""

    landings: list[JoinLanding] = []
    planned: int = 0

    @property
    def joined(self) -> list[str]:
        """The parents in the tree, in the order the merger put them there."""
        return [landing.commit for landing in self.landings]

    @property
    def commit(self) -> str:
        """The tree the last landing produced, which a resume restores to."""
        return self.landings[-1].head if self.landings else ""


class JoinDesk:
    """The run directory's view of one join, as files both transports read."""

    def __init__(self, run_dir: Path, subdirectory: Path = JOIN_DIR) -> None:
        self.root = run_dir / subdirectory

    def plan_path(self) -> Path:
        return self.root / "plan.json"

    def progress_path(self) -> Path:
        return self.root / "progress.json"

    def write_plan(self, plan: JoinPlan) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.plan_path().write_text(plan.model_dump_json(indent=2), encoding="utf-8")

    def plan(self) -> JoinPlan | None:
        path = self.plan_path()
        if not path.is_file():
            return None
        return JoinPlan.model_validate_json(path.read_text(encoding="utf-8"))

    def progress(self) -> JoinProgressRecord:
        path = self.progress_path()
        if not path.is_file():
            return JoinProgressRecord()
        return JoinProgressRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def record(self, landing: JoinLanding, planned: int) -> None:
        """Say where the sequence got to, after the tree it names exists.

        Written by whoever landed the parent rather than by the orchestrator
        afterwards, so the checkpoint survives the run process dying between
        two parents — which is the interruption this is for.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        before = self.progress().landings
        record = JoinProgressRecord(
            landings=[
                *before,
                landing.model_copy(update={"at": utc_now().isoformat()}),
            ],
            planned=planned,
        )
        self.progress_path().write_text(
            record.model_dump_json(indent=2), encoding="utf-8"
        )

    def clear(self) -> None:
        """Drop a finished join's plan and progress, so the next starts clean."""
        for path in (self.plan_path(), self.progress_path()):
            path.unlink(missing_ok=True)
