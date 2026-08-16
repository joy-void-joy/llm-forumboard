"""Where a project's named configuration homes come from, and the surface over them.

What a profile name means is the application's to decide: a personal registry
file listing accounts by hand, or a directory the project already keeps one
account per entry in. Two capabilities answer for such an origin, because
reading one and curating one are separate powers and most callers only ever
need the first — :class:`ProfileNames` says which names exist and what each
selects, :class:`ProfileRegistrar` registers, selects, and forgets them. Both
are engines; :class:`ProfileDirectory` is the concrete surface a command tree
and a launcher hold over whichever pair an application supplied. Both shapes
ship: :mod:`lup.adapters.claude.profile_store` keeps the registry, and
:mod:`lup.runtime.profile_tree` keeps the directories.

Nothing here names a provider. A directory carries the :class:`ProviderLogin`
of the runtime whose homes it holds, so reporting whether one is signed in —
and telling a caller how to sign it in — is answered in that runtime's own
words rather than in words this module would have to choose.
"""

from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel

from lup.runtime.login import ProviderLogin


class UnknownProfile(KeyError):
    """A name no origin answers to, carrying the roster that would.

    Raised by :class:`ProfileDirectory` rather than formatted wherever one is
    caught, because both callers that resolve a name — the command tree and
    the launcher — owe the reader the same answer, and two spellings of it
    drift apart. It stays a ``KeyError`` because that is what an origin
    already raises, so no origin has to learn a new type to be reported well.
    """

    def __init__(self, name: str, known: list[str]) -> None:
        super().__init__(name)
        self.name = name
        self.known = known

    def __str__(self) -> str:
        """The whole diagnostic, since a caller renders this and nothing else."""
        return (
            f"unknown profile {self.name!r}; known: {', '.join(self.known) or 'none'}"
            f" — register one with `profile add {self.name}`"
        )


class ProfileNames(ABC):
    """Which named configuration homes one origin knows, and what each selects.

    Injected into :class:`ProfileDirectory` and never held by a consumer
    directly, so an application supplies its own origin without any caller
    above learning where profiles are kept.
    """

    @abstractmethod
    def names(self) -> list[str]:
        """Every profile name this origin knows, in display order."""

    @abstractmethod
    def config_dir_for(self, name: str) -> Path:
        """The configuration home that name selects, or ``KeyError``."""

    @abstractmethod
    def active_profile(self) -> str | None:
        """The name that answers for a caller naming none."""


class ProfileRegistrar(ABC):
    """Curating one origin's named homes: registering, selecting, forgetting.

    Separate from reading them because it is a separate power. A launcher
    resolves a name on every run and never curates; keeping the two apart
    is what lets an origin offer the first without offering the second.
    """

    @abstractmethod
    def add_profile(self, name: str, config_dir: Path | None = None) -> Path:
        """Register name at ``config_dir``, or where this origin puts one."""

    @abstractmethod
    def set_active(self, name: str) -> None:
        """Make name answer for callers naming none, or ``KeyError``."""

    @abstractmethod
    def remove_profile(self, name: str) -> None:
        """Forget name, if this origin knows it."""


class Profile(BaseModel, frozen=True):
    """One profile, resolved far enough for a caller to act on it."""

    name: str
    config_dir: Path
    active: bool
    logged_in: bool
    """Whether that home already holds a completed login, so a listing can
    say which profile a launch would drop into a sign-in prompt."""


class ProfileDirectory:
    """Resolve, list, and curate profiles over whichever origin holds them."""

    def __init__(
        self, names: ProfileNames, registrar: ProfileRegistrar, login: ProviderLogin
    ) -> None:
        self.names = names
        self.registrar = registrar
        self.login = login

    def unknown(self, name: str) -> UnknownProfile:
        """The error for a name this origin does not answer to, roster included."""
        return UnknownProfile(name, self.names.names())

    def resolve(self, name: str) -> Path:
        """The home one name selects, reporting the roster when there is none."""
        try:
            return self.names.config_dir_for(name)
        except KeyError as error:
            raise self.unknown(name) from error

    def profile(self, name: str) -> Profile:
        """Resolve one name against the origin, login state included."""
        config_dir = self.resolve(name)
        return Profile(
            name=name,
            config_dir=config_dir,
            active=name == self.names.active_profile(),
            logged_in=self.login.logged_in(config_dir),
        )

    def entries(self) -> list[Profile]:
        """Every known profile, in the origin's own display order."""
        return [self.profile(name) for name in self.names.names()]

    def active(self) -> Profile | None:
        """The profile answering for a caller naming none, where there is one."""
        name = self.names.active_profile()
        return None if name is None else self.profile(name)

    def launch_home(self, name: str | None) -> Path | None:
        """The configuration home a launch exports, or None to inherit.

        Naming none while no profile is active is not an error: it leaves
        whatever home the surrounding environment already selected, which is
        both what a project with no profiles expects and what keeps a session
        launched from inside another one on the account it was started under.
        """
        selected = name or self.names.active_profile()
        if selected is None:
            return None
        return self.resolve(selected)

    def add(self, name: str, config_dir: Path | None = None) -> Profile:
        """Register a profile and resolve what registering it produced."""
        self.registrar.add_profile(name, config_dir)
        return self.profile(name)

    def use(self, name: str) -> Profile:
        """Make one profile the active selection, and resolve it."""
        try:
            self.registrar.set_active(name)
        except KeyError as error:
            raise self.unknown(name) from error
        return self.profile(name)

    def remove(self, name: str) -> Profile:
        """Forget a profile, resolving it first so a caller can report it."""
        removed = self.profile(name)
        self.registrar.remove_profile(name)
        return removed
