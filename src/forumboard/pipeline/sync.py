"""One pass over the enrolled profiles: fetch, review, edit, publish.

The order of the guards here is the whole design. A conversation is fetched
only if the cursor says it moved; it is *reviewed* only if it actually changed
since the last decision; and it reaches Notion only if two independent passes
both said it should. Each guard exists because the step after it is expensive
or irreversible, and every one of them is cheap.

The first guard has an opening, and it is the one place it should. A profile
with no cursor has no "moved" to answer to, and the lookback standing in for
one answers nothing at all on an account quieter than the window — so a first
pass seeds instead, and the guard closes behind it.

Failures are per profile and per conversation. An expired session stalls one
person's sync and says so; it does not stop everybody else's, and it does not
advance a cursor past conversations that were never read.
"""

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel

from forumboard.agent.config import settings
from forumboard.claudeai.browser import ProfileCredentials, context_dir
from forumboard.claudeai.client import (
    ClaudeWebClient,
    ClaudeWebError,
    ConversationContent,
    ConversationMeta,
    SessionExpired,
)
from forumboard.context import ForumboardContext
from forumboard.notion.discussions import DiscussionRecord
from forumboard.pipeline import passes
from forumboard.profiles import Roster
from forumboard.store import ConversationRecord, ConversationStore

logger = logging.getLogger(__name__)


class SyncOutcome(BaseModel, frozen=True):
    """What one pass over one profile did."""

    profile: str
    fetched: int = 0
    published: int = 0
    skipped: int = 0
    unchanged: int = 0
    failed: int = 0
    note: str = ""

    def describe(self) -> str:
        """One line for the log."""
        if self.note:
            return f"{self.profile}: {self.note}"
        return (
            f"{self.profile}: {self.fetched} fetched, {self.published} published, "
            f"{self.skipped} skipped, {self.unchanged} unchanged, {self.failed} failed"
        )


async def to_read(
    client: ClaudeWebClient, since: datetime, *, seeding: bool
) -> list[ConversationMeta]:
    """What a pass reads: the window, widened to a seed on a first pass.

    The lookback window is what keeps enrolling somebody from importing their
    whole history. On an account quieter than the window it asks for nothing at
    all, and a first pass that publishes nothing is indistinguishable from a
    broken sync — so a profile with no cursor reaches past the window for the
    newest few, once. Every pass after it has a cursor, and is the window alone.

    The seed is added to the window rather than replacing it, so widening the
    reach can only ever read more than the window would have.
    """
    window = await client.conversations(
        limit=settings.sync_max_conversations, updated_after=since
    )
    seed = settings.sync_seed_conversations
    if not seeding or len(window) >= seed:
        return window
    listed = await client.conversations(limit=settings.sync_max_conversations)
    already = {meta.uuid for meta in window}
    return window + [
        meta
        for meta in ConversationMeta.newest_first(listed)[:seed]
        if meta.uuid not in already
    ]


class ConversationSync:
    """Reads the enrolled profiles and publishes what survives review."""

    def __init__(
        self,
        *,
        profiles_root: Path,
        store: ConversationStore,
        context: ForumboardContext,
        roster: Roster,
        dry_run: bool = False,
    ) -> None:
        self.profiles_root = profiles_root
        self.store = store
        self.context = context
        self.roster = roster
        self.dry_run = dry_run
        """Report what would be read and stop before the first Opus call."""

    def client_for(self, profile: str) -> ClaudeWebClient:
        """A claude.ai client backed by one profile's stored browser session."""
        directory = context_dir(self.profiles_root, profile)
        return ClaudeWebClient(ProfileCredentials(directory))

    async def run_once(self) -> list[SyncOutcome]:
        """One pass over every enrolled profile."""
        if not self.roster.entries:
            return [SyncOutcome(profile="(none)", note="nobody is enrolled")]
        return [await self.sync_profile(name) for name in self.roster.names()]

    async def sync_profile(self, profile: str) -> SyncOutcome:
        """Fetch and process one profile's changed conversations."""
        client = self.client_for(profile)
        cursor = self.store.read_cursor(profile)
        floor = datetime.now(timezone.utc) - timedelta(days=settings.sync_lookback_days)
        since = cursor.updated_through or floor

        try:
            metas = await to_read(client, since, seeding=cursor.updated_through is None)
        except SessionExpired as expired:
            return SyncOutcome(
                profile=profile,
                note=f"session expired — re-run `forumboard profile login {profile}` ({expired})",
            )
        except ClaudeWebError as error:
            return SyncOutcome(profile=profile, note=f"could not list: {error}")

        if not metas:
            return SyncOutcome(
                profile=profile,
                note=(
                    f"nothing under {client.organization} touched since "
                    f"{since.isoformat()} — `forumboard profile probe {profile}` "
                    "says what that session can see"
                ),
            )

        if self.dry_run:
            named = ", ".join(f"{meta.uuid[:8]} {meta.name[:40]!r}" for meta in metas)
            return SyncOutcome(
                profile=profile, note=f"would read {len(metas)}: {named}"
            )

        tallies = [await self.one(profile, meta, client) for meta in metas]
        advanced = cursor
        for meta, tally in zip(metas, tallies, strict=True):
            if tally.failed:
                continue
            advanced = advanced.advanced_to(meta.updated())
        self.store.write_cursor(advanced)

        return SyncOutcome(
            profile=profile,
            fetched=sum(tally.fetched for tally in tallies),
            published=sum(tally.published for tally in tallies),
            skipped=sum(tally.skipped for tally in tallies),
            unchanged=sum(tally.unchanged for tally in tallies),
            failed=sum(tally.failed for tally in tallies),
        )

    async def one(
        self, profile: str, meta: ConversationMeta, client: ClaudeWebClient
    ) -> SyncOutcome:
        """Fetch and process one conversation, reporting what happened to it."""
        try:
            content = await client.conversation(meta.uuid)
        except SessionExpired:
            raise
        except ClaudeWebError as error:
            logger.warning("Could not fetch %s: %s", meta.uuid, error)
            return SyncOutcome(profile=profile, failed=1)

        self.store.save_transcript(profile, content.uuid, content.markdown)
        existing = self.store.read_record(profile, content.uuid)
        if existing is not None and existing.updated_at == content.updated_at:
            return SyncOutcome(profile=profile, fetched=1, unchanged=1)

        try:
            return await self.decide(profile, content)
        except Exception:
            logger.exception("Processing %s failed", content.uuid)
            return SyncOutcome(profile=profile, fetched=1, failed=1)

    async def decide(self, profile: str, content: ConversationContent) -> SyncOutcome:
        """Review, edit, and publish one conversation — or stop at either gate."""
        verdict = await passes.review(content.name, content.markdown)
        plan = verdict.plan_for_editor()
        now = datetime.now(timezone.utc)
        record = ConversationRecord(
            conversation_id=content.uuid,
            profile=profile,
            title=content.name,
            updated_at=content.updated_at,
            message_count=content.message_count,
            reviewed_at=now,
            review=verdict,
        )
        if plan is None:
            self.store.save_record(record)
            logger.info("%s", record.describe())
            return SyncOutcome(profile=profile, fetched=1, skipped=1)

        outcome = await passes.edit(content.name, content.markdown, plan)
        published = outcome.publishable()
        record = record.model_copy(update={"edit": outcome})
        if published is None:
            self.store.save_record(record)
            logger.info("%s", record.describe())
            return SyncOutcome(profile=profile, fetched=1, skipped=1)

        people = self.context.people.resolve(published.attention)
        page = await self.context.discussions.write(
            DiscussionRecord(
                conversation_id=content.uuid,
                profile=profile,
                started=content.started(),
                updated=content.updated(),
                published=published,
                people=people,
            ),
            now,
        )
        self.store.save_record(record.model_copy(update={"page_id": page.id}))
        logger.info("%s — %s", record.describe(), people.describe())
        return SyncOutcome(profile=profile, fetched=1, published=1)
