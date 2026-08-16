"""The offered vocabulary decides, and its parameters move exactly one verdict.

A group that shipped nothing was not neutral: an empty table matches no
command, an unmatched command is unjudged, and unjudged resolves to a deny.
The first test pins that the defaults produce a usable agent rather than one
that refuses to list a directory.

The rest pin the parameters. Each exists because a reasonable project answers
differently, so each has to change the verdict it names and leave its
neighbours alone — a parameter that also moved something else would be a fork
wearing an argument's clothes.
"""

from lup.policy.kernel.decision import KernelDecision
from lup.policy.kernel.shell import decide_shell
from lup.policy.shell_rules import ShellCommandRule, erase_shell_rules
from lup.policy.vocabulary import (
    default_vocabulary,
    gh_rule,
    git_rule,
    read_only_rules,
)


def verdict(command: str, rules: list[ShellCommandRule]) -> KernelDecision:
    """Classify one command against a composed vocabulary."""
    return decide_shell(command, erase_shell_rules(rules))


def test_the_offered_defaults_produce_an_agent_that_can_read() -> None:
    """Shipping no vocabulary denied every command, which is not no policy."""
    rules = default_vocabulary()

    assert verdict("ls -la", rules).effect == "allow"
    assert verdict("grep -rn needle src", rules).effect == "allow"
    assert verdict("git status", rules).effect == "allow"
    assert verdict("gh pr list", rules).effect == "allow"
    # Generosity for reads is not generosity for losses.
    assert verdict("rm notes.md", rules).effect == "ask"
    assert verdict("sudo apt install x", rules).effect == "ask"
    # And an empty table is the verdict the library used to ship by default.
    assert verdict("ls -la", []).effect == "deny"


def test_git_s_object_store_queries_are_reads() -> None:
    """Probing a merge is how an agent checks a branch before touching it.

    `git merge-tree` was refused as "not classified as read-only or
    reversible" while the resolver's own refresh ran it to predict every
    lease merge — so nothing an agent could run reproduced what the tool
    it was operating had just decided.
    """
    rules = default_vocabulary()

    assert verdict("git merge-tree --write-tree dev HEAD", rules).effect == "allow"
    assert verdict("git hash-object -w blob.py", rules).effect == "allow"
    assert verdict("git patch-id --stable", rules).effect == "allow"
    assert verdict("git check-ref-format --branch x", rules).effect == "allow"
    assert verdict("git verify-pack -v pack.idx", rules).effect == "allow"


def test_sweeping_the_queries_left_what_loses_work_alone() -> None:
    """A read-only sweep that widened a destructive verb would be a bug."""
    rules = default_vocabulary()

    assert verdict("git push --delete origin dev", rules).effect == "ask"
    assert verdict("git reset --hard HEAD~1", rules).effect == "ask"
    assert verdict("git clean -fdx", rules).effect == "ask"


def test_an_empty_group_replaces_the_offered_words_rather_than_adding_to_them() -> None:
    """The words are a parameter default, so passing any replaces all of them."""
    assert verdict("ls", read_only_rules()).effect == "allow"
    assert verdict("ls", read_only_rules(["cat"])).effect == "deny"
    assert verdict("cat f", read_only_rules(["cat"])).effect == "allow"


def test_guard_force_push_moves_only_the_rewriting_push() -> None:
    """A rebase flow republishes every round, so the force is the ordinary case.

    Removing a ref is guarded either way: no second push restores it.
    """
    guarded = [git_rule()]
    open_flow = [git_rule(guard_force_push=False)]

    assert verdict("git push --force origin HEAD", guarded).effect == "ask"
    assert verdict("git push --force origin HEAD", open_flow).effect == "allow"
    assert verdict("git push -f origin HEAD", guarded).effect == "ask"
    assert verdict("git push -f origin HEAD", open_flow).effect == "allow"
    # Neither the plain push nor the removing one moves with the parameter.
    assert verdict("git push -u origin HEAD", guarded).effect == "allow"
    assert verdict("git push -u origin HEAD", open_flow).effect == "allow"
    assert verdict("git push --delete origin old", guarded).effect == "ask"
    assert verdict("git push --delete origin old", open_flow).effect == "ask"


def test_redirect_checkout_chooses_between_asking_and_naming_the_newer_verbs() -> None:
    """Off, checkout asks because `checkout -- <path>` discards work.

    On, it denies and says which verbs replaced it. The ref-sourced form is
    recognized ahead of the row either way, because a named commit still
    holds the content.
    """
    asking = [git_rule()]
    redirecting = [git_rule(redirect_checkout=True)]

    assert verdict("git checkout main", asking).effect == "ask"
    assert verdict("git checkout main", redirecting).effect == "deny"
    assert "git switch" in verdict("git checkout main", redirecting).reason
    assert verdict("git checkout HEAD~1 -- src/x.py", asking).effect == "allow"
    assert verdict("git checkout HEAD~1 -- src/x.py", redirecting).effect == "allow"


def test_the_git_family_is_drawn_by_what_a_verb_reaches_not_by_what_it_writes() -> None:
    """One criterion decides the table: no ref, no index, no working tree.

    Asking whether a subcommand writes bytes draws the line in the wrong
    place. `merge-tree --write-tree` writes an object and is how a session
    asks whether two branches still merge, while `read-tree` writes nothing
    a caller sees and replaces the index wholesale. What separates them is
    reach, so the sweep is by reach — and a word arriving on the list for any
    other reason is what this pins against.
    """
    rules = [git_rule()]

    def effect(command: str) -> str:
        return verdict(command, rules).effect

    # Constructing an object moves no ref, so nothing points at the result and
    # nothing needs undoing.
    assert effect("git merge-tree --write-tree main dev") == "allow"
    assert effect("git hash-object -w README.md") == "allow"
    assert effect("git commit-tree -m x HEAD^{tree}") == "allow"
    # Each of the three clauses, refused by a verb that trips only that one.
    assert effect("git read-tree HEAD") == "deny"
    assert effect("git update-ref refs/heads/x HEAD") == "deny"
    assert effect("git format-patch HEAD~1") == "deny"
    # A ref write spelled as a second operand is still a ref write.
    assert effect("git symbolic-ref HEAD") == "allow"
    assert effect("git symbolic-ref HEAD refs/heads/topic") == "ask"
    assert effect("git symbolic-ref --delete HEAD") == "ask"
    # Passing the criterion settles the effect, never the placement: a summary
    # of what a remote would pull is built by asking that remote, so it needs
    # the route ls-remote needs rather than a confinement it dies inside.
    outside = verdict("git request-pull main https://x.test/r HEAD", rules)
    assert (outside.effect, outside.sandbox) == ("allow", "outside")


def test_a_global_that_moves_git_to_another_tree_is_judged_before_the_verb() -> None:
    """The criterion is about refs, index and working tree — not about whose.

    Naming a redirect in `value_flags` alone only advanced the parser past its
    argument, so the verb behind it was answered by a row reasoning about this
    worktree: `commit` allowed because the reflog that undoes it is here. The
    redirect is exactly what makes that premise someone else's, and it is read
    before the subcommand word is found, so it has to answer for itself.
    """
    rules = [git_rule()]

    def effect(command: str) -> str:
        return verdict(command, rules).effect

    assert effect("git -C /tmp/other commit -am x") == "ask"
    assert effect("git -C /tmp/o merge --abort") == "ask"
    assert effect("git --git-dir=/tmp/x --work-tree=/tmp add .") == "ask"
    assert effect("git --namespace=other push") == "ask"
    # The refusal names the way through rather than leaving it to be guessed —
    # but only where one exists. A namespace is not a directory, so offering to
    # cd into it would send an agent somewhere it cannot go.
    assert "cd into that tree" in verdict("git -C /tmp/o status", rules).reason
    assert "cd into" not in verdict("git --namespace=o push", rules).reason
    # Forcing the pager moves nothing, and the program it names is reachable
    # only through the globals that already ask.
    assert effect("git --paginate diff") == "allow"
    assert effect("git --no-pager log") == "allow"


def test_allow_authoring_moves_only_the_author_describing_their_own_work() -> None:
    """Opening and titling a PR is authoring; commenting reaches other people."""
    authoring = [gh_rule()]
    publishing = [gh_rule(allow_authoring=False)]

    assert verdict("gh pr create --fill", authoring).effect == "allow"
    assert verdict("gh pr create --fill", publishing).effect == "ask"
    assert verdict("gh pr ready", authoring).effect == "allow"
    assert verdict("gh pr ready", publishing).effect == "ask"
    # Reads and the verbs that reach reviewers stay where they were.
    assert verdict("gh pr view 12", authoring).effect == "allow"
    assert verdict("gh pr view 12", publishing).effect == "allow"
    assert verdict("gh pr merge 12", authoring).effect == "ask"
    assert verdict("gh pr merge 12", publishing).effect == "ask"
    # The grant says the work is the author's own and the branch is already
    # pushed. Pointing the verb at another repository denies both, under either
    # setting of the parameter — and a read there is still just a read.
    assert verdict("gh pr create -R other/victim --fill", authoring).effect == "ask"
    assert verdict("gh pr create -R other/victim --fill", publishing).effect == "ask"
    assert verdict("gh pr view -R other/repo 12", authoring).effect == "allow"
