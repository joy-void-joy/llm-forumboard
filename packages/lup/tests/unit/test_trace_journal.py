"""The observable journal: redaction, the hash chain, and delegated spans."""

from pathlib import Path

from lup.runtime.models import (
    BlockCompletedEvent,
    MessageCompletedEvent,
    SessionId,
    TurnCompletedEvent,
    TurnId,
    TurnIdentifiers,
    TurnMessage,
    TurnTextBlock,
    TurnToolCallBlock,
)
from lup.telemetry.journal import (
    ArgvRedaction,
    KeyRedaction,
    Redactions,
    TraceActor,
    TraceContext,
    TraceJournal,
    TurnRecorder,
    read_observable_events,
    verify_event_chain,
)
from lup.types import JsonObject

IDENTIFIERS = TurnIdentifiers(
    session=SessionId(value="session-1"), turn=TurnId(value="turn-1")
)


def journal_at(path: Path) -> TraceJournal:
    return TraceJournal(
        path,
        TraceContext.root("run-1", TraceActor(kind="orchestrator", name="main")),
    )


def test_a_key_that_names_a_secret_loses_its_value_at_any_depth() -> None:
    redaction = KeyRedaction()
    payload: JsonObject = {
        "headers": {"Authorization": "Bearer sk-abc", "Accept": "application/json"},
        "nested": [{"api_key": "sk-xyz"}, {"harmless": "kept"}],
    }

    assert redaction.apply(payload) == {
        "headers": {"Authorization": "[REDACTED]", "Accept": "application/json"},
        "nested": [{"api_key": "[REDACTED]"}, {"harmless": "kept"}],
    }


def test_a_secret_under_an_innocuous_key_is_not_caught_by_key_redaction() -> None:
    # Pinned because it is the known limit of matching on names, and the
    # reason another rule composes in rather than this one growing.
    payload: JsonObject = {"note": "the token is sk-abc123"}
    assert KeyRedaction().apply(payload) == payload


def test_argv_redaction_covers_both_spellings_of_a_flag() -> None:
    argv = ["run", "--api-key=sk-abc", "--password", "hunter2", "--verbose"]

    assert ArgvRedaction().arguments(argv) == [
        "run",
        "--api-key=[REDACTED]",
        "--password",
        "[REDACTED]",
        "--verbose",
    ]


def test_argv_redaction_leaves_a_list_that_is_not_a_command_line_alone() -> None:
    assert ArgvRedaction().apply(["alpha", "beta"]) == ["alpha", "beta"]


def test_redactions_apply_each_rule_to_what_it_understands() -> None:
    # A caller who knows it holds a command line composes the argv rule in.
    # Each rule passes over what is not its business, so order is safe.
    composed = Redactions(KeyRedaction(), ArgvRedaction())

    assert composed.apply(["run", "--api-key", "sk-abc"]) == [
        "run",
        "--api-key",
        "[REDACTED]",
    ]
    assert composed.apply({"api_key": "sk-abc"}) == {"api_key": "[REDACTED]"}


def test_the_chain_verifies_and_a_tampered_payload_breaks_it(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = journal_at(path)
    journal.emit("run_start")
    journal.emit("message", {"text": "hello"})

    events = read_observable_events(path)
    assert [event.sequence for event in events] == [0, 1]
    assert verify_event_chain(events)

    tampered = events[1].model_copy(update={"payload": {"text": "goodbye"}})
    assert not verify_event_chain([events[0], tampered])


def test_a_secret_never_reaches_the_file(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    journal_at(path).emit("tool_call", {"api_key": "sk-should-not-persist"})

    assert "sk-should-not-persist" not in path.read_text(encoding="utf-8")


def test_a_child_span_shares_the_chain_and_names_its_parent(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = journal_at(path)
    journal.emit("run_start")
    child = journal.child(TraceActor(kind="tool", name="lup.search"))
    child.emit("tool_call")

    events = read_observable_events(path)
    assert verify_event_chain(events)
    assert events[1].parent_span_id == events[0].span_id
    assert events[1].tool_name == "search"


def test_a_delegated_span_is_written_as_it_streams(tmp_path: Path) -> None:
    # The delegating call streams past first, so the role is known before the
    # delegated messages arrive and nothing has to be reconstructed at the end.
    path = tmp_path / "journal.jsonl"
    recorder = TurnRecorder(journal_at(path))

    recorder.record(
        BlockCompletedEvent(
            identifiers=IDENTIFIERS,
            block=TurnToolCallBlock(
                id="call-1", name="Agent", arguments={"subagent_type": "code-reviewer"}
            ),
        )
    )
    recorder.record(
        MessageCompletedEvent(
            identifiers=IDENTIFIERS,
            message=TurnMessage(
                role="assistant",
                blocks=[TurnTextBlock(text="reviewing")],
                parent_tool_call_id="call-1",
                model="claude",
            ),
        )
    )
    recorder.record(TurnCompletedEvent(identifiers=IDENTIFIERS))

    events = read_observable_events(path)
    assert verify_event_chain(events)
    assert [event.kind for event in events] == [
        "tool_call",
        "subagent_start",
        "message",
        "subagent_end",
        "turn_end",
    ]
    delegated = events[1]
    assert delegated.actor.kind == "native_subagent"
    assert delegated.actor.name == "code-reviewer"
    assert delegated.actor.model == "claude"


def test_an_undelegated_message_opens_no_span(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    recorder = TurnRecorder(journal_at(path))

    recorder.record(
        MessageCompletedEvent(
            identifiers=IDENTIFIERS,
            message=TurnMessage(role="assistant", blocks=[TurnTextBlock(text="hi")]),
        )
    )

    assert read_observable_events(path) == []
