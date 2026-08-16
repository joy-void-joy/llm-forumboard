# lup: ignore[import-re, re-call]
# Diff hunks are semi-structured text with no parser — significance detection
# is alternation over diff lines, so the regex rules are opted out file-wide.
"""Conflict scope classification, audit, and completion for merge/rebase conflicts.

After a failed merge or rebase, classifies conflicted files as in-scope
(touched by this branch) or out-of-scope (only changed on the other side).

The `conflicts`, `conflict_status`, `conflict_audit`, and `conflict_complete`
entry points back the `lup-devtools dev conflict` subcommands wired in
`lup.devtools.dev.app`.

Alone among this project's commands these are spelled without ``uv run``,
which parses ``pyproject.toml`` before it will run anything: the moment the
conflict is *in* that file, ``uv`` refuses to start and the whole conflict
toolchain withdraws exactly where the manifest that configures the project is
at stake. ``LAUNCHER`` names the console script in the project's own
environment, which imports this package directly and reads no manifest.

Examples::

    $ .venv/bin/lup-devtools dev conflict list
    $ .venv/bin/lup-devtools dev conflict list --json
    $ .venv/bin/lup-devtools dev conflict status --json
    $ .venv/bin/lup-devtools dev conflict audit src/lup/agent/core.py --json
    $ .venv/bin/lup-devtools dev conflict complete --dry-run
"""

import logging
import re
from pathlib import Path
from typing import TypedDict

import sh
import typer
from pydantic import BaseModel

from lup.devtools.utils import (
    format_table,
    git,
    decode_stderr,
    output_json,
    short_sha,
)

logger = logging.getLogger(__name__)

# lup: ignore[constant-declaration] — where uv installs a project's own console
# script, which is the property the docstring below turns on
LAUNCHER = ".venv/bin/lup-devtools"
"""How the conflict workflow reaches this toolchain, named by path.

A ``PATH`` lookup would let a sibling worktree's environment answer, and
``uv run`` would parse the manifest that is under repair; the console script
in this project's own environment does neither.
"""

# lup: ignore[constant-declaration] — what Python packaging calls the file
MANIFEST = "pyproject.toml"
"""The file ``uv`` must parse before it will run anything."""


def invocation(*words: str) -> str:
    """Spell one command so it starts while the manifest holds conflicts."""
    return " ".join([LAUNCHER, *words])


def manifest_conflicted(root: Path) -> bool:
    """Whether a merge left markers in the manifest ``uv`` has to parse."""
    try:
        content = (root / MANIFEST).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "<<<<<<<" in content and ">>>>>>>" in content


def conflicted_manifest_notice() -> str:
    """What to run instead, once the toolchain notices it is mid-conflict."""
    return (
        f"{MANIFEST} holds conflict markers, so `uv` cannot parse it and every "
        f"`uv run ...` command will fail to start until the merge is settled. "
        f"Reach this toolchain as `{LAUNCHER} ...` meanwhile — "
        f"`{invocation('dev', 'conflict', 'status', '--json')}`."
    )


class ConflictFile(TypedDict):
    path: str
    conflict_count: int
    scope: str
    branch_touched: bool


class ConflictReport(TypedDict):
    state: str
    base: str
    files: list[ConflictFile]
    in_scope_count: int
    out_of_scope_count: int


def find_git_dir() -> Path:
    """Locate the .git directory (works in worktrees too)."""
    return Path(git.out("rev-parse", "--git-dir"))


def detect_conflict_state() -> str | None:
    """Detect whether we're in a merge, rebase, or cherry-pick."""
    git_dir = find_git_dir()
    if (git_dir / "MERGE_HEAD").exists():
        return "merge"
    if (git_dir / "rebase-merge").is_dir() or (git_dir / "rebase-apply").is_dir():
        return "rebase"
    if (git_dir / "CHERRY_PICK_HEAD").exists():
        return "cherry-pick"
    return None


class BranchScope(BaseModel):
    """The merge base plus the files this branch touched since it."""

    base: str
    files: set[str]  # lup: ignore[set-shape] — membership-tested scope set


def get_branch_files(state: str) -> BranchScope:
    """The merge base and this branch's touched files, for scope classification."""
    git_dir = find_git_dir()

    def ref_file(path: Path) -> str:
        return path.read_text().strip()

    match state:
        case "merge":
            merge_head = git.out("rev-parse", "MERGE_HEAD")
            base = git.out("merge-base", "HEAD", merge_head)
            tip = "HEAD"

        case "rebase":
            rebase_merge = git_dir / "rebase-merge"
            rebase_apply = git_dir / "rebase-apply"
            try:
                if rebase_merge.exists():
                    onto = ref_file(rebase_merge / "onto")
                    orig_head = ref_file(rebase_merge / "head")
                elif rebase_apply.exists():
                    onto = ref_file(rebase_apply / "onto")
                    orig_head = ref_file(rebase_apply / "orig-head")
                else:
                    typer.echo("Cannot determine rebase state", err=True)
                    raise typer.Exit(1)
            except OSError as e:
                typer.echo(f"Cannot read rebase state: {e}", err=True)
                raise typer.Exit(1) from e
            base = git.out("merge-base", orig_head, onto)
            tip = orig_head

        case "cherry-pick":
            cherry_head = git.out("rev-parse", "CHERRY_PICK_HEAD")
            base = git.out("merge-base", "HEAD", cherry_head)
            tip = "HEAD"

        case _:
            typer.echo(f"Unknown conflict state: {state}", err=True)
            raise typer.Exit(1)

    touched = git.lines("diff", "--name-only", f"{base}..{tip}", _ok_code=[0])
    return BranchScope(base=base, files=set(touched))  # lup: ignore[set-shape]


def list_conflicted_files() -> list[str]:
    """List files with unresolved conflicts."""
    return git.lines("diff", "--name-only", "--diff-filter=U", _ok_code=[0])


def count_conflict_markers(path: str) -> int:
    """Count the number of conflict marker blocks in a file."""
    try:
        content = Path(path).read_text(encoding="utf-8")
        return content.count("<<<<<<<")
    except OSError:
        return 0


def build_conflict_report(state: str) -> ConflictReport:
    """Build a structured conflict scope report."""
    conflicted = list_conflicted_files()
    scope = get_branch_files(state)

    files: list[ConflictFile] = [
        {
            "path": path,
            "conflict_count": count_conflict_markers(path),
            "scope": "in-scope" if path in scope.files else "out-of-scope",
            "branch_touched": path in scope.files,
        }
        for path in conflicted
    ]
    in_scope = sum(1 for f in files if f["branch_touched"])

    return {
        "state": state,
        "base": scope.base,
        "files": files,
        "in_scope_count": in_scope,
        "out_of_scope_count": len(files) - in_scope,
    }


def conflicts(as_json: bool) -> None:
    """Show conflicted files with scope classification."""
    state = detect_conflict_state()
    if not state:
        if as_json:
            output_json(
                ConflictReport(
                    state="none",
                    base="",
                    files=[],
                    in_scope_count=0,
                    out_of_scope_count=0,
                )
            )
        else:
            typer.echo("Not in a merge, rebase, or cherry-pick state")
        return

    conflicted = list_conflicted_files()
    if not conflicted:
        typer.echo("No conflicted files found")
        return

    report = build_conflict_report(state)

    if as_json:
        output_json(report)
        return

    typer.echo(f"\nConflict state: {report['state']}")
    typer.echo(f"Merge base: {short_sha(report['base'])}")
    rows = [(f["path"], str(f["conflict_count"]), f["scope"]) for f in report["files"]]
    typer.echo()
    typer.echo(
        format_table(
            ("File", "Conflicts", "Scope"),
            rows,
            aligns=("left", "right", "right"),
        )
    )

    typer.echo(
        f"\nIn-scope: {report['in_scope_count']}, "
        f"Out-of-scope: {report['out_of_scope_count']}"
    )


# -- Status, audit, and completion --


class ConflictStatusResult(BaseModel):
    operation: str
    conflicted_files: list[str]
    ours_ref: str
    theirs_ref: str
    ours_commits: list[str]
    theirs_commits: list[str]


class FileAuditResult(BaseModel):
    path: str
    ours_removals: list[str]
    theirs_removals: list[str]
    warning: bool
    partial: bool = False


class AuditResult(BaseModel):
    files: list[FileAuditResult]
    has_warnings: bool


SIGNIFICANT_PATTERN = re.compile(
    r"^-(def |class |async def |@app\.|@[a-z]+_tool|    def )"
)


def theirs_ref_for(operation: str) -> str:
    """Return the git ref for the *other* side of an in-progress operation.

    A rebase exposes the commit being replayed as ``REBASE_HEAD`` — not
    ``CHERRY_PICK_HEAD`` — so diffing against the wrong ref yields empty
    "theirs" diffs during a rebase.
    """
    match operation:
        case "merge":
            return "MERGE_HEAD"
        case "rebase":
            return "REBASE_HEAD"
        case _:
            return "CHERRY_PICK_HEAD"


def extract_removals(diff_output: str) -> list[str]:
    """Find removed functions/classes/decorators in a diff."""
    return [
        line.removeprefix("-").strip()
        for line in diff_output.splitlines()
        if SIGNIFICANT_PATTERN.match(line)
    ]


def conflict_status(as_json: bool) -> None:
    """Detect conflict state, list files, and show both sides' history."""
    operation = detect_conflict_state()

    if operation is None:
        if as_json:
            result = ConflictStatusResult(
                operation="none",
                conflicted_files=[],
                ours_ref="HEAD",
                theirs_ref="",
                ours_commits=[],
                theirs_commits=[],
            )
            output_json(result)
        else:
            typer.echo("No merge/rebase/cherry-pick in progress")
        return

    conflicted = list_conflicted_files()

    ours_ref = "HEAD"
    theirs_ref = theirs_ref_for(operation)

    def log_range(base: str, tip: str) -> list[str]:
        rows = git.lines("log", "--oneline", f"{base}..{tip}", _ok_code=[0])
        return [r for r in rows if r]

    try:
        merge_base = git.out("merge-base", "HEAD", theirs_ref, _ok_code=[0])
        ours_commits = log_range(merge_base, "HEAD")
        theirs_commits = log_range(merge_base, theirs_ref)
    except sh.ErrorReturnCode:
        logger.warning("No shared history with %s; showing bare status", theirs_ref)
        ours_commits = []
        theirs_commits = []

    result = ConflictStatusResult(
        operation=operation,
        conflicted_files=conflicted,
        ours_ref=ours_ref,
        theirs_ref=theirs_ref,
        ours_commits=ours_commits,
        theirs_commits=theirs_commits,
    )

    if as_json:
        output_json(result)
    else:
        typer.echo(f"Operation: {operation}")
        typer.echo(f"Conflicted files ({len(conflicted)}):")
        for f in conflicted:
            typer.echo(f"  {f}")
        if ours_commits:
            typer.echo(f"\nOurs ({ours_ref}):")
            for c in ours_commits:
                typer.echo(f"  {c}")
        if theirs_commits:
            typer.echo(f"\nTheirs ({theirs_ref}):")
            for c in theirs_commits:
                typer.echo(f"  {c}")


def conflict_audit(files: list[str], as_json: bool) -> None:
    """Post-resolution deletion audit: check for accidentally dropped code."""
    operation = detect_conflict_state()
    if operation is None:
        typer.echo("No merge/rebase/cherry-pick in progress", err=True)
        raise typer.Exit(1)

    theirs_ref = theirs_ref_for(operation)

    def audit_file(path: str) -> FileAuditResult:
        ours_diff = git.out("diff", "HEAD", "--", path, _ok_code=[0, 1])
        ours_removals = extract_removals(ours_diff)

        theirs_diff = ""
        partial = False
        try:
            theirs_diff = git.out("diff", theirs_ref, "--", path, _ok_code=[0, 1])
        except sh.ErrorReturnCode as e:
            partial = True
            typer.echo(
                f"Warning: could not diff {path} against {theirs_ref} — "
                f"theirs-side audit is partial ({decode_stderr(e)})",
                err=True,
            )
        theirs_removals = extract_removals(theirs_diff)

        return FileAuditResult(
            path=path,
            ours_removals=ours_removals,
            theirs_removals=theirs_removals,
            warning=bool(ours_removals or theirs_removals),
            partial=partial,
        )

    file_results = [audit_file(path) for path in files]

    audit_result = AuditResult(
        files=file_results,
        has_warnings=any(f.warning for f in file_results),
    )

    if as_json:
        output_json(audit_result)
    else:
        for f in file_results:
            status = "WARNING" if f.warning else "OK"
            if f.partial:
                status += " (partial)"
            typer.echo(f"  {f.path}: {status}")
            for r in f.ours_removals:
                typer.echo(f"    - [ours] {r}")
            for r in f.theirs_removals:
                typer.echo(f"    - [theirs] {r}")

        if audit_result.has_warnings:
            typer.echo("\nSome files have removals — review before completing.")


def conflict_complete(dry_run: bool) -> None:
    """Finalize the merge/rebase/cherry-pick after all conflicts are resolved."""
    operation = detect_conflict_state()
    if operation is None:
        typer.echo("No merge/rebase/cherry-pick in progress")
        return

    remaining = list_conflicted_files()
    if remaining:
        typer.echo(f"Error: {len(remaining)} conflicted file(s) remain:", err=True)
        for f in remaining:
            typer.echo(f"  {f}", err=True)
        raise typer.Exit(1)

    match operation:
        case "merge":
            cmd_desc = "git commit --no-edit"
        case "rebase":
            cmd_desc = "git rebase --continue"
        case "cherry-pick":
            cmd_desc = "git cherry-pick --continue"
        case _:
            typer.echo(f"Error: unknown operation {operation!r}", err=True)
            raise typer.Exit(1)

    if dry_run:
        typer.echo(f"Would run: {cmd_desc}")
        return

    try:
        match operation:
            case "merge":
                git("commit", "--no-edit")
            case "rebase":
                git("rebase", "--continue")
            case "cherry-pick":
                git("cherry-pick", "--continue")
        typer.echo(f"Completed {operation}")
    except sh.ErrorReturnCode as e:
        typer.echo(f"Failed to complete {operation}: {decode_stderr(e)}", err=True)
        raise typer.Exit(1)
