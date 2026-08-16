"""Profiles a project keeps as directories of its own, one per account.

The second shape :mod:`lup.runtime.profiles` names. A name is a directory
rather than an entry in a registry, so the configuration home that account
runs under — and whatever else it earns — sit together under
``<root>/<name>/``. Making the directory the origin is what keeps one name
meaning one account however it is spelled, to a launch or to a command tree
or to a setup wizard: there is no second list to register a profile in, and
so none to fall out of step with the profiles that exist.

Nothing here names a provider. Both the root and the subdirectory a
configuration home takes inside each profile are the caller's to choose, so
the same layout serves whichever runtime's homes a project keeps, and a
project keeping two runtimes' homes keeps them side by side under one name.
"""

from pathlib import Path

from lup.channels.models import write_atomic
from lup.runtime.profiles import ProfileNames, ProfileRegistrar

ACTIVE_FILE = ".active"
"""Default name for the file recording which profile a launch selects."""


class ProfileFolders:
    """The directory layout, as a plain collaborator both capabilities share.

    A collaborator rather than a base class, for the reason
    :mod:`lup.adapters.claude.profile_store` gives of its registry file: an
    implementation that inherited its reading would be inheriting behavior
    alongside a capability, and the two classes below would then be one class
    answering for two powers.
    """

    def __init__(
        self, root: Path, home_subdir: str, active_file: str = ACTIVE_FILE
    ) -> None:
        self.root = root
        self.home_subdir = home_subdir
        self.active_file = active_file

    def names(self) -> list[str]:
        """Every profile directory under the root, in display order.

        A root that does not exist yet holds no profiles rather than
        failing: a project acquires one the first time it adds a profile,
        and every reader before that should see an empty roster instead of
        an error about a directory nobody has had reason to create.
        """
        if not self.root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.root.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        )

    def home_for(self, name: str) -> Path:
        """The configuration home that name's directory holds."""
        return self.root / name / self.home_subdir

    def active(self) -> str | None:
        """The recorded selection, where one has been recorded."""
        path = self.root / self.active_file
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8").strip() or None

    def select(self, name: str) -> None:
        """Record which profile answers for a caller naming none."""
        self.root.mkdir(parents=True, exist_ok=True)
        write_atomic(self.root / self.active_file, f"{name}\n".encode())


class TreeProfileNames(ProfileNames):
    """Read which accounts a project's profile directories hold."""

    def __init__(self, folders: ProfileFolders) -> None:
        self.folders = folders

    def names(self) -> list[str]:
        return self.folders.names()

    def config_dir_for(self, name: str) -> Path:
        if name not in self.folders.names():
            raise KeyError(name)
        return self.folders.home_for(name)

    def active_profile(self) -> str | None:
        return self.folders.active()


class TreeProfileRegistrar(ProfileRegistrar):
    """Start and select a project's profile directories, and refuse to forget one."""

    def __init__(self, folders: ProfileFolders) -> None:
        self.folders = folders

    def add_profile(self, name: str, config_dir: Path | None = None) -> Path:
        """Start a profile directory, for a login to fill in.

        Where its configuration home sits is derived from the name, so a
        caller naming one elsewhere is asking for something a directory
        profile cannot be rather than for a variation on one. An account
        whose home already exists elsewhere is reached by making that path a
        symlink, which resolves like any other.

        The first profile a project starts becomes its selection, matching
        what registering the first account into a personal registry does:
        a project with exactly one profile should not also have to say so.
        """
        home = self.folders.home_for(name)
        if config_dir is not None and config_dir != home:
            raise ValueError(
                f"a directory profile keeps its configuration home at {home}, "
                f"derived from the name — {config_dir} cannot be one; symlink "
                "that path to point this profile at a home already elsewhere"
            )
        home.mkdir(parents=True, exist_ok=True)
        if self.folders.active() is None:
            self.folders.select(name)
        return home

    def set_active(self, name: str) -> None:
        if name not in self.folders.names():
            raise KeyError(name)
        self.folders.select(name)

    def remove_profile(self, name: str) -> None:
        """Refuse, because forgetting one here would mean deleting it.

        Nothing registers a directory profile, so there is no registration to
        drop: the directory is the profile, and it holds the login that
        account earned. Answered as a ``ValueError`` whose message is the
        explanation, which is what a command tree renders in place of one.
        """
        raise ValueError(
            f"a directory profile is {self.folders.root / name}, which holds "
            "its login — remove that directory to remove the profile"
        )
