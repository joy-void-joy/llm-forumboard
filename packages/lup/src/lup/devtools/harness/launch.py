"""Native launch flows: runtime preflight, then the Claude and Codex launchers.

Each launcher regenerates its target's artifacts, verifies every claimed
native requirement against a live probe, and hands the terminal to the
native CLI with the non-interactive environment applied.
"""

import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import sh
import typer

from lup.runtime.profiles import ProfileDirectory
from lup.adapters.codex.harness_runtime import (
    CodexPluginInstaller,
    PluginCacheConfig,
)
from lup.harness.environment import non_interactive_environment
from lup.harness.models import Plugin
from lup.harness.process import LocalProcessLauncher
from lup.types import EnvVars
from lup.workspace.paths import project_root
from lup.adapters.codex.home import (
    CodexWorktreeHomeStore,
    login_state,
    select_codex_home,
)
from lup.devtools.dev.branches import confirm_base_freshness, probe_base_freshness
from lup.devtools.harness.drift import generate_with_report
from lup.devtools.harness.generate import NativeHarnessComposition
from lup.devtools.dev.worktree import RelocationHint
from lup.devtools.layout import get_tree_dir


def relocation_hint(worktree_path: Path) -> RelocationHint:
    """Name the follow-through in the vocabulary of the running harness.

    Each launcher exports its own configuration home, so a session that
    reached here through one of them is told the move it actually supports.
    Anything else gets the portable shell form alone rather than the name of
    a tool that runtime may not have.
    """
    environ = os.environ  # lup: ignore[os-environ]
    move = f"cd /; cd {worktree_path}"
    if "CLAUDE_CONFIG_DIR" in environ:
        return RelocationHint(
            agent="EnterWorktree(path=<the path above>)",
            shell=f"{move}; claude",
        )
    if "CODEX_HOME" in environ:
        return RelocationHint(
            agent="start a session there — this runtime cannot relocate a running one",
            shell=f"{move}; codex",
        )
    return RelocationHint(agent="", shell=move)


def ready_to_open(composition: NativeHarnessComposition, generate_only: bool) -> bool:
    """Generate this target's artifacts and clear every gate standing before a session.

    Both launchers reach a session through here, so a gate added once is a
    gate every entry point makes — including one written later, which cannot
    open a session without first generating the artifacts it opens against.
    Answers whether to go on: a generate-only invocation has already done
    everything it was asked for.

    The base check is one of those gates rather than a workflow's own step. A
    tree whose base has moved is self-consistent and says nothing about it, so
    a session opened on one plans and edits against code that is no longer
    there — which cost a planning pass over thirteen concerns on a tree ten
    commits behind its remote, where two merged pull requests had already done
    part of the work being planned.
    """
    generate_with_report(composition)
    if generate_only:
        return False
    runtime_preflight(composition)
    confirm_base_freshness(
        probe_base_freshness(LocalProcessLauncher(), project_root()),
        # A launcher hands the terminal to a native CLI, so the human who ran
        # it is the one this count is put to. Nobody is there for a session
        # spawned from a script, and that is the run the incident happened on.
        interactive=sys.stdin.isatty(),
    )
    return True


def runtime_preflight(composition: NativeHarnessComposition) -> None:
    """Verify each claimed native requirement immediately before launch."""
    target = composition.recipe.label
    evidence = composition.readiness()
    for item in evidence:
        state = "ready" if item.supported else "missing"
        typer.echo(f"{target} {item.capability}: {state} ({item.version})")
    if any(not item.supported for item in evidence):
        raise typer.BadParameter(f"{target} runtime preflight failed")


def codex_login_preflight(home: Path, environment: EnvVars) -> None:
    """Offer the sign-in a launch needs, rather than failing inside a session.

    An unusable login does not stop Codex from starting; it surfaces later as
    an authentication error against whichever service is reached first, which
    names neither the home the credential came from nor the way to fix it.
    Declining is respected — a session can still be useful offline.
    """
    state = login_state(home)
    if state.usable_at(datetime.now(UTC)):
        return
    reason = (
        f"expired {state.expires_at:%Y-%m-%d}"
        if state.expires_at is not None
        else "not signed in"
    )
    typer.echo(f"Codex login in {home}: {reason}")
    if not typer.confirm("Sign in to Codex now?", default=True):
        typer.echo("Continuing unauthenticated — Codex will report its own errors")
        return
    try:
        sh.Command("codex")(
            "login", _fg=True, _env={**environment, "CODEX_HOME": str(home)}
        )
    except sh.ErrorReturnCode as error:
        raise typer.BadParameter("Codex sign-in did not complete") from error


def apply_sandbox_environment(
    plugin: Plugin,
    environment: EnvVars,
    label: str,
    required_tools: list[str],
) -> None:
    """Export LUP_SANDBOX_ACTIVE when the declared sandbox can actually run.

    The dispatchers defer unjudged shell only under this flag, so it is set
    exactly when the launch verified the OS boundary; without it the deny
    lattice keeps carrying the escalation recipe.
    """
    hooks = plugin.hooks
    if hooks is None or hooks.sandbox is None:
        return
    missing = [tool for tool in required_tools if shutil.which(tool) is None]
    if missing:
        typer.echo(
            f"{label} sandbox: missing {', '.join(missing)} — deny lattice stays active"
        )
        return
    environment["LUP_SANDBOX_ACTIVE"] = "1"
    typer.echo(f"{label} sandbox: active — unjudged shell defers to the OS boundary")


# lup: ignore[library-default] — each entry is literally a Codex CLI flag
CODEX_SANDBOX_OVERRIDES = (
    "-s",
    "--sandbox",
    "--full-auto",
    "--yolo",
    "--dangerously-bypass-approvals-and-sandbox",
)


def codex_sandbox_arguments(
    plugin: Plugin, environment: EnvVars, extra_args: list[str]
) -> list[str]:
    """Compose the interactive Codex envelope that LUP_SANDBOX_ACTIVE vouches for.

    The launcher establishes the boundary it announces: an explicit
    workspace-write sandbox on the Codex command line, mirroring how the
    Claude settings artifact compiles the same declaration into an OS wall.
    Path-level write and credential denials have no Codex equivalent, and
    neither does taking one command out of the envelope, so the envelope is
    the declaration's strict subset (network stays off). The dispatcher still
    reads the exclusions, judging those commands as though nothing confined
    them — which is the strict direction here too, since an envelope with no
    network is not a boundary they would have survived either. When the
    caller supplies its own sandbox flag the launcher vouches for nothing:
    the flag stays unset and the deny lattice keeps the escalation recipe.
    """
    hooks = plugin.hooks
    if hooks is None or hooks.sandbox is None:
        return []
    overrides = [
        word
        for word in extra_args
        if word in CODEX_SANDBOX_OVERRIDES or word.startswith("--sandbox=")
    ]
    if overrides:
        typer.echo(
            f"codex sandbox: caller envelope ({' '.join(overrides)}) — "
            "deny lattice stays active"
        )
        return []
    environment["LUP_SANDBOX_ACTIVE"] = "1"
    typer.echo(
        "codex sandbox: workspace-write envelope — "
        "unjudged shell defers to the OS boundary"
    )
    return ["--sandbox", "workspace-write", *writable_root_arguments()]


def writable_root_arguments() -> list[str]:
    """Widen the workspace-write root to the tree/ holding sibling worktrees.

    Codex roots writes at the launch directory, so a feature worktree this
    project's own workflow prescribes creating lands outside the boundary
    and cannot be edited from the session that created it.
    """
    try:
        tree = get_tree_dir()
    except (typer.Exit, SystemExit):
        return []
    return ["-c", f'sandbox_workspace_write.writable_roots=["{tree}"]']


def claude_sandbox_arguments(plugin: Plugin) -> list[str]:
    """Widen the Claude sandbox's writable set over the same sibling tree/.

    Claude roots writes at the working directory just as Codex does, so a
    second checkout is read-only to every command a session runs — and
    running the toolchain over one is ordinary work here, which is why the
    symptom arrives as pytest failing to write a cache and `ruff format`
    refusing to save. Neither error names a sandbox.

    The path is this machine's, so it is resolved at launch and passed as
    settings rather than declared: an artifact carrying an absolute path
    would be drift in every other checkout. The declared writable paths ride
    along rather than being left to the generated file, because the two
    surfaces document this key differently — arrays that merge across
    scopes, values that override per session — and a list carrying both is
    the same list under either reading.
    """
    hooks = plugin.hooks
    if hooks is None or hooks.sandbox is None:
        return []
    try:
        tree = get_tree_dir()
    except (typer.Exit, SystemExit):
        return []
    allowed = [*hooks.sandbox.writable_paths, str(tree)]
    widened = {"sandbox": {"filesystem": {"allowWrite": allowed}}}
    return ["--settings", json.dumps(widened)]


# lup: solved: Both launches should check the checkout is current before opening a
# session, and say so when it is not. A session started on a stale base plans and
# edits against code that already moved: this cost a full resolver planning pass
# over 13 concerns, where the tree was ten commits behind its remote, two merged
# PRs had already done part of the planned work, and a merged intake fix would
# have excluded 16 of the 53 notes as generated copies no worker could edit. The
# agent cannot see any of that — the tree looks self-consistent. A fetch and a
# behind-count at launch, reported and refusable, costs one round trip. Put it
# here rather than in the resolve skill, so every session gets it and not only
# the one workflow that noticed.
def launch_claude(
    composition: NativeHarnessComposition,
    extra_args: list[str],
    profiles: ProfileDirectory,
    profile: str | None,
    model: str | None,
    generate_only: bool,
) -> None:
    """Generate/reconcile Claude artifacts and launch the verified local plugin."""
    plugin = composition.recipe.source.plugins[0]
    if not ready_to_open(composition, generate_only):
        return
    arguments: list[str] = []
    if model is not None:
        arguments.extend(["--model", model])
    arguments.extend(
        [
            "--plugin-dir",
            str(project_root() / ".claude" / "plugins" / plugin.name),
            *claude_sandbox_arguments(plugin),
            *extra_args,
        ]
    )
    environment = non_interactive_environment(os.environ)  # lup: ignore[os-environ]
    apply_sandbox_environment(plugin, environment, "claude", ["bwrap", "socat"])
    # A name no origin answers to reaches here from an explicit --profile, and
    # from an active selection whose profile has since gone; both are the
    # caller's to fix, so neither should arrive as a traceback.
    try:
        home = profiles.launch_home(profile)
    except KeyError as error:
        raise typer.BadParameter(str(error)) from error
    if home is not None:
        environment.update(profiles.login.environment(home))
    try:
        sh.Command("claude")(*arguments, _fg=True, _env=environment)
    except sh.CommandNotFound as error:
        raise typer.BadParameter("Claude Code CLI is not installed") from error
    except sh.ErrorReturnCode as error:
        raise typer.Exit(error.exit_code) from error


def launch_codex(
    composition: NativeHarnessComposition,
    extra_args: list[str],
    codex_home: Path | None,
    profile: str | None,
    model: str | None,
    generate_only: bool,
    force_install: bool,
) -> None:
    """Generate/reconcile Codex artifacts and launch without updating the CLI."""
    plugin = composition.recipe.source.plugins[0]
    if not ready_to_open(composition, generate_only):
        return
    environment = non_interactive_environment(os.environ)  # lup: ignore[os-environ]
    envelope = codex_sandbox_arguments(plugin, environment, extra_args)
    store = CodexWorktreeHomeStore()
    home = select_codex_home(codex_home, environment, project_root(), profile, store)
    selected_home = home.path
    if home.isolated:
        typer.echo(f"Using worktree-scoped Codex home: {selected_home}")
    codex_login_preflight(selected_home, environment)
    installer = CodexPluginInstaller(
        PluginCacheConfig(codex_home=selected_home, marketplace=plugin.marketplace)
    )
    arguments: list[str] = list(envelope)
    if profile is not None:
        arguments.extend(["--profile", profile])
    if model is not None:
        arguments.extend(["--model", model])
    arguments.extend(extra_args)
    environment["CODEX_HOME"] = str(selected_home)
    try:
        with installer.temporary(
            project_root() / ".codex" / "plugins" / plugin.name,
            project_root(),
            force=force_install,
        ) as cache:
            typer.echo(f"Verified installed Codex plugin: {cache.installed_root}")
            sh.Command("codex")(*arguments, _fg=True, _env=environment)
    except sh.CommandNotFound as error:
        raise typer.BadParameter("Codex CLI is not installed") from error
    except sh.ErrorReturnCode as error:
        raise typer.Exit(error.exit_code) from error
    finally:
        if home.isolated and store.publish(project_root()):
            typer.echo("Returned the refreshed Codex login to the account home")
