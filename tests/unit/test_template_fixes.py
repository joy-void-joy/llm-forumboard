"""Regression tests for the provider-neutral application template."""

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import BaseModel

from lup_template.agent import prompts
from lup.reflect import ReviewGate
from lup_template.agent.config import aux_model, engine_for_settings, settings
from lup_template.agent.core import reflection_submission_gate
from lup.runtime.contracts import Session, Turn
from lup.runtime.factory import SessionFactory
from lup.runtime.models import (
    SessionHandle,
    SessionId,
    TurnBlock,
    TurnHandle,
    TurnId,
    TurnIdentifiers,
    TurnRequest,
    TurnResult,
    TurnTextBlock,
    turn_request,
)
from lup.telemetry.trace import TraceLogger
from lup.types import Usage
from lup.workspace.notes import NotesConfig
from lup_template.agent.core import (
    decorate_factory,
    normalize_codex_approval,
    provider_factory,
)
from lup_template.agent.models import AgentOutput
from lup_template.agent.tool_policy import ToolPolicy


def test_prompt_renders_with_literal_braces_in_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    json_section = '## Output\n```json\n{"probability": 0.5, "factors": []}\n```'
    monkeypatch.setattr(prompts, "SECTIONS", ["Today is {date}.", json_section])

    rendered = prompts.get_system_prompt()

    assert '{"probability": 0.5, "factors": []}' in rendered
    assert "{date}" not in rendered


def test_prompt_substitutes_date_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prompts, "SECTIONS", ["date={date}"])
    assert "date=2030-01-02" in prompts.get_system_prompt(date=datetime(2030, 1, 2))


def test_output_format_section_derives_from_model() -> None:
    section = prompts.output_format()
    for field_name in AgentOutput.model_fields:
        assert field_name in section


def test_allowed_tools_are_supplied_by_the_concrete_composition() -> None:
    builtins = frozenset(  # lup: ignore[frozenset-shape] — immutable policy fixture
        {"Read", "TodoWrite"}
    )
    allowed = ToolPolicy(settings).get_allowed_tools({}, builtin_tools=builtins)
    assert allowed == ["Read", "TodoWrite"]


def test_aux_model_explicit_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "aux_model", "my-reviewer")
    monkeypatch.setattr(settings, "agent_sdk", "codex")
    assert aux_model() == "my-reviewer"


def test_aux_model_claude_defaults_to_opus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "aux_model", None)
    monkeypatch.setattr(settings, "agent_sdk", "claude")
    monkeypatch.setattr(settings, "openai_base_url", None)
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    assert aux_model() == "claude-opus-5"


def test_aux_model_compat_endpoint_reuses_session_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "aux_model", None)
    monkeypatch.setattr(settings, "agent_sdk", "claude")
    monkeypatch.setattr(settings, "model", "anthropic/claude-opus-5")
    monkeypatch.setattr(settings, "openai_base_url", None)
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")
    assert aux_model() == "anthropic/claude-opus-5"


def test_engine_router_explicit_agent_sdk_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "agent_sdk", "claude")
    monkeypatch.setattr(settings, "model", "gpt-5.6-sol")
    assert engine_for_settings() == "claude"


def test_codex_approval_normalizes_current_and_legacy_spellings() -> None:
    expected = {
        "untrusted": "untrusted",
        "on-request": "on-request",
        "granular": "granular",
        "never": "never",
        "unlessTrusted": "untrusted",
        "onRequest": "on-request",
    }
    assert {value: normalize_codex_approval(value) for value in expected} == expected
    assert normalize_codex_approval(None) is None


def test_codex_approval_rejects_unknown_spelling() -> None:
    with pytest.raises(ValueError, match="app-server accepts"):
        normalize_codex_approval("always")


def test_engine_router_claude_prefix_runs_native_claude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "agent_sdk", None)
    monkeypatch.setattr(settings, "model", "claude-fable-5")
    assert engine_for_settings() == "claude"


def test_engine_router_openai_prefixes_run_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "agent_sdk", None)
    for model in ("gpt-5.6-sol", "o4-mini", "codex-mini-latest"):
        monkeypatch.setattr(settings, "model", model)
        assert engine_for_settings() == "codex"


def test_engine_router_openrouter_fallback_runs_claude_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "agent_sdk", None)
    monkeypatch.setattr(settings, "model", "anthropic/claude-opus-5")
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key")
    assert engine_for_settings() == "claude-compat"


def test_engine_router_unknown_model_runs_openai_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "agent_sdk", None)
    monkeypatch.setattr(settings, "model", "llama-4-scout")
    monkeypatch.setattr(settings, "openrouter_api_key", None)
    assert engine_for_settings() == "openai-compat"


def test_aux_model_follows_the_routed_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "aux_model", None)
    monkeypatch.setattr(settings, "agent_sdk", None)
    monkeypatch.setattr(settings, "model", "gpt-5.6-sol")
    assert aux_model() == "gpt-5.6-sol"


def test_aux_model_codex_reuses_session_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "aux_model", None)
    monkeypatch.setattr(settings, "agent_sdk", "codex")
    monkeypatch.setattr(settings, "model", "gpt-5.5")
    assert aux_model() == "gpt-5.5"


@pytest.mark.asyncio
async def test_reflection_gate_is_the_typed_submission_gate() -> None:
    review = ReviewGate()
    gate = reflection_submission_gate(review)
    output = AgentOutput(summary="complete")

    assert not (await gate(output)).accepted
    review.mark_reflected()
    assert (await gate(output)).accepted


class StaticTurn[T: BaseModel | None](Turn[T]):
    def __init__(self, result: TurnResult[T]) -> None:
        self.value = result

    async def result(self) -> TurnResult[T]:
        return self.value


class StaticSession(Session):
    """Complete every turn with the same successful canned result."""

    def __init__(self, blocks: list[TurnBlock]) -> None:
        self.blocks = blocks

    async def start[T: BaseModel | None](
        self, request: TurnRequest[T]
    ) -> TurnHandle[T]:
        result = TurnResult[T].model_validate(
            {
                "output": None,
                "messages": [],
                "blocks": self.blocks,
                "usage": Usage(),
                "duration": timedelta(),
                "identifiers": TurnIdentifiers(
                    session=SessionId(value="decorated"),
                    turn=TurnId(value="turn-1"),
                ),
            }
        )
        return TurnHandle[T](turn=StaticTurn(result))


def static_session_factory(blocks: list[TurnBlock]) -> SessionFactory:
    @asynccontextmanager
    async def open_static(
        _resume: SessionId | None = None,
    ) -> AsyncGenerator[SessionHandle]:
        yield SessionHandle(session=StaticSession(blocks))

    return SessionFactory(open_static)


@pytest.mark.asyncio
async def test_main_factory_decoration_wires_persistence_display_and_trace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    notes = NotesConfig(
        session=tmp_path / "session",
        output=tmp_path / "output",
        trace_log=tmp_path / "logs" / "trace.md",
    )
    trace = TraceLogger(trace_path=notes.trace_log, title="test")
    inner = static_session_factory([TurnTextBlock(text="decorated turn")])

    decorated = decorate_factory(inner, notes=notes, trace_logger=trace)
    result = await decorated.query(turn_request("run one turn"))

    assert result.blocks == [TurnTextBlock(text="decorated turn")]
    assert len(list((notes.trace_log.parent / "turns").glob("*.json"))) == 1
    assert notes.trace_log.exists()
    assert "decorated turn" in capsys.readouterr().out


def test_codex_rejects_explicit_claude_only_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(settings, "agent_sdk", "codex")
    monkeypatch.setattr(settings, "permission_mode", "plan")

    with pytest.raises(ValueError, match="AGENT_PERMISSION_MODE"):
        provider_factory(model="gpt", system_prompt="", cwd=tmp_path)
