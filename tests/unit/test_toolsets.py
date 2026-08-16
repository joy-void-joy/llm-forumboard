# lup: ignore[set-shape]
# Test fixtures and assertions construct these shapes deliberately.
"""The toolsets registry — single source of truth for both backend paths.

The registry exists to make tool-group drift between backends impossible.
These tests are the tripwire: the names served to subprocess backends must
match the groups the registry actually builds, and the only allowed
difference between the Claude and Codex notes groups is the deliberate
run_subagent asymmetry.
"""

from pathlib import Path

from lup.sandbox.container import Sandbox

from lup_template.agent.toolsets import (
    EXAMPLE_GROUP,
    SessionToolset,
    build_session_toolset,
    tool_group_names,
)


def build(
    base: Path,
    *,
    realtime: bool = False,
    with_sandbox: bool = False,
) -> SessionToolset:
    sandbox = None
    if with_sandbox:
        sandbox = Sandbox(session_id="toolset-test", shared_dir=base / "shared")
    return build_session_toolset(
        session_dir=base / "session",
        outputs_dir=base / "outputs",
        sandbox=sandbox,
        realtime_dir=(base / "realtime") if realtime else None,
    )


def test_served_names_match_built_groups(tmp_path: Path) -> None:
    for realtime in (False, True):
        toolset = build(
            tmp_path / str(realtime),
            realtime=realtime,
            with_sandbox=True,
        )
        built = set(toolset["groups"]) - {EXAMPLE_GROUP}
        assert set(tool_group_names(realtime=realtime)) == built


def test_session_group_requires_realtime_dir(tmp_path: Path) -> None:
    without = build(tmp_path / "without")
    with_relay = build(tmp_path / "with", realtime=True)

    assert "session" not in without["groups"]
    assert with_relay["groups"]["session"]


def test_submit_output_is_owned_by_the_turn_runtime(tmp_path: Path) -> None:
    toolset = build(tmp_path)
    note_names = {tool.name for tool in toolset["groups"]["notes"]}
    assert "submit_output" not in note_names
    assert not toolset["gate"].reflected
