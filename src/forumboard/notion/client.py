"""The Notion workspace, as the operations this project performs on it.

Wraps ``notion-client`` rather than speaking HTTP, and narrows its untyped
replies into models at the point they arrive — so nothing downstream indexes
into a raw payload and discovers at runtime that a field moved.

Three things about the current API shape a caller should know before reading
one:

*A database is not what you query.* Since the 2025-09-03 API a database holds
one or more **data sources**, and queries, property schemas, and page parents
all name the data source. Configuration records the database id, because that
is what a person can copy out of Notion's own URL, and :meth:`data_source_of`
resolves it once.

*A page body is Markdown.* Notion accepts and returns Notion-flavored Markdown
directly, so nothing here converts to blocks — the editor's Markdown is what
gets stored.

*Shrinking a page is opt-in.* ``allow_deleting_content`` defaults to false, and
a rewrite that removes redacted material is exactly a shrink. Every rewrite
here sets it, because refusing to delete is the wrong failure for this project.
"""

import logging
from collections.abc import AsyncIterator, Awaitable

from notion_client import AsyncClient
from notion_client.errors import APIResponseError
from pydantic import BaseModel

from lup.types import JsonObject, JsonValue

from forumboard.notion.schema import DatabaseShape, PropertyMismatch, ReportedTypes

logger = logging.getLogger(__name__)


class NotionError(Exception):
    """A Notion call failed in a way the caller has to decide about."""


class NotionPerson(BaseModel, frozen=True):
    """One workspace member, as the editor sees them when resolving a name."""

    id: str
    name: str = ""

    def mention(self) -> JsonObject:
        """This person as one entry of a people property."""
        entry: JsonObject = {"object": "user", "id": self.id}
        return entry


class NotionDatabase(BaseModel, frozen=True):
    """A database and the data source its pages actually live in."""

    id: str
    data_source_id: str


class DatabaseStatus(BaseModel, frozen=True):
    """Whether one configured database is usable, and what is wrong if not."""

    label: str
    database_id: str
    data_source_id: str = ""
    unreachable: str = ""
    """Why the database could not be read at all — usually that the
    integration has not been given access to it."""
    mismatches: list[PropertyMismatch] = []

    def usable(self) -> bool:
        """Whether the pipeline can write to this database as it stands."""
        return not self.unreachable and not self.mismatches

    def describe(self) -> list[str]:
        """Lines an operator can act on, most important first."""
        if self.unreachable:
            return [f"{self.label}: {self.unreachable}"]
        if not self.mismatches:
            return [f"{self.label}: ready"]
        return [f"{self.label}: {len(self.mismatches)} property problem(s)"] + [
            f"  {mismatch.describe()}" for mismatch in self.mismatches
        ]


class NotionPage(BaseModel, frozen=True):
    """A page this project wrote or found, reduced to what callers use."""

    id: str
    url: str = ""
    properties: JsonObject = {}

    def text_property(self, name: str) -> str:
        """A title or rich-text property as plain text, empty when unset.

        Notion returns both as a list of runs, and a property that was never
        written is absent rather than empty — both read as "" here, because a
        caller asking for the text does not care which happened.
        """
        # lup: ignore[dict-get] — a page payload is open data read off the wire
        value = self.properties.get(name)
        if not isinstance(value, dict):
            return ""
        for key in ("title", "rich_text"):
            # lup: ignore[dict-get] — same open payload
            runs = value.get(key)
            if isinstance(runs, list):
                return "".join(plain_text_of(run) for run in runs)
        return ""

    def date_property(self, name: str) -> str:
        """The start of a date property, empty when the date is unset.

        Empty is load-bearing: an unset ``Merged`` is how this schema spells a
        discussion the worldview has not folded in.
        """
        # lup: ignore[dict-get] — a page payload is open data read off the wire
        value = self.properties.get(name)
        if not isinstance(value, dict):
            return ""
        # lup: ignore[dict-get] — same open payload
        return payload_text(value.get("date"), "start") or ""


def plain_text_of(run: JsonValue) -> str:
    """The text of one rich-text run, or "" when the run carries none."""
    if not isinstance(run, dict):
        return ""
    # lup: ignore[dict-get] — a rich-text run is open data read off the wire
    text = run.get("plain_text")
    return text if isinstance(text, str) else ""


def payload_text(payload: JsonValue, key: str) -> str | None:
    """A string field of a payload, or None when absent or not a string."""
    if not isinstance(payload, dict):
        return None
    # lup: ignore[dict-get] — a Notion payload is open data read off the wire
    value = payload.get(key)
    return value if isinstance(value, str) else None


def payload_list(payload: JsonValue, key: str) -> list[JsonValue]:
    """A list field of a payload, empty when absent."""
    if not isinstance(payload, dict):
        return list[JsonValue]()
    # lup: ignore[dict-get] — a Notion payload is open data read off the wire
    value = payload.get(key)
    return value if isinstance(value, list) else list[JsonValue]()


def payload_cursor(payload: JsonValue) -> str | None:
    """The next page cursor, or None when this was the last page."""
    if not isinstance(payload, dict):
        return None
    # lup: ignore[dict-get] — a Notion payload is open data read off the wire
    if payload.get("has_more") is not True:
        return None
    return payload_text(payload, "next_cursor")


def page_of(entry: JsonValue) -> NotionPage | None:
    """One query result as a page, or None when it is not one."""
    if not isinstance(entry, dict):
        return None
    identifier = payload_text(entry, "id")
    if identifier is None:
        return None
    # lup: ignore[dict-get] — a page payload is open data read off the wire
    properties = entry.get("properties")
    return NotionPage(
        id=identifier,
        url=payload_text(entry, "url") or "",
        properties=properties if isinstance(properties, dict) else {},
    )


def person_of(entry: JsonValue) -> NotionPerson | None:
    """One users-list entry as a person, or None when it is a bot."""
    if not isinstance(entry, dict):
        return None
    # lup: ignore[dict-get] — a users-list entry is open data off the wire
    if entry.get("type") != "person":
        return None
    identifier = payload_text(entry, "id")
    if identifier is None:
        return None
    return NotionPerson(id=identifier, name=payload_text(entry, "name") or "")


class NotionWorkspace:
    """Everything this project does to a Notion workspace.

    Constructed with a token rather than reading settings, so a caller can
    point one at a scratch workspace in a test without moving the environment.
    """

    def __init__(self, token: str) -> None:
        self.client = AsyncClient(auth=token)

    async def call(self, awaitable: Awaitable[JsonValue]) -> JsonValue:
        """Await one Notion call, reporting its own message when it refuses.

        ``notion-client`` raises with the API's explanation attached, and that
        explanation is what tells an operator whether the integration lacks
        access to a page or the payload was malformed — so it is carried
        through rather than replaced with a status code.
        """
        try:
            return await awaitable
        except APIResponseError as error:
            raise NotionError(f"Notion refused the request: {error}") from error

    async def whoami(self) -> str:
        """The integration's own name, which is what proves a token is live."""
        me = await self.call(self.client.users.me())
        return payload_text(me, "name") or "this integration"

    async def people(self) -> list[NotionPerson]:
        """Every human member of the workspace, bots excluded.

        This is the directory the editor resolves a spoken name against — it
        sees "ping Jacob about the migration" and has to decide which Jacob
        that is, or that it cannot tell and should drop the ping.
        """

        async def members() -> AsyncIterator[NotionPerson]:
            cursor: str | None = None
            while True:
                page = await self.call(
                    self.client.users.list(start_cursor=cursor, page_size=100)
                )
                for entry in payload_list(page, "results"):
                    person = person_of(entry)
                    if person is not None:
                        yield person
                cursor = payload_cursor(page)
                if cursor is None:
                    return

        return [person async for person in members()]

    async def create_database(
        self, parent_page_id: str, shape: DatabaseShape
    ) -> NotionDatabase:
        """Create a database under ``parent_page_id`` with the declared shape.

        The properties go in as the database's initial data source, which is
        the only way the current API accepts a schema at creation.
        """
        heading: JsonObject = {"type": "text", "text": {"content": shape.title}}
        parent: JsonObject = {"type": "page_id", "page_id": parent_page_id}
        initial: JsonObject = {"properties": shape.creation_properties()}
        created = await self.call(
            self.client.databases.create(
                parent=parent, title=[heading], initial_data_source=initial
            )
        )
        identifier = payload_text(created, "id")
        if identifier is None:
            raise NotionError(f"Notion created {shape.title!r} without returning an id")
        return NotionDatabase(
            id=identifier, data_source_id=first_data_source(created, identifier)
        )

    async def data_source_of(self, database_id: str) -> str:
        """The data source a database's pages live in.

        Configuration records database ids because that is what Notion's own
        URL shows; everything the API does with those pages names the data
        source instead, so the two are bridged in exactly this one place.
        """
        database = await self.call(self.client.databases.retrieve(database_id))
        return first_data_source(database, database_id)

    async def reported_types(self, data_source_id: str) -> ReportedTypes:
        """Each property of a live data source mapped to the type Notion reports.

        This is the half of validation Notion owns; ``DatabaseShape.mismatches``
        is the half this repository owns, and neither knows the other's content.
        """
        source = await self.call(self.client.data_sources.retrieve(data_source_id))
        if not isinstance(source, dict):
            raise NotionError(f"data source {data_source_id} reported no properties")
        # lup: ignore[dict-get] — a data source payload is open data off the wire
        properties = source.get("properties")
        if not isinstance(properties, dict):
            raise NotionError(f"data source {data_source_id} reported no properties")
        return {
            name: kind
            for name, value in properties.items()
            if isinstance(kind := payload_text(value, "type"), str)
        }

    async def inspect_database(
        self, label: str, database_id: str, shape: DatabaseShape
    ) -> DatabaseStatus:
        """Whether a configured database is usable, without changing anything.

        Reports rather than raises: an operator running a check wants every
        problem at once, not the first one.
        """
        try:
            data_source_id = await self.data_source_of(database_id)
            reported = await self.reported_types(data_source_id)
        except NotionError as error:
            return DatabaseStatus(
                label=label, database_id=database_id, unreachable=str(error)
            )
        return DatabaseStatus(
            label=label,
            database_id=database_id,
            data_source_id=data_source_id,
            mismatches=shape.mismatches(reported),
        )

    async def query(
        self, data_source_id: str, query_filter: JsonObject | None = None
    ) -> list[NotionPage]:
        """Every page a filter matches, following Notion's pagination itself."""

        async def matches() -> AsyncIterator[NotionPage]:
            cursor: str | None = None
            while True:
                page = await self.call(
                    self.client.data_sources.query(
                        data_source_id,
                        filter=query_filter,
                        start_cursor=cursor,
                        page_size=100,
                    )
                )
                for entry in payload_list(page, "results"):
                    found = page_of(entry)
                    if found is not None:
                        yield found
                cursor = payload_cursor(page)
                if cursor is None:
                    return

        return [found async for found in matches()]

    async def create_page(
        self, data_source_id: str, properties: JsonObject, markdown: str
    ) -> NotionPage:
        """Create a page whose body is ``markdown``."""
        parent: JsonObject = {
            "type": "data_source_id",
            "data_source_id": data_source_id,
        }
        created = await self.call(
            self.client.pages.create(
                parent=parent, properties=properties, markdown=markdown
            )
        )
        page = page_of(created)
        if page is None:
            raise NotionError("Notion created a page without returning it")
        return page

    async def update_properties(self, page_id: str, properties: JsonObject) -> None:
        """Change a page's properties, leaving its body alone."""
        await self.call(self.client.pages.update(page_id, properties=properties))

    async def replace_body(self, page_id: str, markdown: str) -> None:
        """Replace a page's whole body with ``markdown``.

        ``allow_deleting_content`` is set because a rewrite that removes
        redacted material is a shrink, and Notion refuses a shrink by default.
        Leaving it unset would turn every successful redaction into a silent
        no-op — the one failure this project must not have.
        """
        replacement: JsonObject = {
            "new_str": markdown,
            "allow_deleting_content": True,
        }
        await self.call(
            self.client.pages.update_markdown(
                page_id, type="replace_content", replace_content=replacement
            )
        )

    async def page_markdown(self, page_id: str) -> str:
        """A page's current body as Markdown, for a rewrite that reads first."""
        body = await self.call(self.client.pages.retrieve_markdown(page_id))
        return payload_text(body, "markdown") or ""


def first_data_source(database: JsonValue, database_id: str) -> str:
    """The id of a database's first data source.

    A database can hold several. This project creates single-source databases
    and reads the first of any it is pointed at, which is the one a database
    made through Notion's own interface has.
    """
    for entry in payload_list(database, "data_sources"):
        identifier = payload_text(entry, "id")
        if identifier is not None:
            return identifier
    raise NotionError(
        f"database {database_id} exposes no data source; the integration may "
        "lack access to it, or it may be a linked view rather than a database"
    )
