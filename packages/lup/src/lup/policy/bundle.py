"""Assembly for generated dispatchers: kernel source plus policy data rows.

Generated native plugins must decide without lup installed, so the adapters'
hook renderers call this module to read :mod:`lup.policy.kernel` verbatim and
to erase validated application inputs — hook URL scopes, protected roots, the
shell vocabulary, the canonical anti-pattern set — into primitive rows
rendered as one generated data file per plugin. Each row list is declared
against the shipped ``kernel.rows`` shapes, so a generated runtime type-checks
as one unit against the kernel beside it. No decision logic lives here; the
kernel decides.
"""

import json
import urllib.parse
from pathlib import Path

from pydantic import BaseModel

from lup.codescan.antipatterns import AntiPatternSet
from lup.harness.banner import REGENERATE_COMMAND, GeneratedBanner
from lup.policy.grants import ALLOWANCE_GRANTS_ENV, known_allowances
from lup.policy.identity import AGENT_IDENTITY_ENV
import lup.policy.kernel as kernel
from lup.policy.kernel.rows import (
    AntiPatternRow,
    PathRoleRow,
    PathRuleRow,
    RefusedToolRow,
    RunnerTargetRow,
    ShellRuleRow,
    UrlScopeRow,
)
from lup.policy.refused_tools import RefusedTool, erase_refused_tools
from lup.policy.shell_rules import (
    RunnerTargetRule,
    ShellCommandRule,
    erase_runner_targets,
    erase_shell_rules,
)
from lup.policy.rules import antipattern_row, human_owned_path_rule, path_rule_row


class KernelModule(BaseModel, frozen=True):
    """One hermetic kernel source file copied into a generated runtime."""

    name: str
    source: str


def policy_kernel_modules() -> list[KernelModule]:
    """Read the canonical kernel package verbatim for runtime assembly.

    The package's relative imports resolve the same beneath ``runtime/`` as
    they do in lup, so every module ships byte for byte.
    """
    directory = Path(kernel.__file__).parent
    return [
        KernelModule(name=path.name, source=path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.py"))
    ]


def bundled_antipattern_rows(
    rules: AntiPatternSet | None = None,
) -> dict[str, list[AntiPatternRow]]:
    """Compile primitive runtime rows directly from canonical rule objects."""
    declared = rules or AntiPatternSet()
    python_rows = [antipattern_row(rule) for rule in declared.python]
    typescript_rows = [antipattern_row(rule) for rule in declared.typescript]
    return {
        ".py": python_rows,
        ".pyi": python_rows,
        ".ts": typescript_rows,
        ".tsx": typescript_rows,
        ".js": typescript_rows,
        ".jsx": typescript_rows,
        ".vue": typescript_rows,
        ".svelte": typescript_rows,
    }


def runtime_url_scope(
    origin: str,
    path_prefix: str,
    reason: str = "",
    include_subdomains: bool = False,
    any_port: bool = False,
) -> UrlScopeRow:
    """Normalize one validated hook scope into a primitive runtime row."""
    parsed = urllib.parse.urlsplit(origin)
    if parsed.hostname is None:
        raise ValueError("validated hook URL scope has no hostname")
    return UrlScopeRow(
        scheme=parsed.scheme,
        host=parsed.hostname,
        port=parsed.port,
        path_prefix=path_prefix,
        reason=reason,
        include_subdomains=include_subdomains,
        any_port=any_port,
    )


def runtime_path_rule(root: str) -> PathRuleRow:
    """Compile one application root into its primitive protected-path row."""
    match root:
        case "tmp":
            return PathRuleRow(
                kind="contains_part",
                value=root,
                reason="scratch path requires approval",
                allow_autonomous=False,
            )
        case _:
            return PathRuleRow(
                kind="subtree",
                value=root,
                reason="protected path requires approval",
                allow_autonomous=True,
            )


def runtime_path_rules(
    protected_roots: list[str], human_owned_files: list[str]
) -> list[PathRuleRow]:
    """Compile application roots plus invariant edit guardrails."""
    return [
        *[runtime_path_rule(root) for root in protected_roots],
        *[path_rule_row(human_owned_path_rule(path)) for path in human_owned_files],
        PathRuleRow(
            kind="name_prefix",
            value=".env",
            reason="protected path requires approval",
            allow_autonomous=False,
        ),
        PathRuleRow(
            kind="new_devtools",
            value="src",
            reason="new devtools module requires approval",
            allow_autonomous=False,
        ),
    ]


def dict_rows_literal(rows: list[list[str]]) -> str:
    """Render already-escaped ``"key": value`` rows in Ruff-stable form."""
    if not rows:
        return "[]"
    blocks = [
        "    {\n" + "".join(f"        {entry},\n" for entry in row) + "    },"
        for row in rows
    ]
    return "[\n" + "\n".join(blocks) + "\n]"


def url_scope_rows_literal(rows: list[UrlScopeRow]) -> str:
    """Render normalized URL scopes as primitive runtime rows."""
    return dict_rows_literal(
        [
            [
                f'"scheme": {json.dumps(row["scheme"])}',
                f'"host": {json.dumps(row["host"])}',
                f'"port": {"None" if row["port"] is None else row["port"]}',
                f'"path_prefix": {json.dumps(row["path_prefix"])}',
                f'"reason": {json.dumps(row["reason"])}',
                f'"include_subdomains": {row["include_subdomains"]}',
                f'"any_port": {row["any_port"]}',
            ]
            for row in rows
        ]
    )


def path_rule_rows_literal(rows: list[PathRuleRow]) -> str:
    """Render protected-path rows as primitive runtime rows."""
    return dict_rows_literal(
        [
            [
                f'"kind": {json.dumps(row["kind"])}',
                f'"value": {json.dumps(row["value"])}',
                f'"reason": {json.dumps(row["reason"])}',
                f'"allow_autonomous": {row["allow_autonomous"]}',
            ]
            for row in rows
        ]
    )


def antipattern_rows_literal(rows: dict[str, list[AntiPatternRow]]) -> str:
    """Render suffix-keyed anti-pattern rows in Ruff-stable form."""
    lines = ["{"]
    for suffix, patterns in sorted(rows.items()):
        lines.append(f"    {json.dumps(suffix)}: [")
        for row in patterns:
            # Rendered from the row's own keys rather than a list of them: a
            # field added to AntiPatternRow reaches the hermetic runtime by
            # construction, instead of being dropped until someone notices.
            fields = "\n".join(
                f"            {json.dumps(key)}: {json.dumps(value)},"
                for key, value in row.items()
            )
            lines.append(f"        {{\n{fields}\n        }},")
        lines.append("    ],")
    lines.append("}")
    return "\n".join(lines)


def path_role_rows_literal(rows: list[PathRoleRow]) -> str:
    """Render declared path roles as primitive runtime rows."""
    return dict_rows_literal(
        [
            [
                f'"root": {json.dumps(row["root"])}',
                f'"role": {json.dumps(row["role"])}',
            ]
            for row in rows
        ]
    )


def refused_tool_rows_literal(rows: list[RefusedToolRow]) -> str:
    """Render declared tool refusals as primitive runtime rows."""
    return dict_rows_literal(
        [
            [
                f'"tool": {json.dumps(row["tool"])}',
                f'"specifier": {json.dumps(row["specifier"])}',
                f'"reason": {json.dumps(row["reason"])}',
            ]
            for row in rows
        ]
    )


def runner_target_rows_literal(rows: list[RunnerTargetRow]) -> str:
    """Render the blessed runner targets as primitive runtime rows."""
    return dict_rows_literal(
        [
            [
                f'"name": {json.dumps(row["name"])}',
                f'"sandbox": {json.dumps(row["sandbox"])}',
            ]
            for row in rows
        ]
    )


def string_rows_literal(rows: list[str]) -> str:
    """Render a sequence of generated string identities."""
    if not rows:
        return "[]"
    return "[\n" + "".join(f"    {json.dumps(row)},\n" for row in rows) + "]"


def string_matrix_literal(rows: list[list[str]]) -> str:
    """Render a sequence of generated argument vectors."""
    if not rows:
        return "[]"
    return "[\n" + "".join(f"    {json.dumps(row)},\n" for row in rows) + "]"


def shell_rule_rows_literal(rows: list[ShellRuleRow]) -> str:
    """Render erased shell rules as Ruff-stable dict literals."""
    if not rows:
        return "[]"
    lines = ["["]
    for row in rows:
        lines.append("    {")
        lines.append(f'        "command": {json.dumps(row["command"])},')
        lines.append(f'        "subcommand": {json.dumps(row["subcommand"])},')
        lines.append(f'        "operation": {json.dumps(row["operation"])},')
        lines.append(f'        "effect": {json.dumps(row["effect"])},')
        lines.append(f'        "effect_source": {json.dumps(row["effect_source"])},')
        for name, flags in (
            ("ask_flags", row["ask_flags"]),
            ("allow_flags", row["allow_flags"]),
            ("read_verbs", row["read_verbs"]),
            ("value_flags", row["value_flags"]),
        ):
            if flags:
                lines.append(f'        "{name}": [')
                lines.extend(f"            {json.dumps(flag)}," for flag in flags)
                lines.append("        ],")
            else:
                lines.append(f'        "{name}": [],')
        lines.append(f'        "sandbox": {json.dumps(row["sandbox"])},')
        lines.append(f'        "sandbox_source": {json.dumps(row["sandbox_source"])},')
        lines.append(f'        "reason": {json.dumps(row["reason"])},')
        lines.append("    },")
    lines.append("]")
    return "\n".join(lines)


POLICY_DATA_BANNER = GeneratedBanner(source=__name__, command=REGENERATE_COMMAND)
"""Provenance every adapter's rendered policy-data module opens with."""


def render_policy_data(
    *,
    allowed_fetch_scopes: list[UrlScopeRow],
    denied_fetch_scopes: list[UrlScopeRow],
    protected_roots: list[str],
    human_owned_files: list[str],
    autonomous_agent_identities: list[str],
    path_roles: list[PathRoleRow],
    shell_rules: list[ShellCommandRule],
    refused_tools: list[RefusedTool],
    recoverable_target_limit: int,
    runner_targets: list[RunnerTargetRule],
    sandbox_excluded_commands: list[str],
    auto_escape_prefixes: list[list[str]],
    diagnostics_command: list[str],
    rules: AntiPatternSet | None = None,
) -> str:
    """Render one plugin's canonical policy rows without executable logic.

    ``rules`` is the table compiled for the runtime this plugin belongs to, so
    a rule whose message names a native tool ships each tree the words that
    tree can act on. Omitting it renders the runtime-neutral table.
    """
    body = "\n\n".join(
        [
            "ALLOWED_FETCH_SCOPES: list[UrlScopeRow] = "
            + url_scope_rows_literal(allowed_fetch_scopes),
            "DENIED_FETCH_SCOPES: list[UrlScopeRow] = "
            + url_scope_rows_literal(denied_fetch_scopes),
            "PATH_RULES: list[PathRuleRow] = "
            + path_rule_rows_literal(
                runtime_path_rules(protected_roots, human_owned_files)
            ),
            "ANTI_PATTERN_ROWS: dict[str, list[AntiPatternRow]] = "
            + antipattern_rows_literal(bundled_antipattern_rows(rules)),
            "PATH_ROLES: list[PathRoleRow] = " + path_role_rows_literal(path_roles),
            "SHELL_RULES: list[ShellRuleRow] = "
            + shell_rule_rows_literal(erase_shell_rules(shell_rules)),
            "REFUSED_TOOLS: list[RefusedToolRow] = "
            + refused_tool_rows_literal(erase_refused_tools(refused_tools)),
            "AUTONOMOUS_AGENT_IDENTITIES: list[str] = "
            + string_rows_literal(autonomous_agent_identities),
            "AGENT_IDENTITY_ENV = " + json.dumps(AGENT_IDENTITY_ENV),
            "ALLOWANCE_GRANTS_ENV = " + json.dumps(ALLOWANCE_GRANTS_ENV),
            "KNOWN_ALLOWANCES: list[str] = " + string_rows_literal(known_allowances()),
            "MAXIMUM_ADDED_LINES = 3",
            "RECOVERABLE_TARGET_LIMIT = " + json.dumps(recoverable_target_limit),
            "RUNNER_TARGETS: list[RunnerTargetRow] = "
            + runner_target_rows_literal(erase_runner_targets(runner_targets)),
            "SANDBOX_EXCLUDED_COMMANDS: list[str] = "
            + string_rows_literal(sandbox_excluded_commands),
            "AUTO_ESCAPE_PREFIXES: list[list[str]] = "
            + string_matrix_literal(auto_escape_prefixes),
            "DIAGNOSTICS_COMMAND: list[str] = "
            + string_rows_literal(diagnostics_command),
        ]
    )
    return (
        '"""Generated application-owned policy data."""\n\n'
        "from kernel.rows import (\n"
        "    AntiPatternRow,\n"
        "    PathRoleRow,\n"
        "    PathRuleRow,\n"
        "    RefusedToolRow,\n"
        "    RunnerTargetRow,\n"
        "    ShellRuleRow,\n"
        "    UrlScopeRow,\n"
        ")"
        "\n\n\n" + body + "\n"
    )
