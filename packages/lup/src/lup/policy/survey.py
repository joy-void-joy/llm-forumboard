"""Every command form a shell vocabulary declares, and how each is decided.

A vocabulary is a table of judgements about one project's toolchain, and its
shape changes more often than those judgements do. Reasoning about the rules is
what produces a reshaping that looks harmless and moves a verdict three levels
down, so the check offered here is mechanical instead: enumerate every form the
table declares, classify each through the same kernel a session consults, and
compare the answers against the ones the table gave before.

A downstream project wants exactly this whenever it edits its own composition,
which is why the walk is library mechanism rather than one repository's test.
"""

from pydantic import BaseModel

from lup.policy.kernel.decision import DecisionEffect, SandboxPlacement
from lup.policy.kernel.rows import RuleLevel, ShellRuleRow
from lup.policy.kernel.shell import classify_shell
from lup.policy.shell_rules import ShellCommandRule, erase_shell_rules

# lup: ignore[constant-declaration] — a probe word chosen so that no table can
# declare it; its whole job is to be absent, which a caller supplying their own
# could only break
UNJUDGED_SUBCOMMAND = "lup-survey-unjudged"
"""A subcommand word no table declares, so every table answers it as unlisted.

What a command does with a subcommand nobody judged is a verdict as much as
the listed ones are, and it is the one form no walk over the rows would reach.
"""


class ClassifiedForm(BaseModel, frozen=True):
    """One command line the table declares, and the verdict it produces."""

    command: str
    effect: DecisionEffect
    sandbox: SandboxPlacement
    reason: str


class SurveyedRule(BaseModel, frozen=True):
    """One erased row: what it decided, and which level decided each half."""

    path: str
    level: RuleLevel
    effect: DecisionEffect
    effect_source: RuleLevel
    sandbox: SandboxPlacement
    sandbox_source: RuleLevel

    def provenance(self) -> str:
        """One line saying what this row decided and where each value came from.

        This is what a reader wants at a verdict they did not expect: three
        levels up is far enough that re-deriving it by hand is a whole session's
        detour, and the row already knows.
        """

        def origin(source: RuleLevel) -> str:
            if source == self.level:
                return "declared here"
            return f"inherited from the {source}"

        return (
            f"{self.path}: {self.effect} ({origin(self.effect_source)}),"
            f" runs {self.sandbox} ({origin(self.sandbox_source)})"
        )


def rule_path(row: ShellRuleRow) -> str:
    """The match path one row answers for, as a reader would type it."""
    names = (row["command"], row["subcommand"], row["operation"])
    return " ".join(name for name in names if name)


def rule_level(row: ShellRuleRow) -> RuleLevel:
    """The level one row sits at, which is the deepest name it carries."""
    if row["operation"]:
        return "operation"
    if row["subcommand"]:
        return "subcommand"
    return "command"


def survey_shell_rules(rules: list[ShellCommandRule]) -> list[SurveyedRule]:
    """Every erased row with its resolved axes and the level each came from."""
    return [
        SurveyedRule(
            path=rule_path(row),
            level=rule_level(row),
            effect=row["effect"],
            effect_source=row["effect_source"],
            sandbox=row["sandbox"],
            sandbox_source=row["sandbox_source"],
        )
        for row in erase_shell_rules(rules)
    ]


def rule_forms(row: ShellRuleRow) -> list[str]:
    """Every command line one row answers: its bare form, and each flag it names.

    The flag lists are where a row's effect stops being the whole story —
    ``ask_flags`` downgrade an allow, ``allow_flags`` and ``read_verbs``
    de-escalate a non-allow one — so a form per named word is what separates a
    row that still decides as it did from one that only looks unchanged.
    """
    base = rule_path(row)
    guarded = [*row["ask_flags"], *row["allow_flags"], *row["read_verbs"]]
    return [base, *[f"{base} {word}" for word in guarded]]


def shell_forms(rules: list[ShellCommandRule]) -> list[str]:
    """Every command line the table declares, plus each command's unlisted form."""
    rows = erase_shell_rules(rules)
    gated = dict.fromkeys(row["command"] for row in rows if row["subcommand"])
    return [
        *[form for row in rows for form in rule_forms(row)],
        *[f"{name} {UNJUDGED_SUBCOMMAND}" for name in gated],
    ]


def classify_forms(rules: list[ShellCommandRule]) -> list[ClassifiedForm]:
    """Classify every form the table declares, through the kernel itself.

    ``classify_shell`` rather than ``decide_shell``: the escalation marker and
    the sandbox conversion are the *boundary's* answers to a verdict, and
    reading them here would compare two tables through a lens neither of them
    set.
    """
    rows = erase_shell_rules(rules)

    def classified(form: str) -> ClassifiedForm:
        verdict = classify_shell(form, rows)
        return ClassifiedForm(
            command=form,
            effect=verdict.effect,
            sandbox=verdict.sandbox,
            reason=verdict.reason,
        )

    return [classified(form) for form in shell_forms(rules)]
