# lup: ignore[dict-str-object, bare-object, set-shape]
# Monkeypatch fixtures intentionally accept and record arbitrary call boundaries.
"""Reviewer composition receives a factory, a prompt, and a strict output model."""

from types import SimpleNamespace

import pytest

from lup.reflect import ReviewResult, ReviewVerdict
from lup_template.agent import core
from lup_template.agent.tools import reflect


def make_input() -> reflect.ReflectInput:
    return reflect.ReflectInput(
        assessment="solid work",
        confidence=0.7,
        tool_audit="all tools fine",
        process_reflection="smooth",
    )


async def test_reviewer_uses_explicit_factory_and_typed_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: dict[str, object] = {}
    marker = object()

    def build(**kwargs: object) -> object:
        built.update(kwargs)
        return marker

    requested: dict[str, object] = {}

    async def query(factory: object, prompt: object, output_type: object) -> object:
        requested.update(factory=factory, prompt=prompt, output_type=output_type)
        return SimpleNamespace(
            output=ReviewResult(
                verdict=ReviewVerdict.approve,
                assessment="critique",
            )
        )

    monkeypatch.setattr(core, "build_auxiliary_factory", build)
    monkeypatch.setattr(reflect, "query", query)

    result = await reflect.run_reviewer(make_input(), None, model="review-model")

    assert result is not None
    assert result.assessment == "critique"
    assert built["model"] == "review-model"
    assert built["tools"] == ["Read", "Glob", "Grep", "WebFetch"]
    assert requested["factory"] is marker
    assert requested["output_type"] is ReviewResult
    assert isinstance(requested["prompt"], str)


async def test_reviewer_factory_shape_does_not_depend_on_model_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def build(**kwargs: object) -> object:
        calls.append(kwargs)
        return object()

    async def query(_factory: object, _prompt: object, _output_type: object) -> object:
        return SimpleNamespace(
            output=ReviewResult(
                verdict=ReviewVerdict.approve,
                assessment="ok",
            )
        )

    monkeypatch.setattr(core, "build_auxiliary_factory", build)
    monkeypatch.setattr(reflect, "query", query)

    await reflect.run_reviewer(make_input(), None, model="claude-model")
    await reflect.run_reviewer(make_input(), None, model="gpt-model")

    assert set(calls[0]) == set(calls[1])
    assert calls[0]["tools"] == calls[1]["tools"]
