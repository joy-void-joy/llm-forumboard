"""Unified pre-flight checks: ruff, pyright, pytest."""

from pathlib import Path

import sh
import typer
from pydantic import BaseModel

from lup.adapters.harness import claude_prompt_renderer, codex_prompt_renderer
from lup.codescan.markers import find_feedback
from lup.harness.models import GUIDANCE_BYTE_BUDGET, PromptDocument, document_byte_size
from lup.workspace.paths import is_template_scaffold, project_root

from lup.devtools.dev.antipatterns import FoundAntiPattern, scan_antipatterns
from lup.devtools.project import DevProject
from lup.devtools.dev.boundaries import (
    scan_boundaries,
    scan_application_placement,
    scan_library_placement,
)
from lup.devtools.dev.branches import unlanded_siblings
from lup.devtools.dev.comments import FoundComment, scan_tracked
from lup.devtools.harness.drift import (
    RepositoryWriter,
    inspect_drift,
    report_stale,
)
from lup.devtools.harness.generate import NativeHarnessComposition
from lup.devtools.utils import decode_stderr, git, uv

# The suite waits on git subprocesses and hook scripts far more than it
# computes, so it parallelizes well — but each worker pays a full interpreter
# boot and package import, and past roughly this many that startup costs more
# than the concurrency returns. `-n auto` on a large host is slower than serial
# arithmetic suggests, so the count is bounded rather than derived from cores.
TEST_WORKERS = 8


class CheckOutcome(BaseModel):
    """One pre-flight check's name and whether it passed."""

    name: str
    passed: bool


class ScopedFindings(BaseModel):
    """A sweep split into what a check answers for, and what it only reports."""

    owned: list[FoundAntiPattern]
    outside: list[FoundAntiPattern]


class TestRoot(BaseModel):
    """One independently installed test suite the project asks the gate to run."""

    name: str
    directory: Path


def inline_notes_lines(found: list[FoundComment], scaffold: bool = False) -> list[str]:
    """The inline-notes header and detail lines.

    Advisory rather than gating: a note is a standing request to somebody, and
    the tree is expected to carry open ones for as long as the work they name
    is open. Failing on them would make every branch red for a condition its
    author chose deliberately, so this reports and the reader decides. Their
    `deferred` lines render after the unresolved ones, carrying the gate a
    bracketed deferral stated, so what is still being asked reads first.

    Customization markers read two ways, and *scaffold* says which. In the
    scaffold itself they are inventory — counted, never listed, because a
    permanent wall of text would sit in front of the notes somebody is
    actually owed, and `dev todos` exists to walk them. In a repository that
    adopted the template they are decisions nobody has made yet, so they list
    like any other note. Advisory either way: a domain that means to leave one
    standing writes `# lup: defer:`, and that is the sentence it should have
    to write rather than a red branch it learns to ignore.
    """
    unresolved = [comment for comment in found if comment.kind in ("note", "solved")]
    deferred = [comment for comment in found if comment.kind == "defer"]
    customization = [comment for comment in found if comment.kind == "template"]
    counts = f"{len(unresolved)} unresolved"
    if deferred:
        counts += f", {len(deferred)} deferred"
    if customization:
        counts += f", {len(customization)} customization"
    lines = [f"inline notes: {counts} (advisory)"]
    lines.extend(
        f"  {comment.file}:{comment.start_line}-{comment.end_line}"
        for comment in unresolved
    )
    lines.extend(
        f"  {comment.deferral_label()} "
        f"{comment.file}:{comment.start_line}-{comment.end_line}"
        for comment in deferred
    )
    lines.extend(
        f"  customization {comment.file}:{comment.start_line}-{comment.end_line}"
        for comment in ([] if scaffold else customization)
    )
    return lines


def changed_paths(since: str) -> list[str]:
    """Every tracked path this tree changed since a ref, as posix strings.

    A ref git cannot resolve refuses the run rather than answering nothing.
    The two readings are indistinguishable once the exit status is dropped —
    an empty answer is exactly what a tree that changed nothing gives — and
    the scope this builds decides which files the blocking gates read. A
    mistyped ref would scope them to none of them and report ok.
    """
    try:
        named = git.lines("diff", "--name-only", since, _ok_code=[0])
    except sh.ErrorReturnCode as error:
        raise typer.BadParameter(
            f"--since {since!r} does not name a commit in this tree: "
            f"{decode_stderr(error)}"
        ) from error
    return [line for line in named if line]


def owned_comments(
    found: list[FoundComment], scope: list[str] | None
) -> list[FoundComment]:
    """Which unresolved notes this check is answerable for.

    A resolver worker's own notes are already cleared from its worktree
    before it starts, so every note it can still see belongs to a sibling
    concern it has no lease on. Reporting the whole tree would tell it about
    work it cannot touch; reporting what it changed says the only thing it
    can act on, which is whether it left a note in its own code.
    """
    if scope is None:
        return found
    owned = dict.fromkeys(scope)
    return [item for item in found if str(item.file) in owned]


def owned_findings(
    findings: list[FoundAntiPattern], scope: list[str] | None
) -> ScopedFindings:
    """Which anti-pattern findings this check is answerable for.

    A lease holds one concern's changes, so its gate answers "is this change
    good?". Answering "is the whole repository clean?" makes every lease's
    verdict depend on state no worker in the run controls, and one pre-existing
    finding then blocks every lease at once with nothing a revision can do
    about it. What lies outside is still named, so a worker learns the tree has
    an issue rather than reading it as clean.
    """
    if scope is None:
        return ScopedFindings(owned=findings, outside=[])
    owned = dict.fromkeys(scope)
    return ScopedFindings(
        owned=[item for item in findings if item.file in owned],
        outside=[item for item in findings if item.file not in owned],
    )


def run_checks(
    fix: bool,
    no_test: bool,
    project: DevProject,
    test_roots: list[TestRoot],
    compositions: list[NativeHarnessComposition],
    repository_writers: list[RepositoryWriter],
    guidance: PromptDocument,
    scope: list[str] | None = None,
    test_workers: int = TEST_WORKERS,
) -> None:
    """Run ruff format, ruff check, pyright, and pytest in sequence.

    Read-only by default (reports issues without modifying files).
    Pass *fix* to auto-fix formatting and lint issues. ``scope`` narrows the
    note and anti-pattern gates to paths this tree is answerable for.
    """
    results: list[CheckOutcome] = []

    # ruff format
    try:
        if not fix:
            uv("run", "ruff", "format", "--check", ".")
            typer.echo("ruff format: ok")
        else:
            uv("run", "ruff", "format", ".")
            typer.echo("ruff format: applied")
        results.append(CheckOutcome(name="ruff format", passed=True))
    except sh.ErrorReturnCode as e:
        typer.echo("ruff format: FAIL")
        if e.stdout:
            typer.echo(e.stdout.decode().rstrip())
        results.append(CheckOutcome(name="ruff format", passed=False))

    # ruff check
    try:
        args = ["run", "ruff", "check", "."]
        if fix:
            args.append("--fix")
        uv(*args)
        typer.echo("ruff check: ok")
        results.append(CheckOutcome(name="ruff check", passed=True))
    except sh.ErrorReturnCode as e:
        typer.echo("ruff check: FAIL")
        if e.stdout:
            typer.echo(e.stdout.decode().rstrip())
        results.append(CheckOutcome(name="ruff check", passed=False))

    if fix:
        changed = git.lines("diff", "--name-only", _ok_code=[0])
        if changed:
            typer.echo(f"  auto-fixed {len(changed)} file(s)")
            for f in changed:
                typer.echo(f"    {f}")

    # pyright
    try:
        uv("run", "pyright")
        typer.echo("pyright: ok")
        results.append(CheckOutcome(name="pyright", passed=True))
    except sh.ErrorReturnCode as e:
        typer.echo("pyright: FAIL")
        if e.stdout:
            typer.echo(e.stdout.decode().rstrip())
        results.append(CheckOutcome(name="pyright", passed=False))

    # pytest, twice. The library ships to an index without the application
    # beside it, so its suite is run from its own directory where `src` is all
    # it can see — a library test reaching for a template fixture passes at
    # the root and fails there, which is the only place that difference shows.
    if not no_test:
        for test_root in test_roots:
            try:
                uv(
                    "run",
                    "pytest",
                    "-n",
                    str(test_workers),
                    _cwd=str(test_root.directory),
                )
                typer.echo(f"{test_root.name}: ok")
                results.append(CheckOutcome(name=test_root.name, passed=True))
            except sh.ErrorReturnCode as e:
                typer.echo(f"{test_root.name}: FAIL")
                if e.stdout:
                    typer.echo(e.stdout.decode().rstrip())
                results.append(CheckOutcome(name=test_root.name, passed=False))

    # advisory — a note asks somebody for something, and a tree is expected to
    # carry open ones; what it says is worth reading, not worth refusing over
    found = owned_comments(scan_tracked(find_feedback), scope)
    scaffold = is_template_scaffold(project_root())
    listed = inline_notes_lines(found, scaffold) if found else ["inline notes: none"]
    for line in listed:
        typer.echo(line)

    scan = scan_antipatterns(project)
    scoped = owned_findings(scan.findings, scope)
    blocking = [f for f in scoped.owned if f.kind != "untyped"]
    refined = f", {len(scan.refuted)} refuted" if scan.refuted else ""
    if blocking:
        typer.echo(f"antipatterns: FAIL ({len(blocking)} finding(s){refined})")
        for finding in blocking:
            typer.echo(f"  {finding.file}:{finding.line} [{finding.kind}]")
        results.append(CheckOutcome(name="antipatterns", passed=False))
    else:
        advisory = len(scoped.owned) - len(blocking)
        tail = f" ({advisory} untyped, advisory{refined})" if advisory else refined
        typer.echo(f"antipatterns: ok{tail}")
        results.append(CheckOutcome(name="antipatterns", passed=True))
    for finding in scoped.outside:
        typer.echo(
            f"  outside this scope: {finding.file}:{finding.line} [{finding.kind}]"
        )

    breaches = scan_boundaries(project)
    if breaches:
        typer.echo(f"seam boundaries: FAIL ({len(breaches)} breach(es))")
        for breach in breaches:
            typer.echo(f"  {breach.file}:{breach.line}  {breach.module}")
        results.append(CheckOutcome(name="seam boundaries", passed=False))
    else:
        typer.echo("seam boundaries: ok")
        results.append(CheckOutcome(name="seam boundaries", passed=True))

    tables = scan_library_placement()
    if tables:
        typer.echo(f"library placement: FAIL ({len(tables)} baked-in table(s))")
        for table in tables:
            typer.echo(f"  {table.file}:{table.line}  {table.module}")
        results.append(CheckOutcome(name="library placement", passed=False))
    else:
        typer.echo("library placement: ok")
        results.append(CheckOutcome(name="library placement", passed=True))

    portable = scan_application_placement(project)
    if portable:
        typer.echo(f"application placement: {len(portable)} portable module(s)")
        for module in portable:
            typer.echo(f"  {module.file}")
    else:
        typer.echo("application placement: ok")
    results.append(CheckOutcome(name="application placement", passed=True))

    # The same reading the commit hook and the pipeline refuse on, asked here
    # rather than recomposed, so a tree cannot be stale at one gate and current
    # at another.
    drift = inspect_drift(compositions, repository_writers)
    if drift.clean:
        typer.echo("harness drift: ok")
    else:
        typer.echo(f"harness drift: FAIL ({len(drift.stale_trees)} tree(s))")
        report_stale(drift)
    results.append(CheckOutcome(name="harness drift", passed=drift.clean))

    used = max(
        document_byte_size(claude_prompt_renderer().render(guidance)),
        document_byte_size(codex_prompt_renderer().render(guidance)),
    )
    free = GUIDANCE_BYTE_BUDGET - used
    state = "ok" if free >= 0 else f"FAIL (over by {-free})"
    typer.echo(
        f"guidance budget: {state} — {used}/{GUIDANCE_BYTE_BUDGET} bytes, {free} free"
    )
    results.append(CheckOutcome(name="guidance budget", passed=free >= 0))

    # advisory — reports another tree's state, so it never gates this one
    unlanded = unlanded_siblings()
    if unlanded:
        typer.echo(f"unlanded siblings: {len(unlanded)} (advisory)")
        for branch in unlanded:
            typer.echo(
                f"  {branch.name}  {branch.unique_commits} commits, "
                f"{branch.source_diff_lines} ln"
            )

    # summary
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    typer.echo(f"\n{passed}/{total} checks passed")

    failed = [r.name for r in results if not r.passed]
    if failed:
        typer.echo(f"Failed: {', '.join(failed)}")
        raise typer.Exit(1)
