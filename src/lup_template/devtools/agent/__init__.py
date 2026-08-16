"""Agent introspection and interactive debugging tools.

Commands:
- inspect: Pretty-print the full agent configuration (tools, schemas, prompt, subagents)
- capabilities: Render the backend capability matrix (the parity contract)
- serve-tools: Start SDK tools as an MCP stdio server (used by the ``claude`` runner)
- repl: Interactive REPL with the agent via the SDK (continuous session)

Examples::

    $ uv run lup-devtools agent inspect
    $ uv run lup-devtools agent inspect --json
    $ uv run lup-devtools agent inspect --full
    $ uv run lup-devtools agent capabilities --markdown
    $ uv run lup-devtools agent repl
    $ uv run lup-devtools agent repl --model sonnet --no-prompt
    $ uv run lup-devtools agent repl --exec "ping" --no-tools
    $ uv run lup-devtools agent serve-tools
"""

import asyncio
from typing import Annotated

import typer

import lup_template.devtools.agent.inspect_agent as inspect_agent
import lup_template.devtools.agent.repl as repl
import lup_template.devtools.agent.serve as serve
from lup_template.agent.toolsets import ServerGroup

app = typer.Typer(no_args_is_help=True)


@app.command("inspect")
def inspect_cmd(
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as machine-readable JSON"),
    ] = False,
    full: Annotated[
        bool,
        typer.Option("--full", help="Show full details (tool schemas, full prompt)"),
    ] = False,
) -> None:
    """Inspect the full agent configuration: tools, schemas, prompt, subagents."""
    inspect_agent.run_inspect(as_json, full)


@app.command("capabilities")
def capabilities_cmd(
    markdown: Annotated[
        bool,
        typer.Option("--markdown", help="Emit the README-ready markdown table"),
    ] = False,
) -> None:
    """Show the backend capability matrix (the parity contract, generated)."""
    inspect_agent.run_capabilities(markdown)


@app.command("serve-tools")
def serve_tools_cmd(
    list_only: Annotated[
        bool,
        typer.Option("--list", help="Print served tool names and exit"),
    ] = False,
    server_group: Annotated[
        ServerGroup | None,
        typer.Option(
            "--server",
            help="Serve only this group; default: all but example",
        ),
    ] = None,
    session: Annotated[
        str | None,
        typer.Option(
            "--session",
            help="Open a session under this name when none is relayed",
        ),
    ] = None,
) -> None:
    """Start SDK tools as an MCP stdio server (the ``notes`` server).

    Launched as a subprocess by the subprocess-served-tool adapters, by the
    ``claude`` runner, and by the generated native harness trees. When
    session-context env vars are present (see
    ``lup.workspace.context.SessionContext``), session-bound
    tools — reflect and
    submit_output — are served alongside the static tools, and tool
    metrics are flushed to the session directory for the parent to read.
    A native runtime relays no such context, so it names a session instead
    and this process opens it.
    """
    serve.serve_tools(list_only, server_group, session)


@app.command("repl")
def repl_cmd(
    model: Annotated[
        str | None,
        typer.Option("--model", "-m", help="Override the model"),
    ] = None,
    no_tools: Annotated[
        bool,
        typer.Option("--no-tools", help="Skip MCP tools"),
    ] = False,
    no_prompt: Annotated[
        bool,
        typer.Option("--no-prompt", help="Skip agent system prompt"),
    ] = False,
    exec_prompt: Annotated[
        str | None,
        typer.Option(
            "--exec",
            help=(
                "Run one prompt non-interactively and exit "
                "(for smoke tests and scripting)"
            ),
        ),
    ] = None,
) -> None:
    """Interactive REPL — continuous session with the agent via the SDK."""
    try:
        if exec_prompt is not None:
            asyncio.run(
                repl.exec_once(
                    exec_prompt, model=model, no_tools=no_tools, no_prompt=no_prompt
                )
            )
        else:
            asyncio.run(repl.repl(model=model, no_tools=no_tools, no_prompt=no_prompt))
    except KeyboardInterrupt:
        pass
