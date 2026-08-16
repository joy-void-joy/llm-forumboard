"""Audit repository source for missing and spurious `# lup: ignore` markers.

Backs `lup-devtools dev check --antipatterns` (and the standalone
`dev check`-row). Walks every tracked or untracked `.py`/TS-family file and
runs the single `lup.codescan.antipatterns` set over it — the same set the
edit hook enforces — reporting three classes the hook cannot catch after the
fact:

- **missing**: a line trips a rule but carries no `# lup: ignore` covering it
  (it slipped in past the hook, or predates the rule). Blocking.
- **spurious**: a `# lup: ignore[id]` (or a bare one) guards a rule the line
  does not trip — a dead directive to delete. Blocking.
- **untyped**: a bare `# lup: ignore` validly silences a line but names no
  rule. Advisory (does not fail the check) — surfaced so the migration to
  typed `# lup: ignore[id]` directives is gradual.

The set is shared with the hook; the verdicts need not be. This sweep reads
whole parseable files, so it hands `lup.codescan.grammar` a type oracle and
decides some rules more narrowly than a hook judging an untyped edit fragment
ever could. Every finding that narrowing drops is reported as a **refuted**
row carrying the declaration that settled it, and the directive that used to
guard it turns up as spurious on the next line of the report.
"""

from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence, Set as AbstractSet
from pathlib import Path

import typer
from pydantic import BaseModel

from lup.codescan.antipatterns import (
    AntiPattern,
    AntiPatternFinding,
    AntiPatternSet,
    audit_text,
    patterns_for_suffix,
)
from lup.codescan.behaviour import audit_model_free_functions
from lup.codescan.boundaries import audit_constant_declarations, audit_path_boundaries
from lup.codescan.capabilities import audit_capabilities
from lup.codescan.common import (
    PACKAGE_ROOTS,
    PythonContext,
    PythonSource,
    Refutation,
    file_level_ignore,
    module_name,
)
from lup.codescan.dispatch import audit_own_model_dispatch
from lup.codescan.grammar import refute
from lup.codescan.narrowing import audit_isinstance_chains
from lup.codescan.registry import RULE_REFERENCE
from lup.policy.kernel.edit import (
    IGNORE_RE,
    SUPPRESSION_COLUMN_LIMIT,
    inline_suppression,
    relocated_suppressions,
    standalone_suppression,
)
from lup.policy.kernel.roles import path_role
from lup.devtools.dev.pyright_oracle import default_oracle
from lup.devtools.project import DevProject
from lup.devtools.utils import git, output_json


# lup: solved: these commands are workflow, not domain, and belong beside the
# rest of the CLI in `lup.devtools` — which they cannot join while they reach
# into this application's harness declaration by name. The import is deferred
# to the call so the roster can carry its own Typer apps without a cycle; the
# fix is a typed project seam the application supplies, not a later import.
def scanned_roots(project: DevProject) -> AbstractSet[str]:
    """The import roots a repository's scans resolve module names against.

    The library knows its own; the application names the package it publishes,
    so renaming it during initialization moves the root with it rather than
    leaving scans resolving against a package that is gone.
    """
    return PACKAGE_ROOTS | {project.package}


class FoundAntiPattern(AntiPatternFinding):
    """An :class:`~lup.codescan.antipatterns.AntiPatternFinding` tagged with its file."""

    file: str


class FoundRefutation(Refutation, frozen=True):
    """A :class:`~lup.codescan.common.Refutation` tagged with its file."""

    file: str


class ScannedFile(BaseModel, arbitrary_types_allowed=True):
    """One tracked file the sweep reads once and audits against its table."""

    rel: str
    path: Path
    patterns: list[AntiPattern]
    text: str


class AntiPatternScan(BaseModel):
    """One repository sweep: the findings it kept, and the ones it refuted."""

    findings: list[FoundAntiPattern]
    refuted: list[FoundRefutation]


def scanned_files(
    project: DevProject, paths: Sequence[str] | None = None
) -> list[ScannedFile]:
    """Every tracked production file the audits read, with its table and text.

    ``paths`` narrows the walk to files under the given repository-relative
    prefixes; ``None`` is the whole repository, and an empty scope is a scope
    rather than an absent one, so a tree that changed nothing is read for
    nothing. The declared path roles decide the rest: a rule the edit hook
    never enforces in a test or scratch tree is not read there either.
    """
    roles = project.path_roles
    declared = AntiPatternSet().selected(project.rules)

    def found() -> Iterator[ScannedFile]:
        for rel in git.lines("ls-files", "--cached", "--others", "--exclude-standard"):
            path = Path(rel)
            patterns = patterns_for_suffix(path.suffix.lower(), declared)
            if patterns is None or path_role(rel, roles) != "production":
                continue
            if paths is not None and not any(
                rel == p or rel.startswith(f"{p}/") for p in paths
            ):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            yield ScannedFile(rel=rel, path=path, patterns=patterns, text=text)

    return list(found())


def scan_antipatterns(
    project: DevProject, paths: Sequence[str] | None = None
) -> AntiPatternScan:
    """Every missing/spurious marker across tracked production `.py`/TS files.

    ``paths`` narrows the sweep to the files under the given repository-relative
    prefixes, for the fix-one-file loop where a whole-repository resolve is the
    dominant cost, and for a caller answerable only for what it changed. It
    only decides which files are read: each one still audits against the same
    tables, and the oracle still resolves them against the whole project, so a
    scoped verdict matches the sweep's verdict for that file. ``None`` is the
    whole repository; naming no path scopes the sweep to nothing, which is
    what a tree that changed nothing is answerable for.

    The audit reads the same declared path roles the edit hook does, so a rule
    the hook never enforces in a test or scratch tree is not reported there
    either. A gate that fires only after the fact is a gate agents cannot act
    on.

    The typed grammar resolves receivers before the tables run, so a finding
    a type oracle proved the rule is not about never reaches a verdict — and
    a `# lup: ignore` that guarded one becomes the dead directive the audit
    reports. Resolution costs a language-server session and `dev check` pays
    it every run; where the checker is absent the grammar refutes nothing and
    every broad regex verdict stands.
    """
    scanned = scanned_files(project, paths)
    sources = [
        PythonSource(
            path=item.path,
            module=module_name(item.path, scanned_roots(project)),
            text=item.text,
        )
        for item in scanned
        if item.path.suffix.lower() in {".py", ".pyi"}
    ]
    refuted = refute(sources, default_oracle())
    results = [
        FoundAntiPattern(file=item.rel, **finding.model_dump())
        for item in scanned
        for finding in audit_text(
            item.text,
            item.patterns,
            refuted[item.path.as_posix()] if item.path.as_posix() in refuted else None,
        )
    ]
    results.extend(
        FoundAntiPattern(
            file=finding.path.as_posix(),
            kind=finding.kind,
            line=finding.line,
            text="",
            message=finding.message,
            rule_id=finding.rule_id,
        )
        for finding in [
            *audit_capabilities(sources),
            *audit_model_free_functions(sources),
            *audit_own_model_dispatch(sources),
            *audit_isinstance_chains(sources),
            *audit_constant_declarations(sources, project.roots),
        ]
    )
    boundary_findings = [
        (source.path, finding)
        for source in sources
        for finding in audit_path_boundaries(source.path, source.text, project.roots)
    ]
    foreign_untyped = {
        (path.as_posix(), finding.line)
        for path, finding in boundary_findings
        if finding.kind == "untyped"
    }
    results = [
        finding
        for finding in results
        if not (
            finding.kind == "spurious"
            and not finding.rule_id
            and (finding.file, finding.line) in foreign_untyped
        )
    ]
    results.extend(
        FoundAntiPattern(
            file=path.as_posix(),
            kind=finding.kind,
            line=finding.line,
            text=finding.text,
            message=finding.message,
            rule_id=finding.rule_id,
        )
        for path, finding in boundary_findings
    )
    return AntiPatternScan(
        findings=[
            finding for finding in results if project.rules.keeps(finding.rule_id)
        ],
        refuted=[
            FoundRefutation(file=file, **refutation.model_dump())
            for file, refutations in sorted(refuted.items())
            for refutation in refutations
        ],
    )


class DirectiveSite(BaseModel, frozen=True):
    """One `# lup: ignore`, where it sits and how wide each placement makes it.

    ``inline_width`` is what the canonical placement would cost: the width of
    the line as written when the directive is already on it, and the width of
    the merged line when the directive stands above one. A site with nothing
    beneath it to merge into reports its own width, because there is no
    inline form of it to measure.
    """

    file: str
    line: int
    standalone: bool
    width: int
    inline_width: int
    text: str


def directive_sites(rel: str, text: str) -> list[DirectiveSite]:
    """Every real directive in one file, measured against both placements.

    The file-level opt-out is skipped: it governs the whole file rather than
    a line, so neither placement is about it.
    """
    context = PythonContext.parse(text)
    file_ignore = file_level_ignore(text)
    lines = text.splitlines()

    def found() -> Iterator[DirectiveSite]:
        for number, line in enumerate(lines, start=1):
            match = IGNORE_RE.search(line)
            if match is None or not context.comment_at(number, match.start()):
                continue
            if file_ignore is not None and number == file_ignore.line:
                continue
            standalone = standalone_suppression(line) is not None
            below = lines[number] if standalone and number < len(lines) else ""
            merged = inline_suppression(below, line) if below.strip() else line
            yield DirectiveSite(
                file=rel,
                line=number,
                standalone=standalone,
                width=len(line),
                inline_width=len(merged) if standalone else len(line),
                text=line.strip(),
            )

    return list(found())


def place_directives(
    project: DevProject, limit: int = SUPPRESSION_COLUMN_LIMIT
) -> list[str]:
    """Put every directive at its canonical placement, reporting each file moved.

    Nothing the audit decides turns on this: both placements are accepted, so
    the move only settles which one a file uses. It exists so no author weighs
    a reason against a column count — the reason gets written, and whichever
    placement holds it whole is the one it lands in.
    """

    def moved() -> Iterator[str]:
        for item in scanned_files(project):
            if item.path.suffix.lower() not in {".py", ".pyi"}:
                continue
            revised = relocated_suppressions(item.text, limit)
            if revised == item.text:
                continue
            item.path.write_text(revised, encoding="utf-8")
            yield item.rel

    return list(moved())


def report_directives(
    project: DevProject, as_json: bool, limit: int = SUPPRESSION_COLUMN_LIMIT
) -> None:
    """Measure every directive in the tree against the canonical placement.

    What the report answers is whether the canonical inline placement can
    actually hold a directive and its reason: how many sites fit the column
    budget inline, and how many only fit standing above. A tree whose
    overflow concentrates in the longest reasons is a tree whose fallback is
    carrying the justifications that say the most.
    """
    sites = [
        site
        for item in scanned_files(project)
        for site in directive_sites(item.rel, item.text)
    ]
    inline = [site for site in sites if not site.standalone]
    above = [site for site in sites if site.standalone]
    overflow = [site for site in sites if site.inline_width > limit]
    if as_json:
        output_json(
            {
                "limit": limit,
                "total": len(sites),
                "inline": len(inline),
                "inline_overflowing": sum(1 for s in inline if s.inline_width > limit),
                "above": len(above),
                "above_fitting_inline": sum(
                    1 for s in above if s.inline_width <= limit
                ),
                "sites": [site.model_dump() for site in sites],
            }
        )
        return
    typer.echo(f"{len(sites)} directive(s), budget {limit} columns")
    typer.echo(
        f"  inline: {len(inline)} — "
        f"{sum(1 for s in inline if s.inline_width <= limit)} fit, "
        f"{sum(1 for s in inline if s.inline_width > limit)} already over"
    )
    typer.echo(
        f"  above:  {len(above)} — "
        f"{sum(1 for s in above if s.inline_width <= limit)} would fit inline, "
        f"{sum(1 for s in above if s.inline_width > limit)} only fit above"
    )
    for site in sorted(overflow, key=lambda site: site.inline_width, reverse=True):
        typer.echo(f"{site.file}:{site.line} [{site.inline_width}] {site.text}")


ADVISORY_KINDS = {"untyped"}


def summarize(
    project: DevProject,
    as_json: bool,
    paths: Sequence[str] | None = None,
    advisory: AbstractSet[str] = ADVISORY_KINDS,
) -> None:
    """Tally anti-pattern findings by rule and kind — the sweep triage view.

    A per-rule count (most-frequent first, with how many files each spans) and
    a per-kind split, so a cleanup targets the noisiest rules first instead of
    reading the whole listing, plus how many findings the typed grammar
    refuted. ``--json`` emits the same tally for tooling.
    """
    scan = scan_antipatterns(project, paths)
    found = scan.findings
    by_rule: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    files_by_rule: defaultdict[str, list[str]] = defaultdict(list)
    for finding in found:
        rule = finding.rule_id or "(bare)"
        by_rule[rule] += 1
        by_kind[finding.kind] += 1
        if finding.file not in files_by_rule[rule]:
            files_by_rule[rule].append(finding.file)

    blocking = sum(1 for finding in found if finding.kind not in advisory)
    file_count = len(dict.fromkeys(finding.file for finding in found))

    if as_json:
        output_json(
            {
                "total": len(found),
                "blocking": blocking,
                "files": file_count,
                "refuted": len(scan.refuted),
                "by_kind": dict(by_kind),
                "by_rule": {
                    rule: {"count": count, "files": len(files_by_rule[rule])}
                    for rule, count in by_rule.most_common()
                },
            }
        )
        return

    if not found:
        typer.echo("No anti-pattern findings.")
        return
    typer.echo(
        f"{len(found)} findings ({blocking} blocking) across {file_count} file(s)"
    )
    typer.echo("  by kind: " + ", ".join(f"{k}={v}" for k, v in by_kind.most_common()))
    typer.echo("  by rule:")
    for rule, count in by_rule.most_common():
        typer.echo(f"    {count:5}  {rule}  ({len(files_by_rule[rule])} file(s))")
    if scan.refuted:
        typer.echo(f"  refuted by receiver type: {len(scan.refuted)}")
    typer.echo(f"Rule reference: {RULE_REFERENCE} (`uv run lup-devtools dev rules`)")


def report(
    project: DevProject,
    as_json: bool,
    paths: Sequence[str] | None = None,
    advisory: AbstractSet[str] = ADVISORY_KINDS,
) -> None:
    """List anti-pattern findings; exit non-zero when a blocking one remains.

    "untyped" findings are advisory (a bare `# lup: ignore` to migrate to a
    typed one) and never fail the command; "missing" and "spurious" do. Every
    finding the typed grammar refuted is listed with the declaration that
    settled it, so a dropped verdict is accountable rather than invisible.
    """
    scan = scan_antipatterns(project, paths)
    found = scan.findings
    blocking = [finding for finding in found if finding.kind not in advisory]
    if as_json:
        output_json(
            {
                "findings": [finding.model_dump() for finding in found],
                "refuted": [refutation.model_dump() for refutation in scan.refuted],
            }
        )
        if blocking:
            raise typer.Exit(1)
        return
    for refutation in scan.refuted:
        typer.echo(
            f"{refutation.file}:{refutation.line} [refuted {refutation.rule_id}] "
            f"{refutation.evidence}"
        )
    if not found:
        typer.echo("No anti-pattern findings.")
        return
    for finding in found:
        typer.echo(f"{finding.file}:{finding.line} [{finding.kind}] {finding.message}")
        typer.echo(f"    {finding.text}")
    files = {finding.file for finding in found}
    reported = len(found) - len(blocking)
    tail = f" (+{reported} untyped, advisory)" if reported else ""
    typer.echo(f"\n{len(blocking)} blocking finding(s){tail} in {len(files)} file(s)")
    typer.echo(f"Rule reference: {RULE_REFERENCE} (`uv run lup-devtools dev rules`)")
    if blocking:
        raise typer.Exit(1)
