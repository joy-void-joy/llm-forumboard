"""Codex's own words for where it keeps a login.

A leaf on purpose: the harness declaration, the account-home sync, and the
profile transform all need these spellings, and none of them should pull the
session runtime in to get them.
"""

from lup.runtime.login import ProviderLogin

# lup: ignore[constant-declaration] — the environment variable Codex reads
CODEX_HOME = "CODEX_HOME"

CODEX_LOGIN = ProviderLogin(
    config_home_env=CODEX_HOME,
    credentials_file="auth.json",
)
"""Where Codex stores a completed login, and how to select one."""
