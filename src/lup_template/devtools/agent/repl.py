"""Interactive REPL with the agent via the SDK (continuous session)."""

import asyncio
import hashlib
import io
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console

    from lup.runtime.contracts import Session
    from lup.runtime.factory import SessionFactory
    from lup.runtime.models import TurnResult

import sh
import typer
from pydantic import BaseModel

from lup.telemetry.display import format_duration
from lup.runtime.models import turn_request
from lup_template.agent.config import settings
from lup_template.devtools.agent.serve import collect_registry_tools


class ClipboardImage(BaseModel):
    """Raw image bytes read from the clipboard, with their MIME type."""

    media_type: str
    data: bytes


# lup: ignore[constant-declaration] — each pair is a media type and the suffix
# the format is written with, both named outside this repository
MIME_TO_EXT: dict[str, str] = {  # lup: ignore[dict-str-payload] — mime → suffix
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def save_images(
    images: list[ClipboardImage],
    images_dir: Path,
) -> list[Path]:
    """Save raw image data to disk, deduplicating by content hash."""
    images_dir.mkdir(parents=True, exist_ok=True)

    def save_one(image: ClipboardImage) -> Path:
        ext = MIME_TO_EXT.get(image.media_type, ".bin")  # lup: ignore[dict-get]
        path = images_dir / (hashlib.sha256(image.data).hexdigest()[:12] + ext)
        if not path.exists():
            path.write_bytes(image.data)
        return path

    return [save_one(image) for image in images]


# lup: ignore[constant-declaration] — the media types a clipboard offers an
# image as, named by the formats rather than by this module
CLIPBOARD_IMAGE_MIMES = ("image/png", "image/jpeg", "image/webp")


def read_clipboard_image() -> ClipboardImage | None:
    """Read image data from the system clipboard via xclip.

    Returns ``(media_type, raw_bytes)`` or ``None`` when no image is available.
    """
    try:
        xclip = sh.Command("xclip")
        targets = str(xclip("-selection", "clipboard", "-o", "-t", "TARGETS"))
    except (sh.ErrorReturnCode, sh.CommandNotFound):
        return None

    for mime in CLIPBOARD_IMAGE_MIMES:
        if mime not in targets:
            continue
        try:
            buf = io.BytesIO()
            xclip("-selection", "clipboard", "-o", "-t", mime, _out=buf)
            data = buf.getvalue()
            if data:
                return ClipboardImage(media_type=mime, data=data)
        except sh.ErrorReturnCode:
            continue
    return None


def read_clipboard_text() -> str | None:
    """Read text from the system clipboard via xclip."""
    try:
        xclip = sh.Command("xclip")
        text = str(xclip("-selection", "clipboard", "-o"))
        return text if text else None
    except (sh.ErrorReturnCode, sh.CommandNotFound):
        return None


class Interrupted(Exception):
    """Raised when the user interrupts response collection via Ctrl-C."""


async def send_interruptible(
    conv: "Session",
    prompt: str,
    console: "Console",
) -> "TurnResult[None]":
    """Send a prompt with Ctrl-C interrupt support.

    First Ctrl-C sends an interrupt signal (graceful stop).
    Second Ctrl-C cancels the task (force stop).
    """
    loop = asyncio.get_running_loop()
    interrupt_count = 0

    handle = await conv.start(turn_request(prompt))
    send_task = asyncio.create_task(handle.turn.result())

    def on_sigint() -> None:
        nonlocal interrupt_count
        interrupt_count += 1
        if interrupt_count == 1:
            console.print("\n  [dim]interrupting...[/dim]")
            if handle.interrupt is not None:
                asyncio.ensure_future(handle.interrupt.interrupt())
            else:
                send_task.cancel()
        else:
            send_task.cancel()

    loop.add_signal_handler(signal.SIGINT, on_sigint)
    try:
        return await send_task
    except asyncio.CancelledError:
        raise Interrupted from None
    finally:
        loop.remove_signal_handler(signal.SIGINT)


def build_repl_factory(
    model: str | None,
    *,
    no_tools: bool,
    no_prompt: bool,
) -> "SessionFactory":
    """Build the configured provider-neutral factory for a REPL session.

    The overrides are assembly knobs on the neutral options
    (``build_session_options``) — realized before translation, on every
    engine alike, never by patching a built client. Session-scoped
    resources (sandbox cleanup) live inside ``client.session()``.
    """
    from lup_template.agent.core import build_session_factory

    return build_session_factory(
        "repl", model=model, toolless=no_tools, bare_prompt=no_prompt
    ).factory


def print_response_stats(response: "TurnResult[None]", console: "Console") -> float:
    """Print the duration/cost (or token-count) summary line for a turn.

    Returns the turn's cost in USD (0.0 when the backend reports none)
    so callers can accumulate a session total.
    """
    parts: list[str] = []
    cost = 0.0
    text = "\n\n".join(
        text for block in response.blocks if (text := block.text_payload) is not None
    )
    if text:
        console.print(text)
    if response.duration.total_seconds():
        parts.append(format_duration(response.duration.total_seconds()))
    from lup_template.agent.core import build_usage_cost

    usage_cost = build_usage_cost()
    if usage_cost is not None:
        cost = usage_cost(response.usage)
        parts.append(f"${cost:.4f}")
    else:
        usage = response.usage
        parts.append(f"{usage.input_tokens}in/{usage.output_tokens}out tok")
    if parts:
        console.print(f"  [dim]{' · '.join(parts)}[/dim]")
    console.print()
    return cost


async def exec_once(
    prompt: str,
    *,
    model: str | None = None,
    no_tools: bool = False,
    no_prompt: bool = False,
) -> None:
    """Run a single prompt through the REPL machinery, then return.

    The non-interactive counterpart of :func:`repl` — same adapter and
    conversation construction, same overrides — for smoke tests and
    scripting. The response streams to the console as it arrives; no
    interactive prompt loop is entered.
    """
    from rich.console import Console

    console = Console(highlight=False)
    factory = build_repl_factory(model, no_tools=no_tools, no_prompt=no_prompt)
    async with factory.open() as opened:
        try:
            response = await send_interruptible(opened.session, prompt, console)
        except Interrupted:
            console.print("  [dim]interrupted[/dim]\n")
            return
        except RuntimeError as e:
            console.print(f"  [red]error:[/red] {e}\n")
            raise typer.Exit(1) from e
        print_response_stats(response, console)


async def repl(
    *,
    model: str | None = None,
    no_tools: bool = False,
    no_prompt: bool = False,
) -> None:
    """Run the interactive REPL loop."""
    from contextlib import AsyncExitStack

    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
    from prompt_toolkit.styles import Style as PTStyle
    from rich.console import Console
    from rich.panel import Panel

    from lup.workspace.paths import project_root

    console = Console(highlight=False)
    effective_model = model or settings.model
    stack = AsyncExitStack()

    # Welcome panel with server → tool listing
    panel_lines = [
        "[bold]✻ Agent REPL[/bold]",
        f"[dim]model:[/dim] {effective_model}",
    ]
    if not no_tools:
        servers = collect_registry_tools()
        for i, (name, stools) in enumerate(servers.items()):
            is_last_server = i == len(servers) - 1
            panel_lines.append(f"[dim]{'└' if is_last_server else '├'} {name}[/dim]")
            for j, tool in enumerate(stools):
                tname = tool.name
                is_last_tool = j == len(stools) - 1
                branch = "  └" if is_last_tool else "  ├"
                if not is_last_server:
                    branch = f"[dim]│[/dim] {'└' if is_last_tool else '├'}"
                panel_lines.append(f"[dim]{branch}[/dim] {tname}")
    else:
        panel_lines.append("[dim]no tools[/dim]")
    panel_lines += [
        "",
        "[dim]/quit · Ctrl-C stop · Ctrl-V paste image · Alt+Enter newline[/dim]",
    ]

    console.print()
    console.print(Panel("\n".join(panel_lines), border_style="blue", width=60))
    console.print()

    # -- prompt_toolkit session --
    session_cost = 0.0
    pending_images: list[ClipboardImage] = []

    def rprompt() -> FormattedText:
        parts = [effective_model]
        if pending_images:
            n = len(pending_images)
            parts.append(f"{n} img{'s' if n > 1 else ''}")
        if session_cost:
            parts.append(f"${session_cost:.4f}")
        return FormattedText([("class:rprompt", " · ".join(parts))])

    history_dir = project_root() / ".lup"
    history_dir.mkdir(parents=True, exist_ok=True)

    # Key bindings: Enter submits, Alt+Enter inserts newline
    kb = KeyBindings()

    @kb.add("escape", "enter")  # Alt+Enter or Esc then Enter
    def newline_binding(event: KeyPressEvent) -> None:
        event.current_buffer.newline()

    @kb.add("enter")
    def submit_binding(event: KeyPressEvent) -> None:
        event.current_buffer.validate_and_handle()

    @kb.add("c-v")
    def paste_binding(event: KeyPressEvent) -> None:
        result = read_clipboard_image()
        if result is not None:
            pending_images.append(result)
            n = len(pending_images)
            console.print(
                f"[dim]{n} image{'s' if n > 1 else ''} attached (/drop to clear)[/dim]"
            )
        else:
            text = read_clipboard_text()
            if text:
                event.current_buffer.insert_text(text)

    pt_session: PromptSession[str] = PromptSession(
        message=FormattedText([("class:prompt", "❯ ")]),
        rprompt=rprompt,
        style=PTStyle.from_dict(
            {
                "prompt": "fg:ansiblue bold",
                "prompt-continuation": "fg:ansiblue",
                "rprompt": "fg:#666666",
            }
        ),
        history=FileHistory(str(history_dir / "repl_history")),
        completer=WordCompleter(
            ["/quit", "/exit", "/q", "/help", "/drop"],
            sentence=True,
        ),
        key_bindings=kb,
        multiline=True,
        prompt_continuation=FormattedText([("class:prompt-continuation", "··· ")]),
    )

    try:
        factory = build_repl_factory(model, no_tools=no_tools, no_prompt=no_prompt)

        async with stack:
            async with factory.open() as opened:
                last_input_sigint = 0.0

                while True:
                    try:
                        user_input = await pt_session.prompt_async()
                    except (EOFError, asyncio.CancelledError):
                        console.print()
                        break
                    except KeyboardInterrupt:
                        now = time.monotonic()
                        if now - last_input_sigint < 2.0:
                            console.print()
                            break
                        last_input_sigint = now
                        console.print("[dim]Press Ctrl-C again to exit[/dim]")
                        continue

                    last_input_sigint = 0.0
                    stripped = user_input.strip()
                    if not stripped:
                        continue
                    if stripped in ("/quit", "/exit", "/q"):
                        break
                    if stripped == "/drop":
                        pending_images.clear()
                        console.print("[dim]images cleared[/dim]")
                        continue

                    console.print("[dim]thinking...[/dim]")
                    if pending_images:
                        images_dir = project_root() / ".lup" / "images"
                        saved = save_images(pending_images, images_dir)
                        path_list = ", ".join(str(p) for p in saved)
                        query_text = (stripped + "\n\n" if stripped else "") + (
                            f"[image attached: {path_list}]"
                        )
                        prompt_text = query_text
                        pending_images.clear()
                    else:
                        prompt_text = user_input
                    try:
                        response = await send_interruptible(
                            opened.session,
                            prompt_text,
                            console,
                        )
                        session_cost += print_response_stats(response, console)
                    except Interrupted:
                        console.print("  [dim]interrupted[/dim]\n")
                    except RuntimeError as e:
                        console.print(f"  [red]error:[/red] {e}\n")
    except KeyboardInterrupt:
        # Additional Ctrl+C during cleanup — containers will be cleaned
        # on next start via stale container removal
        pass
