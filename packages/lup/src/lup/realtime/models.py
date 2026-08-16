"""Tool input/output models for the realtime tools.

The vocabulary the agent-facing realtime tools (sleep, reply, debounce,
remind, schedule_action, context, meta) speak — one place both wirings share.
The in-process template tools and the subprocess :mod:`lup.realtime.relay`
both validate against these models, so a schema change reaches every backend
at once. This module depends on neither the scheduler core nor the relay.
"""

from pydantic import BaseModel, Field

# =====================================================================
# Tool input models
# =====================================================================


class SleepFollowUp(BaseModel):
    """A message to send at a scheduled interval during sleep.

    Follow-ups fill silence: thread-pulls, different angles, check-ins.
    Cancelled (saved as ideas) if the user speaks before they fire.
    """

    message: str = Field(description="Message to send during sleep")
    delay_seconds: int = Field(
        ge=1,
        description="Seconds from sleep start to send this message",
    )


class SleepInput(BaseModel):
    """Input for the sleep tool."""

    seconds: int = Field(description="How long to sleep (max). Wakes early on events.")
    debounce_initial: int | None = Field(
        default=None,
        description=(
            "If set, start a debounce window: wait up to this many seconds "
            "for the first event before waking."
        ),
    )
    debounce_quiet: int | None = Field(
        default=None,
        description=(
            "Quiet period for debounce. Once activity starts, wait this long "
            "after each event before waking. Defaults to debounce_initial."
        ),
    )
    wake_on_empty: bool = Field(
        default=True,
        description=(
            "Wake immediately if debounce_initial expires with no activity. "
            "When false, sleep continues for the full seconds duration."
        ),
    )
    follow_ups: list[SleepFollowUp] = Field(
        default=[],
        description=(
            "Messages to send at intervals during sleep. "
            "Cancelled (saved as ideas) if the user speaks before they fire."
        ),
    )
    force: bool = Field(
        default=False,
        description="Bypass the pending-event guard (sleep even with unread events).",
    )


class DebounceInput(BaseModel):
    """Input for the debounce tool."""

    initial_seconds: int = Field(
        description="Wait up to this long for the first event."
    )
    quiet_seconds: int = Field(
        description="After first event, wait this long after each subsequent event."
    )


class ReplyMessageItem(BaseModel):
    """A single message in a reply batch."""

    message: str = Field(description="The message content.")
    delay_seconds: int = Field(
        default=0,
        description="Cumulative delay before sending (0 = immediate).",
    )


class ReplyInput(BaseModel):
    """Input for the reply tool."""

    messages: list[ReplyMessageItem] = Field(
        description="Messages to send, with optional staggered delays."
    )


class ContextInput(BaseModel):
    """Input for the context tool."""

    last_events: int = Field(
        default=5,
        description="Number of recent read events to include (0 = unread only).",
    )


class MetaInput(BaseModel):
    """Input for the meta tool."""

    thought: str = Field(
        description=(
            "Process self-assessment: pacing, timing, what worked, "
            "what you'd change. Required before sleep."
        )
    )


class RemindInput(BaseModel):
    """Input for the remind tool."""

    label: str = Field(description="What this reminder is about.")
    delay_seconds: int = Field(description="Seconds until the reminder fires.")


class ScheduleActionInput(BaseModel):
    """Input for the schedule_action tool."""

    content: str = Field(description="Content to deliver when the action fires.")
    delay_seconds: int = Field(
        description="Seconds to wait before firing (cancelled if an event arrives)."
    )


# =====================================================================
# Tool output models
# =====================================================================


class ReplyOutput(BaseModel):
    """Output for the reply tool."""

    sent: int
    scheduled: int


class ScheduleActionOutput(BaseModel):
    """Output for the schedule_action tool."""

    delay_seconds: int


class DebounceOutput(BaseModel):
    """Output for the debounce tool."""

    initial_seconds: int
    quiet_seconds: int


class SleepOutput(BaseModel):
    """Output for the sleep tool."""

    reason: str = Field(default="timer")
    time: str = Field(default="")
    fired_reminders: list[str] = []


class RemindOutput(BaseModel):
    """Output for the remind tool."""

    label: str
    delay_seconds: int


class ContextOutput(BaseModel, extra="allow"):
    """Output for the context tool. Accepts domain-specific fields."""


class MetaOutput(BaseModel):
    """Output for the meta tool."""

    status: str = Field(default="recorded")
