"""Policy composition semantics: deny precedence and observer isolation.

`lup.policy.chain` composes the semantic policies the harness dispatcher
is generated from, so its precedence rules are security properties: one
deny must override any number of allows, an explicit ask must survive
surrounding allows, an empty chain must fail conservatively to ask, and
observers must never weaken a verdict — a raising observer is reported
as evidence, not raised past the decision.
"""

from typing import Literal

from lup.policy.chain import OrderedPolicyChain, PolicyDispatcher, UnknownToolPolicy
from lup.policy.contracts import DecisionPolicy, Observer
from lup.policy.models import Decision, ToolIdentity, UnknownTool


class FixedPolicy(DecisionPolicy[str]):
    """A member policy returning one predetermined verdict."""

    def __init__(
        self, effect: Literal["allow", "ask", "deny", "defer"], reason: str = ""
    ) -> None:
        self.verdict = Decision(effect=effect, reason=reason)

    def decide(self, event: str) -> Decision:
        return self.verdict


def test_deny_wins_over_allow_and_ask_and_keeps_its_reason() -> None:
    chain = OrderedPolicyChain(
        [
            FixedPolicy("allow"),
            FixedPolicy("ask", "needs review"),
            FixedPolicy("deny", "forbidden path"),
        ]
    )

    decision = chain.decide("event")

    assert decision.effect == "deny"
    assert decision.reason == "forbidden path"


def test_ask_wins_over_allow_when_nothing_denies() -> None:
    chain = OrderedPolicyChain(
        [FixedPolicy("allow"), FixedPolicy("ask", "needs review"), FixedPolicy("allow")]
    )

    decision = chain.decide("event")

    assert decision.effect == "ask"
    assert decision.reason == "needs review"


def test_defer_wins_over_allow_but_yields_to_ask() -> None:
    deferred = OrderedPolicyChain(
        [FixedPolicy("allow"), FixedPolicy("defer", "sandbox boundary")]
    )
    decision = deferred.decide("event")
    assert decision.effect == "defer"
    assert decision.reason == "sandbox boundary"

    asked = OrderedPolicyChain(
        [FixedPolicy("defer"), FixedPolicy("ask", "needs review")]
    )
    assert asked.decide("event").effect == "ask"


def test_unanimous_allows_produce_allow() -> None:
    chain = OrderedPolicyChain([FixedPolicy("allow"), FixedPolicy("allow")])

    assert chain.decide("event").effect == "allow"


def test_empty_chain_fails_conservatively_to_ask() -> None:
    decision = OrderedPolicyChain([]).decide("event")

    assert decision.effect == "ask"
    assert "no policy classified" in decision.reason


def test_unknown_tool_ask_names_the_original_tool() -> None:
    event = UnknownTool(identity=ToolIdentity(original_name="mystery_tool"))

    decision = UnknownToolPolicy().decide(event)

    assert decision.effect == "ask"
    assert "mystery_tool" in decision.reason


class RecordingObserver(Observer[str]):
    def __init__(self) -> None:
        self.seen: list[str] = []

    def observe(self, event: str) -> None:
        self.seen.append(event)


class BoomObserver(Observer[str]):
    def observe(self, event: str) -> None:
        raise RuntimeError("sink offline")


def test_raising_observer_is_reported_without_weakening_the_verdict() -> None:
    recorder = RecordingObserver()
    dispatcher = PolicyDispatcher(
        FixedPolicy("deny", "forbidden"), [BoomObserver(), recorder]
    )

    evaluation = dispatcher.evaluate("event")

    assert evaluation.decision.effect == "deny"
    assert [(f.observer, f.message) for f in evaluation.observation_failures] == [
        ("BoomObserver", "sink offline")
    ]
    assert recorder.seen == ["event"]  # a failure upstream never starves later sinks


def test_dispatcher_without_observers_reports_no_failures() -> None:
    evaluation = PolicyDispatcher(FixedPolicy("allow")).evaluate("event")

    assert evaluation.decision.effect == "allow"
    assert evaluation.observation_failures == []
