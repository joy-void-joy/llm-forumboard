# lup: ignore[own-model-dispatch]
# Which semantic tool a native payload decodes to is exactly what these parity
# tests claim: `UnknownTool` is the fail-closed outcome for a novel or
# malformed operation, `ShellCommand` the recognized one. The decoded type is
# the assertion, observed from outside both decoders.
"""Cross-native semantic decoding and conservative policy parity tests."""

import ast
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Literal, get_args

import pytest
import sh
from pydantic import AnyHttpUrl, BaseModel, Field

from lup.adapters.claude.native import (
    ClaudeBeforeToolEvent,
    ClaudeEditBatchOperation,
    ClaudeEventDecoder,
    ClaudeHookPayload,
    ClaudeUnknownOperation,
    ClaudeDecisionRenderer,
    parse_claude_before_tool,
)
from lup.adapters.codex.native import (
    CodexBeforeToolEvent,
    CodexEventDecoder,
    CodexFileChange,
    CodexFileChangeOperation,
    CodexUnknownOperation,
    CodexDecisionRenderer,
)
from lup.harness.enforcement import declared_path_rules, semantic_policy_for
from lup.harness.models import HookSet
from lup.types import JsonObject
from lup.policy.chain import UnknownToolPolicy
from lup.policy.grants import LeaseGrants, write_allowance_grants
from lup.policy.identity import ConcernAllowance
from lup.policy.bundle import (
    bundled_antipattern_rows,
    policy_kernel_modules,
    render_policy_data,
    runtime_path_rules,
    runtime_url_scope,
)
from lup.policy.kernel.decision import (
    DecisionEffect,
    KernelDecision,
    SANDBOX_ESCALATION_OFFER,
    SANDBOX_ESCALATION_UNSUPPORTED,
    SANDBOX_TRAPPED_REASON,
    SandboxPlacement,
    sandbox_escaped,
)
from lup.policy.kernel.edit import decide_edit
from lup.policy.kernel.rows import PathRoleRow
from lup.policy.refused_tools import RefusedTool, erase_refused_tools
from lup.policy.kernel.lex import shell_write_targets
from lup.policy.models import (
    Decision,
    EditBatch,
    EditChange,
    FetchUrl,
    ShellCommand,
    ToolIdentity,
    UnknownTool,
)
from lup.policy.rules import (
    EditPolicy,
    antipattern_rows,
    FetchPolicy,
    PathRule,
    ShellPolicy,
    UrlScope,
    human_owned_path_rule,
    path_rule_row,
)

from lup.policy.vocabulary import runner_target_rules
from lup_template.devtools.harness.catalog import declared_hook_set, portable_harness
from lup_template.devtools.harness.content.shell_vocabulary import SHELL_RULES


class DecisionCase(BaseModel, frozen=True):
    """One primitive input and its expected policy effect."""

    input: str
    effect: Literal["allow", "ask", "deny", "defer"]
    sandboxed: bool = False
    escapable: bool = False
    """Whether the host judging this case can place one call outside its sandbox.

    Off by default, matching the kernel: a host that says nothing about
    placement cannot perform one, and a command declared ``outside`` is
    stopped there rather than run somewhere its declaration forbids."""

    interactive: bool = True
    existing: list[str] = Field(default_factory=list)
    """Repository-relative files that already exist when the case is judged."""


class HostShape(BaseModel, frozen=True):
    """The host facts a case is judged under, as one hashable identity."""

    sandboxed: bool
    escapable: bool
    interactive: bool


class EditDecisionCase(BaseModel, frozen=True):
    """One edit fixture shared by canonical and assembled policy forms."""

    path: str
    before: str | None
    after: str | None
    effect: Literal["allow", "ask", "deny", "defer"]
    autonomous: bool = False
    path_exists: bool = True


# The roles this repository declares, mirrored so the fixtures judge the same
# vocabulary the generated runtime is rendered with.
FIXTURE_PATH_ROLES = [
    PathRoleRow(root="tests", role="test"),
    PathRoleRow(root="tmp", role="scratch"),
    PathRoleRow(root=".venv", role="scratch"),
    PathRoleRow(root="build", role="scratch"),
    PathRoleRow(root="node_modules", role="scratch"),
]

FIXTURE_PATH_RULES = declared_path_rules(declared_hook_set())
"""The protected-path table this repository declares.

Shared by the shell and edit fixtures rather than restated for each, because
a table the two gates could be given differently is the drift these cases
exist to catch."""

FIXTURE_EXCLUDED_COMMANDS = ["quuxify *"]
"""One command the fixtures treat as taken out of the OS sandbox.

Unjudged exactly like `frobnicate` beside it, so the pair says the whole
rule between them: unjudged work defers to a boundary that covers it, and
denies where the declaration removed the cover."""

FIXTURE_REFUSED_TOOLS = [
    RefusedTool(tool="Quuxify", reason="quuxifying leaves the repository"),
    RefusedTool(
        tool="Skill", specifier="quux-design", reason="designing quux leaves it too"
    ),
]
"""One whole-tool refusal and one narrowed to a single subject.

The pair says the rule between them: a bare row refuses every use of its
tool, a specifier row refuses one and leaves the tool's other uses to the
runtime, and neither is a name this repository actually refuses — what is
being pinned is the shape, not this project's own judgement."""

FIXTURE_RECOVERABLE_LIMIT = 5
FIXTURE_RUNNER_TARGETS = runner_target_rules()
"""What this project declares `uv run <target>` may reach, and where each runs,
which is what the shell fixtures below are written against."""
"""How many restorable files one command may destroy before it asks."""

SHELL_POLICY_CASES = [
    DecisionCase(input="env MODE=test python script.py", effect="deny"),
    DecisionCase(input="uv run --with requests python -c 'x'", effect="deny"),
    # A scratch root is gitignored, so a script there reaches no reviewer and
    # no diff; the escalation ladder routes one-off work to devtools instead.
    DecisionCase(input="uv run pytest | uv run python tmp/oneoff.py", effect="deny"),
    DecisionCase(input="uv run python tmp/oneoff.py", effect="deny"),
    DecisionCase(input="find . -name '*.py' | xargs grep TODO", effect="allow"),
    DecisionCase(input="echo x | xargs rm -rf", effect="ask"),
    DecisionCase(input="cd /tmp/worktree && uv run pytest", effect="allow"),
    DecisionCase(input="git status\ncurl https://example.com", effect="ask"),
    DecisionCase(input="find . -name '*.tmp' -delete", effect="ask"),
    DecisionCase(input="cat x |& rm -rf ~", effect="ask"),
    DecisionCase(input="cat x ;& rm -rf ~", effect="deny"),
    DecisionCase(
        input="echo payload > pyproject.toml",
        effect="ask",
        existing=["pyproject.toml"],
    ),
    DecisionCase(
        input="echo payload >> src/generated.py",
        effect="ask",
        existing=["src/generated.py"],
    ),
    # `gh api` is the read path for everything the typed subcommands cannot
    # express, so it is screened by method and body the way curl is rather
    # than asked about wholesale.
    DecisionCase(input="gh api /repos/o/r/pulls/1", effect="allow"),
    DecisionCase(input="gh api --jq .state /repos/o/r/pulls/1", effect="allow"),
    DecisionCase(input="gh api -X DELETE /repos/o/r/x", effect="ask"),
    DecisionCase(input="gh api -f title=x /repos/o/r/issues", effect="ask"),
    DecisionCase(input="gh api --method PATCH /repos/o/r", effect="ask"),
    # A read-only form of a writing command allows; the writing form asks.
    DecisionCase(input="tar -tzf archive.tgz", effect="allow"),
    DecisionCase(input="tar -xzf archive.tgz", effect="ask"),
    DecisionCase(input="gzip -l archive.gz", effect="allow"),
    DecisionCase(input="gzip archive.txt", effect="ask"),
    DecisionCase(input="make -n", effect="allow"),
    DecisionCase(input="make test", effect="ask"),
    DecisionCase(input="npm ls", effect="allow"),
    DecisionCase(input="npm install", effect="ask"),
    DecisionCase(input="ss -ltnp", effect="allow"),
    DecisionCase(input="ss -K dst 1.2.3.4", effect="ask"),
    DecisionCase(input="gh pr view 123", effect="allow"),
    DecisionCase(input="gh pr list --state open", effect="allow"),
    DecisionCase(input="gh pr diff 123", effect="allow"),
    DecisionCase(input="gh issue view 7", effect="allow"),
    DecisionCase(input="tree -L 2 src", effect="allow"),
    DecisionCase(input="uv run tool --help", effect="allow"),
    DecisionCase(
        input=(
            "UV_CACHE_DIR=/tmp/lup-uv-cache uv run lup-devtools "
            "harness resolve --adapter codex"
        ),
        effect="allow",
    ),
    DecisionCase(
        input="uv run lup-devtools dev worktree create feature", effect="allow"
    ),
    # The conflict workflow is documented without `uv run`, whose manifest
    # parse is exactly what a conflicted manifest defeats, so the classifier
    # resolves the launcher named by path. Nothing else about the toolchain is
    # admitted that way — it bounces back naming the spelling that is.
    DecisionCase(
        input=".venv/bin/lup-devtools dev conflict status --json", effect="allow"
    ),
    DecisionCase(
        input=".venv/bin/lup-devtools dev conflict audit pyproject.toml", effect="allow"
    ),
    DecisionCase(input=".venv/bin/lup-devtools dev conflict complete", effect="allow"),
    DecisionCase(input="lup-devtools dev conflict list", effect="allow"),
    DecisionCase(input=".venv/bin/lup-devtools dev check", effect="deny"),
    DecisionCase(input=".venv/bin/lup-devtools harness generate all", effect="deny"),
    # Redirections: discards and fd duplication are stripped; file writes ask.
    DecisionCase(input="grep x f 2>&1", effect="allow"),
    DecisionCase(input="grep x f > /dev/null", effect="allow"),
    DecisionCase(input="cat f 2>/dev/null", effect="allow"),
    DecisionCase(input="ls >&2", effect="allow"),
    # Overwriting an existing file is the destructive case and asks; creating
    # one destroys nothing, and a target that cannot be resolved to a literal
    # path keeps the strict verdict rather than guessing it is new.
    DecisionCase(input="echo x > out.txt", effect="ask", existing=["out.txt"]),
    DecisionCase(input="echo x > out.txt", effect="allow"),
    DecisionCase(input="echo x > nested/new.txt", effect="allow"),
    DecisionCase(input="echo x >> notes.log", effect="allow"),
    DecisionCase(input="echo x > $UNSET_DIR/out.txt", effect="ask"),
    DecisionCase(input="echo x > ~/out.txt", effect="ask"),
    DecisionCase(input="cat <<EOF", effect="deny"),
    # The session scratchpad is a write-allowed root like repo-relative tmp/;
    # reassigning TMPDIR is a security-sensitive assignment, and a suffix that
    # climbs out of the root falls back to the ordinary redirection rule —
    # which still asks whenever the target cannot be resolved to a literal.
    DecisionCase(input="echo x > $TMPDIR/out.txt", effect="allow"),
    DecisionCase(input='sort f > "${TMPDIR}/sorted.txt"', effect="allow"),
    DecisionCase(input="echo x > /tmp/claude-1000/scratch/out.txt", effect="allow"),
    DecisionCase(input="cat <<'EOF' > $TMPDIR/notes.md\nbody\nEOF", effect="allow"),
    DecisionCase(input="echo x > $TMPDIR/../etc/crontab", effect="ask"),
    DecisionCase(input="echo x > /tmp/claude-1000/../shadow", effect="allow"),
    # A /tmp path outside the session root is not scratch, so it follows the
    # ordinary create-versus-overwrite rule rather than being written freely.
    DecisionCase(input="echo x > /tmp/other/file", effect="allow"),
    DecisionCase(input="TMPDIR=/etc; echo x > $TMPDIR/passwd", effect="ask"),
    DecisionCase(input="for TMPDIR in /etc; do echo x > $TMPDIR/f; done", effect="ask"),
    # Publishing is how work becomes reviewable, so the verbs that put a
    # branch and its pull request in front of a reader are ordinary. What
    # keeps the ask is what a second attempt cannot restore, and what reaches
    # another person rather than describing your own work.
    DecisionCase(input="git push", effect="allow"),
    DecisionCase(input="git push --force origin HEAD", effect="allow"),
    DecisionCase(input="git push --delete origin feat", effect="ask"),
    DecisionCase(input="gh pr create --fill", effect="allow"),
    DecisionCase(input="gh pr ready 3", effect="allow"),
    DecisionCase(input="gh pr comment 3 --body hi", effect="ask"),
    DecisionCase(input="gh pr merge 3", effect="ask"),
    # A default-method gh api call is a query; the flags that make it
    # anything else carry the ask instead of the subcommand carrying it.
    DecisionCase(input="gh api repos/o/r/pulls", effect="allow"),
    DecisionCase(input="gh api -X POST repos/o/r/issues", effect="ask"),
    DecisionCase(input="gh api -f title=x repos/o/r/issues", effect="ask"),
    # Reading a repository is read-only however deep in git's own vocabulary
    # the question is spelled.
    DecisionCase(input="git ls-remote --heads origin", effect="allow"),
    DecisionCase(input="git diff-tree -r HEAD", effect="allow"),
    DecisionCase(input="git check-ignore -v build/x", effect="allow"),
    DecisionCase(input="git submodule status", effect="allow"),
    DecisionCase(input="git bisect log", effect="allow"),
    DecisionCase(input="git bisect start", effect="ask"),
    # A protected path is protected from the shell too. Creating a file
    # destroys nothing, which is why an ordinary new target is written
    # freely — but the rules that guard a path guard it by who owns it, not
    # by what replacing it would cost, so they answer ahead of that grant and
    # the shell cannot reach what the edit gate stops.
    DecisionCase(input="echo x > README.md", effect="ask"),
    DecisionCase(input="echo x > sync.json", effect="ask"),
    DecisionCase(input="echo x > .env.local", effect="ask"),
    DecisionCase(input="echo x > docs/fresh-note.md", effect="allow"),
    # Housekeeping confined to the disposable roots is as safe as writing
    # them; any long flag, opaque word, or outside target keeps the verb's ask.
    DecisionCase(input="rm tmp/oneoff.py", effect="allow"),
    DecisionCase(input="rm -rf tmp/scratch", effect="allow"),
    DecisionCase(input="rm -f $TMPDIR/out.txt", effect="allow"),
    DecisionCase(input="rm /tmp/claude-1000/scratch/f", effect="allow"),
    DecisionCase(input="rm tmp/x src/y", effect="ask"),
    DecisionCase(input="rm tmp/../src/x.py", effect="ask"),
    DecisionCase(input="rm --no-preserve-root -rf tmp", effect="ask"),
    DecisionCase(input="rm -rf /", effect="ask"),
    DecisionCase(input="rm .claude/settings.local.json", effect="ask"),
    DecisionCase(input="rm -rf .claude/skills", effect="ask"),
    DecisionCase(input="rm .claude/plugins/../settings.json", effect="ask"),
    # A generated plugin tree is a build product the running runtime already
    # loaded, so writing one by hand changes nothing it will honor and the
    # next generation reverts it. Every writing form refuses it and names the
    # typed source instead; a long flag the allow would not recognize must not
    # buy a way past the refusal, and reading such a path stays ordinary.
    DecisionCase(input="rm .codex/plugins/lup/hooks/scripts/policy.py", effect="deny"),
    DecisionCase(input="rm -rf .claude/plugins", effect="deny"),
    DecisionCase(input="rm .claude/plugins/lup/x tmp/y", effect="deny"),
    DecisionCase(input="rm --recursive .claude/plugins/lup", effect="deny"),
    DecisionCase(
        input="mv tmp/policy.py .claude/plugins/lup/hooks/policy.py", effect="deny"
    ),
    DecisionCase(input="cp tmp/a .codex/plugins/lup/b", effect="deny"),
    DecisionCase(input="mkdir -p .claude/plugins/lup/hooks", effect="deny"),
    DecisionCase(input="touch .codex/plugins/lup/marker", effect="deny"),
    DecisionCase(
        input="echo x > .claude/plugins/lup/hooks/scripts/policy.py", effect="deny"
    ),
    DecisionCase(input="echo x >> .codex/plugins/lup/data.py", effect="deny"),
    DecisionCase(
        input="cp .claude/plugins/lup/hooks/scripts/policy.py tmp/copy.py",
        effect="allow",
    ),
    DecisionCase(
        input="cat .claude/plugins/lup/hooks/scripts/policy.py", effect="allow"
    ),
    # Every path-taking judged-ask verb reads the same role, so a scratch root
    # is housekept without friction while production keeps the verb's ask.
    DecisionCase(input="cp tmp/a.json tmp/b.json", effect="allow"),
    DecisionCase(input="mv tmp/draft.md tmp/final.md", effect="allow"),
    DecisionCase(input="mkdir -p tmp/run/logs", effect="allow"),
    DecisionCase(input="touch tmp/marker", effect="allow"),
    DecisionCase(input="rmdir tmp/run", effect="allow"),
    DecisionCase(input="mv tmp/draft.md src/final.md", effect="ask"),
    # An empty directory anywhere, unlike the file beside it: `mkdir` cannot
    # overwrite and leaves nothing to run, so what lands inside is judged on
    # its own path rather than the directory being refused up front.
    DecisionCase(input="mkdir src/newpkg", effect="allow"),
    DecisionCase(input="touch src/newfile.py", effect="ask"),
    DecisionCase(input="cp --archive tmp/a tmp/b", effect="ask"),
    # Copying reads its sources and writes only its destination, so landing
    # production in a scratch root destroys nothing; moving out of one does,
    # because the source is removed, and that keeps the verb's ask.
    DecisionCase(input="cp src/a.py tmp/a.py", effect="allow"),
    DecisionCase(input="cp /etc/hosts tmp/hosts", effect="allow"),
    DecisionCase(input="cp tmp/a src/b.py", effect="ask"),
    DecisionCase(input="mv src/a.py tmp/a.py", effect="ask"),
    DecisionCase(input="rm /home/u/.claude/plugins/lup/x", effect="deny"),
    DecisionCase(input="echo x > /srv/tree/dev/.codex/plugins/lup/y", effect="deny"),
    DecisionCase(input="rm .codex/config.local.toml", effect="ask"),
    # Quote-aware substitution: inert inside single quotes; a live $(...)
    # classifies recursively — the inner command joins the batch, and the
    # opaque result only rides on an argument-safe outer command. Command
    # position, deep nesting, and backticks stay conservative.
    DecisionCase(input="git commit -m 'fixes $(bug)'", effect="allow"),
    DecisionCase(input="echo $(whoami)", effect="allow"),
    DecisionCase(input="cat $(git rev-parse --git-dir)/HEAD", effect="allow"),
    DecisionCase(input="wc -l $(git diff --name-only)", effect="allow"),
    DecisionCase(input='echo "today is $(date)"', effect="allow"),
    DecisionCase(input="[[ -n $(git status --porcelain) ]]", effect="allow"),
    DecisionCase(input="echo $(echo $(ls))", effect="allow"),
    DecisionCase(input="F=$(ls); echo $F", effect="allow"),
    DecisionCase(input="echo $(git push --delete origin feat)", effect="ask"),
    DecisionCase(input="echo $(rm -rf /)", effect="ask"),
    DecisionCase(input="git log $(cat names.txt)", effect="deny"),
    DecisionCase(input="F=$(ls); sed $F 's/a/b/' f", effect="deny"),
    DecisionCase(input="$(which ls) -la", effect="deny"),
    DecisionCase(input="echo $(echo $(echo $(ls)))", effect="deny"),
    DecisionCase(input="echo $(ls", effect="deny"),
    DecisionCase(input="echo `id`", effect="deny"),
    # Read-side process substitution classifies its inner command recursively;
    # the write side still asks and a substituting inner command is denied.
    DecisionCase(input="diff <(git status) <(git log)", effect="allow"),
    DecisionCase(input="diff <(sudo id) f", effect="ask"),
    DecisionCase(input="diff <(cat $(x)) f", effect="deny"),
    DecisionCase(input="cat >(tee f)", effect="ask"),
    # Loops classify their condition and body recursively; literal for-words
    # instantiate the body, and opaque word lists gate guarded arguments.
    DecisionCase(input="sleep 5", effect="allow"),
    DecisionCase(input='for f in a.py b.py; do wc -l "$f"; done', effect="allow"),
    DecisionCase(input='for f in *.py; do wc -l "$f"; done', effect="allow"),
    DecisionCase(input="until grep -q Ready dev.log; do sleep 1; done", effect="allow"),
    DecisionCase(input="while true; do date; done", effect="allow"),
    DecisionCase(
        input='for a in x y; do for b in z; do echo "$a$b"; done; done',
        effect="allow",
    ),
    # A substitution inside a loop body. Spliced where it was read, it landed
    # between the `for` header and its `do`, so the loop reader took it for a
    # condition — which a `for` loop cannot have — and refused the construct
    # while reporting that it did not parse. Running one read-only command per
    # branch is the commonest shape in this work, and all of it was blocked.
    DecisionCase(input="for b in x y; do echo $(git log -1 $b); done", effect="allow"),
    DecisionCase(input="for f in a b; do diff <(cat $f) base; done", effect="allow"),
    DecisionCase(input="for x in -i; do sed \"$x\" 's/a/b/' f; done", effect="deny"),
    DecisionCase(input='for f in *.txt; do sort "$f"; done', effect="deny"),
    DecisionCase(input='for f in a; do python "$f"; done', effect="deny"),
    DecisionCase(input='for f in a; do wc "$f"', effect="deny"),
    DecisionCase(input="while do done", effect="deny"),
    DecisionCase(input='for f a; do wc "$f"; done', effect="deny"),
    # Expanded read-only vocabulary with writer-flag guards.
    DecisionCase(input="sort f", effect="allow"),
    DecisionCase(input="sort -o out f", effect="ask"),
    DecisionCase(input="sed -n '1,5p' f", effect="allow"),
    DecisionCase(input="sed -i 's/a/b/' f", effect="deny"),
    DecisionCase(input="sed 's/x/y/e' f", effect="deny"),
    DecisionCase(input="awk '{print $1}' f", effect="allow"),
    DecisionCase(input="awk -F: '{print $2}' /etc/passwd", effect="allow"),
    DecisionCase(input="cat f | awk 'NR>=2 {print $1}'", effect="allow"),
    DecisionCase(input="awk '$3 > 5' f", effect="allow"),
    DecisionCase(input='mawk \'$1=="a" || $2=="b"\' f', effect="allow"),
    DecisionCase(input="awk '{print > \"out\"}' f", effect="deny"),
    DecisionCase(input="awk 'BEGIN{system(\"id\")}'", effect="deny"),
    DecisionCase(input="seq 3 | awk '{print | \"sort\"}'", effect="deny"),
    DecisionCase(input="gawk -i inplace '{gsub(/a/,\"b\")}1' f", effect="deny"),
    DecisionCase(input="awk -f prog.awk f", effect="deny"),
    DecisionCase(input="jq . f", effect="allow"),
    DecisionCase(input="yq '.a' f", effect="allow"),
    DecisionCase(input="yq -i '.a = 1' f", effect="ask"),
    DecisionCase(input="xmllint --noout f", effect="allow"),
    DecisionCase(input="xmllint -output out f", effect="ask"),
    DecisionCase(input="cut -f1 f", effect="allow"),
    DecisionCase(input="diff a b", effect="allow"),
    DecisionCase(input="rg TODO", effect="allow"),
    # Git: read-only and reversible-local allow; destructive forms ask.
    DecisionCase(input="git rev-parse HEAD", effect="allow"),
    DecisionCase(input="git ls-files", effect="allow"),
    DecisionCase(input="git blame f", effect="allow"),
    DecisionCase(input="git stash push", effect="allow"),
    DecisionCase(input="git reset --soft HEAD~1", effect="allow"),
    DecisionCase(input="git branch -D topic", effect="ask"),
    DecisionCase(input="git worktree remove wt", effect="ask"),
    DecisionCase(input="git stash drop", effect="ask"),
    DecisionCase(input="git reset --hard", effect="ask"),
    DecisionCase(input="git clean -fd", effect="ask"),
    DecisionCase(input="git push --delete origin feat", effect="ask"),
    DecisionCase(input="git checkout -- file", effect="deny"),
    # Ref-sourced pathspec restores name their content's commit; the shell
    # option builtin is shell-local. Both anchor history-rebuild batches.
    DecisionCase(input="set -e", effect="allow"),
    DecisionCase(input="set -euo pipefail", effect="allow"),
    DecisionCase(input="git checkout 81619e7 -- packages/x.py", effect="allow"),
    DecisionCase(input="git checkout main -- f g", effect="allow"),
    DecisionCase(input="git checkout $ref -- f", effect="deny"),
    DecisionCase(input="git checkout -b topic", effect="deny"),
    DecisionCase(
        input="set -e; git checkout 81619e7 -- x.py; git commit -m x",
        effect="allow",
    ),
    DecisionCase(input="git config core.pager=x", effect="ask"),
    # Read verbs pin git config to its query action; writes, scoped writes,
    # and opaque words keep the row's ask.
    DecisionCase(input="git config --get user.name", effect="allow"),
    DecisionCase(input="git config --get-regexp 'branch\\..*'", effect="allow"),
    DecisionCase(input="git config --list", effect="allow"),
    DecisionCase(input="git config -l", effect="allow"),
    DecisionCase(input="git config user.name me", effect="ask"),
    DecisionCase(input="git config --unset user.name", effect="ask"),
    DecisionCase(input="git config --global user.name me", effect="ask"),
    DecisionCase(input="git config --get $KEY", effect="ask"),
    # Global value flags are consumed, never read as the subcommand; globals
    # that change execution behavior or move git to another repository ask.
    # A redirect is judged before the subcommand word is even found, so it
    # cannot be answered by the row that would otherwise reason about this
    # worktree — `commit` is reversible here, where the reflog lives.
    DecisionCase(input="git -C /other status", effect="ask"),
    DecisionCase(input="git -C /tmp/other commit -am x", effect="ask"),
    DecisionCase(input="git -C /tmp/o merge --abort", effect="ask"),
    DecisionCase(input="git --git-dir=/tmp/x --work-tree=/tmp add .", effect="ask"),
    DecisionCase(input="git --namespace=other push", effect="ask"),
    DecisionCase(input="git --super-prefix=x/ status", effect="ask"),
    # Reading another tree keeps its way through, as two allowed segments.
    DecisionCase(input="cd /other && git status", effect="allow"),
    DecisionCase(input="git -C status restore f", effect="ask"),
    DecisionCase(input="git -c core.pager=touch log", effect="ask"),
    DecisionCase(input="git --exec-path=/tmp/x status", effect="ask"),
    # The pager is not gated: it moves nothing, these subcommands already run
    # it by default, and the program it names is reachable only through `-c`
    # and `git config`, which ask.
    DecisionCase(input="git --paginate diff", effect="allow"),
    DecisionCase(input="git --no-pager log", effect="allow"),
    # A configured diff driver stays allowed on purpose, the way `--paginate`
    # does: the flag names no program, only enables one already configured,
    # and that configuration is reachable only through `-c` and `git config`.
    # git also enables textconv by default for `diff` and `log`, so refusing
    # the flag would not stop a driver that already runs on the bare form.
    DecisionCase(input="git diff --ext-diff", effect="allow"),
    DecisionCase(input="git diff --textconv HEAD", effect="allow"),
    DecisionCase(input="git cat-file --textconv HEAD:f", effect="allow"),
    # `--output` is the guard, because it names a path and lands a file there.
    # It follows forwarding rather than only the verbs that document the flag:
    # `stash list`, `stash show` and `bisect view` reach it by handing their
    # arguments to `log` or `diff`. Bare, each still reports and allows.
    DecisionCase(input="git stash show --output=/tmp/f", effect="ask"),
    DecisionCase(input="git stash list --output=/tmp/f", effect="ask"),
    DecisionCase(input="git bisect view --output=/tmp/f", effect="ask"),
    DecisionCase(input="git shortlog --output=/tmp/f HEAD", effect="ask"),
    DecisionCase(input="git stash show", effect="allow"),
    DecisionCase(input="git stash list", effect="allow"),
    DecisionCase(input="git bisect view", effect="allow"),
    DecisionCase(input="git shortlog HEAD", effect="allow"),
    DecisionCase(input="git diff-tree --output=/tmp/f", effect="ask"),
    DecisionCase(input="git diff-index --output=/tmp/f", effect="ask"),
    DecisionCase(input="git diff-pairs --output=/tmp/f", effect="ask"),
    DecisionCase(input="git range-diff --output=/tmp/f a b", effect="ask"),
    DecisionCase(input="git diff-tree HEAD", effect="allow"),
    DecisionCase(input="git diff-files", effect="allow"),
    DecisionCase(input="git diff-index HEAD", effect="allow"),
    DecisionCase(input="git range-diff a...b", effect="allow"),
    DecisionCase(input="git diff-pairs", effect="allow"),
    # Exec-bearing and file-writing flags on allowed subcommands ask.
    DecisionCase(input="git rebase --exec 'touch x' HEAD~2", effect="ask"),
    DecisionCase(input="git fetch --upload-pack=/tmp/x origin", effect="ask"),
    DecisionCase(input="git grep -Ovim pattern", effect="ask"),
    DecisionCase(input="git log --output=/tmp/f", effect="ask"),
    DecisionCase(input="git reflog", effect="allow"),
    DecisionCase(input="git reflog expire --expire=now --all", effect="ask"),
    DecisionCase(input="git pull", effect="allow"),
    DecisionCase(input="git clone https://x.test/r.git", effect="ask"),
    DecisionCase(input="git restore f", effect="ask"),
    # The ref-sourced restore twin of the checkout pathspec form allows;
    # index-sourced and opaque forms keep the row's ask.
    DecisionCase(input="git restore --source=HEAD -- docs/rules.md", effect="allow"),
    DecisionCase(input="git restore --source=HEAD docs/rules.md", effect="allow"),
    DecisionCase(input="git restore --staged --source=HEAD f", effect="allow"),
    DecisionCase(input="git restore --source=$REF f", effect="ask"),
    DecisionCase(input="git restore --source=HEAD", effect="ask"),
    DecisionCase(input="git restore -s HEAD f", effect="ask"),
    # The query family, pinned so a later narrowing reads as a failing test
    # rather than as friction nobody can source. Each of these moves no ref,
    # touches no index entry, and writes nothing into the working tree.
    DecisionCase(input="git merge-tree main dev", effect="allow"),
    DecisionCase(input="git merge-tree --write-tree main dev", effect="allow"),
    DecisionCase(input="git hash-object -w README.md", effect="allow"),
    DecisionCase(input="git commit-tree -m x HEAD^{tree}", effect="allow"),
    DecisionCase(input="git mktree", effect="allow"),
    DecisionCase(input="git mktag", effect="allow"),
    DecisionCase(input="git write-tree", effect="allow"),
    DecisionCase(input="git patch-id", effect="allow"),
    DecisionCase(input="git show-index", effect="allow"),
    DecisionCase(input="git get-tar-commit-id", effect="allow"),
    DecisionCase(input="git fsck", effect="allow"),
    DecisionCase(input="git verify-pack -v x.idx", effect="allow"),
    DecisionCase(input="git check-ref-format --branch topic", effect="allow"),
    DecisionCase(input="git stripspace", effect="allow"),
    DecisionCase(input="git column", effect="allow"),
    DecisionCase(input="git diff-pairs", effect="allow"),
    DecisionCase(input="git fmt-merge-msg", effect="allow"),
    DecisionCase(input="git request-pull main https://x.test/r HEAD", effect="allow"),
    # And the near misses the criterion excludes, each for a different one of
    # its three clauses. A sweep that admitted any of these would have been a
    # sweep of the word rather than of what the word reaches.
    DecisionCase(input="git read-tree HEAD", effect="deny"),
    DecisionCase(input="git update-index --refresh", effect="deny"),
    DecisionCase(input="git update-ref refs/heads/x HEAD", effect="deny"),
    DecisionCase(input="git pack-refs --all", effect="deny"),
    DecisionCase(input="git format-patch HEAD~1", effect="deny"),
    DecisionCase(input="git unpack-file abc123", effect="deny"),
    DecisionCase(input="git difftool -y main", effect="deny"),
    DecisionCase(input="git gc --prune=now", effect="deny"),
    # symbolic-ref spells its write as a second operand rather than as a flag,
    # so the reading form is recognized and every writing form keeps the ask.
    DecisionCase(input="git symbolic-ref HEAD", effect="allow"),
    DecisionCase(input="git symbolic-ref --short HEAD", effect="allow"),
    DecisionCase(input="git symbolic-ref HEAD refs/heads/topic", effect="ask"),
    DecisionCase(input="git symbolic-ref --delete HEAD", effect="ask"),
    DecisionCase(input="git symbolic-ref --short $REF", effect="ask"),
    # A search that runs a program is not a read, however it is spelled.
    DecisionCase(input="rg -n needle src", effect="allow"),
    DecisionCase(input="rg --pre ./decrypt needle", effect="ask"),
    DecisionCase(input="rg --pre=./decrypt needle", effect="ask"),
    DecisionCase(input="rg --hostname-bin ./who needle", effect="ask"),
    DecisionCase(input="rg -z needle archive", effect="ask"),
    DecisionCase(input="find . -name '*.py' -fprint0 out", effect="ask"),
    # Filters that read and print, and the one flag on each that lands a file.
    DecisionCase(input="expr 1 + 2", effect="allow"),
    DecisionCase(input="numfmt --to=iec 1024", effect="allow"),
    DecisionCase(input="base64 payload.bin", effect="allow"),
    DecisionCase(input="base64 -o out.txt payload.bin", effect="ask"),
    DecisionCase(input="tree -L 2 src", effect="allow"),
    DecisionCase(input="tree -o listing.txt", effect="ask"),
    # Patch application allows in every in-repository form; only the flags that
    # write outside the working area are guarded.
    DecisionCase(input="git apply p.diff", effect="allow"),
    DecisionCase(input="git apply --cached p.diff", effect="allow"),
    DecisionCase(input="git apply --index p.diff", effect="allow"),
    DecisionCase(input="git apply --check p.diff", effect="allow"),
    DecisionCase(input="git apply -R p.diff", effect="allow"),
    DecisionCase(input="git apply --unsafe-paths p.diff", effect="ask"),
    DecisionCase(input="git apply --build-fake-ancestor=/tmp/x p.diff", effect="ask"),
    DecisionCase(input="git apply $PATCH", effect="deny"),
    DecisionCase(input="git switch main", effect="allow"),
    DecisionCase(input="git checkout main", effect="deny"),
    DecisionCase(input="git filter-branch --tree-filter x", effect="deny"),
    DecisionCase(input="sort --compress-program=/tmp/x f", effect="ask"),
    # gh: read-only operations allow; mutating forms ask.
    DecisionCase(input="gh run view 1", effect="allow"),
    DecisionCase(input="gh repo view", effect="allow"),
    DecisionCase(input="gh pr close 1", effect="ask"),
    DecisionCase(input="gh api -X POST /repos", effect="ask"),
    DecisionCase(input="gh issue create --title x", effect="ask"),
    DecisionCase(input="gh pr checkout 123", effect="allow"),
    # Authoring allows because the work is the author's own and the branch is
    # already pushed — both claims about this repository, and `--repo` is what
    # makes them someone else's. Reading elsewhere keeps its grant.
    DecisionCase(input="gh pr create --fill", effect="allow"),
    DecisionCase(input="gh pr create --repo other/victim --fill", effect="ask"),
    DecisionCase(input="gh pr create -R other/victim --fill", effect="ask"),
    DecisionCase(input="gh pr edit -R other/victim --title x", effect="ask"),
    DecisionCase(input="gh pr ready -R other/victim", effect="ask"),
    DecisionCase(input="gh pr list -R other/repo", effect="allow"),
    DecisionCase(input="gh pr view -R other/repo 1", effect="allow"),
    # The flag is not the only spelling of the redirect, so guarding it alone
    # would leave the same pull request one word away. `GH_` joins `GIT_` as a
    # prefix rather than a list of names, which fails closed on the next
    # variable gh learns to read.
    DecisionCase(input="GH_REPO=other/victim gh pr create --fill", effect="ask"),
    DecisionCase(input="env GH_REPO=other/victim gh pr create --fill", effect="ask"),
    DecisionCase(input="GH_HOST=evil.test gh pr create --fill", effect="ask"),
    DecisionCase(input="gh auth status", effect="allow"),
    DecisionCase(input="gh secret list", effect="deny"),
    # Adversarial hardening: no auto-allowed code execution or injection.
    DecisionCase(input="sudo cat /etc/shadow", effect="ask"),
    DecisionCase(input="LD_PRELOAD=./x.so ls", effect="ask"),
    DecisionCase(input="GIT_SSH_COMMAND=./x git fetch origin", effect="ask"),
    DecisionCase(input="git fetch ext::sh -c id", effect="ask"),
    DecisionCase(input="uv run --with evil pytest", effect="ask"),
    # Unknown words behind a literal blessed uv run target only reach that
    # target's argv; at or before the target they keep the opaque gate.
    DecisionCase(
        input='uv run lup-devtools dev pr update 22 --body "$(cat tmp/x.md)"',
        effect="allow",
    ),
    DecisionCase(
        input='uv run --python "$(cat v.txt)" lup-devtools dev check', effect="deny"
    ),
    DecisionCase(input='uv run "$(cat t.txt)" dev check', effect="deny"),
    DecisionCase(input="uv run ./pytest", effect="deny"),
    DecisionCase(input="uv run /tmp/tool --help", effect="deny"),
    DecisionCase(input="printf . | xargs find . -delete", effect="ask"),
    DecisionCase(input="find . -execdir sh -c id ;", effect="deny"),
    DecisionCase(input="sort f && python -c 'x'", effect="deny"),
    # The decision lattice: unjudged commands deny and bounce to the agent,
    # judged-risky rows ask, and a leading escalation marker promotes a deny
    # or ask to an approval question carrying the agent's stated reason.
    DecisionCase(input="cargo build", effect="deny"),
    DecisionCase(input="pip install requests", effect="deny"),
    # Credential-agent family: the pure listing form is the declared
    # read-only exception; every other form is a judged deny.
    DecisionCase(input="ssh-add -l", effect="allow"),
    DecisionCase(input="ssh-add -L", effect="allow"),
    DecisionCase(input="ssh-add", effect="deny"),
    DecisionCase(input="ssh-add -D", effect="deny"),
    DecisionCase(input="ssh-add -lD", effect="deny"),
    DecisionCase(input="ssh-add -l ~/.ssh/id_ed25519", effect="deny"),
    DecisionCase(input="ssh-add $flags", effect="deny"),
    DecisionCase(input="ssh-agent", effect="deny"),
    DecisionCase(input="ssh-agent -k", effect="deny"),
    # The compound join is deny > ask > defer > allow: an unjudged segment
    # is subsumed into a judged-risky segment's approval question (the full
    # command is visible at the prompt), while a judged deny dominates it.
    DecisionCase(input="frobnicate; ssh host", effect="ask"),
    DecisionCase(input="ssh host; frobnicate", effect="ask"),
    DecisionCase(input="pip install x; ssh host", effect="deny"),
    DecisionCase(input="ssh-add -D; ssh host", effect="deny"),
    # A build root is disposable by declaration, so emptying one is one act
    # whatever it holds — the same grant `tmp/` has. A production directory
    # keeps the ask, because nothing in the command bounds what is inside it.
    DecisionCase(input="rm -rf build", effect="allow"),
    DecisionCase(input="rm -rf src", effect="ask"),
    DecisionCase(input="make test", effect="ask"),
    DecisionCase(input="wget https://x.test/f", effect="ask"),
    # Docker: the read-only query surface is judged allow; every form that
    # can mutate containers, images, or the daemon keeps the judged ask.
    DecisionCase(input="docker ps", effect="allow"),
    DecisionCase(input="docker info", effect="allow"),
    DecisionCase(input="docker inspect abc123", effect="allow"),
    DecisionCase(input="docker container ls", effect="allow"),
    DecisionCase(input="docker system df", effect="allow"),
    DecisionCase(input="docker run nginx", effect="ask"),
    DecisionCase(input="docker exec -it abc sh", effect="ask"),
    DecisionCase(input="docker system prune", effect="ask"),
    DecisionCase(input="docker compose up", effect="ask"),
    DecisionCase(input="docker rm abc123", effect="ask"),
    DecisionCase(input="docker $verb ps", effect="ask"),
    DecisionCase(input="ps aux", effect="allow"),
    DecisionCase(input="zcat f.gz", effect="allow"),
    DecisionCase(input="# lup: escalate: build the crate\ncargo build", effect="ask"),
    DecisionCase(input="# lup: escalate:\ncargo build", effect="deny"),
    DecisionCase(input="# lup: escalate: routine\ngit status", effect="allow"),
    DecisionCase(
        input="# lup: escalate: clear caches\necho x | xargs rm -rf", effect="ask"
    ),
    # Structured constructs classify their embedded commands recursively:
    # conditionals, case arms, subshells, brace groups, negation, [[ ]],
    # arithmetic expansion, and heredoc bodies.
    DecisionCase(input="if grep -q x f; then echo y; fi", effect="allow"),
    DecisionCase(input="if grep -q x f; then rm y; else echo n; fi", effect="ask"),
    DecisionCase(
        input="if [ -f x ]; then cat x; elif [ -d x ]; then ls x; fi",
        effect="allow",
    ),
    DecisionCase(input="case $m in a) echo a;; *) echo d;; esac", effect="allow"),
    DecisionCase(input="case $m in a) rm f;; esac", effect="ask"),
    DecisionCase(input="case $m in a) echo a;;", effect="deny"),
    DecisionCase(input="(cd pkg && uv run pytest)", effect="allow"),
    DecisionCase(input="if true; then (cd x && rm -rf y); fi", effect="ask"),
    DecisionCase(input="case $m in (a) echo a;; esac", effect="allow"),
    DecisionCase(input="{ git status; git log; }", effect="allow"),
    DecisionCase(input="! grep -q x f", effect="allow"),
    DecisionCase(input="[[ -f x && -n $y ]]", effect="allow"),
    DecisionCase(input="foo() { cat x; }", effect="deny"),
    DecisionCase(input="echo $((1 + 2))", effect="allow"),
    DecisionCase(input="echo $(( $(id) ))", effect="deny"),
    DecisionCase(input="cat <<'EOF'\nliteral $(x)\nEOF", effect="allow"),
    DecisionCase(input="cat <<EOF\nplain body\nEOF", effect="allow"),
    DecisionCase(input="cat <<EOF\n$(id)\nEOF", effect="deny"),
    DecisionCase(input="grep x <<< 'needle haystack'", effect="allow"),
    # A heredoc is judged by what its target costs, not by its own shape: an
    # unrecoverable file asks in both operator orders, and one that is not
    # there yet overwrites nothing and passes. The body never decides —
    # `echo` authors the same content and always could.
    DecisionCase(
        input="cat > out.py <<'EOF'\nbody\nEOF", effect="ask", existing=["out.py"]
    ),
    DecisionCase(
        input="cat <<'EOF' > out.py\nbody\nEOF", effect="ask", existing=["out.py"]
    ),
    DecisionCase(
        input="cat > out.py <<'EOF'\nbody\nEOF",
        effect="ask",
        sandboxed=True,
        existing=["out.py"],
    ),
    DecisionCase(input="cat > fresh.py <<'EOF'\nbody\nEOF", effect="allow"),
    DecisionCase(input="cat > tmp/oneoff.py <<'EOF'\nbody\nEOF", effect="allow"),
    # Frozen variable bindings: assignments and read rebind for the segments
    # that follow; literal values instantiate references, opaque ones gate
    # guarded rows, and unresolved expansions deny toward explicit binding.
    DecisionCase(input="x=5", effect="allow"),
    DecisionCase(input="f=notes.txt; sort $f", effect="allow"),
    DecisionCase(input="f=-o; sort $f x", effect="ask"),
    DecisionCase(input="PATH=/tmp", effect="ask"),
    DecisionCase(input='while read -r line; do echo "$line"; done < f', effect="allow"),
    DecisionCase(input='while read -r line; do sort "$line"; done', effect="deny"),
    DecisionCase(input="sort $UNBOUND f", effect="deny"),
    DecisionCase(input="sort {-o,x} f", effect="deny"),
    # Value-consuming wrappers recurse to the wrapped command; repo-relative
    # tmp/ is a writable scratch target.
    DecisionCase(input="timeout 5 uv run pytest", effect="allow"),
    DecisionCase(input="nice -n 10 uv run pytest", effect="allow"),
    DecisionCase(input="timeout 5 rm -rf x", effect="ask"),
    DecisionCase(input="env", effect="allow"),
    DecisionCase(input="uv run pytest > tmp/out.txt", effect="allow"),
    # find -exec payloads recurse; the sed scanner reads the full stdout-only
    # grammar; curl is screened to read methods against the fetch scopes.
    DecisionCase(input="find src -name '*.py' -exec grep -l TODO {} +", effect="allow"),
    DecisionCase(input="find . -exec rm {} \\;", effect="ask"),
    DecisionCase(input="find . -ok cat {} \\;", effect="deny"),
    DecisionCase(input="sed 's|/old/path|/new/path|g' f", effect="allow"),
    DecisionCase(input="sed -n '/start/,/end/p' f", effect="allow"),
    DecisionCase(input="sed '/^#/!d' f", effect="allow"),
    DecisionCase(input="sed -n '1h;2,$H;${x;p}' f", effect="allow"),
    DecisionCase(input="sed '2a inserted text' f", effect="allow"),
    DecisionCase(input="sed --sandbox 's/a/b/w out' f", effect="allow"),
    DecisionCase(input="sed 'w out' f", effect="deny"),
    DecisionCase(input="curl -s https://example.com/api", effect="ask"),
    DecisionCase(input="curl -X POST https://example.com", effect="ask"),
    DecisionCase(input="curl -o f https://example.com", effect="deny"),
    # Establishing that a service came up is a read. The socket and process
    # listings report; `nc` reports only under -z, and the flags that hand a
    # socket to a program defeat that verb wherever it sits.
    DecisionCase(input="ss -tlnp", effect="allow"),
    DecisionCase(input="ss -K dst 1.2.3.4", effect="ask"),
    DecisionCase(input="lsof -i :8000", effect="allow"),
    DecisionCase(input="pgrep -f supervisor", effect="allow"),
    DecisionCase(input="nc -z localhost 8000", effect="allow"),
    DecisionCase(input="nc localhost 8000", effect="deny"),
    DecisionCase(input="nc -l -p 4444", effect="deny"),
    DecisionCase(input="nc -z -e /bin/sh host 22", effect="deny"),
    # Sandboxed executions: machinery bail-outs defer to the OS boundary,
    # judged decisions hold, and escalation still promotes to a question.
    DecisionCase(input="frobnicate --weird", effect="deny"),
    DecisionCase(input="frobnicate --weird", effect="defer", sandboxed=True),
    DecisionCase(input="sed --frob 's/a/b/' f", effect="defer", sandboxed=True),
    DecisionCase(input="sort $UNBOUND f", effect="defer", sandboxed=True),
    DecisionCase(input="foo() { cat x; }", effect="defer", sandboxed=True),
    DecisionCase(input="case $m in a) echo a;;", effect="defer", sandboxed=True),
    DecisionCase(
        input="git push --delete origin feat",
        effect="ask",
        sandboxed=True,
        escapable=True,
    ),
    DecisionCase(input="sed -i 's/a/b/' f", effect="deny", sandboxed=True),
    DecisionCase(input="ssh-add -D", effect="deny", sandboxed=True),
    DecisionCase(input="frobnicate; ssh host", effect="ask", sandboxed=True),
    DecisionCase(input="python -c 'x'", effect="deny", sandboxed=True),
    # An excluded command runs with no OS boundary beneath it, so unjudged
    # work in it has nothing to defer to and returns to the deny lattice —
    # including when it rides in beside a command the boundary would confine.
    DecisionCase(input="quuxify --weird", effect="deny", sandboxed=True),
    DecisionCase(input="frobnicate; quuxify now", effect="deny", sandboxed=True),
    DecisionCase(input="quuxifyer --weird", effect="defer", sandboxed=True),
    # The toolchain declares its escape, so nothing carries a flag to reach it:
    # unconfined it simply runs, and where a call can be placed it is placed.
    DecisionCase(input="uv run lup-devtools dev check", effect="allow"),
    DecisionCase(
        input="uv run lup-devtools dev check",
        effect="allow",
        sandboxed=True,
        escapable=True,
    ),
    # Confined with nowhere to put the call, the declaration cannot be honoured,
    # so it is stopped with the reason rather than run where it will die on the
    # first write. The same holds for anything else declared outside.
    DecisionCase(input="uv run lup-devtools dev check", effect="deny", sandboxed=True),
    DecisionCase(input="git push --delete origin feat", effect="deny", sandboxed=True),
    DecisionCase(input="uv run pytest tests/unit", effect="allow", sandboxed=True),
    # A help probe only prints usage, so it reads an unclassified command
    # without judging it. Bare -h counts alone; carrying a value it is an
    # ordinary argument (mysql -h host) and classifies normally.
    DecisionCase(input="frobnicate --help", effect="allow"),
    DecisionCase(input="codex plugin marketplace --help", effect="allow"),
    DecisionCase(input="git push --help", effect="allow"),
    DecisionCase(input="frobnicate -h", effect="allow"),
    DecisionCase(input="mysql -h db.example.com", effect="deny"),
    # A non-interactive host cannot put a question to a human: sandboxed, an
    # ask rides the OS boundary; unsandboxed it fails closed. A judged deny
    # is never rescued, and unjudged work defers exactly as it always did.
    DecisionCase(
        input="git push --delete origin feat", effect="deny", interactive=False
    ),
    DecisionCase(
        input="git push --delete origin feat",
        effect="defer",
        sandboxed=True,
        escapable=True,
        interactive=False,
    ),
    # A placement nothing can carry out outranks that boundary either way:
    # deferring means the OS confines the call, which is the one answer a
    # command declared outside cannot take.
    DecisionCase(
        input="git push --delete origin feat",
        effect="deny",
        sandboxed=True,
        interactive=False,
    ),
    DecisionCase(input="PYTHONPATH=src uv run pytest", effect="ask"),
    DecisionCase(
        input="PYTHONPATH=src uv run pytest",
        effect="defer",
        sandboxed=True,
        interactive=False,
    ),
    DecisionCase(
        input="sed -i 's/a/b/' f", effect="deny", sandboxed=True, interactive=False
    ),
    DecisionCase(
        input="frobnicate --weird", effect="defer", sandboxed=True, interactive=False
    ),
    # The classic sourcing bypasses stay outside the vocabulary: deny
    # unsandboxed, defer to the OS boundary inside it; a deferring segment
    # among allows keeps the batch deferred.
    DecisionCase(input="eval echo x", effect="deny"),
    DecisionCase(input="source setup.sh", effect="deny"),
    DecisionCase(input=". ./env.sh", effect="deny"),
    DecisionCase(input="eval echo x", effect="defer", sandboxed=True),
    DecisionCase(input="source setup.sh", effect="defer", sandboxed=True),
    DecisionCase(input="frobnicate; ls", effect="defer", sandboxed=True),
    DecisionCase(input="echo $(whoami)", effect="allow", sandboxed=True),
    DecisionCase(input="echo $(frobnicate)", effect="deny"),
    DecisionCase(input="echo $(frobnicate)", effect="defer", sandboxed=True),
    DecisionCase(input="git log $(cat names.txt)", effect="defer", sandboxed=True),
    DecisionCase(input="echo `id`", effect="deny", sandboxed=True),
    DecisionCase(
        input="# lup: escalate: unknown tool\nfrobnicate",
        effect="ask",
        sandboxed=True,
    ),
]

FETCH_POLICY_CASES = [
    DecisionCase(input="https://docs.example.com:8443/reference/api", effect="allow"),
    DecisionCase(
        input="https://docs.example.com:8443/reference/private/key", effect="deny"
    ),
    DecisionCase(input="http://docs.example.com:8443/reference/api", effect="ask"),
    DecisionCase(input="https://docs.example.com/reference/api", effect="ask"),
    DecisionCase(input="https://docs.example.com:8443/private", effect="ask"),
    DecisionCase(input="https://api.docs.example.com:8443/reference/api", effect="ask"),
    DecisionCase(input="https://cdn.example.org/asset.js", effect="allow"),
    DecisionCase(input="https://raw.cdn.example.org/asset.js", effect="allow"),
    DecisionCase(input="https://one.two.cdn.example.org/asset.js", effect="allow"),
    DecisionCase(input="https://evilcdn.example.org/asset.js", effect="ask"),
]

EDIT_POLICY_CASES = [
    EditDecisionCase(
        path="src/module.py",
        before="value = 1",
        after="value: Any = 1",
        effect="deny",
    ),
    EditDecisionCase(
        path="src/module.py",
        before="value = 1",
        after="value = 1  # type: ignore",
        effect="deny",
    ),
    EditDecisionCase(
        path="src/module.py",
        before="value = 1",
        after="value: dict[str, object] = {}",
        effect="deny",
    ),
    # A directive is judged by what it silences: one covering the violation
    # beside it is the reasoned exception a human weighs, and one covering
    # nothing is refused before anybody is asked to weigh it.
    EditDecisionCase(
        path="src/module.py",
        before="value = 1",
        after="value = 1\nother: Any = 2  # lup: ignore[any-type]",
        effect="ask",
    ),
    EditDecisionCase(
        path="src/module.py",
        before="value = 1",
        after="value = 1\nother = 2  # lup: ignore[any-type]",
        effect="deny",
    ),
    # And a directive covering one line does not carry the line beside it.
    EditDecisionCase(
        path="src/module.py",
        before="value = 1",
        after="value = 1\nfirst: Any = 2  # lup: ignore[any-type]\nsecond: Any = 3",
        effect="deny",
    ),
    EditDecisionCase(
        path="src/module.py", before="value = 1", after="value = 2", effect="allow"
    ),
    EditDecisionCase(
        path="src/module.py",
        before="value = 1",
        after="value = compute()  # may return Any when unset",
        effect="allow",
    ),
    EditDecisionCase(
        path=".claude/settings.json", before="{}", after='{"ok": true}', effect="ask"
    ),
    EditDecisionCase(
        path="src/module.py",
        before="value = 1",
        after="value = 1  # lup: revisit",
        effect="ask",
    ),
    EditDecisionCase(
        path=".claude/settings.json",
        before="{}",
        after='{"ok": true}',
        effect="allow",
        autonomous=True,
    ),
    EditDecisionCase(
        path="src/module.py",
        before="value = 1",
        after="from typing import Any",
        effect="deny",
        autonomous=True,
    ),
    # A scratch root gates nothing: with execution closed there is no longer a
    # path from authoring a file there to running it.
    EditDecisionCase(
        path="tmp/scratch.py",
        before="value = 1",
        after="value = 2",
        effect="allow",
        autonomous=True,
    ),
    # The conventions describe how production reads, so neither a scratch nor
    # a test file is judged against them.
    EditDecisionCase(
        path="tmp/probe.py",
        before="value: str",
        after="value: Any",
        effect="allow",
    ),
    EditDecisionCase(
        path="tests/unit/test_thing.py",
        before="value: str",
        after="value: Any",
        effect="allow",
    ),
    # The small-change gate is a reviewability convention, and it reaches
    # exactly as far as the conventions do: a fixture is written whole.
    EditDecisionCase(
        path="tests/unit/test_thing.py",
        before="value = 1",
        after="a = 1\nb = 2\nc = 3\nd = 4\ne = 5",
        effect="allow",
    ),
    # Creating a file is the same reach for the same reason. Adding lines to
    # a test freely while asking to create one is not a coherent boundary.
    EditDecisionCase(
        path="tests/unit/test_new.py",
        before=None,
        after="def test_thing() -> None:\n    assert True\n",
        effect="allow",
    ),
    EditDecisionCase(
        path="src/module.py",
        before=None,
        after="def thing() -> None:\n    pass\n",
        effect="ask",
    ),
    EditDecisionCase(
        path="src/module.py",
        before="value = 1",
        after="a = 1\nb = 2\nc = 3\nd = 4\ne = 5",
        effect="defer",
    ),
    EditDecisionCase(
        path="src/module.py",
        before="value: str",
        after="value: Any",
        effect="deny",
    ),
    EditDecisionCase(
        path="README.md",
        before="# Lup\n",
        after="# Lup\n\nAn agent-added paragraph.\n",
        effect="ask",
    ),
    EditDecisionCase(
        path="README.md",
        before="# Lup\n",
        after="# Lup, retitled\n",
        effect="ask",
        autonomous=True,
    ),
    EditDecisionCase(
        path="src/module.py",
        before="value = 1",
        after="value = 1  # lup: revisit",
        effect="ask",
        autonomous=True,
    ),
    EditDecisionCase(
        path="README.md",
        before="# Lup\n",
        after="# Lup, renamed\n",
        effect="ask",
    ),
    EditDecisionCase(
        path="sync.json",
        before='{"projects": []}',
        after='{"projects": [{"name": "fleet-app"}]}',
        effect="ask",
    ),
    EditDecisionCase(
        path="downstream.json",
        before='{"projects": []}',
        after='{"projects": [{"name": "fleet-app"}]}',
        effect="ask",
        path_exists=False,
    ),
    EditDecisionCase(
        path="sync.json",
        before='{"projects": []}',
        after='{"projects": [{"name": "fleet-app"}]}',
        effect="allow",
        autonomous=True,
    ),
    EditDecisionCase(
        path="src/new.py",
        before=None,
        after="value = 1",
        effect="ask",
        path_exists=False,
    ),
    EditDecisionCase(
        path="src/module.py", before="value = 1", after=None, effect="allow"
    ),
    EditDecisionCase(
        path="src/module.py",
        before="value = 1",
        after="value = 1\nalpha = 2\nbeta = 3\ngamma = 4\ndelta = 5",
        effect="defer",
    ),
    EditDecisionCase(
        path="src/module.py",
        before="value = 1",
        after="value = 1\n\n# note one\n\n# note two\n\n# note three\n\n# four",
        effect="allow",
    ),
    EditDecisionCase(
        path="src/module.py",
        before="import x",
        after=(
            "import x\n\n\nclass Config(BaseModel):\n    name: str\n"
            "    size: int\n    tags: list[str]\n    active: bool"
        ),
        effect="allow",
    ),
]


def test_policy_bundle_contains_assembly_but_no_decision_implementation() -> None:
    source = Path("packages/lup/src/lup/policy/bundle.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]

    assert "BUNDLED_POLICY_SOURCE" not in source
    assert all(not name.startswith("decide_") for name in functions)


def test_the_neutral_kernel_never_learns_one_runtime_sandbox_spelling() -> None:
    """The kernel carries the axis; only an adapter carries a vendor's word for it.

    Codex matches on this same kernel and has no per-call sandbox at all, so a
    spelling that leaked in here would be one runtime's argument name sitting
    in the shared verdict every other runtime reads.
    """
    kernel = [item.source for item in policy_kernel_modules()]

    assert not [item for item in kernel if "dangerouslyDisableSandbox" in item]
    assert [item for item in kernel if "SandboxPlacement" in item]


def write_kernel_package(runtime: Path) -> Path:
    """Materialize the kernel package a generated runtime directory carries."""
    package = runtime / "kernel"
    package.mkdir(parents=True, exist_ok=True)
    for item in policy_kernel_modules():
        (package / item.name).write_text(item.source, encoding="utf-8")
    return package


def load_bundled_kernel(root: Path, module: str) -> ModuleType:
    """Import one module of a freshly materialized kernel copy.

    The copy is imported as a real package, so its relative imports resolve
    exactly as they do beneath a generated plugin's runtime directory rather
    than through the lup installation under test.
    """
    write_kernel_package(root)
    for name in [
        name for name in sys.modules if name == "kernel" or name.startswith("kernel.")
    ]:
        del sys.modules[name]
    sys.path.insert(0, str(root))
    try:
        return importlib.import_module(f"kernel.{module}")
    finally:
        sys.path.remove(str(root))


def assembled_edit_decision(
    module: ModuleType,
    path: str,
    before: str | None,
    after: str | None,
    protected_roots: list[str],
    human_owned_files: list[str],
    *,
    autonomous: bool = False,
) -> KernelDecision:
    """Invoke an isolated kernel with the same generated primitive rows."""
    suffix = Path(path).suffix.lower()
    rows_by_suffix = bundled_antipattern_rows()
    rows = rows_by_suffix[suffix] if suffix in rows_by_suffix else []
    return module.decide_edit(
        path,
        before,
        after,
        path_exists=Path(path).exists(),
        path_rules=runtime_path_rules(protected_roots, human_owned_files),
        antipattern_rows=rows,
        path_roles=FIXTURE_PATH_ROLES,
        autonomous=autonomous,
        python_source=suffix in (".py", ".pyi"),
    )


def test_assembled_kernel_runs_without_site_packages(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    write_kernel_package(runtime)
    (runtime / "policy_data.py").write_text(
        render_policy_data(
            allowed_fetch_scopes=[
                runtime_url_scope("https://docs.example.com:8443", "/reference/"),
                runtime_url_scope(
                    "https://cdn.example.org", "/", include_subdomains=True
                ),
            ],
            denied_fetch_scopes=[
                runtime_url_scope(
                    "https://docs.example.com:8443",
                    "/reference/private/",
                    "sensitive documentation path",
                )
            ],
            protected_roots=[
                ".claude",
                "pyproject.toml",
                "sync.json",
                "downstream.json",
            ],
            human_owned_files=["README.md"],
            autonomous_agent_identities=["resolver-worker"],
            path_roles=FIXTURE_PATH_ROLES,
            shell_rules=SHELL_RULES,
            refused_tools=FIXTURE_REFUSED_TOOLS,
            recoverable_target_limit=FIXTURE_RECOVERABLE_LIMIT,
            runner_targets=FIXTURE_RUNNER_TARGETS,
            sandbox_excluded_commands=FIXTURE_EXCLUDED_COMMANDS,
            auto_escape_prefixes=[],
            diagnostics_command=[],
        ),
        encoding="utf-8",
    )
    fixtures = runtime / "fixtures.json"
    fixtures.write_text(
        json.dumps(
            {
                "shell": [item.model_dump() for item in SHELL_POLICY_CASES],
                "fetch": [item.model_dump() for item in FETCH_POLICY_CASES],
                "edit": [item.model_dump() for item in EDIT_POLICY_CASES],
            }
        ),
        encoding="utf-8",
    )
    probe = runtime / "probe.py"
    probe.write_text(
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parent))\n"
        "from kernel.edit import decide_edit\n"
        "from kernel.fetch import decide_fetch\n"
        "from kernel.shell import decide_shell\n"
        "from policy_data import (\n"
        "    ALLOWED_FETCH_SCOPES, ANTI_PATTERN_ROWS, DENIED_FETCH_SCOPES,\n"
        "    MAXIMUM_ADDED_LINES, PATH_ROLES, PATH_RULES, RUNNER_TARGETS,\n"
        "    SANDBOX_EXCLUDED_COMMANDS, SHELL_RULES,\n"
        ")\n"
        "fixtures = json.loads(\n"
        "    (Path(__file__).parent / 'fixtures.json').read_text(encoding='utf-8')\n"
        ")\n"
        "for case in fixtures['shell']:\n"
        "    result = decide_shell(\n"
        "        case['input'], SHELL_RULES, sandboxed=case['sandboxed'],\n"
        "        excluded_commands=SANDBOX_EXCLUDED_COMMANDS,\n"
        "        escapable=case['escapable'],\n"
        "        interactive=case['interactive'],\n"
        "        path_roles=PATH_ROLES,\n"
        "        path_rules=PATH_RULES,\n"
        "        existing_targets=case['existing'],\n"
        "        runner_targets=RUNNER_TARGETS,\n"
        "    )\n"
        "    assert result.effect == case['effect'], case\n"
        "for case in fixtures['fetch']:\n"
        "    decision = decide_fetch(\n"
        "        case['input'], ALLOWED_FETCH_SCOPES, DENIED_FETCH_SCOPES\n"
        "    )\n"
        "    assert decision.effect == case['effect'], case\n"
        "for case in fixtures['edit']:\n"
        "    suffix = Path(case['path']).suffix.lower()\n"
        "    rows = ANTI_PATTERN_ROWS[suffix] if suffix in ANTI_PATTERN_ROWS else ()\n"
        "    decision = decide_edit(\n"
        "        case['path'], case['before'], case['after'],\n"
        "        path_exists=case['path_exists'], path_rules=PATH_RULES,\n"
        "        antipattern_rows=rows, path_roles=PATH_ROLES,\n"
        "        maximum_added_lines=MAXIMUM_ADDED_LINES,\n"
        "        autonomous=case['autonomous'],\n"
        "        python_source=suffix in ('.py', '.pyi'),\n"
        "    )\n"
        "    assert decision.effect == case['effect'], case\n",
        encoding="utf-8",
    )

    sh.Command("python3")("-I", "-S", str(probe), _truncate_exc=False)


def test_equivalent_multi_file_native_edits_decode_identically() -> None:
    changes = [
        EditChange(path=Path("a.py"), before="old", after="new"),
        EditChange(path=Path("b.py"), before="left", after="right"),
    ]
    claude = ClaudeEventDecoder().decode(
        ClaudeBeforeToolEvent(operation=ClaudeEditBatchOperation(changes=changes))
    )
    codex = CodexEventDecoder().decode(
        CodexBeforeToolEvent(
            operation=CodexFileChangeOperation(
                changes=[
                    CodexFileChange(
                        path=change.path,
                        before=change.before,
                        after=change.after,
                    )
                    for change in changes
                ]
            )
        )
    )

    assert claude.tool == codex.tool == EditBatch(changes=changes)


def test_unknown_tools_remain_auditable_and_ask() -> None:
    claude = ClaudeEventDecoder().decode(
        ClaudeBeforeToolEvent(operation=ClaudeUnknownOperation(name="Novel", input={}))
    )
    codex = CodexEventDecoder().decode(
        CodexBeforeToolEvent(operation=CodexUnknownOperation(name="Novel", input={}))
    )

    assert isinstance(claude.tool, UnknownTool)
    assert isinstance(codex.tool, UnknownTool)
    assert UnknownToolPolicy().decide(claude.tool).effect == "ask"
    assert UnknownToolPolicy().decide(codex.tool).effect == "ask"


def refused_tool_call(name: str, payload: JsonObject) -> UnknownTool:
    """One unclassified native call, as a decoder hands it to the policy."""
    return UnknownTool(identity=ToolIdentity(original_name=name), input=payload)


REFUSAL_CASES = [
    ("Quuxify", {"content": "a page"}, "deny"),
    ("Skill", {"skill": "quux-design"}, "deny"),
    ("Skill", {"skill": "commit"}, "defer"),
    ("Novel", {"skill": "quux-design"}, "ask"),
    ("Quuxify", {"body": "# lup: escalate: the user asked for a page\nbody"}, "ask"),
    ("Quuxify", {"body": "# lup: escalate:\nbody"}, "deny"),
]
"""What a declared refusal answers, across every shape it has to tell apart.

The whole rule reads off the rows: a refused tool denies, a refused subject
of a tool denies, another subject of that same tool passes to the runtime
rather than stopping at a human, a tool nobody mentioned stays unclassified,
a stated escalation becomes the question it asked for, and one stating
nothing does not.
"""


@pytest.mark.parametrize(("name", "payload", "effect"), REFUSAL_CASES)
def test_declared_tool_refusals_decide_identically(
    name: str, payload: JsonObject, effect: str, tmp_path: Path
) -> None:
    policy = UnknownToolPolicy(FIXTURE_REFUSED_TOOLS)
    module = load_bundled_kernel(tmp_path, "tools")
    bundled = module.decide_tool(
        name,
        [value for value in payload.values() if isinstance(value, str)],
        erase_refused_tools(FIXTURE_REFUSED_TOOLS),
    )

    assert policy.decide(refused_tool_call(name, payload)).effect == effect
    assert (bundled.effect if bundled is not None else "ask") == effect


def test_a_tool_refusal_names_what_to_reach_for_instead() -> None:
    policy = UnknownToolPolicy(FIXTURE_REFUSED_TOOLS)

    decision = policy.decide(refused_tool_call("Quuxify", {"content": "a page"}))

    assert "quuxifying leaves the repository" in decision.reason
    assert "lup: escalate:" in decision.reason


def test_malformed_native_fetch_urls_become_conservative_unknown_tools() -> None:
    from lup.adapters.claude.native import ClaudeFetchOperation
    from lup.adapters.codex.native import CodexFetchOperation

    claude = ClaudeEventDecoder().decode(
        ClaudeBeforeToolEvent(operation=ClaudeFetchOperation(url="not a url"))
    )
    codex = CodexEventDecoder().decode(
        CodexBeforeToolEvent(operation=CodexFetchOperation(url="not a url"))
    )

    assert isinstance(claude.tool, UnknownTool)
    assert isinstance(codex.tool, UnknownTool)


def test_native_decision_renderers_preserve_or_fail_closed_on_ask() -> None:
    decision = Decision(effect="ask", reason="approval required")

    claude = ClaudeDecisionRenderer().render(decision)
    codex = CodexDecisionRenderer(supports_ask=False).render(decision)

    assert claude.permission_decision == "ask"
    assert codex.exit_code == 2
    assert codex.approximation == "ask rendered as fail-closed denial"
    with pytest.raises(ValueError, match="not been evidenced"):
        CodexDecisionRenderer(supports_ask=True)


def test_the_decision_effect_stays_closed_at_four_members() -> None:
    """Escaping the sandbox is the other axis, never a fifth effect.

    A member per placement would need an ask-plus-escape next and then a
    deny-plus-escape, so the two questions stay two fields.
    """
    assert sorted(get_args(DecisionEffect.__value__)) == [
        "allow",
        "ask",
        "defer",
        "deny",
    ]
    assert sorted(get_args(SandboxPlacement.__value__)) == [
        "ambient",
        "escalable",
        "inside",
        "outside",
    ]
    assert Decision(effect="allow", sandbox="outside").effect == "allow"


def test_the_settled_sandbox_composition_rows_render_as_decided() -> None:
    """Every settled pair a rewrite renders, on a runtime that can place a call.

    The placement is an argument of the call on Claude Code, so an allow that
    escapes is an allow plus a rewrite; an ask that escapes is asking two
    things at once, so the reason says both. The two escalable pairs answer on
    a second channel as well, and are pinned where that offer is.
    """
    shell: JsonObject = {"command": "git ls-remote origin HEAD"}
    render = ClaudeDecisionRenderer().render

    asked = render(
        Decision(effect="ask", reason="needs a human", sandbox="outside"), shell
    )
    denied = render(Decision(effect="deny", reason="refused", sandbox="outside"), shell)
    confined = render(Decision(effect="allow", sandbox="inside"), shell)
    ambient = render(Decision(effect="allow", sandbox="ambient"), shell)
    escaped = render(Decision(effect="allow", sandbox="outside"), shell)

    assert asked.permission_decision == "ask"
    assert asked.reason == "needs a human — this will run outside the sandbox"
    assert asked.updated_input == {**shell, "dangerouslyDisableSandbox": True}
    assert (denied.permission_decision, denied.updated_input) == ("deny", None)
    assert confined.updated_input == {**shell, "dangerouslyDisableSandbox": False}
    assert (ambient.permission_decision, ambient.updated_input) == ("allow", None)
    assert escaped.updated_input == {**shell, "dangerouslyDisableSandbox": True}


def test_a_deny_short_circuits_whatever_the_sandbox_says() -> None:
    """Where a call would have run cannot soften a refusal.

    Held at construction rather than checked at each renderer, so no boundary
    can read a placement off a verdict that reached none.
    """
    assert KernelDecision("deny", "refused", "outside").sandbox == "ambient"
    assert Decision(effect="deny", reason="refused", sandbox="outside").sandbox == (
        "ambient"
    )
    assert KernelDecision("defer", "unjudged", "outside").sandbox == "ambient"

    refused = KernelDecision("deny", "refused", "escalable")
    placed = refused.placed(escapable=True, agent_escalates=True)
    assert (refused.sandbox, placed.reason) == ("ambient", "refused")


def test_a_runtime_that_cannot_place_a_call_renders_the_plain_effect() -> None:
    """A Codex verdict rewrites nothing, so an intent it cannot perform is dropped.

    Degrading in silence is the failure this pins: an escape rendered into a
    channel that ignores it reads as honoured to everything upstream, and the
    call runs confined with nobody told.
    """
    escaped = Decision(effect="allow", reason="fine", sandbox="outside")
    asked = Decision(effect="ask", reason="needs a human", sandbox="outside")

    codex = CodexDecisionRenderer(supports_ask=False)

    assert codex.render(escaped).exit_code == 0
    assert "outside the sandbox" not in codex.render(asked).stderr
    assert escaped.placed(escapable=False, agent_escalates=True) == Decision(
        effect="allow", reason="fine"
    )


def test_a_permission_to_escalate_turns_on_the_agent_and_not_on_the_channel() -> None:
    """The offer is addressed to the agent, so it is the agent that decides it.

    Which is why it survives on a runtime whose verdicts place nothing: the
    middle case here is Codex, where a hook rewrites no call — so no placement
    reaches the wire — while the agent still has words for taking its own call
    out, and the reason carries them. Reading that case off the placement
    channel is what would drop an offer the agent could have spent.

    Where the agent has no way out the call runs confined, and says so as
    ``inside`` rather than as the session-deferring placement: withdrawing an
    offer is not the same act as handing the question back to the session,
    and on an unconfined session the two differ by the whole sandbox. The
    reason says the offer is not available, because an agent that spends a
    turn discovering that learns nothing it can act on.
    """
    offered = Decision(effect="allow", reason="fine", sandbox="escalable")

    placed = offered.placed(escapable=True, agent_escalates=True)
    carried = offered.placed(escapable=False, agent_escalates=True)
    degraded = offered.placed(escapable=True, agent_escalates=False)

    assert (placed.sandbox, placed.reason) == (
        "escalable",
        "fine" + SANDBOX_ESCALATION_OFFER,
    )
    assert (carried.sandbox, carried.reason) == (
        "ambient",
        "fine" + SANDBOX_ESCALATION_OFFER,
    )
    assert (degraded.sandbox, degraded.effect) == ("inside", "allow")
    assert degraded.reason == "fine" + SANDBOX_ESCALATION_UNSUPPORTED
    assert CodexDecisionRenderer(supports_ask=False).render(offered).exit_code == 0


def test_only_an_escalable_placement_reads_what_the_call_already_asked() -> None:
    """Which placements leave is one answer, so two renderers cannot differ.

    ``outside`` leaves and ``inside`` stays whatever the call said, because
    those are the verdict's to decide. Only ``escalable`` reads the second
    argument, which is what makes it a permission the agent spends rather
    than a placement done to it.
    """
    for spent in (True, False):
        assert sandbox_escaped("outside", spent) is True
        assert sandbox_escaped("inside", spent) is False
        assert sandbox_escaped("escalable", spent) is spent


def test_fetch_policy_normalizes_origin_and_rejects_lookalikes() -> None:
    policy = FetchPolicy(
        allowed=[
            UrlScope(
                origin=AnyHttpUrl("https://docs.example.com"),
                path_prefix="/reference/",
            )
        ],
        denied=[],
    )

    assert (
        policy.decide(
            FetchUrl(url=AnyHttpUrl("https://docs.example.com/reference/api?q=1"))
        ).effect
        == "allow"
    )
    assert (
        policy.decide(
            FetchUrl(url=AnyHttpUrl("https://docs.example.com.evil.test/reference/api"))
        ).effect
        == "ask"
    )


def test_bundled_fetch_matches_canonical_scheme_port_and_path(tmp_path: Path) -> None:
    bundled = load_bundled_kernel(tmp_path, "fetch")
    scope = UrlScope(
        origin=AnyHttpUrl("https://docs.example.com:8443"),
        path_prefix="/reference/",
    )
    denied_scope = UrlScope(
        origin=AnyHttpUrl("https://docs.example.com:8443"),
        path_prefix="/reference/private/",
        reason="sensitive documentation path",
    )
    subdomain_scope = UrlScope(
        origin=AnyHttpUrl("https://cdn.example.org"), include_subdomains=True
    )
    policy = FetchPolicy(allowed=[scope, subdomain_scope], denied=[denied_scope])
    wire_scope = [
        runtime_url_scope(str(scope.origin), scope.path_prefix),
        runtime_url_scope(
            str(subdomain_scope.origin),
            subdomain_scope.path_prefix,
            include_subdomains=True,
        ),
    ]
    denied_wire_scope = [
        runtime_url_scope(
            str(denied_scope.origin), denied_scope.path_prefix, denied_scope.reason
        )
    ]

    for case in FETCH_POLICY_CASES:
        canonical = policy.decide(FetchUrl(url=AnyHttpUrl(case.input)))
        generated = bundled.decide_fetch(case.input, wire_scope, denied_wire_scope)
        assert canonical.effect == generated.effect == case.effect


def test_curl_screen_consults_the_declared_fetch_scopes() -> None:
    policy = ShellPolicy(
        SHELL_RULES,
        allowed_urls=[UrlScope(origin=AnyHttpUrl("https://docs.example.com"))],
        denied_urls=[UrlScope(origin=AnyHttpUrl("https://internal.example.com"))],
    )

    def effect(command: str) -> str:
        return policy.decide(ShellCommand(command=command)).effect

    assert effect("curl -s https://docs.example.com/api/one") == "allow"
    # A cluster is one word to the shell and to curl, so it is judged as the
    # flags it spells rather than as an option nobody declared.
    assert effect("curl -sI https://docs.example.com/") == "allow"
    assert effect("curl -s -I https://docs.example.com/") == "allow"
    assert effect("curl -sSf https://docs.example.com/") == "allow"
    # Only the declared reporting letters cluster. One that follows redirects
    # or carries a body reaches past the scopes, so it stays unclassified
    # wherever it is spelled.
    assert effect("curl -fsSL https://docs.example.com/") == "deny"
    assert effect("curl -sd a=b https://docs.example.com/api") == "deny"
    assert effect("curl -s https://internal.example.com/x") == "deny"
    assert effect("curl -s https://elsewhere.example.com/") == "ask"
    assert effect("curl -X DELETE https://docs.example.com/api") == "ask"
    assert effect("curl -d a=b https://docs.example.com/api") == "deny"


def test_a_schemeless_curl_url_is_judged_the_way_curl_resolves_it() -> None:
    """curl guesses HTTP for a bare host, and so does the screen judging it.

    `curl localhost:8000/health` is how a liveness probe is typed, and
    reading it as a malformed URL put an approval question on the one form
    an agent reaches for while the fully spelled twin was already declared
    safe. Guessing where curl guesses keeps the verdict conservative: the
    guess is HTTP, so an origin declared for TLS alone is not covered by it.
    """
    policy = ShellPolicy(
        SHELL_RULES,
        allowed_urls=[
            UrlScope(origin=AnyHttpUrl("http://localhost"), any_port=True),
            UrlScope(origin=AnyHttpUrl("https://docs.example.com")),
        ],
    )

    def effect(command: str) -> str:
        return policy.decide(ShellCommand(command=command)).effect

    assert effect("curl -s localhost:8000/health") == "allow"
    assert effect("curl -s http://localhost:8000/health") == "allow"
    # The guess is HTTP, so a scope that only ever declared HTTPS is unmatched
    # and the bare spelling asks rather than inheriting a grant.
    assert effect("curl -s docs.example.com/api") == "ask"
    assert effect("curl -s https://docs.example.com/api") == "allow"


def test_a_scope_may_cover_every_port_on_one_host() -> None:
    """A local service is the same service at whatever port it was started on.

    Every surface this repository serves takes `--port`, so a scope pinned to
    one number puts the question back the first time somebody moves it —
    while the reason loopback is grantable at all, that nothing off this
    machine can reach it, holds at every port equally.
    """
    policy = FetchPolicy(
        [
            UrlScope(origin=AnyHttpUrl("http://127.0.0.1"), any_port=True),
            UrlScope(origin=AnyHttpUrl("https://pinned.example.com:8443")),
        ],
        [],
    )

    def effect(url: str) -> str:
        return policy.decide(FetchUrl(url=AnyHttpUrl(url))).effect

    assert effect("http://127.0.0.1:8765/api/runs") == "allow"
    assert effect("http://127.0.0.1:9999/") == "allow"
    assert effect("http://127.0.0.1/") == "allow"
    assert effect("https://pinned.example.com:8443/x") == "allow"
    assert effect("https://pinned.example.com:9000/x") == "ask"


def committed_tree(root: Path, *names: str) -> None:
    """A repository whose every named file is tracked with nothing pending.

    The recoverable grant is the host's answer to a Git question, so a case
    about it needs a real repository rather than a stub: what is being tested
    is that `ls-files` and `status --porcelain` agree a path costs a checkout.
    """
    sh.Command("git")("init", "-q", str(root))
    for name in names:
        (root / name).write_text("body\n", encoding="utf-8")
    git = sh.Command("git").bake(
        "-C", str(root), "-c", "user.email=t@e", "-c", "user.name=t"
    )
    git("add", "-A")
    git("commit", "-qm", "in")


def test_a_recoverable_grant_never_covers_a_protected_path(tmp_path: Path) -> None:
    """Git restoring a file says nothing about who is allowed to replace it.

    The grant answers what destroying a path costs, which is the wrong
    question for one protected by ownership: a clean tracked `README.md` is
    exactly as restorable as any other file, and exactly as off-limits. The
    two gates read one table so they cannot come to differ about a path.
    """
    committed_tree(tmp_path, "README.md", "notes.md")
    policy = ShellPolicy(
        SHELL_RULES,
        path_rules=[human_owned_path_rule("README.md")],
        runner_targets=FIXTURE_RUNNER_TARGETS,
    )

    def effect(command: str) -> str:
        return policy.decide(ShellCommand(command=command, cwd=tmp_path)).effect

    assert effect("rm notes.md") == "allow"
    assert effect("rm README.md") == "ask"
    assert effect("cp notes.md README.md") == "ask"
    # Restoring one grants on the same host fact, so it defers to the same
    # table: a clean README.md is exactly as restorable and exactly as owned.
    assert effect("git restore notes.md") == "allow"
    assert effect("git restore README.md") == "ask"


def test_restoring_a_file_that_holds_no_pending_work_changes_nothing(
    tmp_path: Path,
) -> None:
    """The restore row asks about discarded work, so it should not ask when there is none.

    Whether `git restore <path>` costs anything is the same question `rm
    <path>` poses and the same host answer settles it: a tracked path with no
    uncommitted change has nothing the index does not already hold, so the
    restore writes back the bytes on disk. Pending work restores the ask,
    which is the only case the row was ever about.
    """
    committed_tree(tmp_path, "notes.md", "other.md")
    policy = ShellPolicy(SHELL_RULES, runner_targets=FIXTURE_RUNNER_TARGETS)

    def effect(command: str) -> str:
        return policy.decide(ShellCommand(command=command, cwd=tmp_path)).effect

    assert effect("git restore notes.md") == "allow"
    assert effect("git restore --staged notes.md") == "allow"
    assert effect("git restore notes.md other.md") == "allow"
    # Deleting and restoring the same clean path agree, because one host fact
    # answers both.
    assert effect("rm notes.md") == "allow"
    # Uncommitted work, an untracked path, and a directory each keep the ask:
    # the first would be discarded, and the host vouches for neither of the
    # others.
    (tmp_path / "notes.md").write_text("uncommitted\n", encoding="utf-8")
    assert effect("git restore notes.md") == "ask"
    assert effect("git restore notes.md other.md") == "ask"
    assert effect("git restore untracked.md") == "ask"
    assert effect("git restore .") == "ask"


def test_moving_a_recoverable_file_costs_what_deleting_it_costs(
    tmp_path: Path,
) -> None:
    """A move is a delete and a create, and neither half is worth a question.

    The verb wrote both operands, so a destination that did not exist yet
    failed the recoverable test and took the whole command to an ask — which
    left `mv` asking about a file `rm` would have removed without one, for
    the sake of a path that holds nothing.
    """
    committed_tree(tmp_path, "notes.md", "other.md")
    policy = ShellPolicy(SHELL_RULES, runner_targets=FIXTURE_RUNNER_TARGETS)

    def effect(command: str) -> str:
        return policy.decide(ShellCommand(command=command, cwd=tmp_path)).effect

    assert effect("rm notes.md") == "allow"
    assert effect("mv notes.md renamed.md") == "allow"
    assert effect("cp notes.md copy.md") == "allow"
    # Replacing a tracked, clean file still costs only a checkout; replacing
    # one the host cannot vouch for costs whatever was in it.
    assert effect("mv notes.md other.md") == "allow"
    (tmp_path / "dirty.md").write_text("uncommitted\n", encoding="utf-8")
    assert effect("mv notes.md dirty.md") == "ask"
    # Content leaving a scratch root enters production without the edit gate
    # ever having read it, so an empty destination is not the whole question.
    scratch = ShellPolicy(
        SHELL_RULES,
        path_roles=FIXTURE_PATH_ROLES,
        runner_targets=FIXTURE_RUNNER_TARGETS,
    )
    (tmp_path / "tmp").mkdir()
    (tmp_path / "tmp" / "draft.md").write_text("draft\n", encoding="utf-8")
    assert (
        scratch.decide(
            ShellCommand(command="mv tmp/draft.md arrived.md", cwd=tmp_path)
        ).effect
        == "ask"
    )


def test_redirecting_over_a_file_costs_what_deleting_it_costs(
    tmp_path: Path,
) -> None:
    """The three writing forms answer one question about the same path.

    A redirection's target was resolved for existence but never for
    recoverability, so `rm notes.md` and `cp x notes.md` were granted while
    `echo x > notes.md` asked about the identical clean, tracked file. The
    heredoc form carried a further deny, justified by an edit gate a
    redirection into a *new* file already bypasses — so it drew the line
    where the cost was lowest rather than where the risk was.
    """
    committed_tree(tmp_path, "notes.md")
    policy = ShellPolicy(
        SHELL_RULES,
        path_rules=[human_owned_path_rule("README.md")],
        runner_targets=FIXTURE_RUNNER_TARGETS,
    )

    def effect(command: str) -> str:
        return policy.decide(ShellCommand(command=command, cwd=tmp_path)).effect

    # Every form writing a tracked, clean file costs one checkout.
    assert effect("rm notes.md") == "allow"
    assert effect("echo x > notes.md") == "allow"
    assert effect("echo x >> notes.md") == "allow"
    assert effect("cat > notes.md <<'EOF'\nbody\nEOF") == "allow"
    # A file the host cannot vouch for keeps its ask, whatever the shape.
    (tmp_path / "dirty.md").write_text("uncommitted\n", encoding="utf-8")
    assert effect("echo x > dirty.md") == "ask"
    assert effect("cat > dirty.md <<'EOF'\nbody\nEOF") == "ask"
    # Ownership is a different question from cost, and still answers first.
    (tmp_path / "README.md").write_text("human\n", encoding="utf-8")
    assert effect("echo x > README.md") == "ask"


def test_shell_policy_checks_every_segment_and_deny_wins() -> None:
    policy = ShellPolicy(SHELL_RULES, runner_targets=FIXTURE_RUNNER_TARGETS)

    assert policy.decide(
        ShellCommand(command="git status && uv run pytest")
    ).effect == ("allow")
    assert (
        policy.decide(
            ShellCommand(command="uv add package && python -c 'print(1)'")
        ).effect
        == "deny"
    )
    assert policy.decide(ShellCommand(command="echo $(dangerous)")).effect == "deny"


def test_shell_policy_allows_control_flow_and_still_refuses_what_outlives_it() -> None:
    policy = ShellPolicy(SHELL_RULES, runner_targets=FIXTURE_RUNNER_TARGETS)

    def effect(command: str) -> str:
        return policy.decide(ShellCommand(command=command)).effect

    # Control flow reports nothing, so it fails the "reads and reports" half of
    # the read-only test and passes the half that decides: it changes nothing,
    # and nothing it does reaches a later command.
    for builtin in ("continue", "break", "shift", "return", "local", "exit"):
        assert effect(builtin) == "allow", builtin

    # Each of these decides what some later command sees or does, which is what
    # the read-only list promises a reader it does not touch.
    for builtin in ("eval", "exec", "export", "declare", "unset"):
        assert effect(builtin) == "deny", builtin

    # Control flow de-escalates nothing around it: a guarded verb sharing the
    # command keeps its own verdict.
    assert effect("test -d tmp && continue") == "allow"
    assert effect("continue && rm -rf packages") == "ask"
    assert effect('break; eval "$payload"') == "deny"


def test_shell_policy_confines_trusted_native_skill_scripts() -> None:
    root = "/opt/codex/skills"
    policy = ShellPolicy(SHELL_RULES, trusted_script_roots=[root])

    def effect(command: str) -> str:
        return policy.decide(ShellCommand(command=command)).effect

    helper = f"{root}/.system/openai-docs/scripts/fetch-codex-manual.mjs"
    assert effect(f"node {helper}") == "allow"
    assert effect(f"if true; then sh {root}/tool/scripts/resolve; fi") == "allow"
    assert effect(f"node {helper} && rm source.py") == "ask"
    assert effect("node /tmp/openai-docs/scripts/fetch-codex-manual.mjs") == "deny"
    assert effect(f"node {root}/../escape.mjs") == "deny"
    assert effect("node --eval 'process.exit()'") == "deny"
    assert (
        ShellPolicy(SHELL_RULES, trusted_script_roots=["/"])
        .decide(ShellCommand(command="node /tmp/untrusted-script.mjs"))
        .effect
        == "deny"
    )


def test_shell_policy_preserves_golden_compound_and_wrapper_outcomes(
    tmp_path: Path,
) -> None:
    bundled = load_bundled_kernel(tmp_path, "shell")
    policy = ShellPolicy(
        SHELL_RULES,
        path_roles=FIXTURE_PATH_ROLES,
        path_rules=FIXTURE_PATH_RULES,
        runner_targets=FIXTURE_RUNNER_TARGETS,
    )
    hosts: dict[HostShape, ShellPolicy] = {}

    def host_policy(case: DecisionCase) -> ShellPolicy:
        """One policy per host shape a case describes, built once for each."""
        shape = HostShape(
            sandboxed=case.sandboxed,
            escapable=case.escapable,
            interactive=case.interactive,
        )
        if shape not in hosts:
            hosts[shape] = ShellPolicy(
                SHELL_RULES,
                sandbox_active=case.sandboxed,
                sandbox_excluded_commands=FIXTURE_EXCLUDED_COMMANDS,
                escapable=case.escapable,
                interactive=case.interactive,
                path_roles=FIXTURE_PATH_ROLES,
                path_rules=FIXTURE_PATH_RULES,
                runner_targets=FIXTURE_RUNNER_TARGETS,
            )
        return hosts[shape]

    for index, case in enumerate(SHELL_POLICY_CASES):
        active = host_policy(case)
        # Each case judges a tree of its own, so a file one case declares
        # present never leaks into the next case's create-versus-overwrite.
        root = tmp_path / f"case{index}"
        root.mkdir()
        for name in case.existing:
            present = root / name
            present.parent.mkdir(parents=True, exist_ok=True)
            present.write_text("already here", encoding="utf-8")
        decided = active.decide(ShellCommand(command=case.input, cwd=root))
        assert decided.effect == case.effect, case.input
        bundled_effect = bundled.decide_shell(
            case.input,
            policy.rules,
            sandboxed=case.sandboxed,
            excluded_commands=FIXTURE_EXCLUDED_COMMANDS,
            escapable=case.escapable,
            interactive=case.interactive,
            path_roles=FIXTURE_PATH_ROLES,
            path_rules=policy.path_rules,
            existing_targets=case.existing,
            runner_targets=policy.runner_targets,
        ).effect
        assert bundled_effect == case.effect, case.input


def test_write_targets_name_only_the_paths_a_command_opens_for_writing() -> None:
    assert shell_write_targets("echo x > out.txt") == ["out.txt"]
    assert shell_write_targets("echo x >> notes.log") == ["notes.log"]
    assert shell_write_targets("cat > a.py <<'EOF'\nbody\nEOF") == ["a.py"]
    assert shell_write_targets("wc -l < input.txt") == []
    assert shell_write_targets("grep x f 2>&1") == []
    assert shell_write_targets("ls >&2") == []
    assert shell_write_targets("a > one.txt | b > two.txt") == ["one.txt", "two.txt"]
    assert shell_write_targets("frobnicate --weird") == []


def test_creating_a_file_passes_where_overwriting_one_still_asks(
    tmp_path: Path,
) -> None:
    policy = ShellPolicy(SHELL_RULES)
    existing = tmp_path / "kept.txt"
    existing.write_text("prior work", encoding="utf-8")
    command = "echo x > kept.txt"

    assert policy.decide(ShellCommand(command=command, cwd=tmp_path)).effect == "ask"
    existing.unlink()
    assert policy.decide(ShellCommand(command=command, cwd=tmp_path)).effect == "allow"


def test_sandbox_escape_reenters_the_deny_lattice() -> None:
    policy = ShellPolicy(SHELL_RULES, sandbox_active=True)
    confined = policy.decide(ShellCommand(command="frobnicate --weird"))
    assert confined.effect == "defer"
    escaped = policy.decide(
        ShellCommand(command="frobnicate --weird", unsandboxed=True)
    )
    assert escaped.effect == "deny"
    assert "escalate" in escaped.reason


def test_the_toolchain_carries_its_own_escape_and_is_refused_without_one() -> None:
    """The declaration is the escape, and where nothing can carry it, the stop.

    Confined with nowhere to put the call, the toolchain would reach the shell
    and die on whatever it wrote first — a bare read-only-filesystem error an
    agent reads as a broken repository rather than as a boundary, so it
    retries, works around it, or reports success from a session that never ran
    a command. Naming the sandbox costs one turn instead.
    """
    command = ShellCommand(command="uv run lup-devtools harness resolve")

    placed = ShellPolicy(
        SHELL_RULES,
        sandbox_active=True,
        escapable=True,
        runner_targets=FIXTURE_RUNNER_TARGETS,
    ).decide(command)
    assert placed.effect == "allow"
    assert placed.sandbox == "outside"

    trapped = ShellPolicy(
        SHELL_RULES,
        sandbox_active=True,
        runner_targets=FIXTURE_RUNNER_TARGETS,
    ).decide(command)
    assert trapped.effect == "deny"
    assert trapped.reason == SANDBOX_TRAPPED_REASON
    assert "native sandbox escalation" in trapped.reason
    assert "only a runtime without that channel" in trapped.reason


def test_non_interactive_denials_do_not_prescribe_escalation() -> None:
    """Codex hooks cannot complete the approval flow, so they never name it."""
    interactive = ShellPolicy(SHELL_RULES).decide(
        ShellCommand(command="git push --delete origin feat")
    )
    assert interactive.effect == "ask"

    blocked = ShellPolicy(SHELL_RULES, interactive=False).decide(
        ShellCommand(command="git push --delete origin feat")
    )
    assert blocked.effect == "deny"
    assert "escalate" not in blocked.reason
    assert "allowed vocabulary" in blocked.reason


def test_claude_decoder_marks_unsandboxed_escapes() -> None:
    payload = ClaudeHookPayload(
        tool_name="Bash",
        tool_input={"command": "ls", "dangerouslyDisableSandbox": True},
    )
    decoded = ClaudeEventDecoder().decode(parse_claude_before_tool(payload))
    assert isinstance(decoded.tool, ShellCommand)
    assert decoded.tool.unsandboxed


def test_edit_policy_checks_every_file_before_allowing_batch() -> None:
    policy = EditPolicy(
        protected=[
            PathRule(
                kind="exact",
                value="pyproject.toml",
                reason="project configuration is protected",
            )
        ]
    )
    batch = EditBatch(
        changes=[
            EditChange(path=Path("safe.py"), before="x = 1", after="x = 2"),
            EditChange(
                path=Path("unsafe.py"),
                before="value: str",
                after="value: Any",
            ),
        ]
    )

    denied = policy.decide(batch)
    assert denied.effect == "deny"
    assert "(rule any-type — see docs/rules.md)" in denied.reason
    protected = EditBatch(
        changes=[EditChange(path=Path("pyproject.toml"), after="version = '2'")]
    )
    assert policy.decide(protected).effect == "ask"


def test_retiring_a_stale_suppression_needs_no_approval() -> None:
    """Removing an `ignore` whose violation is gone is ordinary tidying."""
    policy = EditPolicy(protected=[])
    retired = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before="value: Any  # lup: ignore[any-type]",
                after="value: str",
            )
        ]
    )
    assert policy.decide(retired).effect == "allow"


def test_removing_a_live_suppression_is_caught_by_the_anti_pattern_gate() -> None:
    """No marker gate is needed: the violation it covered resurfaces first."""
    policy = EditPolicy(protected=[])
    exposed = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before="value: Any  # lup: ignore[any-type]",
                after="value: Any",
            )
        ]
    )
    decision = policy.decide(exposed)
    assert decision.effect == "deny"
    assert "any-type" in decision.reason


def test_declaring_a_suppression_still_asks() -> None:
    """Silencing a rule is a decision a human makes, not a small safe edit."""
    policy = EditPolicy(protected=[])
    declared = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before="value: str",
                after="value: Any  # lup: ignore[any-type]",
            )
        ]
    )
    decision = policy.decide(declared)
    assert decision.effect == "ask"
    assert decision.reason.startswith("edit introduces an antipattern suppression")


def test_a_suppression_that_silences_nothing_is_refused() -> None:
    """A marker that suppresses nothing is the cheap way past a gate.

    The gate asks about every added directive alike, so a directive naming a
    rule the line does not trip costs one approval and buys an exemption
    nobody weighed. Asking would spend a human turn admitting a marker the
    audit reports spurious the moment it lands.
    """
    policy = EditPolicy(protected=[])
    dead = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before="x = 1\n",
                after="x = 1\ny = 2  # lup: ignore[dict-get]\n",
            )
        ]
    )
    decision = policy.decide(dead)

    assert decision.effect == "deny"
    assert "dict-get" in decision.reason


def test_an_uncovered_violation_denies_whatever_else_the_edit_declares() -> None:
    """One suppression must not carry the violations beside it through.

    Deciding the declared ask first left the denial below it unreachable for
    any edit that added a directive at all, so a marker covering line 2 bought
    approval for an unsuppressed line 3 the prompt never mentioned. The
    precedence is the strong rule's, applied to the rest of the table.

    The fourth case is what keeps this narrow: an edit whose every added
    violation is covered is the ordinary suppression path, and it still asks.
    """
    policy = EditPolicy(protected=[])

    def decide(after: str) -> Decision:
        return policy.decide(
            EditBatch(
                changes=[EditChange(path=Path("a.py"), before="x = 1\n", after=after)]
            )
        )

    alone = decide("x = 1\nsecond: Any = 3\n")
    genuine = decide(
        "x = 1\nfirst: Any = 2  # lup: ignore[any-type]\nsecond: Any = 3\n"
    )
    bare = decide("x = 1\ny = 2  # lup: ignore\nsecond: Any = 3\n")
    every_one_covered = decide(
        "x = 1\nfirst: Any = 2  # lup: ignore[any-type]\n"
        "second: Any = 3  # lup: ignore[any-type]\n"
    )

    assert alone.effect == "deny"
    assert genuine.effect == "deny"
    assert bare.effect == "deny"
    # The denial names what the ask was hiding, or it trades a silent approval
    # for a silent refusal.
    assert "line 3" in genuine.reason
    assert "any-type" in genuine.reason
    assert every_one_covered.effect == "ask"


def test_the_gate_refuses_the_marker_the_refiner_already_refutes() -> None:
    """Both gates reach one verdict, because one refiner answers both.

    A route decorator trips the `dict-get` regex and the AST refutes it, so a
    marker written there guards nothing. The audit reports that afterwards;
    the exemption it reads is the kernel's own, so the same verdict is
    available at the point of writing, which is where it is worth having.
    """
    policy = EditPolicy(protected=[])
    refuted = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before="def read() -> None:\n    pass\n",
                after=(
                    '@app.get("/x")  # lup: ignore[dict-get]\n'
                    "def read() -> None:\n    pass\n"
                ),
            )
        ]
    )
    decision = policy.decide(refuted)

    assert decision.effect == "deny"
    assert "dict-get" in decision.reason


def test_a_refusal_names_what_the_line_trips_instead() -> None:
    """The directive the site wanted is named, so the next attempt is not a guess."""
    policy = EditPolicy(protected=[])
    misnamed = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before="x = 1\n",
                after="x = 1\nvalue: Any = 2  # lup: ignore[dict-get]\n",
            )
        ]
    )
    decision = policy.decide(misnamed)

    assert decision.effect == "deny"
    assert "names dict-get" in decision.reason
    assert "the line trips any-type instead" in decision.reason


def test_a_directive_written_above_its_violation_is_not_dead() -> None:
    """The overflow placement guards the line below, which an edit need not add.

    The reported failure this answers is a marker that went spurious while the
    violation it was written for stayed live. Judging a directive by its own
    line alone would refuse exactly the placement a reason too long to sit
    inline has to take.
    """
    policy = EditPolicy(protected=[])
    hoisted = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before="x = 1\nvalue: Any = 2\n",
                after=(
                    "x = 1\n"
                    "# lup: ignore[any-type] — the SDK hands back Any\n"
                    "value: Any = 2\n"
                ),
            )
        ]
    )
    assert policy.decide(hoisted).effect == "ask"


def test_shrinking_a_dead_directive_is_still_the_audit_s_own_fix() -> None:
    """The gate refusing a marker must not refuse the edit that removes it.

    Dropping one id from a directive is what the audit demands when it reports
    that id spurious, and the result is smaller whether or not what remains is
    dead too — refusing it would leave the marker unremovable.
    """
    policy = EditPolicy(protected=[])
    narrowed = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before="x = 1\ny = 2  # lup: ignore[any-type, dict-get]\n",
                after="x = 1\ny = 2  # lup: ignore[any-type]\n",
            )
        ]
    )
    assert policy.decide(narrowed).effect == "allow"


def test_only_the_dead_half_of_a_directive_is_refused() -> None:
    """A directive doing part of its job is refused for the part that is dead.

    The remedy names the id rather than the directive, because dropping the
    whole of one that also covers a live violation would only resurface the
    denial it was silencing.
    """
    policy = EditPolicy(protected=[])
    mixed = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before="x = 1\n",
                after="x = 1\nvalue: Any = 2  # lup: ignore[any-type, dict-get]\n",
            )
        ]
    )
    decision = policy.decide(mixed)

    assert decision.effect == "deny"
    assert "names dict-get" in decision.reason
    assert "Drop dict-get from it" in decision.reason


def test_a_rule_another_scanner_owns_is_not_refused_over() -> None:
    """A verdict this gate cannot reach is not one it may refuse over.

    `abc-capability` belongs to a scanner the hermetic runtime does not
    carry, so whether the line trips it is unknowable here — the same line
    the audit draws when it decides which markers it may call spurious.
    """
    policy = EditPolicy(protected=[])
    foreign = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before="x = 1\n",
                after="x = 1\nclass Store(ABC):  # lup: ignore[abc-capability]\n",
            )
        ]
    )
    assert policy.decide(foreign).effect == "ask"


def test_a_fragment_with_no_tree_is_not_refused_over_a_guess() -> None:
    """A refiner reads an AST, and an exemption from a missing one is a guess.

    `tuple_shape_exempt_lines` clears every line where the source does not
    parse, so a gate that read clearance as proof would refuse a directive for
    its own blindness. With no tree there is no hit either, so what is left is
    the ordinary ask.
    """
    policy = EditPolicy(protected=[])
    fragment = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before="    pass\n",
                after="    pair: tuple[int, str] = (1, 2)  # lup: ignore[tuple-shape]\n",
            )
        ]
    )
    assert policy.decide(fragment).effect == "ask"


def test_an_allowance_buys_a_live_suppression_and_never_a_dead_one() -> None:
    """The grant answers the ask, and the refusal is not an ask.

    A human approving a plan that needs suppressions approved reasoned
    exceptions, not markers that silence nothing — so the allowance reaches
    the directive that covers a violation and stops at the one that covers
    none.
    """

    def granted(after: str) -> KernelDecision:
        change = EditChange(path=Path("a.py"), before="value: str\n", after=after)
        return decide_edit(
            "a.py",
            change.before,
            change.after,
            path_exists=True,
            path_rules=[],
            antipattern_rows=antipattern_rows(change),
            allowances=["antipattern-suppression"],
            python_source=True,
        )

    assert granted("value: Any  # lup: ignore[any-type]\n").effect == "allow"
    assert granted("value: str  # lup: ignore[any-type]\n").effect == "deny"


def test_a_creation_names_the_suppressions_it_arrives_carrying() -> None:
    """A whole write is approved for its shape, and its directives ride along.

    Reviewing a creation is worth doing for the layout and the shape it shows,
    which is exactly what makes a directive in the middle of a new module the
    easiest thing in an edit to approve without having seen it.
    """
    policy = EditPolicy(protected=[])
    carrying = EditBatch(
        changes=[
            EditChange(
                path=Path("src/new.py"),
                after='"""Doc."""\n\nvalue: Any = 1  # lup: ignore[any-type]\n',
            )
        ]
    )
    plain = EditBatch(
        changes=[EditChange(path=Path("src/new.py"), after='"""Doc."""\n')]
    )
    decision = policy.decide(carrying)

    assert decision.effect == "ask"
    assert "this new file arrives carrying antipattern suppressions" in decision.reason
    assert (
        "line 3 silences any-type: value: Any = 1  # lup: ignore[any-type]"
        in decision.reason
    )
    assert policy.decide(plain).reason == "full-file writes require approval"


def test_dropping_one_rule_from_a_suppression_needs_no_approval() -> None:
    """Shrinking a directive is what the audit asks for when it calls one spurious.

    Reading the added line alone cannot tell this from a suppression appearing
    out of nowhere, so the gate used to ask — and the audit was already
    demanding the very edit it asked to approve.
    """
    policy = EditPolicy(protected=[])
    narrowed = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before="# lup: ignore[any-type, dict-get]\nvalue = 1\n",
                after="# lup: ignore[any-type]\nvalue = 1\n",
            )
        ]
    )
    assert policy.decide(narrowed).effect == "allow"


def test_a_bare_suppression_narrowed_to_named_rules_needs_no_approval() -> None:
    """The bare directive covers every rule, so naming a few can only shrink it."""
    policy = EditPolicy(protected=[])
    typed = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before="# lup: ignore\nvalue = 1\n",
                after="# lup: ignore[any-type]\nvalue = 1\n",
            )
        ]
    )
    assert policy.decide(typed).effect == "allow"


def test_widening_a_suppression_still_asks() -> None:
    """Adding a rule to a directive silences something it did not before."""
    policy = EditPolicy(protected=[])
    widened = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before="# lup: ignore[any-type]\nvalue = 1\n",
                after="# lup: ignore[any-type, dict-get]\nvalue = 1\n",
            )
        ]
    )
    assert policy.decide(widened).effect == "ask"


def test_a_named_suppression_going_bare_still_asks() -> None:
    """Dropping the names widens the directive to every rule."""
    policy = EditPolicy(protected=[])
    widened = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before="# lup: ignore[any-type]\nvalue = 1\n",
                after="# lup: ignore\nvalue = 1\n",
            )
        ]
    )
    assert policy.decide(widened).effect == "ask"


def test_prose_mentioning_a_suppression_is_not_declaring_one() -> None:
    """Documenting the escape hatch is neither a note nor a directive."""
    policy = EditPolicy(protected=[])
    documented = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before='NOTE_RE = compile(r"lup")',
                after=(
                    "# Matching `# lup: ignore` here is prose, not a directive.\n"
                    'NOTE_RE = compile(r"lup")'
                ),
            )
        ]
    )
    assert policy.decide(documented).effect == "allow"


def test_a_granted_suppression_releases_only_its_own_gate() -> None:
    """An allowance answers the gate it names, never the rest of the lattice."""
    change = EditChange(
        path=Path("a.py"),
        before="x = 1  # lup: fix",
        after="value: Any  # lup: ignore[any-type]",
    )
    decision = decide_edit(
        "a.py",
        change.before,
        change.after,
        path_exists=False,
        path_rules=[],
        antipattern_rows=antipattern_rows(change),
        allowances=["antipattern-suppression"],
        python_source=True,
    )
    assert decision.effect == "deny"
    assert "removes inline review feedback" in decision.reason


def test_adding_feedback_asks_and_deleting_it_is_refused() -> None:
    """The two directions are different acts and get different answers.

    An ask is something an agent argues through in the turn that wanted the
    deletion, and a deleted note is the one thing nobody can review after the
    fact: its absence is indistinguishable from a note that never existed.
    """
    policy = EditPolicy(protected=[])
    added = EditBatch(
        changes=[
            EditChange(path=Path("a.py"), before="x = 1", after="x = 1  # lup: fix")
        ]
    )
    removed = EditBatch(
        changes=[
            EditChange(path=Path("a.py"), before="x = 1  # lup: fix", after="x = 1")
        ]
    )

    assert policy.decide(added).effect == "ask"
    assert policy.decide(removed).effect == "deny"


def test_converting_a_note_into_a_claim_is_the_way_through() -> None:
    """Resolving keeps the words and changes the keyword, so it stays checkable."""
    policy = EditPolicy(protected=[])
    claimed = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before="x = 1  # lup: fix the cache",
                after="x = 1  # lup: solved: fix the cache",
            )
        ]
    )

    assert policy.decide(claimed).effect == "allow"


def test_removing_a_customization_marker_is_not_removing_feedback() -> None:
    """A `template:` marker is answered by writing code, not by claiming.

    It behaves like `ignore` rather than like feedback: nobody is owed an
    answer to a placeholder, and the domain's own code standing where the
    scaffold's example stood leaves no original ask for a claim to be checked
    against. Denying its removal would refuse `/lup:init` the one edit it
    exists to make, in every repository built from this template.
    """
    policy = EditPolicy(protected=[])
    customized = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before="# lup: template: pick your model tier\nTIER = None",
                after='TIER = "strongest"',
            )
        ]
    )

    assert policy.decide(customized).effect == "allow"


def test_no_allowance_retires_a_claim_through_the_edit_gate() -> None:
    """A claim is checked by someone other than whoever made it.

    The verify pass's authority lives in its own instrument
    (`dev comments --retire/--restore`), never in a session environment —
    so a grant claiming otherwise changes nothing here.
    """
    change = EditChange(
        path=Path("a.py"),
        before="x = 1  # lup: solved: fix the cache",
        after="x = 1",
    )
    batch = EditBatch(changes=[change])

    assert EditPolicy(protected=[]).decide(batch).effect == "deny"
    assert (
        decide_edit(
            "a.py",
            change.before,
            change.after,
            path_exists=True,
            path_rules=[],
            antipattern_rows=antipattern_rows(change),
            allowances=["note-resolution"],
            python_source=True,
        ).effect
        == "deny"
    )


def test_prose_documenting_the_marker_syntax_is_not_feedback() -> None:
    """A backtick span is an example, which is how a reader tells them apart.

    Counting quoted markers made documenting the convention indistinguishable
    from leaving a note, so writing about the gate tripped it.
    """
    policy = EditPolicy(protected=[])
    documented = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before='"""Doc."""\n',
                after='"""Resolve a note by writing `# lup: solved:` before it."""\n',
            )
        ]
    )

    assert policy.decide(documented).effect == "allow"


def test_a_note_in_scratch_is_not_gated() -> None:
    """Nothing under a scratch root persists to be read, so no reader is owed."""
    policy = EditPolicy(protected=[], path_roles=FIXTURE_PATH_ROLES)
    batch = EditBatch(
        changes=[
            EditChange(
                path=Path("tmp/probe.py"), before="x = 1  # lup: fix", after="x = 1"
            )
        ]
    )
    assert policy.decide(batch).effect == "allow"


def test_a_note_in_a_test_is_still_gated() -> None:
    """A test file persists and is read, so feedback left there is owed too."""
    policy = EditPolicy(protected=[], path_roles=FIXTURE_PATH_ROLES)
    batch = EditBatch(
        changes=[
            EditChange(
                path=Path("tests/unit/test_thing.py"),
                before="x = 1  # lup: fix",
                after="x = 1",
            )
        ]
    )
    assert policy.decide(batch).effect == "deny"


def test_retiring_a_suppression_the_ast_refutes_is_allowed() -> None:
    """The gate that demanded this marker gone must not be the one refusing it.

    A route decorator trips the `dict-get` regex and nothing else, so before
    the refiner the audit called the marker spurious while the kernel denied
    every edit that removed it — a change one gate required and the other
    forbade, with no operation in between.
    """
    policy = EditPolicy(protected=[])
    batch = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before='@app.get("/x")  # lup: ignore[dict-get]\ndef read() -> None:\n    pass\n',
                after='@app.get("/x")\ndef read() -> None:\n    pass\n',
            )
        ]
    )
    assert policy.decide(batch).effect == "allow"


def test_a_suppression_ask_names_every_line_it_is_asking_about() -> None:
    """A prompt carries the reason and nothing else, so it locates and names.

    The quoted line is a preview and a directive is written at the end of the
    line it guards, so which rule is being silenced is the first thing a cut
    takes — it is stated rather than left to be read back out.
    """
    policy = EditPolicy(protected=[])
    batch = EditBatch(
        changes=[
            EditChange(
                path=Path("a.py"),
                before="x = 1\n",
                after=(
                    "x = 1\n"
                    "first: Any = 1  # lup: ignore[any-type]\n"
                    "second: Any = 2  # lup: ignore[any-type]\n"
                ),
            )
        ]
    )
    decision = policy.decide(batch)

    assert decision.effect == "ask"
    assert (
        "line 2 silences any-type: first: Any = 1  # lup: ignore[any-type]"
        in decision.reason
    )
    assert (
        "line 3 silences any-type: second: Any = 2  # lup: ignore[any-type]"
        in decision.reason
    )


def test_a_denial_names_the_line_that_tripped_it() -> None:
    policy = EditPolicy(protected=[])
    batch = EditBatch(
        changes=[
            EditChange(path=Path("a.py"), before="x = 1\n", after="x = 1\ny: Any = 2\n")
        ]
    )
    decision = policy.decide(batch)

    assert decision.effect == "deny"
    assert decision.reason.startswith("line 2: ")


def test_a_composed_session_enforces_the_rules_the_generated_tree_does() -> None:
    """One declaration, two enforcement paths, and nothing between them.

    A generated dispatcher compiles the hook set into rows; a session this
    program composes builds policy objects from the same hook set. Neither
    can see the other, so a rule added to one and not the other would make a
    run's permissions depend on who launched it — which is exactly how a
    resolver worker ran under a directory ACL while every plugin generated
    from the same declaration judged the acts semantically.
    """
    hooks = next(plugin.hooks for plugin in portable_harness().plugins if plugin.hooks)
    composed = [path_rule_row(rule) for rule in declared_path_rules(hooks)]
    generated = runtime_path_rules(
        [root.as_posix() for root in hooks.protected_edit_roots],
        [path.as_posix() for path in hooks.human_owned_files],
    )

    assert composed == generated


def test_canonical_edit_policy_preserves_shared_security_outcomes() -> None:
    protected = [
        PathRule(
            kind="subtree",
            value=".claude",
            reason="protected path requires approval",
            allow_autonomous=True,
        ),
        human_owned_path_rule("README.md"),
        PathRule(
            kind="subtree",
            value="sync.json",
            reason="protected path requires approval",
            allow_autonomous=True,
        ),
        PathRule(
            kind="subtree",
            value="downstream.json",
            reason="protected path requires approval",
            allow_autonomous=True,
        ),
    ]

    for case in EDIT_POLICY_CASES:
        policy = EditPolicy(
            protected=protected,
            autonomous=case.autonomous,
            path_roles=FIXTURE_PATH_ROLES,
        )
        decision = policy.decide(
            EditBatch(
                changes=[
                    EditChange(
                        path=Path(case.path), before=case.before, after=case.after
                    )
                ]
            )
        )
        assert decision.effect == case.effect


def test_bundled_edit_policy_matches_canonical_security_outcomes(
    tmp_path: Path,
) -> None:
    bundled = load_bundled_kernel(tmp_path, "edit")
    policy = EditPolicy(
        protected=[
            PathRule(
                kind="subtree",
                value=".claude",
                reason="protected path requires approval",
                allow_autonomous=True,
            ),
            human_owned_path_rule("README.md"),
            PathRule(
                kind="subtree",
                value="sync.json",
                reason="protected path requires approval",
                allow_autonomous=True,
            ),
            PathRule(
                kind="subtree",
                value="downstream.json",
                reason="protected path requires approval",
                allow_autonomous=True,
            ),
        ],
        path_roles=FIXTURE_PATH_ROLES,
    )
    cases = [
        item
        for item in EDIT_POLICY_CASES
        if not item.autonomous and item.before is not None and item.after is not None
    ]
    for case in cases:
        canonical = policy.decide(
            EditBatch(
                changes=[
                    EditChange(
                        path=Path(case.path), before=case.before, after=case.after
                    )
                ]
            )
        )
        generated = assembled_edit_decision(
            bundled,
            case.path,
            case.before,
            case.after,
            [".claude", "pyproject.toml", "sync.json", "downstream.json"],
            ["README.md"],
        )
        assert canonical.effect == generated.effect == case.effect


def test_bundled_autonomous_worker_keeps_guardrails(tmp_path: Path) -> None:
    bundled = load_bundled_kernel(tmp_path, "edit")

    cases = [item for item in EDIT_POLICY_CASES if item.autonomous]
    for case in cases:
        decision = assembled_edit_decision(
            bundled,
            case.path,
            case.before,
            case.after,
            [".claude"],
            ["README.md"],
            autonomous=True,
        )
        assert decision.effect == case.effect


def test_edit_policy_uses_full_python_context_for_added_docstrings(
    tmp_path: Path,
) -> None:
    bundled = load_bundled_kernel(tmp_path, "edit")
    before = '"""Documentation.\n"""\nvalue = 1'
    unrestricted_type_name = "A" + "ny"
    after = (
        f'"""Documentation can mention {unrestricted_type_name} safely.\n"""\nvalue = 1'
    )
    canonical = EditPolicy(protected=[]).decide(
        EditBatch(
            changes=[EditChange(path=Path("src/module.py"), before=before, after=after)]
        )
    )

    assert canonical.effect == "allow"
    assert (
        assembled_edit_decision(bundled, "src/module.py", before, after, [], []).effect
        == "allow"
    )


def test_edit_policy_bundle_embeds_canonical_ast_refinement(tmp_path: Path) -> None:
    bundled = load_bundled_kernel(tmp_path, "edit")
    before = (
        "class Scheduler:\n    def __init__(self) -> None:\n        self.ready = True\n"
    )
    empty_list_literal = "[]"
    after = before + f"        self.pending: list[str] = {empty_list_literal}\n"
    canonical = EditPolicy(protected=[]).decide(
        EditBatch(
            changes=[
                EditChange(path=Path("src/scheduler.py"), before=before, after=after)
            ]
        )
    )

    assert canonical.effect == "allow"
    assert (
        assembled_edit_decision(
            bundled, "src/scheduler.py", before, after, [], []
        ).effect
        == "allow"
    )


def test_content_prose_examples_do_not_trip_code_or_marker_gates() -> None:
    path = Path("packages/lup/src/lup/devtools/harness/content/skills/commit.py")
    before = path.read_text(encoding="utf-8")
    after = before + (
        '\nPROSE_GATE_EXAMPLE = """Any and # lup: examples remain prose."""\n'
    )

    decision = EditPolicy(protected=[]).decide(
        EditBatch(changes=[EditChange(path=path, before=before, after=after)])
    )

    assert decision.effect == "allow"


def granting(root: Path, *allowances: ConcernAllowance) -> LeaseGrants:
    """A lease holding exactly these gates, through the document that says so."""
    document = root / "grants.json"
    write_allowance_grants(document, list(allowances))
    return LeaseGrants(document)


def test_a_granted_new_devtools_allowance_releases_exactly_that_gate(
    tmp_path: Path,
) -> None:
    """A concern's grant skips the new-devtools ask and nothing adjacent."""
    rule = PathRule(
        kind="new_devtools",
        value="src",
        reason="new devtools module requires approval",
    )
    creation = EditBatch(
        changes=[
            EditChange(
                path=Path("src/app/devtools/harness/content/docs/newborn.py"),
                after='"""Newborn."""\n',
            )
        ]
    )
    ungranted = EditPolicy(protected=[rule], autonomous=True).decide(creation)
    assert ungranted.effect == "ask"
    assert "new devtools module" in ungranted.reason
    granted = EditPolicy(
        protected=[rule],
        autonomous=True,
        grants=granting(tmp_path, ConcernAllowance.NEW_DEVTOOLS_MODULE),
    )
    assert granted.decide(creation).effect == "allow"


def test_fragment_edits_are_judged_as_the_documents_they_produce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A marker mentioned inside a string literal is prose, not feedback.

    The path is repo-relative because the session scratchpad is exempt from
    the marker gate by role, and the gate is what this test drives.
    """
    monkeypatch.chdir(tmp_path)
    Path("content.py").write_text(
        'TABLE = """\nA note spells itself as # lup: fix this here.\n"""\n',
        encoding="utf-8",
    )
    fragment = EditBatch(
        changes=[
            EditChange(
                path=Path("content.py"),
                before="A note spells itself as # lup: fix this here.\n",
                after="",
            )
        ]
    )
    policy = EditPolicy(protected=[])
    assert policy.decide(fragment).effect == "deny"
    assert policy.decide(fragment.as_documents()).effect == "allow"


def test_semantic_policy_threads_allowances_into_the_edit_gate(
    tmp_path: Path,
) -> None:
    """The composed policy honours the same grants the dispatchers read."""
    creation = EditBatch(
        changes=[
            EditChange(
                path=Path("src/app/devtools/newborn.py"),
                after='"""Newborn."""\n',
            )
        ]
    )
    hooks = HookSet(id="test", policy_ids=["edit"])
    withheld = semantic_policy_for(hooks, autonomous=True)
    assert withheld.decide(creation).effect == "ask"
    released = semantic_policy_for(
        hooks,
        autonomous=True,
        grants=granting(tmp_path, ConcernAllowance.NEW_DEVTOOLS_MODULE),
    )
    assert released.decide(creation).effect == "allow"


def test_removing_a_file_s_last_conflict_marker_is_not_read_as_deleting_a_note() -> (
    None
):
    """Resolving a merge must be able to finish in a file that mentions the marker.

    A conflicted Python file does not tokenize, so the count fell back to a
    whole-text tally that also sees the marker inside ordinary string
    literals. Removing the last conflict marker is what makes the file parse
    for the first time, at which point the count drops to the tokenised
    truth — a difference the gate read as removed feedback, denying the one
    edit that completes any conflict resolution.
    """
    conflicted = (
        "<<<<<<< HEAD\n"
        'MESSAGE = "No unresolved # lup: comments"\n'
        "=======\n"
        'MESSAGE = "No unresolved # lup: comments, and no open issues."\n'
        ">>>>>>> other\n"
    )
    resolved = 'MESSAGE = "No unresolved # lup: comments, and no open issues."\n'

    decision = decide_edit(
        "a.py",
        conflicted,
        resolved,
        path_exists=True,
        path_rules=[],
        antipattern_rows=[],
        allowances=[],
        python_source=True,
    )

    assert "removes inline review feedback" not in decision.reason


def test_a_real_note_deletion_is_still_denied_when_both_sides_parse() -> None:
    """Unknown must widen to no-opinion, not to a hole in the gate."""
    decision = decide_edit(
        "a.py",
        "x = 1  # lup: reconsider this\n",
        "x = 1\n",
        path_exists=True,
        path_rules=[],
        antipattern_rows=[],
        allowances=[],
        python_source=True,
    )

    assert decision.effect == "deny"
    assert "removes inline review feedback" in decision.reason
