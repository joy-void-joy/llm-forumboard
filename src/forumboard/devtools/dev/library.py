"""How this project obtains the ``lup`` library.

A project built on this template reaches ``lup`` one of four ways, and the
mode is a property of ``pyproject.toml`` that can be changed at any time:

``published``
    The release from PyPI. Upgrading is ``uv lock --upgrade-package lup``
    plus a harness regeneration, rather than a merge against a vendored fork.
``git``
    The repository itself, resolved at a branch, tag, or commit. The default
    for a new project while no release is published: it gives an adopter the
    same package a release would, from the only place the library exists yet,
    so nothing has to be vendored to get it.
``local``
    A copy under ``packages/lup``, wired as a uv workspace member. What the
    template ships, and what a project that genuinely needs to modify library
    source keeps.
``linked``
    An editable install of a lup checkout elsewhere on disk. Library changes
    made while working on this project land in lup's own repository, which is
    how an improvement discovered downstream reaches the library.

Leaving ``local`` also strips the workspace wiring that stops resolving once
``packages/lup`` is gone: the uv workspace, the pytest source path, and the
pyright includes and execution environments rooted in the package.

Reading is a ``tomllib`` parse matched structurally; writing goes through
``tomlkit`` so comments and layout survive the edit.

Examples::

    $ uv run lup-devtools dev library status
    $ uv run lup-devtools dev library use published --version 0.3.0
    $ uv run lup-devtools dev library git --branch dev
    $ uv run lup-devtools dev library link ../lup.git/tree/dev
"""

import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Literal, TypedDict

import httpx
import tomlkit
import tomlkit.items
import typer
from pydantic import BaseModel, ValidationError
from importlib.metadata import version as installed_version
from packaging.requirements import Requirement

from lup.workspace.paths import find_project_root
from lup.devtools.utils import git

# The three below spell where the vendored copy sits, which is a fact about
# lup's own layout and this repository's, not a choice either end makes.
VENDORED_ROOT = "packages/lup"  # lup: ignore[constant-declaration] — fixed layout
VENDORED_SRC = "packages/lup/src"  # lup: ignore[constant-declaration] — fixed layout
# lup: ignore[constant-declaration] — fixed layout
VENDORED_SIBLINGS = {"src": VENDORED_SRC, "tests": f"{VENDORED_ROOT}/tests"}
"""Each plain search root and the vendored one that shadows it. A search path
naming the plain root wants its vendored twin exactly while the package is
there, and wants it gone the moment the package is not."""
# lup: ignore[constant-declaration] — the name the library is published under
DISTRIBUTION = "lup"
# lup: ignore[constant-declaration] — the glob this repository's own uv workspace
# is laid out as, which the manifest below already states
WORKSPACE_MEMBERS = ["packages/*"]
REPOSITORY_URL = "https://github.com/joy-void-joy/lup"
"""Where the library is published as source. Overridable: a fork, a mirror, or
a private host serves the same package from the same layout."""

PACKAGE_SUBDIRECTORY = VENDORED_ROOT
"""Where the distribution sits inside that repository — a fixed fact about
lup's own layout, not a choice an adopter makes."""


class ExecutionEnvironment(TypedDict):
    """One pyright execution environment: a root and its extra search paths."""

    root: str
    extraPaths: list[str]


# Type-checking a vendored adapter's dispatcher asset needs the generated
# runtime beside it on the search path. Both halves live under the package, so
# the pair exists exactly when the library is vendored.
# lup: ignore[constant-declaration] — the adapter packages lup actually ships,
# so the value follows lup.adapters rather than any taste
RUNTIMES = ("claude", "codex")
VENDORED_EXECUTION_ENVIRONMENTS = [
    ExecutionEnvironment(
        root=f"{VENDORED_SRC}/lup/adapters/{runtime}/assets",
        extraPaths=[
            f".{runtime}/plugins/lup/hooks/runtime",
            f"{VENDORED_SRC}/lup/policy/assets",
        ],
    )
    for runtime in RUNTIMES
]


class LibraryMode(StrEnum):
    """Where the ``lup`` distribution is resolved from."""

    PUBLISHED = "published"
    GIT = "git"
    LOCAL = "local"
    LINKED = "linked"


type GitRefKind = Literal["branch", "tag", "rev"]
"""Which kind of ref a git source pins, spelled as uv spells it."""


class GitSource(BaseModel, frozen=True):
    """A repository and the single ref of it a project resolves ``lup`` at.

    The ref is one field pair rather than three optional ones, so a source
    naming both a branch and a tag cannot be constructed — uv accepts only
    one, and a model that can hold two only moves the error later.
    """

    url: str = REPOSITORY_URL
    ref_kind: GitRefKind = "branch"
    ref: str = "main"

    def entry(self) -> tomlkit.items.InlineTable:
        """Render the ``[tool.uv.sources]`` value this source declares."""
        entry = tomlkit.inline_table()
        entry.update(
            {
                "git": self.url,
                self.ref_kind: self.ref,
                "subdirectory": PACKAGE_SUBDIRECTORY,
            }
        )
        return entry


class RefFlag(BaseModel, frozen=True):
    """One ref-kind flag, and the ref a command line gave it — or nothing."""

    kind: GitRefKind
    ref: str | None = None


def git_source(
    url: str,
    *,
    branch: str | None = None,
    tag: str | None = None,
    rev: str | None = None,
) -> GitSource:
    """Collapse the three ref flags into the one ref a git source may carry.

    A command line can spell all three; uv accepts one. Rejecting the excess
    here is what keeps :class:`GitSource` unable to represent the conflict.
    """
    named = [
        flag
        for flag in (
            RefFlag(kind="branch", ref=branch),
            RefFlag(kind="tag", ref=tag),
            RefFlag(kind="rev", ref=rev),
        )
        if flag.ref is not None
    ]
    match named:
        case []:
            return GitSource(url=url)
        case [only] if only.ref is not None:
            return GitSource(url=url, ref_kind=only.kind, ref=only.ref)
        case _:
            raise typer.BadParameter(
                "name one of --branch, --tag, or --rev, not "
                + " and ".join(f"--{flag.kind}" for flag in named)
            )


def read_mode(root: Path) -> LibraryMode:
    """Classify the acquisition mode ``pyproject.toml`` declares."""
    with (root / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    match data:
        case {"tool": {"uv": {"sources": {"lup": {"workspace": True}}}}}:
            return LibraryMode.LOCAL
        case {"tool": {"uv": {"sources": {"lup": {"path": str()}}}}}:
            return LibraryMode.LINKED
        case {"tool": {"uv": {"sources": {"lup": {"git": str()}}}}}:
            return LibraryMode.GIT
        case _:
            return LibraryMode.PUBLISHED


def read_git_source(root: Path) -> GitSource | None:
    """Return the repository and the ref it is pinned at, when git."""
    with (root / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    match data:
        case {"tool": {"uv": {"sources": {"lup": dict(source)}}}}:
            declared = source
        case _:
            return None
    match declared:
        case {"git": str(url), "branch": str(ref)}:
            return GitSource(url=url, ref_kind="branch", ref=ref)
        case {"git": str(url), "tag": str(ref)}:
            return GitSource(url=url, ref_kind="tag", ref=ref)
        case {"git": str(url), "rev": str(ref)}:
            return GitSource(url=url, ref_kind="rev", ref=ref)
        case {"git": str(url)}:
            return GitSource(url=url)
        case _:
            return None


def read_linked_path(root: Path) -> Path | None:
    """Return the checkout an editable source points at, when linked."""
    with (root / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    match data:
        case {"tool": {"uv": {"sources": {"lup": {"path": str(path)}}}}}:
            return Path(path)
        case _:
            return None


def requirement_for(entry: str, version: str | None) -> str:
    """Restate one requirement with, or without, a lower version bound.

    The extras the project asked for survive: only the specifier changes,
    because a source override supplies the version in every mode but
    ``published``.
    """
    requirement = Requirement(entry)
    extras = f"[{','.join(sorted(requirement.extras))}]" if requirement.extras else ""
    bound = f">={version}" if version is not None else ""
    return f"{requirement.name}{extras}{bound}"


def apply_dependency(document: tomlkit.TOMLDocument, version: str | None) -> list[str]:
    """Restate the ``lup`` requirement in ``[project].dependencies``."""
    dependencies = document["project"]["dependencies"]
    for index, entry in enumerate(dependencies):
        if Requirement(str(entry)).name != DISTRIBUTION:
            continue
        restated = requirement_for(str(entry), version)
        if restated == str(entry):
            return []
        dependencies[index] = restated
        return [f"dependency: {entry} -> {restated}"]
    raise KeyError(f"no {DISTRIBUTION} requirement in [project].dependencies")


# GitSource's own share of this is `git.entry()`; what is left is the edit to
# `[tool.uv.sources]`, which the document owns and one mode's argument does not.
# lup: ignore[model-free-function] — the TOML document is the subject
def apply_source(
    document: tomlkit.TOMLDocument,
    mode: LibraryMode,
    checkout: Path | None,
    git: GitSource | None = None,
) -> list[str]:
    """Declare, or clear, the ``[tool.uv.sources]`` override for ``lup``."""
    sources = document["tool"]["uv"]["sources"]
    match mode:
        case LibraryMode.PUBLISHED:
            if DISTRIBUTION not in sources:
                return []
            del sources[DISTRIBUTION]
            return ["source: resolved from the package index"]
        case LibraryMode.GIT:
            if git is None:
                raise ValueError("git mode needs a repository and ref")
            entry = git.entry()
        case LibraryMode.LOCAL:
            entry = tomlkit.inline_table()
            entry.update({"workspace": True})
        case LibraryMode.LINKED:
            if checkout is None:
                raise ValueError("linked mode needs a checkout path")
            entry = tomlkit.inline_table()
            entry.update({"path": str(checkout), "editable": True})
    if DISTRIBUTION in sources and dict(sources[DISTRIBUTION]) == dict(entry):
        return []
    sources[DISTRIBUTION] = entry
    return [f"source: {tomlkit.dumps(entry).strip()}"]


def apply_workspace(document: tomlkit.TOMLDocument, vendored: bool) -> list[str]:
    """Declare the uv workspace exactly while the library is vendored."""
    uv = document["tool"]["uv"]
    if not vendored:
        if "workspace" not in uv:
            return []
        del uv["workspace"]
        return ["workspace: removed"]
    if "workspace" in uv:
        return []
    workspace = tomlkit.table()
    workspace["members"] = WORKSPACE_MEMBERS
    uv["workspace"] = workspace
    return [f"workspace: members = {WORKSPACE_MEMBERS}"]


def apply_search_path(
    document: tomlkit.TOMLDocument, table: list[str], key: str, vendored: bool
) -> list[str]:
    """Add or drop every root under the vendored package in one path list.

    Each vendored root is placed directly after the plain one it shadows, so
    a project that leaves the vendored mode and returns to it gets the list it
    started with rather than a reshuffled diff.
    """
    holder = document
    for step in table:
        if step not in holder:
            return []
        holder = holder[step]
    if key not in holder:
        return []
    paths = [str(entry) for entry in holder[key]]
    wanted = [path for path in paths if not path.startswith(f"{VENDORED_ROOT}/")]
    if vendored:
        for plain, shadowed in VENDORED_SIBLINGS.items():
            if plain in wanted:
                wanted.insert(wanted.index(plain) + 1, shadowed)
    if wanted == paths:
        return []
    holder[key] = wanted
    return [f"{'.'.join([*table, key])}: {paths} -> {wanted}"]


def runtime_of(root: str) -> str:
    """Name the runtime whose tree an execution environment is rooted in."""
    return next((runtime for runtime in RUNTIMES if runtime in root), RUNTIMES[0])


def restored_beside_their_runtime(
    kept: list[ExecutionEnvironment],
) -> list[ExecutionEnvironment]:
    """Group every environment under its own runtime, vendored one first.

    Order is reconstructed rather than remembered, so leaving the vendored
    mode and returning to it restores the list the project started with
    instead of handing every adopter a reshuffled diff.
    """
    return [
        entry
        for runtime in RUNTIMES
        for group in (VENDORED_EXECUTION_ENVIRONMENTS, kept)
        for entry in group
        if runtime_of(entry["root"]) == runtime
    ]


def apply_execution_environments(
    document: tomlkit.TOMLDocument, vendored: bool
) -> list[str]:
    """Keep pyright environments rooted in the package only while it is there.

    Restored entries lead, which is the order the template ships and the only
    one reconstructible without remembering where they sat. Environments match
    on disjoint roots, so order carries no meaning to pyright — fixing it is
    what makes leaving and re-entering the vendored mode churn-free.
    """
    pyright = document["tool"]["pyright"]
    key = "executionEnvironments"
    if key not in pyright:
        return []
    current = [
        ExecutionEnvironment(
            root=str(item["root"]),
            extraPaths=[str(path) for path in item["extraPaths"]],
        )
        for item in pyright[key]
    ]
    kept = [item for item in current if not item["root"].startswith(VENDORED_ROOT)]
    desired = restored_beside_their_runtime(kept) if vendored else kept
    if desired == current:
        return []
    environments = tomlkit.aot()
    for declared in desired:
        entry = tomlkit.table()
        entry.update(declared)
        environments.append(entry)
    pyright[key] = environments
    return [f"pyright environments: {len(current)} -> {len(desired)}"]


def guard_leaving_local(root: Path, force: bool) -> None:
    """Refuse to un-vendor a checkout that still is the template itself.

    An uninitialized template and the lup repository are the same bytes, so
    no probe separates them. The rename is the event that makes a checkout
    somebody's project, and it is what ``/lup:init`` runs first.
    """
    if force or not (root / "src" / "lup_template").is_dir():
        return
    raise typer.BadParameter(
        "src/lup_template/ is still present, so this checkout is the template "
        "itself rather than a project built on it. Run `dev init rename-package "
        "<project>` first, or pass --force to un-vendor anyway."
    )


# Rewriting `pyproject.toml` is about the checkout, and `git` is the argument
# exactly one mode reads — beside `version` and `checkout`, which the others do.
# lup: ignore[model-free-function] — the checkout is the subject, git one mode's input
def set_mode(
    root: Path,
    mode: LibraryMode,
    *,
    version: str | None = None,
    checkout: Path | None = None,
    git: GitSource | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Rewrite ``pyproject.toml`` so ``lup`` resolves the way ``mode`` says."""
    vendored = mode is LibraryMode.LOCAL
    if vendored and not (root / VENDORED_ROOT).is_dir():
        raise typer.BadParameter(
            f"{VENDORED_ROOT}/ is not present, so there is no library to vendor. "
            "Use `dev library link <checkout>` to develop against one in place."
        )
    pyproject = root / "pyproject.toml"
    document = tomlkit.parse(pyproject.read_text(encoding="utf-8"))
    changes = [
        *apply_dependency(document, version if mode is LibraryMode.PUBLISHED else None),
        *apply_source(document, mode, checkout, git),
        *apply_workspace(document, vendored),
        *apply_search_path(
            document, ["tool", "pytest", "ini_options"], "pythonpath", vendored
        ),
        *apply_search_path(document, ["tool", "pyright"], "include", vendored),
        *apply_execution_environments(document, vendored),
    ]
    if changes and not dry_run:
        pyproject.write_text(tomlkit.dumps(document), encoding="utf-8")
    return changes


def drop_vendored(root: Path, dry_run: bool) -> list[str]:
    """Remove the vendored copy once nothing resolves through it."""
    if not (root / VENDORED_ROOT).is_dir():
        return []
    if not dry_run:
        git("rm", "-r", "--quiet", VENDORED_ROOT, _cwd=str(root))
    return [f"removed {VENDORED_ROOT}/"]


def report(changes: list[str], dry_run: bool, settled: str) -> None:
    """Print one mode change, or say the project already reads that way."""
    if not changes:
        typer.echo(settled)
        return
    typer.echo(f"Dry run — {len(changes)} change(s):" if dry_run else "Changed:")
    for change in changes:
        typer.echo(f"  {change}")
    if not dry_run:
        typer.echo("\nNext: uv sync && uv run lup-devtools harness generate all")


def use_library(
    mode: LibraryMode,
    version: str | None,
    keep_vendored: bool,
    force: bool,
    dry_run: bool,
) -> None:
    """CLI entry for ``lup-devtools dev library use`` (see module docstring)."""
    root = find_project_root()
    if mode is not LibraryMode.LOCAL:
        guard_leaving_local(root, force)
    changes = set_mode(root, mode, version=version, dry_run=dry_run)
    if mode is not LibraryMode.LOCAL and not keep_vendored:
        changes.extend(drop_vendored(root, dry_run))
    report(changes, dry_run, f"Already resolving {DISTRIBUTION} as {mode}.")


# The body of `dev library git`, beside the other three command entries: its
# subject is the command line, and GitSource is what the flags parsed into.
# lup: ignore[model-free-function] — CLI command body over its parsed flags
def git_library(
    source: GitSource, keep_vendored: bool, force: bool, dry_run: bool
) -> None:
    """CLI entry for ``lup-devtools dev library git`` (see module docstring)."""
    root = find_project_root()
    guard_leaving_local(root, force)
    changes = set_mode(root, LibraryMode.GIT, git=source, dry_run=dry_run)
    if not keep_vendored:
        changes.extend(drop_vendored(root, dry_run))
    report(
        changes,
        dry_run,
        f"Already resolving {DISTRIBUTION} from {source.url} "
        f"at {source.ref_kind} {source.ref}.",
    )


def link_library(
    checkout: Path, keep_vendored: bool, force: bool, dry_run: bool
) -> None:
    """CLI entry for ``lup-devtools dev library link`` (see module docstring)."""
    root = find_project_root()
    package = (checkout / VENDORED_ROOT).resolve()
    if not (package / "pyproject.toml").is_file():
        raise typer.BadParameter(f"no lup package at {package}")
    guard_leaving_local(root, force)
    changes = set_mode(root, LibraryMode.LINKED, checkout=package, dry_run=dry_run)
    if not keep_vendored:
        changes.extend(drop_vendored(root, dry_run))
    report(changes, dry_run, f"Already linked to {package}.")


def library_status() -> None:
    """CLI entry for ``lup-devtools dev library status`` (see module docstring)."""
    root = find_project_root()
    mode = read_mode(root)
    typer.echo(f"mode: {mode}")
    match mode:
        case LibraryMode.LINKED:
            typer.echo(f"checkout: {read_linked_path(root)}")
        case LibraryMode.LOCAL:
            typer.echo(f"vendored: {root / VENDORED_ROOT}")
        case LibraryMode.GIT:
            source = read_git_source(root)
            if source is not None:
                typer.echo(f"repository: {source.url}")
                typer.echo(f"{source.ref_kind}: {source.ref}")
        case LibraryMode.PUBLISHED:
            typer.echo(f"version: {installed_version(DISTRIBUTION)}")


RELEASE_INDEX_URL = f"https://pypi.org/pypi/{DISTRIBUTION}/json"
"""Where a release is looked up. Overridable: a project resolving lup through
a private index asks that index the same question in the same shape."""

RELEASE_PROBE_SECONDS = 10.0
"""How long the look-up waits. An unreachable index is an answer this command
knows how to give, and giving it beats blocking whoever is waiting on it."""


class ReleaseIndexInfo(BaseModel):
    """The one field of the index's document this reads."""

    version: str


class ReleaseIndexDocument(BaseModel):
    """A package index's answer about one distribution."""

    info: ReleaseIndexInfo


class ReleaseProbe(BaseModel, frozen=True):
    """What the package index holds for lup: a version, nothing, or no answer.

    The third outcome stays itself instead of collapsing into the second. An
    index that could not be reached has not said a release is absent, and
    reading it that way pins a project to a repository ref on the strength of
    a dropped connection.

    What this does not decide is the acquisition mode. Only one half of that
    is a fact — whether a release exists at all — and the other half is what
    the project is to the library: one that works on lup, dogfooding a branch
    and sending changes back, wants that branch whether or not a release was
    cut from it.
    """

    version: str = ""
    unreachable: str = ""

    def describe(self) -> list[str]:
        """What the index answered, and the command that takes it at its word."""
        if self.unreachable:
            return [
                f"index unreachable: {self.unreachable}",
                "A probe that did not land settles nothing. Retry, or declare "
                "the mode this project already knows it wants.",
            ]
        if not self.version:
            return [
                "no release published yet",
                f"dev library {LibraryMode.GIT} --branch <branch>",
            ]
        return [
            f"released: {self.version}",
            f"dev library use {LibraryMode.PUBLISHED} --version {self.version}",
            f"`{LibraryMode.GIT}` stays a live choice: a project that works on "
            "lup as well as with it runs the branch it is improving rather "
            "than the last release cut from it.",
        ]


def probe_release(
    url: str = RELEASE_INDEX_URL, timeout: float = RELEASE_PROBE_SECONDS
) -> ReleaseProbe:
    """Ask the package index whether a release of lup exists.

    A missing project is the index answering rather than failing: before the
    first release that is the true state of the world, and it is the answer
    that sends a new project to the repository rather than to nothing.
    """
    try:
        response = (
            httpx.get(  # lup: ignore[dict-get] — httpx's GET verb, not a dict read
                url, timeout=timeout, follow_redirects=True
            )
        )
    except httpx.HTTPError as error:
        return ReleaseProbe(unreachable=f"{type(error).__name__}: {error}")
    if response.status_code == httpx.codes.NOT_FOUND:
        return ReleaseProbe()
    if response.status_code != httpx.codes.OK:
        return ReleaseProbe(unreachable=f"{url} answered {response.status_code}")
    try:
        document = ReleaseIndexDocument.model_validate_json(response.content)
    except ValidationError:
        return ReleaseProbe(unreachable=f"{url} answered a document it could not read")
    return ReleaseProbe(version=document.info.version)


def library_release() -> None:
    """CLI entry for ``lup-devtools dev library release`` (see module docstring)."""
    for line in probe_release().describe():
        typer.echo(line)
