"""Which runs still hold the branches they leased, and which have let go.

A lease is indistinguishable from abandoned work by every signal a branch
survey has: commits the integration branch lacks, and no pull request driving
them. So the survey asks the run directory instead, and a run that answers
holds its branch out of the sweep entirely.

The distinction these tests pin is which phases answer. Finishing releases a
lease because the join machinery carried the work out; failing and aborting do
not, and a sweep that treats them as release offers to land or drop branches
whose work nobody has salvaged — the two verbs that each destroy something.
"""

from pathlib import Path

import pytest
import sh

from lup.devtools.dev.branches import (
    disposition_for,
    runs_holding,
    unlanded_siblings,
)
from lup.harness.models import ResolveSpec, SkillInvocation
from lup.resolver.models import (
    AcceptanceCriterion,
    Concern,
    ConcernProgress,
    ConcernStatus,
    HeldLease,
    ResolvePhase,
    ResolveState,
    SourceSnapshot,
    WritableRootLease,
)
from lup.resolver.state import ResolverStateRepository, live_lease_branches

RUN_ID = "run-1"
BRANCH = "resolve/run-1/alpha"


def run_state(
    phase: ResolvePhase,
    *,
    active: bool = True,
    status: ConcernStatus = ConcernStatus.DISCOVERED,
) -> ResolveState:
    """One run holding one lease, at the phase and standing under test."""
    return ResolveState(
        config_digest="config-sha",
        run_id=RUN_ID,
        phase=phase,
        source=SourceSnapshot(branch="dev", commit="source-sha"),
        spec=ResolveSpec(
            id="resolve",
            worker_identity="resolver-worker",
            worker_skill=SkillInvocation(plugin="lup", skill="worker"),
            review_skill=SkillInvocation(plugin="lup", skill="review"),
            merge_skill=SkillInvocation(plugin="lup", skill="merge"),
        ),
        concerns=[
            Concern(
                id="alpha",
                title="Alpha",
                spec="Resolve alpha",
                criteria=[AcceptanceCriterion(id="alpha-done", description="done")],
            )
        ],
        progress=[ConcernProgress(concern_id="alpha", status=status)],
        leases=[
            WritableRootLease(
                concern_id="alpha",
                root=Path("/tmp/alpha"),
                branch=BRANCH,
                active=active,
            )
        ],
    )


def held(tmp_path: Path, state: ResolveState) -> dict[str, str]:
    """Persist one run and report the reason it gives per branch, if any."""
    ResolverStateRepository(tmp_path, RUN_ID).save(state)
    return {
        branch: lease.reason()
        for branch, lease in live_lease_branches(tmp_path).items()
    }


@pytest.mark.parametrize(
    "phase",
    [ResolvePhase.FAILED, ResolvePhase.ABORTED],
)
def test_a_run_that_died_still_holds_its_branches(
    tmp_path: Path, phase: ResolvePhase
) -> None:
    """Failing is not finishing, and its branches are the least salvaged."""
    assert BRANCH in held(tmp_path, run_state(phase))


def test_a_completed_run_still_reports_the_branch_it_left_behind(
    tmp_path: Path,
) -> None:
    """Completion releases the lease; it does not dispose of the branch.

    A run reaches this phase by finishing its own work, not by getting its
    batch onto the integration branch, and cleanup deactivates every lease
    whether or not it managed to delete the branch. So the branch that
    survives reads as loose work — commits the integration branch lacks and
    no pull request driving them — which is textbook LAND, and both verbs a
    sweep offers for that destroy or duplicate a batch that may already have
    landed under some other branch's pull request.
    """
    assert BRANCH in held(tmp_path, run_state(ResolvePhase.COMPLETE, active=False))


def test_a_completed_run_s_leftover_is_offered_a_reading_not_a_resume(
    tmp_path: Path,
) -> None:
    """Nothing restarts a run that finished, so the reason must not offer to."""
    reason = held(tmp_path, run_state(ResolvePhase.COMPLETE, active=False))[BRANCH]

    assert "resume" not in reason
    assert "--abort" not in reason
    assert f"resolve status --run-id {RUN_ID}" in reason


def test_a_completed_run_s_leftover_surveys_as_keep_rather_than_land(
    tmp_path: Path,
) -> None:
    """One decision about the run, not one per branch it happened to leave."""
    reasons = held(tmp_path, run_state(ResolvePhase.COMPLETE, active=False))
    verdict = disposition_for(
        BRANCH,
        integration="dev",
        current="dev-checkout",
        contained_in=[],
        pr=None,
        unique_commits=108,
        held=reasons[BRANCH],
    )

    assert verdict.status == "KEEP"


def test_a_completed_run_is_reported_as_not_alive(tmp_path: Path) -> None:
    """Nothing is coming back for these, so a sweep asks about the run first."""
    ResolverStateRepository(tmp_path, RUN_ID).save(
        run_state(ResolvePhase.COMPLETE, active=False)
    )
    assert [hold.alive for hold in runs_holding(live_lease_branches(tmp_path))] == [
        False
    ]


def test_a_working_run_holds_its_branches(tmp_path: Path) -> None:
    assert BRANCH in held(tmp_path, run_state(ResolvePhase.WORKERS))


def test_an_inactive_lease_is_never_held(tmp_path: Path) -> None:
    """Released mid-run: the branch is the sweep's business again."""
    assert held(tmp_path, run_state(ResolvePhase.FAILED, active=False)) == {}


def test_a_dead_run_s_hold_names_what_ends_it(tmp_path: Path) -> None:
    """`KEEP (failed)` on every sweep, forever, is the silent bucket itself.

    The hold is right and the branches must not be swept, but a reason that
    names no command leaves 23 branches reporting the same line with nothing
    in the workflow saying what to do about them.
    """
    reason = held(tmp_path, run_state(ResolvePhase.FAILED))[BRANCH]

    assert f"--run-id {RUN_ID}" in reason
    assert "--abort" in reason


def test_a_working_run_s_hold_names_no_command(tmp_path: Path) -> None:
    """A live run needs no instruction: it is working."""
    reason = held(tmp_path, run_state(ResolvePhase.WORKERS))[BRANCH]

    assert "--abort" not in reason
    assert "resume" not in reason


def test_a_dead_run_reports_its_phase_not_a_frozen_concern_status(
    tmp_path: Path,
) -> None:
    """The reason a human reads has to say the run needs salvaging.

    A per-concern status frozen at the moment the run died reads as though
    something is still working on it, which is the opposite of true.
    """
    reasons = held(
        tmp_path, run_state(ResolvePhase.FAILED, status=ConcernStatus.REVIEWING)
    )
    assert "failed" in reasons[BRANCH]
    assert "reviewing" not in reasons[BRANCH]


def test_a_working_run_reports_where_its_concern_had_got_to(tmp_path: Path) -> None:
    reasons = held(
        tmp_path, run_state(ResolvePhase.WORKERS, status=ConcernStatus.REVIEWING)
    )
    assert "reviewing" in reasons[BRANCH]


def test_a_dead_run_is_reported_as_not_alive(tmp_path: Path) -> None:
    """What a sweep reads to know nothing will retire these branches."""
    ResolverStateRepository(tmp_path, RUN_ID).save(run_state(ResolvePhase.FAILED))
    assert [hold.alive for hold in runs_holding(live_lease_branches(tmp_path))] == [
        False
    ]


def test_a_working_run_is_reported_as_alive(tmp_path: Path) -> None:
    ResolverStateRepository(tmp_path, RUN_ID).save(run_state(ResolvePhase.WORKERS))
    assert [hold.alive for hold in runs_holding(live_lease_branches(tmp_path))] == [
        True
    ]


def test_holds_group_under_the_run_answerable_for_them() -> None:
    """One entry per run, so a run-shaped sweep cannot present as loose branches."""
    holds = runs_holding(
        {
            "b": HeldLease(branch="b", run_id="two", standing="failed", alive=False),
            "a": HeldLease(branch="a", run_id="one", standing="workers"),
            "c": HeldLease(branch="c", run_id="one", standing="workers"),
        }
    )
    assert [(hold.run_id, hold.branches) for hold in holds] == [
        ("one", ["a", "c"]),
        ("two", ["b"]),
    ]


def test_no_run_holding_anything_is_no_entries() -> None:
    assert runs_holding({}) == []


def test_a_run_s_branches_stay_out_of_the_unlanded_advisory(
    tmp_lup_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The advisory reads the two signals that make a run's branch look loose.

    It runs inside every `dev check`, offline, so it sees neither the lease
    nor the pull request — just commits the integration branch lacks. A run's
    branches answer that description while the run is carrying them, and its
    leftovers answer it after it finished, so a batch of them prints the same
    line until the reader skips it. The run directory is local, which is the
    one thing this may read without going online for it.
    """
    repo = tmp_lup_project
    git = sh.Command("git").bake("-C", str(repo), _tty_out=False)
    git("init", "-q", "-b", "dev")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    for branch in (BRANCH, "loose-work"):
        git("checkout", "-q", "-b", branch, "dev")
        (repo / f"{branch.replace('/', '-')}.txt").write_text("work\n")
        git("add", ".")
        git("commit", "-q", "-m", f"work on {branch}")
    git("checkout", "-q", "dev")

    ResolverStateRepository(repo / ".lup" / "resolve", RUN_ID).save(
        run_state(ResolvePhase.COMPLETE, active=False)
    )
    monkeypatch.chdir(repo)

    assert [found.name for found in unlanded_siblings()] == ["loose-work"]


def test_a_held_branch_surveys_as_keep_rather_than_land(tmp_path: Path) -> None:
    """The whole point: the disposition a sweep would act on.

    Without the hold this branch is textbook LAND — commits the integration
    branch lacks and no PR driving them — and both verbs on offer destroy
    work the run never got to carry out.
    """
    reasons = held(tmp_path, run_state(ResolvePhase.FAILED))
    verdict = disposition_for(
        BRANCH,
        integration="dev",
        current="dev-checkout",
        contained_in=[],
        pr=None,
        unique_commits=7,
        held=reasons[BRANCH],
    )
    assert verdict.status == "KEEP"
    assert RUN_ID in verdict.reason
