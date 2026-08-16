"""The documents lup publishes about its own machinery, as a declared roster.

One entry per document, each naming the path it renders to, the semantic id
ownership records it under, and the typed module it renders from. A project
composes these with its own and hands the result to generation, so a page
that is not declared does not exist and a file under ``docs/`` produced from
nowhere is deleted as unowned.

Explicit imports rather than a dynamic scan: a misspelled or missing module
is a type-checking error rather than a page that quietly stopped publishing.
"""

from pathlib import Path

import lup.harness.models as models
from lup.devtools.harness.content.docs import (
    architecture,
    contributing,
    conventions,
    harness,
    library,
    native_capabilities,
    orchestration,
    patterns,
    permissions,
    platform_differentiation,
    quality_pipeline,
    resolver,
    self_improvement,
    supervisor,
)

LIBRARY_DOCS_ROOT = "packages/lup/src/lup/devtools/harness/content/docs"
"""Where a checkout of this repository holds the modules below.

A default rather than a constant: the path is only ever read as provenance in
a generated banner, and an adopter vendoring lup somewhere else wants the
banner to point at where the module actually is for them.
"""


def published(
    module: str, filename: str, document: models.PromptDocument, root: str
) -> models.Document:
    """Declare one document rendered from its like-named content module."""
    return models.Document(
        path=Path("docs") / filename,
        semantic_id=f"docs.{Path(filename).stem}",
        source=f"{root}/{module}.py",
        document=document,
    )


def library_documents(
    skills: list[models.Skill],
    agents: list[models.Agent],
    plugin: models.NativeName,
    claude_decodes: list[str],
    codex_decodes: list[str],
    root: str = LIBRARY_DOCS_ROOT,
) -> list[models.Document]:
    """Every page lup publishes, in the order the index teaches them.

    The two that audit the plugin's own roster take it, so their counts and
    their bullet lists describe the harness a project actually composed
    rather than the half of it this library happens to ship. The parity audit
    additionally takes what each runtime's hook decodes, which only a root
    composing the concrete runtimes may name — reading it here would put a
    native implementation behind every page this module publishes.
    """
    return [
        published("library", "library.md", library.DOCUMENT, root),
        published(
            "harness", "harness.md", harness.document(skills, agents, plugin), root
        ),
        published("architecture", "architecture.md", architecture.DOCUMENT, root),
        published("permissions", "permissions.md", permissions.DOCUMENT, root),
        published("resolver", "resolver.md", resolver.DOCUMENT, root),
        published("supervisor", "supervisor.md", supervisor.DOCUMENT, root),
        published(
            "platform_differentiation",
            "platform-differentiation.md",
            platform_differentiation.document(
                skills, agents, claude_decodes, codex_decodes
            ),
            root,
        ),
        published(
            "native_capabilities",
            "native-capabilities.md",
            native_capabilities.DOCUMENT,
            root,
        ),
        published(
            "self_improvement", "self-improvement.md", self_improvement.DOCUMENT, root
        ),
        published("contributing", "contributing.md", contributing.DOCUMENT, root),
        published("conventions", "conventions.md", conventions.DOCUMENT, root),
        published(
            "quality_pipeline", "quality-pipeline.md", quality_pipeline.DOCUMENT, root
        ),
        published("orchestration", "orchestration.md", orchestration.DOCUMENT, root),
        published("patterns", "patterns.md", patterns.DOCUMENT, root),
    ]
