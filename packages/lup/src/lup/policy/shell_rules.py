"""The shape a shell vocabulary takes, and its erasure into kernel rows.

The hermetic kernel classifies one shell command by consulting primitive rows,
exactly as it consults URL scopes and protected-path rows: the control flow
lives in :mod:`lup.policy.kernel`, and the *vocabulary* — which tools are safe
to run unattended, which forms need a human — is a judgement about one
project's toolchain, so it arrives from outside. This module declares the
shape that judgement takes as a nested pydantic table a human can read and
extend, and :func:`erase_shell_rules` flattens it into the ``ShellRuleRow``
tuples the kernel interprets.

An application declares its own table and hands it to ``ShellPolicy`` and to
``HookSet.shell_rules``; :mod:`lup.policy.bundle` erases that same table into
``policy_data.py`` at generation time, so the canonical ``ShellPolicy`` and
every generated dispatcher decide identically. This repository's table is
``lup_template.devtools.harness.content.shell_vocabulary``.

Three nesting levels mirror how real tools are shaped:

* a bare command — ``ls``, ``sort`` — declares ``default_effect="allow"``,
  optionally with ``ask_flags`` that turn a reader into a writer (``sort -o``,
  ``find -delete``);
* a subcommand command — ``git``, ``gh`` — declares ``default_effect="deny"``
  (an unjudged subcommand bounces back to the agent) and lists the subcommands
  it has judged (``git status`` allows, ``git push`` asks); its ``value_flags``
  skip value-taking globals (``git -C <path>``) so the value is never read as
  the subcommand, and its ``ask_flags`` guard dangerous globals (``git -c``);
* a subcommand whose *operation* word decides safety — ``git worktree add`` is
  reversible, ``git worktree remove`` is not — carries ``operations``.

Both axes cascade down that nesting, and absence has exactly one meaning
everywhere: a level that omits ``effect`` or ``sandbox`` inherits the value
from the level above it, and a level that states one overrides what it
inherited in either direction — widening a restrictive parent is as ordinary
as narrowing a permissive one. So ``git`` says once where its subcommands run,
and each of them says only what differs.

Absence is distinguished from a stated value of the same word by what pydantic
already records about which fields a declaration supplied, so no field is
retyped as optional to carry the distinction. The one field with no such
escape is ``ShellCommandRule.default_effect``, which is required: who decides
has no member meaning "no opinion", so a command that forgot to say is a gap a
reader should see rather than a value it silently inherited. ``ROOT_EFFECT``
is what would be inherited beneath every table, spelled restrictively for the
same reason.
"""

from typing import Literal, TypedDict

from pydantic import BaseModel

from lup.policy.kernel.decision import DecisionEffect, SandboxPlacement
from lup.policy.kernel.rows import RunnerTargetRow, RuleLevel, ShellRuleRow

type CommandEffect = Literal["allow", "ask", "deny"]

ROOT_EFFECT: CommandEffect = "deny"
"""Who decides, beneath a table that says nothing — nobody, so a human.

Every command declares its own effect, so this is structurally unreachable
through the models. It is spelled anyway, and spelled restrictively, because
the value a forgotten declaration would fall to is the one place a silent
default becomes a grant.
"""

ROOT_SANDBOX: SandboxPlacement = "ambient"
"""Where a call runs, beneath a table that says nothing — wherever it lands.

Unlike the effect axis, this one has a member meaning "no opinion", so
omission is a statement here rather than a gap, and a command is not made to
repeat it.
"""


class DeclaredAxes(BaseModel, frozen=True):
    """What one level of a table states, leaving the rest to the level above.

    ``None`` is inheritance, and it is the only thing absence means anywhere in
    the table — never a reset to the most permissive value.
    """

    effect: CommandEffect | None = None
    sandbox: SandboxPlacement | None = None


class RowAxes(TypedDict):
    """The four fields one resolved pair contributes to an erased row."""

    effect: DecisionEffect
    effect_source: RuleLevel
    sandbox: SandboxPlacement
    sandbox_source: RuleLevel


class ResolvedAxes(BaseModel, frozen=True):
    """Both axes as one level resolved them, and where each value came from."""

    effect: CommandEffect
    effect_source: RuleLevel
    sandbox: SandboxPlacement
    sandbox_source: RuleLevel

    def row_fields(self) -> RowAxes:
        """This pair as the erased row spells it, provenance included."""
        return RowAxes(
            effect=self.effect,
            effect_source=self.effect_source,
            sandbox=self.sandbox,
            sandbox_source=self.sandbox_source,
        )

    def inherit(self, declared: DeclaredAxes, level: RuleLevel) -> "ResolvedAxes":
        """These axes as the level beneath resolves them over its own statements.

        A declaration wins in either direction: widening a restrictive parent is
        as ordinary as narrowing a permissive one, so nothing here compares the
        two values — only whether the level supplied one.
        """
        return ResolvedAxes(
            effect=self.effect if declared.effect is None else declared.effect,
            effect_source=self.effect_source if declared.effect is None else level,
            sandbox=self.sandbox if declared.sandbox is None else declared.sandbox,
            sandbox_source=self.sandbox_source if declared.sandbox is None else level,
        )


ROOT_AXES = ResolvedAxes(
    effect=ROOT_EFFECT,
    effect_source="root",
    sandbox=ROOT_SANDBOX,
    sandbox_source="root",
)
"""What the outermost level of every table inherits from."""


class RunnerTargetRule(BaseModel, frozen=True):
    """One ``uv run <target>`` a project blesses, and where it has to run.

    ``uv`` is parsed rather than matched against the command table, so this is
    the only surface on which a runner target can carry the sandbox axis. A
    toolchain that opens its own agent sessions needs ``outside``: the runtime
    creates per-session state under its configuration directory, and a session
    launched from inside a sandbox that does not grant that path loses its
    shell entirely. Declaring it beside the name is what keeps the escape off
    the call sites — one that has to remember a flag is one that forgets it.

    The field is the whole placement vocabulary and not a two-valued switch,
    so a target that only sometimes needs the outside says ``escalable`` here
    and keeps the ordinary run confined. One field says every way a target may
    leave, which is what stops a second way of saying it from growing.
    """

    name: str
    sandbox: SandboxPlacement = "ambient"


class ShellOperationRule(BaseModel, frozen=True):
    """One operation word under a subcommand — e.g. ``worktree remove``."""

    name: str
    effect: CommandEffect = ROOT_EFFECT
    ask_flags: list[str] = []
    sandbox: SandboxPlacement = ROOT_SANDBOX
    reason: str = ""

    def declared(self) -> DeclaredAxes:
        """The axes this operation states itself, leaving the rest to inherit."""
        supplied = self.model_fields_set
        return DeclaredAxes(
            effect=self.effect if "effect" in supplied else None,
            sandbox=self.sandbox if "sandbox" in supplied else None,
        )


class ShellSubcommandRule(BaseModel, frozen=True):
    """One subcommand under a command — e.g. ``git worktree``, ``gh pr``.

    ``read_verbs`` name action-selecting flags that pin a one-action-at-a-time
    subcommand to its query form (``git config --get``); their presence among
    literal, unguarded words de-escalates a non-allow effect to allow.
    """

    name: str
    effect: CommandEffect = ROOT_EFFECT
    ask_flags: list[str] = []
    read_verbs: list[str] = []
    operations: list[ShellOperationRule] = []
    sandbox: SandboxPlacement = ROOT_SANDBOX
    reason: str = ""

    def declared(self) -> DeclaredAxes:
        """The axes this subcommand states itself, leaving the rest to inherit."""
        supplied = self.model_fields_set
        return DeclaredAxes(
            effect=self.effect if "effect" in supplied else None,
            sandbox=self.sandbox if "sandbox" in supplied else None,
        )


class ShellCommandRule(BaseModel, frozen=True):
    """One executable — a read-only tool, or a subcommand-gated command.

    On a subcommand-gated command, ``value_flags`` name the global options that
    consume the following word (``git -C <path>``) so the value is never read
    as the subcommand, and ``ask_flags`` guard dangerous globals in that same
    pre-subcommand position (``git -c``). ``allow_flags`` declare the pure
    read-only form of a non-allow command: the row de-escalates to allow only
    when every argument is exactly one of the named flags (``ssh-add -l``).
    ``read_verbs`` do the same for a command whose read-only form still takes
    operands, so no all-flags test can recognize it (``nc -z host port``): a
    declared verb among otherwise literal, unguarded words pins the action.

    ``sandbox`` is the other axis, declared here rather than inferred by a
    renderer: it says where an invocation matched by this rule has to run,
    whatever effect the rule reaches. Reads that need a remote take
    ``outside`` and run unprompted; writes that need one take ``outside``
    beside an ``ask``, so the approval says both things at once. A command
    that usually belongs inside but sometimes has to leave takes
    ``escalable``, which confines it and lets the agent making the call take
    it out — one field says every one of these, so a rule that permits the
    outside never has a second way to say it.
    """

    name: str
    default_effect: CommandEffect
    ask_flags: list[str] = []
    allow_flags: list[str] = []
    read_verbs: list[str] = []
    value_flags: list[str] = []
    subcommands: list[ShellSubcommandRule] = []
    sandbox: SandboxPlacement = ROOT_SANDBOX
    reason: str = ""

    def declared(self) -> DeclaredAxes:
        """The axes this command states itself, leaving the rest to inherit."""
        return DeclaredAxes(
            effect=self.default_effect,
            sandbox=self.sandbox if "sandbox" in self.model_fields_set else None,
        )


def erase_runner_targets(targets: list[RunnerTargetRule]) -> list[RunnerTargetRow]:
    """Flatten the blessed runner targets into the kernel's primitive rows."""
    return [
        RunnerTargetRow(name=target.name, sandbox=target.sandbox) for target in targets
    ]


def erase_shell_rules(rules: list[ShellCommandRule]) -> list[ShellRuleRow]:
    """Flatten the nested table into the kernel's primitive command rows.

    Each row carries its match levels, its flag lists, its reason, and both
    axes already resolved against every level above it, so the kernel matching
    one row has the whole answer and never composes anything. An empty string
    at a level means "the default at that level". A command contributes one
    default row plus, per subcommand, one row per operation and a
    subcommand-default row for the bare form.

    Resolving here rather than at decision time is what keeps the canonical
    policy and every generated dispatcher from disagreeing about what a level
    inherited: they read the same rows, so there is nothing left to disagree
    about.
    """

    def subcommand_rows(
        command_name: str, subcommand: ShellSubcommandRule, above: ResolvedAxes
    ) -> list[ShellRuleRow]:
        axes = above.inherit(subcommand.declared(), "subcommand")
        operations = [
            ShellRuleRow(
                command=command_name,
                subcommand=subcommand.name,
                operation=operation.name,
                ask_flags=list(operation.ask_flags),
                allow_flags=[],
                read_verbs=[],
                value_flags=[],
                reason=operation.reason,
                **axes.inherit(operation.declared(), "operation").row_fields(),
            )
            for operation in subcommand.operations
        ]
        default = ShellRuleRow(
            command=command_name,
            subcommand=subcommand.name,
            operation="",
            ask_flags=list(subcommand.ask_flags),
            allow_flags=[],
            read_verbs=list(subcommand.read_verbs),
            value_flags=[],
            reason=subcommand.reason,
            **axes.row_fields(),
        )
        return [*operations, default]

    def command_rows(command: ShellCommandRule) -> list[ShellRuleRow]:
        axes = ROOT_AXES.inherit(command.declared(), "command")
        default = ShellRuleRow(
            command=command.name,
            subcommand="",
            operation="",
            ask_flags=list(command.ask_flags),
            allow_flags=list(command.allow_flags),
            read_verbs=list(command.read_verbs),
            value_flags=list(command.value_flags),
            reason=command.reason,
            **axes.row_fields(),
        )
        nested = [
            row
            for subcommand in command.subcommands
            for row in subcommand_rows(command.name, subcommand, axes)
        ]
        return [default, *nested]

    return [row for command in rules for row in command_rows(command)]
