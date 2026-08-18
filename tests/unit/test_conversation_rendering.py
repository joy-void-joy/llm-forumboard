"""What survives the trip from a claude.ai payload to a transcript on disk.

A redaction pass can only remove what it can see, so the failure this guards
against is silent: a payload field nobody read renders as nothing, and nothing
reads exactly like a conversation that did not contain it. Each test below
names one thing the service actually sends and asserts it arrives.

The payloads are shaped after real ones — a tool call with its arguments, an
attachment with no name, a message reporting files it does not carry, a
snapshot titling itself the other way, and nulls where a sibling message sent
a value.
"""

from pathlib import Path

import pytest

from lup.types import JsonObject, JsonValue

from forumboard.claudeai.client import (
    Attachment,
    ClaudeWebError,
    ConversationReference,
    rendered_conversation,
)
from forumboard.pipeline import passes
from forumboard.store import ConversationStore

PROFILE = "somebody"


def said(text: str, **extra: JsonValue) -> JsonObject:
    """One human turn carrying ``text``."""
    return {"sender": "human", "content": [{"type": "text", "text": text}], **extra}


def conversation(*messages: JsonValue, **extra: JsonValue) -> JsonObject:
    """A conversation payload holding exactly these messages."""
    return {
        "uuid": "conv-1",
        "name": "A conversation",
        "chat_messages": list(messages),
        **extra,
    }


def test_a_tool_call_reaches_the_transcript_with_its_arguments() -> None:
    """The hole this closes: a call rendered as nothing cannot be redacted.

    A mail search is the case that matters. Its argument names a person who is
    not in the conversation and did not choose to be discussed, which is the
    sensitivity test almost word for word.
    """
    payload = conversation(
        {
            "sender": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "Gmail:search_threads",
                    "integration_name": "Gmail",
                    "input": {"q": "from:someone@example.com"},
                }
            ],
        }
    )

    rendered = rendered_conversation("conv-1", payload).markdown

    assert "Gmail:search_threads" in rendered
    assert "Gmail" in rendered
    assert "from:someone@example.com" in rendered


def test_an_image_says_it_was_there() -> None:
    """An image cannot be reproduced, so the transcript reports its absence.

    Rendering it as nothing would leave the reviewer approving a conversation
    with a hole in it that looks like no hole at all.
    """
    payload = conversation(
        {"sender": "human", "content": [{"type": "image", "source": "..."}]}
    )

    assert "image" in rendered_conversation("conv-1", payload).markdown


def test_files_a_message_carried_are_announced_by_their_count() -> None:
    """The counts are the service's own, and they are all there is to go on."""
    payload = conversation(said("look at these", file_count=2, image_count=1))

    rendered = rendered_conversation("conv-1", payload).markdown

    assert "2 files" in rendered
    assert "1 image" in rendered


def test_a_truncated_message_says_so() -> None:
    payload = conversation(said("the beginning of something", truncated=True))

    assert "truncated" in rendered_conversation("conv-1", payload).markdown


def test_a_null_field_is_read_as_an_absent_one() -> None:
    """A snapshot sends nulls where a conversation omits the key.

    Rejecting the message for the way it said nothing would fail the whole
    conversation, and the failure would surface as the organisation refusing
    to serve it.
    """
    payload = conversation(said("hello", compaction_summary=None, truncated=None))

    assert "hello" in rendered_conversation("conv-1", payload).markdown


def test_a_snapshot_takes_its_title_from_the_name_it_uses() -> None:
    """A shared conversation has no ``name``; it has ``snapshot_name``."""
    payload = {
        "uuid": "snap-1",
        "snapshot_name": "Shared thing",
        "chat_messages": [said("hello")],
    }

    assert rendered_conversation("snap-1", payload).name == "Shared thing"


def test_a_conversation_with_nothing_readable_is_refused() -> None:
    """Publishing an empty page is worse than reporting that there is none."""
    with pytest.raises(ClaudeWebError):
        rendered_conversation("conv-1", conversation())


def test_an_unnamed_attachment_is_named_from_its_type() -> None:
    """The service really does serve entries whose ``file_name`` is empty."""
    bare = Attachment(id="abc", file_type="txt", extracted_content="body")
    typed = Attachment(id="def", file_type="text/markdown", extracted_content="body")

    assert bare.stored_name() == "attachment.txt"
    assert typed.stored_name() == "attachment.md"


def test_an_attachment_name_cannot_climb_out_of_its_directory() -> None:
    """The name is the uploader's, and it reaches a path."""
    escaping = Attachment(id="abc", file_name="../../../etc/passwd")

    assert escaping.stored_name() == "passwd"
    assert ".." not in str(escaping.relative_path())


def test_two_attachments_sharing_a_name_keep_both_files(tmp_path: Path) -> None:
    """Uploading two files called the same thing is an ordinary thing to do.

    A flat directory would let the second silently replace the first, which is
    content loss that reads as success.
    """
    store = ConversationStore(tmp_path)
    first = Attachment(id="one", file_name="notes.md", extracted_content="first")
    second = Attachment(id="two", file_name="notes.md", extracted_content="second")

    store.save_attachment(PROFILE, "conv-1", first)
    store.save_attachment(PROFILE, "conv-1", second)

    written = sorted(store.attachments_dir(PROFILE, "conv-1").rglob("notes.md"))
    assert [path.read_text() for path in written] == ["first", "second"]


def test_the_transcript_names_the_path_its_attachment_was_written_to(
    tmp_path: Path,
) -> None:
    """What the transcript tells a pass to open has to be what is on disk."""
    store = ConversationStore(tmp_path)
    payload = conversation(
        {
            "sender": "human",
            "content": [{"type": "text", "text": "see attached"}],
            "attachments": [
                {"id": "abc", "file_name": "notes.md", "extracted_content": "body"}
            ],
        }
    )
    content = rendered_conversation("conv-1", payload)

    store.save_conversation(PROFILE, content)

    named = f"attachments/{content.attachments[0].relative_path()}"
    assert named in store.transcript_path(PROFILE, "conv-1").read_text()
    assert (store.conversation_dir(PROFILE, "conv-1") / named).is_file()


def test_a_fetched_original_is_left_read_only(tmp_path: Path) -> None:
    """The editor works in this folder holding a tool that can write.

    It is told to write only its page; read-only is what makes an accident
    impossible rather than merely discouraged, and the transcript is the one
    thing here that re-running the pass would not bring back.
    """
    store = ConversationStore(tmp_path)
    written = store.save_transcript(PROFILE, "conv-1", "the original")

    with pytest.raises(PermissionError):
        written.write_text("overwritten")
    assert written.read_text() == "the original"


def test_a_refetch_replaces_a_read_only_original(tmp_path: Path) -> None:
    """A conversation that grew is the same conversation, written again."""
    store = ConversationStore(tmp_path)
    store.save_transcript(PROFILE, "conv-1", "short")

    assert store.save_transcript(PROFILE, "conv-1", "longer").read_text() == "longer"


def test_no_page_reads_as_the_abort_it_is(tmp_path: Path) -> None:
    """An editor that refused a conversation leaves no page.

    Empty is how the caller learns that, so it has to survive a folder that
    exists and holds a transcript — the abort and a conversation nobody has
    edited yet look the same on disk, and both mean "do not publish this".
    """
    store = ConversationStore(tmp_path)
    store.save_transcript(PROFILE, "conv-1", "the original")

    assert store.read_page(PROFILE, "conv-1") == ""


def test_the_page_the_editor_wrote_comes_back_whole(tmp_path: Path) -> None:
    """The body is read from the file rather than from the editor's answer."""
    store = ConversationStore(tmp_path)
    store.save_transcript(PROFILE, "conv-1", "the original")
    page = "# A conversation\n\n" + "long enough to matter. " * 500
    store.page_path(PROFILE, "conv-1").write_text(page)

    assert store.read_page(PROFILE, "conv-1") == page


def test_the_passes_are_told_the_names_the_store_writes(tmp_path: Path) -> None:
    """The prompt names files, and the store puts them there.

    Two spellings of one layout would let the editor write where nothing
    reads: a page under the other name is not found, and a publish that wrote
    one looks identical to an abort that wrote none.
    """
    store = ConversationStore(tmp_path)
    told = passes.where_to_look("A conversation")

    assert store.transcript_path(PROFILE, "conv-1").name in told
    assert store.attachments_dir(PROFILE, "conv-1").name in told
    assert store.page_path(PROFILE, "conv-1").name in passes.where_to_write()


def test_a_share_link_is_told_from_a_conversation_id_by_its_shape() -> None:
    """Whoever pastes a link should not also have to say what they pasted."""
    assert ConversationReference(value="https://claude.ai/share/abc").shared()
    assert ConversationReference(value="claude.ai/share/abc").shared()
    assert not ConversationReference(
        value="2f77d92e-bcd2-43af-9235-9f112b51b472"
    ).shared()
