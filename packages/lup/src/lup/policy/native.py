"""The wire seams a native adapter implements around the semantic core.

:class:`NativeEventDecoder` turns one provider's raw hook payload into the
semantic events of :mod:`lup.policy.models`; :class:`NativeDecisionRenderer`
turns a :class:`~lup.policy.models.Decision` back into that provider's wire
response. Implementations live in ``lup.adapters.<provider>.native``; nothing
here decides — the kernel does.
"""

from abc import ABC, abstractmethod

from lup.policy.models import Decision, SemanticEvent
from lup.types import JsonObject


class NativeEventDecoder[N](ABC):
    """Decode one native boundary into Lup semantic events."""

    @abstractmethod
    def decode(self, event: N) -> SemanticEvent:
        """Decode or return conservative typed evidence."""


class NativeDecisionRenderer[N](ABC):
    """Render a semantic decision for one native boundary."""

    @abstractmethod
    def render(self, decision: Decision, tool_input: JsonObject | None = None) -> N:
        """Render representable effects and fail closed otherwise.

        ``tool_input`` is the judged call's own arguments, which a runtime
        that places a call as well as deciding it has to carry back. One
        that cannot place reads the verdict alone and ignores them.
        """
