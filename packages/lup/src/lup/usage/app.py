"""The usage sub-app, composed from whichever runtimes an application reads.

The library ships no roster here either: each adapter declares one entry
beside the reader that answers it, and the application names the entries it
wants. Every command they produce is the same command — the options, the
three modes, and the rendering are decided once — so a runtime joins the
display by being read, never by growing a command of its own.
"""

from collections.abc import Callable
from typing import Annotated

import typer
from pydantic import BaseModel

from lup.usage.display import WATCH_INTERVAL_SECONDS, UsageDisplay
from lup.usage.models import UsageReader


class UsageEntry(BaseModel, frozen=True):
    """One runtime's place in the display: what it is called, and how to read it.

    ``open`` takes the profile the invocation named, or nothing for whichever
    profile the runtime considers active, and raises
    :class:`~lup.usage.models.UsageUnavailable` when there is no account there
    to read — which is the same failure as a request that does not arrive, and
    reaches the caller by the same route.
    """

    name: str
    """The command word, which is the adapter's own name for its runtime."""

    runtime_name: str
    """How the runtime is titled in prose, borrowed from its own vocabulary."""

    help: str
    open: Callable[[str | None], UsageReader]


def create_usage_app(entries: list[UsageEntry]) -> typer.Typer:
    """Compose one usage sub-app offering exactly the runtimes named."""
    app = typer.Typer(no_args_is_help=True)

    def command_for(entry: UsageEntry) -> Callable[..., None]:
        def show_usage(
            profile: Annotated[
                str | None,
                typer.Option(
                    "--profile",
                    "-p",
                    help="Profile to read usage for (default: the active one).",
                ),
            ] = None,
            detail: Annotated[
                bool,
                typer.Option(
                    "--detail/--no-detail",
                    help="Show the per-day breakdown.",
                ),
            ] = True,
            json_output: Annotated[
                bool,
                typer.Option(
                    "--json",
                    help="Emit machine-readable usage (counts and limits) "
                    "instead of bars.",
                ),
            ] = False,
            watch: Annotated[
                bool,
                typer.Option(
                    "--watch/--no-watch",
                    "-w",
                    help="Continuously refresh the display (requires a terminal).",
                ),
            ] = False,
            interval: Annotated[
                int,
                typer.Option(
                    "--interval",
                    "-n",
                    help="Refresh interval in seconds (with --watch).",
                ),
            ] = WATCH_INTERVAL_SECONDS,
        ) -> None:
            display = UsageDisplay(entry.open(profile), entry.runtime_name)
            display.run(detail, json_output, watch, interval)

        show_usage.__doc__ = entry.help
        return show_usage

    for entry in entries:
        app.command(entry.name)(command_for(entry))
    return app
