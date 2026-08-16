"""What a re-check ruling is allowed to decide.

The question asks whether a criterion that stopped holding was superseded by
later work or is a regression, and the two answers mean opposite things
about the review branch. Recording the answer and completing anyway made the
question a decision about nothing, so these pin what each ruling does.
"""

from lup.resolver.contracts import ResolverRegression
from lup.resolver.joins import asked_rulings
from lup.resolver.models import (
    RECHECK_REGRESSION,
    RECHECK_SUPERSEDED,
    ConcernOutcome,
    MaterialQuestion,
    QuestionAnswer,
    RecheckRuling,
)


def recheck(concern_id: str, *criteria: str) -> MaterialQuestion:
    return MaterialQuestion(
        id=f"{concern_id}-superseded-integrated",
        concern_id=concern_id,
        prompt="superseded or regression?",
        choices=[RECHECK_SUPERSEDED, RECHECK_REGRESSION],
        closed_choices=True,
        criteria=list(criteria),
    )


def answered(question: MaterialQuestion, value: str) -> QuestionAnswer:
    return QuestionAnswer(question_id=question.id, value=value)


def test_an_answered_recheck_is_paired_with_the_criteria_it_ruled_on() -> None:
    question = recheck("provenance", "venue-derived", "one-vocabulary")

    rulings = asked_rulings([question], [answered(question, RECHECK_REGRESSION)])

    assert rulings == [
        RecheckRuling(
            concern_id="provenance",
            criteria=["venue-derived", "one-vocabulary"],
            ruling=RECHECK_REGRESSION,
        )
    ]


def test_an_unanswered_recheck_rules_on_nothing() -> None:
    """No answer is not a silent "superseded" — it is simply not a ruling."""
    assert asked_rulings([recheck("provenance", "venue-derived")], []) == []


def test_superseded_and_regression_are_told_apart() -> None:
    settled = recheck("alpha", "one")
    broken = recheck("beta", "two")

    rulings = asked_rulings(
        [settled, broken],
        [answered(settled, RECHECK_SUPERSEDED), answered(broken, RECHECK_REGRESSION)],
    )

    regressed = [
        rule.concern_id for rule in rulings if rule.ruling == RECHECK_REGRESSION
    ]
    assert regressed == ["beta"]


def test_a_regression_names_every_concern_and_criterion_it_stopped_for() -> None:
    """#71's report: the human must be told what broke, not that something did."""
    error = ResolverRegression(
        [
            RecheckRuling(
                concern_id="source-provenance-fields",
                criteria=[
                    "venue-derived-from-acquisition",
                    "one-vocabulary-downstream",
                ],
                ruling=RECHECK_REGRESSION,
            )
        ]
    )

    assert "source-provenance-fields" in str(error)
    assert "venue-derived-from-acquisition" in str(error)
    assert "one-vocabulary-downstream" in str(error)


def test_a_regressed_criterion_is_recorded_on_the_outcome_that_lost_it() -> None:
    """The ruling outlives the invocation that heard it."""
    outcome = ConcernOutcome(
        concern_id="source-provenance-fields",
        branch="resolve/run/source-provenance-fields",
        commit="abc123",
        verified=True,
    )

    marked = outcome.model_copy(
        update={"regressed": ["venue-derived-from-acquisition"]}
    )

    assert marked.regressed == ["venue-derived-from-acquisition"]
    assert marked.verified, "verification was about its own lease, and still holds"


def test_an_outcome_carries_no_regression_until_one_is_ruled() -> None:
    outcome = ConcernOutcome(concern_id="alpha", branch="resolve/run/alpha")

    assert outcome.regressed == []
