"""Single source of truth for the agent's MCP tool groups.

A *toolset* is the session's tools sorted into named groups, not one flat list.
The grouping is what MCP needs: each group becomes one MCP server, the group
name becomes the server name, and a tool ``foo`` in group ``worldview`` is
addressed as ``mcp__worldview__foo`` on every backend. Grouping is also what
lets the policy withhold a whole capability at once — with no Notion token
configured, the worldview group is simply not served.

This project has one agent whose tools are *this project's*, the worldview
builder, so there is one group. The reviewer and the editor hold tools too,
but not from here: theirs are the runtime's own file tools, and what bounds
them is not a registry but a directory. Each is opened with one conversation's
folder as its working directory, so the whole of what it can reach is the
conversation it was asked about — which is what makes reading a file safe
where reaching a service would not be, and why those tools are granted at the
session rather than served here.

They need them because a conversation is not only prose. Somebody attached a
document, and a document is a file; no prompt can carry one, and a pass that
cannot open it is deciding what may be published about material it has not
seen.
"""

from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    from lup.mcp import LupMcpTool

ServerGroup = Literal["worldview"]
"""A tool-group name this registry can build — the vocabulary every consumer
shares: server registration, subprocess serving, and CLI selection
(``lup-devtools agent serve-tools --server``). A plain alias, not a ``type``
statement, so typer can read the choices off the annotation."""

WORLDVIEW_GROUP: ServerGroup = "worldview"


class SessionToolset(TypedDict):
    """Return type of :func:`build_session_toolset`."""

    groups: dict[ServerGroup, list["LupMcpTool"]]


def tool_group_names() -> list[ServerGroup]:
    """Group names served to subprocess backends (Codex/OpenAI)."""
    return [WORLDVIEW_GROUP]


def build_session_toolset(
    *,
    worldview_tools: list["LupMcpTool"] | None = None,
    subagent_tool: "LupMcpTool | None" = None,
) -> SessionToolset:
    """Build every MCP tool group for one session.

    ``worldview_tools`` are built against a live Notion workspace by the
    pipeline that owns one, and passed in rather than constructed here: this
    module knows the shape of a session's tools, not how to reach a workspace.
    A session given none — an inspection, or a run with no token configured —
    gets an empty group rather than a broken one.

    Args:
        worldview_tools: The Notion-backed tools, from
            :func:`forumboard.agent.tools.worldview.create_worldview_tools`.
        subagent_tool: The delegation tool, on backends without native
            subagents.

    Returns:
        The groups, keyed by server name.
    """
    tools = list(worldview_tools or [])
    if subagent_tool is not None:
        tools.append(subagent_tool)
    groups: dict[ServerGroup, list[LupMcpTool]] = {WORLDVIEW_GROUP: tools}
    return SessionToolset(groups=groups)
