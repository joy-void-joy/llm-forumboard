# lup: ignore[empty-collection, set-shape]
# Test fixtures and assertions construct these shapes deliberately.
"""Live parity test: the same task on both SDK backends.

Costs real LLM calls on two providers, so it carries the integration
marker and runs only with ``-m integration`` (the nightly lane). This is
the executable half of the parity contract: both backends must finish
the Tier-1 core loop — reflection gate, submit_output finalization,
metrics flush, usage accounting, backend stamp — and produce results of
identical shape.

A non-empty output is itself proof the gate ran: submit_output rejects
until review has been called, so output implies reflection ordered
correctly end to end.
"""

import uuid

import pytest

from lup_template.agent.config import settings
from lup_template.agent.core import run_agent

pytestmark = pytest.mark.integration

TASK = (
    "Smoke task: reflect via review with skip_reviewer=true, then submit "
    "a one-line output."
)


async def test_both_backends_complete_the_core_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = {}
    # A fresh session id per run: reused ids accumulate persisted turn and
    # session state under notes/ across nightly runs and contaminate replay.
    stamp = uuid.uuid4().hex[:8]
    for sdk, model in (("claude", "claude-haiku-4-5-20251001"), ("codex", "gpt-5.5")):
        monkeypatch.setattr(settings, "agent_sdk", sdk)
        monkeypatch.setattr(settings, "model", model)
        results[sdk] = await run_agent(TASK, session_id=f"parity-{sdk}-{stamp}")

    for sdk, result in results.items():
        assert result.output.summary, f"{sdk}: empty output summary"
        assert result.output.summary != "No output produced", f"{sdk}: no submission"
        assert result.agent_sdk == sdk, f"{sdk}: backend stamp missing or wrong"
        assert result.tool_metrics is not None, f"{sdk}: missing tool metrics"
        assert result.token_usage is not None, f"{sdk}: missing token usage"
        assert result.token_usage.output_tokens > 0, f"{sdk}: zero output tokens"
        assert result.duration_seconds, f"{sdk}: missing duration"
        assert result.agent_version == results["claude"].agent_version

    claude_fields = set(results["claude"].model_dump(exclude_none=True))
    codex_fields = set(results["codex"].model_dump(exclude_none=True))
    # cost_usd is the one allowed asymmetry: native on Claude, absent on
    # Codex unless CODEX_USD_PER_MTOK_* rates are configured.
    assert claude_fields - codex_fields <= {"cost_usd"}, (
        f"shape drift: {claude_fields ^ codex_fields}"
    )

    from lup.workspace.history import (
        iter_session_dirs,
        load_session_records,
        session_backend,
    )

    for sdk in results:
        loaded = load_session_records(f"parity-{sdk}-{stamp}")
        assert loaded, (
            f"{sdk}: session JSON not retrievable through lup.workspace.history"
        )
        assert loaded[0].agent_sdk == sdk, f"{sdk}: stamp lost in persistence"
        session_dirs = list(iter_session_dirs(session_id=f"parity-{sdk}-{stamp}"))
        assert session_dirs and session_backend(session_dirs[0]) == sdk, (
            f"{sdk}: trace tooling cannot detect the backend"
        )
