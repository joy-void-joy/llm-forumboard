# lup: ignore[own-model-dispatch]
# A live turn returns whatever blocks the provider actually emitted, and
# picking the text ones out of that mixture is how this smoke observes the
# real result. It reads the union from outside on purpose: a helper inside the
# path being smoked could agree with itself while the boundary was broken.
"""Live smokes for the four historically fragile native boundaries.

These tests execute installed provider CLIs and incur real model calls. The
scheduled native lane supplies credentials and deliberately cheap models.
"""

import asyncio
import os
import shutil
from pathlib import Path

import pytest
import sh
from pydantic import BaseModel, Field

from lup.adapters.claude.harness import ClaudeSpellings
from lup.adapters.claude.runtime import (
    ClaudeSessionConfig,
    create_claude_session_factory,
)
from lup.adapters.codex.harness_runtime import CodexPluginInstaller, PluginCacheConfig
from lup.adapters.codex.runtime import CodexSessionConfig, create_codex_session_factory
from lup.harness.process import LocalProcessLauncher
from lup.resolver.core import ResolverCore
from lup.mcp import create_mcp_server, server_tool_names
from lup.channels.models import utc_now
from lup.resolver.mailbox import (
    AnswerDoor,
    AnswerOffer,
    QuestionMailbox,
)
from lup.resolver.tools import create_question_tools
from lup.resolver.models import (
    InventoryNote,
    ResolveRequest,
    ResolverConfig,
    SourceSnapshot,
    VerificationCommand,
    WorkerContext,
)
from lup.runtime.factory import SessionFactory
from lup.runtime.models import TurnTextBlock, turn_request

pytestmark = pytest.mark.integration

CLAUDE_SMOKE_MODEL = "claude-haiku-4-5-20251001"
CODEX_SMOKE_MODEL = "gpt-5.5"


async def test_fresh_claude_session_completes_one_turn(tmp_path: Path) -> None:
    """A fresh native session id survives one complete turn."""
    factory = create_claude_session_factory(
        ClaudeSessionConfig(
            model=CLAUDE_SMOKE_MODEL,
            system_prompt="Answer in one short sentence.",
            cwd=tmp_path,
            max_turns=1,
        )
    )
    async with factory.open() as handle:
        accepted = await handle.session.start(
            turn_request("Reply with the single word: ready")
        )
        result = await accepted.turn.result()

    assert result.identifiers.session.value
    assert result.identifiers.turn.value
    texts = [block.text for block in result.blocks if isinstance(block, TurnTextBlock)]
    assert any("ready" in text.lower() for text in texts)
    assert result.usage.output_tokens > 0


class SmokeSubmission(BaseModel):
    """Typed output carried by the dynamic tool on ``thread/start``."""

    message: str = Field(min_length=1)


async def test_codex_thread_start_carries_a_dynamic_tool(tmp_path: Path) -> None:
    """A typed binding survives the installed app-server schema."""
    factory = create_codex_session_factory(
        CodexSessionConfig(
            model=CODEX_SMOKE_MODEL,
            developer_instructions="Follow the submission instruction exactly.",
            cwd=tmp_path,
            sandbox="read-only",
            approval_policy="never",
        )
    )
    async with factory.open() as handle:
        accepted = await handle.session.start(
            turn_request(
                (
                    "Submit your output now: call the submission tool with "
                    "message set to 'smoke ok'."
                ),
                SmokeSubmission,
            )
        )
        result = await accepted.turn.result()

    assert result.output.message
    assert result.identifiers.session.value


class RecallSubmission(BaseModel):
    """Typed output whose schema is identical on both probe turns."""

    recalled: str = Field(min_length=1)


async def test_a_claude_session_carries_context_across_same_schema_turns(
    tmp_path: Path,
) -> None:
    """The second turn is the one that broke, so a smoke test has to take one.

    Every earlier smoke drove a single turn and passed while no worker could
    finish, because what fails is the turn boundary: the provider persists no
    headless transcript to resume, so a session that reconnects between turns
    starts each one cold. Nothing here resumes anything — the same live
    connection has to carry the first turn's word into the second.
    """
    factory = create_claude_session_factory(
        ClaudeSessionConfig(
            model=CLAUDE_SMOKE_MODEL,
            system_prompt="Call the submission tool. Never ask a question.",
            cwd=tmp_path,
        )
    )
    async with factory.open() as handle:
        first = await handle.session.start(
            turn_request(
                "Remember the word BATHYSPHERE. Submit it as recalled.",
                RecallSubmission,
            )
        )
        opening = await first.turn.result()
        second = await handle.session.start(
            turn_request(
                "Submit the word I asked you to remember, as recalled. "
                "Do not guess a new one.",
                RecallSubmission,
            )
        )
        result = await second.turn.result()

    assert "bathysphere" in opening.output.recalled.lower()
    # Carried by the conversation, not restated in the prompt above.
    assert "bathysphere" in result.output.recalled.lower()
    # The provider reports a fresh id per turn precisely because it persists
    # nothing to attach them to, so continuity is the recalled word rather
    # than a stable identifier.
    assert result.identifiers.session.value


async def answer_every_question(core: ResolverCore, stop: asyncio.Event) -> None:
    """Stand in for a human at the mailbox, answering each recommendation.

    The planner invents the concerns, so their question ids are not known
    until the run asks — which is exactly the case a door serves and a
    pre-seeded offer cannot.
    """
    while not stop.is_set():
        answered = core.mailbox.answered_ids()
        for item in core.mailbox.questions():
            if item.question.id in answered:
                continue
            core.mailbox.offer(
                AnswerOffer(
                    run_id=core.config.run_id,
                    question_id=item.question.id,
                    value=item.question.recommendation
                    or (item.question.choices[0] if item.question.choices else "yes"),
                    door=AnswerDoor.FLAG,
                    offered_at=utc_now(),
                )
            )
        try:
            async with asyncio.timeout(0.05):
                await stop.wait()
        except TimeoutError:
            continue


def fixture_repository(root: Path) -> Path:
    """Create a throwaway repository carrying one review note."""
    repo = root / "fixture-repo"
    repo.mkdir()
    git = sh.Command("git")
    git("init", "--initial-branch=main", _cwd=str(repo))
    git("config", "user.email", "smoke@example.invalid", _cwd=str(repo))
    git("config", "user.name", "Native Smoke", _cwd=str(repo))
    module = repo / "greeting.py"
    module.write_text(
        '# lup: rename GREETING_TEXT to WELCOME_TEXT\nGREETING_TEXT = "hello"\n',
        encoding="utf-8",
    )
    git("add", ".", _cwd=str(repo))
    git("commit", "-m", "chore: seed fixture", _cwd=str(repo))
    return repo


async def test_miniature_resolver_run_on_a_fixture_repository(tmp_path: Path) -> None:
    """The resolver process boundary captures plain, unpaged output."""
    repo = fixture_repository(tmp_path)
    launcher = LocalProcessLauncher()
    run_id = "smoke-run"

    def worker_factory(context: WorkerContext) -> SessionFactory:
        server = create_mcp_server(
            "resolver",
            tools=create_question_tools(
                QuestionMailbox(repo / ".lup" / "resolve" / run_id),
                context.concern_id,
                run_id=run_id,
                lease_root=context.root,
                wake=core.wake,
            ),
        )
        return create_claude_session_factory(
            ClaudeSessionConfig(
                model=CLAUDE_SMOKE_MODEL,
                system_prompt="Execute the persisted Lup resolver assignment.",
                cwd=context.root,
                add_dirs=[context.root],
                tool_servers={"resolver": server},
                allowed_tools=[
                    f"mcp__resolver__{name}" for name in server_tool_names(server)
                ],
            )
        )

    def reviewer_factory(cwd: Path) -> SessionFactory:
        return create_claude_session_factory(
            ClaudeSessionConfig(
                model=CLAUDE_SMOKE_MODEL,
                system_prompt="Independently review the persisted resolver change.",
                cwd=cwd,
                add_dirs=[cwd],
            )
        )

    from lup_template.devtools.harness.catalog import portable_harness

    core = ResolverCore(
        ResolverConfig(
            state_root=repo / ".lup" / "resolve",
            workspace=repo,
            worktree_root=tmp_path / f"fixture-repo-resolve-{run_id}",
            run_id=run_id,
            integration_branch=f"resolve/{run_id}/review",
            verification_commands=[
                VerificationCommand(name="status", arguments=["git", "status"])
            ],
        ),
        portable_harness().resolver,
        worker_factory,
        reviewer_factory,
        ClaudeSpellings(),
        launcher,
        answer_wait_seconds=120.0,
    )
    stop = asyncio.Event()
    answering = asyncio.create_task(answer_every_question(core, stop))
    head = str(sh.Command("git")("rev-parse", "HEAD", _cwd=str(repo))).strip()
    manifest = await core.run(
        ResolveRequest(
            source=SourceSnapshot(branch="main", commit=head),
            notes=[
                InventoryNote(
                    file=Path("greeting.py"),
                    line=1,
                    text="rename GREETING_TEXT to WELCOME_TEXT",
                    context='GREETING_TEXT = "hello"',
                )
            ],
        )
    )

    stop.set()
    await answering

    assert manifest.run_id == run_id
    assert manifest.review_branch
    branches = str(sh.Command("git")("branch", "--list", _cwd=str(repo)))
    assert manifest.review_branch in branches


class CodexEditFixture(BaseModel, frozen=True):
    """Paths participating in the blocked-edit smoke."""

    repository: Path
    module: Path


def codex_edit_fixture(root: Path) -> CodexEditFixture:
    """Create a repository whose requested edit violates the Lup policy."""
    repo = root / "codex-edit-fixture"
    repo.mkdir()
    sh.Command("git")("init", "--initial-branch=main", _cwd=str(repo))
    module = repo / "module.py"
    module.write_text('VALUE = "safe"\n', encoding="utf-8")
    return CodexEditFixture(repository=repo, module=module)


def test_codex_plugin_blocks_a_forbidden_apply_patch(tmp_path: Path) -> None:
    """A denied plugin hook keeps the file unchanged and the session alive."""
    root = Path.cwd()
    codex_home = tmp_path / "codex-home"
    # The isolated home keeps plugin state out of the user's config but must
    # still carry credentials — codex reads auth only from its CODEX_HOME.
    auth = Path.home() / ".codex" / "auth.json"
    if auth.exists():
        codex_home.mkdir(parents=True)
        shutil.copy(auth, codex_home / "auth.json")
    from lup_template.devtools.harness.catalog import portable_harness

    plugin = portable_harness().plugins[0]
    CodexPluginInstaller(
        PluginCacheConfig(codex_home=codex_home, marketplace=plugin.marketplace)
    ).ensure(
        root / ".codex" / "plugins" / plugin.name,
        root,
    )
    fixture = codex_edit_fixture(tmp_path)
    final_message = tmp_path / "codex-final.txt"
    environment = {
        **os.environ,  # lup: ignore[os-environ] — preserve provider credentials
        "CODEX_HOME": str(codex_home),
    }
    prompt = (
        "Use apply_patch exactly once to add `import re` and a call to "
        "`re.search` to module.py. Do not use any other tool. The Lup hook must "
        "reject that anti-pattern; after the rejection reply exactly hook-blocked."
    )

    # No --ignore-user-config: plugins (and their hooks) are recorded in the
    # isolated home's config.toml, which that flag would silently discard.
    sh.Command("codex")(
        "exec",
        "--enable",
        "hooks",
        "--dangerously-bypass-hook-trust",
        "--ephemeral",
        "--sandbox",
        "workspace-write",
        "--model",
        CODEX_SMOKE_MODEL,
        "--output-last-message",
        str(final_message),
        "--cd",
        str(fixture.repository),
        prompt,
        _env=environment,
    )

    assert fixture.module.read_text(encoding="utf-8") == 'VALUE = "safe"\n'
    assert final_message.read_text(encoding="utf-8").strip() == "hook-blocked"
