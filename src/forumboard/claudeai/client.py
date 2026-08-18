"""claude.ai's own conversation API, as this project reads it.

This is an unofficial API and the whole fragile surface is deliberately in one
module, so a change upstream has one place to land. Nothing here reads
settings: a :class:`CredentialSource` supplies the session cookie and can be
asked to produce a fresh one, which is what lets a profile back its session
with a stored browser context without this module knowing browsers exist.

What arrives off the wire is parsed into models rather than read key by key.
That is what makes the payload's shape reviewable in one place: every field
this project depends on is declared, so a rename upstream surfaces as one
validation failure instead of as an empty string at whichever call site asked
for the key that stopped existing — and an unread field is visibly unread
rather than merely unmentioned. ``extra="ignore"`` throughout, because the
service sends a great deal this project has no use for.

Organisations matter more than they look. An account usually has a non-chat
organisation beside the chat one — the developer console's — and reading
conversations under the wrong one answers 403, which is indistinguishable from
an expired cookie unless the organisation is chosen by capability.

Requests go through ``client.request("GET", …)`` rather than ``client.get``:
the two are the same call, and the spelling without ``.get`` does not read as
a dictionary lookup to the tooling that audits this repository.
"""

import json
import logging
import mimetypes
from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import PurePosixPath
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ValidationError

from lup.types import JsonObject, JsonValue, StringMap

logger = logging.getLogger(__name__)

# lup: ignore[constant-declaration] — the service's own address
BASE_URL = "https://claude.ai"
# lup: ignore[constant-declaration] — the query the service documents for
# rendering a conversation with its tool calls intact
RENDER_PARAMS = "rendering_mode=messages&render_all_tools=true"

# lup: ignore[constant-declaration] — the headers a browser sends, which is
# what this endpoint answers to
CLAUDE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.5",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Origin": BASE_URL,
    "Connection": "keep-alive",
}


class ClaudeWebError(Exception):
    """A claude.ai call failed for a reason that is not authentication."""


class SessionExpired(ClaudeWebError):
    """The session cookie is missing, expired, or rejected (401/403)."""


class ClaudeCredentials(BaseModel, frozen=True):
    """What one profile presents to claude.ai."""

    cookie: str = ""
    organization: str = ""
    """A hint only. It is honoured when it names a chat-capable organisation
    and ignored otherwise, so a bare pasted session key still works."""

    def usable(self) -> bool:
        """Whether there is anything here worth presenting."""
        return bool(self.cookie)


class CredentialSource(ABC):
    """Where a client gets its session, and how it gets a fresher one."""

    @abstractmethod
    async def credentials(self, *, refresh: bool) -> ClaudeCredentials:
        """The session to present. ``refresh`` asks for a newly read one."""


class StaticCredentials(CredentialSource):
    """A fixed session, for a caller that already holds one."""

    def __init__(self, credentials: ClaudeCredentials) -> None:
        self.value = credentials

    async def credentials(self, *, refresh: bool) -> ClaudeCredentials:
        return self.value


class OrganizationEntry(BaseModel, frozen=True, extra="ignore"):
    """One organisation the account belongs to."""

    uuid: str
    name: str = ""
    capabilities: list[str] = []

    def chat_capable(self) -> bool:
        """Whether conversations can be read under it."""
        return "chat" in self.capabilities

    def describe(self) -> str:
        """This organisation as one line an operator can tell apart."""
        capability = "chat" if self.chat_capable() else "no chat capability"
        return f"{self.uuid} {self.name or '(unnamed)'} — {capability}"


class ConversationMeta(BaseModel, extra="ignore"):
    """A conversation as the listing describes it — no messages."""

    uuid: str
    name: str = ""
    created_at: str = ""
    updated_at: str = ""

    def updated(self) -> datetime | None:
        """When it was last touched, as far as the listing knows."""
        return parse_timestamp(self.updated_at)

    @classmethod
    def newest_first(cls, metas: list["ConversationMeta"]) -> list["ConversationMeta"]:
        """These conversations, most recently touched first.

        The listing already arrives in that order. Sorting it again is for the
        caller that takes a head rather than the whole list, which would take
        the wrong conversations without saying so if the service ever changed
        its mind. An entry whose timestamp the listing omitted sorts last,
        rather than being dropped for having none.
        """
        epoch = datetime.fromtimestamp(0, timezone.utc)
        return sorted(metas, key=lambda meta: meta.updated() or epoch, reverse=True)


class Attachment(BaseModel, frozen=True, extra="ignore"):
    """One file uploaded into a conversation.

    claude.ai extracts a text document when it is uploaded and serves the
    result in the conversation payload, so an entry carries the file's
    contents rather than a way to fetch them. There is nothing to download.

    A name is not guaranteed — the service serves entries whose ``file_name``
    is empty — and a name that does arrive was chosen by whoever uploaded it,
    so it is somebody else's string reaching a path. Both are why the name a
    file is stored under is decided here, once, rather than at each writer.
    """

    id: str = ""
    file_name: str = ""
    file_size: int = 0
    file_type: str = ""
    extracted_content: str = ""

    def extension(self) -> str:
        """The suffix this file's type implies, empty where it implies none.

        ``file_type`` arrives either as a media type (``text/markdown``) or as
        a bare suffix (``txt``); the media-type database answers the first and
        declines the second, which is what tells the two apart without
        inspecting the string.
        """
        if guessed := mimetypes.guess_extension(self.file_type):
            return guessed
        return f".{self.file_type}" if self.file_type else ""

    def stored_name(self) -> str:
        """What to call this file.

        Reduced to a single path component: the name is the uploader's, and a
        conversation is not entitled to choose where in the filesystem its
        attachment lands. An entry with neither a name nor a type still gets
        one, because a file written under no name is a file nobody can open.
        """
        named = PurePosixPath(self.file_name).name
        return named or f"attachment{self.extension()}"

    def relative_path(self) -> PurePosixPath:
        """Where this file sits under the conversation's attachment directory.

        Each file gets a directory of its own, named by the identifier the
        service gave it. Two uploads into one conversation can carry the same
        name — the same ``notes.md`` twice is an ordinary thing to do — and a
        flat directory would let the second quietly replace the first. The
        identifier is unique where the name is not, so nesting under it makes
        that collision impossible rather than merely detectable.
        """
        return PurePosixPath(PurePosixPath(self.id).name or "unidentified") / (
            self.stored_name()
        )

    def label(self) -> str:
        """How this attachment is announced in the transcript.

        The path is the point: the passes read the conversation from a
        directory, and this is what tells them the file is there to open.
        """
        return (
            f"[Attachment: {self.stored_name()} → attachments/{self.relative_path()}]"
        )


class ConversationContent(BaseModel, frozen=True):
    """A whole conversation, rendered to speaker-tagged Markdown."""

    uuid: str
    name: str = ""
    created_at: str = ""
    updated_at: str = ""
    markdown: str
    message_count: int
    attachments: list[Attachment] = []
    """Every file uploaded across the conversation, for a caller that writes
    them out beside the transcript the labels point into."""

    def started(self) -> datetime | None:
        """When the conversation began."""
        return parse_timestamp(self.created_at)

    def updated(self) -> datetime | None:
        """When its last message landed."""
        return parse_timestamp(self.updated_at)


def parse_timestamp(value: str) -> datetime | None:
    """An ISO 8601 timestamp, or None when absent or malformed."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        logger.debug("claude.ai returned an unparseable timestamp: %r", value)
        return None


def organization_hint(cookie: str) -> str:
    """The ``lastActiveOrg`` a Cookie header carries, empty when it has none."""
    jar = SimpleCookie()
    try:
        jar.load(cookie)
    except ValueError:
        return ""
    return jar["lastActiveOrg"].value if "lastActiveOrg" in jar else ""


def cookie_header(raw: str) -> str:
    """A pasted credential as a Cookie header.

    Copying ``sessionKey`` out of a browser's developer tools yields the value
    alone, with no name. A header with no ``=`` carries no cookies at all, so
    such a value is promoted to a named one rather than silently dropped.
    """
    if raw and "=" not in raw:
        return f"sessionKey={raw}"
    return raw


def share_id(share: str) -> str:
    """The identifier at the end of a share link, or the link if it is bare."""
    path = PurePosixPath(urlparse(share).path or share)
    return path.name or share


def has_text(value: JsonValue) -> bool:
    """Whether a value is a string holding something other than whitespace."""
    return isinstance(value, str) and bool(value) and not value.isspace()


class RefusalDetails(BaseModel, frozen=True, extra="ignore"):
    """The machine-readable half of a rejection."""

    error_code: str = ""


class Refusal(BaseModel, frozen=True, extra="ignore"):
    """What the service said about why it refused."""

    message: str = ""
    details: RefusalDetails = RefusalDetails()


class RefusalBody(BaseModel, frozen=True, extra="ignore"):
    """A rejection envelope, which nests its reason one level down."""

    error: Refusal = Refusal()


def refusal_detail(response: httpx.Response) -> str:
    """Why claude.ai rejected a session, read from its own error body.

    The service distinguishes an invalid session from a wrong-organisation one
    in the body, and a bare status code does not — so the body is what tells an
    operator which of the two happened.

    A body that is neither JSON nor the documented envelope leaves the status
    code to answer alone, which is what it did before there was a body to read.
    """
    refused = RefusalBody()
    try:
        refused = RefusalBody.model_validate(response.json())
    except ValueError:
        logger.debug("claude.ai refused with a body this could not read")
    code = refused.error.details.error_code
    inside = " ".join(part for part in (str(response.status_code), code) if part)
    message = refused.error.message
    return f"session rejected ({inside})" + (f": {message}" if message else "")


class ContentBlock(BaseModel, frozen=True, extra="ignore"):
    """One block of a message, in the shapes claude.ai serves them.

    ``content`` is the recursive field: a tool result holds blocks of its own
    while an ordinary block holds a string, and declaring both is what lets a
    result be walked without asking what it is at each step.

    Every field the renderer reads is declared here rather than reached for by
    key. That is not only convention — a block whose shape changes upstream
    then arrives as a validation failure at one place, instead of as a silently
    empty string at whichever call site asked for the missing key.
    """

    type: str = ""
    text: str = ""
    content: str | list["ContentBlock"] = ""
    source: str = ""
    name: str = ""
    integration_name: str = ""
    input: JsonObject | None = None

    def call_text(self) -> str:
        """This tool call as text, arguments and all.

        A call is where a conversation reaches outside itself, and its
        arguments are what it reached for — a mail search, a file's contents,
        a query. That makes them exactly the material the sensitivity test is
        about: a thread pulled in from an inbox belongs to somebody who is not
        in the conversation and did not choose to be discussed. A reviewer
        that cannot see the call cannot redact what the call brought back.

        The arguments are re-encoded rather than pasted, so an argument that
        happens to contain the speaker tags this transcript is delimited by
        cannot forge one.
        """
        via = f" via {self.integration_name}" if self.integration_name else ""
        spelled = json.dumps(self.input, indent=2, ensure_ascii=False, default=str)
        return f"[tool call: {self.name or '(unnamed tool)'}{via}]\n{spelled}"

    def nested(self) -> str:
        """The blocks a tool result holds, or the string it holds instead."""
        match self.content:
            case str():
                return self.content
            case blocks:
                return "\n\n".join(
                    text for block in blocks if (text := block.text_payload())
                )

    def text_payload(self) -> str:
        """What this block says, empty where it says nothing.

        An image says that it is there. It cannot be reproduced in a text
        transcript, and rendering it as nothing would leave the reviewer
        deciding about a conversation with a hole in it that looks like no
        hole at all.
        """
        match self.type:
            case "image":
                return "[an image was in the conversation here, not reproduced]"
            case "tool_use":
                return self.call_text()
            case "tool_result":
                return self.nested()
            case _:
                inline = self.content if isinstance(self.content, str) else ""
                return next(
                    (value for value in (self.text, inline, self.source) if value), ""
                )


class Message(BaseModel, frozen=True, extra="ignore"):
    """One turn of a conversation, with everything it carried."""

    uuid: str = ""
    sender: str = ""
    content: str | list[ContentBlock] = ""
    attachments: list[Attachment] = []
    file_count: int = 0
    image_count: int = 0
    truncated: bool = False
    compaction_summary: str = ""

    def blocks(self) -> list[str]:
        """What this message's content says."""
        match self.content:
            case str():
                return [self.content] if self.content else list[str]()
            case blocks:
                return [text for block in blocks if (text := block.text_payload())]

    def uploaded(self) -> list[str]:
        """Each attachment, announced by where it was written and then quoted.

        The path is the point: the passes read the conversation from a
        directory, and the label is what sends them to the file.
        """
        return [
            f"{attachment.label()}\n{attachment.extracted_content}"
            for attachment in self.attachments
            if has_text(attachment.extracted_content)
        ]

    def notes(self) -> list[str]:
        """What this message says about itself that its content does not.

        Each of these is the service reporting that the text beside it is not
        all of what was said. A transcript that drops them reads as complete
        while it is not, which is the one failure a redaction pass cannot
        recover from: the reviewer allows what it never saw.
        """
        tallies = [
            f"{count} {noun if count == 1 else noun + 's'}"
            for count, noun in ((self.file_count, "file"), (self.image_count, "image"))
            if count
        ]
        return [
            note
            for note in (
                f"[this message carried {' and '.join(tallies)}, not reproduced here]"
                if tallies
                else "",
                f"[the service compacted this message, summarising it as]\n"
                f"{self.compaction_summary}"
                if has_text(self.compaction_summary)
                else "",
                "[the service reports this message as truncated]"
                if self.truncated
                else "",
            )
            if note
        ]

    def spoken(self) -> str:
        """This message as one speaker-tagged span, empty where it said nothing.

        The ``<user>``/``<claude>`` tags are what tell the reviewer who said
        what, which every judgement about whose information is being disclosed
        depends on.
        """
        body = "\n\n".join(self.blocks() + self.uploaded() + self.notes())
        if not has_text(body):
            return ""
        speaker = "user" if self.sender == "human" else "claude"
        return f"<{speaker}>\n{body}\n</{speaker}>"


class ConversationPayload(BaseModel, frozen=True, extra="ignore"):
    """A conversation or a shared snapshot, as the service serves either.

    One model for both. A snapshot titles itself ``snapshot_name`` and carries
    no ``name``; everything else about the two is the same shape. Two models
    would mean two renderers, and the second would be the one that fell behind
    — so the difference is a field that may be absent rather than a type.
    """

    uuid: str = ""
    name: str = ""
    snapshot_name: str = ""
    created_at: str = ""
    updated_at: str = ""
    chat_messages: list[Message] = []

    def attachments(self) -> list[Attachment]:
        """Every file uploaded anywhere in the conversation."""
        return [
            attachment
            for message in self.chat_messages
            for attachment in message.attachments
        ]

    def content(self, identifier: str) -> ConversationContent:
        """This payload as the rendered conversation the passes read.

        ``identifier`` is what the caller asked for — a conversation id or a
        share id — and stands in where the payload names no uuid of its own.
        """
        spoken = [text for message in self.chat_messages if (text := message.spoken())]
        if not spoken:
            raise ClaudeWebError(f"conversation {identifier} has no readable messages")
        return ConversationContent(
            uuid=self.uuid or identifier,
            name=self.name or self.snapshot_name,
            created_at=self.created_at,
            updated_at=self.updated_at,
            markdown="\n\n".join(spoken),
            message_count=len(spoken),
            attachments=self.attachments(),
        )


def rendered_conversation(identifier: str, payload: JsonValue) -> ConversationContent:
    """A conversation payload as speaker-tagged Markdown."""
    try:
        parsed = ConversationPayload.model_validate(payload)
    except ValidationError as error:
        raise ClaudeWebError(
            f"conversation {identifier} came back in an unexpected shape"
        ) from error
    return parsed.content(identifier)


class PathBuilder(BaseModel, frozen=True):
    """An organisation-scoped path, whose shape only its own kind knows."""

    @abstractmethod
    def path(self, organization: str) -> str:
        """This request's path under ``organization``."""

    def extra_headers(self) -> StringMap:
        """Headers this kind of request needs beyond the session's own.

        Declared here so a request that needs one carries it, rather than the
        one method that sends every request growing a parameter for each kind
        that turned out to be special.
        """
        return {}


class ConversationListing(PathBuilder, frozen=True):
    """The account's conversation list."""

    limit: int = 50

    def path(self, organization: str) -> str:
        return (
            f"/api/organizations/{organization}/chat_conversations"
            f"?limit={self.limit}&offset=0"
        )


class ConversationDetail(PathBuilder, frozen=True):
    """One conversation, with every message rendered."""

    uuid: str

    def path(self, organization: str) -> str:
        return (
            f"/api/organizations/{organization}/chat_conversations/"
            f"{self.uuid}?{RENDER_PARAMS}"
        )


class SnapshotDetail(PathBuilder, frozen=True):
    """One shared conversation, as the organisation holding it serves it.

    The referring page is part of the request: the service serves a snapshot
    to whoever arrived at its share page, and a request that names no such
    page is answered as though the link had not been followed.
    """

    share: str

    def path(self, organization: str) -> str:
        return (
            f"/api/organizations/{organization}/chat_snapshots/"
            f"{self.share}?{RENDER_PARAMS}"
        )

    def extra_headers(self) -> StringMap:
        return {"Referer": f"{BASE_URL}/share/{self.share}"}


class ClaudeWebClient:
    """One profile's view of claude.ai."""

    def __init__(
        self, source: CredentialSource, *, timeout: int = 30, organization: str = ""
    ) -> None:
        self.source = source
        self.pinned = organization
        """An organisation a caller named outright, which is obeyed as given.

        A diagnostic asking what one organisation holds means that one, and a
        resolution that quietly answered about a better one would report the
        wrong thing under its name."""
        self.organization = ""
        """The organisation resolved for this client, cached until a retry."""
        self.timeout = timeout

    def headers(self, cookie: str) -> StringMap:
        """Browser-shaped headers carrying one session."""
        return {**CLAUDE_HEADERS, "Cookie": cookie}

    async def session(self, *, refresh: bool) -> str:
        """The cookie to present, refusing loudly when there is none."""
        credentials = await self.source.credentials(refresh=refresh)
        if not credentials.usable():
            raise SessionExpired("no claude.ai session available for this profile")
        return cookie_header(credentials.cookie)

    async def organization_entries(
        self, client: httpx.AsyncClient, cookie: str
    ) -> list[OrganizationEntry]:
        """Every organisation the account belongs to, as the service lists them."""
        response = await client.request(
            "GET", f"{BASE_URL}/api/organizations", headers=self.headers(cookie)
        )
        if response.status_code in (401, 403):
            raise SessionExpired(refusal_detail(response))
        if response.status_code != 200:
            raise ClaudeWebError(
                f"could not list organizations ({response.status_code})"
            )
        payload = response.json()
        if not isinstance(payload, list):
            raise ClaudeWebError("organizations came back in an unexpected shape")

        def parsed() -> Iterator[OrganizationEntry]:
            for entry in payload:
                try:
                    yield OrganizationEntry.model_validate(entry)
                except ValidationError:
                    logger.debug("skipping an unreadable organization entry")

        return list(parsed())

    async def account_organizations(self) -> list[OrganizationEntry]:
        """Every organisation this profile's session can see.

        Opens its own transport, for a caller that wants the account's shape
        rather than a conversation — which is what tells an operator whether
        the organisation a sync reads under is the one holding the chats.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return await self.organization_entries(
                client, await self.session(refresh=False)
            )

    async def resolve_organization(self, client: httpx.AsyncClient, cookie: str) -> str:
        """The organisation to read under, which has to be one carrying chat.

        A hint — the credential source's, or the cookie's own ``lastActiveOrg``
        — says which organisation the account was last in, and is worth
        honouring so somebody with two of them gets the one they were using.
        It is honoured only where the account reports chat under it. An account
        usually has a console organisation beside the chat one, and reading
        under that answers an empty listing or a 403, neither of which reads as
        "the wrong organisation" to whoever is looking at the silence.

        Both hints are read here rather than left for :meth:`session` to record
        on the way past, so what this decides depends on its arguments and its
        source rather than on having been called in the right order.
        """
        if resolved := self.pinned or self.organization:
            return resolved
        known = await self.organization_entries(client, cookie)
        if not known:
            raise ClaudeWebError("this account has no organizations")
        offered = await self.source.credentials(refresh=False)
        chatting = [entry.uuid for entry in known if entry.chat_capable()]
        honoured = [
            candidate
            for candidate in (offered.organization, organization_hint(cookie))
            if candidate in chatting
        ]
        if not chatting:
            logger.warning(
                "no organisation on this account reports a chat capability; "
                "reading under %s anyway",
                known[0].uuid,
            )
        self.organization = (honoured + chatting + [known[0].uuid])[0]
        return self.organization

    async def reading_organization(self) -> str:
        """The organisation a fetch through this client would read under.

        Resolves exactly as :meth:`fetch` does rather than restating the rule,
        so what a diagnostic reports and what a pass reads cannot disagree.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            cookie = await self.session(refresh=False)
            return await self.resolve_organization(client, cookie)

    async def fetch(self, request: PathBuilder) -> JsonValue:
        """GET an organisation-scoped endpoint, refreshing the session once.

        The retry re-reads the credential rather than replaying it, because the
        failure this recovers from is a cookie that has since been renewed in
        the profile's stored browser session.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            refused: SessionExpired | None = None
            for attempt in (0, 1):
                try:
                    if attempt:
                        self.organization = ""
                    cookie = await self.session(refresh=bool(attempt))
                    organization = await self.resolve_organization(client, cookie)
                    response = await client.request(
                        "GET",
                        f"{BASE_URL}{request.path(organization)}",
                        headers={**self.headers(cookie), **request.extra_headers()},
                    )
                    if response.status_code in (401, 403):
                        raise SessionExpired(refusal_detail(response))
                    if response.status_code != 200:
                        raise ClaudeWebError(
                            f"claude.ai answered {response.status_code}"
                        )
                    return response.json()
                except SessionExpired as expired:
                    refused = expired
            raise refused or SessionExpired("claude.ai session expired")

    async def conversations(
        self, *, limit: int = 50, updated_after: datetime | None = None
    ) -> list[ConversationMeta]:
        """Conversations touched since ``updated_after``, newest first."""
        payload = await self.fetch(ConversationListing(limit=limit))
        if not isinstance(payload, list):
            raise ClaudeWebError("the conversation listing came back malformed")

        def recent() -> Iterator[ConversationMeta]:
            for entry in payload:
                try:
                    meta = ConversationMeta.model_validate(entry)
                except ValidationError:
                    logger.debug("skipping an unreadable conversation listing entry")
                    continue
                touched = meta.updated()
                stale = (
                    updated_after is not None
                    and touched is not None
                    and touched <= updated_after
                )
                if not stale:
                    yield meta

        return list(recent())

    async def conversation(self, uuid: str) -> ConversationContent:
        """One conversation, every message, as Markdown."""
        payload = await self.fetch(ConversationDetail(uuid=uuid))
        return rendered_conversation(uuid, payload)

    async def public_snapshot(self, share: str) -> JsonValue | None:
        """A share link as anyone holding it sees it, or nothing where the
        service declines to serve it without a session."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                "GET",
                f"{BASE_URL}/api/chat_snapshots/{share}?{RENDER_PARAMS}",
                headers=CLAUDE_HEADERS,
            )
        if response.status_code != 200:
            logger.debug(
                "the public snapshot endpoint answered %s", response.status_code
            )
            return None
        return response.json()

    async def snapshot(self, share: str) -> ConversationContent:
        """A conversation from a share link or a bare share id.

        Anonymously first, because a link that needs no session should not
        spend one. A snapshot the service declines to serve that way is asked
        for under each organisation the account belongs to: a share link names
        no organisation, and the one holding it need not be the one this
        account's own conversations are read under.

        Each attempt goes through :meth:`fetch` on a client pinned to that
        organisation, so a share reaches the same session refresh and the same
        refusal reading as everything else here.
        """
        identifier = share_id(share)
        if (public := await self.public_snapshot(identifier)) is not None:
            return rendered_conversation(identifier, public)

        request = SnapshotDetail(share=identifier)
        refused: ClaudeWebError | None = None
        for entry in await self.account_organizations():
            pinned = ClaudeWebClient(
                self.source, timeout=self.timeout, organization=entry.uuid
            )
            try:
                return rendered_conversation(identifier, await pinned.fetch(request))
            except ClaudeWebError as error:
                logger.debug("%s does not serve snapshot %s", entry.uuid, identifier)
                refused = error
        raise refused or ClaudeWebError(
            f"no organisation on this account serves snapshot {identifier}"
        )
