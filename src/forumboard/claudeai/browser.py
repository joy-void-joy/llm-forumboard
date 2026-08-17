"""A browser session per profile, which is what a cookie is really made of.

A pasted ``sessionKey`` expires and nobody notices until the sync has been
quiet for a week. A stored browser context does not: it holds the cookies the
site itself set, refreshes them as the site refreshes them, and can be re-read
whenever a request comes back 401 — which is what
:class:`ProfileCredentials` does.

The context lives beside the profile's Claude Code login, under
``.lup/profiles/<name>/claude-web``. Same person, two credentials: one for the
agent that runs, one for the account whose conversations are read. They are
kept apart because they authorise different things and expire independently.
"""

import logging
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from lup.types import EnvVars, StringMap

from forumboard.claudeai.client import ClaudeCredentials, CredentialSource

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Playwright, ViewportSize

logger = logging.getLogger(__name__)

# lup: ignore[constant-declaration] — Chromium's own flag
AUTOMATION_FLAG = "--disable-blink-features=AutomationControlled"
# lup: ignore[constant-declaration] — the page a login starts at
LOGIN_URL = "https://claude.ai/login"
# lup: ignore[constant-declaration] — the origin whose cookies carry the session
COOKIE_ORIGIN = "https://claude.ai"


class BrowserCookie(BaseModel, frozen=True, extra="ignore"):
    """One cookie, as this project reads it.

    Playwright declares every field of a cookie optional, so reading one means
    deciding what an absent field means. That is decided once here rather than
    at each place that asks a cookie its name.
    """

    name: str = ""
    value: str = ""


class Viewport(BaseModel, frozen=True):
    """The window a streamed login is rendered at."""

    width: int = 1280
    height: int = 800

    def size(self) -> "ViewportSize":
        """This window in Playwright's own spelling."""
        return {"width": self.width, "height": self.height}


def context_dir(profiles_root: Path, profile: str) -> Path:
    """Where a profile's claude.ai browser session is stored."""
    return profiles_root / profile / "claude-web"


def has_session(profiles_root: Path, profile: str) -> bool:
    """Whether a profile has ever completed a login here.

    Reads the directory rather than launching a browser: this is asked when
    listing profiles, and a listing that started one browser per profile would
    be unusable.
    """
    directory = context_dir(profiles_root, profile)
    return directory.is_dir() and any(directory.iterdir())


def require_playwright() -> None:
    """Fail with the install instruction rather than an import traceback."""
    if find_spec("playwright.async_api") is None:
        raise RuntimeError(
            "Playwright is not installed. Run `uv sync`, then "
            "`uv run playwright install chromium`."
        )


async def launch_context(
    playwright: "Playwright",
    directory: Path,
    *,
    headless: bool,
    viewport: "ViewportSize | None" = None,
    env: EnvVars | None = None,
) -> "BrowserContext":
    """Open a profile's persistent context, creating it on first use.

    Takes Playwright's own viewport spelling rather than this module's
    ``Viewport``, which knows how to produce it — so the one function that
    talks to the driver talks only in the driver's vocabulary.
    """
    directory.mkdir(parents=True, exist_ok=True)
    # lup: ignore[dict-str-payload] — environment variables, keyed by whatever
    # the virtual display exports; the value union is Playwright's own
    launch_env: dict[str, str | float | bool] | None = None
    if env is not None:
        launch_env = {name: value for name, value in env.items()}
    return await playwright.chromium.launch_persistent_context(
        str(directory),
        headless=headless,
        args=[AUTOMATION_FLAG],
        viewport=viewport,
        env=launch_env,
    )


async def stored_cookies(directory: Path) -> StringMap:
    """Every claude.ai cookie the stored context holds, by name.

    Runs headless and closes immediately: this is a read, and a visible window
    would be a surprise to whoever is running the sync.
    """
    require_playwright()
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        context = await launch_context(playwright, directory, headless=True)
        try:
            found = await context.cookies(COOKIE_ORIGIN)
        finally:
            await context.close()
    return {
        cookie.name: cookie.value
        for entry in found
        if (cookie := BrowserCookie.model_validate(entry)).name
    }


async def stored_credentials(directory: Path) -> ClaudeCredentials:
    """The session a stored context carries, empty when there is none."""
    if not directory.is_dir():
        return ClaudeCredentials()
    cookies = await stored_cookies(directory)
    # lup: ignore[dict-get] — cookies are keyed by whatever the site set
    if not cookies.get("sessionKey", ""):
        return ClaudeCredentials()
    header = "; ".join(f"{name}={value}" for name, value in cookies.items())
    # lup: ignore[dict-get] — same open map
    organization = cookies.get("lastActiveOrg", "")
    return ClaudeCredentials(cookie=header, organization=organization)


async def login_interactive(directory: Path) -> bool:
    """Open a real browser so somebody at this machine can sign in.

    Returns once the window closes, reporting whether a session was captured.
    The remote equivalent is :class:`forumboard.claudeai.login.StreamingLogin`.
    """
    require_playwright()
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        context = await launch_context(playwright, directory, headless=False)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_event("close", timeout=0)
        except Exception:
            logger.debug("the login window closed or timed out", exc_info=True)
        finally:
            found = await context.cookies(COOKIE_ORIGIN)
            await context.close()
    return any(
        BrowserCookie.model_validate(entry).name == "sessionKey" for entry in found
    )


class ProfileCredentials(CredentialSource):
    """One profile's claude.ai session, re-read from its browser on demand.

    The cached value is what ordinary requests use. A refresh — asked for after
    a 401 — goes back to the stored context, which is where a cookie the site
    has since renewed will be.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.cached: ClaudeCredentials | None = None

    async def credentials(self, *, refresh: bool) -> ClaudeCredentials:
        if refresh or self.cached is None:
            self.cached = await stored_credentials(self.directory)
        return self.cached
