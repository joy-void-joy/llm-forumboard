"""Generate the checked-in Lup rule reference from the canonical rule registry."""

from pathlib import Path

import lup.harness.models as models
from lup.adapters.harness import claude_prompt_renderer
from lup.codescan.common import RuleStrength
from lup.codescan.common import RuleSelection
from lup.codescan.registry import RULE_REFERENCE, RegisteredRule, all_rules
from lup.harness.banner import GeneratedBanner
from lup.harness.materialization import write_generated_file
from lup.harness.models import Artifact
from lup.markdown import CodeCell, HtmlCodeCell, PlainCell, TableCell
from lup.workspace.paths import project_root

# lup: ignore[constant-declaration] — the rule reference's own prose, which is
# the document this module exists to render rather than an input it takes
INTRODUCTION = (
    "# Lup rule reference\n\n"
    "Every executable Lup rule family — anti-pattern, boundary, spelling, and "
    "architecture rules — is indexed here from `lup.codescan.registry`, with the "
    "module that defines and enforces each rule. An edit-hook denial cites its "
    "rule id and this reference. Lup rules enforce repository-specific "
    "architecture and editing conventions; Ruff remains the source of standard "
    "Python diagnostics. The matching examples for anti-patterns are their "
    "canonical regular-expression shapes, so this reference cannot drift from "
    "the edit hook or repository auditor.\n\n"
    "## Typed suppressions\n\n"
    "Suppress one deliberate site with `# lup: ignore[rule-id]` and a reason. "
    "Comma-separated ids cover a line that intentionally matches several rules. "
    "A directive sits on the line it guards, or stands alone directly above "
    "it when the reason is too long to fit inline; nowhere else reaches. A "
    "typed directive in the file's opening comment block applies file-wide. "
    "Bare "
    "`# lup: ignore` remains parseable but is reported as untyped; a stale typed "
    "directive is blocking. `# noqa`, `# type: ignore`, and `# pyright: ignore` "
    "are separate forbidden shapes.\n\n"
    "The **Suppression** column says whether a directive reaches a rule at all. "
    "Most are soft: they name a shape that is usually wrong and occasionally the "
    "only thing that works, so a typed directive is a reasoned exception and the "
    "audit grades it. A rule marked **refused** is strong — its replacement is "
    "right every time, which leaves a directive nothing to express but a decision "
    "to keep the defect. Those rules ignore every directive, report the violation "
    "anyway, and report the directive itself as spurious; the only way past one is "
    "to write the replacement its diagnostic names.\n\n"
    "```python\n"
    "cache: dict[str, int] = {}  # lup: ignore[empty-collection] — mutable fold\n"
    "```\n\n"
    "## Structural rules\n\n"
)

# lup: ignore[constant-declaration] — a heading of that same document
ANTI_PATTERN_HEADING = "\n## Edit anti-patterns\n\n"

# lup: ignore[constant-declaration] — the section of that document explaining
# why the audit decides more narrowly than the hook
REFINEMENT_INTRODUCTION = (
    "\n## Audit-side refinements\n\n"
    "The edit hook sees a fragment of a proposed edit: no parse tree, no "
    "types, and a hermetic kernel that may not reach a type checker. So it "
    "decides on the spelling alone, and every rule above means exactly its "
    "matching example there. The whole-file audit reads finished source and "
    "resolves what a matched name refers to through the type oracle in "
    "`lup.codescan.oracle`, so the rules below decide more narrowly in "
    "`lup-devtools dev check` than they do at edit time — a hook denial you "
    "believe is wrong is answered by the audit, which reports the "
    "declaration that settled it. Where the oracle is unavailable the audit "
    "falls back to the hook's broad verdict, and a `# lup: ignore` left "
    "guarding a refuted line is reported as a dead directive.\n\n"
)


def suppression_cell(strength: RuleStrength) -> TableCell:
    """What a strength offers a reader who has just been denied by a rule."""
    match strength:
        case "soft":
            return PlainCell(text="typed directive")
        case "strong":
            return PlainCell(text="**refused**")


def rule_table(rules: list[RegisteredRule]) -> models.MarkdownTable:
    """One card per rule, as a table part the document composes like any other."""
    return models.MarkdownTable(
        headers=[
            "Rule id",
            "Family",
            "Scope",
            "Matching example",
            "Diagnostic",
            "Suppression",
            "Defined in",
        ],
        rows=[
            [
                CodeCell(text=rule.id),
                PlainCell(text=rule.family),
                PlainCell(text=rule.scope),
                # A matching shape is a regular expression that may quote a
                # backtick, which no fence survives.
                HtmlCodeCell(text=rule.example),
                PlainCell(text=rule.message),
                suppression_cell(rule.strength),
                CodeCell(text=rule.defined_in),
            ]
            for rule in rules
        ],
    )


def rule_reference_document(
    selection: RuleSelection | None = None,
) -> models.PromptDocument:
    """Every Lup rule this repository enforces, and how each is suppressed.

    Generated from the project's own selection, so a rule it retired is
    absent here rather than documented as enforced by a page whose whole
    job is to be the thing a denial can be looked up in.
    """
    rules = all_rules(selection=selection)
    structural = sorted(
        (rule for rule in rules if rule.family != "anti-pattern"),
        key=lambda item: item.id,
    )
    anti_patterns = sorted(
        (rule for rule in rules if rule.family == "anti-pattern"),
        key=lambda item: (item.scope, item.id),
    )
    refined = [rule for rule in rules if rule.refinement]
    refinements = "\n\n".join(
        f"### `{rule.id}`\n\n{rule.refinement}" for rule in refined
    )

    return models.PromptDocument(
        source=__name__,
        parts=[
            models.TextPart(text=INTRODUCTION),
            rule_table(structural),
            models.TextPart(text=ANTI_PATTERN_HEADING),
            rule_table(anti_patterns),
            models.TextPart(text=f"{REFINEMENT_INTRODUCTION}{refinements}\n"),
        ],
    )


RULE_REFERENCE_PATH = Path(RULE_REFERENCE)
"""Where this writes the reference, taken from the path deny messages cite so
the two cannot name different files."""

# lup: ignore[constant-declaration] — the command a reader types, whose words
# are the CLI's own rather than a preference this module holds
RULE_REFERENCE_COMMAND = "uv run lup-devtools dev rules"


def rule_reference_artifact(selection: RuleSelection | None = None) -> Artifact:
    """The rule reference as one artifact, gated like any other generated file.

    Rendered through one runtime's vocabulary because a document has to be
    rendered through some runtime's, and this one names none: it is prose and
    tables end to end, so either vocabulary produces the same bytes.
    """
    document = rule_reference_document(selection)
    return Artifact.generated(
        path=RULE_REFERENCE_PATH,
        body=claude_prompt_renderer().render(document),
        semantic_id="docs.rules",
        banner=GeneratedBanner(
            source=document.declared_source(), command=RULE_REFERENCE_COMMAND
        ),
    )


def write_rule_reference(
    root: Path | None = None,
    *,
    check: bool = False,
    selection: RuleSelection | None = None,
) -> Path:
    """Write or verify the generated rule reference."""
    return write_generated_file(
        rule_reference_artifact(selection),
        root or project_root(),
        RULE_REFERENCE_COMMAND,
        check=check,
    )
