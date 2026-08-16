"""The independently replaceable resolver capability contracts."""

from abc import ABC, abstractmethod
from pathlib import Path

from lup.resolver.models import (
    ConcernProgress,
    MaterialQuestion,
    RecheckRuling,
    ResolvePhase,
    RunTally,
)


class ResolverAwaitingAnswers(Exception):
    """A run parked on material questions no door answered in time.

    The resolver raises this rather than guessing: it records the affected
    concerns as waiting and surfaces every pending question, so one
    flag-carrying rerun can answer the complete set.
    """

    def __init__(self, pending: list[MaterialQuestion], problems: list[str]) -> None:
        super().__init__(f"resolver run is awaiting {len(pending)} material answer(s)")
        self.pending = pending
        self.problems = problems


class ResolverEnvironmentFault(Exception):
    """A run stopped because its host failed, not because its work did.

    A revoked credential, an exhausted allowance or a dead network reaches
    every concern in flight at once and says nothing about any of them. So
    this travels the way a park does rather than the way a failure does: no
    concern is transitioned, no outcome is written, and the run keeps every
    status it held. Resuming re-enters each concern exactly where it was.

    Kept apart from `ResolverAwaitingAnswers` because the two ask different
    things of a human. A park wants an answer; this wants the host fixed,
    and then the same command again.
    """

    def __init__(self, cause: str, concerns: list[str]) -> None:
        super().__init__(f"resolver run stopped on an environmental fault: {cause}")
        self.cause = cause
        self.concerns = concerns


class ResolverDrained(Exception):
    """An operator asked a busy run to stop, and it reached a safe boundary.

    Park ends every open *wait*, which is a run sitting on an answer and no
    other. A worker inside a model turn waits on nothing, so a busy run was
    unaffected by it and the only way to end one was to kill it — which
    discards the uncommitted edits of every interrupted round, and the
    reviewer feedback and round counter with them.

    Draining stops at the junctions where stopping is free: the top of a
    round, after the previous one is committed, and the boundary between
    dependency batches. Nothing is failed and nothing is written off, so
    resuming costs only the turns that had not finished.
    """

    def __init__(self, reason: str, concerns: list[str]) -> None:
        super().__init__(f"resolver run drained at a safe boundary: {reason}")
        self.reason = reason
        self.concerns = concerns


class ResolverAssemblyDeferred(Exception):
    """A human declined, for now, to assemble the review branch.

    Not a failure and not a park: the gate was asked and answered, and the
    answer was "not yet". Every lease, branch and recorded outcome stays
    exactly as it is, so resuming re-asks with the same evidence rather than
    redoing any of the work behind it.
    """

    def __init__(self, verified: list[str], excluded: list[str]) -> None:
        super().__init__(
            f"assembly deferred with {len(verified)} concern(s) ready to merge"
        )
        self.verified = verified
        self.excluded = excluded


class ResolverRegression(Exception):
    """A human ruled that the merged tree broke a criterion that had held.

    The re-check asks whether a lost criterion was superseded by later work
    or is a regression, and the two answers mean opposite things about the
    review branch. "Superseded" settles it; this is the other one, and it
    says the assembled tree carries a defect that needs real work rather
    than a re-read.

    Raised rather than recorded, because recording it is exactly what the
    run used to do: the answer was written down, the concern stayed
    verified, and the branch was assembled and completed around it. A run
    that stops here keeps every lease and branch intact for the repair, and
    the next resume re-checks rather than trusting the old verdict.
    """

    def __init__(self, regressed: list["RecheckRuling"]) -> None:
        detail = "; ".join(
            f"{ruling.concern_id} lost {', '.join(ruling.criteria)}"
            for ruling in regressed
        )
        super().__init__(f"integration regressed verified criteria: {detail}")
        self.regressed = regressed


class ResolverObserver(ABC):
    """Receive every durably recorded resolver transition as it lands.

    The resolver emits only after the state repository has saved, so an
    observer never reports a transition that a crash could roll back.
    """

    @abstractmethod
    def phase_changed(self, phase: ResolvePhase) -> None:
        """One run-level phase was recorded."""

    @abstractmethod
    def concern_changed(self, progress: ConcernProgress) -> None:
        """One concern status or reason was recorded."""

    @abstractmethod
    def tally_changed(self, tally: RunTally) -> None:
        """The run's aggregate progress moved.

        Per-concern lines say what just happened; this says where the run
        stands — counts per status and joins landed — so a watcher stops
        reconstructing the aggregate from the scroll.
        """


class WorktreePreparer(ABC):
    """Make a freshly created leased worktree ready for execution.

    A bare ``git worktree add`` carries no dependency environment, so
    verification commands inside the lease would resolve against the source
    checkout. The application injects the same preparation its ordinary
    feature worktrees receive.
    """

    @abstractmethod
    def prepare(self, root: Path) -> None:
        """Prepare one worktree root after creation or restoration."""
