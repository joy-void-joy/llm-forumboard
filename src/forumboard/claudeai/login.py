"""A claude.ai login somebody completes from their own browser.

The problem this solves: enrolling a profile means signing into claude.ai as
that person, and the person is not sitting at the machine the sync runs on.
Asking them to paste a ``sessionKey`` out of developer tools works once and
then expires, and it asks somebody to handle a bare credential.

So the browser runs here and is *shown* there. A Chrome DevTools screencast
turns the real page into a stream of JPEG frames; the viewer's clicks and
keystrokes come back as input events. What lands at the end is a durable
browser session in the profile's own directory — the same one
:class:`forumboard.claudeai.browser.ProfileCredentials` reads.

Popups are followed rather than ignored: signing in with Google opens one, and
a screencast still pointed at the original page would show a viewer nothing
while they typed their password into a window they cannot see.
"""

import asyncio
import logging
from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from lup.types import EnvVars, JsonObject, JsonValue

from forumboard.claudeai.browser import (
    COOKIE_ORIGIN,
    LOGIN_URL,
    BrowserCookie,
    Viewport,
    launch_context,
    require_playwright,
)

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, CDPSession, Page, Playwright
    from pyvirtualdisplay.display import Display

logger = logging.getLogger(__name__)

# lup: ignore[constant-declaration] — Chrome DevTools Protocol's own name
SCREENCAST_FORMAT = "jpeg"
# lup: ignore[constant-declaration] — a quality that stays legible while
# staying small enough to push over a socket at interactive speed
SCREENCAST_QUALITY = 70


class ScreencastFrame(BaseModel, frozen=True, extra="ignore"):
    """One frame the protocol pushed, and the id used to acknowledge it."""

    data: str
    session_id: int = Field(alias="sessionId")


class InputEvent(BaseModel):
    """Something the viewer did, on its way to the real page."""

    @abstractmethod
    def cdp_method(self) -> str:
        """The Chrome DevTools method that replays it."""

    @abstractmethod
    def cdp_params(self) -> JsonObject:
        """That method's parameters, in the protocol's own spelling."""


class MouseEvent(InputEvent):
    """A click, a release, or a move."""

    kind: Literal["mouse"] = "mouse"
    action: str
    x: float
    y: float
    button: str = "left"
    clicks: int = 1
    modifiers: int = 0

    def cdp_method(self) -> str:
        return "Input.dispatchMouseEvent"

    def cdp_params(self) -> JsonObject:
        params: JsonObject = {
            "type": self.action,
            "x": self.x,
            "y": self.y,
            "button": self.button,
            "clickCount": self.clicks,
            "modifiers": self.modifiers,
        }
        return params


class WheelEvent(InputEvent):
    """A scroll."""

    kind: Literal["wheel"] = "wheel"
    x: float
    y: float
    delta_x: float = 0.0
    delta_y: float = 0.0

    def cdp_method(self) -> str:
        return "Input.dispatchMouseEvent"

    def cdp_params(self) -> JsonObject:
        params: JsonObject = {
            "type": "mouseWheel",
            "x": self.x,
            "y": self.y,
            "deltaX": self.delta_x,
            "deltaY": self.delta_y,
        }
        return params


class KeyEvent(InputEvent):
    """A keystroke, including the text it produces."""

    kind: Literal["key"] = "key"
    action: str
    key: str = ""
    code: str = ""
    text: str = ""
    modifiers: int = 0

    def cdp_method(self) -> str:
        return "Input.dispatchKeyEvent"

    def cdp_params(self) -> JsonObject:
        params: JsonObject = {
            "type": self.action,
            "key": self.key,
            "code": self.code,
            "text": self.text,
            "modifiers": self.modifiers,
        }
        return params


type ViewerInput = MouseEvent | WheelEvent | KeyEvent
"""Anything a viewer can send. The union is what a socket route validates
against, so an unrecognised shape is refused before it reaches the browser."""


class FrameSlot:
    """The latest frame, and a way to wait for the next one.

    A queue would let a viewer that fell behind work through a backlog of
    stale frames, which is exactly the wrong thing for a live screen: what
    somebody who lagged wants is the current page. So there is one slot, a
    late frame overwrites an unread one, and a waiter always wakes to the
    newest.
    """

    def __init__(self) -> None:
        self.latest = ""
        self.arrived = asyncio.Event()

    def put(self, frame: str) -> None:
        """Record a frame, replacing any the viewer has not read."""
        self.latest = frame
        self.arrived.set()

    async def next_frame(self) -> str:
        """Wait for a frame, then take it."""
        await self.arrived.wait()
        self.arrived.clear()
        return self.latest


def virtual_display() -> "Display | None":
    """A virtual X display, where the machine has no real one.

    Chromium runs headed inside it, which matters because some sign-in flows
    behave differently in a headless browser. On a machine with a display, or
    without Xvfb installed, this returns None and the browser runs headless.
    """
    try:
        from pyvirtualdisplay.display import Display
    except ImportError:
        logger.debug("pyvirtualdisplay is not installed; running headless")
        return None
    try:
        display = Display(visible=False, size=(1280, 800))
        display.start()
        return display
    except Exception:
        logger.debug("no virtual display available; running headless", exc_info=True)
        return None


class StreamingLogin:
    """A login browser driven from somewhere else.

    Frames land in a single slot rather than a queue: a viewer that falls
    behind should see the current page when it catches up, not work through a
    backlog of pages that are no longer on screen.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.frames = FrameSlot()
        self.playwright: "Playwright | None" = None
        self.context: "BrowserContext | None" = None
        self.page: "Page | None" = None
        self.cdp: "CDPSession | None" = None
        self.display: "Display | None" = None
        # lup: ignore[set-shape] — strong references to in-flight tasks, held
        # only so the event loop cannot collect one mid-flight
        self.followers: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """Launch the browser, open the login page, and begin streaming."""
        require_playwright()
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.display = virtual_display()
        env: EnvVars | None = None
        if self.display is not None:
            env = {name: value for name, value in self.display.env().items()}
        self.context = await launch_context(
            self.playwright,
            self.directory,
            headless=self.display is None,
            viewport=Viewport(),
            env=env,
        )
        self.context.on("page", self.follow)
        page = (
            self.context.pages[0]
            if self.context.pages
            else await self.context.new_page()
        )
        await self.attach(page)
        await self.clear_stale_session()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)

    async def clear_stale_session(self) -> None:
        """Drop any existing session before showing a login page.

        Re-enrolling somebody whose old session is still present would show
        them a signed-in app rather than a sign-in form, and they would have no
        way to reach the account they meant to add.
        """
        if self.context is None:
            return
        existing = await self.context.cookies(COOKIE_ORIGIN)
        if any(
            BrowserCookie.model_validate(entry).name == "sessionKey"
            for entry in existing
        ):
            await self.context.clear_cookies()

    def follow(self, page: "Page") -> None:
        """Re-point the screencast at a popup as soon as one opens.

        The task is held until it finishes: a bare ``create_task`` can be
        garbage-collected mid-flight, which would silently leave the viewer
        looking at the page they navigated away from.
        """
        task = asyncio.create_task(self.attach(page))
        self.followers.add(task)
        task.add_done_callback(self.followers.discard)

    async def attach(self, page: "Page") -> None:
        """Stream ``page``, replacing whatever was being streamed before."""
        if self.context is None:
            return
        await self.detach()
        self.page = page
        cdp = await self.context.new_cdp_session(page)
        self.cdp = cdp

        async def on_frame(params: JsonValue) -> None:
            frame = ScreencastFrame.model_validate(params)
            self.frames.put(frame.data)
            acknowledgement: JsonObject = {"sessionId": frame.session_id}
            try:
                await cdp.send("Page.screencastFrameAck", acknowledgement)
            except Exception:
                logger.debug("could not acknowledge a frame", exc_info=True)

        cdp.on("Page.screencastFrame", on_frame)
        options: JsonObject = {
            "format": SCREENCAST_FORMAT,
            "quality": SCREENCAST_QUALITY,
            "everyNthFrame": 1,
        }
        await cdp.send("Page.startScreencast", options)

    async def detach(self) -> None:
        """Stop the current screencast, if there is one."""
        if self.cdp is None:
            return
        try:
            await self.cdp.detach()
        except Exception:
            logger.debug("the previous devtools session was already gone")
        self.cdp = None

    async def apply(self, event: InputEvent) -> None:
        """Replay one viewer action against the real page."""
        if self.cdp is None:
            return
        await self.cdp.send(event.cdp_method(), event.cdp_params())

    async def signed_in(self) -> bool:
        """Whether a session cookie has appeared yet."""
        if self.context is None:
            return False
        found = await self.context.cookies(COOKIE_ORIGIN)
        return any(
            BrowserCookie.model_validate(entry).name == "sessionKey" for entry in found
        )

    async def stop(self) -> None:
        """Close everything, leaving the captured session on disk.

        Every step is attempted even when an earlier one fails: a browser
        process left running would hold the profile directory's lock, and the
        next enrolment would fail for a reason nobody could see.
        """
        await self.detach()
        for close in (self.close_context, self.close_playwright, self.close_display):
            try:
                await close()
            except Exception:
                logger.debug("a login teardown step failed", exc_info=True)

    async def close_context(self) -> None:
        """Close the browser context."""
        if self.context is not None:
            await self.context.close()
            self.context = None

    async def close_playwright(self) -> None:
        """Stop the Playwright driver."""
        if self.playwright is not None:
            await self.playwright.stop()
            self.playwright = None

    async def close_display(self) -> None:
        """Tear down the virtual display, where one was started."""
        if self.display is not None:
            self.display.stop()
            self.display = None
