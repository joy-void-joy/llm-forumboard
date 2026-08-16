"""The general report surface: everything left to implement, in one place.

Every other surface here answers one question — `dev comments` what is still
being asked, `harness check` whether the generated trees are current, `dev
branches` what has not landed, `harness resolve supervise` what one run is
doing. None of them answers "what is left", which is the question a session
ends on and the one a report is written to record.

So this composes them rather than recomputing any of it, and shares its shape
with the report skill: the skill writes what a session knows on top of what
this can see, into the same file, rewritten whole.

Examples::

    $ uv run lup-devtools report
    $ uv run lup-devtools report --json
    $ uv run lup-devtools report --write
"""

from pathlib import Path
from typing import Annotated

import typer

from lup.devtools.harness.composition import NativeTargets
from lup.devtools.harness.drift import RepositoryWriter
from lup.devtools.report.build import build_report
from lup.devtools.report.models import DEFAULT_REPORT_PATH
from lup.devtools.supervisor.doors import resolve_state_root
from lup.devtools.utils import output_json
from lup.workspace.paths import project_root


def create_report_app(
    native_targets: NativeTargets,
    repository_writers: list[RepositoryWriter],
    report_path: Path = DEFAULT_REPORT_PATH,
) -> typer.Typer:
    """Wire the report surface over the artifacts one repository generates.

    The targets and writers are what only an application knows; the root every
    topic and the written path resolve against is found when the command runs,
    because a CLI is imported long before anyone knows where it is pointed. So
    a relative ``report_path`` lands in the tree being reported on rather than
    wherever the command was invoked from.
    """
    app = typer.Typer(invoke_without_command=True, no_args_is_help=False)

    @app.callback()
    def report_cmd(
        as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
        write: Annotated[
            bool,
            typer.Option("--write", help=f"Rewrite {report_path} with this report"),
        ] = False,
    ) -> None:
        """Report everything left to implement, across every surface."""
        root = project_root()
        report = build_report(
            native_targets.resolve(native_targets.every, root),
            repository_writers,
            resolve_state_root(),
            root,
        )
        if write:
            written = root / report_path
            written.parent.mkdir(parents=True, exist_ok=True)
            written.write_text(report.markdown(), encoding="utf-8")
            typer.echo(f"{written}: {report.outstanding()} outstanding item(s)")
            return
        if as_json:
            output_json(report)
            return
        typer.echo(report.markdown())

    return app
