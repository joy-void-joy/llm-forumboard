# lup: ignore[dict-get]
# Every read here probes ProjectEntry/config payloads whose keys are all
# optional, so dict-get is opted out file-wide.
"""Track repos to sync with and review their commits since the last sync.

These commands are the read/fetch half of ``/lup:update`` — the workflow that
pulls improvements *from* the lup template (and other tracked upstreams) *into*
this repo. ``/lup:update`` reviews every upstream commit since the last sync,
generalizes the domain-specific ones back into reusable scaffold, and applies
them; this module is what gives it the diffs to review and remembers how far it
got. ``status`` shows which upstreams have new commits, ``log``/``diff`` feed
the commit-by-commit review, and ``mark-synced`` advances the per-project
checkpoint once a review is done so the next run starts where this one stopped.

Reviewing a diff means reading the upstream's *current* code, so commits are
fetched into a local working copy before comparison (see ``ensure_local``):
``status`` stays read-only and never touches the network, while ``log``/``diff``
clone-or-fetch on demand. Each materialized upstream also gets a stable
``refs/<name>`` symlink so other commands (e.g. ``/lup:import``) can browse it
by name without re-resolving cache paths.

Two registry files declare what to track:

- sync.json (committed): the template's registry default, shipping only the
  lup entry so a fresh project can immediately pull template improvements.
  It is template scaffold: agents must never modify it — every personal
  registration belongs in sync.json.local, and the edit policy asks before
  any change to the tracked file.
- sync.json.local (gitignored): personal registrations — local paths, sync
  state, overrides by project name, and local-only projects. Set
  "ignore": true to skip a project (useful when you ARE the upstream).

The registry is direction-neutral: "sync" names the mechanism, not a
direction. Seen from a project built on the template, the shipped lup entry
is an upstream to pull improvements from; seen from the lup repo itself,
sync.json.local registers the downstream fleet whose commits /lup:update
reviews. Same tooling, opposite seats. A repo still carrying the legacy
names downstream.json / downstream.json.local is read as a fallback with a
deprecation warning; migrate by renaming the files.

The script merges both: .local entries override sync.json entries by name.
Projects with a URL but no local path are auto-cloned to .cache/sync/.

Examples::

    $ uv run lup-devtools sync status
    $ uv run lup-devtools sync log my-project
    $ uv run lup-devtools sync log my-project --no-stat
    $ uv run lup-devtools sync diff my-project abc1234
    $ uv run lup-devtools sync mark-synced my-project
    $ uv run lup-devtools sync setup my-project /path/to/repo --synced
    $ uv run lup-devtools sync setup my-project /path/to/repo --branch main
"""

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Required, TypedDict

import sh
import typer
from pydantic import ConfigDict, TypeAdapter, with_config

from lup.workspace.paths import project_root
from lup.devtools.subapps import subapp
from lup.devtools.utils import decode_stderr, format_table, git, short_sha

app = typer.Typer(no_args_is_help=True)
SUBAPP = subapp("sync", "Track sync.json repos and review their commits", app)
logger = logging.getLogger(__name__)


@with_config(ConfigDict(extra="allow"))
class ProjectEntry(TypedDict, total=False):
    """One tracked project, merged from sync.json(.local).

    A ``TypedDict``, not a ``BaseModel``: per CLAUDE.md, ``TypedDict`` types
    the JSON-shaped config we read from and write back to disk verbatim, while
    ``BaseModel`` is for values we validate and construct. These entries are
    hand-edited config, so loads validate the declared keys (catching typos
    with a real error instead of silent misbehavior) and ``extra="allow"``
    keeps any hand-added keys so the document round-trips unchanged.
    """

    name: Required[str]
    path: str
    url: str
    branch: str
    last_synced_commit: str
    ignore: bool


@with_config(ConfigDict(extra="allow"))
class SyncConfig(TypedDict):
    """Top-level shape of sync.json and sync.json.local.

    Same rationale as :class:`ProjectEntry`: a ``TypedDict`` mirroring the
    on-disk JSON document, validated on read with extras preserved.
    """

    projects: list[ProjectEntry]


SYNC_CONFIG_ADAPTER = TypeAdapter(SyncConfig)
PROJECT_ENTRY_ADAPTER = TypeAdapter(ProjectEntry)


def registry_path(name: str, legacy_name: str) -> Path:
    """Resolve a registry file, falling back to its legacy downstream name."""
    preferred = project_root() / name
    legacy = project_root() / legacy_name
    if preferred.exists() or not legacy.exists():
        return preferred
    logger.warning("%s is deprecated; rename it to %s", legacy_name, name)
    return legacy


def sync_file() -> Path:
    return registry_path("sync.json", "downstream.json")


def local_file() -> Path:
    return registry_path("sync.json.local", "downstream.json.local")


def cache_dir() -> Path:
    return project_root() / ".cache" / "sync"


def refs_dir() -> Path:
    return project_root() / "refs"


def load_json(path: Path) -> SyncConfig:
    if not path.exists():
        return {"projects": []}
    try:
        return SYNC_CONFIG_ADAPTER.validate_python(json.loads(path.read_text()))
    except json.JSONDecodeError as error:
        raise typer.BadParameter(f"{path} is not valid JSON: {error}") from error


def save_local(data: SyncConfig) -> None:
    local_file().write_text(json.dumps(data, indent=2) + "\n")


def ensure_ref_symlink(name: str, target: str) -> None:
    """Point the stable ``refs/<name>`` symlink at a project's working copy.

    ``refs/`` (gitignored) is a directory of by-name shortcuts into the
    repos this project tracks for sync, wherever each one actually lives —
    a user-configured path, or a clone under ``.cache/sync/``. It exists
    so commands and humans can reach a tracked repo as ``refs/<name>`` without
    knowing or re-deriving its real location (e.g. ``/lup:import`` does
    ``cd refs/<project> && git log``). Every time a project is materialized the
    link is re-pointed at its current path; a pre-existing non-symlink at that
    name is left untouched so we never clobber real files.
    """
    refs_dir().mkdir(exist_ok=True)
    link = refs_dir() / name
    target_path = Path(target).resolve()
    if link.is_symlink():
        if link.resolve() == target_path:
            return
        link.unlink()
    elif link.exists():
        logger.warning("refs/%s exists but is not a symlink, skipping", name)
        return
    link.symlink_to(target_path)
    logger.debug("refs/%s -> %s", name, target_path)


def load_projects() -> list[ProjectEntry]:
    """Load and merge projects from sync.json + sync.json.local."""
    base = load_json(sync_file())
    local = load_json(local_file())

    merged: dict[str, ProjectEntry] = {}  # lup: ignore[empty-collection] — merge fold
    for p in base.get("projects", []):
        merged[p["name"]] = p.copy()
    for p in local.get("projects", []):
        name = p["name"]
        if name in merged:
            base_entry = merged[name]
            merged[name] = PROJECT_ENTRY_ADAPTER.validate_python({**base_entry, **p})
        else:
            merged[name] = p.copy()

    return list(merged.values())


def find_project(name: str) -> ProjectEntry:
    """Find a project by name, raising Exit if not found."""
    projects = load_projects()
    proj = next((p for p in projects if p["name"] == name), None)
    if not proj:
        typer.echo(f"Project '{name}' not found.")
        typer.echo(f"Available: {', '.join(p['name'] for p in projects)}")
        raise typer.Exit(1)
    return proj


def resolve_existing_path(proj: ProjectEntry) -> str | None:
    """Return a usable local path WITHOUT cloning or fetching, or None.

    Read-only counterpart to :func:`ensure_local` — for status reporting
    that must never mutate the working tree or hit the network.
    """
    path = proj.get("path", "")
    if path and Path(path).exists():
        return path
    cache_path = cache_dir() / proj["name"]
    if cache_path.exists():
        return str(cache_path)
    return None


def ensure_local(
    proj: ProjectEntry,
    report: Callable[[str], None] = typer.echo,
) -> str:
    """Materialize an upstream so its commits can be read, return its path.

    Reviewing upstream commits (``log``/``diff``) means reading the upstream's
    actual git history, which only exists in a local checkout — so before any
    such command can run, the project must be present and current on disk. This
    is that guarantee: it clones a project that has only a URL, fetches and
    hard-resets one already cached so the review sees the latest commits rather
    than a stale snapshot, and leaves a user-provided local path as-is. In
    every case it (re)points ``refs/<name>`` at the result and hands back the
    path the caller runs git in. ``status`` deliberately does *not* call this
    (it uses :func:`resolve_existing_path` instead) so a status check never
    clones, fetches, or writes.

    Progress and error text goes through ``report`` so callers rendering
    tables can defer the messages instead of interleaving them mid-table.
    """
    path = proj.get("path", "")
    name = proj["name"]
    branch = proj.get("branch", "")
    if path and Path(path).exists():
        ensure_ref_symlink(name, path)
        return path

    cache_path = cache_dir() / name
    url = proj.get("url", "")
    reset_target = f"origin/{branch}" if branch else "origin/HEAD"

    if cache_path.exists():
        report(f"Fetching latest for '{name}' from cache...")
        try:
            git("-C", str(cache_path), "fetch", "--quiet")
            git("-C", str(cache_path), "reset", "--hard", reset_target, "--quiet")
        except sh.ErrorReturnCode as e:
            report(f"Warning: fetch failed: {decode_stderr(e)}")
        ensure_ref_symlink(name, str(cache_path))
        return str(cache_path)

    if url:
        report(f"Cloning '{name}' from {url}...")
        cache_dir().mkdir(parents=True, exist_ok=True)
        clone_args = ["clone", "--depth=200"]
        if branch:
            clone_args.extend(["--branch", branch])
        clone_args.extend([url, str(cache_path)])
        try:
            git(*clone_args)
        except sh.ErrorReturnCode as e:
            report(f"Clone failed: {decode_stderr(e)}")
            raise typer.Exit(1)
        ensure_ref_symlink(name, str(cache_path))
        return str(cache_path)

    report(
        f"Project '{name}' has no local path or URL configured.\n"
        "Either:\n"
        "  1. Add a URL for it in sync.json.local\n"
        f"  2. Run: uv run lup-devtools sync setup {name} /path/to/repo"
    )
    raise typer.Exit(1)


def git_in(path: str, *args: str) -> str:
    """Run git command in a specific directory."""
    return git.out("-C", path, *args)


def commit_count(path: str, since: str) -> int:
    """Count commits since a given ref."""
    if not since:
        output = git_in(path, "rev-list", "--count", "HEAD")
        return int(output)
    output = git_in(path, "rev-list", "--count", f"{since}..HEAD")
    return int(output)


def current_head(path: str) -> str:
    """Get current HEAD sha."""
    return git_in(path, "rev-parse", "HEAD")


@app.command("status")
def status_cmd() -> None:
    """Show tracked projects and their sync status (read-only).

    Reports cached/not-cloned/behind state without cloning, fetching, or
    resetting. Run ``sync fetch`` to materialize and refresh repos.
    """
    projects = load_projects()

    if not projects:
        typer.echo("No projects tracked. Check sync.json(.local) or run 'setup'.")
        raise typer.Exit(1)

    def project_row(p: ProjectEntry) -> list[str]:
        synced = p.get("last_synced_commit", "")
        synced_short = short_sha(synced) if synced else "never"

        if p.get("ignore"):
            return [p["name"], "—", "ignored", "(skipped)"]

        resolved = resolve_existing_path(p)
        if resolved is None:
            has_url = bool(p.get("url"))
            note = "not cloned (run: sync fetch)" if has_url else "no path/url"
            return [p["name"], "—", synced_short, note]

        try:
            behind: int | str = commit_count(resolved, synced)
        except (sh.ErrorReturnCode, ValueError):
            behind = "?"
        branch = p.get("branch", "")
        source = f"{resolved} ({branch})" if branch else resolved
        return [p["name"], str(behind), synced_short, source]

    rows = [project_row(p) for p in projects]

    typer.echo()
    typer.echo(format_table(("Project", "Behind", "Last Synced", "Source"), rows))
    typer.echo()


@app.command("fetch")
def fetch_cmd(
    project: Annotated[
        str | None,
        typer.Argument(help="Project to materialize/refresh (default: all)"),
    ] = None,
) -> None:
    """Clone missing repos and fetch/reset cached ones (network + writes)."""
    projects = load_projects()
    targets = (
        [find_project(project)]
        if project
        else [p for p in projects if not p.get("ignore")]
    )
    for p in targets:
        try:
            resolved = ensure_local(p)
            typer.echo(f"{p['name']}: ready at {resolved}")
        except (typer.Exit, sh.ErrorReturnCode):
            typer.echo(f"{p['name']}: could not materialize", err=True)


@app.command("log")
def show_log(
    project: Annotated[str, typer.Argument(help="Project name")],
    stat: Annotated[
        bool, typer.Option("--stat/--no-stat", help="Show file stats")
    ] = True,
) -> None:
    """List commits to review: everything upstream added since the last sync.

    This is the inventory ``/lup:update`` walks — the commits whose diffs get
    read (via ``sync diff``) and considered for porting. Fetches the upstream
    first so the list reflects its current HEAD.
    """
    proj = find_project(project)
    path = ensure_local(proj)

    synced = proj.get("last_synced_commit", "")
    range_spec = f"{synced}..HEAD" if synced else "HEAD"

    args = ["log", "--oneline"]
    if stat:
        args.append("--stat")
    args.append(range_spec)

    output = git_in(path, *args)
    if output:
        typer.echo(output)
    else:
        typer.echo(f"No new commits since {short_sha(synced)}.")


@app.command("diff")
def show_diff(
    project: Annotated[str, typer.Argument(help="Project name")],
    commit: Annotated[str, typer.Argument(help="Commit SHA to show")],
) -> None:
    """Show full diff for a specific commit."""
    proj = find_project(project)
    path = ensure_local(proj)
    output = git_in(path, "show", commit)
    typer.echo(output)


@app.command("mark-synced")
def mark_synced(
    project: Annotated[str, typer.Argument(help="Project name")],
) -> None:
    """Advance the sync checkpoint to the upstream's current HEAD.

    Run once a review is finished: it records that every commit up to the
    upstream's HEAD has been considered, so the next ``sync log`` / ``status``
    only surfaces commits that land afterward. Marking synced even when nothing
    was ported is correct — it means "reviewed, decided to port none."
    """
    proj = find_project(project)
    path = ensure_local(proj)

    head = current_head(path)

    local_data = load_json(local_file())
    local_projects = local_data.get("projects", [])

    entry = next((p for p in local_projects if p["name"] == project), None)
    if entry:
        entry["last_synced_commit"] = head
    else:
        local_projects.append(
            {
                "name": project,
                "path": path,
                "last_synced_commit": head,
            }
        )
        local_data["projects"] = local_projects

    save_local(local_data)
    typer.echo(f"Marked '{project}' as synced at {short_sha(head)}.")


@app.command("setup")
def setup_project(
    name: Annotated[str, typer.Argument(help="Project name")],
    path: Annotated[str, typer.Argument(help="Local path to the repo")],
    synced: Annotated[
        bool, typer.Option("--synced", help="Mark as already synced at current HEAD")
    ] = False,
    branch: Annotated[
        str,
        typer.Option("--branch", "-b", help="Branch to track (default: remote HEAD)"),
    ] = "",
) -> None:
    """Set the local path for a project (writes to sync.json.local)."""
    resolved = Path(path).resolve()
    if not resolved.exists():
        typer.echo(f"Path does not exist: {resolved}")
        raise typer.Exit(1)

    if not (resolved / ".git").exists() and not (resolved / ".git").is_file():
        typer.echo(f"Not a git repository: {resolved}")
        raise typer.Exit(1)

    local_data = load_json(local_file())
    local_projects = local_data.get("projects", [])

    entry = next((p for p in local_projects if p["name"] == name), None)
    if entry:
        entry["path"] = str(resolved)
    else:
        entry = ProjectEntry(name=name, path=str(resolved))
        local_projects.append(entry)
        local_data["projects"] = local_projects

    if branch:
        entry["branch"] = branch

    head = current_head(str(resolved)) if synced else ""
    if synced:
        entry["last_synced_commit"] = head

    save_local(local_data)
    ensure_ref_symlink(name, str(resolved))
    typer.echo(f"Set '{name}' local path to {resolved}")
    if branch:
        typer.echo(f"  Tracking branch: {branch}")
    if synced:
        typer.echo(f"  Marked as synced at {short_sha(head)}")
