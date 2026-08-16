"""What this repository grants, refuses, and enables for itself.

The rendering is the library's (:mod:`lup.devtools.harness.settings`), which
derives the marketplace key, the plugin enablement, the served-tool grants,
and the sandbox boundaries from the declaration itself. Named here is only
the half no derivation can reach: which vendor plugins this repository turns
on, and which tools it grants or refuses by judgement.
"""

from lup.devtools.harness.settings import Settings, project_settings as render
from lup.harness.models import Plugin
from lup.types import JsonObject

DECLARED = Settings(
    base={
        "coauthorship": False,
        "env": {"ENABLE_TOOL_SEARCH": "false"},
        "fileSuggestion": {
            "command": ".claude/plugins/lup/scripts/file_suggest.sh",
            "type": "command",
        },
    },
    official_plugins={
        "agent-sdk-dev@claude-plugins-official": True,
        "claude-md-management@claude-plugins-official": True,
        "github@claude-plugins-official": True,
    },
    allowed=[
        "WebSearch",
        "Skill(lup:hooks)",
        # The guidance sends every change through a worktree, so entering and
        # leaving one is the first thing a session does and the last. Asking
        # about the step the workflow mandates is asking whether to follow it.
        # `EnterWorktree` moves this session into a tree `dev worktree create`
        # already made. `ExitWorktree` can remove one, and what holds it back
        # is the tool's own refusal on uncommitted files and unmerged commits
        # — which its caller overrides by setting `discard_changes`, so the
        # grant is trust in that refusal rather than a second gate behind it.
        "EnterWorktree",
        "ExitWorktree",
        "Read(./.claude/settings.json.local*)",
        "Read(./sync.json.local)",
        "Read(./downstream.json.local)",
    ],
    denied=[
        "Read(./**/.env*.local)",
        "Read(./**/.env.local)",
        "Read(./**/secrets*.local)",
        "Read(./**/*.secret.local)",
    ],
)


def project_settings(plugin: Plugin | None) -> JsonObject:
    """Render this repository's settings artifact from its declaration."""
    return render(DECLARED, plugin)
