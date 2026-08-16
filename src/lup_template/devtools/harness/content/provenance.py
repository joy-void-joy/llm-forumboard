"""What a project settles about where its lup came from.

Standing a project up on the template and installing the plugin into a
repository that already exists ask the same three questions — which branch of
the library the project builds on, how it obtains the distribution, and which
commit its upstream checkpoint records. The answers are the same answers, so
they are written once here and composed by both skills. Two skills restating
them is a pair that drifts, and for these the drift is not hypothetical: the
repository mode is the only mode that resolves while nothing is published, so
a copy that omits it leaves an agent to discover the constraint by failing.

Only the checkouts differ. Installing runs in the library's checkout and writes
to a different repository, so it spells both; initializing runs in the checkout
it is turning into a project, where the two are one directory and naming it
would be noise. That difference is a :class:`Provenance`, and it is the whole
of what a caller supplies.

Everything that is not this question stays with the skill that asks it.
Installing reads a repository it did not write and must leave its own checkout
untouched; initializing knows its checkout is a fresh template clone, renames
the package before anything is allowed to un-vendor it, and interviews a
domain. Framing sentences stay with their skill too, placed before the shared
block rather than folded into it: an addition cannot drift from what it adds
to, while a restatement can.
"""

from pydantic import BaseModel

import lup.harness.models as models

from lup.markdown import contained


class Provenance(BaseModel, frozen=True):
    """How one skill spells the checkouts a provenance answer is about."""

    library_git: str
    """Git run against the checkout the library comes from."""

    project_devtools: str
    """The devtools CLI run against the repository being written to."""

    library_checkout: str
    """That library checkout, as ``sync setup`` is handed it."""


# lup: ignore[model-free-function] — the section is the subject and Provenance is
# how it spells the commands inside it: behaviour goes on a model when the model
# is what the operation is about, not when it is how the prose calls in
def branch_probes(spelling: Provenance) -> list[models.PromptPart]:
    """The two refs that settle which library this is, and the ask between them."""
    return [
        models.TextPart(
            text=rf"""- `{spelling.library_git} rev-parse --abbrev-ref HEAD` — the branch the library would come from
- `{spelling.library_git} symbolic-ref --short refs/remotes/origin/HEAD` — what the remote treats as stable

When they differ, """
        ),
        models.AskUser(
            question="whether to proceed from the checkout's current branch, "
            "which carries work the stable branch has not reviewed, or from the "
            "stable branch instead"
        ),
        models.TextPart(
            text=r"""

Record the branch and the commit the answer settles on. Everything below is
about that commit — the acquisition mode pins its branch and the upstream
checkpoint is taken at it — so the checkout supplying the library has to be
standing there before you go on.

"""
        ),
    ]


# lup: ignore[model-free-function] — the same: a section of the skill, spelled
# with those commands rather than describing them
def acquisition(spelling: Provenance) -> list[models.PromptPart]:
    """The look-up that settles which mode the project resolves ``lup`` through."""
    # A row is split on `|` before its cells are parsed, so the one value that
    # flows into this table is contained where it lands in one. The fence and
    # the prose below take it as it reads: nothing there ends a row.
    celled = contained(spelling.project_devtools)
    return [
        models.TextPart(
            text=rf"""A project depends on `lup` as a package rather than keeping a copy of the
library's source. Half the answer is a fact to look up rather than a
preference — whether a release exists at all, and which:

```
{spelling.project_devtools} dev library release
```

It reports the released version, or that none is published yet, and prints the
command that declares what it found — so the release number is read from the
index rather than guessed at.

The other half is a judgement about what this project is to lup, and the
look-up does not make it. Ask the user which of these describes them:

| Mode | The project it is for | Command |
| --- | --- | --- |
| published | A consumer of the library: it takes releases and upgrades on its own schedule | `{celled} dev library use published --version <release>` |
| **git** | Either nothing is published yet, or the project works *on* lup as well as with it — running a branch to dogfood it and sending changes back | `{celled} dev library git --branch <branch>` |
| linked | The library is being developed alongside this project, in a checkout on the same disk | `{celled} dev library link <checkout>` |

With nothing published, git is the only mode that resolves, so the look-up
settles it. Once a release exists, published is the quieter default and git
stays a live choice: a project that reads the library's own diffs, or that
expects to send work back, is better served by the branch it is improving than
by the last release cut from it. All three hand the project a real package, so
its `packages/lup/` stays absent and nothing has to be merged later. Vendoring
is not on this list — a vendored copy is a fork with all the reconciliation
that implies, and is only right for a project that genuinely intends to modify
library source.

The git mode resolves `subdirectory = "packages/lup"`, because the distribution sits inside the repository rather than at its root, and pins whichever ref you name. **The ref resolves against the remote, not against any checkout on disk**: uv fetches the branch as the remote has it, so work the remote has not seen is not in what you pinned. Before declaring a git source, read what the remote's branch actually resolves to — `{spelling.library_git} ls-remote origin <branch>` names that tip — and if it is not the recorded commit, say so rather than pinning a dependency whose contents you have not accounted for.

The extras come from what the project runs: `claude` and/or `codex` for the
adapters it drives, `docker` for the code-execution sandbox, `web` for the
session API. Name them in the requirement (`lup[claude,codex,docker]`).

"""
        ),
    ]


# lup: ignore[model-free-function] — likewise the checkpoint section, which the
# spellings are rendered into rather than being about
def sync_baseline(spelling: Provenance) -> list[models.PromptPart]:
    """Where the upstream checkpoint is taken, and why the short way misses it."""
    return [
        models.TextPart(
            text=rf"""Baseline the upstream checkpoint at *the recorded commit*, not at whatever the
remote's default branch points to. `--synced` reads the checkpoint from the
named checkout's HEAD, so that checkout has to be standing at the recorded
commit when this runs:

```
{spelling.project_devtools} sync setup lup {spelling.library_checkout} --branch <branch> --synced
```

`setup` records that checkout, the branch settled on above, and its HEAD as the checkpoint, so `"""
        ),
        models.SkillInvocation(plugin="lup", skill="update"),
        models.TextPart(
            text=r"""` only shows commits that land afterward. Plain `sync mark-synced lup` is wrong here: the shipped `sync.json` entry carries a URL and no branch, so it clones the remote's default branch and checkpoints *that* HEAD — so every commit the project already carries comes back as unported work once the branch merges.

"""
        ),
    ]
