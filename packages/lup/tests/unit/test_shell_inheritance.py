"""A shell rule's axes cascade, and spelling one out changes no verdict.

Absence means one thing everywhere in the table: inherit from the level above,
never the most permissive value. That is what lets a placement wanted for a
whole command be stated once instead of repeated per subcommand, and what
makes a command's effect constrain the subcommands it has not judged.

Reading absence that way is only safe where a table states every judgement it
means rather than leaving one to a default, so the offered vocabulary spells
its effects out: `git branch` says `allow` beneath a deny-default `git` rather
than leaning on the omission to say it. The last test here is the one that
matters — the only forms escaping the sandbox are the git forms that reach a
verdict, because `git` declares its placement at the command its verbs share.
"""

from lup.adapters.codex.harness import codex_allow_prefixes
from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.shell import auto_escape_matches, decide_shell
from lup.policy.shell_rules import (
    ROOT_EFFECT,
    ROOT_SANDBOX,
    ShellCommandRule,
    ShellOperationRule,
    ShellSubcommandRule,
    erase_shell_rules,
)
from lup.policy.survey import classify_forms, survey_shell_rules
from lup.policy.vocabulary import default_vocabulary, git_rule

ALLOWED_UNDER_A_RESTRICTIVE_PARENT = (
    # Every subcommand the offered vocabulary allows beneath a parent that
    # denies or asks. Each is where an omission would be read as inheritance
    # and flip the verdict, so each states its own effect and this pins that
    # the whole set still allows.
    "git status",
    "git rev-parse",
    "git ls-files",
    "git ls-tree",
    "git ls-remote",
    "git cat-file",
    "git blame",
    "git annotate",
    "git describe",
    "git shortlog",
    "git rev-list",
    "git name-rev",
    "git merge-base",
    # Reporting on the store, the refs, the index and the config, and the
    # object-construction verbs beside them: each reaches the store and stops
    # there — no ref moves, no index entry changes, no working-tree file is
    # touched — which is the one criterion the vocabulary sweeps git by.
    "git column",
    "git fmt-merge-msg",
    "git fsck",
    "git patch-id",
    "git request-pull",
    "git verify-pack",
    "git pack-redundant",
    "git show-index",
    "git get-tar-commit-id",
    "git check-ref-format",
    "git stripspace",
    "git merge-tree",
    "git hash-object",
    "git commit-tree",
    "git mktree",
    "git mktag",
    "git write-tree",
    # Emitting a diff, where only `--output` lands a file and is guarded.
    "git diff-pairs",
    "git show-ref",
    # `symbolic-ref` is deliberately absent: its write form is spelled by a
    # second operand rather than by a flag, so the bare verb asks and only the
    # reading form — which the kernel recognizes ahead of that row — allows.
    "git for-each-ref",
    "git count-objects",
    "git cherry",
    "git range-diff",
    "git diff-tree",
    "git diff-index",
    "git diff-files",
    "git check-ignore",
    "git check-attr",
    "git check-mailmap",
    "git show-branch",
    "git verify-commit",
    "git verify-tag",
    "git var",
    "git version",
    "git help",
    "git add",
    "git commit",
    "git mv",
    "git cherry-pick",
    "git revert",
    "git merge",
    "git notes",
    "git stage",
    "git log",
    "git diff",
    "git show",
    "git whatchanged",
    "git grep",
    "git rebase",
    "git fetch",
    "git pull",
    "git push",
    "git apply",
    "git branch",
    "git tag",
    "git reset",
    "git switch",
    "git reflog",
    "git stash",
    "git remote",
    "gh status",
    "gh browse",
    "gh api",
    "docker info",
    "docker version",
    "docker ps",
    "docker images",
    "docker inspect",
    "docker logs",
    "docker top",
    "docker port",
    "docker diff",
    "docker history",
    "docker stats",
    "docker events",
)


def verdict(command: str, rules: list[ShellCommandRule]) -> KernelDecision:
    """Classify one command against a composed vocabulary."""
    return decide_shell(command, erase_shell_rules(rules))


def test_a_value_declared_once_reaches_every_level_beneath_it() -> None:
    """`git` says where it runs once, and the verb three levels down inherits it.

    Enumerating only the verbs that open a transport leaves every other one
    confined, which is where a worktree add meets the repository's own config
    lock from inside the sandbox — so the placement follows the tool instead.
    """
    rules = [git_rule()]

    assert verdict("git status", rules).sandbox == "outside"
    assert verdict("git worktree add ../tree", rules).sandbox == "outside"
    assert verdict("git config --get user.name", rules).sandbox == "outside"
    # And the placement a caller passes is the one that cascades.
    confined = [git_rule(sandbox="inside")]
    assert verdict("git worktree add ../tree", confined).sandbox == "inside"


def test_a_declaration_overrides_what_it_inherited_in_either_direction() -> None:
    """Widening a restrictive parent is as ordinary as narrowing a permissive one.

    Nothing compares the two values, so an explicit statement wins even when
    the level above was stricter — a deny-default command with an allowed
    query verb under it is the ordinary shape, not an error to forbid.
    """
    widening = [
        ShellCommandRule(
            name="tool",
            default_effect="deny",
            sandbox="inside",
            subcommands=[
                ShellSubcommandRule(name="show", effect="allow", sandbox="outside")
            ],
        )
    ]
    narrowing = [
        ShellCommandRule(
            name="tool",
            default_effect="allow",
            sandbox="outside",
            subcommands=[
                ShellSubcommandRule(name="wipe", effect="ask", sandbox="inside")
            ],
        )
    ]

    assert verdict("tool show", widening).effect == "allow"
    assert verdict("tool show", widening).sandbox == "outside"
    assert verdict("tool other", widening).effect == "deny"
    assert verdict("tool wipe", narrowing).effect == "ask"
    assert verdict("tool wipe", narrowing).sandbox == "inside"
    assert verdict("tool read", narrowing).effect == "allow"


def test_omission_inherits_at_every_depth_including_an_operation() -> None:
    """One meaning for absence: inherit, never the most permissive value."""
    rules = [
        ShellCommandRule(
            name="tool",
            default_effect="ask",
            sandbox="outside",
            subcommands=[
                ShellSubcommandRule(
                    name="area", operations=[ShellOperationRule(name="poke")]
                )
            ],
        )
    ]

    assert verdict("tool area poke", rules).effect == "ask"
    assert verdict("tool area poke", rules).sandbox == "outside"
    assert verdict("tool area", rules).effect == "ask"


def test_stating_the_value_a_level_would_have_inherited_still_counts() -> None:
    """Declared and defaulted are different, or a level cannot pin what it got.

    A subcommand that means `ambient` under an `outside` command has to be able
    to say so, and saying so is spelling the word the model would also have used
    had nobody spoken.
    """
    rules = [
        ShellCommandRule(
            name="tool",
            default_effect="deny",
            sandbox="outside",
            subcommands=[
                ShellSubcommandRule(name="here", effect="allow", sandbox=ROOT_SANDBOX),
                ShellSubcommandRule(name="there", effect="allow"),
            ],
        )
    ]
    surveyed = {rule.path: rule for rule in survey_shell_rules(rules)}

    assert verdict("tool here", rules).sandbox == "ambient"
    assert verdict("tool there", rules).sandbox == "outside"
    assert surveyed["tool here"].sandbox_source == "subcommand"
    assert surveyed["tool there"].sandbox_source == "command"


def test_a_row_says_which_level_supplied_each_half_of_its_verdict() -> None:
    """Provenance a reader can print, and never a second source of the decision."""
    surveyed = {rule.path: rule for rule in survey_shell_rules([git_rule()])}

    assert surveyed["git worktree add"].effect_source == "operation"
    assert surveyed["git worktree add"].sandbox_source == "command"
    assert surveyed["git worktree add"].provenance() == (
        "git worktree add: allow (declared here),"
        " runs outside (inherited from the command)"
    )
    assert surveyed["git"].provenance() == (
        "git: deny (declared here), runs outside (declared here)"
    )


def test_a_table_that_declares_nothing_about_placement_reaches_the_root() -> None:
    """Absence on the placement axis is a statement, so no command repeats it."""
    surveyed = survey_shell_rules([ShellCommandRule(name="ls", default_effect="allow")])

    assert surveyed[0].sandbox == ROOT_SANDBOX
    assert surveyed[0].sandbox_source == "root"
    assert surveyed[0].effect_source == "command"
    # Who decides has no member meaning "no opinion", so the command declares
    # it and the root fallback beneath every table refuses rather than grants.
    assert ROOT_EFFECT == "deny"


def test_every_subcommand_allowed_beneath_a_restrictive_parent_allows() -> None:
    """A judged subcommand states its effect rather than inheriting its parent's."""
    rules = default_vocabulary()

    for path in ALLOWED_UNDER_A_RESTRICTIVE_PARENT:
        assert verdict(path, rules).effect == "allow", path


def test_the_enumeration_above_is_the_table_s_own_and_cannot_fall_behind() -> None:
    """Every such subcommand is listed, so none is pinned only by accident.

    A hand-kept list that misses a row says nothing about that row, and the one
    it misses is exactly the one a reshaping moves unnoticed.
    """
    rows = erase_shell_rules(default_vocabulary())
    parents = {row["command"]: row["effect"] for row in rows if not row["subcommand"]}
    derived = sorted(
        f"{row['command']} {row['subcommand']}"
        for row in rows
        if row["subcommand"]
        and not row["operation"]
        and row["effect"] == "allow"
        and parents[row["command"]] != "allow"
    )

    assert derived == sorted(ALLOWED_UNDER_A_RESTRICTIVE_PARENT)


def test_a_native_prefix_reads_the_resolved_effect_and_not_the_declared_one() -> None:
    """The other surface compiled from a table resolves the cascade identically.

    Codex prefix rules are the second thing a table compiles into, so reading
    the declarations there would let an inherited effect mean one thing to the
    dispatcher and another to the native runtime — the disagreement resolving
    at erasure exists to prevent.
    """
    inheriting = [
        ShellCommandRule(
            name="tool",
            default_effect="allow",
            sandbox="outside",
            subcommands=[ShellSubcommandRule(name="show")],
        )
    ]

    assert codex_allow_prefixes(inheriting, []) == [["tool", "show"]]


def test_a_confined_placement_is_never_widened_into_a_native_allow() -> None:
    """A prefix rule auto-approves an escape, which is what `inside` denies.

    The effect axis alone would admit this row, and an escaped call would run
    unconfined a call whose whole declaration is that it runs confined.
    """
    confined = [
        ShellCommandRule(
            name="tool",
            default_effect="allow",
            sandbox="inside",
            subcommands=[ShellSubcommandRule(name="show")],
        )
    ]

    assert codex_allow_prefixes(confined, []) == []


def test_native_auto_escape_matches_only_one_simple_command() -> None:
    prefixes = [["uv", "run", "lup-devtools"]]
    assert auto_escape_matches("uv run lup-devtools dev check", prefixes)
    assert not auto_escape_matches(
        "uv run lup-devtools dev check && echo done", prefixes
    )


def test_the_forms_escaping_the_sandbox_are_exactly_the_decided_git_ones() -> None:
    """A placement declared on one command reaches its verbs and nothing else.

    `git` is the only command in the offered table that states a placement, so
    the forms running outside are exactly the ones it decides. A refusal is
    unplaced, held there at construction rather than by this table, so the git
    forms that deny stay ambient too.
    """
    forms = classify_forms(default_vocabulary())
    escaping = [form for form in forms if form.sandbox != "ambient"]
    reached = [
        form
        for form in forms
        if form.command.startswith("git") and form.effect in ("allow", "ask")
    ]

    assert [form.sandbox for form in escaping] == ["outside"] * len(escaping)
    assert escaping == reached
