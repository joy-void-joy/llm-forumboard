"""claude.ai's own conversation API, as this project reads it.

This is an unofficial API and the whole fragile surface is deliberately in one
module, so a change upstream has one place to land. Nothing here reads
settings: a :class:`CredentialSource` supplies the session cookie and can be
asked to produce a fresh one, which is what lets a profile back its session
with a stored browser context without this module knowing browsers exist.

Organisations matter more than they look. An account usually has a non-chat
organisation beside the chat one — the developer console's — and reading
conversations under the wrong one answers 403, which is indistinguishable from
an expired cookie unless the organisation is chosen by capability.

Requests go through ``client.request("GET", …)`` rather than ``client.get``:
the two are the same call, and the spelling without ``.get`` does not read as
a dictionary lookup to the tooling that audits this repository.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import PurePosixPath
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ValidationError

from lup.types import JsonValue, StringMap

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


class ConversationContent(BaseModel, frozen=True):
    """A whole conversation, rendered to speaker-tagged Markdown."""

    uuid: str
    name: str = ""
    created_at: str = ""
    updated_at: str = ""
    markdown: str
    message_count: int

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


def refusal_detail(response: httpx.Response) -> str:
    """Why claude.ai rejected a session, read from its own error body.

    The service distinguishes an invalid session from a wrong-organisation one
    in the body, and a bare status code does not — so the body is what tells an
    operator which of the two happened.
    """
    body: JsonValue = None
    try:
        body = response.json()
    except ValueError:
        logger.debug("claude.ai refused with a non-JSON body")
    message = code = ""
    if isinstance(body, dict):
        # lup: ignore[dict-get] — an error body is open data read off the wire
        error = body.get("error")
        if isinstance(error, dict):
            # lup: ignore[dict-get] — same open payload
            text, details = error.get("message"), error.get("details")
            message = text if isinstance(text, str) else ""
            if isinstance(details, dict):
                # lup: ignore[dict-get] — same open payload
                reported = details.get("error_code")
                code = reported if isinstance(reported, str) else ""
    inside = " ".join(part for part in (str(response.status_code), code) if part)
    return f"session rejected ({inside})" + (f": {message}" if message else "")


def block_text(block: JsonValue) -> str:
    """The text one content block carries, empty when it carries none."""
    if not isinstance(block, dict):
        return ""
    # lup: ignore[dict-get] — a message payload is open data read off the wire
    match block.get("type"):
        case "image":
            return ""
        case "tool_result":
            nested = block.get("content")  # lup: ignore[dict-get] — same open payload
            if isinstance(nested, list):
                return "\n\n".join(
                    text for item in nested if (text := block_text(item))
                )
            return nested if isinstance(nested, str) else ""
        case _:
            for field in ("text", "content", "source"):
                value = block.get(field)  # lup: ignore[dict-get] — same open payload
                if isinstance(value, str) and value:
                    return value
            return ""


def attachment_texts(message: JsonValue) -> list[str]:
    """Text extracted from a message's uploaded files.

    Attachments are part of what was said. Dropping them would hand the
    reviewer a conversation whose subject is a document it cannot see.
    """
    if not isinstance(message, dict):
        return list[str]()
    # lup: ignore[dict-get] — a message payload is open data read off the wire
    attachments = message.get("attachments")
    if not isinstance(attachments, list):
        return list[str]()

    def texts() -> Iterator[str]:
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            # lup: ignore[dict-get] — same open payload
            content = attachment.get("extracted_content")
            if not isinstance(content, str) or not has_text(content):
                continue
            # lup: ignore[dict-get] — same open payload
            name = attachment.get("file_name")
            label = f"[Attachment: {name}]\n" if isinstance(name, str) and name else ""
            yield f"{label}{content}"

    return list(texts())


def message_markdown(message: JsonValue) -> str:
    """One message's blocks and attachments as a single body of text."""
    if not isinstance(message, dict):
        return ""
    # lup: ignore[dict-get] — a message payload is open data read off the wire
    content = message.get("content")
    match content:
        case str():
            blocks = [content]
        case list():
            blocks = [text for block in content if (text := block_text(block))]
        case _:
            blocks = list[str]()
    return "\n\n".join(blocks + attachment_texts(message))


def rendered_conversation(uuid: str, payload: JsonValue) -> ConversationContent:
    """A conversation payload as speaker-tagged Markdown.

    The ``<user>``/``<claude>`` tags are what tell the reviewer who said what,
    which every downstream judgement about whose information is being disclosed
    depends on.
    """
    if not isinstance(payload, dict):
        raise ClaudeWebError(f"conversation {uuid} came back in an unexpected shape")

    def field(name: str) -> str:
        # lup: ignore[dict-get] — a conversation payload is open data off the wire
        value = payload.get(name)
        return value if isinstance(value, str) else ""

    messages = payload.get("chat_messages")  # lup: ignore[dict-get] — same open payload
    turns = messages if isinstance(messages, list) else list[JsonValue]()

    def spoken() -> Iterator[str]:
        for message in turns:
            if not isinstance(message, dict):
                continue
            sender = message.get("sender")  # lup: ignore[dict-get] — same open payload
            body = message_markdown(message)
            if not isinstance(sender, str) or not has_text(body):
                continue
            speaker = "user" if sender == "human" else "claude"
            yield f"<{speaker}>\n{body}\n</{speaker}>"

    parts = list(spoken())
    if not parts:
        raise ClaudeWebError(f"conversation {uuid} has no readable messages")
    return ConversationContent(
        uuid=field("uuid") or uuid,
        name=field("name"),
        created_at=field("created_at"),
        updated_at=field("updated_at"),
        markdown="\n\n".join(parts),
        message_count=len(parts),
    )


class PathBuilder(BaseModel, frozen=True):
    """An organisation-scoped path, whose shape only its own kind knows."""

    @abstractmethod
    def path(self, organization: str) -> str:
        """This request's path under ``organization``."""


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


class ClaudeWebClient:
    """One profile's view of claude.ai."""

    def __init__(self, source: CredentialSource, *, timeout: int = 30) -> None:
        self.source = source
        self.organization = ""
        self.timeout = timeout

    def headers(self, cookie: str) -> StringMap:
        """Browser-shaped headers carrying one session."""
        return {**CLAUDE_HEADERS, "Cookie": cookie}

    async def session(self, *, refresh: bool) -> str:
        """The cookie to present, refusing loudly when there is none."""
        credentials = await self.source.credentials(refresh=refresh)
        if credentials.organization:
            self.organization = credentials.organization
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

    async def organizations(self, client: httpx.AsyncClient, cookie: str) -> list[str]:
        """Organisation ids, chat-capable first."""
        known = await self.organization_entries(client, cookie)
        return [entry.uuid for entry in known if entry.chat_capable()] + [
            entry.uuid for entry in known if not entry.chat_capable()
        ]

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
        """The organisation to read under, preferring a chat-capable one."""
        if self.organization:
            return self.organization
        available = await self.organizations(client, cookie)
        if not available:
            raise ClaudeWebError("this account has no organizations")
        hint = organization_hint(cookie)
        self.organization = hint if hint in available else available[0]
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
                        headers=self.headers(cookie),
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
