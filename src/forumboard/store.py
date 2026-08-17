"""The local cache: raw transcripts, sync cursors, and what was decided.

Two things live here, and they answer different questions.

The **cursor** answers "where did this profile get to", so a pass reads what
has changed rather than everything. It is per profile, because profiles fail
independently and one expired session should not stall everybody else.

The **record** answers "what did we decide about this conversation, and why".
It is what makes a skip stick: without it, every pass would re-review a
conversation somebody already ruled out and pay for the judgement again.

Raw transcripts are kept indefinitely, under ``notes/`` which is gitignored.
They are unredacted by definition — they are the input to redaction — so they
never reach a commit, and anyone with the box has them. That is a deliberate
trade for being able to re-run the editor on old material when a prompt
improves.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from forumboard.agent.models import EditVerdict, ReviewVerdict

logger = logging.getLogger(__name__)


class ConversationRecord(BaseModel, frozen=True):
    """What one pass decided about one conversation."""

    conversation_id: str
    profile: str
    title: str = ""
    updated_at: str = ""
    message_count: int = 0
    reviewed_at: datetime
    review: ReviewVerdict
    edit: EditVerdict | None = None
    page_id: str = ""
    """The Notion page, where one was written."""

    def published(self) -> bool:
        """Whether this conversation reached Notion."""
        return bool(self.page_id)

    def describe(self) -> str:
        """One line for the log."""
        outcome = (
            self.edit.describe() if self.edit is not None else self.review.describe()
        )
        return f"{self.conversation_id[:8]} {self.title[:40]!r}: {outcome}"


class ProfileCursor(BaseModel, frozen=True):
    """How far a profile's sync has read."""

    profile: str
    updated_through: datetime | None = None

    def advanced_to(self, moment: datetime | None) -> "ProfileCursor":
        """This cursor moved forward, never backward.

        A pass that fetched an older conversation than the last one must not
        rewind the high-water mark, or the next pass re-reads everything in
        between.
        """
        if moment is None:
            return self
        if self.updated_through is not None and moment <= self.updated_through:
            return self
        return ProfileCursor(profile=self.profile, updated_through=moment)


class ConversationStore:
    """Where one deployment keeps what it has fetched and decided."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def profile_dir(self, profile: str) -> Path:
        """Where one profile's cache lives."""
        return self.root / profile

    def transcript_path(self, profile: str, conversation_id: str) -> Path:
        """Where a raw transcript is kept."""
        return self.profile_dir(profile) / "transcripts" / f"{conversation_id}.md"

    def record_path(self, profile: str, conversation_id: str) -> Path:
        """Where the decision about a conversation is kept."""
        return self.profile_dir(profile) / "records" / f"{conversation_id}.json"

    def cursor_path(self, profile: str) -> Path:
        """Where a profile's high-water mark is kept."""
        return self.profile_dir(profile) / "cursor.json"

    def save_transcript(
        self, profile: str, conversation_id: str, markdown: str
    ) -> Path:
        """Write a raw transcript, replacing any earlier copy.

        Replacing rather than appending: a conversation that grew is the same
        conversation, and two copies would let a later pass edit the shorter one.
        """
        path = self.transcript_path(profile, conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown)
        return path

    def read_transcript(self, profile: str, conversation_id: str) -> str:
        """A cached transcript, empty when there is none."""
        path = self.transcript_path(profile, conversation_id)
        return path.read_text() if path.is_file() else ""

    def save_record(self, record: ConversationRecord) -> None:
        """Record what was decided about a conversation."""
        path = self.record_path(record.profile, record.conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record.model_dump(mode="json"), indent=2) + "\n")

    def read_record(
        self, profile: str, conversation_id: str
    ) -> ConversationRecord | None:
        """What was decided about a conversation, or None where nothing was."""
        path = self.record_path(profile, conversation_id)
        if not path.is_file():
            return None
        try:
            return ConversationRecord.model_validate_json(path.read_text())
        except ValueError:
            logger.exception(
                "the record at %s is unreadable; treating it as absent", path
            )
            return None

    def read_cursor(self, profile: str) -> ProfileCursor:
        """A profile's high-water mark, fresh where it has never synced."""
        path = self.cursor_path(profile)
        if not path.is_file():
            return ProfileCursor(profile=profile)
        try:
            return ProfileCursor.model_validate_json(path.read_text())
        except ValueError:
            logger.exception("the cursor at %s is unreadable; starting over", path)
            return ProfileCursor(profile=profile)

    def write_cursor(self, cursor: ProfileCursor) -> None:
        """Record a profile's high-water mark."""
        path = self.cursor_path(cursor.profile)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cursor.model_dump(mode="json"), indent=2) + "\n")

    def records(self, profile: str) -> list[ConversationRecord]:
        """Every decision recorded for a profile, for a status listing."""
        directory = self.profile_dir(profile) / "records"
        if not directory.is_dir():
            return list[ConversationRecord]()
        found = [
            record
            for path in sorted(directory.glob("*.json"))
            if (record := self.read_record(profile, path.stem)) is not None
        ]
        return found
