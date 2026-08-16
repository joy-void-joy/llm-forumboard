"""Root CLI app composing all devtools sub-apps.

All development tooling is exposed as the ``lup-devtools`` entry point.
Each sub-app groups related commands.

The module is ``main.py`` rather than ``__main__.py`` or ``app.py`` because
``lup-devtools`` is launched through the ``[project.scripts]`` console entry
point (``lup_template.devtools.main:app``), never as ``python -m
lup_template.devtools``. A ``__main__.py`` would imply the latter and add a
second launch path to keep in sync; ``app.py`` is reserved for the per-sub-app
modules (``dev/app.py``, ``trace/app.py``, ...) so the root composer keeps a
distinct name.

Examples::

    $ uv run lup-devtools --help
    $ uv run lup-devtools agent inspect --json
    $ uv run lup-devtools py info requests
    $ uv run lup-devtools trace show <session_id>
    $ uv run lup-devtools feedback status
    $ uv run lup-devtools dev branches
    $ uv run lup-devtools dev worktree create feat-name
    $ uv run lup-devtools dev check --no-test
    $ uv run lup-devtools report
    $ uv run lup-devtools version
    $ uv run lup-devtools sync status
    $ uv run lup-devtools usage claude --no-detail
"""

from pathlib import Path

import typer

import lup_template.agent.prompts as prompts
from lup.devtools.subapps import SubApp, compose
from lup.workspace.paths import find_nearest_pyproject
from lup_template.devtools.agent import app as agent_app
from lup.devtools.dashboard.app import create_dashboard_app
from lup.devtools.dev import conflicts
from lup_template.devtools.dev.app import app as dev_app
from lup.devtools.feedback.app import create_feedback_app
from lup.devtools.feedback.models import AgentPrompt
from lup.devtools.harness.app import create_harness_app
from lup.devtools.harness.resolve import ConfiguredModel
from lup_template.agent.config import engine_for_model, settings
from lup_template.devtools.harness.composition import (
    REPOSITORY_WIDE,
    TARGETS,
    profile_directory,
)
from lup_template.devtools.setup import INTEGRATIONS
from lup_template.devtools.hooks.app import app as hooks_app
from lup.devtools.report.app import create_report_app
from lup_template.devtools.setup import app as setup_app
from lup_template.devtools.subapps import APPLICATION_SPECS, INHERITED


def assembled_prompt() -> AgentPrompt:
    """This application's system prompt, as the health report weighs it.

    The source is the module that renders the sections rather than a path
    matched by shape, so renaming the package during initialization moves it.
    """
    return AgentPrompt(
        sections=prompts.SECTIONS,
        rendered=prompts.get_system_prompt(),
        source=Path(prompts.__file__),
    )


# lup: ignore[constant-declaration] — this CLI's own composition: which sub-apps
# it takes and under what name, decided here because nothing sits above it
APPLICATION_APPS = {
    "agent": agent_app,
    "dashboard": create_dashboard_app(INTEGRATIONS),
    "dev": dev_app,
    "feedback": create_feedback_app(assembled_prompt),
    "harness": create_harness_app(
        TARGETS,
        REPOSITORY_WIDE,
        ConfiguredModel(name=settings.model, adapter=engine_for_model(settings.model)),
        profile_directory(),
    ),
    "hooks": hooks_app,
    "report": create_report_app(TARGETS, REPOSITORY_WIDE),
    "setup": setup_app,
}
"""Where each application spec meets the Typer app answering to its name.

This module is the composition root and nothing imports it, which is what
lets the apps be named here without the guidance that documents them coming
along. A spec with no app raises on the first invocation rather than serving
a CLI missing a command the docs promise.
"""

app = typer.Typer(
    help="lup-devtools: development and analysis tools",
    pretty_exceptions_show_locals=False,
    no_args_is_help=True,
)

compose(
    app,
    sorted(
        [
            *INHERITED,
            *[
                SubApp(spec=spec, app=APPLICATION_APPS[spec.name])
                for spec in APPLICATION_SPECS
            ],
        ],
        key=lambda entry: entry.spec.name,
    ),
)


@app.callback()
def report_a_conflicted_manifest() -> None:
    """Say what to run when `uv` is about to stop being able to start.

    Every other command here is documented as ``uv run lup-devtools ...``, and
    a conflicted ``pyproject.toml`` turns all of them into a parse error from
    a tool that never reached this program. This runs on whichever invocation
    does get through, so the diagnosis reaches the session before the failure
    does rather than after.
    """
    root = find_nearest_pyproject()
    if root is not None and conflicts.manifest_conflicted(root):
        typer.echo(conflicts.conflicted_manifest_notice(), err=True)
