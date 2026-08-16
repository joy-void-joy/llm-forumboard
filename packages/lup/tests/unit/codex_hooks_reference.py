# lup: ignore[dict-get, dict-str-payload, empty-collection, set-shape]
# Test fixtures and assertions construct these shapes deliberately.
"""Lup hook policies rendered as standalone Codex command-hook scripts.

The wire-format reference, quarantined out of the shipped package: a
live probe showed config.toml command hooks never fire on the Codex
builds this project targets, so no live adapter wires this —
enforcement is the native workspace-write sandbox
(:class:`~lup.adapters.codex.runtime.CodexSessionConfig`).
Kept beside its tests so the format survives for the day the runtime
honors hooks again; do not wire it into a live adapter without
re-probing.

The emitted hook scripts stay inline as f-string templates: each bakes
call-time constants (directories, flags, tool lists) into the generated
source, so a standalone asset would be a brace-doubled ``.format``
template — not valid Python — and would review worse than the literal.
"""

import json
import logging
import sys
from pathlib import Path
from typing import Literal, TypedDict

from lup.hooks import LupHooksConfig

logger = logging.getLogger(__name__)


class CodexHookConfigRequired(TypedDict):
    """Required fields for a Codex command hook."""

    event: str
    command: str


class CodexHookConfig(CodexHookConfigRequired, total=False):
    """Configuration for a single Codex command hook."""

    matcher: str


def build_hook_config_overrides(
    hooks: list[CodexHookConfig],
) -> list[str]:
    """Build config_overrides for Codex command hooks.

    Each hook dict has: event, matcher (optional), command.
    Generates TOML-style config_overrides for the Codex hook system.
    """
    overrides: list[str] = []
    overrides.append("features.codex_hooks=true")

    event_counts: dict[str, int] = {}
    for hook in hooks:
        event = hook["event"]
        idx = event_counts.get(event, 0)
        event_counts[event] = idx + 1

        if "matcher" in hook:
            overrides.append(f'hooks.{event}[{idx}].matcher="{hook["matcher"]}"')
        overrides.append(f'hooks.{event}[{idx}].hooks[0].type="command"')
        overrides.append(f'hooks.{event}[{idx}].hooks[0].command="{hook["command"]}"')

    return overrides


class CodexHookInput(TypedDict, total=False):
    """Input JSON received by Codex hook scripts on stdin."""

    hook_event_name: str
    tool_name: str
    tool_input: dict[str, str]


class CodexHookOutput(TypedDict, total=False):
    """Output JSON emitted by Codex hook scripts to stdout."""

    decision: Literal["allow", "deny", "block"]
    reason: str
    systemMessage: str


def format_codex_hook_output(
    decision: Literal["allow", "deny", "block"],
    reason: str = "",
) -> CodexHookOutput:
    """Format a hook decision as Codex-compatible JSON."""
    output = CodexHookOutput(decision=decision)
    if reason:
        output["reason"] = reason
    return output


def read_hook_input() -> CodexHookInput:
    """Read hook input JSON from stdin (used by hook scripts)."""
    raw = sys.stdin.read()
    return json.loads(raw)


def write_hook_output(output: CodexHookOutput) -> None:
    """Write hook output JSON to stdout (used by hook scripts)."""
    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()


def build_permission_hooks(
    rw_dirs: list[Path],
    ro_dirs: list[Path],
    script_dir: Path,
) -> list[CodexHookConfig]:
    """Generate Codex hook configs for directory-based permission control.

    Creates a PreToolUse hook config that runs a permission check script.
    The script path must be written to disk separately (see
    write_permission_hook_script).

    Args:
        rw_dirs: Directories where Write/Edit/Read are allowed.
        ro_dirs: Additional directories where only Read is allowed.
        script_dir: Directory where hook scripts will be written.

    Returns:
        List of CodexHookConfig entries for config_overrides.
    """
    script_path = script_dir / "codex_permission_hook.py"
    write_permission_hook_script(script_path, rw_dirs, ro_dirs)

    return [
        CodexHookConfig(
            event="PreToolUse",
            command=f"python3 {script_path}",
        ),
    ]


def write_permission_hook_script(
    script_path: Path,
    rw_dirs: list[Path],
    ro_dirs: list[Path],
) -> None:
    """Write a standalone permission hook script to disk.

    The script reads CodexHookInput from stdin, checks directory
    permissions, and writes CodexHookOutput to stdout.
    """
    rw_list = json.dumps([str(d) for d in rw_dirs])
    ro_list = json.dumps([str(d) for d in ro_dirs])

    script = f'''\
"""Auto-generated Codex permission hook script."""
import json
import sys
from pathlib import Path

RW_DIRS = [Path(p) for p in {rw_list}]
RO_DIRS = [Path(p) for p in {ro_list}]
ALL_READABLE = RW_DIRS + RO_DIRS


def path_is_under(file_path: str, dirs: list[Path]) -> bool:
    p = Path(file_path).resolve()
    return any(p == d or d in p.parents for d in dirs)


def check_permission(hook_input: dict) -> dict:
    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {{}})

    match tool_name:
        case "Write" | "Edit":
            file_path = tool_input.get("file_path", "")
            if not file_path:
                return {{"decision": "allow"}}
            if path_is_under(file_path, RW_DIRS):
                return {{"decision": "allow"}}
            return {{"decision": "deny", "reason": f"{{tool_name}} denied outside RW dirs"}}

        case "Read":
            file_path = tool_input.get("file_path", "")
            if not file_path:
                return {{"decision": "allow"}}
            if path_is_under(file_path, ALL_READABLE):
                return {{"decision": "allow"}}
            return {{"decision": "deny", "reason": "Read denied outside allowed dirs"}}

        case "Glob" | "Grep":
            file_path = tool_input.get("path", "")
            if not file_path:
                return {{"decision": "deny", "reason": f"Path required for {{tool_name}}"}}
            if path_is_under(file_path, ALL_READABLE):
                return {{"decision": "allow"}}
            return {{"decision": "deny", "reason": f"{{tool_name}} denied outside allowed dirs"}}

        case _:
            return {{"decision": "allow"}}


raw = sys.stdin.read()
hook_input = json.loads(raw)
result = check_permission(hook_input)
sys.stdout.write(json.dumps(result))
'''
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8")


def build_reflection_gate_hook(
    gate_flag_path: Path,
    gated_tool: str,
    reflection_tool_name: str,
    script_dir: Path,
) -> list[CodexHookConfig]:
    """Generate a Codex hook config for the reflection gate.

    The gate blocks gated_tool until a flag file exists at
    gate_flag_path (set by the reflect tool's MCP handler).

    Args:
        gate_flag_path: Path to the flag file that indicates reflection occurred.
        gated_tool: Tool name to block (e.g., "StructuredOutput").
        reflection_tool_name: Name shown in the denial message.
        script_dir: Directory where hook scripts will be written.

    Returns:
        List of CodexHookConfig entries for config_overrides.
    """
    script_path = script_dir / "codex_reflection_gate_hook.py"
    write_reflection_gate_script(
        script_path, gate_flag_path, gated_tool, reflection_tool_name
    )

    return [
        CodexHookConfig(
            event="PreToolUse",
            matcher=gated_tool,
            command=f"python3 {script_path}",
        ),
    ]


def write_reflection_gate_script(
    script_path: Path,
    gate_flag_path: Path,
    gated_tool: str,
    reflection_tool_name: str,
) -> None:
    """Write a standalone reflection gate hook script to disk."""
    script = f'''\
"""Auto-generated Codex reflection gate hook script."""
import json
import sys
from pathlib import Path

GATE_FLAG = Path("{gate_flag_path}")
GATED_TOOL = "{gated_tool}"
REFLECTION_TOOL = "{reflection_tool_name}"

raw = sys.stdin.read()
hook_input = json.loads(raw)
tool_name = hook_input.get("tool_name", "")

if tool_name == GATED_TOOL and not GATE_FLAG.exists():
    result = {{
        "decision": "deny",
        "reason": f"You must call {{REFLECTION_TOOL}}() before {{GATED_TOOL}}. Reflect first.",
    }}
else:
    result = {{"decision": "allow"}}

sys.stdout.write(json.dumps(result))
'''
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8")


def build_tool_allowlist_hook(
    allowed_tools: list[str],
    script_dir: Path,
) -> list[CodexHookConfig]:
    """Generate a Codex hook config that restricts the agent to allowed tools.

    Equivalent to Claude's :func:`~lup.hooks.create_tool_allowlist_hook`.

    Args:
        allowed_tools: Tool names the agent is allowed to use.
        script_dir: Directory where the hook script will be written.

    Returns:
        List of CodexHookConfig entries for config_overrides.
    """
    script_path = script_dir / "codex_tool_allowlist_hook.py"
    write_tool_allowlist_script(script_path, allowed_tools)

    return [
        CodexHookConfig(
            event="PreToolUse",
            command=f"python3 {script_path}",
        ),
    ]


def write_tool_allowlist_script(
    script_path: Path,
    allowed_tools: list[str],
) -> None:
    """Write a standalone tool allowlist hook script to disk."""
    tools_json = json.dumps(allowed_tools)

    script = f'''\
"""Auto-generated Codex tool allowlist hook script."""
import json
import sys

ALLOWED_TOOLS = set({tools_json})

raw = sys.stdin.read()
hook_input = json.loads(raw)
tool_name = hook_input.get("tool_name", "")

if tool_name in ALLOWED_TOOLS:
    result = {{"decision": "allow"}}
else:
    result = {{"decision": "deny", "reason": f"Tool '{{tool_name}}' not in allowed list."}}

sys.stdout.write(json.dumps(result))
'''
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8")


def build_nudge_hook(
    nudges: dict[str, str],
    script_dir: Path,
) -> list[CodexHookConfig]:
    """Generate a Codex PostToolUse hook that nudges the agent toward alternatives.

    Equivalent to Claude's :func:`~lup.hooks.create_nudge_hook`, but
    simplified: each nudge is a static message string rather than a callable,
    since Codex hooks are external scripts without access to in-process state.

    Args:
        nudges: Mapping of tool_name to nudge message. When the tool runs,
            the message is injected as a systemMessage.
        script_dir: Directory where the hook script will be written.

    Returns:
        List of CodexHookConfig entries for config_overrides.
    """
    script_path = script_dir / "codex_nudge_hook.py"
    write_nudge_script(script_path, nudges)

    return [
        CodexHookConfig(
            event="PostToolUse",
            command=f"python3 {script_path}",
        ),
    ]


def write_nudge_script(
    script_path: Path,
    nudges: dict[str, str],
) -> None:
    """Write a standalone nudge hook script to disk."""
    nudges_json = json.dumps(nudges)

    script = f'''\
"""Auto-generated Codex nudge hook script."""
import json
import sys

NUDGES = {nudges_json}

raw = sys.stdin.read()
hook_input = json.loads(raw)
tool_name = hook_input.get("tool_name", "")

nudge_message = NUDGES.get(tool_name)
if nudge_message:
    result = {{"systemMessage": nudge_message}}
else:
    result = {{}}

sys.stdout.write(json.dumps(result))
'''
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8")


def lup_hooks_to_codex(
    hooks: LupHooksConfig,
    script_dir: Path,
    rw_dirs: list[Path] | None = None,
    ro_dirs: list[Path] | None = None,
    gate_flag_path: Path | None = None,
    nudges: dict[str, str] | None = None,
    allowed_tools: list[str] | None = None,
) -> list[CodexHookConfig]:
    """Convert SDK-agnostic LupHooksConfig to Codex hook configs.

    Dispatches on each ``LupHookMatcher.tag`` to generate the correct
    Codex hook script. Unrecognized tags are logged and skipped rather
    than silently dropped.

    Quarantined with the rest of the hook codegen (see the module
    docstring): config.toml command hooks never fire on current Codex
    builds, so do not wire this into a live adapter without re-verifying
    that the runtime honors the generated hooks.

    Args:
        hooks: SDK-agnostic hook configuration.
        script_dir: Directory to write hook scripts.
        rw_dirs: Read-write directories (for permission hooks).
        ro_dirs: Read-only directories (for permission hooks).
        gate_flag_path: Path for reflection gate flag file.
        nudges: Static nudge messages keyed by tool name (for PostToolUse hooks).
        allowed_tools: Tool allowlist (for PreToolUse allowlist hooks).

    Returns:
        List of CodexHookConfig entries for config_overrides.
    """
    configs: list[CodexHookConfig] = []
    seen_tags: set[str] = set()

    for matchers in hooks.by_event().values():
        for matcher in matchers:
            tag = matcher.tag or ""
            if tag in seen_tags:
                continue

            match tag:
                case "permission":
                    if rw_dirs is not None:
                        configs.extend(
                            build_permission_hooks(
                                rw_dirs=rw_dirs,
                                ro_dirs=ro_dirs or [],
                                script_dir=script_dir,
                            )
                        )
                        seen_tags.add(tag)
                    else:
                        logger.warning("permission hook requires rw_dirs")

                case "reflection_gate":
                    if gate_flag_path and matcher.matcher:
                        configs.extend(
                            build_reflection_gate_hook(
                                gate_flag_path=gate_flag_path,
                                gated_tool=matcher.matcher,
                                reflection_tool_name="mcp__notes__review",
                                script_dir=script_dir,
                            )
                        )
                        seen_tags.add(tag)
                    else:
                        logger.warning(
                            "reflection_gate hook requires gate_flag_path and matcher"
                        )

                case "allowlist":
                    if allowed_tools:
                        configs.extend(
                            build_tool_allowlist_hook(
                                allowed_tools=allowed_tools,
                                script_dir=script_dir,
                            )
                        )
                        seen_tags.add(tag)
                    else:
                        logger.warning("allowlist hook requires allowed_tools")

                case "nudge":
                    if nudges:
                        configs.extend(
                            build_nudge_hook(
                                nudges=nudges,
                                script_dir=script_dir,
                            )
                        )
                        seen_tags.add(tag)
                    else:
                        logger.warning("nudge hook requires nudges dict")

                case "capture":
                    logger.info(
                        "capture hook has no Codex equivalent (in-process only)"
                    )

                case _:
                    if tag:
                        logger.warning("Unknown hook tag %r — skipping", tag)

    return configs
