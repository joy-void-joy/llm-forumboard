"""This project's setup integrations, over the reusable wizard framework.

The framework — env helpers, the status display, the wizard flow, and the
``Integration`` registry — lives in :mod:`lup.devtools.setup`. This module
holds only what *this* project configures, and composes the two into the
``setup`` command tree.

There is really one integration, and it is not a token prompt. Notion needs a
person to do three things in a browser that no API can do for them: create an
integration, make a page for the databases to live under, and grant the
integration access to that page. So the flow walks those, then creates both
databases itself with the schema this repository declares — because a database
somebody assembled by hand from a list of column names is where a
silently-unsaved property comes from.

Usage::

    $ uv run lup-devtools setup            # Full walkthrough
    $ uv run lup-devtools setup status     # Show what's configured
    $ uv run lup-devtools setup notion     # Just Notion
"""

import asyncio
import sys
from importlib.util import find_spec
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse
from uuid import UUID
from zoneinfo import ZoneInfoNotFoundError

import sh
import typer
from tzlocal import get_localzone_name

from lup.devtools.setup import (
    Integration,
    IntegrationStatus,
    console,
    create_setup_app,
    mask,
    open_browser,
    read_env_local,
)
from lup.types import EnvVars

from forumboard.agent.config import settings
from forumboard.devtools.harness.composition import profile_directory
from forumboard.notion.client import NotionError, NotionWorkspace
from forumboard.notion.schema import DatabaseShape, DatabaseShapes

# lup: ignore[constant-declaration] — Notion's own page, where a person has to go
INTEGRATIONS_URL = "https://www.notion.so/profile/integrations/internal"


def no_changes() -> EnvVars:
    """What a step returns when it was skipped or declined."""
    # lup: ignore[dict-str-payload] — environment variables, an open name map
    return dict[str, str]()


def detect_system_timezone() -> str:
    """The system's IANA timezone name, or "" when it cannot be determined.

    Delegates to ``tzlocal``, which reads the right source for each platform
    instead of string-munging just one.
    """
    try:
        return get_localzone_name()
    except ZoneInfoNotFoundError:
        return ""


def page_id_of(value: str) -> str:
    """A Notion page id, from an id or from the page's URL.

    A Notion URL ends in the page's id with the title in front of it, so the
    last path segment's final 32 characters are the id. ``UUID`` is what says
    whether that is true of what somebody actually pasted, rather than a guess
    that produces a plausible-looking id from the wrong URL.
    """
    segment = PurePosixPath(urlparse(value).path or value).name or value
    try:
        return str(UUID(segment[-32:]))
    except ValueError as error:
        raise ValueError(
            f"{value!r} does not end in a Notion page id — open the page in "
            "Notion and copy its URL, or paste the 32-character id itself"
        ) from error


def describe_shape(shape: DatabaseShape) -> None:
    """Print what a database is for and every column it was given."""
    console.print(f"    [dim]{shape.purpose}[/]")
    for prop in shape.properties:
        console.print(
            f"    [dim]· {prop.name} ({prop.notion_type}) — {prop.purpose}[/]"
        )


async def create_databases(token: str, parent: str) -> EnvVars:
    """Create both databases under ``parent``, reporting what each holds."""
    workspace = NotionWorkspace(token)
    shapes = DatabaseShapes()

    async def made(shape: DatabaseShape) -> str:
        database = await workspace.create_database(parent, shape)
        console.print(f"  [green]Created[/] {shape.title} — {database.id}")
        describe_shape(shape)
        return database.id

    return {
        "NOTION_DISCUSSIONS_DATABASE_ID": await made(shapes.discussions),
        "NOTION_WORLDVIEW_DATABASE_ID": await made(shapes.worldview),
    }


def setup_notion() -> EnvVars:
    """Walk somebody through Notion, then build the databases for them."""
    console.rule("[bold]Notion[/]")
    env = read_env_local()
    existing = env.get("NOTION_TOKEN", "")  # lup: ignore[dict-get] — an open env map
    if existing:
        console.print(f"  Already configured: {mask(existing)}")
        if not typer.confirm("  Reconfigure?", default=False):
            return no_changes()

    console.print(
        "\n  [bold]1. Create an integration[/]\n"
        "     Keep the type as [bold]Internal[/]. Give it a name you will\n"
        "     recognise later — it appears on every page it touches.\n"
    )
    open_browser(INTEGRATIONS_URL)
    token = typer.prompt("  Internal Integration Secret (ntn_… or secret_…)")
    if not token:
        console.print("  [yellow]Skipped.[/]")
        return no_changes()

    workspace = NotionWorkspace(token)
    try:
        identity = asyncio.run(workspace.whoami())
    except NotionError as error:
        console.print(f"  [red]That token was rejected:[/] {error}")
        raise typer.Exit(1) from error
    console.print(f"  [green]Connected[/] as {identity}")

    console.print(
        "\n  [bold]2. Make a page for the databases to live under[/]\n"
        "     Anywhere in your workspace. It only needs to exist.\n"
        "\n  [bold]3. Grant the integration access to it[/]\n"
        "     Open the page → ⋯ menu → Connections → add the integration you\n"
        "     just made. Notion grants an integration nothing by default, so\n"
        "     this step is what makes the next one possible.\n"
    )
    pasted = typer.prompt("  Paste that page's URL (or its id)")
    try:
        parent = page_id_of(pasted)
    except ValueError as error:
        console.print(f"  [red]{error}[/]")
        raise typer.Exit(1) from error

    console.print("\n  Creating the databases…")
    try:
        created = asyncio.run(create_databases(token, parent))
    except NotionError as error:
        console.print(
            f"  [red]{error}[/]\n"
            "  The usual cause is step 3 — the integration cannot see that page."
        )
        raise typer.Exit(1) from error

    console.print("\n  [green]Done.[/] Check it any time with `forumboard doctor`.")
    return {"NOTION_TOKEN": token, "NOTION_PARENT_PAGE_ID": parent, **created}


def notion_status(env: EnvVars) -> IntegrationStatus:
    """Whether Notion is configured far enough to publish anything."""
    token = env.get("NOTION_TOKEN", "")  # lup: ignore[dict-get] — an open env map
    if not token:
        return IntegrationStatus(ok=False, detail="not configured")
    missing = [
        key
        for key in ("NOTION_DISCUSSIONS_DATABASE_ID", "NOTION_WORLDVIEW_DATABASE_ID")
        if not env.get(key)  # lup: ignore[dict-get] — same open env map
    ]
    if missing:
        return IntegrationStatus(ok=False, detail=f"no {', '.join(missing)}")
    return IntegrationStatus(ok=True, detail=mask(token))


def browser_registry() -> Path:
    """Where Playwright keeps the browsers it downloads.

    ``PLAYWRIGHT_BROWSERS_PATH`` overrides it, which is how a deployment puts
    them somewhere a service account can read. It is read through settings, so
    this check and the browser it checks for cannot disagree about the place.
    """
    if settings.playwright_browsers_path is not None:
        return settings.playwright_browsers_path
    match sys.platform:
        case "darwin":
            return Path.home() / "Library" / "Caches" / "ms-playwright"
        case "win32":
            return Path.home() / "AppData" / "Local" / "ms-playwright"
        case _:
            return Path.home() / ".cache" / "ms-playwright"


def browser_installed() -> bool:
    """Whether a Chromium has been downloaded for Playwright to drive.

    Read off the registry directory rather than by asking Playwright: the
    answer wanted here is for a status line, and starting the Node driver to
    get it costs a subprocess and leaves asyncio teardown noise on the terminal.

    This says a Chromium is present, not that it is the revision this
    Playwright wants. That is the right granularity for a status line, and
    `setup browser` reinstalls either way — the installer is idempotent and
    quick when the current revision is already there.
    """
    if find_spec("playwright.async_api") is None:
        return False
    registry = browser_registry()
    return registry.is_dir() and any(registry.glob("chromium-*"))


def setup_browser() -> EnvVars:
    """Install the browser a claude.ai login runs in.

    This is a download rather than a setting, so it writes no environment at
    all. It is in the wizard because it is a precondition for reading anybody's
    conversations, and a missing browser otherwise surfaces at the first login
    as a Playwright error nobody expected.
    """
    console.rule("[bold]Login browser[/]")
    if browser_installed():
        console.print("  [green]Chromium is installed.[/]")
        if not typer.confirm("  Reinstall?", default=False):
            return no_changes()

    console.print("  Downloading Chromium — this takes a minute the first time.")
    try:
        install_browser()
    except sh.ErrorReturnCode as error:
        console.print(
            f"  [red]The download failed.[/] Run it directly to see why:\n"
            f"    uv run playwright install chromium\n  {error}"
        )
        raise typer.Exit(1) from error
    console.print("  [green]Installed.[/]")
    return no_changes()


def install_browser() -> None:
    """Run Playwright's own installer for Chromium and its system libraries.

    ``--with-deps`` because a Chromium that cannot find libnss3 fails at launch
    with a message about a shared object, which reads like a broken install
    rather than a missing package.
    """
    uv = sh.Command("uv")
    uv("run", "playwright", "install", "--with-deps", "chromium")


def browser_status(_env: EnvVars) -> IntegrationStatus:
    """Whether a login could actually open a browser."""
    if browser_installed():
        return IntegrationStatus(ok=True, detail="Chromium installed")
    return IntegrationStatus(ok=False, detail="not installed — no login can run")


def setup_timezone() -> EnvVars:
    """Set the timezone briefing windows are named in."""
    console.rule("[bold]Timezone[/]")
    detected = detect_system_timezone()
    if detected:
        console.print(f"  Detected [bold]{detected}[/]")
    entered = typer.prompt(
        "  IANA timezone (e.g. Europe/Paris)", default=detected or "UTC"
    )
    return {"AGENT_TIMEZONE": entered} if entered else no_changes()


def timezone_status(env: EnvVars) -> IntegrationStatus:
    """Whether a timezone has been chosen rather than inherited."""
    zone = env.get("AGENT_TIMEZONE", "")  # lup: ignore[dict-get] — an open env map
    if zone:
        return IntegrationStatus(ok=True, detail=zone)
    return IntegrationStatus(ok=False, detail="system default")


# lup: ignore[constant-declaration] — which integrations this application offers
# to set up, decided here because nothing sits above it to be asked
INTEGRATIONS: list[Integration] = [
    Integration(
        name="Login browser",
        command="browser",
        help="Install the Chromium a claude.ai login runs in.",
        env_keys=[],
        setup_func=setup_browser,
        status_func=browser_status,
    ),
    Integration(
        name="Notion",
        command="notion",
        help="Create the integration and both databases.",
        env_keys=[
            "NOTION_TOKEN",
            "NOTION_DISCUSSIONS_DATABASE_ID",
            "NOTION_WORLDVIEW_DATABASE_ID",
        ],
        setup_func=setup_notion,
        status_func=notion_status,
    ),
    Integration(
        name="Timezone",
        command="timezone",
        help="Set the timezone briefing windows are named in.",
        env_keys=["AGENT_TIMEZONE"],
        setup_func=setup_timezone,
        status_func=timezone_status,
    ),
]


app = create_setup_app(INTEGRATIONS, profile_directory())
