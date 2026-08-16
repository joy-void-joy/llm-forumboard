"""What this project publishes through each native target, and what writes it.

The builders and the selector are the library's; named here is only what is
this project's own — the content its harness compiles beside, the per-runtime
guidance each tree carries, and the generated files that belong to no native
tree at all.
"""

from functools import partial
from pathlib import Path

from lup.devtools.dev.rules import write_rule_reference
from lup.devtools.dev.workflow import write_workflow
from lup.devtools.harness.composition import (
    NativeTargets,
    claude_composition,
    codex_composition,
    local_claude_profile_directory,
)
from lup.devtools.harness.drift import RepositoryWriter
from lup.devtools.harness.generate import (
    NativeHarnessComposition,
    ProjectContent,
)
from lup.runtime.profiles import ProfileDirectory
from lup.workspace.paths import project_root
from lup_template.devtools.harness.catalog import (
    WORKFLOW,
    declared_hook_set,
    portable_harness,
)
from lup_template.devtools.harness.content.docs.catalog import DOCUMENTS
from lup_template.devtools.harness.content.settings import project_settings
from lup_template.devtools.harness.content.template_claude import (
    DOCUMENT as TEMPLATE_CLAUDE,
)
from lup_template.devtools.harness.content.template_codex import (
    DOCUMENT as TEMPLATE_CODEX,
)

CONTENT_ROOT = Path(__file__).parent / "content"


def project_content(root: Path) -> ProjectContent:
    """Everything this repository publishes beside its compiled plugin tree."""
    harness = portable_harness(root=root)
    return ProjectContent(
        harness=harness,
        documents=DOCUMENTS,
        assets=[CONTENT_ROOT / "assets" / "file_suggest.sh"],
        settings=project_settings(harness.plugins[0]),
    )


def profile_directory() -> ProfileDirectory:
    """The Claude accounts this checkout keeps, under ``.lup/profiles``.

    Named once and reached by both the launcher and the setup wizard, so a
    name means the same account whichever tree the caller curates it through
    — which is the whole reason to keep the profiles here rather than let
    each entry point fall back to the operator's personal registry.
    """
    return local_claude_profile_directory(project_root())


def claude_target(root: Path) -> NativeHarnessComposition:
    """This project's content, compiled through the Claude adapter."""
    return claude_composition(root, project_content(root), TEMPLATE_CLAUDE)


def codex_target(root: Path) -> NativeHarnessComposition:
    """This project's content, compiled through the Codex adapter."""
    return codex_composition(root, project_content(root), TEMPLATE_CODEX)


TARGETS = NativeTargets(builders={"claude": claude_target, "codex": codex_target})
"""Every native runtime this project generates a tree for, by CLI selector."""


# lup: ignore[constant-declaration] — which files outside a runtime tree this
# project generates, decided here because nothing sits above it to be asked
REPOSITORY_WIDE: list[RepositoryWriter] = [
    partial(write_rule_reference, selection=declared_hook_set().rules),
    partial(write_workflow, WORKFLOW),
]
"""Every project-owned generated file outside a native runtime tree."""
