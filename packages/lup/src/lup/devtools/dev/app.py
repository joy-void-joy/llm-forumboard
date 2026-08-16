"""Typer command tree for dev operations: worktrees, branches, PRs, checks.

Everything here is workflow rather than domain, so what it needs to know
about the repository it runs in arrives as a declaration: the project facts
the scans read, the harness targets and generated files the gate checks, and
the declarations the policy and plugin commands explain. An application adds
whatever else its own `dev` tree offers to the app it gets back.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import sh
import typer
from pydantic import BaseModel

import lup.devtools.dev.antipatterns as antipatterns_mod
import lup.devtools.dev.boundaries as boundaries_mod
import lup.devtools.dev.branches as branches
import lup.devtools.dev.check as check
import lup.devtools.dev.comments as comments
import lup.devtools.dev.commit_guard as commit_guard
import lup.devtools.dev.conflicts as conflicts
import lup.devtools.dev.issues as issues_mod
import lup.devtools.dev.model_config as model_config_mod
import lup.devtools.dev.plugin as plugin_mod
import lup.devtools.dev.policy_explain as policy_explain
import lup.devtools.dev.pr as pr
import lup.devtools.dev.relocate as relocate_mod
import lup.devtools.dev.resolve_review as resolve_review
import lup.devtools.dev.rules as rules
import lup.devtools.dev.worktree as worktree
from lup.codescan.markers import NoteKind
from lup.devtools.utils import repository_slug
from lup.devtools.harness.composition import NativeTargets
from lup.devtools.harness.drift import RepositoryWriter
from lup.devtools.harness.launch import relocation_hint
from lup.devtools.project import DevProject
from lup.harness.models import HookSet, Plugin, PromptDocument
from lup.policy.kernel.edit import SUPPRESSION_COLUMN_LIMIT
from lup.policy.vocabulary import default_vocabulary
from lup.workspace.paths import project_root


class DevDeclarations(BaseModel, frozen=True):
    """Everything the dev tree reads about the repository it is running in.

    Read when a command runs rather than when the CLI is composed: each of
    these resolves against the working directory, and a CLI is imported long
    before anyone knows which repository it will be pointed at.
    """

    project: DevProject
    hooks: HookSet
    plugin: Plugin
    test_roots: list[check.TestRoot]


def create_dev_app(
    declared: Callable[[], DevDeclarations],
    native_targets: NativeTargets,
    repository_writers: list[RepositoryWriter],
    guidance: PromptDocument,
    relocate_roots: list[Path],
) -> typer.Typer:
    """Wire the dev command tree over what one repository declares about itself."""
    app = typer.Typer(no_args_is_help=True)
    worktree_app = typer.Typer(no_args_is_help=True)
    pr_app = typer.Typer(no_args_is_help=True)
    conflict_app = typer.Typer(no_args_is_help=True)
    plugin_app = typer.Typer(no_args_is_help=True)
    guard_app = typer.Typer(no_args_is_help=True)
    app.add_typer(worktree_app, name="worktree", help="Worktree management")
    app.add_typer(pr_app, name="pr", help="PR lifecycle (status, merge, push, checks)")
    app.add_typer(
        conflict_app, name="conflict", help="Merge/rebase conflict resolution"
    )
    app.add_typer(plugin_app, name="plugin", help="Local plugin marketplace wiring")
    app.add_typer(
        guard_app,
        name="commit-guard",
        help="The pre-commit hook refusing stale generated artifacts",
    )
    app.add_typer(
        model_config_mod.create_model_config_app(),
        name="model-config",
        help="Pydantic configuration census and equivalence",
    )

    # -- worktree commands --

    @worktree_app.command("create")
    def worktree_create_cmd(
        name: Annotated[
            str, typer.Argument(help="Name for the worktree (e.g., feat-name)")
        ],
        no_sync: Annotated[
            bool,
            typer.Option("--no-sync", help="Skip running uv sync"),
        ] = False,
        no_copy_data: Annotated[
            bool,
            typer.Option("--no-copy-data", help="Skip copying gitignored extras"),
        ] = False,
        base_branch: Annotated[
            str | None,
            typer.Option("--base", "-b", help="Base branch (default: current branch)"),
        ] = None,
        force: Annotated[
            bool,
            typer.Option(
                "--force",
                help="Delete an existing unregistered directory at the worktree path",
            ),
        ] = False,
    ) -> None:
        """Create or re-attach a git worktree."""
        worktree.create(
            name, no_sync, no_copy_data, base_branch, relocation_hint, force=force
        )

    @worktree_app.command("list")
    def worktree_list_cmd() -> None:
        """List all git worktrees with branch and status info."""
        worktree.list_worktrees()

    @worktree_app.command("remove")
    def worktree_remove_cmd(
        name: Annotated[str, typer.Argument(help="Worktree name or path to remove")],
        force: Annotated[
            bool,
            typer.Option("--force", help="Force removal even if dirty"),
        ] = False,
    ) -> None:
        """Remove a git worktree."""
        worktree.remove(name, force)

    # -- branch commands --

    @app.command("branches")
    def branches_cmd(
        branch: Annotated[
            str | None,
            typer.Argument(help="Specific branch to check (default: all)"),
        ] = None,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """Analyze branch containment, PR status, and worktree info."""
        branches.branch_status(branch, as_json)

    @app.command("base-branch")
    def base_branch_cmd(
        branch: Annotated[
            str | None,
            typer.Argument(help="Branch to analyze (default: current)"),
        ] = None,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """Detect the base branch for the current (or specified) branch."""
        branches.base_branch(branch, as_json)

    # -- pr-body command --

    @app.command("pr-body")
    def pr_body_cmd(
        base: Annotated[
            str | None,
            typer.Option("--base", "-b", help="Override base branch"),
        ] = None,
    ) -> None:
        """Generate a PR body (summary, commits, test plan) from branch commits."""
        branches.pr_body(base)

    # -- branch survey and delete --

    @app.command("survey")
    def survey_cmd(
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """Full branch inventory: containment, PRs, unique commits, diff sizes."""
        branches.survey(as_json)

    @app.command("merge-driver")
    def merge_driver_cmd() -> None:
        """Register the ownership-manifest merge driver `.gitattributes` names."""
        worktree.register_merge_driver()
        typer.echo(f"Registered merge driver: {worktree.OWNERSHIP_MERGE_DRIVER}")

    @app.command("delete")
    def delete_cmd(
        name: Annotated[str, typer.Argument(help="Branch name to delete")],
        dry_run: Annotated[
            bool,
            typer.Option("--dry-run", "-n", help="Show what would happen"),
        ] = False,
        force: Annotated[
            bool,
            typer.Option(
                "--force",
                "-f",
                help="Force delete the branch and a worktree holding modified files",
            ),
        ] = False,
        remote: Annotated[
            bool | None,
            typer.Option(
                "--remote/--no-remote",
                help="Delete origin's copy too (default: only if merged)",
            ),
        ] = None,
    ) -> None:
        """Delete a branch and its worktree, and origin's copy if it is spent."""
        branches.delete_branch(name, dry_run, force, remote)

    @app.command("resolve-branch")
    def resolve_branch_cmd(
        concern_id: Annotated[
            str, typer.Argument(help="Concern slug; becomes the resolve/<id> branch")
        ],
    ) -> None:
        """Create + switch to the resolve/<id> branch (a resolve editor's first step).

        Runs through the allowlisted `uv run lup-devtools` path so the bash hook
        needs no special case for the editor — autonomy for the editor lives in
        the edit hook.
        """
        branches.create_resolve_branch(concern_id)

    @app.command("resolve-review")
    def resolve_review_cmd(
        manifest: Annotated[
            Path,
            typer.Argument(help="Manifest JSON: workflow task output or a bare array"),
        ],
        base: Annotated[
            str,
            typer.Option(
                "--base", help="Snapshot base ref the resolve branches diff against"
            ),
        ],
        out: Annotated[
            Path,
            typer.Option("--out", help="Output HTML path"),
        ] = Path("tmp/resolve-review.html"),
        intro: Annotated[
            Path | None,
            typer.Option(
                "--intro", help="HTML fragment prepended as a run-specific header"
            ),
        ] = None,
    ) -> None:
        """Render a resolve manifest and its branch diffs into one static HTML review.

        One section per concern: the generalized spec, each original note paired
        with the verifier's per-note finding, the editor summary, the verdict, and
        the full colored diff against the base. The resolve skill runs this in
        its human gate so review reads concrete diffs instead of prose.
        """
        resolve_review.build_review(manifest, base, out, intro)

    @app.command("resolve-summary")
    def resolve_summary_cmd(
        manifest: Annotated[
            Path,
            typer.Argument(help="Manifest JSON: workflow task output or a bare array"),
        ],
    ) -> None:
        """Print per-concern verdicts from a resolve manifest.

        The terminal companion to resolve-review: one block per concern with the
        committed/accepted flags, verdict reason, and residual, for planning merge
        order and approval batches before opening the HTML page.
        """
        resolve_review.summarize(manifest)

    # -- conflict commands --

    @conflict_app.command("list")
    def conflict_list_cmd(
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """Show conflicted files with scope classification (in-scope vs out-of-scope)."""
        conflicts.conflicts(as_json)

    @conflict_app.command("status")
    def conflict_status_cmd(
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """Detect conflict state, list files, and show both sides' history."""
        conflicts.conflict_status(as_json)

    @conflict_app.command("audit")
    def conflict_audit_cmd(
        files: Annotated[
            list[str],
            typer.Argument(help="Files to audit for accidental deletions"),
        ],
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """Post-resolution deletion audit: check for accidentally dropped code."""
        conflicts.conflict_audit(files, as_json)

    @conflict_app.command("complete")
    def conflict_complete_cmd(
        dry_run: Annotated[
            bool,
            typer.Option("--dry-run", "-n", help="Show what would happen"),
        ] = False,
    ) -> None:
        """Finalize the merge/rebase/cherry-pick after all conflicts are resolved."""
        conflicts.conflict_complete(dry_run)

    # -- commit-guard commands --

    GUARD = commit_guard.CommitGuard()

    @guard_app.command("install")
    def guard_install_cmd(
        force: Annotated[
            bool,
            typer.Option("--force", help="Replace a pre-commit hook written elsewhere"),
        ] = False,
    ) -> None:
        """Install the pre-commit hook that refuses stale generated artifacts.

        Idempotent, and shared by every worktree of the clone it is run from,
        so re-running it after a library upgrade refreshes an older body.
        """
        try:
            state = commit_guard.install_guard(GUARD, project_root(), force=force)
        except commit_guard.GuardConflict as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(1) from error
        typer.echo(state.describe())

    @guard_app.command("status")
    def guard_status_cmd() -> None:
        """Report whether this clone refuses a stale artifact at commit time."""
        typer.echo(commit_guard.read_guard(GUARD, project_root()).describe())

    @guard_app.command("uninstall")
    def guard_uninstall_cmd() -> None:
        """Remove the hook, leaving a pre-commit hook written elsewhere alone."""
        typer.echo(commit_guard.uninstall_guard(GUARD, project_root()).describe())

    # -- check command --

    @app.command("check")
    def check_cmd(
        fix: Annotated[
            bool,
            typer.Option("--fix", help="Auto-fix formatting and lint issues"),
        ] = False,
        no_test: Annotated[
            bool,
            typer.Option("--no-test", help="Skip pytest"),
        ] = False,
        antipatterns: Annotated[
            bool,
            typer.Option(
                "--antipatterns",
                help="Audit repository files for missing/spurious `# lup: ignore` "
                "markers only — the same lup.codescan.antipatterns rules the edit "
                "hook enforces",
            ),
        ] = False,
        boundaries: Annotated[
            bool,
            typer.Option(
                "--boundaries",
                help="Scan for native adapter imports outside composition roots "
                "only — the lup.codescan.boundaries guard the full check also runs",
            ),
        ] = False,
        placement: Annotated[
            bool,
            typer.Option(
                "--placement",
                help="List library data tables no adopter can replace only — the "
                "lup.codescan.boundaries placement guard the full check also runs",
            ),
        ] = False,
        stats: Annotated[
            bool,
            typer.Option(
                "--stats",
                help="With --antipatterns: tally findings by rule and kind instead "
                "of listing each — the sweep triage view",
            ),
        ] = False,
        as_json: Annotated[
            bool,
            typer.Option(
                "--json", help="Output findings as JSON (with --antipatterns)"
            ),
        ] = False,
        since: Annotated[
            str | None,
            typer.Option(
                "--since",
                help="Scope the note and anti-pattern gates to paths changed since "
                "this ref, for a tree that holds work it is not answerable for",
            ),
        ] = None,
        path: Annotated[
            list[str] | None,
            typer.Option(
                "--path",
                help="With --antipatterns: audit only files under these paths, for "
                "the fix-one-file loop. Repeatable",
            ),
        ] = None,
    ) -> None:
        """Run ruff format, ruff check, pyright, and pytest. Read-only by default."""
        declarations = declared()
        if antipatterns:
            if stats:
                antipatterns_mod.summarize(declarations.project, as_json, path)
            else:
                antipatterns_mod.report(declarations.project, as_json, path)
            return
        if boundaries:
            boundaries_mod.report(declarations.project, as_json)
            return
        if placement:
            boundaries_mod.report_placement(as_json)
            return
        check.run_checks(
            fix=fix,
            no_test=no_test,
            project=declarations.project,
            test_roots=declarations.test_roots,
            compositions=native_targets.resolve(native_targets.every, project_root()),
            repository_writers=repository_writers,
            guidance=guidance,
            scope=check.changed_paths(since) if since is not None else None,
        )

    # -- comments command --

    @app.command("comments")
    def comments_cmd(
        targets: Annotated[
            list[str] | None,
            typer.Argument(help="file:line markers to remove with --clear"),
        ] = None,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
        kind: Annotated[
            NoteKind | None,
            typer.Option("--kind", help="Show only this note flavor"),
        ] = None,
        commit: Annotated[
            bool,
            typer.Option(
                "--commit", help="Commit comment-bearing files as a prompt snapshot"
            ),
        ] = False,
        clear: Annotated[
            bool,
            typer.Option(
                "--clear",
                help="Strip the file:line markers given (only on a resolve/* branch)",
            ),
        ] = False,
        wake: Annotated[
            bool,
            typer.Option(
                "--wake",
                help="With --clear: also strip the defer notes named, waking them",
            ),
        ] = False,
        retire: Annotated[
            bool,
            typer.Option(
                "--retire",
                help="Delete the solved claims named as file:line (verify pass only)",
            ),
        ] = False,
        restore: Annotated[
            bool,
            typer.Option(
                "--restore",
                help="Reopen the solved claims named as file:line as open feedback",
            ),
        ] = False,
        narrow: Annotated[
            str | None,
            typer.Option(
                "--narrow",
                help="With --restore and one target: the still-outstanding text",
            ),
        ] = None,
    ) -> None:
        """List unresolved `# lup:` feedback comments, or act on specific ones.

        With --clear, removes each `file:line` marker named as an argument; used
        at fork time to strip a concern's own notes from an editor's worktree.
        Deferred notes are skipped unless --wake is passed as well.

        With --retire or --restore, applies the verify-solved pass's verdicts to
        `# lup: solved:` claims — and only to claims: any other note is refused.
        """
        if sum([clear, retire, restore]) > 1:
            typer.echo("--clear, --retire, and --restore are exclusive", err=True)
            raise typer.Exit(2)
        if narrow is not None and (not restore or len(targets or []) != 1):
            typer.echo("--narrow needs --restore and exactly one target", err=True)
            raise typer.Exit(2)
        if retire or restore:
            comments.revise_claims(targets or [], retire=retire, narrow=narrow)
            return
        if clear:
            comments.clear_markers(targets or [], wake=wake)
            return
        comments.report(as_json, commit, kind)

    @app.command("todos")
    def todos_cmd(
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """List `# lup: template:` markers — a scaffold's open decisions.

        Every place a scaffolded project needs a domain decision carries a
        `# lup: template:` note. Initialization runs this to gather them all
        and walk them one by one, so no customization point depends on someone
        remembering to mention it. An alias for `dev comments --kind template`:
        same scan, same file/line/text/context shape, narrowed to that flavor.
        """
        comments.report(as_json, commit=False, kind=NoteKind.template)

    @app.command("directives")
    def directives_cmd(
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
        limit: Annotated[
            int,
            typer.Option("--limit", help="Column budget the inline placement gets"),
        ] = SUPPRESSION_COLUMN_LIMIT,
        fix: Annotated[
            bool,
            typer.Option(
                "--fix", help="Move each directive to its canonical placement"
            ),
        ] = False,
    ) -> None:
        """Measure every `# lup: ignore` against the canonical inline placement.

        Placement is uniform — a directive sits on the line it guards, or
        stands alone directly above it — and the inline form is the canonical
        one. This reports which sites the column budget lets stay inline and
        which only fit above, so the fallback is sized against the tree rather
        than assumed.
        """
        if fix:
            moved = antipatterns_mod.place_directives(declared().project, limit)
            typer.echo(f"{len(moved)} file(s) replaced")
            for rel in moved:
                typer.echo(f"  {rel}")
            return
        antipatterns_mod.report_directives(declared().project, as_json, limit)

    @app.command("report-friction")
    def report_friction_cmd(
        summary: Annotated[str, typer.Option("--summary")],
        component: Annotated[str, typer.Option("--component")],
        command: Annotated[str, typer.Option("--command")],
        error: Annotated[str, typer.Option("--error")],
        state: Annotated[str, typer.Option("--state")],
        recovery_cost: Annotated[str, typer.Option("--recovery-cost")],
        issue: Annotated[int | None, typer.Option("--issue", min=1)] = None,
    ) -> None:
        """File or correct workflow friction in this checkout's repository."""
        report = issues_mod.FrictionReport(
            summary=summary,
            component=component,
            command=command,
            error=error,
            state=state,
            recovery_cost=recovery_cost,
        )
        try:
            url = report.file(issue=issue)
        except (RuntimeError, sh.ErrorReturnCode) as failure:
            typer.echo(str(failure), err=True)
            raise typer.Exit(1) from failure
        typer.echo(url)

    @app.command("issues")
    def issues_cmd(
        excluded: Annotated[
            str,
            typer.Option("--excluded", help="Label that withholds an issue"),
        ] = issues_mod.EXCLUDED_LABEL,
    ) -> None:
        """List the open issues a resolver run would take as evidence.

        Answerable without starting a run, which is the whole point: a run
        leases a worktree per concern, so "what would this plan from?" should
        not cost one.
        """
        found = issues_mod.fetch_open_issues(excluded)
        slug = repository_slug()
        typer.echo(f"{len(found)} open issue(s) in {slug or 'this repository'}")
        for issue in found:
            typer.echo(f"  {issue.reference()}  {issue.title}")

    @app.command("rules")
    def rules_cmd(
        check_only: Annotated[
            bool,
            typer.Option("--check", help="Fail when docs/rules.md is stale"),
        ] = False,
    ) -> None:
        """Generate the Lup rule and typed-suppression reference."""
        try:
            destination = rules.write_rule_reference(check=check_only)
        except RuntimeError as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(1) from error
        verb = "verified" if check_only else "written"
        typer.echo(f"Lup rule reference {verb}: {destination}")

    @app.command("relocate")
    def relocate_cmd(
        moves: Annotated[
            list[str],
            typer.Argument(
                help="Module relocations, each spelled old.module=new.module"
            ),
        ],
        root: Annotated[
            list[Path] | None,
            typer.Option("--root", help="Source root to rewrite (repeatable)"),
        ] = None,
    ) -> None:
        """Repoint every import of a module that moved between the two halves."""

        def parsed(move: str) -> relocate_mod.Relocation:
            # This CLI's own flag grammar, not structured data with a parser.
            old, separator, new = move.partition("=")  # lup: ignore[string-split]
            sides = [relocate_mod.name_parts(old), relocate_mod.name_parts(new)]
            if not separator or any(side is None for side in sides):
                typer.echo(f"expected old.module=new.module; got {move!r}", err=True)
                raise typer.Exit(2)
            return relocate_mod.Relocation(old=sides[0] or [], new=sides[1] or [])

        declared = [parsed(move) for move in moves]
        roots = [path for path in root or relocate_roots if path.exists()]
        for edit in relocate_mod.relocate(roots, declared):
            typer.echo(f"{edit.path}: {edit.imports} import(s)")
        for mention in relocate_mod.surviving_mentions(roots, declared):
            typer.echo(f"still mentions a moved module: {mention}", err=True)

    @app.command("policy")
    def policy_cmd(
        subjects: Annotated[
            list[str],
            typer.Argument(help="Commands, URLs, or paths to classify"),
        ],
        kind: Annotated[
            str,
            typer.Option("--kind", help="What the inputs are: shell, fetch, or edit"),
        ] = "shell",
        sandbox: Annotated[
            bool,
            typer.Option("--sandbox", help="Judge as an OS-sandboxed session would"),
        ] = False,
        autonomous: Annotated[
            bool,
            typer.Option(
                "--autonomous", help="Judge as a self-reviewing identity would"
            ),
        ] = False,
        as_json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
    ) -> None:
        """Show what the declared permission policy decides about an input, and why."""
        if kind not in ("shell", "fetch", "edit"):
            typer.echo(
                f"unknown kind {kind!r}: expected shell, fetch, or edit", err=True
            )
            raise typer.Exit(2)
        policy_explain.explain(
            subjects, kind, sandbox, autonomous, as_json, declared().hooks
        )

    @app.command("vocabulary")
    def vocabulary_cmd(
        offered: Annotated[
            bool,
            typer.Option("--offered", help="Survey lup's offered defaults instead"),
        ] = False,
        as_json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
        output: Annotated[
            Path | None,
            typer.Option("--output", help="Write the survey here instead of stdout"),
        ] = None,
        provenance: Annotated[
            bool,
            typer.Option(
                "--provenance", help="List rules and where each axis came from"
            ),
        ] = False,
    ) -> None:
        """Show every shell form the declared vocabulary judges, and how."""
        rules = default_vocabulary() if offered else list(declared().hooks.shell_rules)
        policy_explain.survey(rules, as_json, output, provenance)

    # -- plugin commands --

    @plugin_app.command("name")
    def plugin_name_cmd(
        name: Annotated[
            str | None,
            typer.Argument(help="Marketplace name (default: pyproject [project].name)"),
        ] = None,
        dry_run: Annotated[
            bool,
            typer.Option("--dry-run", "-n", help="Show changes without writing"),
        ] = False,
    ) -> None:
        """Name this repo's plugin marketplace uniquely (the plugin entry is kept).

        Marketplace names share one global namespace, so a shared name collides
        across repos and worktrees and installs shadow each other. Naming the
        marketplace after the project fixes that.
        """
        plugin_mod.name_marketplace(declared().plugin, name, dry_run)

    # -- pr commands --

    @pr_app.command("status")
    def pr_status_detail_cmd(
        branch: Annotated[
            str | None,
            typer.Option("--branch", "-b", help="Branch name (default: current)"),
        ] = None,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """Fetch PR review status, checks, and comments for a branch."""
        pr.status(branch, as_json)

    @pr_app.command("merge")
    def pr_merge_cmd(
        pr_number: Annotated[int, typer.Argument(help="PR number to merge")],
        dry_run: Annotated[
            bool,
            typer.Option("--dry-run", "-n", help="Show what would happen"),
        ] = False,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
        method: Annotated[
            pr.MergeMethod,
            typer.Option("--method", help="How the commits reach the base branch"),
        ] = pr.MergeMethod.merge,
        gh_args: Annotated[
            list[str] | None,
            typer.Option("--gh", help="Further flag handed to `gh pr merge` untouched"),
        ] = None,
    ) -> None:
        """Merge a PR and pull changes into the integration branch."""
        pr.merge(pr_number, dry_run, as_json, method, tuple(gh_args or ()))

    @pr_app.command("sync-base")
    def pr_sync_base_cmd(
        base: Annotated[
            str | None,
            typer.Option("--base", "-b", help="Base branch (default: auto-detect)"),
        ] = None,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """Sync the base branch and merge it into the current feature branch."""
        pr.sync_base(base, as_json)

    @pr_app.command("push")
    def pr_push_cmd(
        force: Annotated[
            bool,
            typer.Option("--force", "-f", help="Force push"),
        ] = False,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """Push the current branch and report any existing PR."""
        pr.push(force, as_json)

    @pr_app.command("create")
    def pr_create_cmd(
        base: Annotated[str, typer.Option("--base", help="Target branch for PR")],
        title: Annotated[str, typer.Option("--title", help="PR title")],
        body: Annotated[
            str | None, typer.Option("--body", help="PR body (markdown)")
        ] = None,
        body_file: Annotated[
            Path | None,
            typer.Option("--body-file", help="Read the PR body from this file"),
        ] = None,
        as_json: Annotated[
            bool,
            typer.Option("--json", help="Output as JSON"),
        ] = False,
    ) -> None:
        """Create a new PR."""
        pr.create(base, title, pr.resolve_body(body, body_file), as_json)

    @pr_app.command("update")
    def pr_update_cmd(
        pr_number: Annotated[int, typer.Argument(help="PR number to update")],
        body: Annotated[
            str | None, typer.Option("--body", help="New PR body (markdown)")
        ] = None,
        body_file: Annotated[
            Path | None,
            typer.Option("--body-file", help="Read the new PR body from this file"),
        ] = None,
    ) -> None:
        """Update a PR body."""
        pr.update(pr_number, pr.resolve_body(body, body_file))

    return app
