"""Where Codex writes its session transcripts, and what its words mean."""

from pathlib import Path

from lup.adapters.codex.home import DEFAULT_ACCOUNT_HOME
from lup.telemetry.journal import ObservableEventKind
from lup.telemetry.native import (
    NativeSemanticBlock,
    NativeTranscripts,
    blocks_by_type,
    first_string,
)
from lup.types import JsonObject

# lup: ignore[constant-declaration] — Codex's own wire spellings, which it
# chooses and this only reads. Deliberately not merged with Claude Code's:
# the two share words that do not share meanings.
CODEX_BLOCK_SPELLINGS: dict[str, ObservableEventKind] = {
    "reasoning": "reasoning",
    "reasoning_content": "reasoning",
    "function_call": "tool_call",
    "function_call_output": "tool_result",
    "usage": "usage",
}

# lup: ignore[constant-declaration] — the directory Codex itself writes to
CODEX_SESSIONS_DIR = "sessions"
"""Codex files every session under one directory in its home."""


class CodexTranscripts(NativeTranscripts):
    """Read Codex's persisted session records."""

    def __init__(self, codex_home: Path | None = None) -> None:
        self.codex_home = codex_home or DEFAULT_ACCOUNT_HOME

    def roots(self) -> list[Path]:
        return [self.codex_home / CODEX_SESSIONS_DIR]

    def origin(self, record: JsonObject) -> Path | None:
        # Carried inside the opening `session_meta` payload rather than at the
        # top level, which the descent reaches without naming the envelope.
        found = first_string(record, "cwd")
        return Path(found) if found is not None else None

    def semantic_blocks(self, record: JsonObject) -> list[NativeSemanticBlock]:
        return blocks_by_type(record, CODEX_BLOCK_SPELLINGS)
