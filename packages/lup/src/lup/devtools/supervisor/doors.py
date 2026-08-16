"""Console doors onto a resolver run's question mailbox.

Each of these is a handful of lines because the mailbox is just files, and
each is strictly better than a prompt on the run's own stdin ever was: they
work from another shell, or another host, *while the run is still waiting*.

None of them may take :meth:`ResolverStateRepository.exclusive`. A run holds
that lock for its whole life, so a door that reached for it could only ever
serve runs that had already finished — which is exactly the class of runs a
door is useless for.
"""

from pathlib import Path
from time import sleep

import typer

from lup.channels.models import local_stamp, utc_now
from lup.resolver.journal import Journal
from lup.resolver.models import (
    ConcernRetirement,
    ConcernStatus,
    VerificationAcceptance,
)
from lup.resolver.state import ResolverStateRepository, StateTransitionError
from lup.resolver.status import RunStatus, run_status
from lup.resolver.mailbox import (
    AnswerDoor,
    AnswerOffer,
    MailboxConflictError,
    ParkRequest,
    QuestionMailbox,
    new_message,
)
from lup.workspace.paths import project_root
from lup.devtools.harness.resolve import parse_answer_flags
from lup.devtools.supervisor.projection import PendingQuestionView


def resolve_state_root() -> Path:
    return project_root() / ".lup" / "resolve"


def open_mailbox(run_id: str) -> QuestionMailbox:
    """The mailbox for one run, refusing a run that was never recorded."""
    root = resolve_state_root() / run_id
    if not root.is_dir():
        raise typer.BadParameter(f"no resolver run {run_id!r} under {root.parent}")
    return QuestionMailbox(root)


def pending_views(mailbox: QuestionMailbox) -> list[PendingQuestionView]:
    """Every question a run has asked, straight from its mailbox.

    The state file is never read here: a moving run's ``state.json`` lags
    its mailbox by design, and the mailbox alone is authoritative for
    anything still pending.
    """
    answered = {
        record.answer.question_id: record.answer.value for record in mailbox.answers()
    }
    offered = {offer.question_id: offer.value for offer in mailbox.offers()}
    return [
        PendingQuestionView(
            question=item.question,
            asked_by=item.asked_by,
            answered=(
                answered[item.question.id] if item.question.id in answered else None
            ),
            offer=offered[item.question.id] if item.question.id in offered else None,
        )
        for item in mailbox.questions()
    ]


def describe(view: PendingQuestionView) -> list[str]:
    """Render one question the way an operator needs to answer it cold."""
    question = view.question
    lines = [f"{question.id} (concern {question.concern_id})", f"  {question.prompt}"]
    if question.choices:
        lines.append("  choices: " + " | ".join(question.choices))
    if question.recommendation is not None:
        lines.append(f"  recommendation: {question.recommendation}")
    if view.answered is not None:
        lines.append(f"  answered: {view.answered}")
    elif view.offer is not None:
        lines.append(f"  offered, not yet promoted: {view.offer}")
    return lines


def queued(run_id: str, to: str) -> list[str]:
    """Say what a just-posted message is waiting on, rather than that it sent.

    Reporting `sent` on the strength of having written the mailbox is what
    let a redirect read as successful while reaching nobody at all. The
    honest report is that it is queued, for whom, and — when the address
    matches no actor the run has recorded — that nobody it can name will
    read it. Not a refusal: a message may legitimately be posted for a
    concern that has not started, and it waits at that actor's first turn.
    """
    reached = [
        actor
        for actor in Journal(resolve_state_root() / run_id).actors()
        if to in actor.addresses()
    ]
    if not reached:
        return [
            f"queued for {to or 'every actor'}, which names no actor this run "
            "has recorded yet; it waits until one by that address takes a turn",
        ]
    return [
        f"queued for {actor.label()}; it arrives at that actor's next tool call or turn"
        for actor in reached
    ]


app = typer.Typer(help="Read and answer resolver runs from the console")


@app.command("questions")
def list_questions(
    run_id: str = typer.Option(..., "--run-id", help="Run whose mailbox to read"),
    pending_only: bool = typer.Option(
        False, "--pending", help="Show only questions still waiting on you"
    ),
) -> None:
    """List a run's questions and what each one has been answered.

    ``--pending`` is what a human reads after answering, so it means what
    they mean by it: still waiting on you. A question already offered is
    waiting on the run to take it, not on another answer — listed as
    pending it read as though nothing had been recorded, and the only way
    to tell was to go and count files on disk.
    """
    views = pending_views(open_mailbox(run_id))
    waiting = [view for view in views if view.answered is None and view.offer is None]
    offered = [view for view in views if view.answered is None and view.offer]
    selected = waiting if pending_only else views
    if not selected:
        typer.echo("No questions." if not views else "Nothing waiting on you.")
        return
    for view in selected:
        for line in describe(view):
            typer.echo(line)
    if pending_only and offered:
        typer.echo(
            f"{len(offered)} answered and awaiting promotion; "
            "the run takes them when it next advances"
        )


@app.command("answer")
def answer_questions(
    pairs: list[str] = typer.Argument(..., help="<question-id>=<value>, repeatable"),
    run_id: str = typer.Option(..., "--run-id", help="Run whose mailbox to write"),
) -> None:
    """Offer an answer to one or more of a run's questions.

    Offers stay correctable right up until a promoter takes one, so a
    mistyped free-text value is fixed by offering again rather than being
    permanent — nothing under ``.lup/resolve`` is ever unlinked.
    """
    mailbox = open_mailbox(run_id)
    known = {view.question.id: view for view in pending_views(mailbox)}
    for identifier, value in parse_answer_flags(pairs).items():
        if identifier not in known:
            raise typer.BadParameter(
                f"{identifier!r} names no question this run asked; "
                f"run `harness resolve questions --run-id {run_id}`"
            )
        question = known[identifier].question
        if question.closed_choices and value not in question.choices:
            raise typer.BadParameter(
                f"{identifier!r} accepts only: " + ", ".join(question.choices)
            )
        try:
            mailbox.offer(
                AnswerOffer(
                    run_id=run_id,
                    question_id=identifier,
                    value=value,
                    door=AnswerDoor.CONSOLE,
                    offered_at=utc_now(),
                )
            )
        except MailboxConflictError as error:
            raise typer.BadParameter(str(error)) from error
        typer.echo(f"offered {identifier}={value}")


@app.command("actors")
def list_actors(
    run_id: str = typer.Option(..., "--run-id", help="Run whose record to read"),
) -> None:
    """List every actor this run has recorded, and what each has not read yet.

    Undelivered mail is shown because sending is not delivering: a door
    writes the stream and the actor reads it at its next tool call or turn,
    and nothing between those two moments used to say which had happened.
    A redirect sitting here through a whole concern is that concern being
    worked on the instructions it was supposed to abandon.
    """
    # Before the journal is read, not after. A run directory that is not
    # there yields no actors, which printed "nothing recorded yet" and exited
    # zero — indistinguishable from a real run that has not started, and the
    # answer a sibling worktree with no `.lup` at all gave for every id.
    mailbox = open_mailbox(run_id)
    actors = Journal(resolve_state_root() / run_id).actors()
    if not actors:
        typer.echo("No actor has recorded anything yet.")
        return
    for actor in actors:
        typer.echo(actor.label())
        waiting = mailbox.waiting(actor)
        for message in waiting.messages:
            kind = "redirect" if message.redirect else "message"
            typer.echo(f"  undelivered {kind} from {message.door}: {message.text}")


@app.command("say")
def say_to_actor(
    text: str = typer.Argument(..., help="What to tell the actor"),
    run_id: str = typer.Option(..., "--run-id", help="Run whose mailbox to write"),
    to: str = typer.Option(
        "", "--to", help="Actor label from `actors`, or empty to reach every actor"
    ),
    in_reply_to: str = typer.Option(
        "", "--in-reply-to", help="Question or message id this answers, if any"
    ),
) -> None:
    """Tell an actor something. It reads this and keeps going."""
    open_mailbox(run_id).send(
        new_message(run_id, to, text, AnswerDoor.AGENT, in_reply_to)
    )
    for line in queued(run_id, to):
        typer.echo(line)


@app.command("accept")
def accept_verification(
    reason: str = typer.Argument(..., help="Why this failure is accepted"),
    run_id: str = typer.Option(..., "--run-id", help="Run whose state to write"),
    concern: str = typer.Option(..., "--concern", help="Concern to accept"),
    verification: str = typer.Option(
        ..., "--verification", help="The failing verification, by name"
    ),
) -> None:
    """Accept one concern over one failing verification, on the human's word.

    A verdict is an exit code, and some failures are true but unfixable from
    inside the lease that meets them — a finding the worker did not
    introduce and cannot converge on. Resubmitting into it spends a revision
    round each time until the concern fails with its criteria never read.

    The reason is required because this is what review sees in place of a
    green check that was never green.
    """
    root = resolve_state_root() / run_id
    if not root.is_dir():
        raise typer.BadParameter(f"no resolver run {run_id!r} under {root.parent}")
    ResolverStateRepository(resolve_state_root(), run_id).accept(
        VerificationAcceptance(
            concern_id=concern, verification=verification, reason=reason
        )
    )
    typer.echo(f"accepted {concern} over {verification}: {reason}")


@app.command("redirect")
def redirect_actor(
    text: str = typer.Argument(..., help="What the actor should do instead"),
    run_id: str = typer.Option(..., "--run-id", help="Run whose mailbox to write"),
    to: str = typer.Option(
        "", "--to", help="Actor label from `actors`, or empty to reach every actor"
    ),
) -> None:
    """Stop an actor and put it on something else.

    Where `say` rides alongside the actor's next tool call, this refuses that
    call and hands back this text as the reason — so an actor going the wrong
    way cannot take one more step down it before reading why it was stopped.
    """
    open_mailbox(run_id).send(
        new_message(run_id, to, text, AnswerDoor.AGENT, redirect=True)
    )
    for line in queued(run_id, to):
        typer.echo(line)


@app.command("park")
def park_run(
    run_id: str = typer.Option(..., "--run-id", help="Run to park"),
    reason: str = typer.Option(
        "parked from the console", "--reason", help="Why the run is being parked"
    ),
) -> None:
    """Ask every open wait in this run to give up now.

    This reaches a run sitting on an answer and no other. To stop one that
    is *working*, use `drain`: a worker inside a model turn waits on
    nothing, so park does not reach it.
    """
    open_mailbox(run_id).park(ParkRequest(run_id=run_id, reason=reason))
    typer.echo(f"parked {run_id}: {reason}")


@app.command("drain")
def drain_run(
    run_id: str = typer.Option(..., "--run-id", help="Run to drain"),
    reason: str = typer.Option(
        "drained from the console", "--reason", help="Why the run is being stopped"
    ),
) -> None:
    """Ask a busy run to finish what is in flight and stop, resumably.

    The other half of `park`, and a different request. Park ends every open
    wait, which never reached a worker mid-turn — so the only way to end a
    busy run was to kill it, discarding the uncommitted edits of each
    interrupted round along with its reviewer feedback and round counter.

    A drain is observed at the top of a round, where the previous round is
    already committed, and at the boundary between dependency batches.
    Nothing is failed and nothing is written off. It takes effect when the
    run next reaches one of those, so a long turn finishes first.
    """
    open_mailbox(run_id).drain(ParkRequest(run_id=run_id, reason=reason))
    typer.echo(f"draining {run_id}: {reason}")
    typer.echo("It stops at its next round or batch boundary; resuming costs nothing.")


def show_status(
    run_id: str = typer.Option(..., "--run-id", help="Run to report on"),
    watch: bool = typer.Option(
        False,
        "--watch",
        help="Keep reporting until the run parks or finishes, instead of "
        "answering once",
    ),
    heartbeat: float = typer.Option(
        60.0,
        "--heartbeat",
        help="Seconds between verdict lines while nothing changes, so silence "
        "is never ambiguous",
    ),
    poll: float = typer.Option(
        2.0, "--poll", help="Seconds between readings of the run directory"
    ),
    startup: float = typer.Option(
        30.0,
        "--startup",
        help="Seconds a watch allows a just-launched run to take its lock "
        "before an unheld run reads as parked rather than as starting",
    ),
    line: bool = typer.Option(
        False,
        "--line",
        help="Print this report's first line and stop. It carries what "
        "needs you — the time, the phase, how far along, how much longer, "
        "and a question waiting, a stop or a failure. What the full report "
        "adds below it is the per-status breakdown and the last event",
    ),
) -> None:
    """Say whether a run is alive, where it stands, and what it last did.

    The liveness answer comes from the run's own lock rather than from the
    process table, because under a sandbox `/proc` is PID-isolated: `ps` and
    `pgrep` list nothing outside the current shell, so a healthy run looks
    exactly like one that died. That ambiguity has produced confident wrong
    conclusions in both directions, and the run directory is the only thing
    a reader is guaranteed to be able to see.
    """
    root = resolve_state_root()
    repository = ResolverStateRepository(root, run_id)
    status = run_status(repository, run_id)
    if not status.exists:
        raise typer.BadParameter(f"no resolver run {run_id!r} under {root}")
    if line and not watch:
        typer.echo(status_header(status))
        return
    if not watch:
        report_status(status)
        return
    typer.echo(status_header(status))
    report_waiting(run_id)
    watch_status(repository, run_id, heartbeat, poll, startup, status, line)


SETTLED_STATUSES: tuple[ConcernStatus, ...] = (
    ConcernStatus.VERIFIED,
    ConcernStatus.INELIGIBLE,
    ConcernStatus.RETIRED,
    ConcernStatus.FAILED,
    ConcernStatus.INTEGRATED,
    ConcernStatus.CLEANED,
    ConcernStatus.RETAINED,
)
"""Which statuses mean a concern is done being decided, however it ended.

Ours to draw rather than the lifecycle's own, so a caller redraws it: a
reader could reasonably count `integrating` as finished, and this does not.
Naming the settled half rather than the working one makes an unlisted status
read as still in flight, which is the safer way to be wrong — a new state
counted as finished would inflate the fraction silently.
"""


def status_header(
    status: RunStatus, settled: tuple[ConcernStatus, ...] = SETTLED_STATUSES
) -> str:
    """The report's first line, and on its own what ``--line`` prints.

    One composition rather than two, so the compact form cannot drift from
    the full one: what a full report adds below this line is only what this
    line leaves out — the per-status breakdown, and the last journal event.

    A breakdown of every status is progress rather than attention: nobody
    acts on "9 retired", and a reader scanning for what needs them has to
    find it among nine figures that do not. What is here instead is how far
    along the run is, how much longer the phase it is in should take, what
    it has lost on the way, and whether it is holding anybody up.

    A loss is named only where there is one, so its presence means it, and
    it joins the liveness rather than standing in for it: a run works
    quietly through a concern that failed days ago, and a line carrying the
    failure alone reads as though it were happening now.

    The fraction counts every concern that reached a terminal state rather
    than only the ones that produced work, so it can actually arrive at its
    own total. Measured against the plan it would stop short by every
    concern retired or found ineligible, and a progress figure that can
    never complete teaches a reader to stop believing it.
    """
    counts = {count.status: count.concerns for count in status.counts}
    done = sum(counts[status_] for status_ in settled if status_ in counts)
    total = sum(counts.values())
    failed = counts[ConcernStatus.FAILED] if ConcernStatus.FAILED in counts else 0
    waiting = status.unanswered
    iterator = status.progress
    return " · ".join(
        [
            local_stamp(),
            str(status.phase or "initializing"),
            f"{done}/{total} settled",
            *([f"{iterator.label} {iterator.render()}"] if iterator else []),
            *([f"{failed} failed"] if failed else []),
            *(
                [f"{waiting} question{'' if waiting == 1 else 's'} waiting"]
                if waiting
                else []
            ),
            status.verdict(),
        ]
    )


def report_status(status: RunStatus) -> None:
    """Print one reading of a run: the header, then what it leaves out.

    Stamped because whoever reads this reads it inside a report written
    later still, and "how long since I was last told anything" is the
    question they are actually asking. The run's own relative ages answer a
    different one: how long a worker has been quiet.
    """
    typer.echo(status_header(status))
    for count in status.counts:
        typer.echo(f"  {count.concerns:>3} {count.status}")
    if status.last is not None:
        typer.echo(
            f"  last: {status.last.event} by {status.last.actor} "
            f"at {status.last.at:%Y-%m-%d %H:%M:%S}Z"
        )


def report_waiting(run_id: str) -> None:
    """Print any question now waiting, whole, beside the change that raised it.

    A watch reports what moved; a question that moved is one somebody has to
    read, and naming it without carrying it makes every wake-up start with a
    round trip to fetch what happened. The reader is an agent that must
    measure a question's claims against the tree before relaying it, so what
    it needs is the question's own words — the notes it was raised from, the
    concern's specification, its acceptance criteria — not a count.

    Carrying it also survives a channel that drops: a notification lost after
    this one still leaves the question in hand, where a count that went
    missing leaves no trace that anything was ever asked.
    """
    waiting = [
        view
        for view in pending_views(open_mailbox(run_id))
        if view.answered is None and view.offer is None
    ]
    for view in waiting:
        for line in describe(view):
            typer.echo(line)


def watch_status(
    repository: ResolverStateRepository,
    run_id: str,
    heartbeat: float,
    poll: float,
    startup: float,
    first: RunStatus,
    terse: bool = False,
) -> None:
    """Report a run until it parks or finishes, so nobody hand-rolls the loop.

    The supervisor page serves the same projection, but it is server-sent
    events to a browser and an agent in a terminal cannot consume it. What
    an agent reinvented instead was a `tail`, which cannot see a question
    queued by a worker that keeps its siblings running — the run does not
    park, prints nothing, and the answer waits indefinitely.

    Both halves are load-bearing. Emitting on change alone leaves a reader
    unable to tell a quiet run from a dead watch, and the heartbeat alone
    would report a change up to a minute after it happened.
    """
    frame = first.watched()
    running_yet = first.held
    quiet = 0.0
    waited = 0.0
    while not (status := run_status(repository, run_id)).settled(running_yet):
        running_yet = running_yet or status.held or waited >= startup
        sleep(poll)
        quiet += poll
        waited += poll
        if status.watched() != frame:
            frame = status.watched()
            quiet = 0.0
            if terse:
                typer.echo(status_header(status))
            else:
                report_status(status)
            report_waiting(run_id)
        elif quiet >= heartbeat:
            quiet = 0.0
            typer.echo(status_header(status))
    # Nothing is reported here. Every way this loop ends moves a field the
    # frame is taken over — the lock is released, or the phase becomes
    # terminal — so the reading that ended it was already printed as a
    # change, and a run settled before the first poll was printed by the
    # caller.
    if status.phase is None:
        typer.echo(
            f"{local_stamp()} — watch ended before initialization; inspect {repository.root / 'detached.log'}."
        )
        return
    typer.echo(f"{local_stamp()} — watch ended: {run_id} is waiting on you.")
    report_waiting(run_id)


def retire_concern(
    reason: str = typer.Argument(
        ..., help="Where this concern was settled — a commit, branch or issue"
    ),
    run_id: str = typer.Option(..., "--run-id", help="Run whose state to write"),
    concern: str = typer.Option(..., "--concern", help="Concern to retire"),
) -> None:
    """Retire one concern whose work was settled somewhere other than this run.

    A run parked while its branch moved forward routinely finds the branch
    already did some of its work, and base refresh makes that the expected
    consequence of following a branch rather than a rare accident. Without
    this, every route was wrong: hand-resolving an add/add conflict between
    two independent implementations of one thing, letting a worker open on a
    concern whose notes no longer exist in its tree, or aborting the whole
    run — discarding every settled answer — to retire one concern.

    The concern leaves the eligible set without failing, its dependents build
    from the base where the work that settled it now lives, and its lease
    stops being active. The worktree and branch stay: a retired concern often
    built its own answer to what landed upstream, and that is worth reading
    before it is thrown away.
    """
    root = resolve_state_root() / run_id
    if not root.is_dir():
        raise typer.BadParameter(f"no resolver run {run_id!r} under {root.parent}")
    try:
        ResolverStateRepository(resolve_state_root(), run_id).retire(
            ConcernRetirement(concern_id=concern, reason=reason)
        )
    except StateTransitionError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(f"retired {concern}: {reason}")
