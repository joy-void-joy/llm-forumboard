"""Typer command tree for feedback state, metrics, and session commits.

Every command here reads the trace and session records the loop writes,
which any project on lup produces the same way — except the prompt report,
which weighs the one thing only the application can assemble. That arrives
as a callable, so the prompt is rendered when the command runs rather than
when the CLI is composed.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

import lup.devtools.feedback.analyze as analyze
import lup.devtools.feedback.reports as reports
from lup.devtools.feedback.models import AgentPrompt
from lup.devtools.utils import VERSION_OPT, ALL_VERSIONS_OPT, JSON_OPT


def create_feedback_app(prompt: Callable[[], AgentPrompt]) -> typer.Typer:
    """Wire the feedback tree over one application's assembled prompt."""
    app = typer.Typer(no_args_is_help=True)

    @app.command("status")
    def status_cmd(
        version: VERSION_OPT = None,
        all_versions: ALL_VERSIONS_OPT = False,
    ) -> None:
        """Show feedback status: version, data, analysis state, and stats."""
        reports.status(version, all_versions)

    @app.command("collect")
    def collect_cmd(
        since: Annotated[
            str | None,
            typer.Option(
                "--since", "-s", help="Only sessions after this date (YYYY-MM-DD)"
            ),
        ] = None,
        all_time: Annotated[
            bool,
            typer.Option(
                "--all-time",
                help="Include all sessions regardless of date "
                "(the default when --since is not given)",
            ),
        ] = False,
        version: VERSION_OPT = None,
        all_versions: ALL_VERSIONS_OPT = False,
        output: Annotated[
            Path | None,
            typer.Option("--output", "-o", help="Output file path"),
        ] = None,
        dry_run: Annotated[
            bool,
            typer.Option(
                "--dry-run", "-n", help="Show what would be collected without writing"
            ),
        ] = False,
    ) -> None:
        """Collect feedback metrics from sessions."""
        reports.collect(since, all_time, version, all_versions, output, dry_run=dry_run)

    @app.command("costs")
    def costs_cmd(
        version: VERSION_OPT = None,
        all_versions: ALL_VERSIONS_OPT = False,
        as_json: JSON_OPT = False,
    ) -> None:
        """Per-backend cost/token rollup from session JSONs (any backend)."""
        reports.costs(version, all_versions, as_json)

    @app.command("tools")
    def tools_cmd(
        version: VERSION_OPT = None,
        all_versions: ALL_VERSIONS_OPT = False,
        as_json: JSON_OPT = False,
    ) -> None:
        """Show tool usage aggregates."""
        reports.tools(version, all_versions, as_json)

    @app.command("errors")
    def errors_cmd(
        limit: Annotated[
            int,
            typer.Option("-n", "--limit", help="Max errors to show"),
        ] = 20,
        version: VERSION_OPT = None,
        all_versions: ALL_VERSIONS_OPT = False,
        as_json: JSON_OPT = False,
    ) -> None:
        """Show sessions with high error rates from structured metrics."""
        reports.errors(limit, version, all_versions, as_json)

    @app.command("trends")
    def trends_cmd(
        window: Annotated[
            int,
            typer.Option("-w", "--window", help="Rolling window size"),
        ] = 10,
        version: VERSION_OPT = None,
        all_versions: ALL_VERSIONS_OPT = False,
        as_json: JSON_OPT = False,
    ) -> None:
        """Show metric trends over time."""
        reports.trends(window, version, all_versions, as_json)

    @app.command("history")
    def history_cmd(
        limit: Annotated[
            int,
            typer.Option("-n", "--limit", help="Max to show"),
        ] = 10,
    ) -> None:
        """Show previous feedback collection runs."""
        reports.history(limit)

    @app.command("mark")
    def mark_cmd(
        session_ids: Annotated[
            list[str], typer.Argument(help="Session IDs to mark as analyzed")
        ],
    ) -> None:
        """Mark sessions as analyzed in the feedback loop."""
        reports.mark(session_ids)

    @app.command("unmark")
    def unmark_cmd(
        session_ids: Annotated[list[str], typer.Argument(help="Session IDs to unmark")],
    ) -> None:
        """Remove analysis marks from sessions."""
        reports.unmark(session_ids)

    @app.command("prompt-health")
    def prompt_health_cmd(
        as_json: JSON_OPT = False,
    ) -> None:
        """Analyze the agent prompt for size and patch accumulation."""
        reports.prompt_health(prompt(), as_json)

    @app.command("unanalyzed")
    def unanalyzed_cmd(
        version: VERSION_OPT = None,
        all_versions: ALL_VERSIONS_OPT = False,
    ) -> None:
        """List unanalyzed session IDs, one per line."""
        reports.unanalyzed(version, all_versions)

    @app.command("analyze")
    def analyze_cmd(
        version: VERSION_OPT = None,
        all_versions: ALL_VERSIONS_OPT = False,
        output: Annotated[
            Path | None,
            typer.Option(
                "--output", "-o", help="Write JSON report to file instead of stdout"
            ),
        ] = None,
    ) -> None:
        """Produce a structured JSON analysis report (tools, errors, gaps)."""
        analyze.analyze(version, all_versions, output)

    @app.command("commit")
    def commit_cmd(
        dry_run: Annotated[
            bool,
            typer.Option("--dry-run", "-n", help="Show what would be committed"),
        ] = False,
    ) -> None:
        """Commit all uncommitted session result files, one commit per session."""
        reports.commit(dry_run)

    return app
