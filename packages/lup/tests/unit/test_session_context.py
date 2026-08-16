"""The session-context env relay between agent process and tool subprocess.

:class:`SessionContext` promises its producer (``to_env``) and consumer
(:func:`read_session_context`) cannot drift apart. That promise is only
real if the round trip is pinned: every field written must come back
equal, absent optionals must stay ``None`` rather than become empty
strings, and a process not launched by an adapter must read ``None``.
"""

from pathlib import Path

import pytest

from lup.workspace.context import (
    SESSION_DIR_ENV,
    SessionContext,
    read_session_context,
)
from lup.workspace.notes import session_gate_flag


@pytest.fixture(autouse=True)
def clean_relay_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SessionContext(session_dir=Path("/tmp")).to_env():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("LUP_REALTIME_DIR", raising=False)


def apply_env(monkeypatch: pytest.MonkeyPatch, context: SessionContext) -> None:
    for name, value in context.to_env().items():
        monkeypatch.setenv(name, value)


def test_full_context_round_trips_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    context = SessionContext(
        session_dir=Path("/notes/sessions/s1"),
        outputs_dir=Path("/notes/outputs/t1"),
        gate_flag=Path("/notes/sessions/s1/.reflected"),
        session_id="s1",
        task_id="t1",
        realtime_dir=Path("/notes/realtime/s1"),
    )

    apply_env(monkeypatch, context)

    assert read_session_context() == context


def test_minimal_context_keeps_absent_fields_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = SessionContext(session_dir=Path("/notes/sessions/s2"))

    env_keys = set(context.to_env())  # lup: ignore[set-shape] — key equality
    assert env_keys == {SESSION_DIR_ENV}
    apply_env(monkeypatch, context)

    assert read_session_context() == context


def test_process_outside_a_session_reads_none() -> None:
    assert read_session_context() is None


def test_gate_flag_lives_outside_agent_writable_roots(tmp_path: Path) -> None:
    # The codex sandbox grants the workspace and /tmp; a flag the agent
    # could write itself would make the reflection gate forgeable.
    flag = session_gate_flag("s1")

    assert not flag.is_relative_to(tmp_path)
    assert not flag.is_relative_to(Path.cwd())
    assert not flag.is_relative_to(Path("/tmp"))
    assert flag.is_relative_to(Path.home())
    assert flag.name == "s1.reflection"
