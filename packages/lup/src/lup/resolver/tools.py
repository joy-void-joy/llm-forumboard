"""The tools a resolver worker asks its material questions through.

A worker used to surface questions by ending its turn with them in its
typed report, which cost a whole new session to deliver the answer. These
tools let it ask mid-turn and keep working.

The handlers touch nothing but the mailbox and the lease they are bound to,
so one factory serves both transports: Claude registers the server
in-process, Codex spawns it as a stdio subprocess that rebuilds both from
the relayed context. Only where that context comes from differs.
"""

import asyncio
from collections.abc import Collection
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

from lup.harness.process import LocalProcessLauncher, ProcessLauncher
from lup.mcp import LupMcpTool, ToolError, lup_tool
from lup.policy.assets.host import recoverable_write_targets
from lup.channels.models import utc_now
from lup.resolver.declaration import declaration_delta, inspect_changes
from lup.resolver.mailbox import (
    ANSWER_POLL_SECONDS,
    ActorMessage,
    AnswerDoor,
    MailboxConflictError,
    PendingQuestion,
    QuestionMailbox,
    wait_for_answers,
)
from lup.policy.identity import ConcernAllowance
from lup.resolver.models import (
    ALLOWANCE_GRANTED,
    ALLOWANCE_REFUSED,
    MaterialQuestion,
    asks_for_an_allowance,
)
from lup.types import EnvVars

# Four env var names the orchestrator sets and a worker's tool process reads,
# so each is a handshake between two processes rather than a value either picks.
RESOLVER_RUN_DIR_ENV = "LUP_RESOLVER_RUN_DIR"  # lup: ignore[constant-declaration]
RESOLVER_CONCERN_ENV = "LUP_RESOLVER_CONCERN"  # lup: ignore[constant-declaration]
RESOLVER_LEASE_ROOT_ENV = "LUP_RESOLVER_LEASE_ROOT"  # lup: ignore[constant-declaration]
RESOLVER_ACTOR_ENV = "LUP_RESOLVER_ACTOR"  # lup: ignore[constant-declaration]
TOOL_WAIT_SECONDS = 300.0

# lup: ignore[constant-declaration] — one sentence every waiting tool's schema
# states identically, declared beside the tools rather than chosen per caller
WAIT_CONTRACT = (
    "This call blocks until a human answers. Blocking is expected and correct: "
    "you are not stuck, you are waiting. Do not poll, do not call this again in "
    "a loop, do not sleep and retry, and do not read files under .lup/. Make one "
    "call, wait for it to return, then act on what it gives you."
)


class ResolverToolContext(BaseModel):
    """What a tool-serving subprocess needs to find its run's mailbox."""

    run_dir: Path
    concern_id: str
    lease_root: Path
    actor_kind: str = ""
    """Which actor this subprocess serves, where the tools differ by it.

    A merger carries the verbs that sequence a join and a worker must not:
    commit authority over the tree everything lands in is the whole of what
    separates them, and one lease can open both.
    """

    def to_env(self) -> EnvVars:
        return {
            RESOLVER_RUN_DIR_ENV: str(self.run_dir),
            RESOLVER_CONCERN_ENV: self.concern_id,
            RESOLVER_LEASE_ROOT_ENV: str(self.lease_root),
            RESOLVER_ACTOR_ENV: self.actor_kind,
        }


class ResolverToolEnv(BaseSettings):
    """The relay's consumer side, parsed from the environment."""

    run_dir: Path | None = Field(default=None, validation_alias=RESOLVER_RUN_DIR_ENV)
    concern_id: str | None = Field(default=None, validation_alias=RESOLVER_CONCERN_ENV)
    lease_root: Path | None = Field(
        default=None, validation_alias=RESOLVER_LEASE_ROOT_ENV
    )
    actor_kind: str = Field(default="", validation_alias=RESOLVER_ACTOR_ENV)


def read_resolver_tool_context() -> ResolverToolContext | None:
    """Rebuild the relayed context, or None when this is not a tool subprocess."""
    env = ResolverToolEnv()
    if env.run_dir is None or env.concern_id is None or env.lease_root is None:
        return None
    return ResolverToolContext(
        run_dir=env.run_dir,
        concern_id=env.concern_id,
        lease_root=env.lease_root,
        actor_kind=env.actor_kind,
    )


class AskedQuestion(BaseModel):
    """One decision a worker needs a human to make."""

    id: str = Field(
        description="Short id, unique among your questions. Letters, digits, dashes."
    )
    prompt: str = Field(
        description="The decision, stated so a human can answer it cold"
    )
    choices: list[str] = Field(
        default=[],
        description="The allowed answers. Leave empty for free text.",
    )
    recommendation: str | None = Field(
        default=None, description="Your preferred answer, which must be a choice"
    )


class QueueQuestionsInput(BaseModel):
    questions: list[AskedQuestion] = Field(
        min_length=1, description="Every question you want answered"
    )


class QueueQuestionsOutput(BaseModel):
    question_ids: list[str] = Field(description="Pass these verbatim to await_answers")
    already_answered: list[str] = Field(
        description="Questions a human had already answered before you asked"
    )
    pending: list[str] = Field(description="Questions still waiting for a human")


class AwaitAnswersInput(BaseModel):
    question_ids: list[str] = Field(
        default=[],
        description="Ids from queue_questions. Empty waits for every one you queued.",
    )


class AnsweredQuestion(BaseModel):
    id: str
    prompt: str
    value: str


class AwaitAnswersOutput(BaseModel):
    status: Literal["answered", "parked"]
    answers: list[AnsweredQuestion]
    unanswered: list[str]
    instruction: str


class CheckDeclarationInput(BaseModel):
    files_changed: list[Path] = Field(
        default=[],
        description="Every path you believe you changed, as you would report it",
    )
    swept_beyond_scope: list[Path] = Field(
        default=[],
        description="Paths you would report as swept beyond your concern's scope",
    )


class CheckDeclarationOutput(BaseModel):
    settled: bool = Field(
        description="Whether this account would pass the declaration gate"
    )
    changed: list[str] = Field(description="Every path git sees your worktree moved")
    undeclared: list[str] = Field(
        description="Changed, and named nowhere in the account you passed"
    )
    unswept: list[str] = Field(
        description="Claimed as swept beyond scope, and not changed at all"
    )
    instruction: str


IRREVERSIBLE_VERBS = dict.fromkeys(
    ["push", "reset", "checkout", "restore", "clean", "rebase", "filter-branch"]
)


def agent_may_approve(
    command: str, root: Path, irreversible: Collection[str] = IRREVERSIBLE_VERBS
) -> bool:
    """Whether an orchestrating agent may answer this ask, or only a human.

    Recoverability decides, not the verb. Removing a file the object store
    already holds is recoverable, so an agent may approve it; removing an
    untracked one is not, and neither is a hard reset over uncommitted work, a
    force-push, or anything that leaves this machine.

    What counts as recoverable is the permission kernel's own answer, taken
    from the host half rather than asked again here. Two definitions of the
    word is how one of them ends up weaker: this asked only whether Git
    tracked the path, so a tracked file carrying uncommitted edits read as
    recoverable and approving its removal discarded work nothing could
    restore. Directories are excluded there for the same reason, and now here.
    """
    words = command.split()
    if not words:
        return False
    if any(word in irreversible for word in words[:2]):
        return False
    if words[0] != "rm":
        return True
    targets = [word for word in words[1:] if not word.startswith("-")]
    return bool(targets) and recoverable_write_targets(targets, root) == targets


class RequestAllowanceInput(BaseModel):
    allowance: ConcernAllowance = Field(
        description="The gate you need, which must be one this run knows"
    )
    reason: str = Field(
        description="Why the work cannot be done without it, stated concretely"
    )


class SendMessageInput(BaseModel):
    text: str = Field(description="What to tell the humans watching this run")
    to_actor: str = Field(
        default="",
        description=(
            "Actor label to address, like 'worker:some-concern#1'. Leave empty "
            "to reach everyone watching."
        ),
    )
    in_reply_to: str = Field(
        default="",
        description="The id of a message or question this answers, if any",
    )


class SendMessageOutput(BaseModel):
    sent: bool = Field(description="Whether the message reached the run's record")


def create_question_tools(
    mailbox: QuestionMailbox,
    concern_id: str,
    *,
    run_id: str,
    lease_root: Path,
    launcher: ProcessLauncher | None = None,
    poll_interval_seconds: float = ANSWER_POLL_SECONDS,
    wait_seconds: float = TOOL_WAIT_SECONDS,
    wake: asyncio.Event | None = None,
) -> list[LupMcpTool]:
    """Build the ask tools bound to one concern's mailbox.

    ``concern_id`` is bound here rather than taken as an argument, so a
    worker structurally cannot post a question against a sibling concern,
    and ids are composed rather than trusted — two workers inventing the
    same short id must not collide in one flat namespace.
    """
    queued: list[str] = []
    inspector = launcher if launcher is not None else LocalProcessLauncher()

    def compose(identifier: str) -> str:
        return f"{concern_id}-{identifier}"

    def prompts() -> dict[str, str]:  # lup: ignore[dict-str-payload] — open id map
        return {item.question.id: item.question.prompt for item in mailbox.questions()}

    @lup_tool(
        "Ask the human one or more material questions about this concern and "
        "return immediately. Use this the moment you know a decision is not "
        "yours to make — queue every question you have at once so a human can "
        "answer them in one pass, keep working on whatever does not depend on "
        "them, then call await_answers. Returns the ids to wait on.",
        name="queue_questions",
    )
    async def queue_questions(params: QueueQuestionsInput) -> QueueQuestionsOutput:
        answered = mailbox.answered_ids()
        composed: list[str] = []
        for asked in params.questions:
            identifier = compose(asked.id)
            try:
                question = MaterialQuestion(
                    id=identifier,
                    concern_id=concern_id,
                    prompt=asked.prompt,
                    choices=asked.choices,
                    recommendation=asked.recommendation,
                    closed_choices=asks_for_an_allowance(concern_id, identifier),
                )
            except ValueError as error:
                raise ToolError(
                    f"question {asked.id!r} is not well formed: {error}"
                ) from error
            try:
                mailbox.queue(
                    PendingQuestion(
                        run_id=run_id,
                        question=question,
                        asked_by=concern_id,
                        asked_at=utc_now(),
                    )
                )
            except MailboxConflictError as error:
                raise ToolError(str(error)) from error
            composed.append(identifier)
            if identifier not in queued:
                queued.append(identifier)
        return QueueQuestionsOutput(
            question_ids=composed,
            already_answered=[name for name in composed if name in answered],
            pending=[name for name in composed if name not in answered],
        )

    @lup_tool(
        "Wait for the human's answers to questions you queued, then return them. "
        + WAIT_CONTRACT
        + " If nobody answers in time the run parks: you will get status "
        "'parked', and you should stop work on this concern and submit your "
        "report so the orchestrator can resume once an answer arrives.",
        name="await_answers",
    )
    async def await_answers(params: AwaitAnswersInput) -> AwaitAnswersOutput:
        wanted = params.question_ids or list(queued)
        if not wanted:
            raise ToolError("queue a question before waiting for an answer")
        known = prompts()
        unknown = [name for name in wanted if name not in known]
        if unknown:
            raise ToolError(
                "no question is queued under "
                + ", ".join(repr(name) for name in unknown)
                + "; call queue_questions first and pass the ids it returns"
            )
        result = await wait_for_answers(
            mailbox,
            wanted,
            wait_seconds=wait_seconds,
            poll_interval_seconds=poll_interval_seconds,
            wake=wake,
        )
        answers = [
            AnsweredQuestion(
                id=record.answer.question_id,
                prompt=known[record.answer.question_id],
                value=record.answer.value,
            )
            for record in result.answered
        ]
        if not result.unanswered:
            return AwaitAnswersOutput(
                status="answered",
                answers=answers,
                unanswered=[],
                instruction="Act on these answers and finish the concern.",
            )
        return AwaitAnswersOutput(
            status="parked",
            answers=answers,
            unanswered=result.unanswered,
            instruction=(
                f"No answer arrived ({result.reason}). Stop working on this "
                "concern and submit your report now — the orchestrator parks the "
                "run and resumes it once a human answers."
            ),
        )

    @lup_tool(
        "Ask the human one or more material questions and wait for the answers. "
        "This is queue_questions followed by await_answers. " + WAIT_CONTRACT,
        name="ask_questions",
    )
    async def ask_questions(params: QueueQuestionsInput) -> AwaitAnswersOutput:
        posted = await queue_questions(params)
        return await await_answers(AwaitAnswersInput(question_ids=posted.question_ids))

    @lup_tool(
        "Tell the humans watching this run — or one other actor in it — "
        "something, without waiting for a reply. Use this to volunteer what "
        "you have found, flag a consequence for whoever merges your work, or "
        "answer something you were asked, naming the actor that asked. This "
        "never blocks and never parks the run — if you need a decision before "
        "you can continue, that is a question, not a message.",
        name="send_message",
    )
    async def send_message(params: SendMessageInput) -> SendMessageOutput:
        mailbox.send(
            ActorMessage(
                run_id=run_id,
                to_actor=params.to_actor,
                text=params.text,
                door=AnswerDoor.AGENT,
                sent_at=utc_now(),
                in_reply_to=params.in_reply_to,
            )
        )
        return SendMessageOutput(sent=True)

    @lup_tool(
        "Ask for a gate your concern was not approved for. A plan-time "
        "allowance is granted when a concern is planned, and a need nobody "
        "could have foreseen — a rule that only meets its exception once two "
        "branches are joined — has no other route. This asks a human and "
        "waits, like any other question. A grant takes effect in this "
        "session, from the moment it is answered: retry the call that was "
        "refused and carry on from where you stopped. Nothing restarts, and "
        "nothing you have already done needs doing again.",
        name="request_allowance",
    )
    async def request_allowance(params: RequestAllowanceInput) -> AwaitAnswersOutput:
        return await ask_questions(
            QueueQuestionsInput(
                questions=[
                    AskedQuestion(
                        id=f"allow-{params.allowance}",
                        prompt=(
                            f"Grant `{params.allowance}` to {concern_id}?\n\n"
                            f"{params.reason}"
                        ),
                        choices=[ALLOWANCE_GRANTED, ALLOWANCE_REFUSED],
                    )
                ]
            )
        )

    @lup_tool(
        "Check the file account you are about to submit against what your "
        "worktree actually changed. Your report must name every path you "
        "changed, and must not claim to have swept a path you left alone — "
        "two directions, where correcting one is what violates the other. "
        "Call this before you submit: it runs the same reading the gate runs, "
        "so an account it settles is one the gate accepts. You cannot run git "
        "yourself here, and a report the gate rejects costs you a whole "
        "session to correct.",
        name="check_declaration",
    )
    async def check_declaration(
        params: CheckDeclarationInput,
    ) -> CheckDeclarationOutput:
        # HEAD is the commit the gate measures from: a worker holds no commit
        # authority, so the lease's head is where its turn opened and stays
        # there for as long as the turn runs.
        inspected = inspect_changes(inspector, lease_root, "HEAD")
        if inspected.failure:
            raise ToolError(inspected.failure)
        delta = declaration_delta(
            inspected.paths, params.files_changed, params.swept_beyond_scope
        )
        return CheckDeclarationOutput(
            settled=delta.settled,
            changed=sorted(path.as_posix() for path in inspected.paths),
            undeclared=delta.undeclared,
            unswept=delta.unswept,
            instruction=(
                "This account passes the declaration gate. Submit it as it stands."
                if delta.settled
                else f"{delta.reason}. Fix the account, not the work: add "
                "every undeclared path to files_changed, and drop every "
                "unswept path from swept_beyond_scope. Then call this again."
            ),
        )

    return [
        queue_questions,
        await_answers,
        ask_questions,
        send_message,
        request_allowance,
        check_declaration,
    ]
