"""Application composition roots over Lup's provider-neutral runtime."""

import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import AnyHttpUrl, BaseModel, SecretStr

from lup.adapters.claude.config import (
    ClaudeCompatibilityTransform,
    ClaudeCompatibleEndpoint,
)
from lup.adapters.claude.runtime import (
    SESSION_THINKING_TOKENS,
    ClaudeSandboxConfig,
    ClaudeSessionConfig,
    create_claude_session_factory,
)
from lup.adapters.codex.config import (
    CodexCompatibilityTransform,
    CodexCompatibleEndpoint,
)
from lup.adapters.codex.runtime import (
    CodexMcpServerConfig,
    CodexSessionConfig,
    create_codex_session_factory,
)
from lup.runtime.factory import SessionFactory
from lup.hooks import LupHooksConfig
from lup.runtime.models import (
    SessionHandle,
    SessionId,
    SubmissionGateResolver,
    TurnResult,
    turn_request,
)
from lup.runtime.usage import per_mtok_usage_cost
from lup.runtime.wrappers import (
    BudgetConfig,
    CorrectionConfig,
    DisplayConfig,
    DisplayRecord,
    PersistenceConfig,
    TimeoutConfig,
    TraceRecord,
    TracingConfig,
    decorated_session_factory,
)
from lup.mcp import McpServerEntry
from lup.telemetry.metrics import (
    get_metrics_summary,
    log_metrics_summary,
    reset_metrics,
)
from lup.telemetry.trace import TraceLogger
from lup.types import (
    SubagentSpec,
    Usage,
    UsageCost,
)
from lup.workspace.history import save_session
from lup.workspace.notes import NotesConfig, session_gate_flag, setup_notes
from lup.workspace.paths import agent_version
from forumboard.agent.config import (
    compat_api_key,
    compat_base_url,
    engine_for_settings,
    settings,
)
from forumboard.agent.models import AgentOutput, AgentSessionResult
from forumboard.agent.prompts import get_system_prompt

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from lup.mcp import LupMcpTool


class SessionBuild(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """Configured provider-neutral factory and its application workspace."""

    factory: SessionFactory
    notes: NotesConfig
    trace_logger: TraceLogger


def cleaning_session_factory(
    inner: SessionFactory, cleanup: Callable[[], None]
) -> SessionFactory:
    """Run one application resource cleanup after every opened session."""

    @asynccontextmanager
    async def open_cleaned(
        resume: SessionId | None = None,
    ) -> AsyncGenerator[SessionHandle]:
        try:
            async with inner.open(resume) as handle:
                yield handle
        finally:
            cleanup()

    return SessionFactory(open_cleaned)


def build_usage_cost() -> UsageCost | None:
    """Build configured token pricing without coupling it to an adapter."""
    if engine_for_settings() in ("claude", "claude-compat"):
        return reported_usage_cost
    if (
        settings.codex_usd_per_mtok_input is None
        or settings.codex_usd_per_mtok_output is None
    ):
        return None
    return per_mtok_usage_cost(
        input_usd=settings.codex_usd_per_mtok_input,
        output_usd=settings.codex_usd_per_mtok_output,
        cached_input_usd=settings.codex_usd_per_mtok_cached_input,
    )


def reported_usage_cost(usage: Usage) -> float:
    """Use the complete cost reported by providers that supply one."""
    if usage.cost_usd is None:
        raise ValueError("the provider completed without reporting turn cost")
    return usage.cost_usd


def provider_factory(
    *,
    model: str,
    system_prompt: str,
    cwd: Path,
    tools: list[str] | None = None,
    tool_servers: dict[str, McpServerEntry] | None = None,
    allowed_tools: list[str] | None = None,
    hooks: LupHooksConfig | None = None,
    add_dirs: list[Path] | None = None,
    coding_harness_preset: bool = True,
    session_defaults: bool = True,
    submission_gate: SubmissionGateResolver | None = None,
    codex_mcp_servers: dict[str, CodexMcpServerConfig] | None = None,
    writable_roots: list[Path] | None = None,
    subagents: list[SubagentSpec] | None = None,
    thinking_budget: int | None = None,
    max_turns: int | None = None,
) -> SessionFactory:
    """The one application-owned provider selection boundary.

    Native identifiers are intentionally confined to this concrete composition
    root. Every caller above it receives only a configured ``SessionFactory``.
    """
    engine = engine_for_settings()
    logger.info(
        "Engine %s runs model %s (AGENT_SDK %s)",
        engine,
        model,
        settings.agent_sdk or "unset — routed by model",
    )
    if engine in ("claude", "claude-compat"):
        config = ClaudeSessionConfig(
            model=model,
            system_prompt=system_prompt,
            coding_harness_preset=coding_harness_preset,
            tools=tools,
            allowed_tools=allowed_tools or [],
            tool_servers=tool_servers or {},
            permission_mode=(
                settings.permission_mode
                if settings.permission_mode is not None
                else "bypassPermissions"
                if session_defaults
                else None
            ),
            max_turns=max_turns if max_turns is not None else settings.max_turns,
            max_thinking_tokens=(
                thinking_budget
                if thinking_budget is not None
                else settings.max_thinking_tokens
                if settings.max_thinking_tokens is not None
                else SESSION_THINKING_TOKENS
                if session_defaults
                else None
            ),
            effort=normalize_claude_effort(settings.reasoning_effort),
            cwd=cwd,
            add_dirs=add_dirs or list(settings.extra_dirs),
            environment=(
                {"ENABLE_TOOL_SEARCH": settings.tool_search}
                if settings.tool_search is not None
                else {}
            ),
            sandbox=ClaudeSandboxConfig() if session_defaults else None,
            hooks=hooks,
            submission_gate_resolver=submission_gate,
            subagents=subagents or [],
        )
        endpoint = compat_base_url()
        if endpoint is not None:
            config = ClaudeCompatibilityTransform(
                ClaudeCompatibleEndpoint(
                    base_url=AnyHttpUrl(endpoint),
                    api_key=(
                        SecretStr(key)
                        if (key := compat_api_key()) is not None
                        else None
                    ),
                )
            ).apply(config)
        return create_claude_session_factory(config)

    if engine in ("codex", "openai", "openai-compat"):
        unsupported = [
            name
            for name, value in [
                ("AGENT_PERMISSION_MODE", settings.permission_mode),
                ("AGENT_MAX_TURNS", settings.max_turns),
                ("AGENT_MAX_THINKING_TOKENS", settings.max_thinking_tokens),
            ]
            if value is not None
        ]
        if tools:
            unsupported.append("tools")
        if unsupported:
            raise ValueError(
                "Codex app-server cannot honor configured option(s): "
                + ", ".join(unsupported)
            )
        config = CodexSessionConfig(
            model=model,
            developer_instructions=system_prompt,
            cwd=cwd,
            sandbox=(
                normalize_codex_sandbox(settings.codex_sandbox)
                or ("workspace-write" if session_defaults else None)
            ),
            # Hooks are what the app-server puts its approval requests to, so
            # a session carrying them asks and a session without them cannot.
            approval_policy=(
                normalize_codex_approval(settings.codex_approval_policy)
                or ("on-request" if hooks is not None else "never")
            ),
            hooks=hooks,
            effort=normalize_codex_effort(
                settings.codex_effort or settings.reasoning_effort
            ),
            submission_gate_resolver=submission_gate,
            mcp_servers=codex_mcp_servers or {},
            writable_roots=writable_roots or [],
        )
        endpoint = compat_base_url()
        if engine in ("openai", "openai-compat"):
            if endpoint is None:
                raise ValueError(
                    "OPENAI_BASE_URL is required for an OpenAI-compatible route"
                )
            config = CodexCompatibilityTransform(
                CodexCompatibleEndpoint(
                    identifier=settings.openai_model_provider or "lup_openai_compat",
                    base_url=AnyHttpUrl(endpoint),
                    api_key=(
                        SecretStr(key)
                        if (key := compat_api_key()) is not None
                        else None
                    ),
                )
            ).apply(config)
        return create_codex_session_factory(config)

    raise ValueError(f"unsupported engine {engine!r}")


def normalize_claude_effort(
    value: str | None,
) -> Literal["low", "medium", "high", "xhigh", "max"] | None:
    """Validate the application setting at the Claude adapter boundary."""
    if value in (None, "low", "medium", "high", "xhigh", "max"):
        return value
    raise ValueError(f"unsupported Claude reasoning effort {value!r}")


def normalize_codex_sandbox(
    value: str | None,
) -> Literal["read-only", "workspace-write", "danger-full-access"] | None:
    """Translate the documented environment spelling once at composition."""
    aliases = {
        None: None,
        "read_only": "read-only",
        "workspace_write": "workspace-write",
        "danger_full_access": "danger-full-access",
        "read-only": "read-only",
        "workspace-write": "workspace-write",
        "danger-full-access": "danger-full-access",
        "readOnly": "read-only",
        "workspaceWrite": "workspace-write",
        "dangerFullAccess": "danger-full-access",
    }
    try:
        return aliases[value]
    except KeyError as error:
        raise ValueError(f"unsupported Codex sandbox {value!r}") from error


def normalize_codex_approval(
    value: str | None,
) -> Literal["untrusted", "on-request", "granular", "never"] | None:
    """Validate the Codex approval policy before model construction.

    The asking policies are answerable now that the adapter replies to the
    app-server's approval requests from a session's declared hooks, so they
    are settings rather than refusals. Legacy configuration aliases normalize
    here so the app-server wire receives only its current spellings.
    """
    if value is None:
        return None
    match value:
        case "unlessTrusted":
            return "untrusted"
        case "onRequest":
            return "on-request"
        case "untrusted" | "on-request" | "granular" | "never":
            return value
    raise ValueError(
        f"Codex approval policy {value!r} is not one the app-server accepts; "
        "use 'never', 'on-request', 'untrusted', or 'granular'"
    )


def normalize_codex_effort(
    value: str | None,
) -> Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None:
    """Validate reasoning effort at the Codex adapter boundary."""
    if value in (None, "none", "minimal", "low", "medium", "high", "xhigh"):
        return value
    raise ValueError(f"unsupported Codex reasoning effort {value!r}")


def decorate_factory(
    factory: SessionFactory,
    *,
    notes: NotesConfig | None = None,
    trace_logger: TraceLogger | None = None,
) -> SessionFactory:
    """Apply complete-logical-turn governance in its explicit order."""
    usage_cost = build_usage_cost()
    budget = None
    if settings.max_budget_usd is not None:
        if usage_cost is None:
            raise ValueError(
                "a budget requires CODEX_USD_PER_MTOK_INPUT and "
                "CODEX_USD_PER_MTOK_OUTPUT"
            )
        budget = BudgetConfig(
            maximum_usd=settings.max_budget_usd,
            usage_cost=usage_cost,
        )
    timeout = (
        TimeoutConfig(seconds=settings.turn_timeout_seconds)
        if settings.turn_timeout_seconds is not None
        else None
    )
    persistence = None
    tracing = None
    display = None
    if notes is not None and trace_logger is not None:
        from lup.telemetry.display import ColorAssigner, print_block
        from lup.telemetry.trace import TraceEvent

        colors = ColorAssigner()

        async def display_result(record: DisplayRecord) -> None:
            for block in record.blocks:
                print_block(
                    block.telemetry_block,
                    trace=trace_logger,
                    colors=colors,
                )

        async def trace_result(record: TraceRecord) -> None:
            if not record.succeeded and record.failure is not None:
                for block in record.failure.blocks:
                    trace_logger.log_block(block.telemetry_block)
                trace_logger.log_text(record.failure.message, heading="Turn error")
                trace_logger.emit_event(
                    TraceEvent(
                        kind="error",
                        timestamp=datetime.now().isoformat(),
                        brief=record.failure.message,
                    )
                )
            trace_logger.save()

        persistence = PersistenceConfig(directory=notes.trace_log.parent / "turns")
        tracing = TracingConfig(sink=trace_result)
        display = DisplayConfig(sink=display_result)
    return decorated_session_factory(
        factory,
        timeout=timeout,
        budget=budget,
        correction=CorrectionConfig(cycles=2),
        persistence=persistence,
        tracing=tracing,
        display=display,
    )


def build_session_factory(
    session_id: str,
    task_id: str | None = None,
    *,
    model: str | None = None,
    toolless: bool = False,
    bare_prompt: bool = False,
    worldview_tools: list["LupMcpTool"] | None = None,
) -> SessionBuild:
    """Assemble tools and return a fully configured neutral factory.

    ``worldview_tools`` are built by whoever holds a live Notion workspace, so
    this composition root stays about assembling a session rather than about
    reaching a service.
    """
    from lup.hooks import (
        create_permission_hooks,
        create_tool_allowlist_hook,
        merge_hooks,
    )
    from lup.mcp import create_mcp_server
    from forumboard.agent.tool_policy import ToolPolicy
    from forumboard.agent.subagents import get_subagent_specs
    from forumboard.agent.toolsets import build_session_toolset

    notes = setup_notes(session_id, task_id or "0")
    no_subagents: list[SubagentSpec] = []
    subagents = no_subagents if toolless else get_subagent_specs()
    system_prompt = "" if bare_prompt else get_system_prompt()
    tool_servers: dict[str, McpServerEntry] = {}
    codex_mcp_servers: dict[str, CodexMcpServerConfig] = {}
    writable_roots: list[Path] = []
    allowed_tools: list[str] = []
    hooks = create_permission_hooks(notes.rw, notes.ro)
    tools: list[str] | None = [] if toolless else None  # lup: ignore[empty-collection]
    engine = engine_for_settings()
    if not toolless and engine in ("claude", "claude-compat"):
        policy = ToolPolicy(settings)
        toolset = build_session_toolset(worldview_tools=worldview_tools)
        servers = [
            create_mcp_server(name, tools=policy.filter_tools(group_tools))
            for name, group_tools in toolset["groups"].items()
        ]
        tool_servers = dict(policy.get_mcp_servers(*servers))
        from lup.adapters.claude.runtime import SUBMISSION_TOOL

        allowed_tools = policy.get_allowed_tools(
            tool_servers,
            # lup: ignore[frozenset-shape] — immutable policy input
            builtin_tools=frozenset(
                {"Read", "Glob", "Grep", "WebSearch", "WebFetch", "Bash"}
            ),
        )
        # The turn-bound submission tool is registered by the adapter, not the
        # template toolsets; without this the allowlist hook denies the very
        # tool that finalizes the turn.
        allowed_tools.append(SUBMISSION_TOOL)
        hooks = merge_hooks(hooks, create_tool_allowlist_hook(allowed_tools))
    elif not toolless:
        from lup.workspace.context import SessionContext
        from forumboard.agent.toolsets import tool_group_names

        policy = ToolPolicy(settings)
        context = SessionContext(
            session_dir=notes.session,
            outputs_dir=notes.output.parent,
            gate_flag=session_gate_flag(notes.session.name),
            session_id=notes.session.name,
            task_id=notes.output.parent.name,
        )
        environment = {
            **context.to_env(),
            "AGENT_SDK": engine,
            "AGENT_MODEL": model or settings.model,
            "AGENT_SANDBOX_ENABLED": str(settings.sandbox_enabled).lower(),
        }
        if settings.aux_model is not None:
            environment["AGENT_AUX_MODEL"] = settings.aux_model
        codex_mcp_servers = {
            name: CodexMcpServerConfig(
                command="uv",
                args=[
                    "run",
                    "lup-devtools",
                    "agent",
                    "serve-tools",
                    "--server",
                    name,
                ],
                env=environment,
            )
            for name in policy.filter_group_names(tool_group_names())
        }
        writable_roots = list(notes.rw)

    factory = provider_factory(
        model=model or settings.model,
        system_prompt=system_prompt,
        cwd=Path.cwd(),
        tools=tools,
        tool_servers=tool_servers,
        allowed_tools=allowed_tools,
        hooks=hooks,
        add_dirs=[*notes.all_dirs, *settings.extra_dirs],
        coding_harness_preset=not bare_prompt,
        codex_mcp_servers=codex_mcp_servers,
        writable_roots=writable_roots,
        subagents=subagents,
    )
    trace_logger = TraceLogger(
        trace_path=notes.trace_log,
        title=f"Session {session_id}",
    )
    return SessionBuild(
        factory=decorate_factory(
            factory,
            notes=notes,
            trace_logger=trace_logger,
        ),
        notes=notes,
        trace_logger=trace_logger,
    )


def build_auxiliary_factory(
    *,
    model: str,
    system_prompt: str = "",
    tools: list[str] | None = None,
    thinking_budget: int | None = None,
    max_turns: int | None = None,
) -> SessionFactory:
    """Build a one-shot nested/reviewer factory through the same route.

    A nested agent's bounds are the caller's: it is one query with a job, so
    the turn cap and thinking budget belong to whoever declared that job
    rather than to the session settings a whole run shares.
    """
    return decorate_factory(
        provider_factory(
            model=model,
            system_prompt=system_prompt,
            cwd=Path.cwd(),
            tools=tools,
            allowed_tools=tools,
            coding_harness_preset=False,
            session_defaults=False,
            thinking_budget=thinking_budget,
            max_turns=max_turns,
        )
    )


async def run_structured[T: BaseModel](
    *,
    system_prompt: str,
    task: str,
    output: type[T],
    model: str | None = None,
    max_turns: int | None = None,
) -> T:
    """One tool-less query that has to answer in ``output``'s shape.

    This is what the reviewer and the editor are: a job, some text, and a
    required shape. They get no tools deliberately — everything they need is
    in the prompt, and a tool would only give them somewhere else to reach.
    """
    factory = build_auxiliary_factory(
        model=model or settings.model,
        system_prompt=system_prompt,
        tools=list[str](),
        max_turns=max_turns,
    )
    async with factory.open() as handle:
        turn = await handle.session.start(turn_request(task, output))
        result = await turn.turn.result()
    if result.output is None:
        raise ValueError(
            f"the model finished without producing a {output.__name__}; "
            "the turn may have been cut short by a budget or turn cap"
        )
    return result.output


def resolve_resume_token(reference: str) -> SessionId:
    """Resolve a saved run name or accept an opaque provider session id."""
    from lup.workspace.history import latest_session_record

    record = latest_session_record(reference)
    if record is None:
        return SessionId(value=reference)
    if not record.sdk_session_id:
        raise ValueError(f"session {reference!r} has no provider resume identity")
    return SessionId(value=record.sdk_session_id)


def result_text[T: BaseModel | None](result: TurnResult[T]) -> str:
    """Concatenate completed portable text blocks."""
    return "\n\n".join(
        text for block in result.blocks if (text := block.text_payload) is not None
    )


def result_sources[T: BaseModel | None](result: TurnResult[T]) -> list[str]:
    """Extract source URLs and search queries from semantic tool calls."""
    sources: list[str] = []  # lup: ignore[empty-collection]
    for block in result.blocks:
        arguments = block.tool_arguments
        if arguments is None:
            continue
        if block.tool_call_name in ("FetchUrl", "WebFetch"):
            value = arguments.get("url")  # lup: ignore[dict-get]
        elif block.tool_call_name in ("SearchWeb", "WebSearch"):
            value = arguments.get("query")  # lup: ignore[dict-get]
        else:
            continue
        if isinstance(value, str) and value:
            sources.append(value)
    return sources


def application_result(
    result: TurnResult[AgentOutput],
    *,
    session_id: str,
    task_id: str | None,
) -> AgentSessionResult:
    """Project a strict typed turn result into the domain history model."""
    usage_cost = build_usage_cost()
    return AgentSessionResult(
        session_id=session_id,
        task_id=task_id,
        agent_version=agent_version(),
        agent_sdk=engine_for_settings(),
        sdk_session_id=result.identifiers.session.value,
        timestamp=datetime.now().isoformat(),
        output=result.output,
        reasoning=result_text(result),
        sources_consulted=result_sources(result),
        duration_seconds=result.duration.total_seconds(),
        cost_usd=usage_cost(result.usage) if usage_cost is not None else None,
        token_usage=result.usage,
        tool_metrics=get_metrics_summary(),
    )


async def run_agent(
    task: str,
    *,
    session_id: str | None = None,
    task_id: str | None = None,
    resume: SessionId | None = None,
) -> AgentSessionResult:
    """Run one strict typed turn and persist its application projection."""
    identifier = session_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    reset_metrics()
    build = build_session_factory(identifier, task_id)
    async with build.factory.open(resume) as handle:
        turn = await handle.session.start(turn_request(task, AgentOutput))
        result = await turn.turn.result()
    log_metrics_summary()
    projected = application_result(
        result,
        session_id=identifier,
        task_id=task_id,
    )
    save_session(projected, session_id=identifier)
    return projected
