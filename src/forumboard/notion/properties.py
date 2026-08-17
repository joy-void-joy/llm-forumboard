"""Property values, in the shapes Notion writes them as.

One builder per property kind, so a caller assembling a page names what it is
setting rather than nesting three dictionaries and hoping. Reading the same
values back is :class:`forumboard.notion.client.NotionPage`'s job.

A ``None`` date is written as an explicitly empty date rather than omitted:
omitting the key leaves whatever was there, and clearing ``Merged`` is how a
rewritten discussion asks the worldview to look at it again.
"""

from datetime import datetime

from lup.types import JsonObject, JsonValue


def title_value(text: str) -> JsonObject:
    """A title property. Notion gives every data source exactly one."""
    run: JsonObject = {"type": "text", "text": {"content": text}}
    value: JsonObject = {"title": [run]}
    return value


def text_value(text: str) -> JsonObject:
    """A rich-text property, written as a single unformatted run."""
    run: JsonObject = {"type": "text", "text": {"content": text}}
    value: JsonObject = {"rich_text": [run]}
    return value


def date_value(moment: datetime | None) -> JsonObject:
    """A date property, or an explicitly empty one when ``moment`` is None."""
    if moment is None:
        value: JsonObject = {"date": None}
        return value
    start: JsonObject = {"start": moment.isoformat()}
    dated: JsonObject = {"date": start}
    return dated


def people_value(mentions: list[JsonObject]) -> JsonObject:
    """A people property from mentions built by ``NotionPerson.mention``."""
    value: JsonObject = {"people": list[JsonValue](mentions)}
    return value


def select_value(option: str) -> JsonObject:
    """A select property, from the closed vocabulary the schema declares."""
    chosen: JsonObject = {"name": option}
    value: JsonObject = {"select": chosen}
    return value


def multi_select_value(options: list[str]) -> JsonObject:
    """A multi-select property. Notion creates options it has not seen."""
    chosen: list[JsonValue] = [{"name": option} for option in options]
    value: JsonObject = {"multi_select": chosen}
    return value


def number_value(count: int) -> JsonObject:
    """A number property."""
    value: JsonObject = {"number": count}
    return value


def is_empty_filter(property_name: str) -> JsonObject:
    """A filter matching pages whose date property is unset.

    This is what "unmerged" means in the discussion database: the worldview
    stamps ``Merged`` when it folds a page in, so an empty one is outstanding
    work rather than a missing field.
    """
    condition: JsonObject = {"is_empty": True}
    query: JsonObject = {"property": property_name, "date": condition}
    return query


def on_or_after_filter(property_name: str, moment: datetime) -> JsonObject:
    """A filter matching pages whose date property is at or after ``moment``."""
    condition: JsonObject = {"on_or_after": moment.isoformat()}
    query: JsonObject = {"property": property_name, "date": condition}
    return query


def equals_filter(property_name: str, text: str) -> JsonObject:
    """A filter matching pages whose rich-text property is exactly ``text``."""
    condition: JsonObject = {"equals": text}
    query: JsonObject = {"property": property_name, "rich_text": condition}
    return query


def select_is_filter(property_name: str, option: str) -> JsonObject:
    """A filter matching pages whose select property is ``option``."""
    condition: JsonObject = {"equals": option}
    query: JsonObject = {"property": property_name, "select": condition}
    return query


def all_of(*clauses: JsonObject) -> JsonObject:
    """Every clause must match."""
    query: JsonObject = {"and": list[JsonValue](clauses)}
    return query
