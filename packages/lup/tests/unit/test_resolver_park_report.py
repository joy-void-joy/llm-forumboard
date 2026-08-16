"""What a park tells a human is still theirs to answer."""

import pytest

from lup.devtools.harness.resolve import host_retry_delay, report_awaiting
from lup.devtools.supervisor.projection import PendingQuestionView
from lup.resolver.contracts import ResolverAwaitingAnswers
from lup.resolver.models import MaterialQuestion


def question(identifier: str) -> MaterialQuestion:
    return MaterialQuestion(
        id=identifier,
        concern_id="alpha",
        prompt=f"What should {identifier} do?",
        choices=["one", "two"],
    )


def view(
    identifier: str, answered: str | None = None, offer: str | None = None
) -> PendingQuestionView:
    return PendingQuestionView(
        question=question(identifier),
        asked_by="alpha",
        answered=answered,
        offer=offer,
    )


def test_a_question_answered_while_the_run_worked_is_not_asked_again(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The reported case: answered and promoted, then relayed 37 minutes later.

    `pending` is the list one concern held when it raised, and the run kept
    going. Printed unfiltered, the report named a settled question and told
    the human to answer it again.
    """
    parked = ResolverAwaitingAnswers([question("settled"), question("open")], [])

    report_awaiting(
        parked,
        "claude",
        "run-1",
        [],
        [view("settled", answered="one"), view("open")],
    )

    printed = capsys.readouterr().out
    assert "question open" in printed
    assert "question settled" not in printed
    assert "1 question(s) raised with this park were answered" in printed


def test_an_offered_answer_settles_a_question_for_this_report(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """It waits on the run to take it, not on somebody to decide it."""
    parked = ResolverAwaitingAnswers([question("offered")], [])

    report_awaiting(parked, "claude", "run-1", [], [view("offered", offer="two")])

    printed = capsys.readouterr().out
    assert "question offered" not in printed
    assert "Every question this park raised is answered" in printed


def test_the_rerun_recipe_names_only_what_is_still_open(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Answering a settled question again is the round trip this cost."""
    parked = ResolverAwaitingAnswers([question("settled"), question("open")], [])

    report_awaiting(
        parked,
        "claude",
        "run-1",
        [],
        [view("settled", answered="one"), view("open")],
    )

    recipe = [
        line
        for line in capsys.readouterr().out.splitlines()
        if "harness resolve" in line
    ]
    assert recipe
    assert "settled=" not in recipe[0]
    assert "open=" in recipe[0]


def test_a_park_with_nothing_answered_yet_relays_everything(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The ordinary park, which must keep reading exactly as it did."""
    parked = ResolverAwaitingAnswers([question("first"), question("second")], [])

    report_awaiting(parked, "claude", "run-1", [], [view("first"), view("second")])

    printed = capsys.readouterr().out
    assert "question first" in printed
    assert "question second" in printed
    assert "were answered while it ran" not in printed
    assert "Relay the questions to the human" in printed


def test_coming_back_to_a_refusing_host_backs_off_into_a_ceiling() -> None:
    """Doubling reaches an allowance's reset without probing it every minute."""
    schedule = [host_retry_delay(attempt, 20, 60.0) for attempt in range(6)]

    assert schedule == [60.0, 120.0, 240.0, 480.0, 960.0, 1800.0]
    assert host_retry_delay(19, 20, 60.0) == 1800.0


def test_a_run_out_of_retries_stops_asking() -> None:
    """None is what parks the run, so the budget has to be reachable."""
    assert host_retry_delay(20, 20, 60.0) is None
    assert host_retry_delay(0, 0, 60.0) is None
