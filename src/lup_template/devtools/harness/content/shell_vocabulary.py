"""This project's shell auto-allow vocabulary, composed from library groups.

The rule models, :func:`~lup.policy.shell_rules.erase_shell_rules`, and the
groups in :mod:`lup.policy.vocabulary` are library mechanism; what is *this
project's* is the composition below — which groups it takes, what it passes
them, and the one rule no other project has. A downstream project writes its
own composition and never edits lup to change a verdict.

Two judgements here differ from the library's offered defaults, and both are
arguments rather than a fork:

``guard_force_push=False`` — this repository's rebase flow republishes a
branch with ``--force`` every round, so the force is the ordinary case and
guarding it put an approval question on nearly every push. What removes a
remote ref outright stays guarded, because no second push restores it.

``redirect_checkout=True`` — this repository has settled on ``git switch``
and ``git restore``, so ``checkout`` denies and names them instead of asking.
"""

from lup.policy.shell_rules import (
    RunnerTargetRule,
    ShellCommandRule,
    ShellOperationRule,
    ShellSubcommandRule,
)
from lup.policy.vocabulary import (
    docker_rule,
    gh_rule,
    git_rule,
    guarded_tool_rules,
    judged_ask_rules,
    read_only_rules,
    redirected_rules,
    runner_target_rules,
)


def lup_devtools_rule() -> ShellCommandRule:
    """Admit this toolchain reached without `uv`, only where it has to be.

    `uv run lup-devtools` is the entry point everywhere else and stays one:
    parsing `pyproject.toml` is how it guarantees a synced environment. The
    conflict workflow is the single place that guarantee cannot be paid for,
    because its commands exist to repair the merge that left the manifest
    unparseable — so those are documented as `.venv/bin/lup-devtools`, and a
    documented invocation the classifier does not resolve is a denial rather
    than a fix. The rows match on the executable's name, which is what a
    console script named by path still presents. Every other subcommand
    bounces back naming the spelling that is admitted, which is what an agent
    reaching past `uv` for no reason should be told.

    The one operation it admits carries the placement `RUNNER_TARGETS` gives
    the same toolchain reached through `uv`: a merge repair rewrites the git
    configuration behind a worktree, which a confined session cannot do, and a
    verdict that depended on which spelling reached it would be two policies.
    """
    reach_through_uv = (
        "reach this toolchain through `uv run lup-devtools`, which guarantees"
        " the environment it runs in — only the conflict workflow, whose"
        " commands must start while the manifest does not parse, is documented"
        " without it"
    )
    return ShellCommandRule(
        name="lup-devtools",
        default_effect="deny",
        subcommands=[
            ShellSubcommandRule(
                name="dev",
                effect="deny",
                operations=[
                    ShellOperationRule(
                        name="conflict", effect="allow", sandbox="outside"
                    )
                ],
                reason=reach_through_uv,
            )
        ],
        reason=reach_through_uv,
    )


RUNNER_TARGETS: list[RunnerTargetRule] = runner_target_rules()
"""What `uv run <target>` may reach here, and where each target has to run.

Taken as the library offers it: the checkers are this project's, and
`lup-devtools` is the toolchain the group places outside the sandbox.
"""


# lup: ignore[constant-declaration] — which vocabulary groups this project takes
# and what it passes them, decided here because nothing sits above it to be asked
SHELL_RULES: list[ShellCommandRule] = [
    *read_only_rules(),
    *judged_ask_rules(),
    *redirected_rules(),
    *guarded_tool_rules(),
    lup_devtools_rule(),
    git_rule(guard_force_push=False, redirect_checkout=True),
    gh_rule(),
    docker_rule(),
]
