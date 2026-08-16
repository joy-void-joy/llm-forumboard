"""Non-interactive shell environment for agent-run commands.

Credential helpers prompt on ``/dev/tty`` — the terminal the native TUI or
resolver console owns — so a passphrase or editor prompt raised from an agent
shell wedges the whole session instead of failing. These variables make every
such command fail fast with a readable error: ssh refuses to prompt (keys
already loaded in ssh-agent keep working), git never opens an editor or pager,
and gh and keyring lookups stay non-interactive. The devtools launch and
resolver flows merge these defaults beneath the inherited environment at
every agent spawn point, so explicit caller values always win.
"""

from collections.abc import Mapping

from lup.types import EnvVars

# lup: ignore[library-default] — each pair is the variable and off-value git, ssh, gh, and keyring document
NON_INTERACTIVE_SHELL_ENV: EnvVars = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
    "SSH_ASKPASS_REQUIRE": "never",
    "GIT_EDITOR": "true",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GH_PAGER": "cat",
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
}


def non_interactive_environment(
    base: Mapping[str, str],  # lup: ignore[dict-str-payload] — open env-var map
) -> EnvVars:
    """Merge the non-interactive defaults beneath an existing environment."""
    return {**NON_INTERACTIVE_SHELL_ENV, **base}
