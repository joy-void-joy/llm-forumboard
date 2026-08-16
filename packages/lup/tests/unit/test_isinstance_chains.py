"""Tests for the isinstance-chain rule: a repeat narrowing wants a match."""

from pathlib import Path

import pytest

from lup.codescan.common import PythonSource
from lup.codescan.narrowing import RULE_ID, audit_isinstance_chains


def source(text: str, name: str = "sample") -> PythonSource:
    return PythonSource(path=Path(f"{name}.py"), module=name, text=text)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (
            "if isinstance(n, Name):\n    a()\nelif isinstance(n, Attribute):\n    b()",
            "the plain elif chain",
        ),
        (
            "if isinstance(n, Name):\n    a()\nelse:\n"
            "    if isinstance(n, Attribute):\n        b()",
            "a lone if nested in an else is the same tree as an elif",
        ),
        (
            "if isinstance(n, Name) and n.id:\n    a()\nelif isinstance(n, Attribute):"
            "\n    b()",
            "an and conjunct survives translation as the arm's guard",
        ),
        (
            "if isinstance(n, Name):\n    a()\nelif flag:\n    b()\n"
            "elif isinstance(n, Attribute):\n    c()",
            "an unrelated arm between the two becomes case _ if flag",
        ),
        (
            "if isinstance(n.func, Attribute) and isinstance(n, Call):\n    a()\n"
            "elif isinstance(n, Name):\n    b()",
            "every conjunct is read, not just the first",
        ),
        (
            "if isinstance(n, Name):\n    a()\nelif isinstance(n, Attribute):\n"
            "    b()\nelse:\n    c()",
            "a trailing else is the fallthrough case _",
        ),
    ],
)
def test_repeat_narrowing_of_one_subject_is_reported(body: str, reason: str) -> None:
    findings = audit_isinstance_chains([source(body)])

    assert [finding.kind for finding in findings] == ["missing"], reason
    assert findings[0].rule_id == RULE_ID
    assert "case arm per type" in findings[0].message


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (
            "if isinstance(n, Name):\n    a()\nelse:\n    b()",
            "one narrowing plus an else is what CLAUDE.md sanctions",
        ),
        (
            "if isinstance(n, Name):\n    a()\nelif isinstance(other, Attribute):\n"
            "    b()",
            "different subjects are not one value's types",
        ),
        (
            "if isinstance(n, Name) or fallback:\n    a()\nelif isinstance(n, Attr):\n"
            "    b()",
            "a disjunct arm is reachable without the type holding",
        ),
        (
            "names = [x for x in items if isinstance(x, Name)]\n"
            "attrs = [x for x in items if isinstance(x, Attribute)]",
            "expression position has no match spelling at all",
        ),
        (
            "value = [] if isinstance(content, str) else list(content)",
            "a ternary is an expression, not a chain",
        ),
        (
            "if not isinstance(result, Model):\n    raise TypeError\n"
            "if not isinstance(other, Model):\n    raise TypeError",
            "sequential guard clauses are separate statements, not one chain",
        ),
        (
            "if isinstance(n, Name):\n    a()\nelif callable(n):\n    b()\n"
            "elif inspect.ismodule(n):\n    c()",
            "one isinstance among predicate arms is still a single narrowing",
        ),
    ],
)
def test_single_narrowings_and_expressions_stay_silent(body: str, reason: str) -> None:
    assert audit_isinstance_chains([source(body)]) == [], reason


def test_one_chain_is_reported_once_not_once_per_arm() -> None:
    """Every `elif` is an `ast.If`, so only the head may be reported."""
    body = (
        "if isinstance(n, A):\n    a()\n"
        "elif isinstance(n, B):\n    b()\n"
        "elif isinstance(n, C):\n    c()\n"
    )

    findings = audit_isinstance_chains([source(body)])

    assert len(findings) == 1
    assert findings[0].line == 1
    assert findings[0].message.startswith("3 isinstance arms")


def test_an_arm_narrowing_one_subject_twice_counts_once() -> None:
    """What makes a chain is a second *arm* deciding, not a second call."""
    body = "if isinstance(n, A) and isinstance(n, B):\n    a()\nelse:\n    b()\n"

    assert audit_isinstance_chains([source(body)]) == []


def test_the_rule_is_strong_and_refuses_every_directive() -> None:
    """A strong rule has no reasoned exception, so a directive is dead text."""
    body = (
        "if isinstance(n, A):  # lup: ignore[isinstance-chain]\n    a()\n"
        "elif isinstance(n, B):\n    b()\n"
    )

    findings = audit_isinstance_chains([source(body)])
    kinds = {finding.kind for finding in findings}

    assert "missing" in kinds, "the directive must not silence the violation"
    assert "spurious" in kinds, "and is reported as the dead marker it now is"
    missing = next(finding for finding in findings if finding.kind == "missing")
    assert "no suppression: write the replacement" in missing.message


def test_a_file_level_directive_does_not_silence_it_either() -> None:
    body = (
        "# lup: ignore[isinstance-chain]\n"
        "if isinstance(n, A):\n    a()\nelif isinstance(n, B):\n    b()\n"
    )

    findings = audit_isinstance_chains([source(body)])

    assert [finding.kind for finding in findings].count("missing") == 1


def test_unparseable_source_is_skipped_rather_than_raising() -> None:
    assert audit_isinstance_chains([source("def broken(:\n")]) == []
