"""The git hook that refuses a commit while a generated artifact is stale.

A drift check that runs when somebody remembers to run it is a warning, not a
guarantee: the sources compiled into the native trees are copied there
verbatim, so rewording a comment in one of them makes both trees stale without
changing anything either does, and a commit that skips ``dev check`` writes
that staleness into history. This installs the check as a git ``pre-commit``
hook, so refusing it takes saying so — ``--no-verify`` — rather than
forgetting to ask.

The hook body is one line — :data:`DRIFT_COMMAND`, the same command the
pipeline runs as its first step — and that command reads the drift verdict
``dev check`` reads. So the three places that can refuse stale output run one
computation rather than three that can disagree, and a hook nobody installed
is still refused at the same line by the pipeline.
"""

from pathlib import Path
from typing import Literal

import sh
from pydantic import BaseModel

from lup.harness.banner import REGENERATE_COMMAND
from lup.devtools.utils import git

DRIFT_COMMAND = "uv run lup-devtools harness check all"
"""The read-only drift check every path that refuses stale output runs."""

# lup: ignore[constant-declaration] — the command a reader types, whose words
# are the CLI's own rather than a preference this module holds
INSTALL_COMMAND = "uv run lup-devtools dev commit-guard install"
"""How a checkout arms the guard, named in the hook it writes."""

# lup: ignore[constant-declaration] — the marker this command writes into a
# hook and reads back to know the hook is its own, so it is an identity rather
# than a setting: a caller changing it would orphan every hook already installed
GUARD_MARKER = "lup-commit-guard"
"""How an installed hook says it is this command's to rewrite."""


class CommitGuard(BaseModel, frozen=True):
    """What one repository installs as its commit-time refusal of stale output.

    Both fields are judgements rather than facts — another project may guard a
    different check, or hang it off a different git hook — so each is a default
    a caller replaces rather than a constant it would have to fork this module
    to change.
    """

    command: str = DRIFT_COMMAND
    """What the hook runs, and refuses the commit on a nonzero exit from."""

    hook: str = "pre-commit"
    """Which git hook the check is installed as."""

    def body(self) -> str:
        """The hook script: the marker that claims it, then the check itself."""
        return (
            "#!/bin/sh\n"
            f"# {GUARD_MARKER}: written by `{INSTALL_COMMAND}`.\n"
            "# Refuses this commit while a generated artifact differs from what\n"
            f"# its source renders. Settle it with `{REGENERATE_COMMAND}`.\n"
            f"exec {self.command}\n"
        )


type GuardStatus = Literal["current", "stale", "absent", "foreign"]
"""What sits at the hook path, judged against what this guard would write."""


class GuardState(BaseModel, frozen=True):
    """Where a repository's commit guard lives and what is actually there."""

    path: Path
    status: GuardStatus

    @property
    def armed(self) -> bool:
        """Whether a commit in this repository now runs the guarded check."""
        return self.status == "current"

    def describe(self) -> str:
        """One line saying what is installed, and what to do when it is not."""
        match self.status:
            case "current":
                return f"commit guard armed: {self.path}"
            case "stale":
                return f"commit guard at {self.path} is an older body; reinstall it"
            case "absent":
                return (
                    f"commit guard not installed at {self.path}; "
                    f"run `{INSTALL_COMMAND}`"
                )
            case "foreign":
                return f"{self.path} holds a hook this did not write"


class GuardConflict(RuntimeError):
    """A hook nobody here wrote already occupies the guard's path."""


def hooks_directory(root: Path) -> Path:
    """Where git looks for this clone's hooks, asked from any of its worktrees.

    ``core.hooksPath`` moves the whole directory, so a guard written to
    ``.git/hooks`` under a clone that sets it would be a hook git never runs.
    Asking git resolves both that setting and the shared common directory a
    linked worktree hooks through.
    """
    configured = git.out(
        "-C", str(root), "config", "--get", "core.hooksPath", _ok_code=[0, 1]
    )
    named = configured or git.out(
        "-C", str(root), "rev-parse", "--git-path", "hooks", _ok_code=[0]
    )
    return root / named


# lup: ignore[model-free-function] — driver: it reads the hook file on disk, and
# CommitGuard is the declaration of which hook rather than the thing that reads
def guard_state(guard: CommitGuard, directory: Path) -> GuardState:
    """Read what is installed at the guard's hook path."""
    path = directory / guard.hook
    if not path.is_file():
        return GuardState(path=path, status="absent")
    installed = path.read_text(encoding="utf-8")
    if GUARD_MARKER not in installed:
        return GuardState(path=path, status="foreign")
    current = installed == guard.body()
    return GuardState(path=path, status="current" if current else "stale")


# lup: ignore[model-free-function] — driver: the same disk read, resolved from a
# checkout root
def read_guard(guard: CommitGuard, root: Path) -> GuardState:
    """What one checkout would run, or fail to run, before its next commit."""
    return guard_state(guard, hooks_directory(root))


# lup: ignore[model-free-function] — driver: it writes the hook file
def install_guard(guard: CommitGuard, root: Path, *, force: bool = False) -> GuardState:
    """Write the hook, refusing to displace one this command did not write."""
    directory = hooks_directory(root)
    existing = guard_state(guard, directory)
    if existing.status == "foreign" and not force:
        raise GuardConflict(
            f"{existing.path} holds a hook this did not write; read it, then pass "
            "--force to replace it"
        )
    directory.mkdir(parents=True, exist_ok=True)
    existing.path.write_text(guard.body(), encoding="utf-8", newline="\n")
    existing.path.chmod(0o755)
    return guard_state(guard, directory)


# lup: ignore[model-free-function] — driver: it removes the hook file
def uninstall_guard(guard: CommitGuard, root: Path) -> GuardState:
    """Remove the hook, leaving one this command did not write alone."""
    state = read_guard(guard, root)
    match state.status:
        case "current" | "stale":
            state.path.unlink()
            return GuardState(path=state.path, status="absent")
        case "absent" | "foreign":
            return state


# lup: ignore[model-free-function] — driver: it installs the hook and reports
# what a setup command should print, which is the caller's surface not the model's
def arm(guard: CommitGuard, root: Path) -> str:
    """Install the guard where a setup command can only report a failure.

    Worktree creation runs under sandboxes that mount the hooks directory
    read-only, and a checkout without the hook is still a working checkout —
    the pipeline refuses the same drift on the way in. So this says what
    happened and lets setup carry on, rather than failing it over a second
    line of defence.
    """
    try:
        return install_guard(guard, root).describe()
    except (OSError, GuardConflict, sh.ErrorReturnCode) as error:
        return f"commit guard not installed: {error}"
