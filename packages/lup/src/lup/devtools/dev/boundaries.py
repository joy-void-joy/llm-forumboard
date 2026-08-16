"""Walk repository Python files for the boundary rules in both directions.

Backs ``lup-devtools dev check --boundaries`` and ``--placement`` plus their
standalone check rows. Inward, every git-tracked ``.py`` file outside the
sanctioned homes (the adapters package, tests) runs through
:mod:`lup.codescan.boundaries`; the tree is expected to hold zero breaches, and
this is the regression guard that keeps backend dispatch from creeping back
outside the seam. Outward, every library module is checked for data tables an
adopter cannot replace — those are open placement debt, so that row names the
tables still to be moved rather than asserting zero.
"""

from pathlib import Path

import typer

from pydantic import BaseModel

from lup.codescan.boundaries import (
    KERNEL_ROOT,
    LIBRARY_ROOT,
    BoundaryBreach,
    audit_path_boundaries,
    default_position_names,
    find_kernel_import_breaches,
    find_library_default_breaches,
    library_placement_path_is_audited,
)
from lup.devtools.project import DevProject
from lup.devtools.utils import git, output_json


class FoundBreach(BoundaryBreach):
    """A :class:`~lup.codescan.boundaries.BoundaryBreach` tagged with its file."""

    file: str


class TrackedSource(BaseModel):
    """One git-tracked Python file, read once for every scan over the tree."""

    rel: str
    path: Path
    text: str


def tracked_python_sources() -> list[TrackedSource]:
    """Every tracked Python file that exists on disk, with its text."""
    tracked = str(
        git("ls-files", "--cached", "--others", "--exclude-standard")
    ).splitlines()
    return [
        TrackedSource(rel=rel, path=path, text=path.read_text(encoding="utf-8"))
        for rel in tracked
        if (path := Path(rel)).suffix == ".py" and path.exists()
    ]


def library_sources() -> list[TrackedSource]:
    """Every tracked Python file that ships inside ``packages/lup``."""
    return [
        source
        for source in tracked_python_sources()
        if source.rel.startswith(LIBRARY_ROOT)
    ]


def overridable_names(
    sources: list[TrackedSource],
) -> set[str]:  # lup: ignore[set-shape] — name identity membership
    """Constant names a caller can replace, pooled across whole modules."""
    return {name for source in sources for name in default_position_names(source.text)}


def scan_library_placement() -> list[FoundBreach]:
    """Every library data table no adopter can replace, across ``packages/lup``.

    Whether a table is reachable as an overridable default is a property of the
    library as a whole, not of the module that writes it down, so the names
    callers can replace are collected across every library module — adapters
    included — before any one module is judged against them.
    """
    sources = library_sources()
    overridable = overridable_names(sources)
    return [
        FoundBreach(file=source.rel, **breach.model_dump())
        for source in sources
        if library_placement_path_is_audited(source.path)
        for breach in find_library_default_breaches(source.text, overridable)
    ]


class PortableModule(BaseModel):
    """One application module whose imports never reach the application."""

    file: str
    imports: int


def application_imports(text: str, package: str) -> list[str]:
    """Every import in one module that names the application package."""
    return [
        line
        for line in text.splitlines()
        if line.startswith((f"from {package}", f"import {package}"))
    ]


def scan_application_placement(project: DevProject) -> list[PortableModule]:
    """Every devtools module that could be library code as it stands.

    The library row asks whether a library module baked in a choice an
    adopter cannot replace. This asks the mirror question, and it is the one
    that decides what a downstream project keeps receiving: this template is
    copied and renamed, so a module living here is frozen for every adopter
    at the moment they copy it, while one in ``packages/lup`` reaches them
    through an ordinary dependency bump.

    A module that imports nothing from the application is, by the placement
    test the conventions already state, library code sitting in the
    application. Reported rather than failed, because moving one is a change
    with its own review — this row names the debt and shrinks.

    The declared prose roots are exempt, and not as a concession: a content
    module is this project's own judgement written as data, so importing
    nothing is what it looks like when it is exactly where it belongs. The
    same holds for the declaration this project compiles its harness from.
    """
    package = project.package
    root = f"src/{package}/devtools/"
    exempt = (
        f"{root}harness/content/",
        f"{root}harness/catalog.py",
        f"{root}subapps.py",
    )
    return [
        PortableModule(file=source.rel, imports=0)
        for source in tracked_python_sources()
        if source.rel.startswith(root)
        and not source.rel.startswith(exempt)
        and source.path.name != "__init__.py"
        and not application_imports(source.text, package)
    ]


def scan_boundaries(project: DevProject) -> list[FoundBreach]:
    """Every native import, spelling, and kernel-import breach in the tree."""
    found: list[FoundBreach] = []  # lup: ignore[empty-collection]
    roots = project.roots
    for source in tracked_python_sources():
        found.extend(
            FoundBreach(
                file=source.rel,
                line=finding.line,
                module=finding.module,
                text=finding.text,
            )
            for finding in audit_path_boundaries(source.path, source.text, roots)
            if finding.kind == "missing"
        )
        if source.rel.startswith(KERNEL_ROOT):
            found.extend(
                FoundBreach(file=source.rel, **breach.model_dump())
                for breach in find_kernel_import_breaches(source.text)
            )
    return found


def report(project: DevProject, as_json: bool) -> None:
    """List every breach; exit non-zero when any exist."""
    found = scan_boundaries(project)
    if as_json:
        output_json([breach.model_dump() for breach in found])
    elif found:
        for breach in found:
            typer.echo(f"{breach.file}:{breach.line}  {breach.module}")
    else:
        typer.echo("seam boundaries: ok")
    if found:
        raise typer.Exit(1)


def report_placement(as_json: bool) -> None:
    """List every baked-in library table; exit non-zero when any exist."""
    found = scan_library_placement()
    if as_json:
        output_json([breach.model_dump() for breach in found])
    elif found:
        for breach in found:
            typer.echo(f"{breach.file}:{breach.line}  {breach.module}")
    else:
        typer.echo("library placement: ok")
    if found:
        raise typer.Exit(1)
