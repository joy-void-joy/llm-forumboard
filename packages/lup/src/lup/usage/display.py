"""The one usage display, over whichever runtime's reader fills it.

A display is the same program on every runtime — read, render, print, or hold
a live panel and read again — so this composes a :class:`UsageReader` rather
than being written once per account. The reader is the only thing that knows
a credential, an endpoint, or a wire shape; nothing below it does.
"""

import json
import time
from datetime import datetime

import typer
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from lup.usage.models import UsageReader, UsageUnavailable
from lup.usage.render import build_display, build_error_panel, build_snapshot

WATCH_INTERVAL_SECONDS = 600
"""How often a live panel re-reads, absent a caller saying otherwise."""


class UsageDisplay:
    """Show one account's usage, however the runtime behind it is read."""

    def __init__(
        self, reader: UsageReader, runtime_name: str, console: Console | None = None
    ) -> None:
        self.reader = reader
        self.runtime_name = runtime_name
        self.console = console or Console()

    def panel(self, detail: bool, bar_width: int) -> Panel:
        """Read once and render the panel that reading produces."""
        return build_display(self.reader.read(detail), bar_width)

    def emit_json(self, detail: bool) -> None:
        """Print the snapshot so an agent can read counts and limits.

        stdout stays valid JSON on failure too: a failed read is reported as
        an ``{"error": ...}`` object with a non-zero exit, never as
        human-formatted text a parser would choke on.
        """
        try:
            report = self.reader.read(detail)
        except UsageUnavailable as error:
            print(json.dumps({"error": str(error)}))
            raise typer.Exit(1) from error
        print(build_snapshot(report).model_dump_json(indent=2))

    def show(self, detail: bool) -> None:
        """Read once and print, reporting a failed read as a failed command."""
        bar_width = min(self.console.width - 10, 58)
        try:
            panel = self.panel(detail, bar_width)
        except UsageUnavailable as error:
            self.console.print(f"[red]{error}[/red]")
            raise typer.Exit(1) from error
        self.console.print()
        self.console.print(panel)

    def footer(self, interval: int) -> Text:
        return Text(
            f"  updated {datetime.now().strftime('%H:%M:%S')}"
            f"  ·  every {interval}s  ·  ctrl-c to quit",
            style="dim",
        )

    def watch(self, detail: bool, interval: int) -> None:
        """Hold a live panel, re-reading on an interval until interrupted.

        A failed read replaces the panel rather than ending the watch: the
        window it was showing is still the last thing known to be true, and a
        connection that drops for one interval usually returns for the next.
        """
        bar_width = min(self.console.width - 10, 58)

        def rendered() -> Panel:
            try:
                return self.panel(detail, bar_width)
            except UsageUnavailable as error:
                return build_error_panel(self.runtime_name, str(error)[:120])

        panel = rendered()
        with Live(
            Group(panel, self.footer(interval)),
            console=self.console,
            refresh_per_second=1,
            screen=True,
        ) as live:
            while True:
                try:
                    time.sleep(interval)
                except KeyboardInterrupt:
                    break
                live.update(Group(rendered(), self.footer(interval)))

    def run(self, detail: bool, json_output: bool, watch: bool, interval: int) -> None:
        """Answer one invocation in whichever of the three modes it asked for."""
        if json_output:
            self.emit_json(detail)
        elif watch and self.console.is_terminal:
            self.watch(detail, interval)
        else:
            self.show(detail)
