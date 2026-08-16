"""Personal Claude account registry used by concrete CLI composition roots.

The registry file is a plain collaborator rather than a base class both
capabilities extend: an implementation that inherited its reading would be
inheriting behavior alongside a capability, and the two implementations here
would then be one class answering for two powers. Composing it instead lets
them share every byte of the format and stay separately constructible.
"""

from pathlib import Path

from pydantic import BaseModel

from lup.channels.models import publish_atomic
from lup.adapters.claude.config import (
    ClaudeProfileRegistry,
    ClaudeProfileSelection,
)
from lup.runtime.profiles import ProfileNames, ProfileRegistrar

REGISTRY_PATH = Path.home() / ".lup" / "profiles.json"


class Account(BaseModel, frozen=True):
    """One registered Claude configuration home."""

    config_dir: str


class Registry(BaseModel):
    """Personal on-disk named account registry."""

    profiles: dict[str, Account] = {}
    active: str | None = None


class AccountFile:
    """The personal registry file, read and written whole.

    The origin for a project that keeps no accounts of its own: names are
    registered by hand and each carries wherever its home happens to live.
    """

    def __init__(self, registry_path: Path = REGISTRY_PATH) -> None:
        self.registry_path = registry_path

    def homes_root(self) -> Path:
        """Where a profile registered without a home of its own is put.

        Beside the registry rather than under a fixed absolute path, so a
        store pointed at a scratch registry keeps its homes there too.
        """
        return self.registry_path.parent / "homes"

    def load_registry(self) -> Registry:
        if not self.registry_path.exists():
            return Registry()
        return Registry.model_validate_json(
            self.registry_path.read_text(encoding="utf-8")
        )

    def save_registry(self, registry: Registry) -> None:
        publish_atomic(self.registry_path, registry)

    def resolver_registry(self) -> ClaudeProfileRegistry:
        """Project personal storage into immutable runtime selection data."""
        registry = self.load_registry()
        return ClaudeProfileRegistry(
            profiles={
                name: ClaudeProfileSelection(
                    config_directory=Path(account.config_dir).expanduser()
                )
                for name, account in registry.profiles.items()
            },
            active=registry.active,
        )

    def resolve_config_dir(self, name: str | None = None) -> Path:
        """Resolve explicit, active, then default through the typed registry."""
        registry = self.resolver_registry()
        selected = name or registry.active
        if selected is None:
            return registry.default.config_directory
        try:
            return registry.profiles[selected].config_directory
        except KeyError as error:
            raise KeyError(f"unknown Claude profile {selected!r}") from error


class ClaudeProfileNames(ProfileNames):
    """Read which accounts the personal registry holds, and what each selects."""

    def __init__(self, accounts: AccountFile | None = None) -> None:
        self.accounts = accounts or AccountFile()

    def names(self) -> list[str]:
        return sorted(self.accounts.load_registry().profiles)

    def config_dir_for(self, name: str) -> Path:
        registry = self.accounts.load_registry()
        return Path(registry.profiles[name].config_dir).expanduser()

    def active_profile(self) -> str | None:
        return self.accounts.load_registry().active


class ClaudeProfileRegistrar(ProfileRegistrar):
    """Atomically add, select, and forget accounts in the personal registry."""

    def __init__(self, accounts: AccountFile | None = None) -> None:
        self.accounts = accounts or AccountFile()

    def add_profile(self, name: str, config_dir: Path | None = None) -> Path:
        home = (
            config_dir if config_dir is not None else self.accounts.homes_root() / name
        )
        registry = self.accounts.load_registry()
        profiles = dict(registry.profiles)
        profiles[name] = Account(config_dir=str(home))
        self.accounts.save_registry(
            registry.model_copy(
                update={
                    "profiles": profiles,
                    "active": registry.active or name,
                }
            )
        )
        return home.expanduser()

    def set_active(self, name: str) -> None:
        registry = self.accounts.load_registry()
        if name not in registry.profiles:
            raise KeyError(name)
        self.accounts.save_registry(registry.model_copy(update={"active": name}))

    def remove_profile(self, name: str) -> None:
        registry = self.accounts.load_registry()
        profiles = dict(registry.profiles)
        profiles.pop(name, None)
        self.accounts.save_registry(
            registry.model_copy(
                update={
                    "profiles": profiles,
                    "active": None if registry.active == name else registry.active,
                }
            )
        )
