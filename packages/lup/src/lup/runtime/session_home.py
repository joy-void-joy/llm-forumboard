"""Private per-session configuration homes derived under a shared one.

A provider CLI reads its configuration document at startup and writes it
back, and nothing about that exchange is atomic. Sessions opened together
therefore read one another's partial writes: a run that leased eleven
concerns opened eleven sessions at once, six of them read a document another
was still writing, and every one died on a truncated parse before any work
began. Serializing the startup would only make them take turns rewriting a
file they all still share, so what is isolated here is the file.

A derived home is not a copy. Every entry it does not have to own is a
symlink back to the shared home, so a session keeps the settings, plugins,
and stored login the selected profile already carries — the login most of
all, since a refresh rotates the token and one copy of it going stale
invalidates the rest. Only the entries a runtime names unshareable are real
files in the derived home, and a runtime that names none shares everything.
"""

import hashlib
from pathlib import Path

from pydantic import BaseModel


class SessionHomeLayout(BaseModel, frozen=True):
    """Which of one runtime's configuration entries a session cannot share."""

    private_files: list[str] = []
    """Entries a starting session rewrites, so concurrent ones each need
    their own. Named by the runtime that spells them, because which file a
    startup rewrites is that runtime's own fact and no portable one."""

    derived_dir: str = ".lup-sessions"
    """Where under the shared home the derived homes are kept. Dot-prefixed
    and lup-named so it cannot collide with a directory the runtime itself
    keeps there."""


class SessionHomes:
    """One private configuration home per workspace, under a shared home."""

    def __init__(self, shared: Path, layout: SessionHomeLayout) -> None:
        self.shared = shared
        self.layout = layout

    def root(self) -> Path:
        """Where every home derived from this shared one is kept."""
        return self.shared / self.layout.derived_dir

    def name_for(self, workspace: Path) -> str:
        """The directory name the sessions opened in one workspace share.

        The workspace decides rather than a per-session identifier, because
        the workspace is what actually runs concurrently: a run leases one
        checkout per concern and opens them together, while the turns inside
        a single lease are sequential. Keying on it gives every racing
        session a document of its own and still lets a concern's worker and
        its reviewer read one. The digest carries the whole path so two
        leases with the same basename cannot land on one home; the basename
        rides in front of it so a human can tell them apart on disk.
        """
        digest = hashlib.sha256(str(workspace.resolve()).encode("utf-8"))
        return f"{workspace.name}-{digest.hexdigest()[:12]}"

    def derive(self, workspace: Path) -> Path:
        """Create or refresh the home that one workspace's sessions open."""
        home = self.root() / self.name_for(workspace)
        home.mkdir(parents=True, exist_ok=True)
        self.link_shared(home)
        return home

    def link_shared(self, home: Path) -> None:
        """Point every shareable entry of one derived home at the shared one.

        Re-linked on each derivation rather than once when the home is
        created: a runtime makes directories in its home the first time it
        needs one, so an entry that did not exist yet when a home was first
        derived would stay invisible to every session opened through it
        afterwards.
        """
        if not self.shared.is_dir():
            return
        reserved = [self.layout.derived_dir, *self.layout.private_files]
        for entry in self.shared.iterdir():
            link = home / entry.name
            if entry.name in reserved or link.is_symlink() or link.exists():
                continue
            link.symlink_to(entry)
