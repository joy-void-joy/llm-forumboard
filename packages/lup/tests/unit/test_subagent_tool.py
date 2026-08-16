# lup: ignore[dict-get, dict-str-object, bare-object]
# External MCP ToolResponse dictionaries and monkeypatch call records are test fixtures.
"""Subagent delegation is driven by an explicitly injected factory recipe."""

from types import SimpleNamespace
from contextlib import AbstractAsyncContextManager

import pytest

import lup.subagents
from lup.mcp import ToolResponse
from lup.runtime.models import TurnTextBlock
from lup.runtime.factory import SessionFactory
from lup.runtime.models import SessionHandle, SessionId
from lup.subagents import create_run_subagent_tool
from lup.types import SubagentSpec

RESEARCHER = SubagentSpec(
    name="researcher",
    description="Researches questions",
    prompt="You research.",
    tools=["WebSearch"],
    model="haiku",
)


def marker_factory() -> SessionFactory:
    def refuse(
        resume: SessionId | None = None,
    ) -> AbstractAsyncContextManager[SessionHandle]:
        raise AssertionError(f"query should be replaced in this test: {resume}")

    return SessionFactory(refuse)


def response_text(response: ToolResponse) -> str:
    return "".join(item.get("text", "") for item in response.get("content", []))


class TestRunSubagentTool:
    async def test_unknown_role_lists_available(self) -> None:
        tool = create_run_subagent_tool(
            [RESEARCHER], factory_recipe=lambda _spec: marker_factory()
        )

        result = await tool.handler({"name": "ghost", "task": "x"})
        assert result.get("is_error") is True
        assert "researcher" in response_text(result)

    async def test_invalid_recipe_fails_loudly(self) -> None:
        def invalid(_spec: SubagentSpec) -> SessionFactory:
            raise ValueError("model route is unavailable")

        tool = create_run_subagent_tool([RESEARCHER], factory_recipe=invalid)
        result = await tool.handler({"name": "researcher", "task": "x"})

        assert result.get("is_error") is True
        assert "model route is unavailable" in response_text(result)

    async def test_dispatches_query_with_selected_spec(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        selected: list[SubagentSpec] = []
        marker = marker_factory()

        def recipe(spec: SubagentSpec) -> SessionFactory:
            selected.append(spec)
            return marker

        captured: dict[str, object] = {}

        async def query(factory: object, prompt: object) -> object:
            captured.update(factory=factory, prompt=prompt)
            return SimpleNamespace(blocks=[TurnTextBlock(text="findings")])

        monkeypatch.setattr(lup.subagents, "query", query)
        tool = create_run_subagent_tool([RESEARCHER], factory_recipe=recipe)

        result = await tool.handler({"name": "researcher", "task": "look this up"})

        assert result.get("is_error", False) is False
        assert "findings" in response_text(result)
        assert selected == [RESEARCHER]
        assert captured["factory"] is marker
        assert captured["prompt"] == "look this up"
