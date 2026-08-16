# lup: ignore[library-default, constant-declaration]
# Every constant here is a block of prose offered for composition, not a
# table of judgements imposed on a reader: a project that wants different
# words composes different blocks, or writes its own beside these. The
# override is which blocks a document assembles, which is not a spelling the
# mechanical half of the rule can see.
"""Convention text that is portable, held once and rendered by every flavor.

A project's guidance, the downstream template it publishes, and the reference
pages beside them used to restate the same conventions in near-identical
prose, and a skill instructed an editor to "mirror relevant changes" between
them by hand. That is the shape ``docs/self-improvement.md`` rejects — a
prompt rule coexisting peacefully with the failure it warns about — and the
drift it produced was visible: em dashes in one copy and double hyphens in
the other, a bullet present in one and missing from the other, for text that
was supposed to be the same text.

What lives here is what every reader needs identically, which is why it is
the library's rather than any one project's. Anything true only of a
particular repository — its layout, its tooling paths — stays in that
project's guidance as an addition after the shared part, never as a
restatement of it: an addition cannot drift from what it adds to.

Blocks earn their place by what a gate cannot say in time. A rule the
checker enforces is described once in its own generated index, where it
carries its matching shape and its diagnostic; restating it here would be a
second copy that can drift from the first and is worse while it agrees.
What is held here instead is the shape of the gates themselves, and the
judgements no gate fires on.
"""

from pydantic import BaseModel

import lup.harness.models as models
from lup.codescan.common import RuleSelection

PLAN_AT_AGENT_SPEED: list[models.PromptPart] = [
    models.TextPart(
        text=r"""## Plan at Agent Speed

Every instinct you have about how long software takes was learned from human teams, whose implementation time is scarce. Yours is not: what you would estimate as months completes in an afternoon. Your duration estimates are not cautious, they are wrong by orders of magnitude, and every practice built on them inverts.

**Never scope, defer, or reject work from a predicted duration.** Scope by content — what changes, what it touches, how it is verified. A calendar figure in a plan is noise from someone else's constraints: delete it and re-derive the plan. Prototype-first exists to protect scarce human effort, and for you the real implementation costs what the throwaway was supposed to cost, so build it and let review cut scope afterward rather than pre-shrinking the attempt.

Catch the reflex in the act. "Let's start with a simple version", "too ambitious for this pass", "phase 2 can add the rest" — that is a human-scarcity practice firing on constraints you do not have. When you notice it, ask what is actually expensive here besides the imagined schedule.

"""
    ),
]

AGENT_VOCABULARY: list[models.PromptPart] = [
    models.TextPart(
        text=r"""## Agent Vocabulary

Two kinds of delegated agents look alike and must not be conflated:

- A **native subagent** ("subagent" for short) is dispatched by the harness: its delegation tool hands a focused task to a named role defined upfront, inside the main agent's session — shared trace, shared metrics.
- A **nested agent** (also called a *tool-subagent*) runs inside a tool call: the handler opens one independent session via `query()` and folds the result into the tool's response. The harness never sees it — to the calling agent it is just a tool.

Guidance that says "subagent" unqualified means the native kind. `docs/orchestration.md` carries the full delegation catalog — subagent, nested, background, deferred tool schemas — and when to reach for each. `docs/patterns.md` carries the recurring *code* shapes: declaration-plus-renderer, closed-by-construction, the typed-matcher router, and the engine-versus-surface split.

"""
    ),
]

THE_GATES: list[models.PromptPart] = [
    models.TextPart(
        text=r"""## The Gates You Will Meet

You are not expected to hold this repository's conventions in memory. Gates enforce them, each names what it caught and how to answer, and their diagnostics are written to be read cold. What is worth knowing up front is that they exist, and what a refusal from each one looks like.

**The rule checker.** Executable rules in anti-pattern, boundary, spelling, and architecture families run on every edit and in `dev check`. A denial cites its rule id, and `docs/rules.md` indexes every rule with the shape it matches, its diagnostic, and the module that enforces it — generated from the same registry that runs, so it cannot drift from what stopped you.

Suppress one deliberate site with `# lup: ignore[rule-id]` and a reason, comma-separating ids where a line trips several. The directive sits on the line it guards, or alone directly above it when the reason will not fit inline; nowhere else reaches, and one placed in a file's opening comment block applies file-wide. A bare `# lup: ignore` still parses but is reported untyped. A stale directive blocks. A rule marked **refused** takes no directive at all — its replacement is right every time, so a directive there could only express a decision to keep the defect, and the way past is to write what the diagnostic names. `# noqa`, `# type: ignore`, and `# pyright: ignore` are forbidden shapes rather than suppressions.

**The permission policy.** Every shell command, URL scope, and edit in a batch is classified. Segments join deny > ask > defer > allow, and malformed input fails conservatively. A denial names what tripped and the recovery, so you rarely need to read the lattice first. `# lup: escalate: <why>` as the leading line of a shell command promotes a classified deny or ask into an approval question carrying that reason.

**The edit budget.** A change block of at most three "real" changed lines is auto-allowed, so split large changes — imports in one edit, logic in another. A file declared human-owned surfaces every change as an approval instead: propose the exact edit as a question and let the user apply it, rather than writing it yourself.

**The drift check.** Generated trees are regenerated, never hand-edited and never hand-merged. Take either side of a conflict, regenerate, and let the check confirm it settled.

`docs/permissions.md` carries the full lattice, what counts as a real changed line, how the escalation marker scopes, and the recovery when work is denied as unjudged; `docs/contributing.md` carries the suppression marker's scoping.

"""
    ),
]
"""The four gates, taught as mechanisms rather than as their contents.

An agent that knows a checker exists, that a denial names a rule id, and how
a suppression is spelled can meet a rule it has never read. An agent handed
twenty rules and no mechanism is stopped by the twenty-first. The contents
live in the generated index, which carries every rule rather than whichever
subset a prose list happened to name, and cannot fall behind the registry.
"""


class ShapingRule(BaseModel, frozen=True):
    """One rule worth knowing before the fact, and the shape it steers to."""

    id: str
    steers_to: str

    def named(self) -> str:
        """This rule as the guidance spells it, id first and gloss beside it."""
        return f"`{self.id}` ({self.steers_to})"


SHAPING_RULES: list[ShapingRule] = [
    ShapingRule(
        id="own-model-dispatch",
        steers_to=(
            "a union answers through its members, never through `isinstance` "
            "over our own types"
        ),
    ),
    ShapingRule(
        id="abc-capability",
        steers_to="a capability ABC is an engine, never a surface a consumer holds",
    ),
    ShapingRule(
        id="model-free-function", steers_to="a model carries its own operations"
    ),
    ShapingRule(
        id="constant-declaration",
        steers_to="a judgement reaches its caller as an overridable default",
    ),
]
"""The rules worth naming before the fact, each with the shape it steers to.

Held as a declaration rather than inside the sentence because the sentence
has to name only the rules a project still enforces: one that retired a rule
and went on being told to know it by name would be reading advice about a
gate that cannot stop it, which is the drift this module exists to prevent.
"""


def shaping_sentence(selection: RuleSelection) -> str:
    """The paragraph naming the live shaping rules, or nothing where none are."""
    kept = [rule.named() for rule in SHAPING_RULES if selection.keeps(rule.id)]
    if not kept:
        return ""
    listed = kept[0] if len(kept) == 1 else f"{', '.join(kept[:-1])}, and {kept[-1]}"
    return (
        "\nSome rules shape a design before any gate could catch it. Know these by "
        "name and read them in `docs/rules.md` while choosing a shape rather than "
        f"after being stopped: {listed}.\n"
    )


def design_principles(
    selection: RuleSelection | None = None,
) -> list[models.PromptPart]:
    """What no rule fires on, plus the rule ids this project still enforces.

    Everything mechanical was removed on the test that a denial would have
    named it in time. What survived either has no executable rule at all, or
    has one that arrives too late to change the shape being chosen — for
    those, the id is a lookup key rather than the rule restated, and only
    while the project still holds itself to it.
    """
    return [
        models.TextPart(
            text=r"""### Design Principles

A gate catches a violation once it is written. These change what gets written, so they are here rather than in the index.

- **Compiling is stronger than emitting** — build an artifact from a typed declaration and it cannot diverge; transport checked source and a checker can only warn once it already has. When tempted to add a check that two things still match, ask whether one can be derived from the other instead (`docs/patterns.md`).
- **Structured data, not strings** — reaching for `re`, `.replace()`, `.split()`, or slicing to process structured data means a parser was missed, and `docs/conventions.md` names one per format. Never hand-parse an agent's output either: take a structured output through a Pydantic model.
- **Placement decides the package** — would another project built on this library want it? Then it belongs to the library. Only this application? Then it stays in the application. The same test applies to values, not only to code, which is why a judgement reaches a caller as an overridable default rather than as a constant they would have to fork.
- **The code is the source of truth** — it should read as though it had always been written this way. Never reference what code used to do, and never write "now", "new", "updated", "fixed", or "changed" in a comment. Change history belongs in commit messages.
- Reach for `for` and comprehensions over `while`, and for `match`/`case` over an `if`/`elif` chain dispatching on a value or a range.
"""
        ),
        models.TextPart(text=shaping_sentence(selection or RuleSelection())),
        models.TextPart(text="\n"),
    ]


SANCTIONED_EXCEPTIONS: list[models.PromptPart] = [
    models.TextPart(
        text=r"""### Exceptions No Rule Can See

A rule states the shape it refuses. These carve-outs are ours, and its diagnostic does not carry them:

- **Barrel files.** `__all__` and `__init__.py` re-exports are refused, and import goes directly to the module that defines the symbol. The exception is a standalone package's own top-level `__init__.py`, which may declare a public API that way — the package root only, never a subpackage.
- **Private prefixes.** Nothing is private, so a `_` prefix is refused on functions, methods, classes, and constants. An unused parameter (`_context`, `_exc_type`) is exempt: that is a linting convention, not a privacy one. What to do instead depends on why you wanted the prefix:
  - A helper that genuinely should not pollute the module namespace **nests inside its only caller**, which hides it without claiming privacy.
  - A wrapper whose only purpose is to call one other function, with no logic of its own, is not a helper worth hiding at all — inline it and let the caller reach the target directly.

"""
    ),
]
"""The carve-outs a rule id cannot deliver.

Dropping the enumerated conventions is safe exactly where the checker says
the same thing at the moment it matters. These three are what the checker
does *not* say: its diagnostic names the refused shape and stops, so an
agent obeying it literally would remove a package's public API or refuse a
linting convention. They stay because nothing else carries them.
"""

FAILURE_ANALYSIS: list[models.PromptPart] = [
    models.TextPart(
        text=r"""**When analyzing failures:** Ask "what general principle would have prevented this?" not "what specific rule would catch this case?" The fix is almost never a prompt line about a specific decision. Instead: does the agent have enough context? The right tools? A strong enough model?

When the principle points to a workflow failure, fix the workflow at the exact juncture where the failure enters — don't add a warning about it. A step named "Classify each commit" invites whole-commit thinking regardless of how many times the text says "decompose." Renaming the step to "Extract portable pieces" and separating reading from judging makes the failure structurally impossible. Warnings coexist peacefully with the workflows they warn against; structural changes don't.

"""
    ),
]
"""The two paragraphs every self-improvement reader needs identically.

Authored in three places before this — the repository guidance, the
downstream template, and the reference page — and already visibly drifted:
one copy still asked "Is the model strong enough?" in the older, longer
phrasing. Nothing but holding them once keeps three copies in step.
"""

MERGE_CONFLICT_RESOLUTION: list[models.PromptPart] = [
    models.TextPart(
        text=r"""### Merge Conflict Resolution

**Never silently drop code during conflict resolution.** The bias is toward inclusion — keeping both sides is always safer than losing features. A rename on one side must not swallow an addition on the other.

Before completing any merge, **audit for deletions**: compare the result against both parents and verify that every removed function, parameter, or command was intentionally removed, not lost as a side effect of choosing one conflict side.

Use `"""
    ),
    models.SkillInvocation(plugin="lup", skill="merge"),
    models.TextPart(
        text=r"""` (with no argument) for guided conflict resolution. See the command for the full decision tree.

"""
    ),
]

COMMIT_GUIDELINES: list[models.PromptPart] = [
    models.TextPart(
        text=r"""### Commit Guidelines

- **Commit before responding** — Don't accumulate changes across responses
- **Commit early, commit often** — Frequent commits provide checkpoints
- **Keep commits atomic** — If you need "and" in your message, it should be two commits
- **History will be rebased** — Don't worry about perfect messages during development
- **Meaningful final commits** — After rebasing, each commit should tell what changed and why

**Format:** `type(scope): description`

"""
    ),
]
