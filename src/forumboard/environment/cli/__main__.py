"""The ``forumboard`` command: run the pipeline, or one pass of it.

Every long-running behaviour has a one-shot twin. ``run`` is the daemon;
``sync-once``, ``worldview-once``, and ``briefings-once`` do exactly what one
tick of each loop does and then exit. That is not a convenience — it is how
anything here gets debugged, because a pass that only exists inside a daemon
can only be observed by watching logs and waiting.

Nothing here decides anything about a conversation. The passes do that, and
this module's whole job is choosing which of them to run and printing what
they said.
"""

import asyncio
import logging
from pathlib import Path
from typing import Annotated

import typer

import lup.workspace.paths
import forumboard.profiles
from forumboard.agent.config import settings
from forumboard.devtools.setup import app as setup_app
from forumboard.enrolment import (
    ActOutcome,
    Enrol,
    ProfileAct,
    ProfileTarget,
    SetNote,
    SignIn,
    Withdraw,
)

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="forumboard",
    help="Review claude.ai conversations into Notion, and keep a current worldview",
    no_args_is_help=True,
    add_completion=False,
)

profile_app = typer.Typer(
    name="profile", help="Enrol, sign in, and inspect the synced profiles"
)
app.add_typer(profile_app)

# The same wizard `lup-devtools setup` serves, mounted here too. Whoever
# operates this reads `doctor`'s advice and types what it says; sending them to
# a second CLI to act on it would make the advice wrong.
app.add_typer(setup_app, name="setup")


@app.callback(invoke_without_command=True)
def callback(ctx: typer.Context) -> None:
    """Point the workspace at the configured notes and logs directories.

    Relative values (including the defaults) are anchored at the project root,
    so behaviour does not depend on where the command was run from; an absolute
    value moves the data wholesale.
    """
    root = lup.workspace.paths.project_root()
    lup.workspace.paths.configure(
        notes_dir=(root / settings.notes_path).resolve(),
        logs_dir=(root / settings.logs_path).resolve(),
    )
    if ctx.invoked_subcommand is None:
        raise typer.Exit()


def configure_logging(verbose: bool) -> None:
    """Send the pipeline's progress to stderr at the requested level."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def project_root() -> Path:
    """The checkout this command is operating on."""
    return lup.workspace.paths.project_root()


def profiles_root() -> Path:
    """Where profile directories live, for this checkout."""
    return forumboard.profiles.profiles_root(project_root())


VerboseOption = Annotated[
    bool, typer.Option("--verbose", "-v", help="Log every step, not just outcomes")
]


def report(lines: list[str]) -> None:
    """Print what a pass reported, one line each."""
    for line in lines:
        typer.echo(line)


async def one_pass(which: str, *, dry_run: bool = False) -> list[str]:
    """Open the pipeline and run a single named pass."""
    from forumboard.context import NotConfigured
    from forumboard.pipeline.daemon import open_pipeline

    try:
        pipeline = await open_pipeline(project_root(), profiles_root())
    except NotConfigured as error:
        raise typer.Exit(1) from error
    match which:
        case "sync":
            return await pipeline.sync_once(dry_run=dry_run)
        case "worldview":
            return await pipeline.worldview_once()
        case "briefings":
            return await pipeline.briefings_once()
        case _:
            return await pipeline.all_once()


def run_pass(which: str, verbose: bool, *, dry_run: bool = False) -> None:
    """Run one pass, reporting a configuration problem rather than a traceback."""
    from forumboard.context import NotConfigured

    configure_logging(verbose)
    try:
        report(asyncio.run(one_pass(which, dry_run=dry_run)))
    except NotConfigured as error:
        typer.echo(f"Not configured: {error}", err=True)
        raise typer.Exit(1) from error


@app.command()
def run(verbose: VerboseOption = False) -> None:
    """Run the daemon: sync, worldview, and briefings, each on its own timer."""
    from forumboard.context import NotConfigured
    from forumboard.pipeline.daemon import run_forever

    configure_logging(verbose)
    try:
        asyncio.run(run_forever(project_root(), profiles_root()))
    except NotConfigured as error:
        typer.echo(f"Not configured: {error}", err=True)
        raise typer.Exit(1) from error
    except KeyboardInterrupt:
        typer.echo("Stopped.")


@app.command(name="sync-once")
def sync_once(
    verbose: VerboseOption = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Name what would be read, and stop there"),
    ] = False,
) -> None:
    """Fetch, review, edit, and publish one round of new conversations.

    ``--dry-run`` stops after the listing, before the first Opus call and
    anything reaching Notion. It is what says whether a quiet pass read
    nothing or decided against everything it read.
    """
    run_pass("sync", verbose, dry_run=dry_run)


@app.command(name="worldview-once")
def worldview_once(verbose: VerboseOption = False) -> None:
    """Fold every unmerged discussion into the worldview, once."""
    run_pass("worldview", verbose)


@app.command(name="briefings-once")
def briefings_once(verbose: VerboseOption = False) -> None:
    """Write whichever briefings are owed, once."""
    run_pass("briefings", verbose)


@app.command(name="once")
def once(verbose: VerboseOption = False) -> None:
    """Run one of each pass, in the order a conversation moves through them."""
    run_pass("all", verbose)


@app.command()
def doctor() -> None:
    """Check that Notion is reachable and both databases have the right shape."""
    from forumboard.context import configuration_report

    for line in asyncio.run(configuration_report()):
        typer.echo(line)


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Interface to bind")] = None,
    port: Annotated[int | None, typer.Option(help="Port to bind")] = None,
) -> None:
    """Serve the enrolment page, where somebody signs in from their own browser."""
    import uvicorn

    from forumboard.environment.web.app import create_app

    configure_logging(False)
    uvicorn.run(
        create_app(project_root(), profiles_root()),
        host=host or settings.web_host,
        port=port or settings.web_port,
    )


@profile_app.command(name="list")
def list_profiles() -> None:
    """Show every profile, whether it is enrolled, and whether it is signed in."""
    from forumboard.profiles import profile_states

    states = profile_states(project_root(), profiles_root())
    if not states:
        typer.echo(f"No profiles yet — none under {profiles_root()}.")
        return
    for state in states:
        typer.echo(state.describe())


def report_act(act: ProfileAct, name: str) -> ActOutcome:
    """Run one act against a named profile, and print what it said.

    These verbs and the editor perform the same acts, so what each one does and
    the words it says it in cannot drift apart. Nothing having happened goes to
    stderr, which is what lets a script tell a no-op from a change without
    reading the sentence.
    """
    target = ProfileTarget(
        project_root=project_root(), profiles_root=profiles_root(), name=name
    )
    outcome = asyncio.run(act.perform(target))
    typer.echo(outcome.message, err=not outcome.changed)
    return outcome


@profile_app.command()
def edit() -> None:
    """Sign in, enrol, withdraw, or edit a note, choosing from the listing.

    The same editor ``forumboard setup profiles`` walks. It exists separately
    because correcting the roster later should not mean re-entering setup.
    """
    from forumboard.devtools.enrolment import edit_roster

    edit_roster(project_root(), profiles_root())


@profile_app.command()
def login(name: str) -> None:
    """Open a browser on this machine to sign a profile into claude.ai.

    For somebody who is not at this machine, use ``forumboard serve`` instead —
    it streams the same browser to theirs.
    """
    if not report_act(SignIn(), name).changed:
        raise typer.Exit(1)


@profile_app.command()
def enrol(
    name: str,
    note: Annotated[
        str, typer.Option(help="Who arranged this, or what was agreed")
    ] = "",
) -> None:
    """Add a profile to the sync roster.

    A profile directory existing does not mean its conversations are read. This
    is the step that says they are, and it is recorded in a gitignored file.
    """
    report_act(Enrol(answer=note), name)


@profile_app.command()
def note(
    name: str,
    text: Annotated[str, typer.Argument(help="Who arranged this, or what was agreed")],
) -> None:
    """Change what an enrolment records about how it was arranged.

    The enrolment date stays: writing down more accurately what somebody agreed
    to is not them agreeing again.
    """
    report_act(SetNote(answer=text), name)


@profile_app.command()
def withdraw(name: str) -> None:
    """Remove a profile from the sync roster.

    Future passes stop reading it. Nothing already published is touched: taking
    a page down is a decision about material other people may have acted on,
    and is not made as a side effect of ending a sync.
    """
    report_act(Withdraw(), name)


@profile_app.command()
def probe(name: str) -> None:
    """Ask claude.ai what one profile can see, and what a sync would keep.

    Reads only — it lists the account's organisations and the conversations
    under each, and touches neither the cursor nor Notion. This is what tells
    a "nothing new" apart: an expired session, an organisation that holds no
    conversations, and a listing the freshness floor filtered all say it.
    """
    from forumboard.claudeai.probe import probe_profile
    from forumboard.store import ConversationStore

    notes = (project_root() / settings.notes_path).resolve()
    probed = asyncio.run(
        probe_profile(
            profiles_root=profiles_root(),
            store=ConversationStore(notes / "conversations"),
            profile=name,
        )
    )
    report(probed.lines())


@profile_app.command()
def status(name: str) -> None:
    """Show what has been decided about one profile's conversations."""
    from forumboard.store import ConversationStore

    notes = (project_root() / settings.notes_path).resolve()
    records = ConversationStore(notes / "conversations").records(name)
    if not records:
        typer.echo(f"Nothing recorded for {name} yet.")
        return
    for record in records:
        typer.echo(record.describe())
    published = sum(1 for record in records if record.published())
    typer.echo(f"\n{published}/{len(records)} published")


if __name__ == "__main__":
    app()
