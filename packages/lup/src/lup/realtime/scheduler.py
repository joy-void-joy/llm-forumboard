# lup: ignore[native-spelling]
# The retained scheduler callback surface still accepts legacy hook events.
"""In-process scheduler core for persistent agents.

Owns all async timing state: sleep/wake, debounce windows, scheduled
actions, reminders, and delayed actions. The environment layer (Discord
bot, game server, CLI) wires callbacks; the agent interacts through
MCP tools that delegate to this scheduler.

This is the core the whole ``lup.realtime`` package is built on: it runs
entirely in-process and imports neither the shared tool models nor the
subprocess relay. On backends whose tools run in a separate process,
:mod:`lup.realtime.relay` drives an instance of this ``Scheduler`` from the
parent side — the relay depends on the core, never the reverse.

See also: ``src/lup_template/agent/tools/realtime.py`` for the MCP tool
implementations that wrap this scheduler.

Examples:
    Create a scheduler and wire it to an environment callback::

        >>> async def send_message(content: str) -> None:
        ...     await channel.send(content)
        >>> scheduler = Scheduler(on_action=send_message)

    Agent sleeps until a wake event or timeout::

        >>> result = await scheduler.sleep(300)
        >>> result["reason"]
        'user_message'

    Environment wakes the agent on external event::

        >>> scheduler.wake("user_message")

    Use debounce to batch rapid events::

        >>> scheduler.start_debounce(initial_seconds=30, quiet_seconds=5)

    Create a Stop hook to keep the agent in a persistent loop::

        >>> from lup.realtime.scheduler import create_stop_guard
        >>> from lup.hooks import merge_hooks
        >>> hooks = merge_hooks(permission_hooks, create_stop_guard())
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TypedDict

from pydantic import BaseModel, Field

from lup.hooks import LupHookInput, LupHooksConfig, create_tool_gate
from lup.reflect import ReflectionGate

logger = logging.getLogger(__name__)

ActionCallback = Callable[[str], Awaitable[None]]
"""Async callback for delivering actions (messages, commands, etc.)."""


class DelayedAction(BaseModel, arbitrary_types_allowed=True):
    """A pending delayed-action task with its content, so cancels can save ideas."""

    task: asyncio.Task[None]
    content: str


# =====================================================================
# Result types
# =====================================================================


class SleepResult(BaseModel):
    """Result returned by Scheduler.sleep()."""

    reason: str = "timer"
    fired_reminders: list[str] = []
    time: str = ""


class ScheduledActionState(TypedDict):
    """State for a pending scheduled action."""

    content: str | None
    remaining_seconds: int


class ReminderState(TypedDict):
    """State for a pending reminder."""

    label: str
    remaining_seconds: int


class SchedulerState(TypedDict, total=False):
    """Full scheduling state returned by get_state()."""

    scheduled_action: ScheduledActionState
    pending_reminders: list[ReminderState]
    debounce_active: bool


# =====================================================================
# Scheduler
# =====================================================================


class PendingReminder(BaseModel, arbitrary_types_allowed=True):
    """A scheduled self-prompt."""

    task: asyncio.Task[None]
    label: str
    fire_at: float = Field(description="loop.time() when reminder fires")


class Scheduler:
    """Manages all timed actions for a persistent agent session.

    All scheduling methods are non-blocking and return immediately.
    Only ``sleep()`` blocks — it waits for a wake event or timeout.

    The environment layer calls ``wake()`` and ``extend_debounce()``
    when external events arrive. The agent calls ``sleep()`` to yield
    and the various scheduling methods to plan future actions.
    """

    def __init__(
        self,
        *,
        on_action: ActionCallback,
        on_sleep: Callable[[], None] | None = None,
        ideas: list[str] | None = None,
        meta_gate: ReflectionGate | None = None,
    ) -> None:
        self.on_action = on_action
        self.on_sleep = on_sleep
        self.ideas: list[str] = ideas if ideas is not None else []

        # Wake mechanism
        self.wake_event: asyncio.Event = asyncio.Event()
        self.wake_reason: str | None = None

        # Debounce
        self.debounce_task: asyncio.Task[None] | None = None
        self.debounce_event: asyncio.Event = asyncio.Event()

        # Scheduled action
        self.scheduled_action_task: asyncio.Task[None] | None = None
        self.scheduled_action_content: str | None = None
        self.scheduled_action_fire_at: float | None = None

        # Reminders — live mutable state, the scheduler's whole job.
        self.reminders: list[PendingReminder] = []
        self.fired_reminder_labels: list[str] = []

        # Delayed actions
        self.pending_actions: list[DelayedAction] = []

        # Meta-before-sleep gate. Pass a file-backed gate when the meta
        # tool runs in a subprocess (see lup.realtime.relay).
        self.meta_gate = meta_gate if meta_gate is not None else ReflectionGate()

    async def send_action(self, content: str) -> None:
        """Deliver an action to the environment via the registered callback."""
        await self.on_action(content)

    # ------------------------------------------------------------------
    # Wake / Sleep
    # ------------------------------------------------------------------

    def wake(self, reason: str) -> None:
        """Wake the sleeping agent with a reason."""
        self.wake_reason = reason
        self.wake_event.set()

    def consume_wake(self) -> None:
        """Clear a pending wake event after the agent has read its cause.

        Call this after the agent processes the messages that triggered
        a wake, to prevent ``sleep()`` from returning immediately on
        the stale event.
        """
        self.wake_event.clear()
        self.wake_reason = None

    async def sleep(self, seconds: int) -> SleepResult:
        """Block until timer expires or a wake event fires.

        Debounce persists across sleep cycles — if active, events
        extend the quiet window and the next sleep waits for it.

        Returns a context dict with ``reason`` and scheduling state.
        """
        if self.on_sleep:
            self.on_sleep()

        if self.wake_event.is_set():
            pass  # Already have a pending wake — return immediately
        else:
            self.wake_reason = None
            try:
                await asyncio.wait_for(self.wake_event.wait(), timeout=seconds)
            except asyncio.TimeoutError:
                self.wake_reason = "timer"

        self.wake_event.clear()

        result = SleepResult(
            reason=self.wake_reason or "timer",
            time=datetime.now().strftime("%H:%M:%S"),
        )
        if self.fired_reminder_labels:
            result.fired_reminders = list(self.fired_reminder_labels)
            self.fired_reminder_labels.clear()
        return result

    # ------------------------------------------------------------------
    # Debounce
    # ------------------------------------------------------------------

    @property
    def debounce_active(self) -> bool:
        """Whether a debounce window is currently open."""
        return self.debounce_task is not None and not self.debounce_task.done()

    @property
    def wake_pending(self) -> bool:
        """Whether a wake event is already queued."""
        return self.wake_event.is_set()

    def start_debounce(
        self,
        initial_seconds: int,
        quiet_seconds: int,
        *,
        wake_on_empty: bool = True,
    ) -> None:
        """Start a debounce window. Replaces any existing window.

        Phase 1 (initial): Wait up to ``initial_seconds`` for the first
        event. If nothing arrives and ``wake_on_empty`` is True, wake
        immediately; otherwise silently deactivate.
        Phase 2 (quiet): Once activity is detected, wait ``quiet_seconds``
        after each event. Wake when the quiet period elapses.
        """
        if self.debounce_task and not self.debounce_task.done():
            self.debounce_task.cancel()
        self.debounce_event.clear()

        # Absorb pending wake so sleep doesn't return immediately
        if self.wake_event.is_set():
            self.wake_event.clear()
            self.wake_reason = None
            self.debounce_event.set()

        self.debounce_task = asyncio.create_task(
            self.run_debounce(initial_seconds, quiet_seconds, wake_on_empty)
        )

    def extend_debounce(self) -> None:
        """Signal activity to reset the quiet timer."""
        if self.debounce_active:
            self.debounce_event.set()

    async def run_debounce(
        self,
        initial_seconds: int,
        quiet_seconds: int,
        wake_on_empty: bool = True,
    ) -> None:
        """Debounce timer: initial wait for activity, then quiet-period loop."""
        try:
            # Phase 1: wait for first activity
            try:
                await asyncio.wait_for(
                    self.debounce_event.wait(), timeout=initial_seconds
                )
            except asyncio.TimeoutError:
                if wake_on_empty:
                    self.wake("timer")
                return

            # Phase 2: quiet-period loop
            while True:
                self.debounce_event.clear()
                try:
                    await asyncio.wait_for(
                        self.debounce_event.wait(), timeout=quiet_seconds
                    )
                except asyncio.TimeoutError:
                    break
            self.wake("event")
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Scheduled action
    # ------------------------------------------------------------------

    def start_scheduled_action(self, content: str, delay: int) -> None:
        """Schedule an action that fires after delay if no event arrives.

        Only one at a time — calling again replaces the previous one.
        Cancelled actions are saved as ideas.
        """
        self.cancel_scheduled_action()
        self.scheduled_action_content = content
        self.scheduled_action_fire_at = asyncio.get_running_loop().time() + delay
        self.scheduled_action_task = asyncio.create_task(
            self.run_scheduled_action(content, delay)
        )

    def cancel_scheduled_action(self) -> None:
        """Cancel the pending scheduled action, saving it as an idea."""
        if self.scheduled_action_task and not self.scheduled_action_task.done():
            self.scheduled_action_task.cancel()
            if self.scheduled_action_content:
                self.ideas.append(self.scheduled_action_content)
        self.scheduled_action_content = None
        self.scheduled_action_fire_at = None
        self.scheduled_action_task = None

    async def run_scheduled_action(self, content: str, delay: int) -> None:
        """Scheduled action coroutine. Fires but does NOT wake the agent."""
        try:
            await asyncio.sleep(delay)
            if self.on_sleep:
                self.on_sleep()
            await self.on_action(content)
            self.scheduled_action_content = None
            self.scheduled_action_fire_at = None
            self.scheduled_action_task = None
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Reminders
    # ------------------------------------------------------------------

    def add_reminder(self, label: str, delay: int) -> None:
        """Schedule a self-prompt reminder that wakes the agent."""
        fire_at = asyncio.get_running_loop().time() + delay
        task = asyncio.create_task(self.run_reminder(label, delay))
        self.reminders.append(PendingReminder(task=task, label=label, fire_at=fire_at))

    async def run_reminder(self, label: str, delay: int) -> None:
        """Reminder coroutine. Records label and wakes agent."""
        try:
            await asyncio.sleep(delay)
            self.fired_reminder_labels.append(label)
            self.wake("reminder")
            self.reminders = [r for r in self.reminders if not r.task.done()]
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Delayed actions
    # ------------------------------------------------------------------

    def add_delayed_action(self, content: str, delay: int) -> None:
        """Schedule an action with a delay. Cancelled if an event arrives."""
        task = asyncio.create_task(self.run_delayed_action(content, delay))
        self.pending_actions.append(DelayedAction(task=task, content=content))

    def cancel_delayed_actions(self) -> None:
        """Cancel all pending delayed actions, saving them as ideas."""
        for action in self.pending_actions:
            if not action.task.done():
                action.task.cancel()
                self.ideas.append(action.content)
        self.pending_actions.clear()

    async def run_delayed_action(self, content: str, delay: int) -> None:
        """Delayed action coroutine."""
        try:
            await asyncio.sleep(delay)
            if self.on_sleep:
                self.on_sleep()
            await self.on_action(content)
            self.pending_actions = [
                action for action in self.pending_actions if not action.task.done()
            ]
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Event handlers (called by environment layer)
    # ------------------------------------------------------------------

    def on_agent_action(self) -> None:
        """Cancel scheduled action on agent's own action and require new meta."""
        self.cancel_scheduled_action()
        self.meta_gate.reset()

    # ------------------------------------------------------------------
    # State for context tool
    # ------------------------------------------------------------------

    def get_state(self) -> SchedulerState:
        """Return scheduling state for the context tool."""
        loop = asyncio.get_running_loop()
        now = loop.time()

        state = SchedulerState(debounce_active=self.debounce_active)

        if self.scheduled_action_task and not self.scheduled_action_task.done():
            remaining = max(0, int((self.scheduled_action_fire_at or now) - now))
            state["scheduled_action"] = ScheduledActionState(
                content=self.scheduled_action_content,
                remaining_seconds=remaining,
            )

        active_reminders = [r for r in self.reminders if not r.task.done()]
        self.reminders = active_reminders
        if active_reminders:
            state["pending_reminders"] = [
                ReminderState(
                    label=r.label,
                    remaining_seconds=max(0, int(r.fire_at - now)),
                )
                for r in active_reminders
            ]

        return state


# =====================================================================
# Hook factories
# =====================================================================


def create_stop_guard() -> LupHooksConfig:
    """Create a Stop hook that prevents the agent from ending its turn.

    **What:** Preset over :func:`lup.hooks.create_tool_gate` — blocks the
    Stop event with a redirect to the ``sleep`` tool, unless the SDK
    reports ``stop_hook_active`` (the guard already fired during this
    stop sequence; passing through avoids an infinite block loop).

    **When:** Wire into every persistent agent session. This keeps the
    agent in the wake -> act -> sleep -> wake loop: the only way to
    yield control is the (blocking) ``sleep`` tool.

    **Why:** Without it, the agent ends its turn after responding and
    the persistent session dies.

    Returns:
        LupHooksConfig with a Stop hook.

    Usage:
        from lup.realtime.scheduler import create_stop_guard
        from lup.hooks import merge_hooks

        hooks = merge_hooks(permission_hooks, create_stop_guard())
    """

    def stop_already_handled(input_data: LupHookInput) -> bool:
        return input_data.event == "Stop" and input_data.stop_hook_active

    return create_tool_gate(
        event="Stop",
        style="block",
        message="You cannot end your turn. Use sleep to pause between turns.",
        unlocked=stop_already_handled,
    )


def create_pending_event_guard(
    *,
    check_unread: Callable[[], int],
    scheduler: Scheduler,
    guarded_tools: list[str],
) -> LupHooksConfig:
    """Create a PreToolUse hook that blocks timing tools when unread events exist.

    **What:** Preset over :func:`lup.hooks.create_tool_gate` — blocks the
    guarded timing tools while unread events exist, with the live count
    in the denial message. Unlocked when the call forces through
    (``force=true``), starts its own debounce window, a debounce window
    is already open, a wake is pending, or there is nothing unread.

    **When:** Guard ``sleep`` and scheduling tools in persistent agents
    so the agent reads pending events (via ``context``) before yielding.

    **Why:** Sleeping past unread events means reacting a full cycle
    late; the denial redirects the agent to ``context`` first.

    Args:
        check_unread: Callable returning the count of unread events.
        scheduler: The Scheduler instance (checked for debounce/wake state).
        guarded_tools: MCP tool names to guard (e.g., ``["mcp__session__sleep"]``).

    Returns:
        LupHooksConfig with PreToolUse hooks.
    """

    def no_unread_events(input_data: LupHookInput) -> bool:
        if input_data.event != "PreToolUse":
            return True
        match input_data.tool_input:
            case {"force": True} | {"debounce_initial": int() | float()}:
                return True
        if scheduler.debounce_active:
            return True
        if scheduler.wake_pending:
            return True
        return not check_unread()

    return create_tool_gate(
        gated_tool=guarded_tools,
        style="block",
        message=lambda: (
            f"Blocked — {check_unread()} unread event(s). Call context first."
        ),
        unlocked=no_unread_events,
    )


def create_meta_before_sleep_guard(
    *,
    scheduler: Scheduler,
    sleep_tool_name: str,
) -> LupHooksConfig:
    """Create a PreToolUse hook that requires meta before sleep.

    Preset over :func:`lup.hooks.create_tool_gate`, via
    :func:`~lup.reflect.create_reflection_gate`, for the persistent
    agent pattern. Forces the agent to call the ``meta`` tool (process
    self-assessment) before every sleep. The gate resets automatically
    via ``scheduler.on_agent_action()``.

    Args:
        scheduler: The Scheduler instance (uses ``scheduler.meta_gate``).
        sleep_tool_name: MCP tool name for sleep (e.g., ``"mcp__session__sleep"``).

    Returns:
        LupHooksConfig with PreToolUse hooks.
    """
    from lup.reflect import create_reflection_gate

    return create_reflection_gate(
        gate=scheduler.meta_gate,
        gated_tool=sleep_tool_name,
        reflection_tool_name="meta",
        denial_message="You must call meta before sleeping. Assess your process this turn.",
    )
