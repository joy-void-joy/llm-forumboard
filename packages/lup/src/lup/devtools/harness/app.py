"""Typer command tree for ``lup-devtools harness``: wiring only, no bodies.

Each command delegates to the module owning its concern: ``drift`` for
generation and checking, ``reconcile`` for local-difference workflows,
``doctor`` for runtime evidence, ``resolve`` for the persisted resolver,
and ``launch`` for the native launchers. Every one of them works on already
concrete compositions, so the targets a project declares are what this tree
operates on and no command names a runtime of its own.

A launch command exists exactly when its adapter is among those targets: a
project generating one native tree is not offered a launcher for the other.
"""

from pathlib import Path
from typing import Annotated

import typer

import lup.devtools.harness.doctor as doctor
import lup.devtools.harness.drift as drift
import lup.devtools.harness.launch as launch
import lup.devtools.harness.reconcile as reconcile
import lup.devtools.harness.resolve as resolve
from lup.devtools.dev.issues import EXCLUDED_LABEL
from lup.devtools.harness.composition import NativeTargets, claude_profile_directory
from lup.devtools.harness.profile_app import create_profile_app
from lup.runtime.profiles import ProfileDirectory
from lup.devtools.harness.drift import RepositoryWriter
from lup.devtools.supervisor.app import serve_supervisor
from lup.devtools.supervisor.page import SUPERVISOR_PORT
from lup.devtools.supervisor.doors import (
    accept_verification,
    answer_questions,
    drain_run,
    list_actors,
    list_questions,
    park_run,
    redirect_actor,
    retire_concern,
    say_to_actor,
    show_status,
)
from lup.workspace.paths import project_root


def create_harness_app(
    targets: NativeTargets,
    repository_writers: list[RepositoryWriter],
    model: resolve.ConfiguredModel | None = None,
    profiles: ProfileDirectory | None = None,
) -> typer.Typer:
    """Wire the harness command tree over the targets one project declares.

    A project that keeps Claude accounts of its own supplies ``profiles``
    over that origin, so one name selects the same account for a launch here
    as it does everywhere else in that project. Supplying none falls back to
    the personal registry, which is the answer for a project keeping none.
    """
    directory = profiles or claude_profile_directory()
    app = typer.Typer(no_args_is_help=True, help="Generate and launch a native harness")
    selector = f"{', '.join(targets.builders)}, or {targets.every}"

    def repository_wide(target: str) -> list[RepositoryWriter]:
        """The writers a selector reaches: every one of them, or none.

        A generated file outside a native tree belongs to no single target,
        so only the selector naming all of them is answerable for it.
        """
        return repository_writers if target == targets.every else []

    @app.command("generate")
    def generate_command(
        target: Annotated[str, typer.Argument(help=selector)] = targets.every,
    ) -> None:
        """Deterministically generate owned native artifacts without launching."""
        drift.generate_targets(
            targets.resolve(target, project_root()), repository_wide(target)
        )

    @app.command("check")
    def check_command(
        target: Annotated[str, typer.Argument(help=selector)] = targets.every,
    ) -> None:
        """Read-only ownership and generated-artifact drift check for CI."""
        drift.check_targets(
            targets.resolve(target, project_root()), repository_wide(target)
        )

    @app.command("reconcile")
    def reconcile_command(
        target: Annotated[str, typer.Argument(help=selector)] = targets.every,
    ) -> None:
        """Classify local differences without rewriting canonical Python source."""
        reconcile.classify_targets(targets.resolve(target, project_root()))

    @app.command("apply-reconciliation")
    def apply_reconciliation(
        proposal_id: Annotated[str, typer.Argument(help="Persisted proposal id")],
    ) -> None:
        """Apply a stale-base-checked source patch, then regenerate every target."""
        reconcile.apply_proposal(
            proposal_id, targets.resolve(targets.every, project_root())
        )

    @app.command("propose-reconciliation")
    def propose_reconciliation(
        patch: Annotated[
            Path,
            typer.Argument(help="Git-format patch against canonical Python source"),
        ],
    ) -> None:
        """Persist a source patch for separate review and stale-base-checked apply."""
        reconcile.propose_patch(patch)

    @app.command("doctor")
    def doctor_command(
        target: Annotated[str, typer.Argument(help=selector)] = targets.every,
        strict_evidence: Annotated[
            bool,
            typer.Option(
                "--strict-evidence",
                help="Exit nonzero when an installed component is newer than the "
                "evidence ledger (the nightly lane's re-probe trigger)",
            ),
        ] = False,
    ) -> None:
        """Report installed native runtime evidence without updating either CLI."""
        doctor.run_doctor(targets.resolve(target, project_root()), strict_evidence)

    @app.command("serve-resolver-tools")
    def serve_resolver_tools_command() -> None:
        """Serve one worker's question tools over stdio, for out-of-process runtimes."""
        resolve.run_resolver_tool_server()

    resolve_app = typer.Typer(
        help="Drive the persisted resolver, and browse or answer its runs",
        invoke_without_command=True,
        no_args_is_help=False,
    )
    resolve_app.command("status")(show_status)
    resolve_app.command("supervise")(serve_supervisor)
    resolve_app.command("questions")(list_questions)
    resolve_app.command("answer")(answer_questions)
    resolve_app.command("actors")(list_actors)
    resolve_app.command("say")(say_to_actor)
    resolve_app.command("accept")(accept_verification)
    resolve_app.command("retire")(retire_concern)
    resolve_app.command("redirect")(redirect_actor)
    resolve_app.command("park")(park_run)
    resolve_app.command("drain")(drain_run)
    resolve_app.command("refresh")(resolve.refresh_run)
    resolve_app.command("intake")(resolve.preview_intake)
    app.add_typer(resolve_app, name="resolve")

    @resolve_app.callback(invoke_without_command=True)
    def resolve_command(
        context: typer.Context,
        adapter: Annotated[
            str | None,
            typer.Option("--adapter", help=", ".join(targets.builders)),
        ] = None,
        run_id: Annotated[
            str | None,
            typer.Option(
                "--run-id", help="Stable run id; defaults to the source commit"
            ),
        ] = None,
        answer: Annotated[
            list[str] | None,
            typer.Option(
                "--answer",
                help="Answer a parked material question as <question-id>=<value> "
                "(repeatable)",
            ),
        ] = None,
        abort: Annotated[
            str | None,
            typer.Option(
                "--abort",
                help="End this run with the given reason, freeing every concern "
                "worktree and branch. Retains the review branch and the run's "
                "recorded evidence. Requires the run's process to have exited.",
            ),
        ] = None,
        adopt_config: Annotated[
            bool,
            typer.Option(
                "--adopt-config",
                help="Resume a run whose composition moved, re-stamping it onto "
                "the current one. The refusal names which fields moved; adopt "
                "once they read as compatible, rather than aborting and losing "
                "every answer the run has collected.",
            ),
        ] = False,
        admit: Annotated[
            list[str] | None,
            typer.Option(
                "--admit",
                help="Work described in the human's own words (repeatable). It "
                "seeds a run that does not exist yet, alongside whatever notes "
                "the tree holds, and joins one that does — where only the new "
                "evidence is planned and recorded answers and completed work "
                "are kept.",
            ),
        ] = None,
        admit_note: Annotated[
            list[str] | None,
            typer.Option(
                "--admit-note",
                help="Admit a `# lup:` note already written in the tree, as "
                "<file>:<line> (repeatable). Its text is read from the file, so "
                "the admitted concern stays traceable to code.",
            ),
        ] = None,
        admit_issue: Annotated[
            list[int] | None,
            typer.Option(
                "--admit-issue",
                help="Admit an open tracker issue by number (repeatable). Its "
                "title and body are read from the tracker, so the admitted "
                "concern stays traceable to what was filed.",
            ),
        ] = None,
        issues: Annotated[
            bool,
            typer.Option(
                "--issues/--no-issues",
                help="Take the project's open issues as evidence alongside the "
                f"tree's notes, minus anything labelled `{EXCLUDED_LABEL}`.",
            ),
        ] = True,
        wait: Annotated[
            float,
            typer.Option(
                "--wait",
                help="Seconds to wait for a human to answer a material question "
                "before parking the run. Zero parks immediately, so an unattended "
                "invocation is deterministic.",
            ),
        ] = 0.0,
        supervise: Annotated[
            bool,
            typer.Option(
                "--supervise",
                help="Open the supervisor page beside this run. Sugar for a long "
                "--wait plus `lup-devtools harness resolve supervise`, which you "
                "can also run yourself against any run at any time.",
            ),
        ] = False,
        supervise_port: Annotated[
            int, typer.Option("--supervise-port", help="Port for the supervisor page")
        ] = SUPERVISOR_PORT,
        supervise_linger: Annotated[
            bool,
            typer.Option(
                "--supervise-linger",
                help="Leave the supervisor page running after the run exits",
            ),
        ] = False,
        host_retries: Annotated[
            int,
            typer.Option(
                "--host-retries",
                help="How many times to come back to a host that refused — an "
                "exhausted allowance, a rate limit, an unreachable upstream — "
                "before parking the run for a human. Zero parks on the first "
                "refusal. A fault only a person can clear, such as an empty "
                "balance, parks however this is set — after the single probe "
                "that rules out a sibling having rotated the credential.",
            ),
        ] = resolve.HOST_RETRIES,
        host_backoff: Annotated[
            float,
            typer.Option(
                "--host-backoff",
                help="Seconds to wait after the first refusal; each later wait "
                "doubles, up to half an hour between probes.",
            ),
        ] = resolve.HOST_BACKOFF_SECONDS,
        auth_probe_delay: Annotated[
            float,
            typer.Option(
                "--auth-probe-delay",
                help="Seconds to let a credential settle before the one fresh "
                "session that tells a rotated token from a dead one. Sessions "
                "share a credential file, so a sibling's refresh denies every "
                "other session in the words a dead credential uses; the probe "
                "is what separates them.",
            ),
        ] = resolve.AUTH_PROBE_SECONDS,
        max_parallel_workers: Annotated[
            int,
            typer.Option(
                "--max-parallel-workers",
                help="How many concerns may hold a session at once. Uncapped, a "
                "batch opens one per runnable concern — a measured run reached "
                "eleven in the same second, which spends the host's allowance "
                "at the width of the batch, races the credential file every "
                "session shares, and loses all of it to one interruption.",
            ),
        ] = 4,
        start_new: Annotated[
            bool,
            typer.Option(
                "--new",
                help="Start a fresh run even though this project has an "
                "unfinished one. Without it, an unfinished run is put to you "
                "rather than left behind: a run id defaults to the commit it "
                "started from, so the default moves at every commit and a bare "
                "rerun would otherwise strand every answer already collected.",
            ),
        ] = False,
        recheck_standing_per_join: Annotated[
            bool,
            typer.Option(
                "--recheck-standing-per-join",
                help="After each join, re-check every concern already in the tree "
                "that the join touched. Buys attribution — the join that broke a "
                "criterion is named — at a reviewer turn per overlapping pair, "
                "which grows quadratically. The final pass examines every concern "
                "against the finished tree either way.",
            ),
        ] = False,
        detach: Annotated[
            bool,
            typer.Option(
                "--detach",
                help=(
                    "Start the run and return, instead of holding this terminal "
                    "until it parks. The run directory is the only contract, so "
                    "the page and an agent reach it as peers afterwards"
                ),
            ),
        ] = False,
    ) -> None:
        """Drive the shared persisted resolver through one explicit native adapter."""
        if context.invoked_subcommand is not None:
            return
        admitted = resolve.AdmissionFlags(
            statements=admit or [], notes=admit_note or [], issues=admit_issue or []
        )
        if detach:
            if adapter is None:
                raise typer.BadParameter(
                    "--adapter is required to drive a resolver run"
                )
            # Ending a run reads recorded state and frees worktrees: it takes
            # no turn, so there is nothing for a child to outlive this command
            # with, and a relaunch carrying no `--abort` would start the run it
            # was asked to end.
            if abort is not None:
                raise typer.BadParameter(
                    "a run cannot be started detached and ended in one command"
                )
            resolve.detach_resolve(
                resolve.DetachedRun(
                    adapter=adapter,
                    run_id=run_id,
                    answers=answer or [],
                    admitted=admitted,
                    issues=issues,
                    wait=wait,
                    host_retries=host_retries,
                    host_backoff=host_backoff,
                    supervisor=resolve.SupervisorSpawn(
                        enabled=supervise,
                        port=supervise_port,
                        linger=supervise_linger,
                    ),
                    adopt_config=adopt_config,
                    auth_probe_delay=auth_probe_delay,
                    max_parallel_workers=max_parallel_workers,
                    recheck_standing_per_join=recheck_standing_per_join,
                )
            )
            return
        # Ending a run reads its recorded state and frees its worktrees; no turn
        # is taken and no skill invocation is rendered, so the one thing an
        # adapter decides never comes up.
        if adapter is None and abort is None:
            raise typer.BadParameter("--adapter is required to drive a resolver run")
        spawn = resolve.SupervisorSpawn(
            enabled=supervise, port=supervise_port, linger=supervise_linger
        )
        resolve.run_resolve(
            # An abort needs no adapter, and asking for one reads as a bug. The
            # core still holds a composition, so ending a run without the flag
            # takes the first declared adapter and never asks it for anything.
            targets.resolve(adapter or next(iter(targets.builders)), project_root())[0],
            run_id,
            answer or [],
            abort,
            spawn.waiting(wait),
            spawn,
            resolve.admission_request(admitted),
            model,
            adopt_config,
            issues,
            host_retries,
            host_backoff,
            auth_probe_delay,
            max_parallel_workers,
            recheck_standing_per_join,
            start_new,
        )

    claude_target = targets.builder("claude")
    if claude_target is not None:
        app.add_typer(create_profile_app(directory), name="profile")

        @app.command(
            "claude",
            context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
        )
        def claude(
            ctx: typer.Context,
            profile: Annotated[
                str | None,
                typer.Option("--profile", "-p", help="Claude config-directory profile"),
            ] = None,
            model: Annotated[
                str | None,
                typer.Option("--model", "-m", help="Native model override"),
            ] = None,
            generate_only: Annotated[
                bool,
                typer.Option("--generate-only", help="Generate without launching"),
            ] = False,
        ) -> None:
            """Generate/reconcile Claude artifacts and launch the verified plugin."""
            launch.launch_claude(
                claude_target(project_root()),
                ctx.args,
                directory,
                profile,
                model,
                generate_only,
            )

    codex_target = targets.builder("codex")
    if codex_target is not None:

        @app.command(
            "codex",
            context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
        )
        def codex(
            ctx: typer.Context,
            codex_home: Annotated[
                Path | None,
                typer.Option(
                    "--codex-home", help="Override the worktree-scoped Codex home"
                ),
            ] = None,
            profile: Annotated[
                str | None,
                typer.Option("--profile", "-p", help="Codex named config overlay"),
            ] = None,
            model: Annotated[
                str | None,
                typer.Option("--model", "-m", help="Native model override"),
            ] = None,
            generate_only: Annotated[
                bool,
                typer.Option("--generate-only", help="Generate without launching"),
            ] = False,
            force_install: Annotated[
                bool,
                typer.Option(
                    "--force-install",
                    help="Reinstall even when the cached digest matches",
                ),
            ] = False,
        ) -> None:
            """Generate/reconcile Codex artifacts and launch without updating the CLI."""
            launch.launch_codex(
                codex_target(project_root()),
                ctx.args,
                codex_home,
                profile,
                model,
                generate_only,
                force_install,
            )

    return app
