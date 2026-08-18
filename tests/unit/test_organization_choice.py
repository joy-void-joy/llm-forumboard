"""Which organisation a session reads conversations under.

An account usually holds a second organisation beside the one it chats in —
the developer console's. Reading under that one answers an empty listing or a
403, and both look exactly like a quiet week or an expired cookie from the
outside. So the choice is made by capability, and a hint saying which
organisation the browser was last in is honoured only where it names one that
carries chat.

The transport is faked rather than the resolution's own helpers, so the real
listing is parsed and the real hint is read out of a real Cookie header.
"""

import httpx
import pytest

from forumboard.claudeai.client import (
    ClaudeCredentials,
    ClaudeWebClient,
    ClaudeWebError,
    OrganizationEntry,
    StaticCredentials,
)

CONSOLE = "11111111-1111-1111-1111-111111111111"
CHATTING = "22222222-2222-2222-2222-222222222222"
ALSO_CHATTING = "33333333-3333-3333-3333-333333333333"


def account(*entries: OrganizationEntry) -> httpx.AsyncClient:
    """A transport answering the organisation listing with exactly these."""

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[entry.model_dump() for entry in entries])

    return httpx.AsyncClient(transport=httpx.MockTransport(answer))


def console(uuid: str = CONSOLE) -> OrganizationEntry:
    """An organisation with no chat under it, as an API-only one reports."""
    return OrganizationEntry(uuid=uuid, name="Console", capabilities=["api"])


def chats(uuid: str = CHATTING) -> OrganizationEntry:
    """An organisation conversations can be read under."""
    return OrganizationEntry(uuid=uuid, name="Chat", capabilities=["chat", "api"])


def signed_in(hint: str = "") -> ClaudeWebClient:
    """A client whose credential source offers ``hint`` as its organisation."""
    return ClaudeWebClient(
        StaticCredentials(ClaudeCredentials(cookie="sessionKey=x", organization=hint))
    )


async def resolved(
    client: ClaudeWebClient, cookie: str, *entries: OrganizationEntry
) -> str:
    """What ``client`` reads under, given an account holding ``entries``."""
    async with account(*entries) as transport:
        return await client.resolve_organization(transport, cookie)


async def test_a_chatting_organization_is_chosen_over_the_console_one() -> None:
    """The default with no hint at all: capability decides."""
    client = signed_in()
    assert await resolved(client, "sessionKey=x", console(), chats()) == CHATTING


async def test_a_credential_hint_naming_a_chat_organization_is_honoured() -> None:
    """Two chat organisations, and the hint says which one they were in."""
    client = signed_in(ALSO_CHATTING)
    chosen = await resolved(
        client, "sessionKey=x", chats(), chats(ALSO_CHATTING), console()
    )
    assert chosen == ALSO_CHATTING


async def test_a_credential_hint_naming_the_console_organization_is_ignored() -> None:
    """The bug this pins: a stale cookie must not send a sync somewhere quiet."""
    client = signed_in(CONSOLE)
    assert await resolved(client, "sessionKey=x", console(), chats()) == CHATTING


async def test_a_cookie_hint_naming_the_console_organization_is_ignored() -> None:
    """The same rule for the hint the Cookie header carries itself."""
    client = signed_in()
    cookie = f"sessionKey=x; lastActiveOrg={CONSOLE}"
    assert await resolved(client, cookie, console(), chats()) == CHATTING


async def test_a_cookie_hint_naming_a_chat_organization_is_honoured() -> None:
    client = signed_in()
    cookie = f"sessionKey=x; lastActiveOrg={ALSO_CHATTING}"
    chosen = await resolved(client, cookie, chats(), chats(ALSO_CHATTING))
    assert chosen == ALSO_CHATTING


async def test_an_account_chatting_nowhere_still_reads_somewhere() -> None:
    """No chat capability anywhere is a claim about the account, not a refusal.

    The listing that follows says what it says, and a refusal here would turn
    an account whose capabilities are reported differently into a dead sync.
    """
    client = signed_in()
    assert await resolved(client, "sessionKey=x", console()) == CONSOLE


async def test_an_account_with_no_organizations_refuses() -> None:
    client = signed_in()
    with pytest.raises(ClaudeWebError, match="no organizations"):
        await resolved(client, "sessionKey=x")


async def test_a_pinned_organization_is_read_under_whatever_it_carries() -> None:
    """A diagnostic naming one organisation is answered about that one."""
    client = ClaudeWebClient(
        StaticCredentials(ClaudeCredentials(cookie="sessionKey=x")),
        organization=CONSOLE,
    )
    assert await resolved(client, "sessionKey=x", console(), chats()) == CONSOLE


async def test_a_resolved_organization_is_kept_rather_than_asked_again() -> None:
    """Resolution is per client, and the listing is not re-read for each fetch."""
    client = signed_in()
    assert await resolved(client, "sessionKey=x", console(), chats()) == CHATTING
    assert client.organization == CHATTING
    assert await resolved(client, "sessionKey=x", console()) == CHATTING
