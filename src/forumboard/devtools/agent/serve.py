"""Tool collection and the MCP stdio tool server (``serve-tools``)."""

import asyncio

import typer

from lup.mcp import LupMcpTool
from lup.workspace.context import SessionContext

from forumboard.agent.toolsets import (
    WORLDVIEW_GROUP,
    ServerGroup,
    build_session_toolset,
)


def worldview_tools() -> list[LupMcpTool]:
    """The Notion-backed tools, or none where Notion is not configured.

    A served group that is empty is the honest answer to an unconfigured
    workspace: the tool policy withholds the group anyway, and building tools
    that would fail on their first call helps nobody.
    """
    from forumboard.agent.tools.worldview import create_worldview_tools
    from forumboard.context import NotConfigured, open_context

    async def build() -> list[LupMcpTool]:
        try:
            context = await open_context()
        except NotConfigured as error:
            typer.echo(f"Notion is not configured: {error}", err=True)
            return list[LupMcpTool]()
        return create_worldview_tools(
            context.discussions, context.worldview, context.people
        )

    return asyncio.run(build())


def collect_tools_by_server(
    context: SessionContext | None = None,
) -> dict[ServerGroup, list[LupMcpTool]]:
    """Collect the servable tools, grouped by server name.

    The groups come from the toolsets registry — the same builder every backend
    registers — so the served names cannot drift from the session's.
    """
    tools = worldview_tools()
    if context is None:
        return build_session_toolset(worldview_tools=tools)["groups"]

    from lup.runtime.factory import SessionFactory
    from lup.subagents import create_run_subagent_tool
    from lup.types import SubagentSpec

    from forumboard.agent.config import settings
    from forumboard.agent.core import build_auxiliary_factory
    from forumboard.agent.subagents import get_subagent_specs

    def subagent_factory(spec: SubagentSpec) -> SessionFactory:
        return build_auxiliary_factory(
            model=spec.model or settings.model,
            system_prompt=spec.prompt,
            tools=spec.tools,
        )

    return build_session_toolset(
        worldview_tools=tools,
        subagent_tool=create_run_subagent_tool(
            get_subagent_specs(), factory_recipe=subagent_factory
        ),
    )["groups"]


def collect_registry_tools() -> dict[ServerGroup, list[LupMcpTool]]:
    """Enumerate every tool group the registry can build, for inspection.

    Introspection only: the tools are built against whatever workspace the
    environment names, so read their schemas rather than calling them.
    """
    return collect_tools_by_server()


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
    from lup.mcp import create_mcp_server, serve_stdio
    from lup.telemetry.metrics import configure_metrics, metrics_path
    from lup.workspace.context import read_session_context

    context = read_session_context()
    if context is None and session is not None:
        context = harness_session_context(session)
    if context is not None:
        configure_metrics(metrics_path(context.session_dir))

    by_server = collect_tools_by_server(context)
    served = [
        tool
        for group, tools in by_server.items()
        if server_group is None or group == server_group
        for tool in tools
    ]

    if list_only:
        for tool in served:
            typer.echo(tool.name)
        return

    serve_stdio(create_mcp_server(server_group or WORLDVIEW_GROUP, tools=served))
