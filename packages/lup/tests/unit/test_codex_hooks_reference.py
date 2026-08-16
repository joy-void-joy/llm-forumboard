"""The quarantined Codex command-hook codegen stays importable and correct.

The reference module preserves a probed wire format for a runtime that
does not currently honor it; these tests keep that preservation honest.
The generated scripts are executed for real (``python3`` on a fresh
interpreter, JSON on stdin/stdout) so a codegen edit that emits a broken
or wrongly-deciding script fails here, and the tag dispatch that would
assemble a live config is pinned including its dedup and unknown-tag
skip behavior.
"""

import json
from pathlib import Path

import sh

from lup.hooks import (
    LupHookInput,
    LupHookMatcher,
    LupHookOutput,
    LupHooksConfig,
)
from tests.unit.codex_hooks_reference import (
    CodexHookInput,
    build_hook_config_overrides,
    lup_hooks_to_codex,
)


def run_script(
    script: Path, payload: CodexHookInput
) -> dict[str, str]:  # lup: ignore[dict-str-payload] — dispatcher JSON
    output = str(sh.Command("python3")("-I", str(script), _in=json.dumps(payload)))
    return json.loads(output)


async def recording_hook(data: LupHookInput) -> LupHookOutput:
    del data
    return LupHookOutput()


def hook(tag: str, matcher: str | None = None) -> LupHookMatcher:
    return LupHookMatcher(matcher=matcher, hook=recording_hook, tag=tag)


def test_generated_permission_script_enforces_rw_and_ro_grants(
    tmp_path: Path,
) -> None:
    rw = tmp_path / "rw"
    ro = tmp_path / "ro"
    configs = lup_hooks_to_codex(
        LupHooksConfig(pre_tool_use=[hook("permission")]),
        script_dir=tmp_path / "scripts",
        rw_dirs=[rw],
        ro_dirs=[ro],
    )
    script = Path(str(configs[0]["command"]).removeprefix("python3 "))

    write_inside = run_script(
        script,
        CodexHookInput(tool_name="Write", tool_input={"file_path": str(rw / "a.txt")}),
    )
    write_readonly = run_script(
        script,
        CodexHookInput(tool_name="Write", tool_input={"file_path": str(ro / "a.txt")}),
    )
    read_readonly = run_script(
        script,
        CodexHookInput(tool_name="Read", tool_input={"file_path": str(ro / "a.txt")}),
    )
    glob_unscoped = run_script(script, CodexHookInput(tool_name="Glob", tool_input={}))

    assert write_inside == {"decision": "allow"}
    assert write_readonly["decision"] == "deny"
    assert "RW" in write_readonly["reason"]
    assert read_readonly == {"decision": "allow"}
    assert glob_unscoped["decision"] == "deny"


def test_generated_reflection_gate_script_opens_only_on_the_flag(
    tmp_path: Path,
) -> None:
    flag = tmp_path / ".reflected"
    configs = lup_hooks_to_codex(
        LupHooksConfig(pre_tool_use=[hook("reflection_gate", "StructuredOutput")]),
        script_dir=tmp_path / "scripts",
        gate_flag_path=flag,
    )
    gate = configs[0]
    assert "matcher" in gate and gate["matcher"] == "StructuredOutput"
    script = Path(str(gate["command"]).removeprefix("python3 "))

    closed = run_script(script, CodexHookInput(tool_name="StructuredOutput"))
    other_tool = run_script(script, CodexHookInput(tool_name="Read"))
    flag.write_text("", encoding="utf-8")
    opened = run_script(script, CodexHookInput(tool_name="StructuredOutput"))

    assert closed["decision"] == "deny"
    assert "mcp__notes__review" in closed["reason"]
    assert other_tool == {"decision": "allow"}
    assert opened == {"decision": "allow"}


def test_dispatch_dedupes_tags_and_skips_unknown_and_unsupported(
    tmp_path: Path,
) -> None:
    configs = lup_hooks_to_codex(
        LupHooksConfig(
            pre_tool_use=[hook("allowlist"), hook("allowlist"), hook("mystery")],
            post_tool_use=[hook("nudge"), hook("capture")],
        ),
        script_dir=tmp_path / "scripts",
        nudges={"Bash": "prefer the structured API"},
        allowed_tools=["Read", "Bash"],
    )

    assert [(config["event"], "matcher" in config) for config in configs] == [
        ("PreToolUse", False),
        ("PostToolUse", False),
    ]


def test_config_overrides_render_the_probed_toml_wire_format(tmp_path: Path) -> None:
    configs = lup_hooks_to_codex(
        LupHooksConfig(pre_tool_use=[hook("reflection_gate", "StructuredOutput")]),
        script_dir=tmp_path / "scripts",
        gate_flag_path=tmp_path / ".reflected",
    )

    overrides = build_hook_config_overrides(configs)

    assert overrides[0] == "features.codex_hooks=true"
    assert overrides[1] == 'hooks.PreToolUse[0].matcher="StructuredOutput"'
    assert overrides[2] == 'hooks.PreToolUse[0].hooks[0].type="command"'
    assert overrides[3].startswith('hooks.PreToolUse[0].hooks[0].command="python3 ')
