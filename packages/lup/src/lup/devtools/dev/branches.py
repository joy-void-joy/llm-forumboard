"""Branch analysis: containment, PR status, base detection, freshness, PR bodies."""

import json
import logging
from collections import defaultdict
from collections.abc import Set as AbstractSet
from itertools import groupby
from operator import attrgetter
from pathlib import Path
from typing import Literal, NoReturn, Required, TypedDict

import sh
import typer
from pydantic import BaseModel, Field

from lup.harness.environment import non_interactive_environment
from lup.harness.process import LaunchRequest, ProcessLauncher
from lup.devtools.dev.remote_auth import check_remote_auth
from lup.resolver.models import HeldLease
from lup.resolver.state import live_lease_branches
from lup.types import StringMap
from lup.workspace.paths import project_root
from lup.devtools.utils import (
    format_table,
    git,
    gh,
    config_lock_diagnosis,
    decode_stderr,
    output_json,
    repository_arguments,
    short_sha,
)

logger = logging.getLogger(__name__)


class PRStatus(BaseModel):
    """One PR row as `gh pr list --json` returns it (aliases are gh's names)."""

    number: int
    title: str = ""
    state: str = ""
    merged_at: str | None = Field(default=None, alias="mergedAt")
    head_ref: str = Field(default="", alias="headRefName")
    url: str = ""


type BranchStatus = Literal["LAND", "DELETE", "STALE", "KEEP", "CURRENT", "NOT_FOUND"]


class Disposition(BaseModel):
    """The one verb a branch resolves to, and the reason it got it."""

    status: BranchStatus
    reason: str


class WorktreeChanges(BaseModel):
    """What a worktree holds that removing it would discard."""

    modified: int = 0
    untracked: int = 0

    def dirty(self) -> bool:
        return bool(self.modified or self.untracked)

    def summary(self) -> str:
        return f"{self.modified} modified, {self.untracked} untracked"

    def compact(self) -> str:
        """The same count in a table cell, where a sentence would not fit."""
        if not self.dirty():
            return "-"
        return " ".join(
            [
                *([f"{self.modified}M"] if self.modified else []),
                *([f"{self.untracked}U"] if self.untracked else []),
            ]
        )


class BranchInfo(BaseModel):
    name: str
    commit: str
    tracking: str | None
    worktree: str | None
    is_current: bool
    contained_in: list[str]
    pr: PRStatus | None
    unique_commits: int
    source_diff_lines: int
    disposition: BranchStatus
    reason: str
    changes: WorktreeChanges | None = None
    """What this branch's worktree holds uncommitted, where it has one.

    Carried beside the disposition rather than folded into its reason: the
    classifier is shared with `dev status`, and a verb decided partly on
    working-tree state there would drift from the same verb decided here.
    What this changes is the cost of acting, not the action — a DELETE whose
    worktree is dirty still deletes, but refuses until forced, and a reader
    handed the disposition alone plans a step that will stop.
    """


class RunHold(BaseModel):
    """One resolver run and the branches it is still holding out of the sweep.

    A sweep reads this before it reads the branch list. A run that is still
    working answers for its own branches, so they are somebody's business
    already; a run that died answers for nothing, and the branches it holds
    are work with no owner and no verb — the one shape a sweep exists to
    surface and cannot express as a per-branch disposition.
    """

    run_id: str
    alive: bool
    branches: list[str]


class SurveyResult(BaseModel):
    integration_branch: str
    current_branch: str
    branches: list[BranchInfo]
    runs: list[RunHold] = []


def runs_holding(leased: dict[str, HeldLease]) -> list[RunHold]:
    """Group every held branch under the run answerable for it."""
    ordered = sorted(leased.values(), key=attrgetter("run_id"))
    return [
        RunHold(
            run_id=run_id,
            alive=held[0].alive,
            branches=sorted(lease.branch for lease in held),
        )
        for run_id, group in groupby(ordered, key=attrgetter("run_id"))
        if (held := list(group))
    ]


class BranchClassification(TypedDict, total=False):
    branch: Required[str]
    status: Required[BranchStatus]
    reason: Required[str]
    worktree: str | None
    pr: str | int
    pr_url: str


class ParsedBranch(TypedDict):
    """One local branch row from ``git for-each-ref``."""

    name: str
    commit: str
    tracking: str | None
    is_current: bool


def parse_branches() -> list[ParsedBranch]:
    """Structured local-branch rows via ``git for-each-ref``.

    NUL-separated ``--format`` fields are the machine interface — no marker
    columns or bracket surgery; ``%(HEAD)`` is ``*`` exactly for the branch
    checked out in the current worktree.
    """

    def parse(row: str) -> ParsedBranch:
        name, commit, upstream, head = row.split(  # lup: ignore[string-split] — NUL
            "\x00"
        )
        return {
            "name": name,
            "commit": commit,
            "tracking": upstream or None,
            "is_current": head == "*",
        }

    return [
        parse(row)
        for row in git.lines(
            "for-each-ref",
            "refs/heads",
            "--format=%(refname:short)%00%(objectname:short)"
            "%00%(upstream:short)%00%(HEAD)",
        )
    ]


def parse_worktrees() -> dict[str, str]:  # lup: ignore[dict-str-payload]
    """Map branch name -> worktree path from ``git worktree list --porcelain``.

    Open, data-driven keys (whatever branches have worktrees), folded from
    porcelain records that span multiple lines — the one stateful walk here.
    """
    mapping: dict[str, str] = {}  # lup: ignore[dict-str-payload, empty-collection]
    current_path = ""

    for line in git.lines("worktree", "list", "--porcelain"):
        if line.startswith("worktree "):
            current_path = line.removeprefix("worktree ")
        elif line.startswith("branch refs/heads/"):
            mapping[line.removeprefix("branch refs/heads/")] = current_path

    return mapping


def build_containment(branch_names: list[str]) -> dict[str, list[str]]:
    """For each branch, find which other branches contain it."""
    containment: dict[str, list[str]] = {b: [] for b in branch_names}

    for branch in branch_names:
        for target in branch_names:
            if branch == target:
                continue
            try:
                git("merge-base", "--is-ancestor", branch, target)
                containment[branch].append(target)
            except sh.ErrorReturnCode:
                pass

    return containment


def fetch_pr_status(branch_names: list[str]) -> dict[str, PRStatus]:
    """Query GitHub for PR status of branches."""
    try:
        rows = json.loads(
            gh.out(
                "pr",
                "list",
                *repository_arguments(),
                "--state",
                "all",
                "--limit",
                "200",
                "--json",
                "number,title,headRefName,state,mergedAt",
            )
        )
    except sh.ErrorReturnCode as e:
        logger.warning("Failed to fetch PR status: %s", decode_stderr(e))
        return {}
    prs = [PRStatus.model_validate(row) for row in rows]
    return {pr.head_ref: pr for pr in prs if pr.head_ref in branch_names}


def count_unique_commits(branch: str, integration: str) -> int:
    """Count commits on branch not cherry-picked into integration (-1: unknown)."""
    try:
        return int(
            git.out(
                "rev-list",
                "--count",
                "--cherry-pick",
                "--left-only",
                f"{branch}...{integration}",
                _ok_code=[0],
            )
        )
    except (sh.ErrorReturnCode, ValueError):
        return -1


def count_source_diff_lines(branch: str, integration: str) -> int:
    """Count lines of source-file diff between branch and integration (-1: unknown).

    ``--numstat`` is the machine format: one ``added<TAB>deleted<TAB>path`` row
    per file (``-`` for binary files, which count no lines).
    """
    try:
        rows = git.lines(
            "diff",
            "--numstat",
            branch,
            integration,
            "--",
            "src/",
            ".claude/",
            "tests/",
            _ok_code=[0, 1],
        )
    except sh.ErrorReturnCode:
        return -1
    total = 0
    for row in rows:
        added, _, rest = row.partition("\t")  # lup: ignore[string-split] — numstat
        deleted = rest.partition("\t")[0]  # lup: ignore[string-split] — numstat
        total += int(added) if added.isdigit() else 0
        total += int(deleted) if deleted.isdigit() else 0
    return total


def get_integration_branch() -> str:
    """Return 'dev' if it exists locally, else 'main'."""
    from lup.devtools.dev.worktree import branch_exists

    if branch_exists("dev"):
        return "dev"
    return "main"


def is_ancestor(ancestor: str, descendant: str) -> bool:
    """Check if ancestor is an ancestor of descendant."""
    try:
        git("merge-base", "--is-ancestor", ancestor, descendant)
        return True
    except sh.ErrorReturnCode:
        return False


def get_branch_worktree(branch: str) -> str | None:
    """Return the worktree path for a branch, or None."""
    return parse_worktrees().get(branch)  # lup: ignore[dict-get] — open branch map


def get_pr_info(branch: str) -> PRStatus | None:
    """Get PR info for a branch via gh CLI. None when there is none (or no gh).

    The repository is named rather than inferred. `gh` reads the origin
    remote to decide which repository it is talking about, and a remote
    written through an SSH alias names no host it recognizes — so every
    query failed, and this returned the same ``None`` it returns for a
    branch that genuinely has no pull request. A branch with an open PR
    then classified ``LAND``.
    """
    try:
        items = json.loads(
            gh.out(
                "pr",
                "list",
                *repository_arguments(),
                "--state=all",
                f"--head={branch}",
                "--json=number,title,state,mergedAt,url",
                "--limit=1",
                _ok_code=[0],
            )
        )
    except (sh.ErrorReturnCode, sh.CommandNotFound, json.JSONDecodeError) as error:
        logger.warning("no pull-request status for %s: %s", branch, error)
        return None
    return PRStatus.model_validate(items[0]) if items else None


PROTECTED_BRANCHES = {"main", "master", "dev", "develop"}
"""Branches no workflow here offers to delete, unless a project says otherwise.

The four names cover the two-tier and single-tier conventions in common use;
a project whose trunk is called something else passes its own set rather than
forking the functions that consult this one.
"""


# lup: ignore[model-free-function] — subject is the branch; the PR is one signal
def disposition_for(
    name: str,
    *,
    integration: str,
    current: str,
    contained_in: list[str],
    pr: PRStatus | None,
    unique_commits: int,
    protected: AbstractSet[str] = PROTECTED_BRANCHES,
    held: str = "",
) -> Disposition:
    """Resolve a branch to its single disposition.

    Every branch resolves to exactly one verb, so unlanded work has no silent
    bucket to sit in: a branch holding commits the integration branch lacks,
    with no open PR driving it, is ``LAND`` rather than ``KEEP``. Containment
    counts as landed only against the integration branch — sitting inside a
    sibling that has not landed either is no reason to drop work.

    ``held`` is why something else is already answerable for this branch, and
    outranks every disposition but the current and protected ones. A resolver
    lease is the case: it reads as abandoned work by every other signal here,
    and both verbs a sweep would offer for it destroy something.
    """
    if name == current:
        return Disposition(status="CURRENT", reason="current branch")
    if name in protected:
        return Disposition(status="KEEP", reason="protected branch")
    if held:
        return Disposition(status="KEEP", reason=held)
    if integration in contained_in:
        return Disposition(status="DELETE", reason=f"merged into {integration}")
    if pr is not None and pr.state == "MERGED":
        return Disposition(status="DELETE", reason=f"PR #{pr.number} merged")
    if unique_commits == 0:
        return Disposition(
            status="STALE", reason=f"all commits cherry-picked into {integration}"
        )
    if pr is not None and pr.state == "OPEN":
        return Disposition(
            status="KEEP",
            reason=f"PR #{pr.number} open, {unique_commits} unique commits",
        )

    carried = [b for b in contained_in if b != integration]
    also = f"; also carried by {', '.join(carried)}" if carried else ""
    return Disposition(
        status="LAND", reason=f"{unique_commits} unique commits, no PR{also}"
    )


def classify_branch(
    branch: str,
    integration: str,
    current: str,
    *,
    has_remote: bool = True,
    protected: AbstractSet[str] = PROTECTED_BRANCHES,
) -> BranchClassification:
    """Classify a branch as DELETE/STALE/KEEP/CURRENT/NOT_FOUND with reason."""
    from lup.devtools.dev.worktree import branch_exists

    if not branch_exists(branch):
        return {
            "branch": branch,
            "status": "NOT_FOUND",
            "reason": "no such local branch",
        }

    if branch == current or branch in protected:
        guard = disposition_for(
            branch,
            integration=integration,
            current=current,
            contained_in=[],
            pr=None,
            unique_commits=0,
        )
        return {"branch": branch, "status": guard.status, "reason": guard.reason}

    merged_into_integration = is_ancestor(branch, integration)
    worktree = get_branch_worktree(branch)
    pr = get_pr_info(branch) if has_remote else None
    pr_number: str | int = pr.number if pr else ""
    pr_url = pr.url if pr else ""

    counted = count_unique_commits(branch, integration)
    verdict = disposition_for(
        branch,
        integration=integration,
        current=current,
        contained_in=[integration] if merged_into_integration else [],
        pr=pr,
        unique_commits=counted if counted >= 0 else 999,
    )

    return {
        "branch": branch,
        "status": verdict.status,
        "reason": verdict.reason,
        "worktree": worktree,
        "pr": pr_number,
        "pr_url": pr_url,
    }


class UnlandedBranch(BaseModel):
    """A branch holding commits the integration branch does not have."""

    name: str
    unique_commits: int
    source_diff_lines: int
    worktree: str | None


def unlanded_siblings(
    protected: AbstractSet[str] = PROTECTED_BRANCHES,
) -> list[UnlandedBranch]:
    """Local-only scan for branches holding work the integration branch lacks.

    Deliberately offline — no fetch, no PR query — because this runs inside
    every ``dev check``. An open PR driving a branch is therefore invisible
    here, which is why the result is advisory: it reports what has not
    reached integration, and the full sweep decides what to do about it.

    The current branch is excluded: work in hand is not work parked out of
    sight, and reporting it every run would train the reader to skip the line.

    So is any branch a resolver run answers for. A run holds its branches
    out of the sweep deliberately, and its leftovers are one decision about
    the run rather than a list of parked work; a batch of them here is the
    same line repeated until the reader skips it, and reads as a backlog
    nobody is carrying when a run either is carrying it or has finished.
    Reading the run directory keeps this offline, which the rest of it is.
    """
    integration = get_integration_branch()
    worktrees = parse_worktrees()
    current = git.out("branch", "--show-current")
    leased = live_lease_branches(project_root() / ".lup" / "resolve")

    def measure(name: str) -> UnlandedBranch | None:
        if name == current or name in protected or name in leased:
            return None
        if is_ancestor(name, integration):
            return None
        unique = count_unique_commits(name, integration)
        if unique <= 0:
            return None
        return UnlandedBranch(
            name=name,
            unique_commits=unique,
            source_diff_lines=count_source_diff_lines(name, integration),
            worktree=worktrees.get(name),  # lup: ignore[dict-get] — open map
        )

    return [
        found
        for name in git.lines("branch", "--format=%(refname:short)")
        if (found := measure(name)) is not None
    ]


class BaseCandidate(BaseModel):
    """A candidate base branch, measured against the branch under test.

    ``source`` states whether the name came from the base recorded at
    worktree creation or from topological guessing.
    """

    name: str
    distance: int
    merge_base: str
    is_ancestor: bool
    source: Literal["recorded", "guessed"] = "guessed"


def base_config_key(branch: str) -> str:
    """Where the base a worktree was cut from is recorded, for one branch.

    Written at creation and read wherever a branch's origin has to be
    recovered, so the key is named once instead of spelled at each end.
    """
    return f"branch.{branch}.lup-base"


def recorded_base(branch: str) -> str | None:
    """The base recorded at worktree creation, when one was written."""
    try:
        value = git.out("config", "--get", base_config_key(branch), _ok_code=[0])
    except sh.ErrorReturnCode:
        return None
    return value or None


def detect_base_branch(branch: str | None = None) -> BaseCandidate:
    """Detect the base branch for the given (or current) branch.

    A base recorded at worktree creation (``branch.<name>.lup-base``) wins
    outright — topology cannot recover the creation point once the parent
    has merged on. Without a record, prefers ancestor branches (the natural
    parent in a two-tier model) over siblings. Among ancestors, picks the
    one with the fewest commits ahead (``distance``). Falls back to
    non-ancestors when no ancestor exists.
    """
    effective = branch or git.out("branch", "--show-current")

    local_branches = [
        b for b in git.lines("branch", "--format=%(refname:short)") if b != effective
    ]

    if not local_branches:
        typer.echo("No other local branches to compare against.", err=True)
        raise typer.Exit(1)

    def measure(candidate: str) -> BaseCandidate | None:
        try:
            merge_base = git.out("merge-base", effective, candidate, _ok_code=[0])
            distance = int(git.out("rev-list", "--count", f"{merge_base}..{effective}"))
        except sh.ErrorReturnCode:
            return None
        return BaseCandidate(
            name=candidate,
            distance=distance,
            merge_base=merge_base,
            is_ancestor=is_ancestor(candidate, effective),
        )

    recorded = recorded_base(effective)
    if recorded is not None and recorded in local_branches:
        pinned = measure(recorded)
        if pinned is not None:
            return pinned.model_copy(update={"source": "recorded"})

    measured = [m for c in local_branches if (m := measure(c)) is not None]
    ancestors = [m for m in measured if m.is_ancestor]

    # Prefer ancestors — they are the natural base. With none, every measured
    # candidate is a non-ancestor, so `measured` IS the sibling fallback.
    candidates = ancestors or measured

    if not candidates:
        typer.echo("Could not determine base branch.", err=True)
        raise typer.Exit(1)

    ranked = sorted(candidates, key=lambda c: c.distance)
    best = ranked[0]

    tied = [c for c in ranked[1:] if c.distance == best.distance]
    if tied:
        typer.echo("Ambiguous base branch. Candidates:", err=True)
        for c in [best, *tied]:
            typer.echo(f"  {c.name} ({c.distance} commits ahead)", err=True)
        raise typer.Exit(1)

    return best


class BaseFreshness(BaseModel, frozen=True):
    """How far a checkout sits behind the remote branch its base answers to."""

    tracked: str = ""
    """The remote branch measured against; empty when there is none to measure."""

    behind: int = 0
    """Commits that branch holds which this checkout does not."""

    unreachable: str = ""
    """Why the remote could not be asked; empty when it answered."""

    update_command: str = "git pull --ff-only"
    """What a reader runs to take the commits they are missing."""

    def stale(self) -> bool:
        """Whether the remote is known to hold commits this checkout does not."""
        return self.behind > 0

    def report(self) -> str:
        """One line naming what the probe found, in whichever case it found it.

        An unknown answer says so rather than reading as a clean bill: a
        checkout that could not reach its remote knows exactly as much about
        its base as it did before asking.
        """
        if self.unreachable:
            return f"base freshness unknown: {self.unreachable}"
        if not self.tracked:
            return "base freshness unknown: this checkout answers to no remote branch"
        if not self.behind:
            return f"base is current with {self.tracked}"
        return (
            f"base is {self.behind} commit(s) behind {self.tracked}: "
            f"update with `{self.update_command}`"
        )


def git_line(launcher: ProcessLauncher, root: Path, arguments: list[str]) -> str:
    """One git probe's single line of output, empty when it had nothing to say.

    The freshness probe runs through the launcher seam rather than this
    module's own bound git, because one of its steps reaches the network: a
    launcher merges the non-interactive environment every agent spawn point
    uses over the console's, so a credential nobody can supply fails fast
    instead of waiting on a terminal prompt. Each of these probes answers a
    question that has a blank answer — no upstream, no recorded base, no
    branch — so a failure and empty output mean the same thing here.
    """
    status = launcher.launch(
        LaunchRequest(
            arguments=["git", *arguments],
            cwd=root,
            environment=non_interactive_environment({}),
        )
    )
    lines = status.stdout.splitlines()
    return lines[0].strip() if status.code == 0 and lines else ""


def tracked_remote_branch(launcher: ProcessLauncher, root: Path) -> str:
    """The remote branch a checkout answers to, empty when it answers to none.

    A branch that tracks one names it outright. A feature worktree tracks
    nothing, so the base recorded when it was created is asked what *it*
    tracks — which is how a worktree cut from an integration branch is still
    measured against that branch on the remote, the case a plain upstream
    question answers nothing about.
    """
    named = ["rev-parse", "--abbrev-ref", "--symbolic-full-name"]
    direct = git_line(launcher, root, [*named, "@{upstream}"])
    if direct:
        return direct
    branch = git_line(launcher, root, ["branch", "--show-current"])
    recorded = (
        git_line(launcher, root, ["config", "--get", base_config_key(branch)])
        if branch
        else ""
    )
    return (
        git_line(launcher, root, [*named, f"{recorded}@{{upstream}}"])
        if recorded
        else ""
    )


def probe_base_freshness(launcher: ProcessLauncher, root: Path) -> BaseFreshness:
    """Fetch, then count what the tracked branch holds and this checkout does not.

    A tree whose base has moved is self-consistent and says nothing about it,
    so only the remote can answer the question — one fetch and one count. A
    remote that cannot be reached leaves the answer unknown rather than
    guessing it either way.
    """
    tracked = tracked_remote_branch(launcher, root)
    if not tracked:
        return BaseFreshness()
    fetched = launcher.launch(
        LaunchRequest(
            arguments=["git", "fetch", "--quiet"],
            cwd=root,
            environment=non_interactive_environment({}),
        )
    )
    if fetched.code != 0:
        return BaseFreshness(
            tracked=tracked,
            unreachable=fetched.stderr.strip() or f"`git fetch` exited {fetched.code}",
        )
    counted = git_line(launcher, root, ["rev-list", "--count", f"HEAD..{tracked}"])
    if not counted.isdigit():
        return BaseFreshness(
            tracked=tracked, unreachable=f"git did not count {tracked}"
        )
    return BaseFreshness(tracked=tracked, behind=int(counted))


# lup: ignore[model-free-function] — driver: it puts a question to whoever is at
# the terminal, which is the command's business rather than the reading's
def confirm_base_freshness(freshness: BaseFreshness, interactive: bool) -> None:
    """Report the count, and let whoever is there answer for a moved base.

    A human at the terminal is shown what moved and decides; for an
    autonomous session nobody is, so the same count refuses to open one
    rather than scrolling past unread. A remote that could not be reached
    stops nothing — an offline checkout is still a checkout to work in.
    """
    typer.echo(freshness.report())
    if not freshness.stale():
        return
    if interactive and typer.confirm(
        "Open a session against the moved base anyway?", default=False
    ):
        return
    raise typer.BadParameter(freshness.report())


# lup: ignore[model-free-function] — driver: it ends the command, so what it
# does is refuse an invocation rather than answer anything about the reading
def require_fresh_base(freshness: BaseFreshness) -> None:
    """Refuse to start work that pins this base for everything it hands out.

    A run captures its base once and cuts every lease from it, so following a
    base that has already moved means re-basing each lease, re-deriving each
    diff against the new base, and re-running intake — which can add or drop
    concerns while work is in flight. Refusing before any of that exists
    costs a fetch; discovering it afterwards costs the run.
    """
    typer.echo(freshness.report())
    if freshness.stale():
        raise typer.BadParameter(freshness.report())


# -- CLI functions --


def branch_status(branch: str | None, as_json: bool) -> None:
    """Analyze branch containment, PR status, and worktree info."""
    has_remote = check_remote_auth()

    integration = get_integration_branch()
    current = git.out("branch", "--show-current")

    branch_list = (
        [branch] if branch else git.lines("branch", "--format=%(refname:short)")
    )

    results = [
        classify_branch(b, integration, current, has_remote=has_remote)
        for b in branch_list
    ]

    if as_json:
        output_json(results)
        return

    typer.echo(f"\nIntegration branch: {integration}")
    typer.echo(f"Current branch: {current}\n")
    status_markers: dict[BranchStatus, str] = {
        "LAND": "^",
        "DELETE": "x",
        "STALE": "~",
        "KEEP": " ",
        "CURRENT": "*",
        "NOT_FOUND": "?",
    }

    def row(r: BranchClassification) -> list[str]:
        marker = status_markers[r["status"]]
        wt = " [worktree]" if r.get("worktree") else ""  # lup: ignore[dict-get]
        return [f"[{marker}] {r['branch']}", r["status"], f"{r['reason']}{wt}"]

    typer.echo(format_table(("Branch", "Status", "Reason"), [row(r) for r in results]))

    typer.echo()
    deletable = [r for r in results if r["status"] in ("DELETE", "STALE")]
    if deletable:
        typer.echo(f"{len(deletable)} branch(es) can be cleaned up")

    landable = [r for r in results if r["status"] == "LAND"]
    if landable:
        typer.echo(f"{len(landable)} branch(es) hold unlanded work")


def base_branch(branch: str | None, as_json: bool) -> None:
    """Detect the base branch for the current (or specified) branch."""
    base = detect_base_branch(branch)
    effective = branch or git.out("branch", "--show-current")

    if as_json:
        output_json(
            {
                "branch": effective,
                "base": base.name,
                "merge_base": base.merge_base,
                "commits_ahead": base.distance,
            }
        )
    else:
        typer.echo(f"Branch: {effective}")
        typer.echo(f"Base: {base.name}")
        typer.echo(f"Merge base: {short_sha(base.merge_base)}")
        typer.echo(f"Commits ahead: {base.distance}")


COMMIT_PREFIX_LABELS = {
    "feat": "Added",
    "fix": "Fixed",
    "refactor": "Refactored",
    "docs": "Updated docs for",
    "test": "Added tests for",
    "chore": "Updated",
    "meta": "Updated",
    "data": "Added data for",
}
"""How a PR body reads each commit type aloud, for a project that uses these.

The types are one convention among several, and the English is a second
choice on top: a project spelling either differently passes its own table
rather than reading someone else's vocabulary back in its summaries.
"""


def pr_body(
    base_override: str | None,
    # Open keys: a prefix is whatever a commit subject happens to start with,
    # read off the log rather than chosen from a set this code knows.
    labels: StringMap = COMMIT_PREFIX_LABELS,
) -> None:
    """Generate a PR body from the current branch's commits against its base."""
    base = base_override or detect_base_branch().name

    log_lines = git.lines(
        "log",
        "--oneline",
        "--no-decorate",
        "--no-merges",
        f"{base}..HEAD",
        _ok_code=[0],
    )
    if not log_lines:
        typer.echo("No commits found since base branch", err=True)
        raise typer.Exit(1)

    groups: dict[str, list[str]] = defaultdict(list)
    for line in log_lines:
        message = line.partition(" ")[2]  # lup: ignore[string-split] — log line
        if not message:
            continue
        head = message.partition("(")[0]  # lup: ignore[string-split] — commit type
        prefix = head.partition(":")[0].lower()  # lup: ignore[string-split] — type
        groups[prefix].append(message)

    def summarize(prefix: str, messages: list[str]) -> str:
        fallback = prefix.capitalize()
        label = labels.get(prefix, fallback)  # lup: ignore[dict-get]
        first = messages[0].partition(":")[2]  # lup: ignore[string-split] — log line
        desc = (first or messages[0]).lstrip()
        more = f" (+{len(messages) - 1} more)" if len(messages) > 1 else ""
        return f"- {label} {desc}{more}"

    summary_lines = [summarize(p, msgs) for p, msgs in groups.items()]
    body_parts = ["## Summary", *summary_lines, "", "## Commits", *log_lines]
    body_parts.extend(["", "## Test plan", "- [ ] Verify changes work as expected"])

    typer.echo("\n".join(body_parts))


def survey(as_json: bool) -> None:
    """Collect branch, worktree, PR, and containment data."""
    has_remote = check_remote_auth()
    if has_remote:
        if not as_json:
            typer.echo("Fetching and pruning remote...", err=True)
        try:
            git("fetch", "--prune")
        except sh.ErrorReturnCode as e:
            logger.warning("Failed to fetch: %s", decode_stderr(e))

    integration = get_integration_branch()
    cur = git.out("branch", "--show-current")

    raw_branches = parse_branches()
    worktrees = parse_worktrees()
    branch_names = [b["name"] for b in raw_branches]
    containment = build_containment(branch_names)

    if has_remote and not as_json:
        typer.echo("Querying PR status...", err=True)
    pr_map: dict[str, PRStatus] = fetch_pr_status(branch_names) if has_remote else {}
    # A run records every branch it ever leased, and a completed run keeps
    # reporting the ones its cleanup did not delete. Only what is still on
    # disk is a branch this sweep can say anything about.
    lease_of = live_lease_branches(project_root() / ".lup" / "resolve")
    leased = {name: lease_of[name] for name in branch_names if name in lease_of}

    def info(b: ParsedBranch) -> BranchInfo:
        name = b["name"]
        checkout = worktrees.get(name)  # lup: ignore[dict-get] — open map
        contained_in = containment[name]
        pr_merged = name in pr_map and pr_map[name].state == "MERGED"

        if integration in contained_in or pr_merged:
            unique = 0
            diff_lines = 0
        else:
            unique = count_unique_commits(name, integration)
            diff_lines = count_source_diff_lines(name, integration)

        verdict = disposition_for(
            name,
            integration=integration,
            current=cur,
            contained_in=contained_in,
            pr=pr_map.get(name),  # lup: ignore[dict-get] — open map
            unique_commits=unique,
            held=leased[name].reason() if name in leased else "",
        )

        return BranchInfo(
            name=name,
            commit=b["commit"],
            tracking=b["tracking"],
            worktree=checkout,
            is_current=b["is_current"],
            contained_in=contained_in,
            pr=pr_map.get(name),  # lup: ignore[dict-get] — open map
            unique_commits=unique,
            source_diff_lines=diff_lines,
            disposition=verdict.status,
            reason=verdict.reason,
            changes=worktree_changes(checkout) if checkout else None,
        )

    branches_list = [info(b) for b in raw_branches]

    result = SurveyResult(
        integration_branch=integration,
        current_branch=cur,
        branches=branches_list,
        runs=runs_holding(leased),
    )

    if as_json:
        output_json(result)
    else:
        typer.echo(f"\nIntegration: {integration} | Current: {cur}\n")

        def display_row(bi: BranchInfo) -> list[str]:
            pr_str = f"#{bi.pr.number} {bi.pr.state}" if bi.pr else "-"
            marker = "* " if bi.is_current else "  "
            return [
                f"{marker}{bi.name}",
                bi.disposition,
                str(bi.unique_commits),
                str(bi.source_diff_lines),
                bi.changes.compact() if bi.changes else "-",
                pr_str,
                bi.reason,
            ]

        headers = ("Branch", "Disposition", "Unique", "Diff", "Dirt", "PR", "Reason")
        typer.echo(format_table(headers, [display_row(bi) for bi in branches_list]))

        for hold in result.runs:
            if not hold.alive:
                typer.echo(
                    f"\nrun {hold.run_id} is not running and holds "
                    f"{len(hold.branches)} branch(es): nothing will retire them.\n"
                    f"  uv run lup-devtools harness resolve status "
                    f"--run-id {hold.run_id}"
                )


type ActionVerdict = Literal["ok", "forced", "blocked"]


class PlannedAction(BaseModel):
    """One step of a deletion, carrying the verdict a preflight probe gave it.

    ``detail`` says what forcing would discard, or why the step cannot run —
    the annotation is a probe result, never a restatement of the flag.
    """

    description: str
    verdict: ActionVerdict = "ok"
    detail: str = ""

    def render(self) -> str:
        match self.verdict:
            case "ok":
                return f"{self.description} (ok)"
            case "forced":
                return f"{self.description} (force: {self.detail})"
            case "blocked":
                return f"{self.description} (blocked: {self.detail})"


class DeletionPlan(BaseModel):
    """Every step a deletion would take, evaluated without mutating anything."""

    branch: str
    worktree: str | None = None
    stranded: bool = False
    has_remote: bool = False
    delete_remote: bool = False
    """Whether origin's copy goes too, which is a decision, not an observation.

    ``has_remote`` is a fact about the world and this is an intention about
    it. Reading both from one field is what made a remote copy delete itself
    for existing — and a copy that exists is the thing that made deleting
    the local branch survivable in the first place.
    """
    actions: list[PlannedAction] = []

    def blocked(self) -> list[PlannedAction]:
        return [action for action in self.actions if action.verdict == "blocked"]


def worktree_changes(path: str) -> WorktreeChanges:
    """Count what a worktree holds, so a report can say what force discards."""
    modified = 0
    untracked = 0
    for line in git.lines("-C", path, "status", "--porcelain"):
        if line.startswith("??"):
            untracked += 1
        else:
            modified += 1
    return WorktreeChanges(modified=modified, untracked=untracked)


def remote_branch_exists(name: str) -> bool:
    """Report whether ``origin`` still carries the branch."""
    try:
        git("rev-parse", "--verify", f"refs/remotes/origin/{name}")
        return True
    except sh.ErrorReturnCode:
        return False


def plan_worktree_step(path: str, stranded: bool, force: bool) -> PlannedAction:
    """Judge the worktree removal — the one irreversible step."""
    if stranded:
        return PlannedAction(
            description=f"Prune stranded worktree: {path}",
            detail="checkout already gone",
        )

    changes = worktree_changes(path)
    description = f"Remove worktree: {path}"
    if not changes.dirty():
        return PlannedAction(description=description)

    if force:
        return PlannedAction(
            description=description,
            verdict="forced",
            detail=f"discards {changes.summary()}",
        )
    return PlannedAction(
        description=description,
        verdict="blocked",
        detail=f"holds {changes.summary()}; --force discards them",
    )


def plan_branch_step(name: str, force: bool) -> PlannedAction:
    """Judge the branch deletion the way ``git branch -d`` would."""
    description = f"Delete local branch: {name}"
    if is_ancestor(name, "HEAD"):
        return PlannedAction(description=description)
    if force:
        return PlannedAction(
            description=description, verdict="forced", detail="branch is unmerged"
        )
    return PlannedAction(
        description=description,
        verdict="blocked",
        detail="branch is unmerged; --force deletes it anyway",
    )


def dependent_pulls(name: str) -> list[int]:
    """Open PRs based on *name*, which deleting origin's copy would close.

    A stacked PR names its parent as its base, and GitHub closes a pull
    request whose base branch is deleted rather than moving it. Landing the
    parent and cleaning up after it is therefore enough to close the child,
    which is the ordinary end of the parent's life and no part of the
    child's — and the branch survives, so the closure reads as work lost
    until somebody recreates the base to reopen it.

    An unanswerable query is no dependents: this refines a deletion the
    caller already asked for, and a network failure is not a reason to
    refuse one.
    """
    try:
        rows = json.loads(
            gh.out(
                "pr",
                "list",
                *repository_arguments(),
                "--base",
                name,
                "--state",
                "open",
                "--json",
                "number",
            )
        )
    except sh.ErrorReturnCode as e:
        logger.warning(
            "Could not check for PRs based on %s: %s", name, decode_stderr(e)
        )
        return []
    return [PRStatus.model_validate(row).number for row in rows]


def plan_remote_step(name: str, force: bool) -> PlannedAction:
    """Judge deleting origin's copy by what else is still pointing at it."""
    description = f"Delete remote branch: origin/{name}"
    dependents = dependent_pulls(name)
    if not dependents:
        return PlannedAction(description=description)
    listed = ", ".join(f"#{number}" for number in dependents)
    if force:
        return PlannedAction(
            description=description,
            verdict="forced",
            detail=f"closes {listed}",
        )
    return PlannedAction(
        description=description,
        verdict="blocked",
        detail=(
            f"{listed} targets this branch and would be closed; "
            "retarget it first, or --force to close it anyway"
        ),
    )


def plan_deletion(name: str, force: bool, remote: bool | None = None) -> DeletionPlan:
    """Evaluate every precondition a deletion depends on, changing nothing.

    A dry run and the real path both read this, so what the dry run promises
    is what the real path went on to check.

    Whether origin's copy goes with it defaults to whether the branch is
    merged, because that is what the answer turns on. Cleaning up after a
    merged branch should take the remote too — the commits are in the
    integration branch and the copy is spent. An unmerged branch is the
    opposite case: origin holds the only copy that outlives this command,
    which is exactly what a push before deleting was for, so it stays
    unless a caller says otherwise in so many words.
    """
    worktree = parse_worktrees().get(name)  # lup: ignore[dict-get] — open branch map
    stranded = worktree is not None and not Path(worktree).exists()
    actions: list[PlannedAction] = []

    if worktree is not None:
        actions.append(plan_worktree_step(worktree, stranded=stranded, force=force))

    actions.append(plan_branch_step(name, force=force))

    merged = is_ancestor(name, "HEAD")
    has_remote = remote_branch_exists(name)
    delete_remote = has_remote and (merged if remote is None else remote)
    if delete_remote:
        actions.append(plan_remote_step(name, force=force))

    return DeletionPlan(
        branch=name,
        worktree=worktree,
        stranded=stranded,
        has_remote=has_remote,
        delete_remote=delete_remote,
        actions=actions,
    )


# lup: ignore[model-free-function] — driver: it prunes, reports, and exits
def abort_deletion(plan: DeletionPlan, completed: list[str], failure: str) -> NoReturn:
    """Report a mid-deletion failure, repairing a stranded registration first.

    ``git worktree remove`` clears the checkout before it unregisters, so a
    failure here can leave a worktree git still believes in. Pruning is the
    repair, and the caller cannot be expected to know that.

    Prune, removal, and the branch deletion itself all take the config lock,
    so all three fail alike where the sandbox holds it — and there the repair
    is not a repair, because the prune it prescribes fails the same way. The
    mount state is what says which of the two failures this is.
    """
    typer.echo(f"Failed to delete {plan.branch}: {failure}", err=True)

    diagnosis = config_lock_diagnosis()
    if diagnosis:
        typer.echo(diagnosis, err=True)
    elif plan.worktree is not None and not Path(plan.worktree).exists():
        try:
            git("worktree", "prune")
            typer.echo(
                f"Pruned the stranded registration for {plan.worktree}", err=True
            )
        except sh.ErrorReturnCode as error:
            typer.echo(
                f"The checkout at {plan.worktree} is gone but still registered. "
                f"Recover with `git worktree prune`: {decode_stderr(error)}",
                err=True,
            )

    typer.echo(f"Completed first: {', '.join(completed) or 'nothing'}", err=True)
    raise typer.Exit(1)


# lup: ignore[model-free-function] — driver: the plan describes, this runs git
def run_deletion(plan: DeletionPlan, force: bool) -> None:
    """Carry out a plan whose preflight passed, reporting what actually ran."""
    completed: list[str] = []

    if plan.stranded:
        try:
            git("worktree", "prune")
            typer.echo(f"Pruned stranded worktree: {plan.worktree}")
            completed.append("pruned worktree")
        except sh.ErrorReturnCode as error:
            abort_deletion(plan, completed, f"prune failed: {decode_stderr(error)}")
    elif plan.worktree is not None:
        try:
            git("worktree", "remove", *(["--force"] if force else []), plan.worktree)
            typer.echo(f"Removed worktree: {plan.worktree}")
            completed.append("removed worktree")
        except sh.ErrorReturnCode as error:
            abort_deletion(
                plan, completed, f"worktree removal failed: {decode_stderr(error)}"
            )

    try:
        git("branch", "-D" if force else "-d", plan.branch)
        typer.echo(f"Deleted branch: {plan.branch}")
        completed.append("deleted branch")
    except sh.ErrorReturnCode as error:
        abort_deletion(
            plan, completed, f"branch deletion failed: {decode_stderr(error)}"
        )

    if not plan.delete_remote:
        if plan.has_remote:
            typer.echo(
                f"Kept remote branch: origin/{plan.branch} — it holds these "
                "commits now (--remote deletes it too)"
            )
        return

    try:
        git("push", "origin", "--delete", plan.branch)
        typer.echo(f"Deleted remote branch: origin/{plan.branch}")
    except sh.ErrorReturnCode as error:
        typer.echo(f"Warning: remote deletion failed: {decode_stderr(error)}", err=True)


def delete_branch(
    name: str,
    dry_run: bool,
    force: bool,
    remote: bool | None = None,
) -> None:
    """Delete a branch and its worktree, and origin's copy if it is spent."""
    cur = git.out("branch", "--show-current")
    if name == cur:
        typer.echo(f"Error: cannot delete the current branch ({name})", err=True)
        raise typer.Exit(1)

    plan = plan_deletion(name, force, remote)

    if dry_run:
        typer.echo(f"Would perform {len(plan.actions)} action(s):")
        for action in plan.actions:
            typer.echo(f"  {action.render()}")
        return

    blocked = plan.blocked()
    if blocked:
        typer.echo(f"Refusing to delete {name} — nothing was changed:", err=True)
        for action in blocked:
            typer.echo(f"  {action.render()}", err=True)
        typer.echo("Use --force to override.", err=True)
        raise typer.Exit(1)

    if plan.delete_remote and not is_ancestor(name, "HEAD"):
        typer.echo(
            f"Warning: {name} holds commits HEAD does not, and origin/{name} "
            "is going with it — after this the work is in no branch.",
            err=True,
        )

    run_deletion(plan, force)


def create_resolve_branch(concern_id: str) -> None:
    """Create and switch to the `resolve/<id>` branch for a /lup:resolve editor.

    The execute workflow's editor calls this as its first step, through the
    allowlisted `uv run lup-devtools` path, instead of a raw `git checkout -b` —
    so the bash hook needs no editor special-case. The name is fixed to the
    `resolve/<slug>` convention the workflow's merge and cleanup rely on.
    """
    slug = concern_id.strip().strip("/")  # lup: ignore[string-strip] — typed-id hygiene
    ok = bool(slug) and slug[0].isalnum()
    ok = ok and all(c.isalnum() or c in "._-" for c in slug)
    if not ok:
        typer.echo(f"Invalid concern id: {concern_id!r}", err=True)
        raise typer.Exit(1)
    branch = f"resolve/{slug}"
    try:
        git("checkout", "-b", branch)
    except sh.ErrorReturnCode as e:
        typer.echo(f"Could not create {branch}: {decode_stderr(e)}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Created and switched to {branch}")
