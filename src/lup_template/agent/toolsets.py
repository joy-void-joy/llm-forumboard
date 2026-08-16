"""Single source of truth for the agent's MCP tool groups.

A *toolset* is the session's tools sorted into named groups, not one flat
list. The grouping is what MCP needs: each group becomes one MCP server, the
group name becomes the server name, and a tool ``foo`` in group ``notes`` is
addressed as ``mcp__notes__foo`` on every backend. Grouping also lets the
policy enable or withhold a whole capability (a server) at once.

Both backend paths consume this module — the Claude path registers the groups
in-process, the Codex/OpenAI path serves them over stdio (``lup-devtools agent
serve-tools``) and selects them by name; both assemblies live in
``core.build_session_options`` — so adding a group or tool here reaches every
backend, and there is deliberately nowhere else to add one.

Add each domain group in :func:`build_session_toolset`, and its name in
:func:`tool_group_names` when it should be served to subprocess backends.
"""

# lup: template: register tool groups in build_session_toolset + tool_group_names.

from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    from lup.mcp import LupMcpTool
    from lup.reflect import ReviewGate
    from lup.sandbox.container import Sandbox

ServerGroup = Literal["notes", "sandbox", "codeintel", "session", "example"]
"""A tool-group name this registry can build — the group vocabulary every
consumer shares: server registration (``core.build_session_options``),
subprocess serving and CLI selection (``lup-devtools agent serve-tools
--server``). Extend it together with :func:`build_session_toolset` when
adding a group. (A plain alias, not a ``type`` statement, so typer can
read the choices off the annotation.)"""

# One name per member of the closed ``ServerGroup`` alias above, which the CLI
# spells back and a served group is registered under.
NOTES_GROUP: ServerGroup = "notes"
# lup: ignore[constant-declaration] — group identity
SANDBOX_GROUP: ServerGroup = "sandbox"
# lup: ignore[constant-declaration] — group identity
CODEINTEL_GROUP: ServerGroup = "codeintel"
# lup: ignore[constant-declaration] — group identity
SESSION_GROUP: ServerGroup = "session"
# lup: ignore[constant-declaration] — group identity
EXAMPLE_GROUP: ServerGroup = "example"
"""Placeholder tools with fabricated data — never served to a live agent
by default; select explicitly (``serve-tools --server example``) to test."""


class SessionToolset(TypedDict):
    """Return type of :func:`build_session_toolset`."""

    groups: dict[ServerGroup, list["LupMcpTool"]]
    gate: "ReviewGate"


def tool_group_names(*, realtime: bool) -> list[ServerGroup]:
    """Group names served to subprocess backends (Codex/OpenAI).

    Excludes :data:`EXAMPLE_GROUP`. Uses the same name constants as
    :func:`build_session_toolset`; ``test_toolsets`` asserts the two
    stay aligned.
    """
    if realtime:
        return [NOTES_GROUP, SANDBOX_GROUP, CODEINTEL_GROUP, SESSION_GROUP]
    return [NOTES_GROUP, SANDBOX_GROUP, CODEINTEL_GROUP]


def build_session_toolset(
    *,
    session_dir: Path,
    outputs_dir: Path | None,
    gate: "ReviewGate | None" = None,
    sandbox: "Sandbox | None" = None,
    realtime_dir: Path | None = None,
    subagent_tool: "LupMcpTool | None" = None,
) -> SessionToolset:
    """Build every MCP tool group for one session.

    Args:
        session_dir: Session directory (``output.json``, review artifacts).
        outputs_dir: Past outputs for reviewer calibration.
        gate: Shared review gate. None creates an in-memory gate (the
            Claude in-process path); subprocess paths pass a file-backed
            gate so the parent and the tool subprocess agree.
        sandbox: Session sandbox whose tools form the ``sandbox`` group.
        realtime_dir: Relay mailbox directory; presence adds the
            ``session`` group (persistent-mode tools), wired with a
            file-backed reflection gate so this domain keeps its
            meta-before-sleep requirement — reflection is opt-in and the
            library imposes none by default.

    Returns:
        The groups plus the shared reflection gate.
    """
    from lup.codeintel.tools import create_codeintel_tools
    from lup.workspace.paths import project_root
    from lup_template.agent.config import aux_model
    from lup_template.agent.tools.example import EXAMPLE_TOOLS
    from lup_template.agent.tools.reflect import create_reflect_tools
    from lup.devtools.dev.pyright_oracle import langserver_path

    reflect_kit = create_reflect_tools(
        session_dir=session_dir,
        outputs_dir=outputs_dir,
        gate=gate,
        reviewer_model=aux_model(),
    )
    notes_tools = list(reflect_kit["tools"])
    if subagent_tool is not None:
        notes_tools.append(subagent_tool)

    groups: dict[ServerGroup, list[LupMcpTool]] = {NOTES_GROUP: notes_tools}

    if sandbox is not None:
        groups[SANDBOX_GROUP] = sandbox.create_tools()

    server = langserver_path()
    if server is not None:
        groups[CODEINTEL_GROUP] = create_codeintel_tools(server, project_root())

    if realtime_dir is not None:
        from lup.realtime.relay import RealtimeMailbox, create_realtime_relay_tools
        from lup.reflect import ReflectionGate

        meta_flag = RealtimeMailbox(realtime_dir).meta_flag_path
        groups[SESSION_GROUP] = create_realtime_relay_tools(
            realtime_dir, gate=ReflectionGate(flag_path=meta_flag)
        )

    groups[EXAMPLE_GROUP] = list(EXAMPLE_TOOLS)

    return SessionToolset(
        groups=groups,
        gate=reflect_kit["gate"],
    )
