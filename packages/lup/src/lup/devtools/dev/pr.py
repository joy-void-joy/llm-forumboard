"""PR lifecycle: status, merge, push, checks, and base sync.

Mechanical helpers for ``/lup:close`` and ``/lup:rebase``.

Examples::

    $ uv run lup-devtools dev pr status --json
    $ uv run lup-devtools dev pr merge 42
    $ uv run lup-devtools dev pr sync-base --json
    $ uv run lup-devtools dev pr push --force --json
    $ uv run lup-devtools dev pr create --base dev --title "feat: search" --body "..."
    $ uv run lup-devtools dev pr create --base dev --title "feat: search" --body-file body.md
    $ uv run lup-devtools dev pr update 42 --body-file body.md
"""

import json
import logging
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlparse

import sh
import typer
from pydantic import BaseModel, Field

from lup.devtools.dev.branches import (
    detect_base_branch,
    get_integration_branch,
)
from lup.devtools.layout import get_tree_dir

from lup.devtools.utils import git, gh, decode_stderr, output_json

logger = logging.getLogger(__name__)


class MergeMethod(StrEnum):
    """How a PR's commits reach the base branch.

    The three GitHub offers. Which one a repository wants is its own
    decision rather than this command's: one whose history is merge commits
    reads a squash as a break in it, and one that squashes reads the
    reverse. A merge commit is the default because it is the only one of the
    three that loses nothing — the branch's own commits stay reachable, and
    a PR stacked on this one keeps its base as a real ancestor instead of
    facing a rewritten copy of the work it already contains.
    """

    merge = "merge"
    squash = "squash"
    rebase = "rebase"


def current_branch() -> str:
    return git.out("branch", "--show-current")


class ReviewInfo(BaseModel):
    author: str
    state: str
    body: str


class CheckInfo(BaseModel):
    name: str
    status: str
    conclusion: str


class GhAuthor(BaseModel):
    """`author` object inside `gh pr view --json reviews`."""

    login: str = "unknown"


class GhReview(BaseModel):
    """One element of `gh pr view --json reviews`, as gh names the fields."""

    author: GhAuthor | None = None
    state: str = ""
    body: str = ""


class GhCheck(BaseModel):
    """One `statusCheckRollup` element: check runs carry `name`/`status`/
    `conclusion`; legacy status contexts only `context`."""

    name: str = ""
    context: str = ""
    status: str = ""
    conclusion: str = ""


class GhPrDetail(BaseModel):
    """The `gh pr view --json` payload (aliases are gh's camelCase names)."""

    reviews: list[GhReview] = []
    checks: list[GhCheck] = Field(default=[], alias="statusCheckRollup")
    review_decision: str = Field(default="", alias="reviewDecision")
    mergeable: str = ""
    state: str = ""


class GhPrRef(BaseModel):
    """One row of `gh pr list --json number,title,url`."""

    number: int
    title: str = ""
    url: str = ""


class PRInfo(BaseModel):
    number: int
    title: str
    url: str
    review_decision: str
    mergeable: str
    checks_passing: bool
    reviews: list[ReviewInfo]
    checks: list[CheckInfo]

    def render(self) -> None:
        """Pretty-print PR status with formatted reviews and checks."""
        typer.echo(f"\n  PR #{self.number}: {self.title}")
        typer.echo(f"  {self.url}")
        typer.echo(f"  Review: {self.review_decision or 'pending'}")
        typer.echo(f"  Mergeable: {self.mergeable}")

        if self.reviews:
            typer.echo(f"\n  Reviews ({len(self.reviews)}):")
            author_width = max(len(r.author) for r in self.reviews)
            for r in self.reviews:
                typer.echo(f"    {r.author:<{author_width}} {r.state}")

        if self.checks:
            typer.echo(f"\n  Checks ({len(self.checks)}):")
            passed = sum(
                1
                for c in self.checks
                if c.conclusion.upper() in ("SUCCESS", "NEUTRAL", "SKIPPED")
            )
            typer.echo(f"    {passed}/{len(self.checks)} passing")
            for c in self.checks:
                marker = (
                    "✓"
                    if c.conclusion.upper() in ("SUCCESS", "NEUTRAL", "SKIPPED")
                    else "✗"
                )
                typer.echo(f"    {marker} {c.name}: {c.conclusion or c.status}")


class PRResult(BaseModel):
    """One PR command's outcome, rendering itself for a human reader.

    The only question the CLI asks of a result is how to print it, so the base
    declares that and each variant answers it — a new command's result is one
    class rather than an edit to a printer that would have to notice it. The
    default answer names each field in turn, which is the whole of what a flat
    result has to say; a variant whose shape deserves a layout overrides it.
    """

    def render(self) -> None:
        """Print this result as plain lines, one per field."""
        for key, value in self.model_dump().items():
            match value:
                case list():
                    typer.echo(f"{key}:")
                    for item in value:
                        typer.echo(f"  - {item}")
                case dict():
                    typer.echo(f"{key}:")
                    for k, v in value.items():
                        typer.echo(f"  {k}: {v}")
                case _:
                    typer.echo(f"{key}: {value}")


class PRStatusResult(PRResult):
    branch: str
    pr: PRInfo | None

    def render(self) -> None:
        if self.pr is None:
            typer.echo(f"branch: {self.branch}")
            typer.echo("pr: no open PR")
            return
        self.pr.render()


class MergeResult(PRResult):
    pr_number: int
    merged: bool
    integration_branch: str
    pulled: bool


class SyncBaseResult(PRResult):
    feature_branch: str
    base_branch: str
    base_source: Literal["explicit", "recorded", "guessed"]
    merged: bool
    conflicts: list[str]


class ExistingPR(BaseModel):
    number: int
    url: str


class PushResult(PRResult):
    branch: str
    pushed: bool
    force: bool
    existing_pr: ExistingPR | None


class CreateResult(PRResult):
    number: int
    url: str


# The result already answers how to print itself; what is left is the flag.
# lup: ignore[model-free-function] — subject is the CLI's --json choice
def output_result(result: PRResult, as_json: bool) -> None:
    if as_json:
        output_json(result)
        return
    result.render()


class DetectedBase(BaseModel):
    """The auto-detected base branch and how its name was determined."""

    name: str
    source: Literal["recorded", "guessed"]


def find_base_branch() -> DetectedBase:
    """Auto-detect the base branch, preferring the recorded creation base."""
    try:
        candidate = detect_base_branch()
        return DetectedBase(name=candidate.name, source=candidate.source)
    except (typer.Exit, SystemExit):
        return DetectedBase(name=get_integration_branch(), source="guessed")


def status(
    branch: str | None,
    as_json: bool,
) -> None:
    """Fetch PR review status, checks, and comments for a branch."""
    branch_name = branch or current_branch()

    try:
        rows = json.loads(
            gh.out(
                "pr",
                "list",
                "--head",
                branch_name,
                "--state",
                "open",
                "--json",
                "number,title,url",
            )
        )
    except sh.ErrorReturnCode as e:
        typer.echo(f"Failed to query PRs via gh: {decode_stderr(e)}", err=True)
        raise typer.Exit(1)
    prs = [GhPrRef.model_validate(row) for row in rows]

    if not prs:
        result = PRStatusResult(branch=branch_name, pr=None)
        output_result(result, as_json)
        if not as_json:
            typer.echo(f"No open PR found for branch {branch_name}")
        return

    pr_data = prs[0]
    pr_number = pr_data.number

    try:
        detail = GhPrDetail.model_validate_json(
            gh.out(
                "pr",
                "view",
                str(pr_number),
                "--json",
                "reviews,statusCheckRollup,mergeable,mergeStateStatus,reviewDecision",
            )
        )
    except sh.ErrorReturnCode as e:
        typer.echo(
            f"Failed to fetch PR #{pr_number} via gh: {decode_stderr(e)}", err=True
        )
        raise typer.Exit(1)

    reviews = [
        ReviewInfo(
            author=r.author.login if r.author else "unknown",
            state=r.state,
            body=r.body,
        )
        for r in detail.reviews
    ]

    checks = [
        CheckInfo(
            name=c.name or c.context or "unknown",
            status=c.status,
            conclusion=c.conclusion,
        )
        for c in detail.checks
    ]

    checks_passing = (
        all(
            c.conclusion.upper() in ("SUCCESS", "NEUTRAL", "SKIPPED")
            for c in checks
            if c.status.upper() == "COMPLETED"
        )
        if checks
        else True
    )

    pr_info = PRInfo(
        number=pr_number,
        title=pr_data.title,
        url=pr_data.url,
        review_decision=detail.review_decision,
        mergeable=detail.mergeable,
        checks_passing=checks_passing,
        reviews=reviews,
        checks=checks,
    )

    result = PRStatusResult(branch=branch_name, pr=pr_info)
    output_result(result, as_json)


def pr_merged(pr_number: int) -> bool:
    """Whether GitHub says the PR is merged, asked rather than inferred.

    ``gh pr merge`` merges and then deletes the branch, and reports a
    failure of the second as a failure of the whole. Reading the state back
    separates a merge that did not happen from a cleanup that did not, which
    are opposite situations: the first is retried, the second is finished
    work with a leftover, and treating either as the other is how a landed
    PR comes to look like one still waiting.
    """
    try:
        detail = GhPrDetail.model_validate_json(
            gh.out("pr", "view", str(pr_number), "--json", "state")
        )
    except sh.ErrorReturnCode:
        logger.exception("could not read PR #%s state back", pr_number)
        return False
    return detail.state == "MERGED"


def merge(
    pr_number: int,
    dry_run: bool,
    as_json: bool = False,
    method: MergeMethod = MergeMethod.merge,
    gh_args: tuple[str, ...] = (),
) -> None:
    """Merge a PR and pull changes into the integration branch.

    Args:
        pr_number: PR to merge.
        dry_run: Report what would happen, changing nothing.
        as_json: Emit the result as JSON.
        method: How the commits reach the base branch.
        gh_args: Further flags handed to ``gh pr merge`` untouched, for
            anything this signature does not name.
    """
    integration = get_integration_branch()

    if dry_run:
        typer.echo(f"Would merge PR #{pr_number} ({method})")
        typer.echo(f"Would pull changes into {integration}")
        return

    try:
        gh("pr", "merge", str(pr_number), f"--{method}", "--delete-branch", *gh_args)
        typer.echo(f"Merged PR #{pr_number}")
    except sh.ErrorReturnCode as e:
        if not pr_merged(pr_number):
            typer.echo(f"Merge failed: {decode_stderr(e)}", err=True)
            raise typer.Exit(1)
        typer.echo(
            f"Merged PR #{pr_number}, but its cleanup did not finish: "
            f"{decode_stderr(e)}",
            err=True,
        )

    try:
        tree_dir = get_tree_dir()
    except (typer.Exit, SystemExit):
        tree_dir = None
    integration_path = tree_dir / integration if tree_dir else None
    pulled = False
    if integration_path and integration_path.is_dir():
        try:
            git("-C", str(integration_path), "pull", "--ff-only")
            typer.echo(f"Pulled changes into {integration}")
            pulled = True
        except sh.ErrorReturnCode as e:
            typer.echo(
                f"Warning: pull failed in {integration}: {decode_stderr(e)}", err=True
            )

    result = MergeResult(
        pr_number=pr_number,
        merged=True,
        integration_branch=integration,
        pulled=pulled,
    )
    output_result(result, as_json)


def sync_base(
    base: str | None,
    as_json: bool,
) -> None:
    """Sync the base branch and merge it into the current feature branch.

    A base only topology could name is reported and not merged. Guessing wrong
    picks a branch the feature never diverged from, and every later step reads
    that answer as settled — the history rebuild resets onto it, so a wrong
    guess rewrites whatever sits between the two branches. An authoritative
    base is one the caller passed or worktree creation recorded.
    """
    feature = current_branch()
    base_source: Literal["explicit", "recorded", "guessed"] = "explicit"
    if base:
        base_branch = base
    else:
        detected = find_base_branch()
        base_branch = detected.name
        base_source = detected.source

    if not as_json:
        typer.echo(f"Feature branch: {feature}", err=True)
        typer.echo(f"Base branch: {base_branch}", err=True)

    if base_source == "guessed":
        if not as_json:
            typer.echo(
                f"Topology alone picked {base_branch}, so nothing is merged."
                f" Confirm the base, then rerun with --base <branch>.",
                err=True,
            )
        output_result(
            SyncBaseResult(
                feature_branch=feature,
                base_branch=base_branch,
                base_source=base_source,
                merged=False,
                conflicts=[],
            ),
            as_json,
        )
        raise typer.Exit(1)

    try:
        tree_dir = get_tree_dir()
    except (typer.Exit, SystemExit):
        tree_dir = None
    base_path = tree_dir / base_branch if tree_dir else None

    if base_path and base_path.is_dir():
        if not as_json:
            typer.echo(f"Syncing {base_branch}...", err=True)
        try:
            git("-C", str(base_path), "pull", "--ff-only")
            git("-C", str(base_path), "push")
        except sh.ErrorReturnCode as e:
            typer.echo(
                f"Warning: sync of {base_branch} failed: {decode_stderr(e)}", err=True
            )

    if not as_json:
        typer.echo(f"Merging {base_branch} into {feature}...", err=True)

    try:
        git("merge", base_branch)
        result = SyncBaseResult(
            feature_branch=feature,
            base_branch=base_branch,
            base_source=base_source,
            merged=True,
            conflicts=[],
        )
    except sh.ErrorReturnCode:
        unmerged = git.lines("diff", "--name-only", "--diff-filter=U", _ok_code=[0, 1])
        conflicts = [f for f in unmerged if f]
        result = SyncBaseResult(
            feature_branch=feature,
            base_branch=base_branch,
            base_source=base_source,
            merged=False,
            conflicts=conflicts,
        )
        if not as_json:
            typer.echo(f"Merge conflicts in {len(conflicts)} file(s):", err=True)
            for f in conflicts:
                typer.echo(f"  {f}", err=True)

    output_result(result, as_json)
    if not result.merged:
        raise typer.Exit(1)


def push(
    force: bool,
    as_json: bool,
) -> None:
    """Push the current branch and report any existing PR."""
    branch_name = current_branch()

    try:
        if force:
            git("push", "--force")
        else:
            git("push", "-u", "origin", branch_name)
        pushed = True
    except sh.ErrorReturnCode as e:
        typer.echo(f"Push failed: {decode_stderr(e)}", err=True)
        pushed = False

    existing_pr = None
    try:
        pr_raw = gh.out(
            "pr",
            "list",
            "--head",
            branch_name,
            "--state",
            "open",
            "--json",
            "number,url",
        )
        rows = json.loads(pr_raw) if pr_raw else []
        prs = [GhPrRef.model_validate(row) for row in rows]
        if prs:
            existing_pr = ExistingPR(number=prs[0].number, url=prs[0].url)
    except sh.ErrorReturnCode:
        pass

    result = PushResult(
        branch=branch_name,
        pushed=pushed,
        force=force,
        existing_pr=existing_pr,
    )
    output_result(result, as_json)
    if not pushed:
        raise typer.Exit(1)


def parse_pr_url(stdout: str) -> str:
    """Extract the PR URL from ``gh pr create`` stdout (last URL-like line)."""
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if urlparse(candidate).scheme in ("http", "https"):
            return candidate
    return ""


def resolve_body(body: str | None, body_file: Path | None) -> str:
    """The body text, from whichever of the two ways of giving it was used.

    A PR body is prose long enough to want headings, code spans and lists,
    and whoever composes one has usually just written it to a file. Reading
    that file here keeps the text out of an argument list, where quoting is
    the caller's problem: a body spliced through a shell needs every
    apostrophe in its prose escaped by hand, and one missed truncates the
    document into a parse error that names an offset rather than the body.
    """
    match (body, body_file):
        case (None, None):
            raise typer.BadParameter("pass --body or --body-file")
        case (str(), Path()):
            raise typer.BadParameter("pass --body or --body-file, not both")
        case (None, Path() as path):
            try:
                return path.read_text(encoding="utf-8")
            except OSError as e:
                raise typer.BadParameter(f"cannot read {path}: {e}")
        case (str() as text, None):
            return text


def create(
    base: str,
    title: str,
    body: str,
    as_json: bool,
) -> None:
    """Create a new PR.

    ``gh pr create`` has no ``--json`` flag — on success it prints the new
    PR's URL to stdout. The PR number is the final path segment of that URL.
    """
    try:
        raw = gh.out(
            "pr",
            "create",
            "--base",
            base,
            "--title",
            title,
            "--body",
            body,
        )
    except sh.ErrorReturnCode as e:
        typer.echo(f"Failed to create PR: {decode_stderr(e)}", err=True)
        raise typer.Exit(1)

    url = parse_pr_url(raw)
    if not url:
        typer.echo(f"PR created but URL not found in output:\n{raw}", err=True)
        raise typer.Exit(1)

    number_segment = PurePosixPath(urlparse(url).path).name
    if not number_segment.isdigit():
        typer.echo(f"PR created at {url} but could not parse number", err=True)
        raise typer.Exit(1)

    result = CreateResult(number=int(number_segment), url=url)
    output_result(result, as_json)


def update(
    pr_number: int,
    body: str,
) -> None:
    """Update a PR body."""
    try:
        gh("pr", "edit", str(pr_number), "--body", body)
        typer.echo(f"Updated PR #{pr_number}")
    except sh.ErrorReturnCode as e:
        typer.echo(f"Failed to update PR: {decode_stderr(e)}", err=True)
        raise typer.Exit(1)
