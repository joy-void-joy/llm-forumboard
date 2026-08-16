"""Library-side validated policies that erase into kernel rows to decide.

Each policy (shell, fetch, edit) validates its configuration as pydantic
surfaces — URL scopes, path rules, the canonical anti-pattern set — then
flattens them into primitive rows and delegates every verdict to
:mod:`lup.policy.kernel`. :mod:`lup.policy.bundle` performs the same erasure
at generation time, which is how this layer and the generated dispatchers
stay decision-identical; the shared fixture suite asserts exactly that.
"""

from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, BaseModel, Field

from lup.codescan.antipatterns import AntiPattern, patterns_for_suffix
from lup.policy.contracts import DecisionPolicy
from lup.policy.grants import LeaseGrants
from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.edit import (
    decide_edit,
    path_rule_matches as kernel_path_rule_matches,
)
from lup.policy.kernel.fetch import decide_fetch
from lup.policy.assets.host import (
    directory_write_targets,
    recoverable_write_targets,
)
from lup.policy.kernel.lex import (
    parse_shell_words,
    shell_path_verb_targets,
    shell_write_targets,
)
from lup.policy.kernel.rows import (
    AntiPatternRow,
    PathRoleRow,
    PathRuleKind,
    PathRuleRow,
    UrlScopeRow,
)
from lup.policy.kernel.shell import decide_shell, decide_shell_segment, shell_context
from lup.policy.kernel.words import command_words as kernel_command_words
from lup.policy.shell_rules import (
    RunnerTargetRule,
    ShellCommandRule,
    erase_runner_targets,
    erase_shell_rules,
)
from lup.policy.models import (
    Decision,
    EditBatch,
    EditChange,
    FetchUrl,
    ShellCommand,
    UrlPathPrefix,
)


def pydantic_decision(decision: KernelDecision) -> Decision:
    """Restore the validated public decision at the kernel boundary."""
    return Decision(
        effect=decision.effect, reason=decision.reason, sandbox=decision.sandbox
    )


class UrlScope(BaseModel, frozen=True):
    """One normalized scheme/host/port and path-prefix rule."""

    origin: AnyHttpUrl
    path_prefix: UrlPathPrefix = "/"
    reason: str = ""
    include_subdomains: bool = False
    any_port: bool = False


# The three erasures to the kernel's primitive rows read alike on purpose, and
# `antipattern_row` erases a model another module declares, so it could not be
# a method even if these two were.
# lup: ignore[model-free-function] — one of that erasure family
def url_scope_row(scope: UrlScope) -> UrlScopeRow:
    """Erase a validated URL scope into the kernel's primitive row."""
    parsed = urlsplit(str(scope.origin))
    if parsed.hostname is None:
        raise ValueError("validated URL scope has no hostname")
    return UrlScopeRow(
        scheme=parsed.scheme,
        host=parsed.hostname,
        port=parsed.port,
        path_prefix=scope.path_prefix,
        reason=scope.reason,
        include_subdomains=scope.include_subdomains,
        any_port=scope.any_port,
    )


class FetchPolicy(DecisionPolicy[FetchUrl]):
    """Evaluate deny scopes before allow scopes and ask on everything else."""

    def __init__(self, allowed: list[UrlScope], denied: list[UrlScope]) -> None:
        self.allowed = list(allowed)
        self.denied = list(denied)

    def decide(self, event: FetchUrl) -> Decision:
        return pydantic_decision(
            decide_fetch(
                str(event.url),
                [url_scope_row(scope) for scope in self.allowed],
                [url_scope_row(scope) for scope in self.denied],
            )
        )


class ShellSegment(BaseModel, frozen=True):
    """One parsed command segment with its ordered shell words."""

    words: list[str] = Field(min_length=1)


def command_words(words: list[str]) -> list[str]:
    """Expose effective-command parsing for compatibility consumers."""
    return kernel_command_words(words)


def parse_shell_segments(command: str) -> list[ShellSegment] | None:
    """Expose validated segment models for compatibility consumers."""
    segments = parse_shell_words(command)
    if isinstance(segments, KernelDecision):
        return None
    return [ShellSegment(words=words) for words in segments]


class ShellPolicy(DecisionPolicy[ShellCommand]):
    """Delegate shell classification to the shared hermetic kernel.

    The vocabulary is the caller's: ``rules`` is the whole table this project
    judges, not an extension of one the library chose. URL scopes feed the
    kernel's curl screen, so shell reads and WebFetch consult one declared
    origin table.
    """

    def __init__(
        self,
        rules: list[ShellCommandRule],
        allowed_urls: list[UrlScope] | None = None,
        denied_urls: list[UrlScope] | None = None,
        sandbox_active: bool = False,
        sandbox_excluded_commands: list[str] | None = None,
        escapable: bool = False,
        trusted_script_roots: list[str] | None = None,
        interactive: bool = True,
        path_roles: list[PathRoleRow] | None = None,
        path_rules: list["PathRule"] | None = None,
        recoverable_target_limit: int = 5,
        runner_targets: list[RunnerTargetRule] | None = None,
    ) -> None:
        self.path_rules = [path_rule_row(rule) for rule in path_rules or []]
        self.path_roles = path_roles or []
        self.recoverable_target_limit = recoverable_target_limit
        self.runner_targets = erase_runner_targets(runner_targets or [])
        self.escapable = escapable
        self.rules = erase_shell_rules(rules)
        self.allowed_scopes = [url_scope_row(scope) for scope in allowed_urls or []]
        self.denied_scopes = [url_scope_row(scope) for scope in denied_urls or []]
        self.sandbox_active = sandbox_active
        self.sandbox_excluded_commands = sandbox_excluded_commands or []
        self.trusted_script_roots = trusted_script_roots or []
        self.interactive = interactive

    def decide(self, event: ShellCommand) -> Decision:
        root = event.cwd or Path.cwd()
        acted_on = shell_path_verb_targets(event.command)
        return pydantic_decision(
            decide_shell(
                event.command,
                self.rules,
                self.allowed_scopes,
                self.denied_scopes,
                sandboxed=self.sandbox_active and not event.unsandboxed,
                excluded_commands=self.sandbox_excluded_commands,
                trusted_script_roots=self.trusted_script_roots,
                path_roles=self.path_roles,
                path_rules=self.path_rules,
                interactive=self.interactive,
                existing_targets=[
                    target
                    for target in [*shell_write_targets(event.command), *acted_on]
                    if (root / target).exists()
                ],
                recoverable_targets=recoverable_write_targets(
                    [*shell_write_targets(event.command), *acted_on], root
                ),
                directory_targets=directory_write_targets(acted_on, root),
                recoverable_target_limit=self.recoverable_target_limit,
                runner_targets=self.runner_targets,
                escapable=self.escapable,
            )
        )

    def decide_segment(self, segment: ShellSegment) -> Decision:
        return pydantic_decision(
            decide_shell_segment(
                segment.words,
                shell_context(
                    self.rules,
                    self.allowed_scopes,
                    self.denied_scopes,
                    self.trusted_script_roots,
                    self.path_roles,
                    self.path_rules,
                ),
            )
        )


class PathRule(BaseModel, frozen=True):
    """One semantic protected-path match supplied by a composition root.

    ``kind`` spans the whole primitive vocabulary rather than a subset of it.
    A rule a generated dispatcher can enforce and a composed session cannot
    express is a rule whose reach depends on who launched the run, which is
    the one thing single-sourcing the policy exists to prevent.
    """

    kind: PathRuleKind
    value: str
    reason: str
    allow_autonomous: bool = False

    def matches(self, path: Path) -> bool:
        """Compare a path with this rule through the canonical kernel matcher."""
        return kernel_path_rule_matches(
            path.as_posix(), path.exists(), path_rule_row(self)
        )


# lup: ignore[model-free-function] — the same erasure family as url_scope_row
def path_rule_row(rule: PathRule) -> PathRuleRow:
    """Erase one validated path rule into the kernel's primitive row."""
    return PathRuleRow(
        kind=rule.kind,
        value=rule.value,
        reason=rule.reason,
        allow_autonomous=rule.allow_autonomous,
    )


def human_owned_path_rule(path: str) -> PathRule:
    """Declare one human-owned file whose edits always require approval."""
    return PathRule(
        kind="exact",
        value=path,
        reason=(
            f"{path} is human-authored; propose changes via AskUserQuestion"
            " instead of editing"
        ),
    )


def antipattern_row(rule: AntiPattern) -> AntiPatternRow:
    """Erase one declared rule into the primitive row the kernel matches on.

    The single projection from the declaration to the runtime shape. Both the
    live policy and the bundled hermetic runtime go through it, so a field the
    declaration gains cannot reach one gate and not the other.
    """
    return AntiPatternRow(
        id=rule.id,
        pattern=rule.pattern.pattern,
        message=rule.message,
        context=rule.context,
        refiner="" if rule.refiner is None else rule.refiner.exempt.__name__,
        strength=rule.strength,
    )


def antipattern_rows(change: EditChange) -> list[AntiPatternRow]:
    """Compile rules selected by one edit path into primitive kernel rows."""
    patterns = patterns_for_suffix(change.path.suffix.lower())
    if patterns is None:
        return []
    return [antipattern_row(rule) for rule in patterns]


class EditPolicy(DecisionPolicy[EditBatch]):
    """Apply the shared marker, anti-pattern, path, deletion, and size gates.

    ``grants`` is asked what the lease holds per change rather than read into
    a list when the policy is built, which is what lets a gate granted while
    this session runs release the very next edit — and what makes one taken
    back stop releasing it. The generated dispatchers read the same document,
    so a lease has one answer wherever it is asked.
    """

    def __init__(
        self,
        protected: list[PathRule],
        maximum_added_lines: int = 3,
        autonomous: bool = False,
        path_roles: list[PathRoleRow] | None = None,
        grants: LeaseGrants | None = None,
    ) -> None:
        self.path_roles = path_roles or []
        self.grants = LeaseGrants() if grants is None else grants
        self.protected = list(protected)
        self.maximum_added_lines = maximum_added_lines
        self.autonomous = autonomous

    def decide(self, event: EditBatch) -> Decision:
        decisions = [self.decide_change(change) for change in event.changes]
        denied = next((item for item in decisions if item.effect == "deny"), None)
        if denied is not None:
            return denied
        asked = next((item for item in decisions if item.effect == "ask"), None)
        if asked is not None:
            return asked
        deferred = next((item for item in decisions if item.effect == "defer"), None)
        if deferred is not None:
            return deferred
        return Decision(effect="allow", reason="every edit in the batch is safe")

    def decide_change(self, change: EditChange) -> Decision:
        suffix = change.path.suffix.lower()
        return pydantic_decision(
            decide_edit(
                change.path.as_posix(),
                change.before,
                change.after,
                path_exists=change.path.exists(),
                path_rules=[path_rule_row(rule) for rule in self.protected],
                antipattern_rows=antipattern_rows(change),
                path_roles=self.path_roles,
                maximum_added_lines=self.maximum_added_lines,
                autonomous=self.autonomous,
                allowances=self.grants.granted(),
                python_source=suffix in (".py", ".pyi"),
            )
        )
