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
from collections import Counter
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

prompts_app = typer.Typer(
    name="prompts", help="Show what each pass is told, after local replacements"
)
app.add_typer(prompts_app)

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


@prompts_app.command(name="list")
def list_prompts() -> None:
    """Show every declared piece, and which this deployment has replaced.

    A replacement that silently changed nothing is the failure this exists to
    make visible, so the marker beside a piece is the whole point of the
    listing rather than decoration on it.
    """
    from forumboard.agent.prompts import catalog

    replaced = [piece.name for piece in catalog.overrides()]
    path = catalog.local_path()
    typer.echo(f"local replacements: {path if path.is_file() else 'none'}\n")
    for each in catalog.BY_NAME:
        typer.echo(each.name)
        for piece in each.composition.pieces:
            mark = " (replaced)" if piece.name in replaced else ""
            typer.echo(f"  {piece.name}{mark}")


@prompts_app.command(name="show")
def show_prompt(name: str) -> None:
    """Print one pass's prompt exactly as the model receives it."""
    from forumboard.agent.prompts import catalog

    rendered = catalog.rendered(name)
    if rendered is None:
        known = ", ".join(each.name for each in catalog.BY_NAME)
        typer.echo(f"No prompt named {name!r}. There is: {known}", err=True)
        raise typer.Exit(1)
    typer.echo(rendered)


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
def fetch(
    name: str,
    conversation: Annotated[
        str,
        typer.Argument(
            help="A conversation id, or a claude.ai/share/... link", metavar="REFERENCE"
        ),
    ],
    verbose: VerboseOption = False,
) -> None:
    """Fetch one conversation into its folder, deciding nothing about it.

    Reads only: it writes the transcript and the files it names, and touches
    neither the cursor, nor the records, nor Notion. A sync is how a
    conversation gets judged; this is how one gets looked at, which until now
    could only be done by running the whole pipeline over whatever the cursor
    happened to admit.

    A share link is accepted wherever a conversation id is, so a conversation
    somebody sent you can be read without it being in this account.

    It writes into the same folder a sync reads, so what a pass would see is
    exactly what lands here.
    """
    from forumboard.claudeai.browser import ProfileCredentials, context_dir
    from forumboard.claudeai.client import (
        ClaudeWebError,
        ClaudeWebClient,
        ConversationReference,
    )
    from forumboard.store import ConversationStore

    if verbose:
        configure_logging(verbose)
    notes = (project_root() / settings.notes_path).resolve()
    store = ConversationStore(notes / "conversations")
    reference = ConversationReference(value=conversation)
    client = ClaudeWebClient(ProfileCredentials(context_dir(profiles_root(), name)))
    try:
        content = asyncio.run(reference.fetch(client))
    except ClaudeWebError as error:
        typer.echo(f"Could not read {reference.describe()}: {error}", err=True)
        raise typer.Exit(1) from error

    store.save_conversation(name, content)
    report(
        [
            f"{content.name or '(untitled)'} — {content.message_count} messages",
            f"wrote {store.conversation_dir(name, content.uuid)}",
            *[
                f"  attachments/{attachment.relative_path()}"
                for attachment in content.attachments
            ],
        ]
    )


@profile_app.command()
def decide(
    name: str,
    conversation: Annotated[
        str,
        typer.Argument(
            help="A conversation id already fetched into its folder",
            metavar="CONVERSATION_ID",
        ),
    ],
    title: Annotated[
        str,
        typer.Option(
            help="The title, where no record from an earlier pass carries one"
        ),
    ] = "",
    verbose: VerboseOption = False,
) -> None:
    """Review and edit one fetched conversation, publishing nothing.

    The two model passes and nothing around them: no listing, no cursor, no
    Notion page, no record. What it leaves behind is ``page.md`` beside the
    transcript — the editor's own output, in the place the pipeline would read
    it from — so what a publish would carry can be read before one happens.

    This is the counterpart to ``profile fetch``, which gets a conversation
    without deciding anything about it. Between them a conversation can be
    taken through the passes one step at a time, which otherwise needs a sync
    over whatever the cursor happens to admit.
    """
    from forumboard.pipeline import passes
    from forumboard.store import TRANSCRIPT_FILE, ConversationStore

    if verbose:
        configure_logging(verbose)
    notes = (project_root() / settings.notes_path).resolve()
    store = ConversationStore(notes / "conversations")
    folder = store.conversation_dir(name, conversation)
    if not (folder / TRANSCRIPT_FILE).is_file():
        typer.echo(
            f"No transcript at {folder} — run `forumboard profile fetch {name} "
            f"{conversation}` first.",
            err=True,
        )
        raise typer.Exit(1)

    known = store.read_record(name, conversation)
    heading = title or (known.title if known is not None else "")
    verdict = asyncio.run(passes.review(heading, folder))
    typer.echo(f"review: {verdict.describe()}")
    plan = verdict.plan_for_editor()
    if plan is None:
        return

    outcome = asyncio.run(passes.edit(heading, folder, plan))
    typer.echo(f"edit: {outcome.describe()}")
    if outcome.publishable() is None:
        return

    page = store.read_page(name, conversation)
    if not page:
        typer.echo(
            "The editor decided to publish but wrote no page — a sync would "
            "refuse this rather than publish an empty one.",
            err=True,
        )
        raise typer.Exit(1)
    report([f"wrote {store.page_path(name, conversation)} — {len(page)} characters"])


@profile_app.command()
def audit(
    name: str,
    conversation: Annotated[
        str,
        typer.Argument(
            help="A conversation id, or a claude.ai/share/... link", metavar="REFERENCE"
        ),
    ],
    kind: Annotated[
        str,
        typer.Option(help="Report the fields carried by blocks of this kind instead"),
    ] = "",
) -> None:
    """Report what a fetch reads of a conversation, and what it drops.

    The payload models ignore fields they do not declare, so an upstream
    addition arrives as nothing rather than as an error. That is what stops
    one new key rejecting a whole conversation, and it is also how material
    could reach here and be lost with nothing said. This says which fields
    are in which set, for one real conversation.

    Reads only, and writes nothing at all — not even the transcript.
    """
    from forumboard.claudeai.browser import ProfileCredentials, context_dir
    from forumboard.claudeai.client import (
        ClaudeWebClient,
        ClaudeWebError,
        ConversationReference,
    )
    from forumboard.claudeai.probe import PayloadAudit

    reference = ConversationReference(value=conversation)
    client = ClaudeWebClient(ProfileCredentials(context_dir(profiles_root(), name)))
    try:
        payload = asyncio.run(reference.raw(client))
    except ClaudeWebError as error:
        typer.echo(f"Could not read {reference.describe()}: {error}", err=True)
        raise typer.Exit(1) from error

    if kind:
        carried = Counter(PayloadAudit.fields_on(payload, kind))
        report(
            [f"{kind} blocks carry:"]
            + [f"  {field} ×{count}" for field, count in sorted(carried.items())]
        )
        return
    report(PayloadAudit.over(payload).lines())


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
