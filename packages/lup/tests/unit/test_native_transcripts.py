"""Native transcript ingestion: scope, cursors, and per-runtime vocabulary."""

from pathlib import Path

from lup.adapters.claude.transcripts import ClaudeTranscripts
from lup.adapters.codex.transcripts import CodexTranscripts
from lup.telemetry.journal import (
    TraceActor,
    TraceContext,
    TraceJournal,
    read_observable_events,
)
from lup.telemetry.native import NativeTranscriptWatcher
from lup.types import JsonObject


def journal_at(path: Path) -> TraceJournal:
    return TraceJournal(
        path,
        TraceContext.root("run-1", TraceActor(kind="harness", name="native")),
    )


def test_each_runtime_reads_a_shared_word_in_its_own_vocabulary() -> None:
    # The reason the two tables are not merged: `reasoning` is a block Codex
    # emits, and a merged table would read it the same way in either record.
    reasoning: JsonObject = {"type": "reasoning", "text": "considering"}

    assert CodexTranscripts().semantic_blocks(reasoning) != []
    assert ClaudeTranscripts().semantic_blocks(reasoning) == []


def test_each_runtime_recognizes_its_own_tool_call_spelling() -> None:
    claude_call: JsonObject = {"type": "tool_use", "name": "Read"}
    codex_call: JsonObject = {"type": "function_call", "name": "read"}

    assert [
        block.kind for block in ClaudeTranscripts().semantic_blocks(claude_call)
    ] == ["tool_call"]
    assert [block.kind for block in CodexTranscripts().semantic_blocks(codex_call)] == [
        "tool_call"
    ]
    assert ClaudeTranscripts().semantic_blocks(codex_call) == []


def test_a_nested_block_is_found_without_naming_its_envelope() -> None:
    record: JsonObject = {
        "payload": {"content": [{"type": "tool_use", "name": "Read"}]}
    }

    assert [block.kind for block in ClaudeTranscripts().semantic_blocks(record)] == [
        "tool_call"
    ]


def test_codex_finds_a_cwd_carried_inside_session_meta() -> None:
    record: JsonObject = {"type": "session_meta", "payload": {"cwd": "/work/project"}}

    assert CodexTranscripts().origin(record) == Path("/work/project")


def test_a_record_naming_no_directory_has_no_origin() -> None:
    record: JsonObject = {"type": "user"}

    assert ClaudeTranscripts().origin(record) is None


def test_only_bytes_written_after_the_snapshot_are_ingested(tmp_path: Path) -> None:
    sessions = tmp_path / "home" / "projects"
    sessions.mkdir(parents=True)
    transcript = sessions / "session.jsonl"
    transcript.write_text('{"type":"user","cwd":"/work"}\n', encoding="utf-8")

    watcher = NativeTranscriptWatcher(
        ClaudeTranscripts(tmp_path / "home"), journal_at(tmp_path / "journal.jsonl")
    )
    watcher.snapshot()
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write('{"type":"tool_use","name":"Read","cwd":"/work"}\n')
    watcher.scan()

    kinds = [event.kind for event in read_observable_events(tmp_path / "journal.jsonl")]
    assert kinds == ["message", "tool_call"]


def test_a_transcript_outside_the_scope_is_not_mirrored(tmp_path: Path) -> None:
    # The roots hold every project's sessions, so an unscoped watcher would
    # pull a concurrent unrelated session into this project's record.
    sessions = tmp_path / "home" / "projects"
    sessions.mkdir(parents=True)
    (sessions / "elsewhere.jsonl").write_text(
        '{"type":"tool_use","name":"Read","cwd":"/other/project"}\n', encoding="utf-8"
    )

    watcher = NativeTranscriptWatcher(
        ClaudeTranscripts(tmp_path / "home"),
        journal_at(tmp_path / "journal.jsonl"),
        scope=Path("/work"),
    )
    watcher.scan()

    assert read_observable_events(tmp_path / "journal.jsonl") == []


def test_a_partial_line_waits_for_the_rest_but_lands_on_the_final_scan(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "home" / "projects"
    sessions.mkdir(parents=True)
    (sessions / "session.jsonl").write_text('{"type":"user"}', encoding="utf-8")
    journal_path = tmp_path / "journal.jsonl"

    watcher = NativeTranscriptWatcher(
        ClaudeTranscripts(tmp_path / "home"), journal_at(journal_path)
    )
    watcher.scan()
    assert read_observable_events(journal_path) == []

    watcher.scan(final=True)
    assert len(read_observable_events(journal_path)) == 1
