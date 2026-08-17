"""The shape of the two Notion databases, declared once.

A database is described here as typed properties, and both things anyone needs
of that description are rendered from it: the payload that *creates* the
database and the comparison that *validates* one somebody else made. Writing
the two by hand would let them disagree, and the disagreement would surface as
a page that silently fails to save a field.

Each property answers for its own Notion spelling, so adding a kind means
adding a member here and nothing anywhere else.
"""

from abc import abstractmethod
from collections.abc import Iterator, Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, Discriminator, Field

from lup.types import JsonObject

# lup: ignore[dict-str-payload] — a live database's property name to the type
# Notion reports for it, which is open data read off the wire
type ReportedTypes = Mapping[str, str]


class PropertyShape(BaseModel, frozen=True):
    """One database property: what it is called, why, and how Notion spells it."""

    name: str
    purpose: str
    """Shown by the setup wizard, so somebody creating the database by hand
    knows what the column is for rather than only what type it is."""

    @property
    @abstractmethod
    def notion_type(self) -> str:
        """The ``type`` Notion reports for this property when reading it back."""

    def creation_payload(self) -> JsonObject:
        """This property as the create-a-database call wants it.

        Most types carry no configuration, so an empty object under the type
        name is the shape; a member with options overrides this.
        """
        return {self.notion_type: {}}


class TitleShape(PropertyShape, frozen=True):
    """The page title. Notion gives every database exactly one."""

    kind: Literal["title"] = "title"

    @property
    def notion_type(self) -> str:
        return "title"


class TextShape(PropertyShape, frozen=True):
    """Free text — an identifier we carry, a list of page ids."""

    kind: Literal["text"] = "text"

    @property
    def notion_type(self) -> str:
        return "rich_text"


class DateShape(PropertyShape, frozen=True):
    """A timestamp. An empty one is meaningful: ``Merged`` reads as unmerged."""

    kind: Literal["date"] = "date"

    @property
    def notion_type(self) -> str:
        return "date"


class PeopleShape(PropertyShape, frozen=True):
    """Notion workspace members, so an assignment reaches their notifications."""

    kind: Literal["people"] = "people"

    @property
    def notion_type(self) -> str:
        return "people"


class NumberShape(PropertyShape, frozen=True):
    """A count — how many passages the editor removed, for instance."""

    kind: Literal["number"] = "number"

    @property
    def notion_type(self) -> str:
        return "number"


class SelectShape(PropertyShape, frozen=True):
    """One value from a closed vocabulary this repository defines."""

    kind: Literal["select"] = "select"
    options: list[str]

    @property
    def notion_type(self) -> str:
        return "select"

    def creation_payload(self) -> JsonObject:
        return {"select": {"options": [{"name": option} for option in self.options]}}


class MultiSelectShape(PropertyShape, frozen=True):
    """Open-ended tags the agent coins as it goes, so no options are declared."""

    kind: Literal["multi_select"] = "multi_select"

    @property
    def notion_type(self) -> str:
        return "multi_select"


type AnyPropertyShape = Annotated[
    TitleShape
    | TextShape
    | DateShape
    | PeopleShape
    | NumberShape
    | SelectShape
    | MultiSelectShape,
    Discriminator("kind"),
]


class PropertyMismatch(BaseModel, frozen=True):
    """One way a live database departs from what this repository expects."""

    property_name: str
    expected: str
    found: str | None
    """``None`` when the property is absent rather than the wrong type."""

    def describe(self) -> str:
        """A line naming the fix, which is what the operator actually needs."""
        if self.found is None:
            return f"{self.property_name!r} is missing (add it as {self.expected})"
        return (
            f"{self.property_name!r} is {self.found}, expected {self.expected} "
            "(change the property type, or rename the existing column aside)"
        )


class DatabaseShape(BaseModel, frozen=True):
    """A whole database: its title, what it holds, and every property."""

    title: str
    purpose: str
    properties: list[AnyPropertyShape]

    def creation_properties(self) -> JsonObject:
        """Every property as one create-a-database payload."""
        return {shape.name: shape.creation_payload() for shape in self.properties}

    def mismatches(self, reported: ReportedTypes) -> list[PropertyMismatch]:
        """How a live database's property types depart from this shape.

        Extra properties are not mismatches: somebody may have added their own
        column, and nothing here writes to one it does not know about.
        """

        def departures() -> Iterator[PropertyMismatch]:
            for shape in self.properties:
                # lup: ignore[dict-get] — an open map read off the wire
                reported_type = reported.get(shape.name)
                if reported_type != shape.notion_type:
                    yield PropertyMismatch(
                        property_name=shape.name,
                        expected=shape.notion_type,
                        found=reported_type,
                    )

        return list(departures())


class DatabaseShapes(BaseModel, frozen=True):
    """Both databases this project writes, so a caller takes them together."""

    discussions: DatabaseShape = Field(
        default=DatabaseShape(
            title="AI Discussions",
            purpose=(
                "One page per reviewed claude.ai conversation, plus the "
                "periodic briefings. Rewritten in place whenever the "
                "conversation it came from grows."
            ),
            properties=[
                TitleShape(name="Title", purpose="What the discussion was about"),
                TextShape(
                    name="Conversation",
                    purpose=(
                        "claude.ai conversation id — the key that decides "
                        "whether a pass creates a page or rewrites one"
                    ),
                ),
                SelectShape(
                    name="Kind",
                    purpose="Whether this page is a discussion or a briefing",
                    options=["discussion", "briefing"],
                ),
                TextShape(
                    name="Profile",
                    purpose="Which enrolled profile the conversation came from",
                ),
                DateShape(name="Started", purpose="When the conversation began"),
                DateShape(name="Updated", purpose="Its last message, upstream"),
                DateShape(name="Published", purpose="When this page was last written"),
                DateShape(
                    name="Merged",
                    purpose=(
                        "When the worldview last folded this in. Empty means "
                        "unmerged, which is what the worldview pass queries for"
                    ),
                ),
                PeopleShape(
                    name="Attention",
                    purpose="People named in the conversation who should read this",
                ),
                MultiSelectShape(
                    name="Topics", purpose="Topics the editor said this belongs to"
                ),
                NumberShape(
                    name="Redactions",
                    purpose=(
                        "How many passages the editor removed — a number that "
                        "should never be reconstructible from the page itself"
                    ),
                ),
            ],
        )
    )

    worldview: DatabaseShape = Field(
        default=DatabaseShape(
            title="Current Worldview",
            purpose=(
                "One page per topic, rewritten whole on every pass. What is "
                "happening now, what is waiting on us, what each person needs "
                "to know — not a log of what happened."
            ),
            properties=[
                TitleShape(name="Topic", purpose="What this page is about"),
                TextShape(
                    name="Slug",
                    purpose="Stable identity, so a rewrite finds its own page",
                ),
                DateShape(name="Updated", purpose="When this topic was last rewritten"),
                PeopleShape(
                    name="Attention", purpose="Who should look at this topic now"
                ),
                TextShape(
                    name="Sources",
                    purpose="Discussion page ids this rewrite drew on",
                ),
                MultiSelectShape(
                    name="Tags", purpose="Cross-cutting tags the agent coins"
                ),
            ],
        )
    )
