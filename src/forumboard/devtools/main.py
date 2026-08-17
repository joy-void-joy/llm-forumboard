"""Root CLI app composing all devtools sub-apps.

All development tooling is exposed as the ``lup-devtools`` entry point.
Each sub-app groups related commands.

The module is ``main.py`` rather than ``__main__.py`` or ``app.py`` because
``lup-devtools`` is launched through the ``[project.scripts]`` console entry
point (``forumboard.devtools.main:app``), never as ``python -m
forumboard.devtools``. A ``__main__.py`` would imply the latter and add a
second launch path to keep in sync; ``app.py`` is reserved for the per-sub-app
modules (``dev/app.py``, ``trace/app.py``, ...) so the root composer keeps a
distinct name.

Examples::

    $ uv run lup-devtools --help
    $ uv run lup-devtools agent inspect --json
    $ uv run lup-devtools py info requests
    $ uv run lup-devtools trace show <session_id>
    $ uv run lup-devtools setup notion
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

from lup.devtools.feedback.models import AgentPrompt
from lup.devtools.roster import DevtoolsDeclarations
from lup.devtools.subapps import SubApp, compose
from lup.workspace.paths import find_nearest_pyproject
from forumboard.devtools.agent import app as agent_app
from lup.devtools.dev import conflicts
from forumboard.devtools.dev.app import app as dev_app, declared
from lup.devtools.harness.resolve import ConfiguredModel
from forumboard.agent.config import engine_for_model, settings
from forumboard.agent.prompts import get_system_prompt
from forumboard.devtools.harness.composition import (
    REPOSITORY_WIDE,
    TARGETS,
    profile_directory,
)
from forumboard.devtools.harness.content.guidance import document as guidance_document
from forumboard.devtools.hooks.app import app as hooks_app
from forumboard.devtools.setup import INTEGRATIONS
from forumboard.devtools.subapps import APPLICATION_SPECS, SELECTION, USAGE_ENTRIES


def agent_prompt() -> AgentPrompt:
    """This project's system prompt, as the health report weighs it.

    One section, because the worldview pass is the only tool-using session
    here and what it is told is written as one document rather than composed
    from parts. The other prompts are per-pass and quoted into each other, so
    no session ever receives them assembled.
    """
    rendered = get_system_prompt()
    return AgentPrompt(
        sections=[rendered],
        rendered=rendered,
        source=Path("src/forumboard/agent/prompts.py"),
    )


DECLARED = DevtoolsDeclarations(
    dev=declared,
    targets=TARGETS,
    repository_writers=REPOSITORY_WIDE,
    guidance=guidance_document(declared().hooks.rules),
    prompt=agent_prompt,
    relocate_roots=[Path("src"), Path("tests")],
    integrations=INTEGRATIONS,
    usage_entries=USAGE_ENTRIES,
    model=ConfiguredModel(
        name=settings.model, adapter=engine_for_model(settings.model)
    ),
    profiles=profile_directory(),
)
"""The facts every sub-app the library ships needs of this repository.

One declaration rather than a call per sub-app: the roster wires each entry
from these, so a factory that grows an argument grows a field here with a
default instead of breaking this call site.
"""

# lup: ignore[constant-declaration] — this CLI's own composition: which Typer app
# answers to each name only it serves, decided here because nothing sits above it
APPLICATION_APPS = {
    "agent": agent_app,
    "dev": dev_app,
    "hooks": hooks_app,
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
    SELECTION.over(
        DECLARED.roster(),
        [
            SubApp(spec=spec, app=APPLICATION_APPS[spec.name])
            for spec in APPLICATION_SPECS
        ],
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
