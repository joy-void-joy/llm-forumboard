"""Named native harness composition roots over canonical declarations."""

from enum import StrEnum

from lup.adapters.claude.harness import (
    ClaudeAgentRenderer,
    ClaudeGuidanceRenderer,
    ClaudeHookRenderer,
    ClaudeMcpRenderer,
    ClaudePluginManifestRenderer,
    ClaudeSkillRenderer,
    ClaudeSpellings,
)
from lup.adapters.codex.harness import (
    CodexAgentRenderer,
    CodexGuidanceRenderer,
    CodexHookRenderer,
    CodexPluginManifestRenderer,
    CodexSkillRenderer,
    CodexSpellings,
)
from lup.codescan.portable import prose_breaches
from lup.harness.contracts import NativeSpellings
from lup.harness.prompts import SpelledPromptRenderer
from lup.harness.models import (
    GUIDANCE_BYTE_BUDGET,
    document_byte_size,
    Artifact,
    ArtifactTree,
    Harness,
)
from lup.harness.validation import validated_tree


class AdapterName(StrEnum):
    """Which native runtime a caller means, as a closed set.

    The library ships exactly these adapter packages, so naming them is a
    fact about this library rather than a judgement an adopter could make
    differently — which is why it is a type and not a table of strings.
    Carrying it as one keeps a mistyped selector from resolving to whichever
    branch a chain of string comparisons happened to end in.
    """

    CLAUDE = "claude"
    CODEX = "codex"


def prompt_renderer(own: NativeSpellings) -> SpelledPromptRenderer:
    """Compose the one renderer around the vocabulary of one runtime.

    Prose that teaches every tree names the runtimes in this order, so the
    ordering is a single composition decision rather than a claim any one
    adapter makes about the others.
    """
    return SpelledPromptRenderer(own=own, every=[ClaudeSpellings(), CodexSpellings()])


def claude_prompt_renderer() -> SpelledPromptRenderer:
    """Render prompts in Claude's vocabulary."""
    return prompt_renderer(ClaudeSpellings())


def codex_prompt_renderer() -> SpelledPromptRenderer:
    """Render prompts in Codex's vocabulary."""
    return prompt_renderer(CodexSpellings())


def reject_oversized_guidance(
    tree: ArtifactTree, budget: int = GUIDANCE_BYTE_BUDGET
) -> None:
    """Hold the always-loaded document to its budget as a session sees it.

    The declaration-time lower bound in ``Harness`` cannot know what the parts
    render to, and the gap grows with every part that replaces literal prose.
    Bytes, not characters: that is the unit the runtime's own ceiling counts
    in, and the two differ wherever the document uses non-ASCII punctuation.
    """
    for artifact in tree.artifacts:
        used = document_byte_size(artifact.content)
        if artifact.semantic_id != "harness.guidance" or used <= budget:
            continue
        raise ValueError(
            f"rendered guidance {artifact.path.as_posix()} is {used} bytes, "
            f"over the {budget} budget by {used - budget}. Move a section to a "
            "generated document under docs/ and leave a file-path pointer, the "
            "way Self-Improvement Loop and Permission Hooks were split."
        )


def reject_native_prose(source: Harness) -> None:
    """Keep every native spelling inside the adapter that owns it.

    Composition is the only place that sees the assembled text, so a
    description built elsewhere and folded into a prompt is judged here too.
    """
    breaches = prose_breaches(source, [ClaudeSpellings(), CodexSpellings()])
    if breaches:
        named = ", ".join(
            f"{breach.declaration_id} names {breach.spelling!r}"
            for breach in breaches[:8]
        )
        raise ValueError(
            "prose every tree renders must name no platform; a typed part "
            f"spells these for each runtime instead: {named}"
        )


def compile_claude(source: Harness) -> ArtifactTree:
    """Compile canonical declarations directly to Claude-owned artifacts."""
    reject_native_prose(source)
    spellings = ClaudeSpellings()
    prompts = prompt_renderer(spellings)
    manifest_renderer = ClaudePluginManifestRenderer()
    mcp_renderer = ClaudeMcpRenderer(spellings)
    guidance_renderer = ClaudeGuidanceRenderer(prompts)
    artifacts: list[Artifact] = []  # lup: ignore[empty-collection]
    for plugin in source.plugins:
        skill_renderer = ClaudeSkillRenderer(prompts, plugin.name)
        agent_renderer = ClaudeAgentRenderer(prompts, plugin.name, spellings)
        for declaration in plugin.skills:
            artifacts.extend(skill_renderer.render(declaration).artifacts)
        for declaration in plugin.agents:
            artifacts.extend(agent_renderer.render(declaration).artifacts)
        artifacts.extend(manifest_renderer.render(plugin).artifacts)
        artifacts.extend(mcp_renderer.render(plugin).artifacts)
        if plugin.hooks is not None:
            artifacts.extend(
                ClaudeHookRenderer(
                    plugin.name, source.resolver.worker_identity, spellings
                )
                .render(plugin.hooks)
                .artifacts
            )
    guidance = guidance_renderer.render(source)
    reject_oversized_guidance(guidance)
    artifacts.extend(guidance.artifacts)
    return validated_tree(artifacts)


def compile_codex(source: Harness) -> ArtifactTree:
    """Compile canonical declarations directly to Codex-owned artifacts."""
    reject_native_prose(source)
    spellings = CodexSpellings()
    prompts = prompt_renderer(spellings)
    manifest_renderer = CodexPluginManifestRenderer()
    guidance_renderer = CodexGuidanceRenderer(prompts, spellings)
    artifacts: list[Artifact] = []  # lup: ignore[empty-collection]
    for plugin in source.plugins:
        skill_renderer = CodexSkillRenderer(prompts, plugin.name)
        agent_renderer = CodexAgentRenderer(prompts, spellings)
        for declaration in plugin.skills:
            artifacts.extend(skill_renderer.render(declaration).artifacts)
        for declaration in plugin.agents:
            artifacts.extend(agent_renderer.render(declaration).artifacts)
        artifacts.extend(manifest_renderer.render(plugin).artifacts)
        if plugin.hooks is not None:
            artifacts.extend(
                CodexHookRenderer(
                    plugin.name, source.resolver.worker_identity, spellings
                )
                .render(plugin.hooks)
                .artifacts
            )
    guidance = guidance_renderer.render(source)
    reject_oversized_guidance(guidance)
    artifacts.extend(guidance.artifacts)
    return validated_tree(artifacts)
