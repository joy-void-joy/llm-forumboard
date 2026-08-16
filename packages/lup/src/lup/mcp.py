"""MCP server factory with proper is_error propagation.

Creates in-process MCP servers from ``LupMcpTool`` definitions. The
``create_mcp_server`` function returns an ``LupMcpServerConfig`` that
each SDK adapter converts to its native server configuration.

Tool naming convention:
    After registration, tools are named: mcp__{server_name}__{tool_name}
    Example: mcp__my-server__my_tool

Examples:
    Define a tool with typed input/output using the ``lup_tool`` decorator::

        >>> from pydantic import BaseModel, Field
        >>> class SearchInput(BaseModel):
        ...     query: str = Field(description="Search query")
        >>> class SearchOutput(BaseModel):
        ...     results: list[str]
        >>> @lup_tool("Search the knowledge base.", tags=["search"])
        ... async def search(params: SearchInput) -> SearchOutput:
        ...     return SearchOutput(results=["result1", "result2"])

    Create an MCP server from tools::

        >>> tools = [search, another_tool]
        >>> server = create_mcp_server("my-server", tools=tools)
"""

import asyncio
import inspect
import json
import logging
import signal
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Literal, NotRequired, TypedDict, cast, get_type_hints

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ContentBlock, ImageContent, TextContent, Tool
from pydantic import BaseModel, ValidationError

from lup.types import Decorator, EnvVars, JsonObject

logger = logging.getLogger(__name__)


class TextWire(TypedDict):
    """A text content block as it crosses the MCP tool-result wire."""

    type: Literal["text"]
    text: str


class ImageWire(TypedDict):
    """A base64 image content block as it crosses the MCP tool-result wire."""

    type: Literal["image"]
    data: str
    mimeType: str


class ToolResponse(TypedDict, total=False):
    """The MCP tool-result protocol shape every tool handler returns.

    Cross-backend, not Claude-specific: Codex and OpenAI serve the same
    ``mcp__server__tool`` protocol, so this dict is the one result shape a
    handler produces on every engine."""

    content: list[TextWire | ImageWire]
    is_error: bool


def response_text(response: ToolResponse) -> str:
    """Every text block a tool result carries, joined in reading order.

    The content key is optional and its blocks are a union, so reading a
    result's text means two narrowings no caller should repeat — and one that
    skips them reads an image block's absent ``text``.
    """
    content = response.get("content", [])  # lup: ignore[dict-get] — optional key
    return "\n".join(block["text"] for block in content if block["type"] == "text")


def mcp_response(text: str, *, is_error: bool = False) -> ToolResponse:
    """Create an MCP response with text content."""
    response = ToolResponse(content=[{"type": "text", "text": text}])
    if is_error:
        response["is_error"] = True
    return response


class CallToolResultWithAlias(CallToolResult):
    """CallToolResult with snake_case alias for SDK compatibility.

    Some SDK query runners check ``is_error`` (snake_case) but MCP's
    CallToolResult uses ``isError`` (camelCase). This subclass adds a
    property alias so both work.
    """

    @property
    def is_error(self) -> bool:
        """Snake_case alias for isError."""
        return self.isError


# MCP hands an arbitrary JSON args object validated by the per-tool BaseModel.
type LupToolHandler = Callable[[JsonObject], Awaitable[ToolResponse]]

# A tool's typed implementation: a validated input model in, an output model out.
type ToolHandler[I: BaseModel, O: BaseModel] = Callable[[I], Awaitable[O]]


class LupMcpServerConfig(BaseModel, arbitrary_types_allowed=True):
    """SDK-agnostic MCP server configuration.

    Wraps an ``mcp.server.Server`` instance. Each adapter converts
    this to its native server config at build time.
    """

    name: str
    server: Server
    tool_names: list[str] = []


class RawStdioServerConfig(TypedDict):
    """An external MCP server launched as a stdio subprocess."""

    type: NotRequired[Literal["stdio"]]
    command: str
    args: NotRequired[list[str]]
    env: NotRequired[EnvVars]


class RawSseServerConfig(TypedDict):
    """An external MCP server reached over Server-Sent Events."""

    type: Literal["sse"]
    url: str
    headers: NotRequired[dict[str, str]]  # lup: ignore[dict-str-payload] — wire


class RawHttpServerConfig(TypedDict):
    """An external MCP server reached over streamable HTTP."""

    type: Literal["http"]
    url: str
    headers: NotRequired[dict[str, str]]  # lup: ignore[dict-str-payload] — wire


type RawMcpServerConfig = (
    RawStdioServerConfig | RawSseServerConfig | RawHttpServerConfig
)
"""An MCP server the framework does not host: a transport config, no instance."""

type McpServerEntry = LupMcpServerConfig | RawMcpServerConfig
"""One MCP server in a session: an in-process ``LupMcpServerConfig`` carrying a
live ``Server`` instance, or a transport config for an external one. An adapter
narrows by ``isinstance(entry, LupMcpServerConfig)`` — the in-process case has a
``.server`` to register, the external case is passed to the SDK as-is.

This is a seam, not a union we declare variants of: the external arm is a
``TypedDict`` mirroring a vendor's wire shape, which has no base and can carry
no method to answer with. Narrowing it separates something we host from a
foreign payload, and each adapter projects the hosted case into its own native
config — a conversion that would drag that vendor's spelling back across the
boundary if the neutral model answered it."""


def create_mcp_server(
    name: str,
    version: str = "1.0.0",
    tools: Sequence["LupMcpTool"] | None = None,
) -> LupMcpServerConfig:
    """Create an in-process MCP server with proper is_error handling.

    Args:
        name: Unique identifier for the server.
        version: Server version string.
        tools: List of LupMcpTool instances created with the @lup_tool decorator.

    Returns:
        LupMcpServerConfig for adapter conversion.

    A server built without tools still registers its handlers and
    advertises an empty tool list — selecting an unpopulated group is a
    valid (if useless) session, not a protocol error.
    """
    server = Server(name, version=version)
    registered = list(tools or [])
    tool_map = {tool_def.name: tool_def for tool_def in registered}

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """Return the list of available tools."""
        return [
            Tool(
                name=tool_def.name,
                description=tool_def.description,
                inputSchema=tool_def.input_schema,
            )
            for tool_def in registered
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: JsonObject) -> CallToolResult:
        """Execute a tool by name with given arguments."""
        if name not in tool_map:
            raise ValueError(f"Tool '{name}' not found")

        tool_def = tool_map[name]
        result = await tool_def.handler(arguments)

        is_error = "is_error" in result and bool(result["is_error"])

        content: list[
            ContentBlock
        ] = []  # lup: ignore[empty-collection] — match-arm fold
        if "content" in result:
            for item in result["content"]:
                match item:
                    case {"type": "text", "text": str(text)}:
                        content.append(TextContent(type="text", text=text))
                    case {"type": "image", "data": str(data), "mimeType": str(mime)}:
                        content.append(
                            ImageContent(type="image", data=data, mimeType=mime)
                        )

        return CallToolResultWithAlias(content=content, isError=is_error)

    return LupMcpServerConfig(
        name=name,
        server=server,
        tool_names=[t.name for t in registered],
    )


def server_tool_names(server: McpServerEntry) -> list[str]:
    """List the tool names registered on an in-process MCP server.

    Servers built with :func:`create_mcp_server` carry their tool list on
    the config. Use this to compute the full ``mcp__{server}__{tool}``
    names the agent will see — e.g. when building a tool allowlist or an
    inspection display — without maintaining a second tool list that can
    drift. External server configs (stdio, SSE, HTTP) cannot be
    introspected without connecting, so they yield an empty list.
    """
    match server:
        case LupMcpServerConfig():  # lup: ignore[own-model-dispatch] — seam arm
            return list(server.tool_names)
        case _:
            return []


# lup: ignore[model-free-function] — a process entrypoint owning signals and transport
def serve_stdio(config: LupMcpServerConfig) -> None:
    """Serve an in-process MCP server over stdio (blocking).

    The subprocess half of tool serving: backends that cannot host
    in-process servers launch a tool-server subprocess (``lup-devtools
    agent serve-tools``), which builds the same
    :func:`create_mcp_server` config the in-process path registers and
    exposes it here over a stdio transport — one server construction for
    every backend. SIGTERM raises ``SystemExit`` so ``atexit`` cleanup
    (sandbox teardown, metrics flush) runs when the parent stops the
    subprocess.
    """

    def terminate(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)

    async def run() -> None:
        init_options = config.server.create_initialization_options()
        async with stdio_server() as (read_stream, write_stream):
            await config.server.run(read_stream, write_stream, init_options)

    asyncio.run(run())


class ToolError(Exception):
    """Raise in a tool handler to return an MCP error response."""


class LupMcpTool[I: BaseModel, O: BaseModel]:
    """MCP tool with typed input/output models for introspection.

    Stores the tool definition (name, description, schema, handler) directly.
    Devtools can inspect ``input_model`` / ``output_model`` for full JSON Schemas.
    Also callable directly with a typed model instance, bypassing MCP
    serialization.

    Deliberately a plain generic class, not a ``BaseModel``: as a generic
    pydantic model this type defeats ``@lup_tool``'s return-type inference —
    pyright degrades every decorated tool to its raw function type, so
    ``list[LupMcpTool]`` collection sites (toolsets, subagents, reflect) stop
    type-checking. Its fields are a validated handler callable and a
    ``type[I]`` model, none of which need pydantic validation, so BaseModel
    buys nothing here and costs the decorator inference.
    """

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: JsonObject,
        handler: LupToolHandler,
        call_handler: ToolHandler[I, O],
        input_model: type[I],
        output_model: type[O] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler
        self.call_handler = call_handler
        self.input_model = input_model
        self.output_model = output_model
        self.tags = tags or []

    async def __call__(self, params: I) -> O:
        return await self.call_handler(params)


def lup_tool[I: BaseModel, O: BaseModel](
    description: str,
    input_model: type[BaseModel] | None = None,
    output_model: type[BaseModel] | None = None,
    *,
    name: str | None = None,
    tags: list[str] | None = None,
) -> Decorator[ToolHandler[I, O], LupMcpTool[I, O]]:
    """Decorator for defining MCP tools with typed input/output models.

    Infers input/output schemas from type annotations, auto-validates input,
    auto-serializes BaseModel output, and tracks call metrics.

    The handler receives a validated model instance, not a raw dict.
    The handler must return a BaseModel, which is auto-serialized via
    ``mcp_response(json.dumps(result.model_dump(mode="json"), default=str))``.

    Raise ``ToolError`` in the handler to return an MCP error response.

    Args:
        description: What/when/why — the agent's only documentation for this tool.
        input_model: Pydantic BaseModel for the tool's input.
              Inferred from the handler's first parameter type if omitted.
        output_model: Pydantic BaseModel for the tool's output.
              Inferred from the handler's return type if omitted.
        name: Unique tool identifier (becomes ``mcp__{server}__{name}``).
              Defaults to the handler's function name.
        tags: Optional classification tags for tool policy filtering.

    Returns:
        A decorator that wraps the async handler into a ``LupMcpTool``.
    """
    from lup.telemetry.metrics import collector
    from lup.workspace.content_safety import guard_result

    def decorator(
        handler: ToolHandler[I, O],
    ) -> LupMcpTool[I, O]:
        tool_name = name or handler.__name__

        resolved_input = input_model
        resolved_output = output_model

        if resolved_input is None or resolved_output is None:
            hints = get_type_hints(handler)
            if resolved_input is None:
                params = list(inspect.signature(handler).parameters.values())
                if not params:
                    msg = f"lup_tool '{tool_name}': handler has no parameters to infer input_model from"
                    raise TypeError(msg)
                param_type = hints.get(  # lup: ignore[dict-get] — reflection hints
                    params[0].name
                )
                if isinstance(param_type, type) and issubclass(param_type, BaseModel):
                    resolved_input = param_type
            if resolved_output is None:
                return_type = hints.get(  # lup: ignore[dict-get] — reflection hints
                    "return"
                )
                if isinstance(return_type, type) and issubclass(return_type, BaseModel):
                    resolved_output = return_type

        if resolved_input is None:
            msg = f"lup_tool '{tool_name}': cannot infer input_model from annotations"
            raise TypeError(msg)

        final_input = cast(type[I], resolved_input)  # lup: ignore[cast] — hint infer

        async def wrapper(args: JsonObject) -> ToolResponse:
            start = time.perf_counter()
            is_error = False
            try:
                try:
                    params = final_input.model_validate(args)
                except ValidationError as e:
                    is_error = True
                    return mcp_response(f"Invalid input: {e}", is_error=True)
                try:
                    result = await handler(params)
                except ToolError as e:
                    is_error = True
                    return mcp_response(str(e), is_error=True)
                if not isinstance(result, BaseModel):
                    raise TypeError(
                        f"lup_tool '{tool_name}': handler must return a BaseModel, "
                        f"got {type(result).__name__}"
                    )
                if resolved_output is not None and not isinstance(
                    result, resolved_output
                ):
                    raise TypeError(
                        f"lup_tool '{tool_name}': expected {resolved_output.__name__}, "
                        f"got {type(result).__name__}"
                    )
                guarded = guard_result(tool_name, params, result)
                # JSON mode, because JSON is what this crosses: python mode
                # hands back live objects and leaves `default=str` to render
                # them with `str()`, which spells a datetime with a space
                # where ISO 8601 wants a T. `default=str` stays as the
                # fallback for what pydantic's JSON mode still cannot render.
                return mcp_response(
                    json.dumps(guarded.model_dump(mode="json"), default=str)
                )
            except Exception:
                is_error = True
                raise
            finally:
                duration_ms = (time.perf_counter() - start) * 1000
                collector.record(tool_name, duration_ms, is_error)

        return LupMcpTool(
            name=tool_name,
            description=description,
            input_schema=final_input.model_json_schema(),
            handler=wrapper,
            call_handler=handler,
            input_model=final_input,
            output_model=cast(  # lup: ignore[cast] — hint infer
                type[O] | None, resolved_output
            ),
            tags=tags or [],
        )

    return decorator
