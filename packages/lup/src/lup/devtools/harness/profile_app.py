"""The command tree over whichever origin holds a project's runtime accounts.

Mounted wherever a project already talks about profiles — beside the native
launchers as ``harness profile``, or inside a setup wizard — so the roster a
launch selects from is curated in one vocabulary no matter which tree the
caller reached it through.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from lup.runtime.profiles import Profile, ProfileDirectory


def create_profile_app(directory: ProfileDirectory) -> typer.Typer:
    """Wire the profile command tree over one project's profile origin."""
    app = typer.Typer(
        no_args_is_help=True,
        help="Inspect and curate the accounts a launch can select",
    )

    def acting(act: Callable[[], Profile]) -> Profile:
        """Answer for what an origin refuses, rather than tracebacking.

        Both refusals arrive already worded: :class:`UnknownProfile` carries
        the roster a launcher reports the same way, and an origin that
        derives its profiles from something else — a directory the project
        keeps, rather than a registry of its own — cannot honour every
        curation the tree offers and says so with a ``ValueError`` whose
        message is the explanation. Rendering theirs is what keeps this tree
        and the launcher from wording the same refusal two ways.
        """
        try:
            return act()
        except (KeyError, ValueError) as error:
            raise typer.BadParameter(str(error)) from error

    @app.command("list")
    def list_command() -> None:
        """Show every profile, and which one a launch selects by default."""
        entries = directory.entries()
        if not entries:
            typer.echo("No profiles yet — add one with `profile add`")
            return
        for entry in entries:
            selected = "*" if entry.active else " "
            login = "logged in" if entry.logged_in else "no login yet"
            typer.echo(f"{selected} {entry.name}  {entry.config_dir}  ({login})")

    @app.command("add")
    def add_command(
        name: Annotated[str, typer.Argument(help="Name for the account")],
        config_dir: Annotated[
            Path | None,
            typer.Option(
                "--config-dir",
                help="Configuration home to register, instead of the one this "
                "project would keep for that name",
            ),
        ] = None,
    ) -> None:
        """Register a runtime configuration home under a name."""
        entry = acting(lambda: directory.add(name, config_dir))
        typer.echo(f"Added {entry.name}: {entry.config_dir}")
        if not entry.logged_in:
            typer.echo(
                "No login there yet — sign one in by starting the runtime with "
                f"{directory.login.config_home_env}={entry.config_dir}"
            )

    @app.command("use")
    def use_command(
        name: Annotated[str, typer.Argument(help="Profile to select")],
    ) -> None:
        """Select the profile a launch uses when none is named."""
        entry = acting(lambda: directory.use(name))
        typer.echo(f"Active profile: {entry.name} ({entry.config_dir})")

    @app.command("remove")
    def remove_command(
        name: Annotated[str, typer.Argument(help="Profile to forget")],
    ) -> None:
        """Forget a profile, leaving its configuration home on disk."""
        entry = acting(lambda: directory.remove(name))
        typer.echo(f"Removed {entry.name} — left {entry.config_dir} on disk")

    return app
