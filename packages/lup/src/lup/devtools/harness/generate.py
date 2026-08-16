"""Ownership-safe generation engine beneath the harness CLI.

Each frozen ``GenerationRecipe`` compiles the canonical catalog through an
adapter, validates the rendered tree, reconciles it against recorded
ownership, materializes a conflict-free proposal, and saves the manifest.
Inspection exposes the same pipeline without writes. Console-facing command
bodies live in ``drift`` and ``reconcile``; ``composition`` maps target names
to concrete recipes.
"""

import json
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import BaseModel

from lup.adapters.harness import (
    claude_prompt_renderer,
    codex_prompt_renderer,
    compile_claude,
    compile_codex,
)
from lup.harness.banner import (
    REGENERATE_COMMAND,
    VERBATIM_COPY,
    GeneratedBanner,
)
from lup.harness.materialization import (
    AtomicMaterializer,
    discard_staged_write,
    refused_write,
)
from lup.harness.models import (
    Artifact,
    ArtifactTree,
    CapabilityReport,
    Document,
    Harness,
    PromptDocument,
)
from lup.types import JsonObject
from lup.harness.ownership import (
    OwnershipManifest,
    build_manifest,
    load_manifest,
    save_manifest,
)
from lup.harness.reconciliation import (
    DeterministicReconciler,
    FilesystemCurrentTreeReader,
    ReconciliationConflict,
    ReconciliationProposal,
)
from lup.harness.contracts import (
    CurrentTreeReader,
    PromptRenderer,
    Reconciler,
    SkillInvocationRenderer,
)
from lup.harness.validation import validated_tree


class ProjectContent(BaseModel, frozen=True):
    """What a project publishes on top of the tree its harness compiles.

    The harness says what the plugin *is*; this says what else the repository
    ships beside it — the documents rendered into ``docs/``, the downstream
    guidance each runtime publishes, the verbatim assets, and the native
    settings. All of it is one project's, which is why generation takes it
    rather than importing a catalog it would have to name.
    """

    harness: Harness

    documents: list[Document] = []
    """Repository documents, rendered once into the tree that publishes them."""

    assets: list[Path] = []
    """Files copied verbatim into the plugin tree rather than rendered.

    Named one by one rather than swept from a directory: what sits beside
    them is the package machinery of the module that holds them, and a sweep
    would ship that too.
    """

    settings: JsonObject = {}
    """Native settings for the runtime that reads a settings file."""


class GenerationRecipe(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """Injected data and capabilities needed by neutral generation orchestration."""

    label: str
    root: Path
    source: Harness
    desired: ArtifactTree
    manifest_path: Path
    prior: OwnershipManifest | None
    reader: CurrentTreeReader
    reconciler: Reconciler = DeterministicReconciler()
    target_requirements: list[str]


type RuntimeReadiness = Callable[[], Sequence[CapabilityReport]]
"""How a composition asks its runtime whether it is actually installed."""


class NativeHarnessComposition(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """Concrete capabilities supplied to one CLI composition root.

    The one shape every harness command works in: a generation recipe, a
    readiness probe set, and a renderer for skill invocations. Which classes
    fill those is the composition root's business, never the command's.
    """

    recipe: GenerationRecipe
    readiness: RuntimeReadiness
    invocation_renderer: SkillInvocationRenderer


class HarnessGenerationConflict(RuntimeError):
    """Generated output collides with local or unproven content."""

    def __init__(self, conflicts: list[ReconciliationConflict]) -> None:
        detail = ", ".join(conflict.path.as_posix() for conflict in conflicts)
        super().__init__(f"harness generation has unresolved conflicts: {detail}")
        self.conflicts = conflicts


class GenerationReport(BaseModel, frozen=True):
    target: str
    changed: list[Path]
    removed: list[Path]
    source_digest: str


class DriftReport(BaseModel, frozen=True):
    """Read-only desired-tree comparison for pre-commit and CI."""

    target: str
    ownership_present: bool
    manifest_current: bool
    """Whether the manifest on disk is the one this source would write.

    The artifacts and the proof that covers them go stale independently. A
    merge is where they part: the driver resolving a manifest keeps one
    side, which is a digest of a tree neither branch has now, while the
    artifacts themselves merged cleanly and match their source. Reading only
    the artifacts calls that settled, and the regeneration the conflict was
    supposed to force is then the step nothing asks for.
    """

    proposal: ReconciliationProposal

    @property
    def clean(self) -> bool:
        return (
            self.ownership_present
            and self.manifest_current
            and not self.proposal.writes
            and not self.proposal.deletes
            and not self.proposal.conflicts
        )


def rendered_document(
    *,
    path: Path,
    document: PromptDocument,
    prompts: PromptRenderer,
    semantic_id: str,
) -> Artifact:
    """Render one canonical document below the banner naming its module."""
    return Artifact.generated(
        path=path,
        body=prompts.render(document),
        semantic_id=semantic_id,
        banner=GeneratedBanner(
            source=document.declared_source(), command=REGENERATE_COMMAND
        ),
    )


def installer_guidance(
    *, path: Path, document: PromptDocument | None, prompts: PromptRenderer
) -> list[Artifact]:
    """Render the guidance an installer merges into a target, if there is any.

    Only a template has one: a project that is nobody's starting point has no
    downstream to hand guidance to, so it publishes no such file rather than
    advertising its own project guidance as something to install elsewhere.
    """
    if document is None:
        return []
    return [
        rendered_document(
            path=path,
            document=document,
            prompts=prompts,
            semantic_id="harness.template-guidance",
        )
    ]


def published_documents(
    prompts: PromptRenderer, documents: list[Document]
) -> list[Artifact]:
    """Render every document the roster declares.

    The roster is the whole of what ``docs/`` contains: a document not
    declared there is not published, and a file found there that this did not
    produce is deleted as unowned. Both trees render the same set, so the two
    cannot disagree about what the repository documents.
    """
    return [
        rendered_document(
            path=document.path,
            document=document.document,
            prompts=prompts,
            semantic_id=document.semantic_id,
        )
        for document in documents
    ]


def managed_paths(desired: ArtifactTree, prior: OwnershipManifest | None) -> list[Path]:
    """Combine desired and formerly owned paths for deletion detection."""
    paths = [artifact.path for artifact in desired.artifacts]
    if prior is not None:
        paths.extend(item.path for item in prior.files)
    return list(dict.fromkeys(paths))


def current_reader(
    prior: OwnershipManifest | None,
    desired: ArtifactTree,
    *,
    sensitive_local_only: list[Path],
) -> FilesystemCurrentTreeReader:
    """Build the ordinary ownership reader used by concrete native recipes."""
    return FilesystemCurrentTreeReader(
        prior,
        sensitive_local_only=sensitive_local_only,
        managed_paths=managed_paths(desired, prior),
    )


# Compiler, prompt renderers, ownership reader, and reconciler in one place.
# lup: ignore[model-free-function] — composition root building a recipe
def claude_generation_recipe(
    root: Path, content: ProjectContent, guidance: PromptDocument | None = None
) -> GenerationRecipe:
    """Compose the complete Claude tree from canonical typed declarations."""
    source = content.harness
    compiled = compile_claude(source)
    prompts = claude_prompt_renderer()
    plugin = Path(".claude/plugins") / source.plugins[0].name
    verbatim = [
        Artifact(
            path=plugin / "scripts" / asset.name,
            content=asset.read_text(encoding="utf-8"),
            semantic_id="harness.file-suggestion",
            executable=True,
            banner=VERBATIM_COPY,
        )
        for asset in content.assets
    ]
    support_artifacts = [
        *published_documents(prompts, content.documents),
        *installer_guidance(
            path=plugin / "TEMPLATE_CLAUDE.md", document=guidance, prompts=prompts
        ),
        *verbatim,
        Artifact(
            path=Path(".claude/settings.json"),
            content=json.dumps(content.settings, indent=2, sort_keys=True),
            semantic_id="harness.project-settings",
        ),
    ]
    desired = validated_tree([*compiled.artifacts, *support_artifacts])
    manifest_path = root / ".claude" / ".lup-ownership.json"
    prior = load_manifest(manifest_path)
    reader = current_reader(
        prior,
        desired,
        sensitive_local_only=[Path(".claude/settings.local.json")],
    )
    return GenerationRecipe(
        label="claude",
        root=root,
        source=source,
        desired=desired,
        manifest_path=manifest_path,
        prior=prior,
        reader=reader,
        reconciler=DeterministicReconciler(),
        target_requirements=["claude-code"],
    )


# lup: ignore[model-free-function] — composition root building a recipe
def codex_generation_recipe(
    root: Path, content: ProjectContent, guidance: PromptDocument | None = None
) -> GenerationRecipe:
    """Compose the Codex renderers, reader, and ownership location."""
    source = content.harness
    prompts = codex_prompt_renderer()
    support_artifacts = installer_guidance(
        path=Path(".codex/plugins") / source.plugins[0].name / "TEMPLATE_AGENTS.md",
        document=guidance,
        prompts=prompts,
    )
    compiled = compile_codex(source)
    desired = validated_tree([*compiled.artifacts, *support_artifacts])
    manifest_path = root / ".codex" / ".lup-ownership.json"
    prior = load_manifest(manifest_path)
    return GenerationRecipe(
        label="codex",
        root=root,
        source=source,
        desired=desired,
        manifest_path=manifest_path,
        prior=prior,
        reader=current_reader(
            prior,
            desired,
            sensitive_local_only=[Path(".codex/config.local.toml")],
        ),
        reconciler=DeterministicReconciler(),
        target_requirements=["codex-cli>=0.144"],
    )


# lup: ignore[model-free-function] — recipe is a transparent capability carrier
def manifest_of(recipe: GenerationRecipe) -> OwnershipManifest:
    """The proof *recipe* would write, built once for writing and for reading.

    Inspection compares against this and generation saves it, so the manifest
    a check calls current is the one a regeneration would produce — the two
    cannot disagree about what settled means.
    """
    return build_manifest(
        recipe.source,
        recipe.desired,
        generator_version=recipe.source.generator_version,
        target_requirements=recipe.target_requirements,
    )


# lup: ignore[model-free-function] — recipe is a transparent capability carrier
def inspect_generation(recipe: GenerationRecipe) -> DriftReport:
    """Compute ownership-aware drift without changing the working tree."""
    current = recipe.reader.read(recipe.root)
    return DriftReport(
        target=recipe.label,
        ownership_present=recipe.prior is not None,
        manifest_current=recipe.prior == manifest_of(recipe),
        proposal=recipe.reconciler.propose(current, recipe.desired),
    )


# lup: ignore[model-free-function] — driver: it materializes and saves proof
def generate(recipe: GenerationRecipe) -> GenerationReport:
    """Compile, reconcile, materialize, then update proof—never source prompts."""
    drift = inspect_generation(recipe)
    proposal = drift.proposal
    if proposal.conflicts:
        raise HarnessGenerationConflict(proposal.conflicts)
    try:
        result = AtomicMaterializer().apply(proposal)
    except OSError as error:
        discard_staged_write(error)
        raise refused_write(error) from error
    manifest = manifest_of(recipe)
    save_manifest(recipe.manifest_path, manifest)
    return GenerationReport(
        target=recipe.label,
        changed=result.changed,
        removed=result.removed,
        source_digest=manifest.source_digest,
    )
