"""Selecting a runtime is one assignment, and both runtimes answer it.

The autonomy roster is read out of the type rather than restated, so a degree
added to :data:`~lup.runtime.selection.SessionAutonomy` fails here until every
runtime has spelled it — which is the whole point of the request being a
declaration each runtime renders.
"""

from pathlib import Path
from typing import get_args

import pytest

from lup.adapters.claude.runtime import ClaudeSessionConfig
from lup.adapters.claude.selection import CLAUDE_AUTONOMY, CLAUDE_RUNTIME
from lup.adapters.codex.runtime import CodexSessionConfig
from lup.adapters.codex.selection import (
    CODEX_AUTONOMY,
    CODEX_RUNTIME,
    codex_mcp_server,
)
from lup.hooks import LupHooksConfig
from lup.runtime.factory import SessionFactory
from lup.runtime.selection import Runtime, SessionAutonomy, SessionRequest

AUTONOMY_DEGREES = get_args(SessionAutonomy.__value__)


@pytest.mark.parametrize("runtime", [CLAUDE_RUNTIME, CODEX_RUNTIME])
def test_a_runtime_carries_its_own_login(runtime: Runtime) -> None:
    assert runtime.name
    assert runtime.login.config_home_env
    assert runtime.login.credentials_file


@pytest.mark.parametrize("degree", AUTONOMY_DEGREES)
def test_every_runtime_spells_every_degree_of_autonomy(degree: str) -> None:
    assert degree in CLAUDE_AUTONOMY
    assert degree in CODEX_AUTONOMY


def test_claude_renders_the_whole_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rendered: list[ClaudeSessionConfig] = []

    def record(config: ClaudeSessionConfig) -> SessionFactory:
        rendered.append(config)
        return SessionFactory(lambda resume=None: None)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(
        "lup.adapters.claude.selection.create_claude_session_factory", record
    )
    CLAUDE_RUNTIME.session_factory(
        SessionRequest(
            model="a-model",
            instructions="be brief",
            cwd=tmp_path,
            autonomy="unattended",
            allowed_tools=["Read"],
            max_turns=3,
            environment={"KEEP": "1"},
            hooks=LupHooksConfig(),
        )
    )

    config = rendered[0]
    assert config.model == "a-model"
    assert config.system_prompt == "be brief"
    assert config.cwd == tmp_path
    assert config.permission_mode == "bypassPermissions"
    assert config.allowed_tools == ["Read"]
    assert config.max_turns == 3
    assert config.environment == {"KEEP": "1"}
    assert config.hooks is not None


def test_codex_renders_what_it_can_spell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rendered: list[CodexSessionConfig] = []

    def record(config: CodexSessionConfig) -> SessionFactory:
        rendered.append(config)
        return SessionFactory(lambda resume=None: None)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(
        "lup.adapters.codex.selection.create_codex_session_factory", record
    )
    CODEX_RUNTIME.session_factory(
        SessionRequest(
            model="a-model",
            instructions="be brief",
            cwd=tmp_path,
            autonomy="accept_edits",
            tool_servers={"group": {"command": "uv", "args": ["run", "tools"]}},
        )
    )

    config = rendered[0]
    assert config.model == "a-model"
    assert config.developer_instructions == "be brief"
    assert config.sandbox == "workspace-write"
    assert config.writable_roots == [tmp_path]
    assert config.mcp_servers["group"].command == "uv"
    assert config.mcp_servers["group"].args == ["run", "tools"]


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"tools": ["Read"]},
        {"allowed_tools": ["Read"]},
        {"hooks": LupHooksConfig()},
    ],
    ids=["tools", "allowed_tools", "hooks"],
)
def test_codex_refuses_what_it_cannot_govern(
    request_kwargs: dict[str, object], tmp_path: Path
) -> None:
    """A field Codex has no words for is an error, never a silent drop."""
    with pytest.raises(ValueError, match="no session-level"):
        CODEX_RUNTIME.session_factory(
            SessionRequest(cwd=tmp_path, **request_kwargs)  # pyright: ignore[reportArgumentType]
        )


def test_codex_will_not_infer_the_directory_it_sandboxes_against() -> None:
    with pytest.raises(ValueError, match="cwd"):
        CODEX_RUNTIME.session_factory(SessionRequest(model="a-model"))


def test_codex_rejects_a_tool_group_it_cannot_launch() -> None:
    with pytest.raises(ValueError, match="subprocess"):
        codex_mcp_server("group", {"type": "sse", "url": "https://example.test"})
