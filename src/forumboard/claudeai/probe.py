"""What one profile's claude.ai session can actually see.

A sync that reports "nothing new" has several causes that look identical from
outside: a session the service no longer accepts, an organisation holding no
conversations, and a listing whose every entry is older than the freshness
floor. This asks all of them at once and answers them apart, so an operator
reads which one happened rather than inferring it from a silence.

Nothing here re-derives what a sync would do — it asks the same client the
same questions. The organisation reported is the one a sync resolves, and the
fresh count is the list a sync would receive, so a discrepancy between this
report and a pass is a discrepancy in the service, not in two copies of a rule.

Every call is a read. It lists organisations and conversations, and touches
neither a cursor nor Notion.
"""

from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel

from lup.types import JsonValue

from forumboard.agent.config import settings
from forumboard.claudeai.browser import (
    ProfileCredentials,
    context_dir,
    stored_credentials,
)
from forumboard.claudeai.client import (
    Attachment,
    BlockMetadata,
    ClaudeCredentials,
    ClaudeWebClient,
    ClaudeWebError,
    ContentBlock,
    ConversationMeta,
    ConversationPayload,
    Message,
    OrganizationEntry,
    StaticCredentials,
)
from forumboard.store import ConversationStore


class PayloadAudit(BaseModel, frozen=True):
    """What a conversation payload carried, against what the models read.

    The payload models ignore fields they do not declare. That is deliberate —
    it is what keeps one new upstream key from rejecting a whole conversation
    — and it is also the one way material reaches this repository and is lost
    without anything saying so. Redaction can only remove what it can see, and
    a pass cannot judge a field that never became text.

    So the ignoring is made answerable: every key the service sent that
    carries something, set against the names the models read. What lands under
    ``dropped`` is not necessarily a bug, but it is the complete list of places
    one could be hiding.
    """

    # lup: ignore[dict-str-payload] — keyed by whatever the service sent
    read: dict[str, int] = {}
    # lup: ignore[dict-str-payload] — the same, for what nothing reads
    dropped: dict[str, int] = {}
    # lup: ignore[dict-str-payload] — keyed by the block kinds it served
    kinds: dict[str, int] = {}

    @classmethod
    def over(cls, payload: JsonValue) -> "PayloadAudit":
        """Audit one payload, exactly as the service sent it."""
        carried = Counter(cls.carried(payload))
        known = {
            name
            for model in (
                ConversationPayload,
                Message,
                ContentBlock,
                BlockMetadata,
                Attachment,
                ConversationMeta,
                OrganizationEntry,
            )
            for name in model.model_fields
        }
        return cls(
            read={key: n for key, n in sorted(carried.items()) if key in known},
            dropped={key: n for key, n in sorted(carried.items()) if key not in known},
            kinds=dict(sorted(Counter(cls.kinds_of(payload)).items())),
        )

    @classmethod
    def fields_on(cls, value: JsonValue, kind: str) -> Iterator[str]:
        """Every field name carried by blocks calling themselves ``kind``.

        What a kind is made of decides how it can be rendered, and guessing
        that from a name is how a renderer comes to read a key the service
        does not send.
        """
        match value:
            case {"type": str() as served, **rest} if served == kind:
                for key, held in rest.items():
                    if held not in (None, "", [], {}):
                        yield key
            case _:
                pass
        match value:
            case dict():
                for held in value.values():
                    yield from cls.fields_on(held, kind)
            case list():
                for held in value:
                    yield from cls.fields_on(held, kind)
            case _:
                return

    @classmethod
    def kinds_of(cls, value: JsonValue) -> Iterator[str]:
        """Every ``type`` the payload's blocks call themselves.

        The renderer dispatches on this, so a kind it does not name falls
        through to the general case. Listing the kinds actually served is what
        says whether that fallthrough is a default or a hole.
        """
        match value:
            case {"type": str() as kind} if kind:
                yield kind
            case _:
                pass
        match value:
            case dict():
                for held in value.values():
                    yield from cls.kinds_of(held)
            case list():
                for held in value:
                    yield from cls.kinds_of(held)
            case _:
                return

    @classmethod
    def carried(cls, value: JsonValue) -> Iterator[str]:
        """Every key anywhere in ``value`` that arrived holding something.

        Empty strings, empty lists and nulls are not yielded: the service
        spells "this message had none of that" in all three, and a key that
        only ever arrives empty is not material anybody lost.

        A tool call's ``input`` is not descended into. It is declared as raw
        JSON and rendered whole, so its own keys are already in the transcript
        — walking them would report a tool's every argument name as dropped
        material, which is the opposite of true.
        """
        match value:
            case dict():
                for key, held in value.items():
                    if held not in (None, "", [], {}):
                        yield key
                    if key != "input":
                        yield from cls.carried(held)
            case list():
                for held in value:
                    yield from cls.carried(held)
            case _:
                return

    def lines(self) -> list[str]:
        """This audit as an operator reads it."""
        return [
            f"read {len(self.read)} field(s), dropped {len(self.dropped)}",
            "",
            "read:",
            *[f"  {key} ×{count}" for key, count in self.read.items()],
            "",
            "dropped — carried something, nothing reads it:",
            *(
                [f"  {key} ×{count}" for key, count in self.dropped.items()]
                or ["  (nothing)"]
            ),
            "",
            "block kinds served:",
            *[f"  {kind} ×{count}" for kind, count in self.kinds.items()],
        ]


class OrganizationProbe(BaseModel, frozen=True):
    """What one organisation answered about the conversations under it."""

    entry: OrganizationEntry
    used: bool = False
    """Whether a sync reads under this one."""
    listed: int = 0
    fresh: int = 0
    """Of those listed, how many a sync would treat as new."""
    newest: datetime | None = None
    refusal: str = ""
    """Why it could not be listed, empty where it was."""

    def describe(self) -> str:
        """This organisation and its conversations, as one line."""
        marker = "  <- a sync reads under this one" if self.used else ""
        if self.refusal:
            return f"{self.entry.describe()} — {self.refusal}{marker}"
        if not self.listed:
            return f"{self.entry.describe()} — no conversations{marker}"
        stamp = self.newest.isoformat() if self.newest is not None else "unknown"
        return (
            f"{self.entry.describe()} — {self.listed} conversations, "
            f"{self.fresh} of them new, newest {stamp}{marker}"
        )


class ProfileProbe(BaseModel, frozen=True):
    """What one profile's session sees, and what a sync would make of it."""

    profile: str
    since: datetime
    from_cursor: bool = False
    """Whether the floor is a recorded cursor rather than the lookback."""
    hint: str = ""
    """The organisation the session's ``lastActiveOrg`` cookie names."""
    used: str = ""
    organizations: list[OrganizationProbe] = []
    refusal: str = ""
    """Why nothing could be read, empty where something was."""

    def floor_origin(self) -> str:
        """Where the freshness cutoff came from."""
        if self.from_cursor:
            return "the cursor this profile last recorded"
        return f"a {settings.sync_lookback_days}-day lookback, there being no cursor"

    def choice_origin(self) -> str:
        """How the organisation being read was arrived at."""
        if self.hint == self.used:
            return "named by the session's lastActiveOrg cookie"
        if self.hint:
            return (
                "chosen from the account's own list; the lastActiveOrg cookie "
                f"names {self.hint}, which was not honoured"
            )
        return "chosen from the account's own list"

    def findings(self) -> list[str]:
        """What the reading says is wrong, where it says anything."""
        used = [probe for probe in self.organizations if probe.used]
        chat = [
            probe.entry.uuid
            for probe in self.organizations
            if probe.entry.chat_capable() and not probe.used
        ]
        elsewhere = [
            probe for probe in self.organizations if probe.listed and not probe.used
        ]

        if not used:
            return [
                f"The account does not list {self.used}, which is the "
                "organisation being read under. The session names an "
                "organisation this account is no longer a member of."
            ]
        found = used[0]
        reported = list[str]()
        if not found.entry.chat_capable():
            reported.append(
                "Conversations are not readable under the organisation being read "
                "under: the account reports no chat capability for it."
                + (f" {', '.join(chat)} does." if chat else "")
            )
        if found.listed and not found.fresh:
            reported.append(
                f"Every one of its {found.listed} conversations is older than "
                f"{self.since.isoformat()}, so the floor filtered them, not the "
                "listing."
            )
        if not found.listed and elsewhere:
            reported.append(
                "It holds no conversations, while "
                + ", ".join(
                    f"{probe.entry.uuid} holds {probe.listed}" for probe in elsewhere
                )
                + "."
            )
        return reported

    def lines(self) -> list[str]:
        """The whole report, one line at a time."""
        if self.refusal:
            return [f"{self.profile}: {self.refusal}"]
        capped = (
            f" A listing asks for at most {settings.sync_max_conversations}, so a"
            " count at that number means at least that many."
        )
        return [
            f"{self.profile}: reading under {self.used} — {self.choice_origin()}.",
            f"New means touched after {self.since.isoformat()}, from"
            f" {self.floor_origin()}.{capped}",
            "",
            *[probe.describe() for probe in self.organizations],
            *([""] if self.findings() else []),
            *self.findings(),
        ]


async def organization_probe(
    entry: OrganizationEntry,
    client: ClaudeWebClient,
    since: datetime,
    *,
    used: bool,
) -> OrganizationProbe:
    """List one organisation twice — everything, then what a sync would keep."""
    try:
        listed = await client.conversations(limit=settings.sync_max_conversations)
        fresh = await client.conversations(
            limit=settings.sync_max_conversations, updated_after=since
        )
    except ClaudeWebError as error:
        return OrganizationProbe(entry=entry, used=used, refusal=str(error))
    stamps = [stamp for meta in listed if (stamp := meta.updated()) is not None]
    return OrganizationProbe(
        entry=entry,
        used=used,
        listed=len(listed),
        fresh=len(fresh),
        newest=max(stamps, default=None),
    )


async def probe_profile(
    *, profiles_root: Path, store: ConversationStore, profile: str
) -> ProfileProbe:
    """Ask claude.ai what this profile can see, deciding nothing about it."""
    cursor = store.read_cursor(profile)
    floor = datetime.now(timezone.utc) - timedelta(days=settings.sync_lookback_days)
    since = cursor.updated_through or floor
    from_cursor = cursor.updated_through is not None
    directory = context_dir(profiles_root, profile)
    credentials = await stored_credentials(directory)

    if not credentials.usable():
        return ProfileProbe(
            profile=profile,
            since=since,
            from_cursor=from_cursor,
            refusal=(
                "no stored claude.ai session — run "
                f"`forumboard profile login {profile}`"
            ),
        )

    account = ClaudeWebClient(
        StaticCredentials(ClaudeCredentials(cookie=credentials.cookie))
    )
    try:
        entries = await account.account_organizations()
        used = await ClaudeWebClient(
            ProfileCredentials(directory)
        ).reading_organization()
    except ClaudeWebError as error:
        return ProfileProbe(
            profile=profile,
            since=since,
            from_cursor=from_cursor,
            hint=credentials.organization,
            refusal=f"could not read the account: {error}",
        )

    def pinned(entry: OrganizationEntry) -> ClaudeWebClient:
        """A client that reads under exactly one organisation.

        Pinned rather than hinted: a report on what an organisation holds has
        to be about the one it names, including the one whose capability would
        have made resolution pass over it.
        """
        return ClaudeWebClient(
            StaticCredentials(ClaudeCredentials(cookie=credentials.cookie)),
            organization=entry.uuid,
        )

    return ProfileProbe(
        profile=profile,
        since=since,
        from_cursor=from_cursor,
        hint=credentials.organization,
        used=used,
        organizations=[
            await organization_probe(
                entry, pinned(entry), since, used=entry.uuid == used
            )
            for entry in entries
        ],
    )
