"""Tool collection and the MCP stdio tool server (``serve-tools``)."""

import atexit

import typer

from lup_template.agent.toolsets import (
    EXAMPLE_GROUP,
    NOTES_GROUP,
    ServerGroup,
    build_session_toolset,
)
from lup_template.agent.tools.example import EXAMPLE_TOOLS
from lup.workspace.context import SessionContext
from lup.mcp import LupMcpTool


def collect_tools_by_server(
    context: SessionContext | None = None,
) -> dict[ServerGroup, list[LupMcpTool]]:
    """Collect the servable LupMcpTool instances, grouped by server name.

    The groups come from the toolsets registry
    (:func:`~lup_template.agent.toolsets.build_session_toolset`) — the
    same builder every backend registers — so the served names cannot
    drift from the session's. Without a session context only the static
    example group is servable; with one (relayed by the
    subprocess-served-tool adapters via env vars) the session-bound
    tools — reflect,
    submit_output, sandbox, relay — are constructed and served too. To
    enumerate the full registry without a session (inspection), use
    :func:`collect_registry_tools`.
    """
    if context is None:
        return {EXAMPLE_GROUP: list(EXAMPLE_TOOLS)}

    from lup.reflect import ReviewGate
    from lup.runtime.factory import SessionFactory
    from lup.subagents import create_run_subagent_tool
    from lup.types import SubagentSpec
    from lup_template.agent.core import build_auxiliary_factory
    from lup_template.agent.config import settings
    from lup_template.agent.subagents import get_subagent_specs

    sandbox = None
    if context.session_id and settings.sandbox_enabled:
        from lup.sandbox.container import Sandbox

        sandbox = Sandbox(
            session_id=context.session_id,
            shared_dir=context.session_dir / "sandbox_shared",
        )
        atexit.register(sandbox.stop)

    def subagent_factory(spec: SubagentSpec) -> SessionFactory:
        return build_auxiliary_factory(
            model=spec.model or settings.model,
            system_prompt=spec.prompt,
            tools=spec.tools,
        )

    toolset = build_session_toolset(
        session_dir=context.session_dir,
        outputs_dir=context.outputs_dir,
        gate=ReviewGate(flag_path=context.gate_flag),
        sandbox=sandbox,
        realtime_dir=context.realtime_dir,
        subagent_tool=create_run_subagent_tool(
            get_subagent_specs(), factory_recipe=subagent_factory
        ),
    )
    return toolset["groups"]


def collect_registry_tools() -> dict[ServerGroup, list[LupMcpTool]]:
    """Enumerate every tool group the registry can build, for inspection.

    Builds the full toolset — session-bound groups included — against a
    throwaway directory, so callers with no live session (``inspect``,
    the ``repl`` welcome panel) can list real tools with real schemas.
    The handlers close over the discarded paths: introspect these tools,
    never serve or call them.
    """
    import tempfile
    from pathlib import Path

    from lup.sandbox.container import Sandbox

    with tempfile.TemporaryDirectory(prefix="lup_toolset_enum_") as tmp:
        base = Path(tmp)
        toolset = build_session_toolset(
            session_dir=base / "session",
            outputs_dir=None,
            sandbox=Sandbox(session_id="toolset-enum", shared_dir=base / "shared"),
            realtime_dir=base / "realtime",
        )
    return toolset["groups"]


def harness_session_context(name: str) -> SessionContext:
    """Open the session a natively launched tool server serves.

    An adapter-launched server is handed a session that already exists; a
    server a native runtime starts is the first thing in that session to run,
    so it opens one under the name it was given. The name is the whole
    identity, and every group of one runtime's session is started with the
    same name, so those processes agree on where session state lives without
    a channel between them.
    """
    from lup.workspace.notes import session_gate_flag, setup_notes

    notes = setup_notes(session_id=name, task_id=name, type="harness")
    return SessionContext(
        session_dir=notes.session,
        outputs_dir=notes.output.parent,
        gate_flag=session_gate_flag(name),
        session_id=name,
        task_id=name,
    )


def serve_tools(
    list_only: bool, server_group: ServerGroup | None, session: str | None = None
) -> None:
    """Serve the collected tools over MCP stdio (see the ``serve-tools`` command)."""
    from lup.workspace.context import read_session_context
    from lup.mcp import create_mcp_server, serve_stdio
    from lup.telemetry.metrics import configure_metrics, metrics_path

    context = read_session_context()
    if context is None and session is not None:
        context = harness_session_context(session)
    if context is not None:
        configure_metrics(metrics_path(context.session_dir))

    by_server = collect_tools_by_server(context)
    if server_group is None:
        # Default tool set excludes the example placeholder, which ships
        # fabricated data and is served to no live agent — matching the
        # Claude path. Serve it explicitly with --server example to test it.
        lup_tools = [
            t for key, tools in by_server.items() if key != EXAMPLE_GROUP for t in tools
        ]
    else:
        lup_tools = list(
            by_server.get(server_group, [])  # lup: ignore[dict-get] — group map
        )

    if list_only:
        for t in lup_tools:
            typer.echo(t.name)
        return

    serve_stdio(create_mcp_server(server_group or NOTES_GROUP, tools=lup_tools))
