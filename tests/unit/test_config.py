"""Behavior tests for Settings env parsing (agent/config.py)."""

from pathlib import Path

import pytest

from lup_template.agent.config import Settings, engine_for_model


class EnvOnlySettings(Settings, env_file=None, extra="ignore"):
    """Settings variant that ignores .env files — process env vars only."""


def test_extra_dirs_parses_path_style_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_EXTRA_DIRS", "/data/reference:/home/u/transcripts")
    assert EnvOnlySettings().extra_dirs == [
        Path("/data/reference"),
        Path("/home/u/transcripts"),
    ]


def test_extra_dirs_defaults_empty_and_skips_blank_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_EXTRA_DIRS", raising=False)
    assert EnvOnlySettings().extra_dirs == []

    monkeypatch.setenv("AGENT_EXTRA_DIRS", "/only:")
    assert EnvOnlySettings().extra_dirs == [Path("/only")]


def test_engine_for_model_routes_by_vendor_prefix_alone() -> None:
    assert engine_for_model("claude-opus-5") == "claude"
    assert engine_for_model("gpt-5.6-sol") == "codex"
    assert engine_for_model("codex-mini-latest") == "codex"
    assert engine_for_model("o4-mini") == "codex"
    assert engine_for_model("qwen3-coder") in {"claude-compat", "openai-compat"}
