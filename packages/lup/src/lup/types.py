"""Internal content block, message, and response types.

These types are the shared vocabulary for all consumer code — the
application's orchestration, the trace logger, the tools. SDK-specific
adapters convert to/from these types at the boundary — consumer code
never imports from SDK packages directly. The hook vocabulary lives in
:mod:`lup.hooks`, not here.
"""

import json
from collections.abc import Callable, Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, Discriminator, Field, StringConstraints


# ---------------------------------------------------------------------------
# JSON vocabulary
# ---------------------------------------------------------------------------

type JsonValue = (
    str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
)
"""One JSON-decodable value — the shape ``json.loads`` yields.

The shared vocabulary for data whose schema is defined elsewhere (tool
arguments, JSON Schemas, vendor payloads): unlike ``object`` it stays
introspectable, and unlike ``Any`` it keeps the type checker honest.
"""

type JsonObject = dict[str, JsonValue]
"""A JSON object: tool inputs, JSON Schemas, structured outputs, session data."""

type EnvVars = dict[str, str]  # lup: ignore[dict-str-payload] — open env-var map
"""An environment-variable map — process env, dotenv values, MCP server env.

The keys are open and data-driven by nature (whatever variables exist), which
is exactly the shape the dict-str-payload rule otherwise flags: annotate env
maps with this alias instead of respelling ``dict[str, str]`` per site."""

type StringMap = dict[str, str]  # lup: ignore[dict-str-payload] — open string map
"""Any other open map of strings to strings — a reason table, response headers.

The same shape as ``EnvVars`` and deliberately a separate name: what makes
these keys open differs, and a header map annotated as environment variables
reads as a mistake even where the checker cannot see one. Reach for it when
the keys are data rather than a schema; a fixed set of fields is a
``TypedDict`` or a model, not this."""

type Namespace = dict[str, object]  # lup: ignore[dict-str-object] — live objects
"""A live Python namespace: names bound to whatever objects they name.

The one shape ``object`` is honest for, because the values genuinely are
arbitrary objects rather than data with a schema somewhere — a module, a
builtin, a class. Reach for ``JsonObject`` for anything that will be
serialized; this is for what an interpreter holds."""

type Decorator[T, R] = Callable[[T], R]
"""A decorator: applied with ``@`` to a `T`, yields an `R`. Names the intent
at a signature (``-> Decorator[Handler, Tool]``) where a bare ``Callable`` of
a callable reads as noise. ``R`` need not be ``T`` — a decorator may return a
different type than it wraps (a builder, a registration object)."""


# ---------------------------------------------------------------------------
# Tool vocabulary
# ---------------------------------------------------------------------------

type KnownToolName = Literal[
    "Agent",
    "AskUserQuestion",
    "Bash",
    "BashOutput",
    "Edit",
    "EnterWorktree",
    "ExitPlanMode",
    "ExitWorktree",
    "Glob",
    "Grep",
    "KillShell",
    "ListMcpResources",
    "MultiEdit",
    "NotebookEdit",
    "Read",
    "ReadMcpResource",
    "Skill",
    "SlashCommand",
    "StructuredOutput",
    "Task",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
    "Workflow",
    "Write",
]
"""The well-known built-in tool names — the framework's lingua franca.

Claude Code's tool vocabulary is adopted as the neutral spelling: adapters
translate their backend's native tool identities onto these names, so hooks,
policies, and harness declarations all read one vocabulary."""

type McpToolName = Annotated[
    str, StringConstraints(pattern=r"^mcp__[A-Za-z0-9_-]+(?:__[A-Za-z0-9_-]+)*$")
]
"""A dynamically registered MCP tool: ``mcp__<server>`` or ``mcp__<server>__<tool>``."""

type ToolName = KnownToolName | McpToolName
"""One tool identity: a well-known built-in or a registered ``mcp__*`` tool."""

type ScopedToolGrant = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]*\([^()]+\)$")
]
"""A tool grant narrowed by a parenthesized specifier, e.g. ``Bash(git:*)``."""

type ToolGrant = ToolName | ScopedToolGrant
"""One entry of a declared tool grant list: a whole tool or a scoped rule."""


# ---------------------------------------------------------------------------
# Content blocks
# ---------------------------------------------------------------------------


def normalize_content(content: str | Sequence[object] | None) -> str:
    """Flatten tool-result content — MCP block list or scalar — to a string."""
    if content is None:
        return "(empty)"
    if isinstance(content, list):
        texts: list[str] = []  # lup: ignore[empty-collection] — block fold
        for item in content:
            match item:
                case {"type": "text", "text": text}:
                    texts.append(str(text))
        return "\n".join(texts)
    return str(content)


class LupContentBlock(BaseModel):
    """One block of a message, answering every question about itself.

    Whatever a transcript, a console, or a trace needs to know about a block is
    declared here and answered — or declined — by the block, so a new kind of
    block is one class rather than an edit to every walk that would have to
    notice it. The declining answers are what make omission safe: a caller
    asking ``spoken_text`` reaches every kind that voices prose, including
    kinds written long after the caller was.
    """

    @property
    def display_emoji(self) -> str:
        """The glyph a console leads this block with."""
        return "❓"

    @property
    def display_label(self) -> str:
        """The short name a console and a trace heading give this block."""
        return "Unknown"

    @property
    def display_body(self) -> str:
        """This block's content as one readable string."""
        return str(self)

    @property
    def markdown_fence(self) -> str | None:
        """The code-fence language a markdown trace wraps the body in, if any."""
        return None

    @property
    def spoken_text(self) -> str | None:
        """Prose the assistant voiced, if this block voices any."""
        return None

    @property
    def opens_pairing(self) -> str | None:
        """The id this block opens, which a later block closes."""
        return None

    @property
    def closes_pairing(self) -> str | None:
        """The id this block closes, opened by an earlier block."""
        return None

    @property
    def tool_call_name(self) -> str | None:
        """The tool this block invokes, if it invokes one."""
        return None

    @property
    def result_payload(self) -> str | Sequence[object] | None:
        """Raw tool output this block carries, for a caller that reformats it."""
        return None

    def log_summary(self, body: str) -> str:
        """One stream-log line for this block, given its rendered `body`."""
        return f"{self.display_label.upper()}: {body}"


class LupTextBlock(LupContentBlock):
    """Text content from the assistant."""

    type: Literal["text"] = "text"
    text: str

    @property
    def display_emoji(self) -> str:
        return "💬"

    @property
    def display_label(self) -> str:
        return "Response"

    @property
    def display_body(self) -> str:
        return self.text

    @property
    def spoken_text(self) -> str | None:
        return self.text


class LupThinkingBlock(LupContentBlock):
    """Extended thinking / reasoning content."""

    type: Literal["thinking"] = "thinking"
    thinking: str = ""
    redacted: bool = False

    @property
    def display_emoji(self) -> str:
        return "💭"

    @property
    def display_label(self) -> str:
        return "Thinking"

    @property
    def display_body(self) -> str:
        return "[redacted]" if self.redacted else self.thinking


class LupToolUseBlock(LupContentBlock):
    """A tool invocation by the agent."""

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: JsonObject | None = None

    @property
    def display_emoji(self) -> str:
        return "🔧"

    @property
    def display_label(self) -> str:
        return f"Tool: {self.name}"

    @property
    def display_body(self) -> str:
        return json.dumps(self.input, indent=2) if self.input else ""

    @property
    def markdown_fence(self) -> str | None:
        return "json"

    @property
    def opens_pairing(self) -> str | None:
        return self.id

    @property
    def tool_call_name(self) -> str | None:
        return self.name

    def log_summary(self, body: str) -> str:
        arguments = json.dumps(self.input) if self.input else ""
        return f"TOOL_USE [{self.id}] {self.name}: {arguments}"


class LupToolResultBlock(LupContentBlock):
    """Result returned from a tool invocation."""

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str | Sequence[object] | None = None

    @property
    def display_emoji(self) -> str:
        return "📋"

    @property
    def display_label(self) -> str:
        return "Result"

    @property
    def display_body(self) -> str:
        return normalize_content(self.content)

    @property
    def markdown_fence(self) -> str | None:
        return ""

    @property
    def closes_pairing(self) -> str | None:
        return self.tool_use_id

    @property
    def result_payload(self) -> str | Sequence[object] | None:
        return self.content

    def log_summary(self, body: str) -> str:
        return f"TOOL_RESULT [{self.tool_use_id}]: {body}"


type MessageContentBlock = Annotated[
    LupTextBlock | LupThinkingBlock | LupToolUseBlock | LupToolResultBlock,
    Discriminator("type"),
]
"""One block as a message *field* validates it: the closed set, discriminated.

Annotations that only read a block name :class:`LupContentBlock`, the base. A
pydantic field must name this alias instead — validating against the base
alone would rebuild every block as a base instance and drop its payload.
"""


# ---------------------------------------------------------------------------
# Subagent specification
# ---------------------------------------------------------------------------


class SubagentSpec(BaseModel):
    """Provider-neutral subagent definition used by injected factory recipes."""

    name: str
    description: str
    prompt: str
    tools: list[str] = []
    model: str | None = Field(
        default=None,
        description="Model for this subagent; None inherits the session's "
        "main model on every backend",
    )
    max_turns: int | None = Field(
        default=None,
        description="Turn cap for delegated one-shot runs (None = backend default)",
    )


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class LupMessage(BaseModel):
    """One transcript message, answering every question about itself.

    A walk that wants the blocks of a message asks :attr:`content_blocks`
    rather than naming the kinds of message that carry them, so a kind added
    later is reached by every existing walk and a kind that carries none — a
    status line — declines once, here.
    """

    @property
    def content_blocks(self) -> list[LupContentBlock]:
        """The blocks this message carries, empty when it carries none."""
        return []


class LupAssistantMessage(LupMessage):
    """Message from the assistant containing content blocks."""

    role: Literal["assistant"] = "assistant"
    content: list[MessageContentBlock]

    @property
    def content_blocks(self) -> list[LupContentBlock]:
        return list(self.content)


class LupUserMessage(LupMessage):
    """Message from the user or tool results."""

    role: Literal["user"] = "user"
    content: list[MessageContentBlock] | str

    @property
    def content_blocks(self) -> list[LupContentBlock]:
        return [] if isinstance(self.content, str) else list(self.content)


class LupSystemMessage(LupMessage):
    """System-level message (status updates, etc.)."""

    role: Literal["system"] = "system"
    subtype: str
    data: str


class Usage(BaseModel):
    """Portable token usage — the counts every backend can provide.

    Adapters produce this through a ``usage_normalizer`` callback supplied
    at construction, defaulting to each adapter's standard converter. A
    custom normalizer may return a *subclass* carrying vendor-specific
    fields (service tier, per-iteration costs, …); fields holding it are
    declared ``SerializeAsAny[Usage]`` so subclass data survives into
    session JSON.
    """

    input_tokens: int = Field(
        default=0,
        description=(
            "Complete input token count, cached reads and cache creation"
            " included; adapters normalize cache-exclusive native counts"
        ),
    )
    cost_usd: float | None = Field(
        default=None,
        description="Optional provider-reported complete cost for this usage span",
    )
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


type UsageCost = Callable[[Usage], float]
"""Estimates the USD cost of accumulated token usage.

Adapters that report token counts but no cost take one of these to enforce a
budget; build it with :func:`lup.runtime.usage.per_mtok_usage_cost`.
"""
