"""A shell vocabulary an adopter starts from, offered as groups to compose.

:mod:`lup.policy.shell_rules` declares the *shape* a vocabulary takes and
says the words themselves are a judgement about one project's toolchain, so
they arrive from outside. That is true, and it left the library shipping
nothing — which is not the neutral position it reads as. An empty table
matches no command, every command is then unjudged, and unjudged resolves to
a deny: a fresh adopter's agent cannot run ``ls`` until several hundred lines
of vocabulary exist. Shipping nothing chose a verdict for them just as surely
as shipping something would have, and chose the least useful one.

So each group below is a function returning rules, and every word it declares
is a parameter default rather than a table. An adopter calls the group to
take the judgement as offered, passes their own words to replace it, or
splices extra rules around it::

    SHELL_RULES = [
        *read_only_rules(),
        *judged_ask_rules(),
        *guarded_tool_rules(),
        git_rule(guard_force_push=False, redirect_checkout=True),
        gh_rule(),
        docker_rule(),
        my_own_cli_rule(),
    ]

The judgement running through all of it: generous for reading and for local
work a second attempt undoes, conservative for anything that loses something.
Networked is deliberately not the line — publishing is how work becomes
reviewable and happens many times a session, so ``git push`` and the pull
request verbs that open and describe one are ordinary. What stays guarded is
the direction that removes something no second attempt restores.

Where a group's default encodes a judgement a reasonable project would make
differently, it takes a parameter instead of a fork: whether a force push is
guarded, whether ``checkout`` is redirected toward ``switch``/``restore``,
whether opening a pull request is authoring or publishing.
"""

from collections.abc import Sequence

from pydantic import BaseModel

from lup.policy.kernel.decision import SandboxPlacement
from lup.policy.shell_rules import (
    RunnerTargetRule,
    ShellCommandRule,
    ShellOperationRule,
    ShellSubcommandRule,
)


class JudgedCommand(BaseModel, frozen=True):
    """One command that stops for a human, and the reason it gives them."""

    name: str
    reason: str
    read_verbs: list[str] = []
    """This command's own spellings of its query action, which de-escalate it.

    Without somewhere to say "except this verb", listing an archive is as
    much an approval as extracting one."""


def read_only_rules(
    commands: Sequence[str] = (
        "ls",
        "cat",
        "echo",
        "printf",
        "test",
        "file",
        "wc",
        "head",
        "tail",
        "nl",
        "tac",
        "rev",
        "fold",
        "cut",
        "tr",
        "expr",
        "numfmt",
        "comm",
        "join",
        "paste",
        "column",
        "uniq",
        "grep",
        "egrep",
        "fgrep",
        "diff",
        "cmp",
        "jq",
        "stat",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "date",
        "seq",
        "du",
        "df",
        "cksum",
        "md5sum",
        "sha256sum",
        "which",
        "true",
        "false",
        "set",
        "continue",
        "break",
        "shift",
        "return",
        "local",
        "exit",
        "sleep",
        "pwd",
        "id",
        "whoami",
        "hostname",
        "uname",
        "printenv",
        "env",
        "ps",
        "pgrep",
        "pidof",
        "lsof",
        "free",
        "uptime",
        "nproc",
        "xxd",
        "od",
        "strings",
        "getent",
        "zcat",
        "[",
    ),
) -> list[ShellCommandRule]:
    """Commands that read and report, and change nothing by running.

    The process and socket listings sit here for the same reason the file
    ones do: establishing that a service came up is a read, and a session
    that just started one is asking about its own process.

    The control-flow builtins report nothing and are here on the second half
    of that test: they change nothing by running, and nothing they do can
    reach a later command. `eval`, `exec`, `export`, `declare` and `unset`
    are deliberately absent — each decides what some later command sees or
    does, which is the thing this list promises a reader it does not touch.
    """
    return [ShellCommandRule(name=name, default_effect="allow") for name in commands]


def judged_ask_rules(
    commands: Sequence[JudgedCommand] = (
        JudgedCommand(name="rm", reason="deleting files requires approval"),
        JudgedCommand(name="rmdir", reason="deleting directories requires approval"),
        JudgedCommand(name="mv", reason="moving files requires approval"),
        JudgedCommand(name="cp", reason="copying over files requires approval"),
        JudgedCommand(
            name="touch",
            reason="creating files requires approval — prefer the Write tool",
        ),
        JudgedCommand(name="chmod", reason="changing permissions requires approval"),
        JudgedCommand(name="chown", reason="changing ownership requires approval"),
        JudgedCommand(name="ln", reason="creating links requires approval"),
        JudgedCommand(
            name="tee",
            reason="writing files requires approval — prefer the Write tool",
        ),
        JudgedCommand(name="dd", reason="raw device or file writes require approval"),
        JudgedCommand(name="truncate", reason="truncating files requires approval"),
        JudgedCommand(name="kill", reason="terminating processes requires approval"),
        JudgedCommand(name="pkill", reason="terminating processes requires approval"),
        JudgedCommand(
            name="tar",
            # Named whole rather than scanned for a `t`, which a bundled
            # `-xzf` also contains: these are tar's own list-mode spellings.
            read_verbs=["-t", "--list", "-tf", "-tvf", "-tzf", "-tzvf", "-tjf", "-tJf"],
            reason="archive operations write files — requires approval",
        ),
        JudgedCommand(
            name="unzip",
            read_verbs=["-l", "-t", "-v", "-z"],
            reason="archive extraction writes files — requires approval",
        ),
        JudgedCommand(
            name="zip",
            read_verbs=["-sf", "--show-files"],
            reason="archive creation writes files — requires approval",
        ),
        JudgedCommand(
            name="gzip",
            read_verbs=["-l", "--list", "-t", "--test"],
            reason="compression rewrites files — requires approval",
        ),
        JudgedCommand(
            name="gunzip",
            read_verbs=["-l", "--list", "-t", "--test"],
            reason="decompression rewrites files — requires approval",
        ),
        JudgedCommand(name="sudo", reason="privilege escalation requires approval"),
        JudgedCommand(name="doas", reason="privilege escalation requires approval"),
        JudgedCommand(name="ssh", reason="remote access requires approval"),
        JudgedCommand(name="scp", reason="remote copies require approval"),
        JudgedCommand(name="rsync", reason="remote sync requires approval"),
        JudgedCommand(
            name="wget",
            reason="downloading files requires approval — prefer curl or WebFetch",
        ),
        JudgedCommand(
            name="make",
            read_verbs=["-n", "--dry-run", "--just-print", "-q", "--question"],
            reason="make executes arbitrary recipes — requires approval",
        ),
        JudgedCommand(
            name="npm",
            read_verbs=["ls", "list", "view", "outdated", "why", "explain"],
            reason="package tools fetch and execute code — requires approval",
        ),
        # lup: A full verify line — ruff format, ruff check, pyright, pytest,
        # then `cd frontend && npx tsc --noEmit` — was refused here with
        # "package tools fetch and execute code". It should probably have been
        # auto-allowed, and so should the `command tsc isn't recognized` probe
        # the same line falls back to.
        JudgedCommand(
            name="npx",
            reason="package tools fetch and execute code — requires approval",
        ),
        JudgedCommand(
            name="pnpm",
            reason="package tools fetch and execute code — requires approval",
        ),
        JudgedCommand(
            name="yarn",
            reason="package tools fetch and execute code — requires approval",
        ),
        JudgedCommand(name="apt", reason="system package changes require approval"),
        JudgedCommand(name="apt-get", reason="system package changes require approval"),
        JudgedCommand(name="pacman", reason="system package changes require approval"),
        JudgedCommand(name="brew", reason="system package changes require approval"),
        JudgedCommand(name="systemctl", reason="service management requires approval"),
        JudgedCommand(name="crontab", reason="schedule changes require approval"),
    ),
) -> list[ShellCommandRule]:
    """Commands that ask on every production path, with the reason each carries.

    Inside a root declared as scratch these allow instead, which the kernel
    settles by resolving each target's role — so the ask lands on production
    work rather than on a disposable tree.
    """
    return [
        ShellCommandRule(
            name=command.name,
            default_effect="ask",
            read_verbs=command.read_verbs,
            reason=command.reason,
        )
        for command in commands
    ]


def redirected_rules(
    commands: Sequence[JudgedCommand] = (
        JudgedCommand(name="pip", reason="use uv add / uv remove instead of pip"),
        JudgedCommand(name="pip3", reason="use uv add / uv remove instead of pip"),
    ),
) -> list[ShellCommandRule]:
    """Commands denied toward the spelling a project actually uses.

    A redirect is house style rather than a safety verdict, which is why the
    pairs are a parameter: a project on a different package manager replaces
    them instead of inheriting an argument about uv.
    """
    return [
        ShellCommandRule(
            name=command.name, default_effect="deny", reason=command.reason
        )
        for command in commands
    ]


def guarded_tool_rules() -> list[ShellCommandRule]:
    """Readers whose own flags can turn them into writers, and the guards.

    Each entry is a fact about the utility rather than a preference: ``sort
    -o`` writes a file, ``ss -K`` closes sockets, ``nc`` moves bytes unless
    ``-z`` pins it to a scan. What varies between projects is which of these
    are installed, not what they do, so nothing here takes a parameter.
    """
    return [
        ShellCommandRule(
            # Not read-only, and not judged either. The verbs in the ask list
            # are destructive or content-bearing: they overwrite something, or
            # author a file that later holds code. An empty directory does
            # neither — `-p` is a no-op on one that exists, and it holds
            # nothing to run. Everything landing inside it still passes the
            # write and edit gates on its own path.
            name="mkdir",
            default_effect="allow",
        ),
        ShellCommandRule(
            # -l/-L print fingerprints and public keys — the read-only
            # diagnostic for push-auth failures; every other form mutates the
            # agent.
            name="ssh-add",
            default_effect="deny",
            allow_flags=["-l", "-L"],
            reason="credential-agent changes stay with the user — ask them to run it",
        ),
        ShellCommandRule(
            name="ssh-agent",
            default_effect="deny",
            reason="credential-agent lifecycle stays with the user — ask them to run it",
        ),
        ShellCommandRule(
            name="sort",
            default_effect="allow",
            ask_flags=["-o", "--output", "--compress-program"],
            reason="a sort flag that writes a file or runs a program requires approval",
        ),
        ShellCommandRule(
            # Listing a directory is a read, and `-o` lands that listing in a
            # file — the same flag on the same kind of tool as `sort -o`.
            name="tree",
            default_effect="allow",
            ask_flags=["-o"],
            reason="a tree flag that writes a file requires approval",
        ),
        ShellCommandRule(
            # A search that runs a program. `--pre` and `--hostname-bin` name
            # one ripgrep invokes for every file it touches, which is
            # arbitrary execution wearing a search's clothes, and `-z` hands
            # the input to whichever decompressor the extension implies.
            name="rg",
            default_effect="allow",
            ask_flags=["--pre", "--hostname-bin", "--search-zip", "-z"],
            reason="a ripgrep flag that runs another program requires approval",
        ),
        ShellCommandRule(
            # Encoding is a filter; `-o` is the one form that lands a file.
            name="base64",
            default_effect="allow",
            ask_flags=["-o", "--output"],
            reason="a base64 flag that writes a file requires approval",
        ),
        ShellCommandRule(
            name="yq",
            default_effect="allow",
            ask_flags=["-i", "--inplace", "--in-place", "-s", "--split-exp"],
            reason=(
                "a yq flag that edits files in place or splits into files"
                " requires approval"
            ),
        ),
        ShellCommandRule(
            # xmllint reads every option with one or two leading dashes, so
            # both spellings are guarded; a two-char "-o" guard would
            # cluster-match benign words like -noout, and no bare "-o" option
            # exists.
            name="xmllint",
            default_effect="allow",
            ask_flags=["--output", "-output", "--shell", "-shell"],
            reason=(
                "an xmllint flag that writes files or opens a shell requires approval"
            ),
        ),
        ShellCommandRule(
            # -exec/-execdir payloads recurse through the kernel's find
            # screen; only the file-writing and deleting actions remain
            # flag-guarded.
            name="find",
            default_effect="allow",
            ask_flags=["-delete", "-fprint", "-fprint0", "-fprintf", "-fls"],
            reason="a mutating find action requires approval",
        ),
        ShellCommandRule(
            # `ss -K` closes established sockets; every other form reports.
            name="ss",
            default_effect="allow",
            ask_flags=["-K", "--kill"],
            reason="killing sockets requires approval",
        ),
        ShellCommandRule(
            # Establishing that a port answers is how a session learns the
            # service it just started came up, and `-z` does exactly that and
            # nothing else: connect, report, close. Every other form moves
            # bytes — `-l` listens, `-e`/`-c` hand the socket to a program.
            name="nc",
            default_effect="deny",
            read_verbs=["-z"],
            ask_flags=["-l", "-e", "-c"],
            reason="netcat moves data unless -z pins it to a port scan",
        ),
        ShellCommandRule(
            name="cd", default_effect="allow", reason="directory navigation"
        ),
    ]


def runner_target_rules(
    ambient: Sequence[str] = ("pyright", "pytest", "ruff"),
    session_opening: Sequence[str] = ("lup-devtools",),
) -> list[RunnerTargetRule]:
    """The ``uv run`` targets a project blesses, grouped by what each needs.

    A checker reads the tree and writes inside it, so it runs wherever the
    session runs and takes ``ambient``.

    A toolchain that opens agent sessions cannot. The runtime keeps
    per-session state under its own configuration directory — for Claude Code,
    ``~/.claude/session-env/<session id>``, following ``CLAUDE_CONFIG_DIR`` —
    and a session opened from inside a sandbox that does not grant that path
    dies on its first shell call with a bare ``EROFS``, which reads to an agent
    like a broken repository rather than like a boundary. It then retries,
    works around it, or reports success from a session that never ran a
    command; one planning run finished that way and looked normal.

    Measured rather than assumed, because the deny looks like the runtime
    protecting its own configuration directory: under one ``~/.claude`` parent,
    ``debug`` — a path the sandbox grants — accepts a directory, while
    ``session-env`` and a per-session configuration directory beside it, which
    it does not grant, both refuse one with ``EROFS``. So the deny is the
    ordinary write allowlist and a grant would lift it. It is still not the
    remedy: the runtime chooses that path per session, so the grant is a family
    rather than a path; deriving a private configuration home does not move it,
    because every entry such a home does not own links back to the shared one;
    and session state is only the first thing such a
    toolchain writes outside the tree — a worktree, a plugin cache, and the
    git configuration behind them follow it.

    Which is why the escape is declared here, once, on the toolchain itself,
    rather than left to each caller to remember. Where a runtime places no
    single call outside its sandbox, a confined session is stopped with that
    reason instead — see :func:`~lup.policy.kernel.shell.decide_shell`.
    """
    return [
        *[RunnerTargetRule(name=name) for name in ambient],
        *[RunnerTargetRule(name=name, sandbox="outside") for name in session_opening],
    ]


# One criterion decides this table, applied across git's surface rather than to
# whichever word was last found missing: a subcommand belongs here when it moves
# no ref, mutates no index entry, and writes nothing into the working tree.
#
# That is a question about what a verb *reaches*, not about whether it happens
# to write bytes, which is why the object-construction verbs pass it. An object
# nothing points at is unreachable the moment it exists and git collects it, so
# there is no ref to restore and nothing to undo — `merge-tree --write-tree`,
# the way to ask whether two branches still merge, is as unremarkable as
# `merge-base`, and refusing it refuses the question rather than the write.
#
# What fails the criterion is absent on purpose and meets git's own deny:
# `read-tree` and `update-index` write the index, `update-ref` and `pack-refs`
# move refs, and `format-patch`, `unpack-file`, and `difftool --dir-diff` each
# land files in the working tree.
# lup: ignore[library-default] — git's own query and object-construction verbs, taken by the criterion above; which of them reaches a ref is a fact about git rather than a choice made for an adopter
GIT_READ_ONLY_SUBCOMMANDS = (
    # Reporting on the object store, the refs, the index, and the config.
    "status",
    "rev-parse",
    "ls-files",
    "ls-tree",
    "ls-remote",
    "cat-file",
    "blame",
    "annotate",
    "describe",
    "rev-list",
    "name-rev",
    "merge-base",
    # lup: solved: This toolchain should carry an automatic sandbox escalation rather
    # than each caller remembering a flag. Every `lup-devtools` command that
    # opens agent sessions is broken inside the sandbox, and the resolver is
    # only the case that cost 90 minutes to notice. Whatever mechanism lands —
    # a declared escape list, or the sandbox axis on a decision — the toolchain
    # that runs the workflow should be on it by declaration.
    #
    # lup: solved: A command that will fail this way should be *blocked*, with the
    # reason, rather than allowed to run and die on a bare `EROFS`. The agent
    # cannot tell that failure from a broken repository, so it retries, works
    # around it, or reports success from a session that never ran a command.
    # A denial naming the sandbox and the escape costs one turn; this cost a
    # whole planning run whose output looked normal.
    # lup: solved: `git merge-tree` belongs on this list and is missing, so
    # probing whether a branch still merges is refused as "not classified as
    # read-only or reversible". Even with `--write-tree` it only adds objects
    # to the store: no ref, no index, no working tree. Sweep the rest of git's
    # query verbs the same way rather than adding this one word.
    "show-ref",
    "for-each-ref",
    "count-objects",
    "cherry",
    "check-ignore",
    "check-attr",
    "check-mailmap",
    "check-ref-format",
    "column",
    "fmt-merge-msg",
    "show-branch",
    "show-index",
    "verify-commit",
    "verify-tag",
    "verify-pack",
    "pack-redundant",
    "fsck",
    "patch-id",
    "request-pull",
    "stripspace",
    "get-tar-commit-id",
    "var",
    "version",
    "help",
    # Constructing an object, which no ref yet points at.
    "merge-tree",
    "hash-object",
    "commit-tree",
    "mktree",
    "mktag",
    "write-tree",
)

# lup: ignore[library-default] — git subcommands the reflog or a second invocation undoes; fixed by what git records rather than by taste
GIT_REVERSIBLE_SUBCOMMANDS = (
    "add",
    "commit",
    "mv",
    "cherry-pick",
    "revert",
    "merge",
    "notes",
    "stage",
)


def git_rule(
    guard_force_push: bool = True,
    redirect_checkout: bool = False,
    sandbox: SandboxPlacement = "outside",
) -> ShellCommandRule:
    """Compile the git surface: reads and reversible work allow, losses ask.

    Two judgements a project can reasonably differ on are parameters rather
    than a reason to fork the table.

    ``guard_force_push`` decides whether replacing what a remote ref points
    at is worth a question. It usually is. A project whose review flow
    rebases and republishes a branch every round answers no, because there
    the force is the ordinary case and the ask lands on nearly every push.
    What removes a ref outright stays guarded either way: no second push
    restores it.

    ``redirect_checkout`` decides how ``git checkout`` is met. Off, it asks —
    the branch-switching form is harmless, but ``checkout -- <path>``
    discards work. On, it denies and names ``switch`` and ``restore``
    instead, which suits a project that has settled on the newer verbs. The
    ref-sourced ``checkout <ref> -- <path>`` form is recognized by the kernel
    ahead of this row either way, because committed content stays
    recoverable.

    ``sandbox`` is where git runs, stated once here and inherited by every
    subcommand. It is the other axis rather than another effect: a fetch
    confined to a sandbox with no route to the remote fails however freely it
    was allowed, and a worktree or config write confined away from the
    repository's own locks fails the same way, so the placement follows the
    tool rather than a list of its verbs. A project whose sandbox does reach
    its remotes answers ``escalable`` instead, which keeps the ordinary fetch
    confined and leaves the way out to the agent that finds it needs one.
    """
    leaf = [
        ShellSubcommandRule(name=name, effect="allow")
        for name in (*GIT_READ_ONLY_SUBCOMMANDS, *GIT_REVERSIBLE_SUBCOMMANDS)
    ]
    push_flags = ["--delete", "--mirror", "--prune"]
    guarded = [
        *[
            ShellSubcommandRule(
                name=name,
                effect="allow",
                ask_flags=["--output"],
                reason="writing command output to a file requires approval",
            )
            # `--output` names a path on the command line and lands a file
            # there, which is the clause that disqualified `format-patch` from
            # the query family above. The plumbing spellings are not a quieter
            # kind of read: `diff-tree --output=` lands a file exactly where
            # `log --output=` does.
            #
            # The guard has to follow forwarding rather than only the verbs
            # that document the flag, because several reach it by handing their
            # arguments to `log` or `diff` — `stash list`, `stash show` and
            # `bisect view` carry it for that reason, on their own rows below.
            # Where a verb forwards is a fact about git worth checking against
            # its source; guarding one that turns out to reject the flag costs
            # nothing, since git refuses it either way.
            #
            # `--ext-diff` and `--textconv` are deliberately absent, by the
            # same reasoning that leaves `--paginate` alone: neither names a
            # program. They enable a driver already configured, and reaching
            # that configuration means `-c` or `git config`, which ask. That is
            # what separates them from `rg --pre`, which takes its program as
            # the next word.
            for name in (
                "log",
                "diff",
                "show",
                "whatchanged",
                "diff-tree",
                "diff-index",
                "diff-files",
                "diff-pairs",
                "range-diff",
                "shortlog",
            )
        ],
        ShellSubcommandRule(
            name="grep",
            effect="allow",
            ask_flags=["-O", "--open-files-in-pager"],
            reason="opening matches in an arbitrary program requires approval",
        ),
        ShellSubcommandRule(
            name="rebase",
            effect="allow",
            ask_flags=["-x", "--exec"],
            reason="replaying commits through a shell command requires approval",
        ),
        ShellSubcommandRule(
            name="fetch",
            effect="allow",
            ask_flags=["--upload-pack"],
            reason="overriding the transport program requires approval",
        ),
        ShellSubcommandRule(
            name="pull",
            effect="allow",
            ask_flags=["--upload-pack"],
            reason="overriding the transport program requires approval",
        ),
        ShellSubcommandRule(
            name="push",
            effect="allow",
            ask_flags=(
                [*push_flags, "-f", "--force", "--force-with-lease"]
                if guard_force_push
                else push_flags
            ),
            reason=(
                "rewriting or removing a remote ref requires approval"
                if guard_force_push
                else "removing a remote ref requires approval"
            ),
        ),
        ShellSubcommandRule(
            name="clone",
            effect="ask",
            reason="cloning fetches external code — requires approval",
        ),
        ShellSubcommandRule(
            name="apply",
            effect="allow",
            ask_flags=["--unsafe-paths", "--build-fake-ancestor"],
            reason="a patch that writes outside the working area requires approval",
        ),
        ShellSubcommandRule(
            name="restore",
            effect="ask",
            reason="restoring files discards working-tree changes",
        ),
        ShellSubcommandRule(
            name="rm",
            effect="ask",
            reason="removing tracked files requires approval",
        ),
        ShellSubcommandRule(
            name="clean",
            effect="ask",
            reason="deleting untracked files is destructive — requires approval",
        ),
        ShellSubcommandRule(
            name="config",
            effect="ask",
            read_verbs=[
                "--get",
                "--get-all",
                "--get-regexp",
                "--get-urlmatch",
                "--get-color",
                "--get-colorbool",
                "--list",
                "-l",
            ],
            reason="git config can change how commands execute",
        ),
        ShellSubcommandRule(
            name="checkout",
            effect="deny" if redirect_checkout else "ask",
            reason=(
                "use git switch for branches or git restore for files"
                if redirect_checkout
                else "checkout can discard working-tree changes"
            ),
        ),
        ShellSubcommandRule(
            name="reflog",
            effect="allow",
            operations=[
                ShellOperationRule(
                    name="expire",
                    effect="ask",
                    reason="expiring reflog entries is destructive — requires approval",
                ),
                ShellOperationRule(
                    name="delete",
                    effect="ask",
                    reason="deleting reflog entries is destructive — requires approval",
                ),
            ],
        ),
        ShellSubcommandRule(
            name="branch",
            effect="allow",
            ask_flags=["-d", "-D", "--delete", "-m", "-M", "--move"],
            reason="deleting or moving a branch requires approval",
        ),
        ShellSubcommandRule(
            name="bisect",
            effect="ask",
            operations=[
                ShellOperationRule(name="log", effect="allow"),
                ShellOperationRule(
                    # Falls back to `git log` where no display is available,
                    # and hands it the arguments it was given.
                    name="view",
                    effect="allow",
                    ask_flags=["--output"],
                    reason="writing command output to a file requires approval",
                ),
            ],
            reason="a bisect step moves HEAD across commits",
        ),
        ShellSubcommandRule(
            name="submodule",
            effect="ask",
            operations=[
                ShellOperationRule(name="status", effect="allow"),
                ShellOperationRule(name="summary", effect="allow"),
            ],
            reason="submodule operations fetch and check out external code",
        ),
        ShellSubcommandRule(
            name="tag",
            effect="allow",
            ask_flags=["-d", "--delete"],
            reason="deleting a tag requires approval",
        ),
        ShellSubcommandRule(
            # The one query verb whose write form is spelled by arity rather
            # than by a flag: a second operand points the ref somewhere else.
            # The kernel recognizes the reading form ahead of this row.
            name="symbolic-ref",
            effect="ask",
            reason="pointing a symbolic ref somewhere else moves HEAD",
        ),
        ShellSubcommandRule(
            name="reset",
            effect="allow",
            ask_flags=["--hard", "--merge", "--keep"],
            reason="a working-tree-destroying reset requires approval",
        ),
        ShellSubcommandRule(
            name="switch",
            effect="allow",
            ask_flags=["-f", "--force", "--discard-changes"],
            reason="a force switch can discard working-tree changes",
        ),
        ShellSubcommandRule(
            name="worktree",
            effect="deny",
            operations=[
                ShellOperationRule(name="list", effect="allow"),
                ShellOperationRule(name="add", effect="allow"),
                ShellOperationRule(name="move", effect="allow"),
                ShellOperationRule(name="repair", effect="allow"),
                ShellOperationRule(
                    name="remove",
                    effect="ask",
                    reason="removing a worktree deletes it — requires approval",
                ),
                ShellOperationRule(
                    name="prune",
                    effect="ask",
                    reason="pruning worktrees is destructive — requires approval",
                ),
            ],
            reason="this worktree operation is not classified",
        ),
        ShellSubcommandRule(
            name="stash",
            effect="allow",
            operations=[
                ShellOperationRule(
                    # Forwards its arguments to `git log`, `--output` included.
                    name="list",
                    effect="allow",
                    ask_flags=["--output"],
                    reason="writing command output to a file requires approval",
                ),
                ShellOperationRule(
                    # Accepts any format git diff knows, `--output` included.
                    name="show",
                    effect="allow",
                    ask_flags=["--output"],
                    reason="writing command output to a file requires approval",
                ),
                ShellOperationRule(name="push", effect="allow"),
                ShellOperationRule(name="save", effect="allow"),
                ShellOperationRule(name="pop", effect="allow"),
                ShellOperationRule(name="apply", effect="allow"),
                ShellOperationRule(
                    name="drop",
                    effect="ask",
                    reason="dropping a stash is destructive — requires approval",
                ),
                ShellOperationRule(
                    name="clear",
                    effect="ask",
                    reason="clearing stashes is destructive — requires approval",
                ),
            ],
        ),
        ShellSubcommandRule(
            name="remote",
            effect="allow",
            operations=[
                ShellOperationRule(
                    name="remove",
                    effect="ask",
                    reason="removing a remote requires approval",
                ),
                ShellOperationRule(
                    name="rm",
                    effect="ask",
                    reason="removing a remote requires approval",
                ),
                ShellOperationRule(
                    name="set-url",
                    effect="ask",
                    reason="changing a remote URL requires approval",
                ),
                ShellOperationRule(
                    name="prune",
                    effect="ask",
                    reason="pruning a remote is destructive — requires approval",
                ),
            ],
        ),
    ]
    # A global that points git somewhere else is judged here rather than per
    # subcommand, because the subcommand word is found only after these are
    # read. A redirect left to `value_flags` alone would only advance the
    # parser past its argument, and every verb behind it would be answered by a
    # row reasoning about this worktree: `git -C /elsewhere commit` reads as
    # reversible because the reflog that undoes it is *here*. The redirect is
    # exactly what makes that premise someone else's.
    #
    # Only the three that name a directory are also in `value_flags`, which
    # selects the wording of the question rather than the parse — the ask is
    # reached before any value is skipped. Those three have a way through worth
    # naming, because `cd there && git status` is two allowed segments.
    # `--namespace` has none: it redirects refs rather than a path, and the
    # environment spelling of it is a guarded assignment of its own.
    #
    # `--paginate` is deliberately not among them, though it is on the list this
    # sweep was measured against. It moves no ref, no index entry, and no file:
    # it forces the pager these subcommands already run by default, and the
    # program that pager names is reachable only through `-c` or `git config`,
    # which ask. Gating it would spend a question on the flag rather than on
    # what the flag could reach.
    directory_flags = ["-C", "--git-dir", "--work-tree"]
    return ShellCommandRule(
        name="git",
        default_effect="deny",
        ask_flags=[
            "-c",
            "--config-env",
            "--exec-path",
            "--super-prefix",
            "--namespace",
            *directory_flags,
        ],
        value_flags=directory_flags,
        sandbox=sandbox,
        subcommands=[*leaf, *guarded],
        reason="this git subcommand is not classified as read-only or reversible",
    )


def gh_rule(allow_authoring: bool = True) -> ShellCommandRule:
    """Compile the gh surface: reads allow, judged mutations ask, else deny.

    ``allow_authoring`` decides whether opening a pull request, retitling it,
    and marking it ready are the author describing their own work or a
    publication worth a question. On, they allow — a review flow does all
    three every round, and the branch is already pushed by then. Off, they
    join the verbs that reach other people. Commenting, reviewing, merging
    and closing ask either way: those reach reviewers, or change what the
    repository is.

    Both halves of that grant are claims about *this* repository — the work is
    the author's own, and the branch is already pushed — and ``--repo`` is what
    makes them someone else's, so the authoring verbs carry it as a guard the
    way git's redirecting globals do. Spelled as a flag, the redirect is judged
    per verb, so reading another repository keeps its grant: a read is a read
    wherever it points.

    The flag is only half of it. ``GH_REPO`` and ``GH_HOST`` reach the same
    retarget through the environment, and that spelling is caught by the
    dangerous-assignment prefixes in :mod:`lup.policy.kernel.words` rather than
    here — a guard that reads the assignment before any verb is known, so
    unlike the flag it stops a redirected read as well. The asymmetry is the
    price of catching the variable gh has not learned yet.
    """
    authoring = ["create", "edit", "ready"]
    elsewhere = ["-R", "--repo"]

    def group(
        name: str,
        allowed: list[str],
        asked: list[str] | None = None,
        authored: list[str] | None = None,
    ) -> ShellSubcommandRule:
        return ShellSubcommandRule(
            name=name,
            effect="deny",
            operations=[
                *[
                    ShellOperationRule(name=operation, effect="allow")
                    for operation in allowed
                ],
                *[
                    ShellOperationRule(
                        name=operation,
                        effect="allow",
                        ask_flags=elsewhere,
                        reason=f"gh {name} {operation} against another"
                        " repository requires approval",
                    )
                    for operation in authored or []
                ],
                *[
                    ShellOperationRule(
                        name=operation,
                        effect="ask",
                        reason=f"gh {name} {operation} changes remote state"
                        " — requires approval",
                    )
                    for operation in asked or []
                ],
            ],
            reason=f"this gh {name} operation is not classified",
        )

    return ShellCommandRule(
        name="gh",
        default_effect="deny",
        subcommands=[
            group(
                "pr",
                ["list", "view", "diff", "status", "checks", "checkout"],
                [
                    "comment",
                    "review",
                    "merge",
                    "close",
                    *([] if allow_authoring else authoring),
                ],
                authoring if allow_authoring else [],
            ),
            group(
                "issue",
                ["list", "view", "status"],
                ["create", "edit", "comment", "close"],
            ),
            group("run", ["list", "view", "watch"], ["rerun", "cancel", "download"]),
            group("repo", ["view", "list"], ["clone", "fork"]),
            group("release", ["list", "view"], ["create", "upload"]),
            group("cache", ["list"], ["delete"]),
            group("workflow", ["list", "view"], ["run", "enable", "disable"]),
            group("auth", ["status"]),
            group("search", ["repos", "issues", "prs", "code", "commits"]),
            group("label", ["list"], ["create", "edit", "delete", "clone"]),
            group("gist", ["list", "view"], ["create", "edit", "delete", "clone"]),
            group(
                "project",
                ["list", "view", "item-list", "field-list"],
                ["create", "edit", "close", "delete", "item-add", "item-delete"],
            ),
            group("variable", ["list", "get"], ["set", "delete"]),
            group("ruleset", ["list", "view", "check"]),
            group("config", ["get", "list"], ["set"]),
            ShellSubcommandRule(name="status", effect="allow"),
            ShellSubcommandRule(name="browse", effect="allow"),
            ShellSubcommandRule(
                # The default method is GET, so the unguarded form is a read.
                # Every way of making it something else — naming a method,
                # attaching a field, reading a body from a file — is guarded,
                # which leaves the mutation ask exactly where it belongs
                # instead of on every query that shares the subcommand.
                name="api",
                effect="allow",
                ask_flags=[
                    "-X",
                    "--method",
                    "-f",
                    "--field",
                    "-F",
                    "--raw-field",
                    "--input",
                ],
                reason="gh api can mutate anything — requires approval",
            ),
        ],
        reason="this gh command is not classified",
    )


def docker_rule() -> ShellCommandRule:
    """Compile the docker surface: queries allow, everything else asks.

    Nothing here is a parameter because the split is not a judgement: a verb
    either reports on containers, images, volumes and the daemon, or changes
    one of them. An unclassified or expansion-obscured subcommand keeps the
    ask rather than falling through to a grant.
    """

    def noun(name: str, verbs: list[str]) -> ShellSubcommandRule:
        return ShellSubcommandRule(
            name=name,
            effect="ask",
            operations=[
                ShellOperationRule(name=verb, effect="allow") for verb in verbs
            ],
            reason="container operations require approval",
        )

    queries = [
        ShellSubcommandRule(name=name, effect="allow")
        for name in (
            "info",
            "version",
            "ps",
            "images",
            "inspect",
            "logs",
            "top",
            "port",
            "diff",
            "history",
            "stats",
            "events",
        )
    ]
    return ShellCommandRule(
        name="docker",
        default_effect="ask",
        subcommands=[
            *queries,
            noun(
                "container", ["ls", "inspect", "logs", "top", "port", "diff", "stats"]
            ),
            noun("image", ["ls", "inspect", "history"]),
            noun("volume", ["ls", "inspect"]),
            noun("network", ["ls", "inspect"]),
            noun("context", ["ls", "show", "inspect"]),
            noun("system", ["df", "info", "events"]),
        ],
        reason="container operations require approval",
    )


def default_vocabulary() -> list[ShellCommandRule]:
    """Every group at its offered defaults — the batteries-included table.

    A project with no opinion yet composes this and gets a working agent; one
    that has an opinion replaces the groups it differs on rather than this
    call.
    """
    return [
        *read_only_rules(),
        *judged_ask_rules(),
        *redirected_rules(),
        *guarded_tool_rules(),
        git_rule(),
        gh_rule(),
        docker_rule(),
    ]
