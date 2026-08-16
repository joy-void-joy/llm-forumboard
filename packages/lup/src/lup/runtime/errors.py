"""Typed errors for unsuccessful turns and invalid session transitions."""

from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from lup.runtime.models import AnyTurnBlock, TurnIdentifiers
from lup.types import Usage


class ValidationAttempt(BaseModel, frozen=True):
    """One rejected structured-output submission."""

    message: str


class TurnFailure(BaseModel, frozen=True):
    """Partial evidence available when a logical turn fails."""

    message: str
    blocks: list[AnyTurnBlock] = []
    usage: Usage = Field(default_factory=Usage)
    duration: timedelta = timedelta()
    identifiers: TurnIdentifiers | None = None
    validation_history: list[ValidationAttempt] = []

    correctable: bool = True
    """Whether re-prompting could produce a different outcome.

    A correction cycle re-sends the turn with an instruction appended, which
    is worth doing when the model could have answered and did not. When the
    submission tool was refused rather than misused, the same prompt meets
    the same refusal, so the cycles only delay the failure they report.
    """

    environmental: bool = False
    """Whether the fault is a property of the host rather than of the work.

    A revoked credential, an exhausted session allowance or a dead network
    says nothing about the turn that met it: the same request on a healthy
    host would have succeeded. Callers that record a verdict need the two
    apart, because attributing an expired login to the work makes a record
    that reads as the work having failed and cannot be told from one.

    False by default, so a fault nobody has classified is attributed to the
    turn — the conservative direction, since treating a real failure as
    environmental would retry it forever.
    """


class TurnError(Exception):
    """Base for failures carrying complete available partial evidence."""

    def __init__(self, failure: TurnFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


class ProviderTurnError(TurnError):
    """The provider rejected or failed a turn."""


class TurnTimeoutError(TurnError):
    """The whole logical turn exceeded its deadline."""


class BudgetExceededError(TurnError):
    """The whole logical turn exhausted its configured budget."""


class QuotaExceededError(TurnError):
    """A provider account allowance is exhausted until its reset time.

    Distinct from :class:`BudgetExceededError`, which is our own ceiling on a
    turn: nothing was overspent here, the account simply cannot buy more work
    until the provider's window rolls over. Carrying that moment as data is
    what lets a caller wait exactly long enough instead of retrying blind.
    """

    def __init__(
        self,
        failure: TurnFailure,
        *,
        reset_at: datetime | None = None,
        quota_type: str | None = None,
    ) -> None:
        super().__init__(failure)
        self.reset_at = reset_at
        self.quota_type = quota_type


class TurnInterruptedError(TurnError):
    """The active turn ended because interruption was requested."""


class TurnAbortedError(TurnError):
    """The session closed before the active turn completed."""


class StructuredOutputError(TurnError):
    """A required valid submission was not produced."""


class TurnAlreadyActiveError(RuntimeError):
    """A session was asked to start a second concurrent turn."""


class DeltaStreamingDisabled(RuntimeError):
    """A live view was asked of a session built without partial streaming.

    Raised rather than yielding a delta-free stream, because a stream that
    is quiet because nothing was configured looks exactly like one that is
    quiet because nothing happened.
    """
