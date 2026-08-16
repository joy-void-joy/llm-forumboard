"""Real-time MCP tools for persistent agents.

This is a TEMPLATE. These tools show how to wire the Scheduler from
``lup.realtime.scheduler`` into an agent session. Customize for your domain.

The pattern:
1. Create a Scheduler with your environment's action callback
2. Define tools as closures bound to session state
3. Wire hooks (stop guard, pending event guard, meta guard)
4. Use streaming input mode with a single session-start turn

Core tools:
- ``sleep`` is the ONLY blocking tool — all others return immediately
- ``context`` reads state and advances the event pointer
- ``reply`` delivers actions to the environment
- Timing tools (debounce, remind, schedule) are non-blocking

Background agents (:class:`lup.runtime.background.BackgroundAgent`):
- Run companion agents alongside the main session
- Observer example at the bottom shows conversation summarization
- Any use case: research, execution, monitoring — not just observation

Tool descriptions are comprehensive because they're the agent's only
documentation. See Tool Design Philosophy in CLAUDE.md.
"""

import logging
from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel, Field

from lup.mcp import LupMcpTool, ToolError, lup_tool
from lup.realtime.models import (
    ContextInput,
    ContextOutput,
    DebounceInput,
    DebounceOutput,
    MetaInput,
    MetaOutput,
    RemindInput,
    RemindOutput,
    ReplyInput,
    ReplyOutput,
    ScheduleActionInput,
    ScheduleActionOutput,
    SleepInput,
    SleepOutput,
)
from lup.realtime.scheduler import Scheduler
from lup.runtime.background import BackgroundAgent
from lup.runtime.errors import TurnError
from lup.runtime.models import TurnResult, turn_request
from lup.telemetry.trace import TraceLogger

logger = logging.getLogger(__name__)


# lup: template: this domain expects a meta assessment before sleep, so the nudge
# names that step. Pass it to ``run_relay_session(missing_sleep_message=...)``;
# drop the meta wording for domains that don't gate sleep on reflection.
MISSING_SLEEP_MESSAGE = (
    "Your turn ended without calling sleep. This is a persistent session: "
    "you yield control with the sleep tool, and the environment wakes you "
    "when something happens. Finish any pending replies, record a meta "
    "assessment, then call sleep."
)
"""Corrective nudge for a turn that ended without sleeping."""


# =====================================================================
# Additional input models (domain-specific, customize these)
# =====================================================================


class NotesInput(BaseModel):
    """Input for the notes tool."""

    action: str = Field(description="One of: write, read, list, delete.")
    key: str = Field(default="", description="Note key.")
    content: str = Field(default="", description="Note content (for write).")


class IdeasInput(BaseModel):
    """Input for the ideas tool."""

    action: str = Field(description="One of: add, list, remove, set.")
    content: str = Field(default="", description="Idea text (for add).")
    index: int = Field(default=0, description="Index to remove (for remove).")
    ideas: list[str] = Field(default=[], description="Full replacement list (for set).")


# =====================================================================
# Output models
# =====================================================================


class NotesOutput(BaseModel):
    """Output for the notes tool."""

    message: str


class IdeasOutput(BaseModel):
    """Output for the ideas tool."""

    message: str
    ideas: list[str] = []


class ObserverNotesOutput(BaseModel):
    """Output for the observer notes tool."""

    count: int


class ObserverState(BaseModel):
    """Immutable transcript delta supplied by the environment on wake."""

    messages: list[str]


class ObserverSummary(BaseModel):
    """Typed replacement summary produced by the background turn."""

    content: str


# =====================================================================
# Tool factory
# =====================================================================


# lup: template: customize these tools' wording and context for your domain.
def create_realtime_tools(
    *,
    scheduler: Scheduler,
    build_context: Callable[[int], ContextOutput],
    trace_logger: TraceLogger | None = None,
) -> list[LupMcpTool]:
    """Create the standard set of real-time MCP tools.

    The tools are closures bound to the session state via the scheduler
    and build_context callback.

    Args:
        scheduler: The Scheduler instance for this session.
        build_context: Callable(last_events: int) -> ContextOutput that
            builds the context state and advances the read pointer.
            Use ``ContextOutput(**your_dict)`` — extra fields are accepted.
        trace_logger: Optional TraceLogger for meta assessments.

    Returns:
        List of LupMcpTool instances.
    """

    # -- Communication tools -------------------------------------------

    @lup_tool(
        "Deliver a message to the environment. Text output and thinking "
        "don't reach the user — this is the only way to communicate. "
        "One message is the default. For a sequence of short reactions "
        "you can batch with staggered delay_seconds. Delayed messages "
        "cancel if an external event arrives (cancelled text becomes an "
        "idea). Sending also cancels any pending scheduled_action.",
        name="reply",
    )
    async def reply_tool(inp: ReplyInput) -> ReplyOutput:
        sent = 0
        scheduled = 0
        cumulative_delay = 0
        for item in inp.messages:
            cumulative_delay += item.delay_seconds
            if cumulative_delay == 0:
                await scheduler.send_action(item.message)
                sent += 1
            else:
                scheduler.add_delayed_action(item.message, cumulative_delay)
                scheduled += 1

        if sent or scheduled:
            scheduler.on_agent_action()

        return ReplyOutput(sent=sent, scheduled=scheduled)

    @lup_tool(
        "Schedule an action that fires if the environment stays quiet for "
        "delay_seconds. Cancels (saved as idea) if an event arrives or "
        "you send a reply. Only one at a time — calling again replaces "
        "the previous one. Does not generate a new turn when it fires.",
        name="schedule_action",
    )
    async def schedule_action_tool(inp: ScheduleActionInput) -> ScheduleActionOutput:
        scheduler.start_scheduled_action(inp.content, inp.delay_seconds)
        return ScheduleActionOutput(delay_seconds=inp.delay_seconds)

    # -- Timing tools --------------------------------------------------

    @lup_tool(
        "Suppress wake events until activity stops. Two phases: waits "
        "up to initial_seconds for the first event; once activity starts, "
        "holds wake until quiet_seconds elapse with no new activity. "
        "Events still go to state — context works during debounce. "
        "Returns immediately. Only one debounce at a time — calling "
        "again replaces the previous one.",
        name="debounce",
    )
    async def debounce_tool(inp: DebounceInput) -> DebounceOutput:
        scheduler.start_debounce(inp.initial_seconds, inp.quiet_seconds)
        return DebounceOutput(
            initial_seconds=inp.initial_seconds,
            quiet_seconds=inp.quiet_seconds,
        )

    @lup_tool(
        "Pause until timer expires or an event occurs (external event, "
        "reminder). This is the ONLY blocking tool — all others return "
        "immediately. You MUST call meta before sleeping. Returns wake "
        "reason only — call context after waking to read state. "
        "No debounce by default — wakes immediately on events. "
        "Set debounce_initial/debounce_quiet to batch event bursts. "
        "Blocked when unread events exist — use force=true to bypass.",
        name="sleep",
    )
    async def sleep_tool(inp: SleepInput) -> SleepOutput:
        if inp.debounce_initial is not None:
            scheduler.start_debounce(
                inp.debounce_initial,
                inp.debounce_quiet or inp.debounce_initial,
            )

        result = await scheduler.sleep(inp.seconds)
        return SleepOutput(
            reason=result.reason,
            time=datetime.now().strftime("%H:%M:%S"),
            fired_reminders=result.fired_reminders,
        )

    @lup_tool(
        "Schedule a self-prompt that fires after delay_seconds. "
        "When it fires, it wakes you from sleep with fired_reminders "
        "in the result. Multiple reminders can be active simultaneously. "
        "Use for things you want to come back to — checking in, "
        "revisiting a topic, following up on something.",
        name="remind",
    )
    async def remind_tool(inp: RemindInput) -> RemindOutput:
        scheduler.add_reminder(inp.label, inp.delay_seconds)
        return RemindOutput(label=inp.label, delay_seconds=inp.delay_seconds)

    # -- State tools ---------------------------------------------------

    @lup_tool(
        "Get current state and read new events. Returns timing info, "
        "environment state, and two separate lists: recent history and "
        "unread events (no overlap). Also returns scheduler state "
        "(pending actions, reminders, debounce). last_events controls "
        "history depth (default 5, 0 = unread only).",
        name="context",
    )
    async def context_tool(inp: ContextInput) -> ContextOutput:
        return build_context(inp.last_events)

    @lup_tool(
        "Read or write private session notes. Not visible to the user.",
        name="notes",
    )
    async def notes_tool(_inp: NotesInput) -> NotesOutput:
        # NOTE: This is a template. Wire to your session's notes dict.
        return NotesOutput(message="Notes tool not wired. Customize in your session.")

    @lup_tool(
        "Capture threads worth exploring later. Cancelled actions "
        "automatically become ideas too. Use 'set' to replace the "
        "entire list after reorganizing.",
        name="ideas",
    )
    async def ideas_tool(inp: IdeasInput) -> IdeasOutput:
        match inp.action:
            case "add":
                scheduler.ideas.append(inp.content)
                return IdeasOutput(
                    message=f"Idea added ({len(scheduler.ideas)} total).",
                )
            case "list":
                return IdeasOutput(
                    message=f"{len(scheduler.ideas)} ideas."
                    if scheduler.ideas
                    else "No ideas.",
                    ideas=list(scheduler.ideas),
                )
            case "remove":
                if inp.index < 0 or inp.index >= len(scheduler.ideas):
                    raise ToolError(f"Invalid index {inp.index}. Use 'list' first.")
                removed = scheduler.ideas.pop(inp.index)
                return IdeasOutput(message=f"Removed: {removed}")
            case "set":
                scheduler.ideas.clear()
                scheduler.ideas.extend(inp.ideas)
                return IdeasOutput(
                    message=f"Ideas replaced ({len(scheduler.ideas)} total).",
                )
        raise ToolError(f"Unknown action: {inp.action}")

    @lup_tool(
        "Record a process assessment for the improvement loop. "
        "What worked this turn? What was friction? Are you missing "
        "tools or information? Rate pacing, timing, quality. "
        "Be specific. Required before sleep.",
        name="meta",
    )
    async def meta_tool(inp: MetaInput) -> MetaOutput:
        if trace_logger:
            trace_logger.log_text(inp.thought, heading="Meta")
        scheduler.meta_gate.mark_reflected()
        return MetaOutput()

    return [
        reply_tool,
        schedule_action_tool,
        debounce_tool,
        sleep_tool,
        remind_tool,
        context_tool,
        notes_tool,
        ideas_tool,
        meta_tool,
    ]


# =====================================================================
# Background agent example: Observer
# =====================================================================
#
# An observer is one use of background agents — it maintains running
# summaries of the conversation. Other uses: research agents that
# fetch data, executor agents that run long tasks, etc.
#
# Integration:
# 1. Create shared state (e.g. notes list)
# 2. Create observer with create_observer()
# 3. Call observer.start() in session setup
# 4. Call observer.wake() when transcript changes
# 5. Include notes[-1] in the main agent's context tool
# 6. Call observer.stop() on session teardown

# lup: ignore[constant-declaration] — the observer's own standing prose, which
# is what this scaffold exists to state
OBSERVER_SYSTEM_PROMPT = """\
You are a background observer for a conversation agent. Each turn you \
receive new transcript messages and write a note the agent reads for \
context.

Write a concise summary — what the agent needs to understand the \
conversation if earlier messages scrolled out of context. Include \
specifics: names, stories, claims, examples, the user's mood and \
what they care about right now. Keep it short — a few sentences to \
a short paragraph. No headers, no bullet lists, no numbered items.

Describe context only — never prescribe behavior. No "What Agent \
Should Do" sections, no action items, no directives. The agent \
decides how to act; you provide the picture it acts on.

If the new messages are trivial fragments, skip the update entirely — \
only write when the picture meaningfully changed.\
"""


class ObserverNotesInput(BaseModel):
    """Input for the observer notes tool."""

    content: str = Field(
        description=(
            "Complete conversation summary replacing the previous note. "
            "Include everything the agent needs for context."
        )
    )


def create_observer_tools(
    *,
    notes: list[str],
) -> list[LupMcpTool]:
    """Create tools for the observer background agent.

    Args:
        notes: Shared list where observer appends summaries.
            The main agent reads from this via its context tool.
    """

    @lup_tool(
        "Write a complete summary of the conversation so far. "
        "This replaces your previous note — include everything "
        "the agent needs. The agent sees your latest note for "
        "temporal context.",
        name="notes",
    )
    async def observer_notes_tool(inp: ObserverNotesInput) -> ObserverNotesOutput:
        notes.append(inp.content)
        logger.info("Observer note: %s", inp.content)
        return ObserverNotesOutput(count=len(notes))

    return [observer_notes_tool]


# lup: template: customize the observer's prompt, model, and build_message.
def create_observer(
    *,
    notes: list[str],
    model: str | None = None,
) -> BackgroundAgent[ObserverState, ObserverSummary]:
    """Create an observer background agent.

    The environment supplies an :class:`ObserverState` on each wake. The
    background turn returns a typed replacement summary; no provider-specific
    client or tool driver is reconstructed here.

    Args:
        notes: Shared list for observer summaries.
        model: Model for the observer; defaults to the backend-coherent
            auxiliary model.

    Returns:
        A configured background agent (call ``.start()`` to begin).
    """
    from lup_template.agent.config import aux_model
    from lup_template.agent.core import build_auxiliary_factory

    def state_to_request(state: ObserverState):
        msgs_text = "\n".join(state.messages)
        last_note = notes[-1] if notes else "(none yet)"
        return turn_request(
            f"New messages:\n{msgs_text}\n\nYour last note:\n{last_note}",
            ObserverSummary,
        )

    async def record(result: TurnResult[ObserverSummary]) -> None:
        notes.append(result.output.content)

    async def report(error: TurnError) -> None:
        logger.error("Observer turn failed: %s", error.failure.message)

    return BackgroundAgent(
        build_auxiliary_factory(
            model=model or aux_model(), system_prompt=OBSERVER_SYSTEM_PROMPT
        ),
        state_to_request,
        record,
        report,
    )
