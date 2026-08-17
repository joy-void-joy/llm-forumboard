"""Everything a pass needs of the outside world, opened once.

Resolving configuration into live clients happens here and nowhere else, so a
pass takes a context rather than reading settings. That is what lets a test
hand one a scratch workspace, and what keeps the number of Notion round trips
honest: the people directory is read once per pass rather than once per name.

Opening is fallible on purpose. A misconfigured database is reported as a
refusal naming the fix, not as a ``None`` that fails three calls later.
"""

import logging

from pydantic import BaseModel

from forumboard.agent.config import settings
from forumboard.notion.client import NotionError, NotionWorkspace
from forumboard.notion.discussions import DiscussionsDatabase
from forumboard.notion.people import PeopleDirectory
from forumboard.notion.schema import DatabaseShapes
from forumboard.notion.worldview import WorldviewDatabase

logger = logging.getLogger(__name__)


class NotConfigured(Exception):
    """Something required has not been set up, and the message says what."""


class ForumboardContext(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """One pass's live view of Notion."""

    workspace: NotionWorkspace
    discussions: DiscussionsDatabase
    worldview: WorldviewDatabase
    people: PeopleDirectory


async def open_context() -> ForumboardContext:
    """Resolve settings into live clients, refusing with the fix when it cannot.

    Both database ids are resolved to their data source ids here, in one place,
    because that translation is the single thing a caller would otherwise have
    to remember about the current Notion API.
    """
    if not settings.notion_token:
        raise NotConfigured(
            "NOTION_TOKEN is not set. Run `forumboard setup notion` to create "
            "an integration and record it in .env.local."
        )
    missing = [
        name
        for name, value in (
            ("NOTION_DISCUSSIONS_DATABASE_ID", settings.notion_discussions_database_id),
            ("NOTION_WORLDVIEW_DATABASE_ID", settings.notion_worldview_database_id),
        )
        if not value
    ]
    if missing:
        raise NotConfigured(
            f"{', '.join(missing)} not set. Run `forumboard setup notion` to "
            "create the databases and record their ids."
        )

    workspace = NotionWorkspace(settings.notion_token)
    try:
        discussions_source = await workspace.data_source_of(
            settings.notion_discussions_database_id or ""
        )
        worldview_source = await workspace.data_source_of(
            settings.notion_worldview_database_id or ""
        )
        members = await workspace.people()
    except NotionError as error:
        raise NotConfigured(
            f"{error} Check that the integration has been given access to both "
            "databases — Notion grants none by default."
        ) from error

    return ForumboardContext(
        workspace=workspace,
        discussions=DiscussionsDatabase(workspace, discussions_source),
        worldview=WorldviewDatabase(workspace, worldview_source),
        people=PeopleDirectory(members=members),
    )


def browser_report() -> list[str]:
    """Whether a login could open a browser, which the sync depends on.

    Reported beside Notion because the two failures look nothing alike and
    both stop the same pipeline: without the browser no profile can sign in,
    so every enrolled profile syncs nothing and says only that its session
    expired.
    """
    from forumboard.devtools.setup import browser_installed

    if browser_installed():
        return ["login browser: Chromium installed"]
    return [
        "login browser: NOT installed — run `forumboard setup browser`; "
        "without it no profile can sign in to claude.ai"
    ]


def roster_report() -> list[str]:
    """Who a sync pass will actually read.

    Reported beside Notion because a deployment can be green everywhere else
    and still publish nothing: enrolment is a committed file rather than a
    setting, so nothing in the environment says whether anybody is in it.
    """
    from lup.workspace.paths import project_root

    from forumboard.profiles import profile_states, profiles_root

    root = project_root()
    states = profile_states(root, profiles_root(root))
    syncable = [state for state in states if state.syncable()]
    if syncable:
        return [f"profiles: reading {', '.join(state.name for state in syncable)}"]
    return [
        "profiles: NOT reading anybody — run `forumboard profile edit` to sign "
        "somebody in and enrol them",
        *(f"  {state.describe()}" for state in states),
    ]


async def configuration_report() -> list[str]:
    """Lines describing whether this deployment can run, for a doctor command.

    Reports rather than raises: somebody checking their setup wants every
    problem at once, and the first one is rarely the only one.
    """
    local = [*browser_report(), *roster_report()]
    if not settings.notion_token:
        return [*local, "NOTION_TOKEN is not set — run `forumboard setup notion`"]
    workspace = NotionWorkspace(settings.notion_token)
    shapes = DatabaseShapes()
    try:
        identity = await workspace.whoami()
    except NotionError as error:
        return [*local, f"the token was rejected: {error}"]

    lines = [*local, f"connected as {identity}"]
    for label, database_id, shape in (
        ("Discussions", settings.notion_discussions_database_id, shapes.discussions),
        ("Worldview", settings.notion_worldview_database_id, shapes.worldview),
    ):
        if not database_id:
            lines.append(f"{label}: no database id configured")
            continue
        status = await workspace.inspect_database(label, database_id, shape)
        lines.extend(status.describe())
    return lines
