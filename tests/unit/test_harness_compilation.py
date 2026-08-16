"""Canonical declaration, native rendering, and reconciliation tests."""

import ast
import errno
import json
import os
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Literal, get_args

import pytest
import sh
import typer
from claude_agent_sdk.types import SandboxNetworkConfig, SandboxSettings
from pydantic import BaseModel, Field

from lup.policy.identity import AGENT_IDENTITY_ENV
from lup.types import JsonObject
from lup.adapters.claude.harness import CLAUDE_DISPATCHER, ClaudeSpellings
from lup.adapters.codex.harness import CODEX_DISPATCHER, CodexSpellings
from lup.adapters.codex.harness_runtime import (
    PluginCacheConfig,
    directory_digest,
    plugin_cache_evidence,
)
from lup.adapters.harness import (
    claude_prompt_renderer,
    codex_prompt_renderer,
    compile_claude,
    compile_codex,
)
from lup.codescan.registry import RULE_REFERENCE
from lup.harness.banner import (
    ARTIFACT_COMMENT_ROUTER,
    REGENERATE_COMMAND,
    GeneratedBanner,
)
from lup.harness.generation import ArtifactValidationError
from lup.harness.materialization import (
    AtomicMaterializer,
    MaterializationConflictError,
    discard_staged_write,
    refused_write,
)
from lup.harness.validation import validated_tree
from lup.harness.models import (
    GUIDANCE_BYTE_BUDGET,
    INVOCATION_SIGILS,
    Agent,
    Argument,
    Artifact,
    ArgumentsRef,
    ArtifactTree,
    AskUser,
    Delegate,
    Harness,
    InvocationArgument,
    MarkdownTable,
    NativePath,
    Plugin,
    PluginPath,
    PromptDocument,
    PromptPart,
    RelocateSession,
    RequestApproval,
    ResolverEntry,
    RuntimeDocs,
    SemanticPart,
    Skill,
    SkillInvocation,
    SkillPattern,
    SpellingExample,
    TextPart,
    document_byte_size,
)
from lup.harness.contracts import PromptRenderer
from lup.markdown import CodeCell, PlainCell
from lup.harness.ownership import (
    OwnershipManifest,
    OwnershipManifestError,
    build_manifest,
    content_digest,
    generated_artifacts,
    load_manifest,
    save_manifest,
)
from lup.harness.proposals import ReconciliationProposalWriter
from lup.harness.reconciliation import (
    CurrentArtifact,
    CurrentTree,
    DeterministicReconciler,
    FilesystemCurrentTreeReader,
    source_patch_base_digest,
)
from lup.policy.bundle import policy_kernel_modules
from lup.policy.dispatcher import (
    SHARED_MEMBER,
    SPLICED_MEMBERS,
    SHARED_PACKAGE,
    DispatcherDeclaration,
    SourceHalf,
    compile_dispatcher,
    resolvable,
    source_half,
)
from lup.types import EnvVars
from lup_template.agent.toolsets import EXAMPLE_GROUP, NOTES_GROUP, tool_group_names
from lup_template.devtools.agent.serve import (
    collect_tools_by_server,
    harness_session_context,
)
from lup.devtools.dev.rules import rule_reference_artifact
from lup_template.devtools.harness.catalog import (
    HARNESS_SESSION,
    declared_hook_set,
    portable_harness,
)
from lup_template.devtools.harness.content.docs.catalog import DOCUMENTS
from lup_template.devtools.harness.content.guidance import document as guidance_document
from lup_template.devtools.harness.content.settings import project_settings
from lup.devtools.harness import launch
from lup.devtools.harness.launch import (
    claude_sandbox_arguments,
    codex_sandbox_arguments,
)
from lup.policy.kernel.shell import sandbox_excluded
from lup_template.devtools.harness.content.template_claude import (
    DOCUMENT as TEMPLATE_CLAUDE,
)
from lup_template.devtools.harness.content.template_codex import (
    DOCUMENT as TEMPLATE_CODEX,
)
from lup_template.devtools.harness.composition import (
    claude_target,
    codex_target,
)
from lup.devtools.harness.generate import (
    GenerationRecipe,
    current_reader,
    manifest_of,
    generate,
    inspect_generation,
)

GUIDANCE = guidance_document(declared_hook_set().rules)
"""The guidance this repository actually ships, selection included."""


class ClaudeHookDecision(BaseModel, frozen=True):
    """Validated decision emitted by the generated Claude dispatcher."""

    hook_event_name: Literal["PreToolUse"] = Field(alias="hookEventName")
    permission_decision: Literal["allow", "ask", "deny"] = Field(
        alias="permissionDecision"
    )
    permission_decision_reason: str = Field(alias="permissionDecisionReason")


class ClaudeHookOutput(BaseModel, frozen=True):
    """Generated Claude hook output envelope."""

    hook_specific_output: ClaudeHookDecision = Field(alias="hookSpecificOutput")


class CodexPermissionDecision(BaseModel, frozen=True):
    """Validated permission decision emitted by the Codex dispatcher."""

    behavior: Literal["allow", "deny"]


class CodexPermissionHookOutput(BaseModel, frozen=True):
    """Codex PermissionRequest hook-specific output."""

    hook_event_name: Literal["PermissionRequest"] = Field(alias="hookEventName")
    decision: CodexPermissionDecision


class CodexPermissionOutput(BaseModel, frozen=True):
    """Generated Codex permission hook output envelope."""

    hook_specific_output: CodexPermissionHookOutput = Field(alias="hookSpecificOutput")


class ShippedDispatcher(BaseModel, frozen=True):
    """One hook dispatcher: its declaration, its half, its script, its runtime."""

    declaration: DispatcherDeclaration
    asset: Path
    script: Path
    runtime: Path


SHIPPED_DISPATCHERS: dict[str, ShippedDispatcher] = {
    "claude": ShippedDispatcher(
        declaration=CLAUDE_DISPATCHER,
        asset=Path("packages/lup/src/lup/adapters/claude/assets/policy_dispatcher.py"),
        script=Path(".claude/plugins/lup/hooks/scripts/policy.py"),
        runtime=Path(".claude/plugins/lup/hooks/runtime"),
    ),
    "codex": ShippedDispatcher(
        declaration=CODEX_DISPATCHER,
        asset=Path("packages/lup/src/lup/adapters/codex/assets/policy_dispatcher.py"),
        script=Path(".codex/plugins/lup/hooks/scripts/policy.py"),
        runtime=Path(".codex/plugins/lup/hooks/runtime"),
    ),
}
"""Every script a session's permissions run through, canonical and shipped."""

SHARED_DISPATCHER_HALF = Path("packages/lup/src/lup/policy/assets/host.py")
"""The host-side half both dispatchers above are compiled from."""


def compiled_functions(script: str) -> dict[str, str]:  # lup: ignore[dict-str-payload]
    """Every top-level function of a compiled dispatcher, by name.

    Open keys by construction: whatever the compiled source happens to define
    is what a reader of it can compare, which is the point of reading it back.
    """
    tree = ast.parse(script)
    return {
        node.name: ast.get_source_segment(script, node) or ""
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


class PyrightExecutionEnvironment(BaseModel, frozen=True):
    """One type-checking scope and the search paths it resolves imports on."""

    root: Path
    extra_paths: list[Path] = Field(alias="extraPaths", default=[])


class PyrightConfiguration(BaseModel, frozen=True):
    """The workspace type-checking scope, as pyproject.toml declares it."""

    include: list[Path]
    exclude: list[Path] = []
    execution_environments: list[PyrightExecutionEnvironment] = Field(
        alias="executionEnvironments", default=[]
    )


def test_catalog_has_one_portable_skill_per_baseline_command() -> None:
    harness = portable_harness()
    plugin = harness.plugins[0]

    assert len(plugin.skills) == 32
    assert len(plugin.agents) == 4
    assert {skill.name for skill in plugin.skills} >= {
        "resolve",
        "implementer",
        "resolve-reviewer",
        "merge",
    }
    encoded = harness.model_dump_json()
    assert "/lup:" not in encoded
    assert "$lup:" not in encoded


def test_claude_tree_renders_every_typed_support_document() -> None:
    paths = {
        artifact.path for artifact in claude_target(Path.cwd()).recipe.desired.artifacts
    }

    assert Path("docs/orchestration.md") in paths
    assert Path("docs/patterns.md") in paths
    assert Path(".claude/plugins/lup/TEMPLATE_CLAUDE.md") in paths
    assert Path(".claude/plugins/lup/scripts/file_suggest.sh") in paths
    assert Path(".claude/settings.json") in paths
    assert {document.path for document in DOCUMENTS} <= paths


def test_every_published_document_is_generated_and_banners_itself() -> None:
    """A document under docs/ that generation does not own could be hand-edited.

    The roster is the only source of documents, so an entry missing from the
    tree, a stray file beside them, or a page whose banner does not name its
    own module all mean a reader cannot trust the banner to be true.
    """
    artifacts = {
        artifact.path: artifact
        for artifact in claude_target(Path.cwd()).recipe.desired.artifacts
    }
    published = {document.path for document in DOCUMENTS}
    unmanaged = sorted(
        path for path in Path("docs").glob("*.md") if path not in published
    )

    assert unmanaged == [Path(RULE_REFERENCE)]
    for document in DOCUMENTS:
        banner = GeneratedBanner(
            source=document.document.declared_source(), command=REGENERATE_COMMAND
        )
        assert banner.opens(document.path, artifacts[document.path].content)


def test_guidance_reaches_sections_by_name_not_by_anchor() -> None:
    """Guidance carried links to sections that live only in the adopter template.

    A same-document anchor resolves right up until the text it names moves to
    another document, and then fails silently. Naming the section, or the file
    that holds it, survives the move that broke the anchor.
    """
    rendered = claude_prompt_renderer().render(GUIDANCE)

    assert "](#" not in rendered


def test_guidance_stays_within_its_always_loaded_budget() -> None:
    """The budget bounds what a session loads, not what the declaration holds.

    Measured in UTF-8 bytes, because that is the unit the runtime's own
    ceiling counts in — a character count runs looser than the real cap
    wherever the document uses non-ASCII punctuation, and would pass a
    document the runtime would silently truncate.
    """
    declared = GUIDANCE.text_size()
    rendered = {
        "claude": claude_prompt_renderer().render(GUIDANCE),
        "codex": codex_prompt_renderer().render(GUIDANCE),
    }

    for runtime, document in rendered.items():
        used = document_byte_size(document)
        assert used >= declared
        assert used <= GUIDANCE_BYTE_BUDGET, (
            f"{runtime} guidance is {used} bytes, over budget by "
            f"{used - GUIDANCE_BYTE_BUDGET}"
        )


def test_codex_config_states_the_same_ceiling_the_check_enforces() -> None:
    """A generated config that disagreed with the check would truncate silently."""
    config = next(
        artifact
        for artifact in codex_target(Path.cwd()).recipe.desired.artifacts
        if artifact.path.as_posix() == ".codex/config.toml"
    )
    assert f"project_doc_max_bytes = {GUIDANCE_BYTE_BUDGET}" in config.content


def test_codex_tree_renders_the_agents_flavored_template() -> None:
    paths = {
        artifact.path for artifact in codex_target(Path.cwd()).recipe.desired.artifacts
    }

    assert Path(".codex/plugins/lup/TEMPLATE_AGENTS.md") in paths


def test_template_flavors_share_sections_and_differ_natively() -> None:
    claude_render = claude_prompt_renderer().render(TEMPLATE_CLAUDE)
    codex_render = codex_prompt_renderer().render(TEMPLATE_CODEX)

    def sections(render: str) -> list[str]:
        return [
            line for line in render.splitlines() if line.startswith("<!-- section: ")
        ]

    assert sections(claude_render)[0] == "<!-- section: CLAUDE.md -->"
    assert sections(codex_render)[0] == "<!-- section: AGENTS.md -->"
    assert sections(claude_render)[1:] == sections(codex_render)[1:]
    assert "/lup:init" in claude_render and "$lup:" not in claude_render
    assert "$lup:init" in codex_render and "/lup:" not in codex_render
    assert "AskUserQuestion" in claude_render
    assert "AskUserQuestion" not in codex_render
    # Session relocation is a Claude tool; Codex can only be told to start
    # a session in the worktree, never to call one it does not have.
    assert "EnterWorktree" in claude_render and "ExitWorktree" in claude_render
    assert "EnterWorktree" not in codex_render
    assert "ExitWorktree" not in codex_render


def test_claude_recipe_overrides_legacy_hook_entry_with_hermetic_dispatcher() -> None:
    recipe = claude_target(Path.cwd()).recipe
    artifacts = {artifact.path: artifact for artifact in recipe.desired.artifacts}

    hook_config = artifacts[Path(".claude/plugins/lup/hooks/hooks.json")].content
    assert "uv run" not in hook_config
    assert "hooks/scripts/policy.py" in hook_config
    assert Path(".claude/plugins/lup/hooks/runtime/kernel/shell.py") in artifacts
    assert Path(".claude/plugins/lup/hooks/runtime/policy_data.py") in artifacts
    assert Path(".claude/plugins/lup/hooks/runtime/evidence.json") in artifacts


def test_codex_recipe_registers_semantic_permission_approval() -> None:
    recipe = codex_target(Path.cwd()).recipe
    artifacts = {artifact.path: artifact for artifact in recipe.desired.artifacts}

    hook_config = artifacts[Path(".codex/plugins/lup/hooks/hooks.json")].content
    assert '"PermissionRequest"' in hook_config
    assert "hooks/scripts/policy.py" in hook_config


def test_the_watching_event_is_registered_for_editing_tools_alone() -> None:
    """A narrow matcher, because this event is registered to record, not judge.

    The deciding events cover everything the dispatcher routes, so sharing
    one registration would spawn the script after every shell command and
    every fetch to find no edited file to record. The two are separate keys
    for that reason, and the compiler still proves the dispatcher may name
    the event at all.
    """
    for target, plugin_root, edits in (
        (claude_target, ".claude", "Edit|Write"),
        (codex_target, ".codex", "apply_patch"),
    ):
        artifacts = {
            artifact.path: artifact
            for artifact in target(Path.cwd()).recipe.desired.artifacts
        }
        registered = json.loads(
            artifacts[Path(f"{plugin_root}/plugins/lup/hooks/hooks.json")].content
        )["hooks"]

        assert registered["PostToolUse"][0]["matcher"] == edits
        assert registered["PreToolUse"][0]["matcher"] != edits


def test_generated_resolver_entries_only_launch_the_shared_python_core() -> None:
    harness = portable_harness()
    claude = {
        artifact.path: artifact.content
        for artifact in compile_claude(harness).artifacts
    }
    codex = {
        artifact.path: artifact.content for artifact in compile_codex(harness).artifacts
    }
    command = claude[Path(".claude/plugins/lup/commands/resolve.md")]
    skill = codex[Path(".codex/plugins/lup/skills/resolve/SKILL.md")]

    assert "uv run lup-devtools harness resolve --adapter claude --detach" in command
    assert "Triage into concerns" not in command
    assert "Workflow(" not in command
    assert "uv run lup-devtools harness resolve --adapter codex --detach" in skill
    assert "scheduling" not in skill
    for entry in (command, skill):
        assert "exactly one `status --watch`" in entry
        assert "event-driven waiter" in entry
        assert "--run-id" in entry and "--answer" in entry
        # The entry named flags the CLI has never had, and the acceptance
        # question it pointed at instead does not exist either. An entry
        # that documents a flag into being is worse than one that omits it:
        # the reader spends a turn on `No such option`.
        assert "--accept" not in entry and "--reject" not in entry
        assert "integration-assembly" in entry


def test_invocation_renderers_own_complete_spelling_and_escaping() -> None:
    invocation = SkillInvocation(
        plugin="lup",
        skill="merge",
        arguments=[InvocationArgument(name="target", value="feature with spaces")],
    )

    assert ClaudeSpellings().render(invocation) == (
        "/lup:merge target='feature with spaces'"
    )
    assert CodexSpellings().render(invocation) == (
        "$lup:merge target='feature with spaces'"
    )


def test_prompt_renderer_preserves_ordinary_trailing_whitespace() -> None:
    prompt = PromptDocument(parts=[TextPart(text="keep these spaces  ")])

    assert codex_prompt_renderer().render(prompt) == ("keep these spaces  \n")


def test_argument_reference_has_one_semantic_part_and_native_renderings() -> None:
    prompt = PromptDocument(parts=[ArgumentsRef()])

    assert claude_prompt_renderer().render(prompt) == ("$ARGUMENTS\n")
    assert codex_prompt_renderer().render(prompt) == (
        "the arguments supplied with this skill invocation\n"
    )


class PartExpectation(BaseModel, frozen=True):
    """One prompt part and whether its two native renderings must differ."""

    part: PromptPart
    diverges: bool


PART_CONTRACT: dict[str, PartExpectation] = {
    "TextPart": PartExpectation(part=TextPart(text="plain prose"), diverges=False),
    "SpellingExample": PartExpectation(
        part=SpellingExample(text="`/lup:merge` beside `$lup:merge`"), diverges=False
    ),
    "MarkdownTable": PartExpectation(
        part=MarkdownTable(
            headers=["Rule id", "Diagnostic"],
            rows=[[CodeCell(text="dict-get"), PlainCell(text="a | b")]],
        ),
        diverges=False,
    ),
    "SkillInvocation": PartExpectation(
        part=SkillInvocation(plugin="lup", skill="merge"), diverges=True
    ),
    "NativePath": PartExpectation(
        part=NativePath(location="ownership_manifest"), diverges=True
    ),
    "PluginPath": PartExpectation(
        part=PluginPath(plugin="lup", location="skills", member="merge"), diverges=True
    ),
    "SkillPattern": PartExpectation(
        part=SkillPattern(plugin="lup", placeholder="<name>"), diverges=True
    ),
    "RuntimeDocs": PartExpectation(part=RuntimeDocs(), diverges=True),
    "AskUser": PartExpectation(
        part=AskUser(question="which branch to land first"), diverges=True
    ),
    "Delegate": PartExpectation(
        part=Delegate(subagent_type="lup:trace-explorer", prompt="read the trace"),
        diverges=True,
    ),
    "RequestApproval": PartExpectation(
        part=RequestApproval(action="pushing", reason="it is visible"), diverges=False
    ),
    "RelocateSession": PartExpectation(
        part=RelocateSession(path="the path step 1 prints"), diverges=True
    ),
    "ResolverEntry": PartExpectation(part=ResolverEntry(), diverges=True),
    "ArgumentsRef": PartExpectation(part=ArgumentsRef(), diverges=True),
}
"""Every prompt part, with the cross-runtime promise its renderings make."""


def test_every_prompt_part_states_what_it_promises_across_runtimes() -> None:
    """A part neither renderer handles renders as nothing, silently.

    The base catches the kind that never says how it is spelled; this catches
    the kind nobody decided about — whether its two renderings agree is a
    design choice no type can hold.
    """
    union, _ = get_args(PromptPart.__value__)

    assert sorted(PART_CONTRACT) == sorted(
        member.__name__ for member in get_args(union)
    )


@pytest.mark.parametrize("name", sorted(PART_CONTRACT))
def test_each_prompt_part_keeps_its_cross_runtime_promise(name: str) -> None:
    expectation = PART_CONTRACT[name]
    prompt = PromptDocument(parts=[expectation.part])

    claude = claude_prompt_renderer().render(prompt)
    codex = codex_prompt_renderer().render(prompt)

    assert claude.strip() and codex.strip()
    assert (claude != codex) is expectation.diverges


class PartQuestion(BaseModel, frozen=True):
    """One question the harness asks a part, and the kinds that answer it."""

    ask: Callable[[SemanticPart], bool]
    answered_by: list[str]


PART_QUESTIONS: dict[str, PartQuestion] = {
    "text_payload": PartQuestion(
        ask=lambda part: part.text_payload is not None,
        answered_by=["TextPart", "SpellingExample", "MarkdownTable"],
    ),
    "invocation": PartQuestion(
        ask=lambda part: part.invocation is not None, answered_by=["SkillInvocation"]
    ),
    "named_plugin": PartQuestion(
        ask=lambda part: part.named_plugin is not None,
        answered_by=["SkillInvocation", "PluginPath", "SkillPattern"],
    ),
    "named_agent": PartQuestion(
        ask=lambda part: part.named_agent is not None, answered_by=["Delegate"]
    ),
    "references_arguments": PartQuestion(
        ask=lambda part: part.references_arguments, answered_by=["ArgumentsRef"]
    ),
}
"""Every question a walk asks a part rather than deciding about it from outside."""


@pytest.mark.parametrize("question", sorted(PART_QUESTIONS))
def test_each_question_is_answered_by_exactly_the_parts_that_carry_it(
    question: str,
) -> None:
    """A kind that carries something and declines to say so is the silent bug.

    Every question here defaults to declining, so a walk that asks it can never
    miss a kind by omission — but a kind that carries prose, or names a plugin,
    and forgets to answer would go unseen. Pinning who answers turns that into
    a failure the moment a new kind joins ``PART_CONTRACT``.
    """
    asked = PART_QUESTIONS[question]

    answering = [
        name
        for name, expectation in PART_CONTRACT.items()
        if asked.ask(expectation.part)
    ]

    assert answering == asked.answered_by


@pytest.mark.parametrize("name", sorted(PART_CONTRACT))
def test_each_prompt_part_round_trips_through_its_discriminator(name: str) -> None:
    """Answering for itself leaves a part exactly as parseable as it was."""
    document = PromptDocument(parts=[PART_CONTRACT[name].part])

    restored = PromptDocument.model_validate_json(document.model_dump_json())

    assert restored == document
    assert type(restored.parts[0]) is type(document.parts[0])


def test_prompt_documents_parse_every_part_from_its_discriminator() -> None:
    """One document holding every kind still validates through ``type`` alone."""
    payload = {
        "parts": [
            expectation.part.model_dump() for expectation in PART_CONTRACT.values()
        ]
    }

    document = PromptDocument.model_validate(payload)

    assert [type(part).__name__ for part in document.parts] == list(PART_CONTRACT)


def test_an_undeclared_plugin_behind_an_invocation_names_the_invocation() -> None:
    """One part answers two questions, and the sharper gate must speak first.

    An invocation names a plugin, so both the plugin gate and the invocation
    gate see it — but only one of them can say which skill went missing.
    """
    source = portable_harness().model_dump()
    source["guidance"]["parts"].append(
        SkillInvocation(plugin="absent", skill="merge").model_dump()
    )

    with pytest.raises(ValueError, match="unknown declaration: absent:merge"):
        Harness.model_validate(source)


class UnansweredPart(SemanticPart, frozen=True):
    """A thirteenth kind that declines to say how it should be spelled."""

    type: Literal["unanswered"] = "unanswered"


class AnsweredPart(SemanticPart, frozen=True):
    """A thirteenth kind that answers the base and declines everything else."""

    type: Literal["answered"] = "answered"

    def spell(self, renderer: PromptRenderer) -> str:
        return f"{renderer.own.runtime_name} answered"


def test_a_part_that_answers_nothing_cannot_be_constructed() -> None:
    """The base is what forces a new kind to decide, not a walk that meets it."""

    def construct(kind: type[SemanticPart]) -> SemanticPart:
        return kind()

    assert construct(AnsweredPart)

    with pytest.raises(TypeError, match="abstract"):
        construct(UnansweredPart)


def test_a_new_kind_of_part_renders_without_editing_a_renderer_or_walk() -> None:
    """The walk hands over the reader's vocabulary and never names a kind."""
    document = PromptDocument.model_construct(parts=[AnsweredPart()])

    assert claude_prompt_renderer().render(document) == (
        f"{ClaudeSpellings().runtime_name} answered\n"
    )
    assert codex_prompt_renderer().render(document) == (
        f"{CodexSpellings().runtime_name} answered\n"
    )
    assert document.text_size() == 0


def test_every_tree_paths_read_the_same_under_either_runtime() -> None:
    """Prose teaching every tree must not depend on which runtime renders it."""
    prompt = PromptDocument(
        parts=[
            NativePath(location="guidance_file", scope="every_tree"),
            TextPart(text=" and "),
            PluginPath(plugin="lup", location="skills", scope="every_tree"),
        ]
    )

    rendered = claude_prompt_renderer().render(prompt)

    assert rendered == codex_prompt_renderer().render(prompt)
    assert rendered == (
        ".claude/CLAUDE.md under Claude Code, AGENTS.md under Codex and "
        ".claude/plugins/lup/commands/ under Claude Code, "
        ".codex/plugins/lup/skills/ under Codex\n"
    )


@pytest.mark.parametrize(
    ("arguments", "parts"),
    [
        ([Argument(name="target", description="Target")], [TextPart(text="body")]),
        ([], [ArgumentsRef()]),
    ],
)
def test_skill_argument_declarations_require_a_matching_reference(
    arguments: list[Argument], parts: list[PromptPart]
) -> None:
    with pytest.raises(ValueError, match="argument declarations and ArgumentsRef"):
        Skill(
            id="skill.invalid",
            name="invalid",
            description="Invalid argument declaration",
            arguments=arguments,
            prompt=PromptDocument(parts=parts),
        )


def test_every_typed_content_module_is_reachable_from_a_catalog() -> None:
    """An orphaned content module renders into no tree and drifts unnoticed.

    Importing the generation recipes pulls in every declaration a catalog
    aggregates, so a module still on disk but absent from ``sys.modules`` is
    one no artifact is rendered from — a retired skill left behind, or a
    document nobody listed.
    """
    content = Path("src/lup_template/devtools/harness/content")
    loaded = {
        Path(source).resolve()
        for source in (
            getattr(module, "__file__", None) for module in list(sys.modules.values())
        )
        if source is not None
    }
    orphans = [
        path.as_posix()
        for path in sorted(content.rglob("*.py"))
        if path.name != "__init__.py" and path.resolve() not in loaded
    ]

    assert not orphans


def test_source_tree_contains_no_embedded_base64() -> None:
    sources = list(Path("src").rglob("*.py"))

    assert sources
    assert all("base64" not in path.read_text(encoding="utf-8") for path in sources)


def test_retired_native_catalog_paths_stay_deleted() -> None:
    harness = Path("src/lup_template/devtools/harness")

    assert not (harness / "native_catalog.py").exists()
    assert not (harness / "native_overrides.py").exists()
    assert not (harness / "importer.py").exists()


INVOCATION_IN_PROSE = "then run /lup:merge and wait"

PROSE_DECLARATIONS: dict[
    str, Callable[[str], Agent | Argument | Plugin | SemanticPart | Skill]
] = {
    "Agent.description": lambda prose: Agent(
        id="agent.probe",
        name="probe",
        description=prose,
        prompt=PromptDocument(parts=[TextPart(text="body")]),
    ),
    "Argument.description": lambda prose: Argument(name="target", description=prose),
    "AskUser.question": lambda prose: AskUser(question=prose),
    "Delegate.prompt": lambda prose: Delegate(
        subagent_type="lup:trace-explorer", prompt=prose
    ),
    "Plugin.description": lambda prose: Plugin(
        id="plugin.probe",
        name="probe",
        marketplace="probe",
        version="0.0.0",
        description=prose,
        skills=[],
        agents=[],
    ),
    "RelocateSession.path": lambda prose: RelocateSession(path=prose),
    "RequestApproval.action": lambda prose: RequestApproval(
        action=prose, reason="it is visible"
    ),
    "RequestApproval.reason": lambda prose: RequestApproval(
        action="pushing", reason=prose
    ),
    "Skill.argument_hint": lambda prose: Skill(
        id="skill.probe",
        name="probe",
        description="Probe",
        argument_hint=prose,
        prompt=PromptDocument(parts=[TextPart(text="body")]),
    ),
    "Skill.description": lambda prose: Skill(
        id="skill.probe",
        name="probe",
        description=prose,
        prompt=PromptDocument(parts=[TextPart(text="body")]),
    ),
    "TextPart.text": lambda prose: TextPart(text=prose),
}
"""Every field a declaration holds as free text, and how one is declared.

A native tree renders each of these as prose, so each is the whole of the way
an invocation could reach a reader who cannot use it."""


NAMES_RATHER_THAN_PROSE = [
    "Agent.id",
    "Harness.generator_version",
    "Plugin.id",
    "Plugin.version",
    "Skill.id",
    "SpellingExample.text",
]
"""Declared strings deliberately exempt from the portable-prose constraint.

Most identify or version a declaration rather than teaching anything, so they
never reach a reader as words. ``SpellingExample.text`` is the one that does
and is exempt anyway: its whole subject is what each runtime spells, which is
the one thing portable prose cannot say. Every other free-text field a prompt
or its discovery metadata carries is portable, and a new field has to join one
list or the other rather than quietly accepting anything."""


def test_every_free_text_declaration_field_is_portable_prose() -> None:
    """A field typed plain ``str`` is the one way back to scanning afterwards."""
    union, _ = get_args(PromptPart.__value__)
    declarations = [*get_args(union), Argument, Skill, Agent, Plugin, Harness]

    unconstrained = [
        f"{declaration.__name__}.{name}"
        for declaration in declarations
        for name, field in declaration.model_fields.items()
        if not field.metadata and field.annotation in (str, str | None)
    ]

    assert sorted(unconstrained) == NAMES_RATHER_THAN_PROSE


@pytest.mark.parametrize("field", sorted(PROSE_DECLARATIONS))
def test_declaring_an_invocation_in_prose_is_refused(field: str) -> None:
    """The words are refused where an author writes them, not where they compile."""
    declare = PROSE_DECLARATIONS[field]

    assert declare("then run the merge skill and wait")

    with pytest.raises(ValueError, match="portable prose spells"):
        declare(INVOCATION_IN_PROSE)


def test_a_refused_declaration_names_its_field_and_the_offending_spelling() -> None:
    """Naming both is what lets a contributor go straight to the words."""
    with pytest.raises(ValueError) as refusal:
        Agent(
            id="agent.probe",
            name="probe",
            description=INVOCATION_IN_PROSE,
            prompt=PromptDocument(parts=[TextPart(text="body")]),
        )

    report = str(refusal.value)
    assert "Agent" in report
    assert "description" in report
    assert "'/lup:merge'" in report


def test_no_runtime_spells_an_invocation_portable_prose_would_admit() -> None:
    """The shape is syntax, so each runtime proves its own sigil is one of them.

    That is what lets the declaration layer refuse an invocation without
    knowing which plugins exist or which runtime will read the words.
    """
    for runtime in (ClaudeSpellings(), CodexSpellings()):
        for spelling in (
            runtime.render(SkillInvocation(plugin="lup", skill="merge")),
            runtime.invocation_pattern("lup", "*"),
            runtime.invocation_pattern("lup", "<name>"),
        ):
            assert spelling[0] in INVOCATION_SIGILS
            with pytest.raises(ValueError, match="portable prose spells"):
                TextPart(text=f"Run {spelling}")


def test_deserializing_a_harness_refuses_an_invocation_in_guidance() -> None:
    """Reading a harness back is a declaration too, and refuses the same words."""
    source = portable_harness().model_dump()
    source["guidance"]["parts"].append({"type": "text", "text": "Run $lup:merge"})

    with pytest.raises(ValueError, match="portable prose spells"):
        Harness.model_validate(source)


def test_artifact_paths_reject_backslash_traversal() -> None:
    with pytest.raises(ValueError, match="beneath its root"):
        Artifact(path=Path("..\\secret"), content="x", semantic_id="unsafe")


def test_both_native_trees_compile_deterministically() -> None:
    harness = portable_harness()
    claude = compile_claude(harness)
    codex = compile_codex(harness)

    assert claude == compile_claude(harness)
    assert codex == compile_codex(harness)
    assert (
        len([item for item in claude.artifacts if "/commands/" in item.path.as_posix()])
        == 32
    )
    assert len([item for item in codex.artifacts if item.path.name == "SKILL.md"]) == 32
    assert Path(".codex/plugins/lup/.codex-plugin/plugin.json") in {
        item.path for item in codex.artifacts
    }
    assert Path(".codex/rules/lup.rules") in {item.path for item in codex.artifacts}
    assert Path("AGENTS.md") in {item.path for item in codex.artifacts}

    def shipped_kernel(artifacts: list[Artifact]) -> list[str]:
        """Render every shipped kernel module as one comparable block."""
        return sorted(
            f"{item.path.name}\n{item.content}"
            for item in artifacts
            if "hooks/runtime/kernel/" in item.path.as_posix()
        )

    canonical = sorted(
        f"{module.name}\n{module.source}" for module in policy_kernel_modules()
    )
    assert shipped_kernel(claude.artifacts) == shipped_kernel(codex.artifacts)
    assert shipped_kernel(claude.artifacts) == canonical


def test_codex_compiles_prefix_safe_shell_allows_to_native_rules() -> None:
    artifacts = {
        artifact.path: artifact.content
        for artifact in compile_codex(portable_harness()).artifacts
    }
    rules = artifacts[Path(".codex/rules/lup.rules")]

    assert 'pattern = ["uv", "run", "lup-devtools"]' in rules
    assert 'pattern = ["git", "status"]' in rules
    assert 'pattern = ["gh", "pr", "view"]' in rules
    assert 'pattern = ["uv"]' not in rules
    assert 'pattern = ["uv", "run", "pytest"]' not in rules
    assert 'pattern = ["env"]' not in rules
    assert 'pattern = ["sort"]' not in rules
    assert 'pattern = ["git", "push"]' not in rules


def test_every_commentable_generated_file_carries_the_one_banner_form() -> None:
    harness = portable_harness()
    root = Path.cwd()
    trees = [
        claude_target(root).recipe.desired,
        codex_target(root).recipe.desired,
        ArtifactTree(artifacts=[rule_reference_artifact()]),
    ]
    bannered = [
        (artifact, artifact.banner)
        for tree in trees
        for artifact in tree.artifacts
        if isinstance(artifact.banner, GeneratedBanner)
    ]

    assert {Path("docs/rules.md"), Path("docs/permissions.md")} <= {
        artifact.path for artifact, _ in bannered
    }
    for artifact, banner in bannered:
        assert banner.opens(artifact.path, artifact.content)
        assert f"Generated from {banner.source} by " in artifact.content
        assert f"`{banner.command}`" in artifact.content
    for tree in trees:
        for artifact in tree.artifacts:
            spelled = ARTIFACT_COMMENT_ROUTER.route_for(artifact.path)
            assert (artifact.banner is not None) == (spelled is not None)
    assert harness == portable_harness()


def test_a_generated_file_that_states_no_provenance_fails_the_check() -> None:
    silent = Artifact(
        path=Path("docs/invented.md"), content="# Invented\n", semantic_id="docs.new"
    )

    with pytest.raises(ArtifactValidationError, match="declares no generated-from"):
        validated_tree([silent])


def test_a_banner_the_content_does_not_open_with_is_rejected() -> None:
    banner = GeneratedBanner(source="lup_template.invented", command="uv run invent")

    with pytest.raises(ValueError, match="does not open with the banner"):
        Artifact(
            path=Path("docs/invented.md"),
            content="# Invented\n",
            semantic_id="docs.new",
            banner=banner,
        )


def test_repository_generated_harness_is_drift_clean() -> None:
    reports = [
        inspect_generation(claude_target(Path.cwd()).recipe),
        inspect_generation(codex_target(Path.cwd()).recipe),
    ]

    assert all(report.clean for report in reports)


def test_generic_generation_accepts_an_injected_third_recipe(tmp_path: Path) -> None:
    desired = ArtifactTree(
        artifacts=[
            Artifact(
                path=Path(".third/plugin.txt"),
                content="third-provider\n",
                semantic_id="third.plugin",
            )
        ]
    )
    recipe = GenerationRecipe(
        label="third-provider",
        root=tmp_path,
        source=portable_harness(),
        desired=desired,
        manifest_path=tmp_path / ".third" / ".lup-ownership.json",
        prior=None,
        reader=current_reader(None, desired, sensitive_local_only=[]),
        target_requirements=["third-cli>=1"],
    )

    report = inspect_generation(recipe)

    assert report.target == "third-provider"
    assert [write.artifact.path for write in report.proposal.writes] == [
        Path(".third/plugin.txt")
    ]
    generated = generate(recipe)
    assert generated.target == "third-provider"
    assert (tmp_path / ".third" / "plugin.txt").read_text(encoding="utf-8") == (
        "third-provider\n"
    )
    assert recipe.manifest_path.is_file()


def third_recipe(tmp_path: Path, prior: OwnershipManifest | None) -> GenerationRecipe:
    """One injected recipe over a settled tree, reading *prior* as its proof."""
    desired = ArtifactTree(
        artifacts=[
            Artifact(
                path=Path(".third/plugin.txt"),
                content="third-provider\n",
                semantic_id="third.plugin",
            )
        ]
    )
    return GenerationRecipe(
        label="third-provider",
        root=tmp_path,
        source=portable_harness(),
        desired=desired,
        manifest_path=tmp_path / ".third" / ".lup-ownership.json",
        prior=prior,
        reader=current_reader(prior, desired, sensitive_local_only=[]),
        target_requirements=["third-cli>=1"],
    )


def test_a_regenerated_tree_reports_its_proof_current(tmp_path: Path) -> None:
    generate(third_recipe(tmp_path, None))
    settled = manifest_of(third_recipe(tmp_path, None))

    report = inspect_generation(third_recipe(tmp_path, settled))

    assert report.manifest_current
    assert report.clean


def test_a_proof_from_another_tree_is_stale_though_the_artifacts_match(
    tmp_path: Path,
) -> None:
    """The merge case: the driver keeps one side's proof, the artifacts merge clean.

    Reading only the artifacts calls that settled, and the regeneration the
    conflict existed to force becomes the step nothing asks for.
    """
    generate(third_recipe(tmp_path, None))
    settled = manifest_of(third_recipe(tmp_path, None))
    kept = settled.model_copy(update={"source_digest": "0" * 64})

    report = inspect_generation(third_recipe(tmp_path, kept))

    assert not report.proposal.writes, "the artifacts themselves are current"
    assert not report.manifest_current
    assert not report.clean


def test_reconciliation_preserves_local_and_sensitive_collisions(
    tmp_path: Path,
) -> None:
    local_content = "local\n"
    secret_content = "credential\n"
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("local.txt"),
                content=local_content,
                category="local_only",
                sha256=content_digest(local_content),
            ),
            CurrentArtifact(
                path=Path("secret.txt"),
                content="",
                category="sensitive_local_only",
                sha256=content_digest(secret_content),
            ),
        ],
    )
    desired = ArtifactTree(
        artifacts=[
            Artifact(path=Path("local.txt"), content="generated", semantic_id="local"),
            Artifact(
                path=Path("secret.txt"), content="generated", semantic_id="secret"
            ),
        ]
    )

    proposal = DeterministicReconciler().propose(current, desired)

    assert len(proposal.conflicts) == 2
    assert proposal.conflicts[1].sensitive
    with pytest.raises(MaterializationConflictError):
        AtomicMaterializer().apply(proposal)


def test_materialization_rejects_stale_base(tmp_path: Path) -> None:
    path = tmp_path / "owned.txt"
    path.write_text("old\n", encoding="utf-8")
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("owned.txt"),
                content="old\n",
                category="generated",
                sha256=content_digest("old\n"),
            )
        ],
    )
    desired = ArtifactTree(
        artifacts=[Artifact(path=Path("owned.txt"), content="new", semantic_id="owned")]
    )
    proposal = DeterministicReconciler().propose(current, desired)
    path.write_text("changed locally\n", encoding="utf-8")

    with pytest.raises(MaterializationConflictError, match="stale base"):
        AtomicMaterializer().apply(proposal)


def test_a_refused_write_names_the_boundary_and_drops_its_staging(
    tmp_path: Path,
) -> None:
    """A runtime protects its own configuration by mounting it, not by mode.

    So a sandboxed session replacing one of those paths is refused with a
    busy device — an errno about hardware, which sends a reader looking at
    the disk instead of at the boundary that actually decided.
    """
    staged = tmp_path / ".settings.json.abc123.tmp"
    staged.write_text("staged\n", encoding="utf-8")
    error = OSError(errno.EBUSY, "Device or resource busy", str(staged))
    error.filename2 = str(tmp_path / "settings.json")

    discard_staged_write(error)
    refusal = str(refused_write(error))

    assert not staged.exists()
    assert "settings.json" in refusal and ".tmp" not in refusal
    assert "sandbox" in refusal


def test_materialization_rejects_symlink_path_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    current = CurrentTree(root=root, artifacts=[])
    desired = ArtifactTree(
        artifacts=[
            Artifact(
                path=Path("link/escaped.txt"),
                content="unsafe",
                semantic_id="escape",
            )
        ]
    )
    proposal = DeterministicReconciler().propose(current, desired)

    with pytest.raises(MaterializationConflictError, match="symlink"):
        AtomicMaterializer().apply(proposal)
    assert not (outside / "escaped.txt").exists()


def test_exact_generated_content_can_acquire_first_ownership(tmp_path: Path) -> None:
    content = "generated\n"
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("new.txt"),
                content=content,
                category="unknown_conflict",
                sha256=content_digest(content),
            )
        ],
    )
    desired = ArtifactTree(
        artifacts=[Artifact(path=Path("new.txt"), content=content, semantic_id="new")]
    )

    proposal = DeterministicReconciler().propose(current, desired)

    assert proposal.conflicts == []
    assert proposal.writes == []


def test_interrupted_exact_write_can_reacquire_prior_ownership(tmp_path: Path) -> None:
    content = "new generated content\n"
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("owned.txt"),
                content=content,
                category="backpropagation_candidate",
                sha256=content_digest(content),
            )
        ],
    )
    desired = ArtifactTree(
        artifacts=[
            Artifact(path=Path("owned.txt"), content=content, semantic_id="owned")
        ]
    )

    proposal = DeterministicReconciler().propose(current, desired)

    assert proposal.conflicts == []
    assert proposal.writes == []


def test_an_owned_path_the_generator_disagrees_with_is_regenerated(
    tmp_path: Path,
) -> None:
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("owned.txt"),
                content="what a merge left behind\n",
                category="backpropagation_candidate",
                sha256=content_digest("what a merge left behind\n"),
            )
        ],
    )
    desired = ArtifactTree(
        artifacts=[
            Artifact(
                path=Path("owned.txt"),
                content="what the generator wants\n",
                semantic_id="owned",
            )
        ]
    )

    proposal = DeterministicReconciler().propose(current, desired)

    # A resolver join merges both plugin trees and their ownership proof, so
    # the recorded digest ends up describing neither parent. Read as an edit
    # nobody made, that refused generation outright and left no recovery a
    # worker could reach.
    assert proposal.conflicts == []
    assert [write.artifact.path.as_posix() for write in proposal.writes] == [
        "owned.txt"
    ]


def test_native_override_does_not_silently_reown_backpropagation(
    tmp_path: Path,
) -> None:
    content = "locally changed native override\n"
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("owned.txt"),
                content=content,
                category="backpropagation_candidate",
                sha256=content_digest(content),
            )
        ],
    )
    desired = ArtifactTree(
        artifacts=[
            Artifact(path=Path("owned.txt"), content=content, semantic_id="owned")
        ]
    )

    proposal = DeterministicReconciler(adopt_exact_backpropagation=False).propose(
        current, desired
    )

    assert [conflict.category for conflict in proposal.conflicts] == [
        "backpropagation_candidate"
    ]


def test_codex_cache_digest_requires_an_exact_separate_copy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    home = tmp_path / "home"
    # codex caches an installed plugin under its manifest version segment.
    installed = home / "plugins" / "cache" / "lup-template-repository" / "lup" / "9.9.9"
    source.mkdir()
    installed.mkdir(parents=True)
    for root in (source, installed):
        manifest_dir = root / ".codex-plugin"
        manifest_dir.mkdir()
        (manifest_dir / "plugin.json").write_text(
            '{"name": "lup", "version": "9.9.9"}\n', encoding="utf-8"
        )
        (root / "plugin.txt").write_text("same\n", encoding="utf-8")
    config = PluginCacheConfig(codex_home=home, marketplace="lup-template-repository")

    assert plugin_cache_evidence(source, config).ready
    (installed / "plugin.txt").write_text("stale\n", encoding="utf-8")
    assert not plugin_cache_evidence(source, config).ready


def test_codex_cache_digest_ignores_python_bytecode(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    (root / "__pycache__").mkdir(parents=True)
    (root / "policy.py").write_text("pass\n", encoding="utf-8")
    before = directory_digest(root)

    (root / "__pycache__" / "policy.cpython-314.pyc").write_bytes(b"cache")

    assert directory_digest(root) == before


def test_binary_managed_file_becomes_typed_unknown_conflict(tmp_path: Path) -> None:
    path = Path("managed.md")
    (tmp_path / path).write_bytes(b"\xff\xfe")

    tree = FilesystemCurrentTreeReader(None, managed_paths=[path]).read(tmp_path)

    assert tree.artifacts[0].category == "unknown_conflict"
    assert tree.artifacts[0].content == ""


def test_executable_mode_drift_is_not_treated_as_generated(tmp_path: Path) -> None:
    path = Path("hook.py")
    target = tmp_path / path
    target.write_text("pass\n", encoding="utf-8")
    target.chmod(0o644)
    source = portable_harness()
    manifest = build_manifest(
        source,
        ArtifactTree(
            artifacts=[
                Artifact(
                    path=path,
                    content="pass\n",
                    semantic_id="hook",
                    executable=True,
                )
            ]
        ),
        generator_version="test",
        target_requirements=[],
    )

    tree = FilesystemCurrentTreeReader(manifest, managed_paths=[path]).read(tmp_path)

    assert tree.artifacts[0].category == "generated"
    proposal = DeterministicReconciler().propose(
        tree,
        ArtifactTree(
            artifacts=[
                Artifact(
                    path=path,
                    content="pass\n",
                    semantic_id="hook",
                    executable=True,
                )
            ]
        ),
    )
    assert proposal.writes[0].artifact.executable


def codex_hook_result(body: JsonObject, sandboxed: bool) -> sh.RunningCommand:
    environment = {**os.environ, "LUP_SANDBOX_ACTIVE": "1" if sandboxed else "0"}
    return sh.Command(
        str(Path(".codex/plugins/lup/hooks/scripts/policy.py").resolve())
    )(_in=json.dumps(body), _env=environment, _ok_code=[0, 2], _return_cmd=True)


def test_generated_codex_hook_fails_closed_for_inline_code() -> None:
    script = Path(".codex/plugins/lup/hooks/scripts/policy.py").resolve()
    result = sh.Command(str(script))(
        _in='{"tool_name":"Bash","tool_input":{"command":"python -c 1"}}',
        _ok_code=[0, 2],
        _return_cmd=True,
    )
    assert isinstance(result, sh.RunningCommand)
    assert result.exit_code == 2
    assert b"interpreters" in result.stderr


def test_generated_codex_pretool_accepts_a_safe_requested_escape() -> None:
    body: JsonObject = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {
            "command": "uv run lup-devtools harness resolve intake",
            "sandbox_permissions": "require_escalated",
        },
    }
    assert codex_hook_result(body, sandboxed=True).exit_code == 0


def test_generated_codex_pretool_accepts_a_safe_automatic_escape() -> None:
    body: JsonObject = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "uv run lup-devtools harness resolve intake"},
    }
    assert codex_hook_result(body, sandboxed=True).exit_code == 0


def test_generated_codex_pretool_defers_an_escape_only_native_codex_can_see() -> None:
    body: JsonObject = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git diff --check"},
    }
    assert codex_hook_result(body, sandboxed=True).exit_code == 0


def test_generated_codex_pretool_refuses_an_ambient_escape() -> None:
    body: JsonObject = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {
            "command": "cat README.md",
            "sandbox_permissions": "require_escalated",
        },
    }
    assert codex_hook_result(body, sandboxed=True).exit_code == 2


def test_generated_codex_permission_request_allows_safe_resolver() -> None:
    script = Path(".codex/plugins/lup/hooks/scripts/policy.py").resolve()
    body = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {
            "command": (
                "UV_CACHE_DIR=/tmp/lup-uv-cache uv run lup-devtools "
                "harness resolve --adapter codex"
            )
        },
    }
    result = sh.Command(str(script))(
        _in=json.dumps(body),
        _ok_code=[0, 2],
        _return_cmd=True,
    )
    assert isinstance(result, sh.RunningCommand)
    output = CodexPermissionOutput.model_validate_json(result.stdout)
    assert result.exit_code == 0
    assert output.hook_specific_output.decision.behavior == "allow"


def test_generated_codex_permission_request_preserves_human_approval() -> None:
    script = Path(".codex/plugins/lup/hooks/scripts/policy.py").resolve()
    body = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": "# lup: escalate: required diagnostic\npython -c 1"},
    }
    result = sh.Command(str(script))(
        _in=json.dumps(body),
        _ok_code=[0, 2],
        _return_cmd=True,
    )
    assert isinstance(result, sh.RunningCommand)
    assert result.exit_code == 0
    assert result.stdout == b""


def test_generated_codex_permission_request_denies_unapproved_code() -> None:
    script = Path(".codex/plugins/lup/hooks/scripts/policy.py").resolve()
    body = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": "python -c 1"},
    }
    result = sh.Command(str(script))(
        _in=json.dumps(body),
        _ok_code=[0, 2],
        _return_cmd=True,
    )
    assert isinstance(result, sh.RunningCommand)
    assert result.exit_code == 2
    assert b"interpreters" in result.stderr


def test_generated_codex_hook_refuses_the_declared_calls() -> None:
    """The refusal table is consulted on both runtimes, not only on Claude.

    Everything the refusal is made of is portable — the field on ``HookSet``,
    the kernel module both trees carry, the rows this renderer emits — so a
    tree that shipped all of it and never asked would read as a refusal in
    force while the call went through. These two names are Claude's own
    spellings and match nothing Codex offers; what is pinned is that the
    mechanism reaches this dispatcher, for whatever an adopter refuses here.
    """
    hook = sh.Command(str(Path(".codex/plugins/lup/hooks/scripts/policy.py").resolve()))

    def run(name: str, payload: JsonObject) -> sh.RunningCommand:
        body = json.dumps({"tool_name": name, "tool_input": payload})
        result = hook(_in=body, _ok_code=[0, 2], _return_cmd=True)
        assert isinstance(result, sh.RunningCommand)
        return result

    refused = run("Artifact", {"content": "a page"})
    assert refused.exit_code == 2
    assert b"lup-devtools report" in refused.stderr

    narrowed = run("Skill", {"skill": "artifact-design"})
    assert narrowed.exit_code == 2

    other = run("Skill", {"skill": "lup:commit"})
    assert other.exit_code == 0
    assert other.stdout == b""


def test_generated_codex_hook_allows_managed_skill_scripts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    script = Path(".codex/plugins/lup/hooks/scripts/policy.py").resolve()
    helper = codex_home / "skills/.system/openai-docs/scripts/fetch-codex-manual.mjs"
    body = {"tool_name": "Bash", "tool_input": {"command": f"node {helper}"}}
    allowed = sh.Command(str(script))(
        _in=json.dumps(body),
        _ok_code=[0, 2],
        _return_cmd=True,
    )
    assert isinstance(allowed, sh.RunningCommand)
    assert allowed.exit_code == 0

    body["tool_input"]["command"] = "node /tmp/untrusted-script.mjs"
    denied = sh.Command(str(script))(
        _in=json.dumps(body),
        _ok_code=[0, 2],
        _return_cmd=True,
    )
    assert isinstance(denied, sh.RunningCommand)
    assert denied.exit_code == 2


def test_generated_claude_hook_allows_managed_skill_scripts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "claude-profile"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    script = Path(".claude/plugins/lup/hooks/scripts/policy.py").resolve()

    def decision(command: str) -> str:
        body = {"tool_name": "Bash", "tool_input": {"command": command}}
        result = sh.Command(str(script))(_in=json.dumps(body), _return_cmd=True)
        assert isinstance(result, sh.RunningCommand)
        output = ClaudeHookOutput.model_validate_json(result.stdout)
        return output.hook_specific_output.permission_decision

    helper = config_dir / "plugins/cache/official/tool/scripts/validate.mjs"
    assert decision(f"node {helper}") == "allow"
    assert decision(f"node {config_dir}/skills/tool/scripts/report.mjs") == "allow"
    assert decision("node /tmp/untrusted-script.mjs") == "deny"
    workspace_script = Path(".claude/plugins/lup/scripts/file_suggest.sh").resolve()
    assert decision(f"sh {workspace_script}") == "deny"


def test_generated_claude_hook_refuses_the_declared_calls() -> None:
    """The refusal this repository declares, as the shipped hook enforces it.

    Routing is half the mechanism and the declared rows are the other half,
    so this goes through the compiled script rather than the kernel beneath
    it: a row the hook is never handed refuses nothing, and no unit below
    this level would notice.
    """
    script = Path(".claude/plugins/lup/hooks/scripts/policy.py").resolve()

    def decision(name: str, payload: JsonObject) -> ClaudeHookDecision:
        body = {"tool_name": name, "tool_input": payload}
        result = sh.Command(str(script))(_in=json.dumps(body), _return_cmd=True)
        assert isinstance(result, sh.RunningCommand)
        return ClaudeHookOutput.model_validate_json(result.stdout).hook_specific_output

    refused = decision("Artifact", {"content": "a page"})
    assert refused.permission_decision == "deny"
    assert "lup-devtools report" in refused.permission_decision_reason

    narrowed = decision("Skill", {"skill": "artifact-design"})
    assert narrowed.permission_decision == "deny"

    escalated = decision(
        "Artifact", {"content": "# lup: escalate: the user asked for a page\npage"}
    )
    assert escalated.permission_decision == "ask"


def test_generated_claude_hook_leaves_every_other_skill_to_the_runtime() -> None:
    """Routing `Skill` must not put every skill invocation to a human."""
    script = Path(".claude/plugins/lup/hooks/scripts/policy.py").resolve()
    result = sh.Command(str(script))(
        _in='{"tool_name":"Skill","tool_input":{"skill":"lup:commit"}}',
        _return_cmd=True,
    )

    assert isinstance(result, sh.RunningCommand)
    assert json.loads(result.stdout) == {}


def test_generated_claude_hook_executes_the_canonical_kernel() -> None:
    script = Path(".claude/plugins/lup/hooks/scripts/policy.py").resolve()
    result = sh.Command(str(script))(
        _in='{"tool_name":"Bash","tool_input":{"command":"python -c 1"}}',
        _return_cmd=True,
    )
    assert isinstance(result, sh.RunningCommand)
    output = ClaudeHookOutput.model_validate_json(result.stdout)
    assert output.hook_specific_output.permission_decision == "deny"
    assert "interpreters" in output.hook_specific_output.permission_decision_reason


@pytest.mark.parametrize("target", sorted(SHIPPED_DISPATCHERS))
def test_generated_dispatcher_resolves_its_runtime_from_anywhere(
    target: str, tmp_path: Path
) -> None:
    """A plugin host spawns the hook as a bare script and arranges nothing.

    Isolated mode drops the working directory, the script's own directory, and
    every ``PYTHON*`` variable from the search path, and the environment is
    empty, so reaching ``kernel.*`` and ``policy_data`` at all proves the
    script finds its runtime from its own location.
    """
    result = sh.Command(sys.executable)(
        "-I",
        str(SHIPPED_DISPATCHERS[target].script.resolve()),
        _in='{"tool_name":"Bash","tool_input":{"command":"python -c 1"}}',
        _cwd=str(tmp_path),
        _env={},
        _ok_code=[0, 2],
        _return_cmd=True,
    )

    assert isinstance(result, sh.RunningCommand)
    match target:
        case "claude":
            decision = ClaudeHookOutput.model_validate_json(
                result.stdout
            ).hook_specific_output
            assert decision.permission_decision == "deny"
            reason = decision.permission_decision_reason
        case _:
            assert result.exit_code == 2
            reason = result.stderr.decode()
    assert "interpreters" in reason


def test_static_checking_reaches_every_shipped_dispatcher() -> None:
    """A dispatcher is the one artifact whose breakage is silent.

    A plugin host runs these scripts, not the workspace, so an unresolved
    import or a mistyped argument surfaces as a permission decision that never
    happens — in a session that only sees the tool go through. Every scope
    that could exempt one is asserted here rather than trusted.
    """
    declared = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    config = PyrightConfiguration.model_validate(declared["tool"]["pyright"])
    halves = [SHARED_DISPATCHER_HALF]

    for dispatcher in SHIPPED_DISPATCHERS.values():
        halves.append(dispatcher.asset)
        for source in (dispatcher.asset, dispatcher.script):
            assert dispatcher.runtime in [
                path
                for environment in config.execution_environments
                if source.is_relative_to(environment.root)
                for path in environment.extra_paths
            ]
        assert SHARED_DISPATCHER_HALF.parent in [
            path
            for environment in config.execution_environments
            if dispatcher.asset.is_relative_to(environment.root)
            for path in environment.extra_paths
        ]
    for source in [*halves, *[item.script for item in SHIPPED_DISPATCHERS.values()]]:
        assert any(source.is_relative_to(root) for root in config.include)
        assert not any(source.is_relative_to(root) for root in config.exclude)


def test_both_dispatchers_are_compiled_from_one_shared_host_half() -> None:
    """The half neither runtime spells differently is written exactly once.

    Every function the two scripts genuinely share must be a function the
    shared half offers — anything else is the same code living in two places,
    which is how the halves drifted apart before they were compiled.
    """
    shared = [
        node.name
        for member in SPLICED_MEMBERS
        for node in source_half(SHARED_PACKAGE, member).functions()
    ]
    claude = compiled_functions(compile_dispatcher(CLAUDE_DISPATCHER))
    codex = compiled_functions(compile_dispatcher(CODEX_DISPATCHER))

    assert "sandbox_active" in shared and "existing_write_targets" in shared
    assert "granted_allowances" in shared and "declared_identity" in shared
    # What a lease currently holds has one reader as well as one document:
    # two readings of the same file is the same drift as two files.
    assert "document_allowances" in shared
    # Every kernel call site is shared, which is what stops one runtime from
    # passing a fact the other has quietly stopped passing.
    assert "bash_decision" in shared and "edit_decision" in shared
    identical = [
        name
        for name in claude
        if name in codex
        and ast.dump(ast.parse(claude[name])) == ast.dump(ast.parse(codex[name]))
    ]
    assert sorted(identical) == sorted(shared)
    assert all(claude[name] == codex[name] for name in shared)


@pytest.mark.parametrize("target", sorted(SHIPPED_DISPATCHERS))
def test_compiled_dispatcher_reaches_only_what_a_bare_script_resolves(
    target: str,
) -> None:
    """The compiled script keeps the hermeticity floor its runtime promises.

    ``host`` is deliberately absent: the shared half is compiled in rather
    than shipped beside the script, so there is no second runtime module to
    keep in step with the one this dispatcher was type-checked against.
    """
    dispatcher = SHIPPED_DISPATCHERS[target]
    script = compile_dispatcher(dispatcher.declaration)
    modules = [
        item.module
        for item in SourceHalf(
            module=target, text=script, tree=ast.parse(script)
        ).imports()
    ]

    assert modules
    assert all(resolvable(module, dispatcher.declaration) for module in modules)
    assert SHARED_MEMBER not in modules
    assert not any(module == "lup" or module.startswith("lup.") for module in modules)
    assert script == dispatcher.script.read_text(encoding="utf-8")


@pytest.mark.parametrize("target", sorted(SHIPPED_DISPATCHERS))
def test_compiled_dispatcher_is_already_formatted(target: str) -> None:
    """What the compiler emits has to be formatted, not merely correct.

    The generated tree is checked by the same formatter as everything else, so
    a compiler that splices source into a shape the formatter would rewrite
    fails a gate nothing near the compiler runs. Asserting it here is what
    couples the two: emitting an unformatted script is a failing test rather
    than a red sweep somebody meets later, on a file they are told not to
    edit.
    """
    script = compile_dispatcher(SHIPPED_DISPATCHERS[target].declaration)
    formatted = str(
        sh.Command("ruff")("format", "-", "--stdin-filename", "policy.py", _in=script)
    )

    assert script == formatted


def test_compilation_refuses_a_dispatcher_that_breaks_its_declaration() -> None:
    """A dispatcher that cannot be proven is a session without a boundary.

    Every axis is read back out of the syntax rather than trusted, so a tool
    the plugin registers the hook for but the router never reaches stops
    generation instead of reaching a session as a decision that never happens.
    """
    unrouted = CLAUDE_DISPATCHER.model_copy(
        update={"routed_tools": [*CLAUDE_DISPATCHER.routed_tools, "NotebookEdit"]}
    )
    misread = CODEX_DISPATCHER.model_copy(update={"managed_root_env": "CODEX_ROOT"})
    unregistered = CODEX_DISPATCHER.model_copy(update={"hook_events": ["PreToolUse"]})

    with pytest.raises(ValueError, match="routes"):
        compile_dispatcher(unrouted)
    with pytest.raises(ValueError, match="never reads CODEX_ROOT"):
        compile_dispatcher(misread)
    with pytest.raises(ValueError, match="not registered for"):
        compile_dispatcher(unregistered)


AUTONOMY_PROBE = {
    "tool_name": "Write",
    "tool_input": {
        "file_path": "packages/lup/src/lup/generated_probe.py",
        "content": "".join(f"VALUE_{index} = {index}\n" for index in range(8)),
    },
}


def hook_environment(identity: str | None) -> EnvVars:
    """Build the hook's environment, never inheriting a declared identity.

    An operator with the identity exported would otherwise decide the
    outcome of every autonomy assertion below.
    """
    inherited = {
        key: value
        for key, value in os.environ.items()  # lup: ignore[os-environ] — test shell
        if key != "LUP_AGENT_IDENTITY"
    }
    return (
        inherited if identity is None else {**inherited, AGENT_IDENTITY_ENV: identity}
    )


def hook_decision(
    payload: JsonObject, agent_type: str | None = None, identity: str | None = None
) -> ClaudeHookDecision:
    """Run the installed Claude hook over one payload and identity pair."""
    script = Path(".claude/plugins/lup/hooks/scripts/policy.py").resolve()
    body = payload if agent_type is None else {**payload, "agent_type": agent_type}
    result = sh.Command(str(script))(
        _in=json.dumps(body),
        _env=hook_environment(identity),
        _return_cmd=True,
    )
    assert isinstance(result, sh.RunningCommand)
    output = ClaudeHookOutput.model_validate_json(result.stdout)
    return output.hook_specific_output


def autonomy_effect(agent_type: str | None = None, identity: str | None = None) -> str:
    return hook_decision(
        AUTONOMY_PROBE, agent_type=agent_type, identity=identity
    ).permission_decision


def test_generated_claude_hook_maps_agent_type_to_editor_autonomy() -> None:
    assert autonomy_effect() == "ask"
    assert autonomy_effect(agent_type="implementer") == "ask"
    assert autonomy_effect(agent_type="resolver-worker") == "allow"
    assert autonomy_effect(agent_type="lup:resolver-worker") == "allow"


def test_generated_claude_hook_maps_declared_identity_to_editor_autonomy() -> None:
    """The resolver's worker is a top-level session, so `agent_type` is empty.

    Autonomy has to reach it through the identity its launcher declared, or
    the mechanism is unreachable on the one path that needs it.
    """
    assert autonomy_effect(identity="") == "ask"
    assert autonomy_effect(identity="implementer") == "ask"
    assert autonomy_effect(identity="resolver-worker") == "allow"
    assert autonomy_effect(identity="lup:resolver-worker") == "allow"


def test_generated_claude_hook_asks_for_human_owned_readme_edits() -> None:
    """Autonomy is a release of named rules, never a blanket bypass.

    Both channels are checked: an identity that grants autonomy through the
    environment must not buy anything the payload channel would not.
    """
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(Path("README.md").resolve()),
            "content": "# Rewritten by an agent\n",
        },
    }
    assert hook_decision(payload).permission_decision == "ask"
    for granted in (
        hook_decision(payload, agent_type="resolver-worker"),
        hook_decision(payload, identity="resolver-worker"),
    ):
        assert granted.permission_decision == "ask"
        assert "human-authored" in granted.permission_decision_reason


def test_generated_codex_hook_fails_closed_for_unknown_tools() -> None:
    script = Path(".codex/plugins/lup/hooks/scripts/policy.py").resolve()
    result = sh.Command(str(script))(
        _in='{"tool_name":"custom_tool","tool_input":{}}',
        _ok_code=[0, 2],
        _return_cmd=True,
    )
    assert isinstance(result, sh.RunningCommand)
    assert result.exit_code == 2
    assert b"unknown tool" in result.stderr


def test_reconciliation_source_digest_rejects_a_stale_preimage(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("old\n", encoding="utf-8")
    patch = """diff --git a/source.py b/source.py
--- a/source.py
+++ b/source.py
@@ -1 +1 @@
-old
+new
"""

    before = source_patch_base_digest(tmp_path, patch)
    source.write_text("changed after proposal\n", encoding="utf-8")

    assert source_patch_base_digest(tmp_path, patch) != before


def test_reconciliation_proposal_persists_reviewable_patch_and_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("old\n", encoding="utf-8")
    patch = """diff --git a/source.py b/source.py
--- a/source.py
+++ b/source.py
@@ -1 +1 @@
-old
+new
"""

    record = ReconciliationProposalWriter().write(tmp_path, patch)
    directory = tmp_path / ".lup" / "reconcile" / record.proposal_id

    assert (directory / "source.patch").read_text(encoding="utf-8") == patch
    assert (directory / "metadata.json").read_text(encoding="utf-8").endswith("\n")


def test_reconciliation_source_digest_tracks_new_file_absence(tmp_path: Path) -> None:
    patch = """diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1 @@
+new
"""

    before = source_patch_base_digest(tmp_path, patch)
    (tmp_path / "new.py").write_text("occupied\n", encoding="utf-8")

    assert source_patch_base_digest(tmp_path, patch) != before


def test_source_patch_parser_does_not_treat_hunk_content_as_file_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("-- /dev/null\n", encoding="utf-8")
    patch = """diff --git a/source.py b/source.py
--- a/source.py
+++ b/source.py
@@ -1 +1 @@
--- /dev/null
+value
"""

    assert source_patch_base_digest(tmp_path, patch)


def test_reconciliation_source_digest_rejects_escaping_paths(tmp_path: Path) -> None:
    patch = """diff --git a/../secret b/../secret
--- a/../secret
+++ b/../secret
"""

    with pytest.raises(ValueError, match="escapes"):
        source_patch_base_digest(tmp_path, patch)


def test_unchanged_ownership_manifest_is_not_replaced(tmp_path: Path) -> None:
    path = tmp_path / ".native" / ".lup-ownership.json"
    manifest = build_manifest(
        portable_harness(),
        ArtifactTree(artifacts=[]),
        generator_version="test",
        target_requirements=[],
    )
    save_manifest(path, manifest)
    inode = path.stat().st_ino

    save_manifest(path, manifest)

    assert path.stat().st_ino == inode


def test_annotated_ownership_manifest_raises_a_typed_recovery_error(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".lup-ownership.json"
    assert load_manifest(path) is None
    manifest = build_manifest(
        portable_harness(),
        ArtifactTree(artifacts=[]),
        generator_version="test",
        target_requirements=[],
    )
    save_manifest(path, manifest)
    assert load_manifest(path) == manifest

    with path.open("a", encoding="utf-8") as handle:
        handle.write("# a trailing annotation\n")

    with pytest.raises(OwnershipManifestError, match="repair or remove"):
        load_manifest(path)


def test_the_generator_owns_the_proof_it_writes_and_never_lists(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".claude"
    home.mkdir()
    manifest = build_manifest(
        portable_harness(),
        ArtifactTree(artifacts=[]),
        generator_version="test",
        target_requirements=[],
    )
    save_manifest(home / ".lup-ownership.json", manifest)

    # A manifest lists what it proves and never itself, so every consumer
    # asking who owns the proof was told "the repository" about the one file
    # materialization always writes.
    assert not [item for item in manifest.files if "ownership" in str(item.path)]
    owned = generated_artifacts(tmp_path, homes=[".claude"])
    assert owned.owning(".claude/.lup-ownership.json") is not None
    assert owned.owning("packages/lup/src/lup/harness/ownership.py") is None


def test_proven_obsolete_deletion_is_proposed_and_executed(tmp_path: Path) -> None:
    obsolete = tmp_path / "obsolete.txt"
    obsolete.write_text("stale output\n", encoding="utf-8")
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("obsolete.txt"),
                content="stale output\n",
                category="generated",
                sha256=content_digest("stale output\n"),
            )
        ],
    )

    proposal = DeterministicReconciler().propose(current, ArtifactTree(artifacts=[]))
    result = AtomicMaterializer().apply(proposal)

    assert [delete.path for delete in proposal.deletes] == [Path("obsolete.txt")]
    assert result.removed == [Path("obsolete.txt")]
    assert not obsolete.exists()


def test_deletion_prunes_the_directories_it_empties(tmp_path: Path) -> None:
    gone = tmp_path / "skills" / "gone" / "SKILL.md"
    gone.parent.mkdir(parents=True)
    gone.write_text("stale skill\n", encoding="utf-8")
    kept = tmp_path / "skills" / "kept" / "SKILL.md"
    kept.parent.mkdir(parents=True)
    kept.write_text("live skill\n", encoding="utf-8")
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("skills/gone/SKILL.md"),
                content="stale skill\n",
                category="generated",
                sha256=content_digest("stale skill\n"),
            )
        ],
    )

    AtomicMaterializer().apply(
        DeterministicReconciler().propose(current, ArtifactTree(artifacts=[]))
    )

    assert not gone.parent.exists()
    assert kept.exists()
    assert tmp_path.exists()


def test_deletion_with_changed_ownership_proof_is_refused(tmp_path: Path) -> None:
    obsolete = tmp_path / "obsolete.txt"
    obsolete.write_text("stale output\n", encoding="utf-8")
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("obsolete.txt"),
                content="stale output\n",
                category="generated",
                sha256=content_digest("stale output\n"),
            )
        ],
    )
    proposal = DeterministicReconciler().propose(current, ArtifactTree(artifacts=[]))
    obsolete.write_text("user edited after the proposal\n", encoding="utf-8")

    with pytest.raises(MaterializationConflictError, match="ownership proof changed"):
        AtomicMaterializer().apply(proposal)
    assert obsolete.exists()


def test_materialization_rejects_stale_executable_mode(tmp_path: Path) -> None:
    path = tmp_path / "hook.py"
    path.write_text("pass\n", encoding="utf-8")
    path.chmod(0o644)
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("hook.py"),
                content="pass\n",
                category="generated",
                sha256=content_digest("pass\n"),
                executable=False,
            )
        ],
    )
    desired = ArtifactTree(
        artifacts=[
            Artifact(path=Path("hook.py"), content="updated\n", semantic_id="hook")
        ]
    )
    proposal = DeterministicReconciler().propose(current, desired)
    path.chmod(0o755)  # mode drift between proposal and apply

    with pytest.raises(MaterializationConflictError, match="stale executable mode"):
        AtomicMaterializer().apply(proposal)
    assert path.read_text(encoding="utf-8") == "pass\n"


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        (
            "diff --git a/x.py b/x.py\ndiff --git a/y.py b/y.py\n--- a/y.py\n",
            "missing its old-file header",
        ),
        ("diff --git a/x.py\n--- a/x.py\n", "malformed git source-patch header"),
        ("diff --git x/a.py y/a.py\n--- x/a.py\n", "repository-relative"),
        ("diff --git a/x.py b/x.py\n--- a/other.py\n", "does not match its diff"),
        ("diff --git a/x.py b/x.py\n", "missing its old-file header"),
        ("just prose\n", "no git file entries"),
    ],
    ids=[
        "header-without-old-file",
        "truncated-header",
        "foreign-prefixes",
        "mismatched-old-file",
        "trailing-header",
        "no-entries",
    ],
)
def test_malformed_source_patches_are_rejected(
    tmp_path: Path, patch: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        source_patch_base_digest(tmp_path, patch)


def test_source_digest_rejects_symlinked_preimage_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "file.py").write_text("secret\n", encoding="utf-8")
    (root / "link").symlink_to(outside, target_is_directory=True)
    patch = (
        "diff --git a/link/file.py b/link/file.py\n"
        "--- a/link/file.py\n"
        "+++ b/link/file.py\n"
    )

    with pytest.raises(ValueError, match="escapes the repository"):
        source_patch_base_digest(root, patch)


def test_declared_sensitive_paths_are_classified_without_content(
    tmp_path: Path,
) -> None:
    path = Path("secrets.env")
    (tmp_path / path).write_text("API_KEY=hunter2\n", encoding="utf-8")

    tree = FilesystemCurrentTreeReader(None, sensitive_local_only=[path]).read(tmp_path)

    assert [(item.category, item.content) for item in tree.artifacts] == [
        ("sensitive_local_only", "")
    ]
    assert tree.artifacts[0].sha256  # identity still proven for reconciliation


def test_missing_root_reads_as_an_empty_tree(tmp_path: Path) -> None:
    tree = FilesystemCurrentTreeReader(None).read(tmp_path / "ghost")

    assert tree.artifacts == []


def test_symlink_escaping_the_root_is_sensitive_and_unread(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("credential\n", encoding="utf-8")
    (root / "escape.txt").symlink_to(secret)

    tree = FilesystemCurrentTreeReader(None).read(root)

    assert [(item.category, item.content, item.sha256) for item in tree.artifacts] == [
        ("sensitive_local_only", "", "")
    ]


def test_binary_local_only_file_cannot_claim_local_preservation(
    tmp_path: Path,
) -> None:
    path = Path("local.bin")
    (tmp_path / path).write_bytes(b"\xff\xfe")

    tree = FilesystemCurrentTreeReader(None, local_only=[path]).read(tmp_path)

    assert tree.artifacts[0].category == "unknown_conflict"
    assert tree.artifacts[0].content == ""


def test_binary_owned_file_requires_explicit_reconciliation(tmp_path: Path) -> None:
    path = Path("owned.md")
    manifest = build_manifest(
        portable_harness(),
        ArtifactTree(
            artifacts=[Artifact(path=path, content="text\n", semantic_id="owned")]
        ),
        generator_version="test",
        target_requirements=[],
    )
    (tmp_path / path).write_bytes(b"\xff\xfe")

    tree = FilesystemCurrentTreeReader(manifest, managed_paths=[path]).read(tmp_path)

    assert tree.artifacts[0].category == "unknown_conflict"
    assert tree.artifacts[0].content == ""


def test_exact_content_adoption_still_corrects_executable_drift(
    tmp_path: Path,
) -> None:
    content = "#!/bin/sh\n"
    current = CurrentTree(
        root=tmp_path,
        artifacts=[
            CurrentArtifact(
                path=Path("hook.sh"),
                content=content,
                category="unknown_conflict",
                sha256=content_digest(content),
                executable=False,
            )
        ],
    )
    desired = ArtifactTree(
        artifacts=[
            Artifact(
                path=Path("hook.sh"),
                content=content,
                semantic_id="hook",
                executable=True,
            )
        ]
    )

    proposal = DeterministicReconciler().propose(current, desired)

    assert proposal.conflicts == []
    assert [
        (write.previous_sha256, write.previous_executable) for write in proposal.writes
    ] == [(content_digest(content), False)]
    assert proposal.writes[0].artifact.executable


def test_proposal_rewrite_is_idempotent_but_tampering_refuses(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("old\n", encoding="utf-8")
    patch = (
        "diff --git a/source.py b/source.py\n"
        "--- a/source.py\n"
        "+++ b/source.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new"  # no trailing newline: the writer must normalize it
    )

    writer = ReconciliationProposalWriter()
    record = writer.write(tmp_path, patch)
    assert writer.write(tmp_path, patch) == record  # identical re-write is a no-op

    patch_path = tmp_path / ".lup" / "reconcile" / record.proposal_id / "source.patch"
    assert patch_path.read_text(encoding="utf-8").endswith("\n")
    patch_path.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="collision"):
        writer.write(tmp_path, patch)


def test_project_settings_derive_sandbox_from_hook_declaration() -> None:
    plugin = portable_harness().plugins[0]
    hooks = plugin.hooks
    assert hooks is not None
    settings = project_settings(plugin)
    sandbox = settings["sandbox"]
    assert isinstance(sandbox, dict)
    filesystem = sandbox["filesystem"]
    network = sandbox["network"]
    assert isinstance(filesystem, dict) and isinstance(network, dict)
    assert filesystem["denyWrite"] == ["README.md"]
    domains = network["allowedDomains"]
    assert isinstance(domains, list)
    assert "code.claude.com" in domains
    assert "github.com" in domains
    assert "sandbox" not in project_settings(None)
    assert sandbox["excludedCommands"] == hooks.excluded_commands()


def test_a_declared_tool_server_is_granted_rather_than_asked_about() -> None:
    """A server the harness wires in is this project's own code.

    The grant names each server by the scoped name a runtime addresses a
    plugin's server by; the bare key it is declared under matches nothing.
    """
    plugin = portable_harness().plugins[0]
    permissions = project_settings(plugin)["permissions"]
    assert isinstance(permissions, dict)
    allowed = permissions["allow"]
    assert isinstance(allowed, list)
    for server in plugin.mcp_servers:
        assert f"mcp__plugin_{plugin.name}_{server.name}" in allowed
    assert "WebSearch" in allowed


def test_rendered_sandbox_keys_are_the_runtime_documented_ones() -> None:
    """The block is checked against the runtime's shape rather than assumed.

    A misspelled sandbox key changes nothing and reports nothing, so every
    key is drawn from the SDK's published shape. Two are settings-file-only,
    because the SDK routes filesystem and credential limits through
    permission rules instead — named here so the split reads as a fact about
    the two surfaces rather than as a mismatch nobody checked.
    """
    sandbox = project_settings(portable_harness().plugins[0])["sandbox"]
    assert isinstance(sandbox, dict)
    network = sandbox["network"]
    assert isinstance(network, dict)
    session_keys = SandboxSettings.__annotations__
    network_keys = SandboxNetworkConfig.__annotations__

    assert "excludedCommands" in session_keys
    assert sorted(key for key in sandbox if key not in session_keys) == [
        "credentials",
        "filesystem",
    ]
    assert [key for key in network if key not in network_keys] == []


def test_declared_exclusions_cover_the_commands_the_boundary_cannot_carry() -> None:
    """Each pattern is checked against the command as the toolchain spells it.

    A pattern matches on a literal prefix, so one that reads convincingly
    beside the declaration and misses the invocation as typed excludes
    nothing — and it fails as whatever the boundary blocks, naming neither
    the pattern nor the sandbox.
    """
    hooks = portable_harness().plugins[0].hooks
    assert hooks is not None
    excluded = hooks.excluded_commands()
    for command in (
        "uv run lup-devtools py eval 'lup.harness.models.HookSandbox'",
        "git push origin HEAD",
        "git fetch --all",
        "gh pr view 47",
        "ssh -T git@github.com",
    ):
        assert sandbox_excluded(command, excluded), command
    assert not sandbox_excluded("uv run pytest -q", excluded)
    assert not sandbox_excluded("uv run lup-devtools py info lup.hooks", excluded)


def test_claude_sandbox_widens_the_writable_set_to_sibling_worktrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Claude roots writes at the launch cwd, so a second checkout is outside.

    The declared paths ride along with the resolved tree: this key is
    documented as merging across scopes and as overriding per session, and a
    list carrying both is the same list under either reading.
    """
    monkeypatch.setattr(launch, "get_tree_dir", lambda: tmp_path)
    plugin = portable_harness().plugins[0]
    assert plugin.hooks is not None and plugin.hooks.sandbox is not None
    arguments = claude_sandbox_arguments(plugin)
    widened = json.loads(arguments[arguments.index("--settings") + 1])

    assert widened["sandbox"]["filesystem"]["allowWrite"] == [
        *plugin.hooks.sandbox.writable_paths,
        str(tmp_path),
    ]


def test_entering_and_leaving_a_worktree_is_granted_rather_than_asked_about() -> None:
    """The workflow mandates the worktree, so asking about it asks whether to follow it.

    Neither tool is a shell command, so no vocabulary sweep reaches them and
    `hooks classify` cannot answer for them: the grant lives in the settings
    artifact, which is why the pin does too.
    """
    permissions = project_settings(portable_harness().plugins[0])["permissions"]
    assert isinstance(permissions, dict)
    allowed = permissions["allow"]
    assert isinstance(allowed, list)
    assert "EnterWorktree" in allowed
    assert "ExitWorktree" in allowed


def test_codex_sandbox_arguments_establish_the_envelope() -> None:
    environment: EnvVars = {}
    arguments = codex_sandbox_arguments(
        portable_harness().plugins[0], environment, ["--model", "gpt-5.2"]
    )
    assert arguments[:2] == ["--sandbox", "workspace-write"]
    assert environment["LUP_SANDBOX_ACTIVE"] == "1"


def test_codex_sandbox_widens_the_root_to_sibling_worktrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Codex roots writes at the launch cwd; the prescribed worktree is outside."""
    monkeypatch.setattr(launch, "get_tree_dir", lambda: tmp_path)
    environment: EnvVars = {}
    arguments = codex_sandbox_arguments(portable_harness().plugins[0], environment, [])
    roots = arguments[arguments.index("-c") + 1]

    assert roots.startswith("sandbox_workspace_write.writable_roots=")
    assert str(tmp_path) in roots


def test_codex_sandbox_omits_the_root_outside_a_tree_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain clone has no tree/ to widen to, so the envelope stands alone."""

    def no_tree() -> Path:
        raise typer.Exit(1)

    monkeypatch.setattr(launch, "get_tree_dir", no_tree)
    environment: EnvVars = {}

    assert codex_sandbox_arguments(portable_harness().plugins[0], environment, []) == [
        "--sandbox",
        "workspace-write",
    ]


def test_codex_sandbox_arguments_defer_to_a_caller_envelope() -> None:
    environment: EnvVars = {}
    caller_forms = [
        ["--sandbox", "danger-full-access"],
        ["--sandbox=read-only"],
        ["-s", "read-only"],
        ["--yolo"],
        ["--full-auto"],
        ["--dangerously-bypass-approvals-and-sandbox"],
    ]
    for extra_args in caller_forms:
        assert (
            codex_sandbox_arguments(
                portable_harness().plugins[0], environment, extra_args
            )
            == []
        )
    assert "LUP_SANDBOX_ACTIVE" not in environment


def test_declared_tool_servers_are_the_registry_the_backends_assemble() -> None:
    """A group added to the toolsets registry reaches a native session too."""
    servers = portable_harness().plugins[0].mcp_servers
    assert [server.name for server in servers] == tool_group_names(realtime=False)


def test_each_runtime_spells_the_project_root_a_tool_server_starts_from() -> None:
    """Neither tree may leave the root to whatever directory a launch had."""
    server = portable_harness().plugins[0].mcp_servers[0]
    assert "${CLAUDE_PROJECT_DIR}" in server.command_line(ClaudeSpellings())
    assert "." in server.command_line(CodexSpellings())


def test_claude_tree_offers_the_tool_servers_as_a_plugin_configuration() -> None:
    """The scope that follows the plugin, so enabling it is what starts them."""
    tree = compile_claude(portable_harness())
    declaration = next(
        artifact
        for artifact in tree.artifacts
        if artifact.path == Path(".claude/plugins/lup/.mcp.json")
    )
    servers = json.loads(declaration.content)["mcpServers"]
    assert sorted(servers) == sorted(tool_group_names(realtime=False))
    assert servers["notes"]["command"] == "uv"
    assert "${CLAUDE_PROJECT_DIR}" in servers["notes"]["args"]


def test_codex_tree_offers_the_tool_servers_in_its_project_config() -> None:
    """Codex keeps a project's servers beside the rest of its project config."""
    tree = compile_codex(portable_harness())
    config = next(
        artifact
        for artifact in tree.artifacts
        if artifact.path == Path(".codex/config.toml")
    )
    parsed = tomllib.loads(config.content)
    assert parsed["features"]["hooks"] is True
    assert sorted(parsed["mcp_servers"]) == sorted(tool_group_names(realtime=False))
    assert parsed["mcp_servers"]["notes"]["command"] == "uv"


def test_a_named_session_is_what_makes_a_native_server_serve_real_tools() -> None:
    """No adapter relays a context to a natively launched server; it opens one."""
    assert collect_tools_by_server(None).keys() == {EXAMPLE_GROUP}
    context = harness_session_context(HARNESS_SESSION)
    assert context.session_id == HARNESS_SESSION
    assert NOTES_GROUP in collect_tools_by_server(context)
