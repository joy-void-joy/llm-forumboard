"""Behavior tests for the non-interactive agent shell environment.

Agent shells must never reach an interactive prompt (ssh passphrase, git
editor, pager); these pin that the defaults are present, that a caller's
explicit environment always wins over them, and that merging never mutates
the caller's mapping.
"""

from lup.harness.environment import (
    NON_INTERACTIVE_SHELL_ENV,
    non_interactive_environment,
)


def test_defaults_present_over_empty_base() -> None:
    merged = non_interactive_environment({})
    assert merged == NON_INTERACTIVE_SHELL_ENV
    assert merged["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
    assert merged["GIT_TERMINAL_PROMPT"] == "0"


def test_base_overrides_defaults() -> None:
    base = {"GIT_SSH_COMMAND": "ssh -i /custom/key", "HOME": "/home/user"}
    merged = non_interactive_environment(base)
    assert merged["GIT_SSH_COMMAND"] == "ssh -i /custom/key"
    assert merged["HOME"] == "/home/user"
    assert merged["GIT_EDITOR"] == "true"


def test_merge_does_not_mutate_base() -> None:
    base = {"HOME": "/home/user"}
    merged = non_interactive_environment(base)
    merged["GIT_PAGER"] = "less"
    assert base == {"HOME": "/home/user"}
    assert NON_INTERACTIVE_SHELL_ENV["GIT_PAGER"] == "cat"
