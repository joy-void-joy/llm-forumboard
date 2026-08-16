"""Claude Code's own words for where it keeps a login.

A leaf on purpose: the harness declaration, the usage reader, and the profile
transform all need these spellings, and none of them should pull the session
runtime in to get them.
"""

from lup.runtime.login import ProviderLogin

# lup: ignore[constant-declaration] — the environment variable Claude Code reads
CLAUDE_CONFIG_DIR = "CLAUDE_CONFIG_DIR"

CLAUDE_LOGIN = ProviderLogin(
    config_home_env=CLAUDE_CONFIG_DIR,
    credentials_file=".credentials.json",
)
"""Where Claude Code stores a completed login, and how to select one."""
