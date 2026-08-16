"""Deny-before-ask composition that can never weaken a member verdict.

``OrderedPolicyChain`` folds several
:class:`~lup.policy.contracts.DecisionPolicy` verdicts into the strictest
one, ``UnknownToolPolicy`` refuses the calls a project declared against and
holds every other unclassified tool at ``ask``, and
``PolicyDispatcher`` notifies observers after deciding without letting an
observer failure alter the outcome. The shared fixture suite and embedding
applications compose the :mod:`lup.policy.rules` policies through here.
"""

from lup.policy.contracts import DecisionPolicy, Observer
from lup.policy.kernel.tools import decide_tool
from lup.policy.refused_tools import RefusedTool, erase_refused_tools
from lup.policy.models import (
    Decision,
    ObservationFailure,
    PolicyEvaluation,
    UnknownTool,
)


class OrderedPolicyChain[E](DecisionPolicy[E]):
    """Aggregate named policies with deny-before-ask precedence."""

    def __init__(self, policies: list[DecisionPolicy[E]]) -> None:
        self.policies = list(policies)

    def decide(self, event: E) -> Decision:
        decisions = [policy.decide(event) for policy in self.policies]
        denied = next(
            (decision for decision in decisions if decision.effect == "deny"), None
        )
        if denied is not None:
            return denied
        asked = next(
            (decision for decision in decisions if decision.effect == "ask"), None
        )
        if asked is not None:
            return asked
        deferred = next(
            (decision for decision in decisions if decision.effect == "defer"), None
        )
        if deferred is not None:
            return deferred
        if decisions:
            return Decision(effect="allow")
        return Decision(effect="ask", reason="no policy classified this event")


class UnknownToolPolicy(DecisionPolicy[UnknownTool]):
    """Refuse a declared call, and fail conservatively on the rest.

    A refusal table is the one rule surface a tool with no semantics of its
    own can have, so it is consulted here rather than in a family of its own:
    what is being judged is still the call nothing classified. Everything the
    table does not speak to keeps the conservative ask.
    """

    def __init__(self, refused: list[RefusedTool] | None = None) -> None:
        self.refused = erase_refused_tools(refused or [])

    def decide(self, event: UnknownTool) -> Decision:
        name = event.identity.original_name
        refusal = decide_tool(
            name,
            [value for value in event.input.values() if isinstance(value, str)],
            self.refused,
        )
        if refusal is not None:
            return Decision(effect=refusal.effect, reason=refusal.reason)
        return Decision(effect="ask", reason=f"unclassified tool {name!r}")


class PolicyDispatcher[E]:
    """Evaluate once, then notify observers without weakening the verdict."""

    def __init__(
        self, policy: DecisionPolicy[E], observers: list[Observer[E]] | None = None
    ) -> None:
        self.policy = policy
        self.observers = list(observers or [])

    def evaluate(self, event: E) -> PolicyEvaluation:
        decision = self.policy.decide(event)
        failures: list[ObservationFailure] = []
        for observer in self.observers:
            try:
                observer.observe(event)
            except Exception as error:
                failures.append(
                    ObservationFailure(
                        observer=type(observer).__name__, message=str(error)
                    )
                )
        return PolicyEvaluation(decision=decision, observation_failures=failures)
