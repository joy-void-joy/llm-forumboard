"""Where a runtime keeps a stored login, and how to point it at one.

An application that spawns a provider CLI under a chosen account needs two
facts no other portable contract carries: the environment variable selecting a
configuration home, and the file the runtime writes a completed login into.
Both are words only the provider gets to choose, so each adapter declares its
own and every consumer above holds this value instead of spelling either.

The value is a transparent carrier — it composes no seam and decides nothing,
so an application stores one the way it stores the runtime's name.
"""

from pathlib import Path

from pydantic import BaseModel

from lup.types import EnvVars


class ProviderLogin(BaseModel, frozen=True):
    """One runtime's stored-login location, in that runtime's own words."""

    config_home_env: str
    """Environment variable pointing this runtime's CLI at a config home."""

    credentials_file: str
    """What this runtime writes a completed login into, inside that home."""

    def environment(self, home: Path) -> EnvVars:
        """The environment routing a spawned CLI at that configuration home."""
        return {self.config_home_env: str(home)}

    def credentials_path(self, home: Path) -> Path:
        """Where a completed login sits inside that configuration home."""
        return home / self.credentials_file

    def logged_in(self, home: Path) -> bool:
        """Whether that configuration home already holds a completed login."""
        return self.credentials_path(home).exists()
