"""This project's `dev` tree: the library's, plus what only a template has.

The workflow commands — worktrees, branches, PRs, the quality gate — are the
library's, composed here over what this repository declares about itself.
The two added below have template-ness as their subject: renaming the package
an adopter inherits, and choosing where lup itself is resolved from. Neither
means anything inside a project that has already been initialized once.
"""

from pathlib import Path
from typing import Annotated

import typer

import lup.devtools.dev.check as check
import lup_template.devtools.dev.init as init
import lup_template.devtools.dev.library as library
import lup_template.devtools.harness.catalog as catalog
from lup.devtools.dev.app import DevDeclarations, create_dev_app
from lup_template.devtools.harness.composition import REPOSITORY_WIDE, TARGETS
from lup_template.devtools.harness.content.guidance import document as guidance_document


def declared() -> DevDeclarations:
    """What this repository tells the dev tree, read where a command runs.

    Both test suites are installed separately — the workspace root and the
    vendored library — so the gate runs pytest once per root rather than
    reporting a green tree that never exercised half of it.
    """
    return DevDeclarations(
        project=catalog.dev_project(),
        hooks=catalog.declared_hook_set(),
        plugin=catalog.declared_plugin(),
        test_roots=[
            check.TestRoot(name="pytest", directory=Path.cwd()),
            check.TestRoot(name="pytest (lup)", directory=Path("packages/lup")),
        ],
    )


app = create_dev_app(
    declared=declared,
    native_targets=TARGETS,
    repository_writers=REPOSITORY_WIDE,
    guidance=guidance_document(catalog.declared_hook_set().rules),
    relocate_roots=[
        Path("src"),
        Path("packages"),
        Path("tests"),
        Path("examples"),
    ],
)

init_app = typer.Typer(no_args_is_help=True)
library_app = typer.Typer(no_args_is_help=True)
app.add_typer(init_app, name="init", help="Project initialization")
app.add_typer(library_app, name="library", help="How this project obtains lup")


# -- init commands --


@init_app.command("rename-package")
def init_rename_package_cmd(
    new_name: Annotated[
        str,
        typer.Argument(
            help="New package name (valid Python identifier, e.g. 'aib', 'forecast_bot')"
        ),
    ],
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", "-n", help="Show what would change without modifying files"
        ),
    ] = False,
) -> None:
    """Rename the lup Python package to a project-specific name."""
    init.rename_package(new_name, dry_run)


# -- library commands --

DryRun = Annotated[
    bool,
    typer.Option("--dry-run", "-n", help="Show what would change without writing"),
]
KeepVendored = Annotated[
    bool,
    typer.Option("--keep-vendored", help=f"Leave {library.VENDORED_ROOT}/ on disk"),
]
Force = Annotated[
    bool,
    typer.Option("--force", help="Un-vendor even from an unrenamed template"),
]


@library_app.command("status")
def library_status_cmd() -> None:
    """Report where the lup library is resolved from."""
    library.library_status()


@library_app.command("release")
def library_release_cmd() -> None:
    """Ask the package index whether a release exists, and which mode that settles."""
    library.library_release()


@library_app.command("use")
def library_use_cmd(
    mode: Annotated[
        library.LibraryMode, typer.Argument(help="published, local, or linked")
    ],
    version: Annotated[
        str | None,
        typer.Option("--version", help="Lower version bound for the published release"),
    ] = None,
    keep_vendored: KeepVendored = False,
    force: Force = False,
    dry_run: DryRun = False,
) -> None:
    """Resolve lup from the package index, or from the vendored copy."""
    library.use_library(mode, version, keep_vendored, force, dry_run)


@library_app.command("git")
def library_git_cmd(
    url: Annotated[
        str, typer.Option("--url", help="Repository serving the lup package")
    ] = library.REPOSITORY_URL,
    branch: Annotated[
        str | None, typer.Option("--branch", help="Branch to resolve lup at")
    ] = None,
    tag: Annotated[
        str | None, typer.Option("--tag", help="Tag to resolve lup at")
    ] = None,
    rev: Annotated[
        str | None, typer.Option("--rev", help="Commit to pin lup at")
    ] = None,
    keep_vendored: KeepVendored = False,
    force: Force = False,
    dry_run: DryRun = False,
) -> None:
    """Resolve lup from its repository, for use before a release is published."""
    library.git_library(
        library.git_source(url, branch=branch, tag=tag, rev=rev),
        keep_vendored,
        force,
        dry_run,
    )


@library_app.command("link")
def library_link_cmd(
    checkout: Annotated[
        Path, typer.Argument(help="Path to a lup checkout holding packages/lup")
    ],
    keep_vendored: KeepVendored = False,
    force: Force = False,
    dry_run: DryRun = False,
) -> None:
    """Develop against a lup checkout so library changes land in its repo."""
    library.link_library(checkout, keep_vendored, force, dry_run)


@library_app.command("unlink")
def library_unlink_cmd(
    version: Annotated[
        str | None, typer.Option("--version", help="Lower version bound to restore")
    ] = None,
    dry_run: DryRun = False,
) -> None:
    """Stop developing against a checkout and go back to the published release."""
    library.use_library(library.LibraryMode.PUBLISHED, version, True, True, dry_run)
