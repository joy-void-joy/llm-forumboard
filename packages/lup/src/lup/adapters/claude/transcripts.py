"""Where Claude Code writes its session transcripts, and what its words mean."""

from pathlib import Path

from lup.adapters.claude.config_home import default_config_home
from lup.telemetry.journal import ObservableEventKind
from lup.telemetry.native import (
    NativeSemanticBlock,
    NativeTranscripts,
    blocks_by_type,
    first_string,
)
from lup.types import JsonObject

# lup: ignore[constant-declaration] — Claude Code's own wire spellings, which
# it chooses and this only reads
CLAUDE_BLOCK_SPELLINGS: dict[str, ObservableEventKind] = {
    "thinking": "reasoning",
    "tool_use": "tool_call",
    "tool_result": "tool_result",
    "usage": "usage",
}

# lup: ignore[constant-declaration] — the directory Claude Code itself writes to
CLAUDE_SESSIONS_DIR = "projects"
"""Claude Code files a session under the project it was started in."""


class ClaudeTranscripts(NativeTranscripts):
    """Read Claude Code's persisted session records."""

    def __init__(self, config_home: Path | None = None) -> None:
        self.config_home = config_home or default_config_home()

    def roots(self) -> list[Path]:
        return [self.config_home / CLAUDE_SESSIONS_DIR]

    def origin(self, record: JsonObject) -> Path | None:
        found = first_string(record, "cwd")
        return Path(found) if found is not None else None

    def semantic_blocks(self, record: JsonObject) -> list[NativeSemanticBlock]:
        return blocks_by_type(record, CLAUDE_BLOCK_SPELLINGS)
