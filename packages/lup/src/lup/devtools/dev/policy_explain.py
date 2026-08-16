"""Ask the live permission policy what it would decide, and why.

Tuning a vocabulary against a session's actual friction needs the verdict for
a spelling, not a reading of the table that produces it: the shell classifier
recurses through substitutions, loops, and redirections, resolves paths
against the filesystem, and consults four declared row sets, so which of them
answered is rarely obvious from the command alone.

This runs the same composition a session runs — ``semantic_policy_for`` over
the project's own ``HookSet`` — so a verdict here is the verdict there rather
than an approximation of it. The generated dispatchers reach the identical
kernel through their own compiled halves, which is why one answer covers both
the in-process session and the native plugin.
"""

import json
from pathlib import Path

import typer
from pydantic import AnyHttpUrl, BaseModel

from lup.devtools.utils import output_json
from lup.harness.enforcement import semantic_policy_for
from lup.harness.models import HookSet
from lup.policy.models import EditBatch, EditChange, FetchUrl, ShellCommand
from lup.policy.shell_rules import ShellCommandRule
from lup.policy.survey import classify_forms, survey_shell_rules
from lup.types import StringMap

# Open keys only by spelling: the effects are lup's own closed set, but this
# reads them off a verdict rather than matching on them, so a caller may add
# a colour for an effect a later policy introduces.
EFFECT_STYLES: StringMap = {
    "allow": typer.colors.GREEN,
    "ask": typer.colors.YELLOW,
    "defer": typer.colors.BLUE,
    "deny": typer.colors.RED,
}
"""How each verdict reads at a glance, for a caller that does not say."""


class PolicyVerdict(BaseModel, frozen=True):
    """One classified input and what the declared policy decided about it."""

    input: str
    kind: str
    effect: str
    reason: str


def verdict_for(
    subject: str,
    kind: str,
    sandbox: bool,
    autonomous: bool,
    cwd: Path,
    hooks: HookSet,
) -> PolicyVerdict:
    """Classify one input under the project's declared policy.

    ``edit`` is judged as a whole-file write of unchanged content, which is
    the shape that isolates the path gates from the anti-pattern and size
    ones: the question this command answers is whether a path may be written
    at all, not whether some particular diff passes review.
    """
    policy = semantic_policy_for(hooks, sandbox_active=sandbox, autonomous=autonomous)
    match kind:
        case "shell":
            event = ShellCommand(command=subject, cwd=cwd)
        case "fetch":
            event = FetchUrl(url=AnyHttpUrl(subject))
        case _:
            event = EditBatch(
                changes=[EditChange(path=Path(subject), before=None, after="")]
            )
    decision = policy.decide(event)
    return PolicyVerdict(
        input=subject, kind=kind, effect=decision.effect, reason=decision.reason
    )


def explain(
    subjects: list[str],
    kind: str,
    sandbox: bool,
    autonomous: bool,
    as_json: bool,
    hooks: HookSet,
    styles: StringMap = EFFECT_STYLES,
) -> None:
    """Print each input's verdict, exiting non-zero when none of them allow."""
    root = Path.cwd()
    verdicts = [
        verdict_for(subject, kind, sandbox, autonomous, root, hooks)
        for subject in subjects
    ]
    if as_json:
        output_json([verdict.model_dump() for verdict in verdicts])
    else:
        for verdict in verdicts:
            effect = typer.style(
                f"{verdict.effect:>5}",
                fg=styles[verdict.effect],
                bold=True,
            )
            typer.echo(f"{effect}  {verdict.input}")
            typer.echo(f"       {verdict.reason}")
    if all(verdict.effect != "allow" for verdict in verdicts):
        raise typer.Exit(1)


def survey(
    rules: list[ShellCommandRule],
    as_json: bool,
    output: Path | None,
    provenance: bool,
) -> None:
    """Print every command form the shell vocabulary declares, and its verdict.

    Reshaping a table is where verdicts move without anybody deciding to move
    them, so ``output`` exists to capture the whole table before a change and
    diff it against the same capture after — an equality no reading of the
    rules can establish.

    ``provenance`` answers the other question, one row per rule rather than one
    per form: which level of the nesting supplied each axis, so a verdict that
    was inherited from three levels up says so instead of being re-derived.
    """
    lines = (
        [rule.provenance() for rule in survey_shell_rules(rules)]
        if provenance
        else [
            json.dumps(form.model_dump(), sort_keys=True)
            if as_json
            else f"{form.effect:>5} {form.sandbox:>7}  {form.command}"
            for form in classify_forms(rules)
        ]
    )
    if output is None:
        for line in lines:
            typer.echo(line)
        return
    output.write_text("\n".join([*lines, ""]), encoding="utf-8")
    typer.echo(f"{len(lines)} line(s) written: {output}")
