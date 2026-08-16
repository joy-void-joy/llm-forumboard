"""Ask this project's permission policy what it thinks, without running it.

Tuning a vocabulary you cannot query is guesswork. Before this, finding out
whether a command asks meant adding a fixture to the semantic-policy suite and
running the file — so nobody swept the everyday commands, and each one earned
its allowance only after annoying somebody enough to be reported. The same
question is also what an agent wants at the moment it is stopped, where the
reason string alone leaves it guessing which clause it tripped.

The policy read here is the declared one — the same ``HookSet`` both generated
plugins are compiled from — so an answer here is the answer a session gets.
"""

from pathlib import Path
from typing import Annotated

import typer

from pydantic import AnyHttpUrl

from lup.harness.enforcement import declared_path_rules, semantic_policy_for
from lup.policy.models import Decision, FetchUrl, ShellCommand
from lup.workspace.paths import project_root
from lup_template.devtools.harness.catalog import declared_hook_set
from lup.devtools.utils import output_json

app = typer.Typer(help="Query the permission policy", no_args_is_help=True)


def shell_decision(
    command: str, autonomous: bool, interactive: bool, trapped: bool = False
) -> Decision:
    """Classify one shell command exactly as a live session would.

    The host facts a decision needs — which redirect targets already exist,
    which operands Git could restore, which are directories — are resolved by
    the policy itself against ``cwd``, so this answer is the answer a session
    standing here would get rather than one reached without them.

    ``trapped`` asks the other question a placement raises: what a session an
    OS sandbox confines, on a runtime that puts no single call outside it,
    is told about a command that has to run outside. Left off, the answer is
    the declared verdict — the placement itself, which is what a reader wants
    to see. Turned on, it is the refusal that boundary reaches.
    """
    policy = semantic_policy_for(
        declared_hook_set(),
        autonomous=autonomous,
        interactive=interactive,
        sandbox_active=trapped,
    )
    return policy.decide(ShellCommand(command=command, cwd=project_root()))


def report(subject: str, decision: Decision, as_json: bool) -> None:
    """Print one verdict, and exit non-zero on anything but an allow.

    The exit code is what makes this usable from a sweep: a batch of commands
    a project expects to pass fails the run when one of them stops passing.
    """
    if as_json:
        output_json({"subject": subject, **decision.model_dump()})
    else:
        typer.echo(f"{decision.effect:>5}  {subject}")
        match decision.sandbox:
            case "ambient":
                pass
            case "escalable":
                typer.echo("       runs inside the sandbox, and may be taken out")
            case placement:
                typer.echo(f"       runs {placement} the sandbox")
        if decision.reason:
            typer.echo(f"       {decision.reason}")
    if decision.effect != "allow":
        raise typer.Exit(1)


@app.command("classify")
def classify_command(
    command: Annotated[str, typer.Argument(help="The shell command to classify")],
    autonomous: Annotated[
        bool, typer.Option("--autonomous", help="Judge as a reviewed worker session")
    ] = False,
    headless: Annotated[
        bool, typer.Option("--headless", help="Judge with no human to answer an ask")
    ] = False,
    trapped: Annotated[
        bool,
        typer.Option(
            "--trapped",
            help="Judge as a confined session whose runtime cannot escape",
        ),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Say what the policy decides about one shell command, and why.

    Examples::

        $ uv run lup-devtools hooks classify 'gh api /repos/o/r/pulls/1'
        $ uv run lup-devtools hooks classify 'rm build/out' --json
        $ uv run lup-devtools hooks classify 'uv run lup-devtools dev check' --trapped
    """
    report(command, shell_decision(command, autonomous, not headless, trapped), as_json)


@app.command("classify-fetch")
def classify_fetch(
    url: Annotated[str, typer.Argument(help="The URL to classify")],
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Say whether a URL is inside this project's declared fetch scopes."""
    policy = semantic_policy_for(declared_hook_set())
    report(url, policy.decide(FetchUrl(url=AnyHttpUrl(url))), as_json)


@app.command("sweep")
def sweep_commands(
    file: Annotated[Path, typer.Argument(help="A file of commands, one per line")],
    autonomous: Annotated[
        bool, typer.Option("--autonomous", help="Judge as a reviewed worker session")
    ] = False,
    headless: Annotated[
        bool, typer.Option("--headless", help="Judge with no human to answer an ask")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Classify a list of commands at once, and exit non-zero if any is not allowed.

    This is the shape a change to the vocabulary should be checked with: keep
    the everyday commands in a file, and a rule that tightens something it did
    not mean to tighten fails here instead of in somebody's session.
    """
    commands = [
        line.strip()
        for line in file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    decisions = [
        (command, shell_decision(command, autonomous, not headless))
        for command in commands
    ]
    if as_json:
        output_json(
            [
                {"subject": command, **decision.model_dump()}
                for command, decision in decisions
            ]
        )
    else:
        for command, decision in decisions:
            typer.echo(f"{decision.effect:>5}  {command}")
    if any(decision.effect != "allow" for _, decision in decisions):
        raise typer.Exit(1)


@app.command("roots")
def show_roots(
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List the path roles and protected roots the declaration carries.

    What a verdict on a path depends on, in one place — so "why did that ask?"
    is answerable without reading the catalog.
    """
    hooks = declared_hook_set()
    rules = declared_path_rules(hooks)
    if as_json:
        output_json(
            {
                "roles": [role.model_dump(mode="json") for role in hooks.path_roles],
                "protected": [str(root) for root in hooks.protected_edit_roots],
                "human_owned": [str(path) for path in hooks.human_owned_files],
                "rules": len(rules),
            }
        )
        return
    for role in hooks.path_roles:
        typer.echo(f"{role.role:>10}  {role.root}")
    for root in hooks.protected_edit_roots:
        typer.echo(f"{'protected':>10}  {root}")
    for path in hooks.human_owned_files:
        typer.echo(f"{'human':>10}  {path}")
