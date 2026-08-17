# lup: ignore[constant-declaration]
# Both constants here are blocks of this index's own prose, and the index is
# the document rather than a value some caller passes into one.
"""The documentation index: what this repository is, and where each part is.

The component table and the closing note are this repository's own words. The
page tables are built from the documents each half actually declares, so a
page that is renamed or added on either side of the library boundary carries
its own row rather than waiting for someone to notice a dead link.
"""

import lup.harness.models as models
from lup.devtools.harness.content.docs.index import (
    IndexEntry,
    IndexGroup,
    document_index,
    entry,
)

PREAMBLE = r"""# Lup documentation

Lup is three components in one repository. Start with the one you are actually
touching.

| Component | What it is | Guide |
| --- | --- | --- |
| `packages/lup` | The reusable, provider-neutral library. Session and turn engine, harness compiler, permission kernel, resolver, code scanner. Published standalone; never imports the application. | [library.md](library.md) |
| `src/lup_template` | The application template. The agent you customize, the `lup-devtools` CLI, and the environment that runs a session. Built on the library. | [template.md](template.md) |
| `.claude/`, `.codex/`, `.agents/`, `AGENTS.md` | The harness — the native plugin trees. Compiler output from typed Python, committed so a checkout is launchable with no build step. Carries the roster of every skill and agent the plugin ships. | [harness.md](harness.md) |

"""

EPILOGUE = r"""## Every page here is generated

Files under `docs/` are compiler output from typed Python — the pages about
the library from `packages/lup/src/lup/devtools/harness/content/docs/`, the
pages about this repository from
`src/lup_template/devtools/harness/content/docs/` — the same way the native
trees are. Each opens with a banner naming its source module. Edit the module
and regenerate; a hand-edit is preserved and reported as a conflict rather
than silently overwritten. [harness.md](harness.md) is the whole story.
"""

RULES_REFERENCE = IndexEntry(
    link="rules.md",
    answers=(
        "Every executable Lup rule, its matching shape, its diagnostic, and "
        "the module that enforces it."
    ),
)
"""The one page with no declaring module: it renders from the rule registry."""


def document(
    reference: list[models.Document], project: list[models.Document]
) -> models.PromptDocument:
    """Compose the index over the pages both halves declare.

    A page is looked up by the identity ownership already records it under, so
    a document that stopped being published fails generation here rather than
    leaving a link that resolves to nothing.
    """
    page = {item.semantic_id: item for item in [*reference, *project]}
    return document_index(
        preamble=[models.TextPart(text=PREAMBLE)],
        groups=[
            IndexGroup(
                title="Reference",
                blurb=(
                    "Subjects that span the three components, or that are "
                    "large enough to own a page."
                ),
                entries=[
                    entry(
                        page["docs.architecture"],
                        "Why the seams are where they are: one capability per "
                        "ABC, adapters at the edge, structured output with one "
                        "mechanism.",
                    ),
                    entry(
                        page["docs.patterns"],
                        "The recurring code shapes: declaration-plus-renderer, "
                        "closed-by-construction, the typed-matcher router, and "
                        "the engine-versus-surface split.",
                    ),
                    entry(
                        page["docs.orchestration"],
                        "The delegation catalog: subagent, nested, background, "
                        "and deferred tools, and when to reach for each.",
                    ),
                    entry(
                        page["docs.permissions"],
                        "How a shell command, fetch, or edit becomes allow, "
                        "ask, defer, or deny — and how the generated hooks "
                        "decide identically without importing the library.",
                    ),
                    RULES_REFERENCE,
                    entry(
                        page["docs.resolver"],
                        "How reviewed feedback becomes concerns, worktrees, "
                        "workers, and an accepted integration branch.",
                    ),
                    entry(
                        page["docs.supervisor"],
                        "The local page that watches a resolver run and "
                        "answers its questions.",
                    ),
                    entry(
                        page["docs.platform-differentiation"],
                        "Every intended Claude/Codex difference, and the "
                        "parity decision for each generated artifact family.",
                    ),
                    entry(
                        page["docs.native-capabilities"],
                        "The evidence ledger: which native contracts are "
                        "proven, at which versions, and the release gaps.",
                    ),
                    entry(
                        page["docs.self-improvement"],
                        "How to turn an observed agent failure into a durable "
                        "capability change.",
                    ),
                ],
            ),
            IndexGroup(
                title="Working in this repository",
                entries=[
                    entry(
                        page["docs.contributing"],
                        "How to get set up, where a change of each kind "
                        "belongs, and what has to be green before it lands.",
                    ),
                    entry(
                        page["docs.conventions"],
                        "The lookup behind each code-convention rule: which "
                        "library, which typed stand-in for a dict, which "
                        "parser, which resolver tool.",
                    ),
                    entry(
                        page["docs.quality-pipeline"],
                        "The three check layers, and what each one uniquely catches.",
                    ),
                    entry(
                        page["docs.dev-tooling-decisions"],
                        "The architectural decisions behind the development "
                        "tooling, each stated against the current system.",
                    ),
                ],
            ),
        ],
        epilogue=[models.TextPart(text=EPILOGUE)],
    )
