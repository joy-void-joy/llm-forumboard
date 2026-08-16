"""Captured CLI stderr must surface on failure without breaking classification.

The Agent SDK raises ``ProcessError`` carrying a fixed "Check stderr output
for details" and never attaches the subprocess's real stderr — so an ``exit
code 1`` crash is undiagnosable. Splicing the captured lines back in fixes
that, but it must leave the ``exit code N`` token intact, because exit-code
matching downstream is what separates an interrupt from a crash.
"""

from collections import deque

from claude_agent_sdk import ProcessError

from lup.adapters.claude.runtime import attach_cli_stderr


def make_process_error(exit_code: int) -> ProcessError:
    return ProcessError(
        f"Command failed with exit code {exit_code}",
        exit_code=exit_code,
        stderr="Check stderr output for details",
    )


def test_captured_stderr_replaces_placeholder() -> None:
    error = make_process_error(1)

    attach_cli_stderr(
        error, deque(["ModuleNotFoundError: no module named 'x'", "  at f"])
    )

    assert "ModuleNotFoundError" in str(error)
    assert "Check stderr output for details" not in str(error)
    assert error.stderr == "ModuleNotFoundError: no module named 'x'\n  at f"


def test_the_exit_code_token_survives_the_rewrite() -> None:
    """Everything downstream that reads an exit code still finds one."""
    error = make_process_error(-2)

    attach_cli_stderr(error, deque(["KeyboardInterrupt"]))

    assert "exit code -2" in str(error)


def test_empty_buffer_leaves_the_error_untouched() -> None:
    error = make_process_error(1)
    before = str(error)

    attach_cli_stderr(error, deque())

    assert str(error) == before


def test_a_non_process_error_is_left_alone() -> None:
    """Only the error that names a stderr it never attached is rewritten."""
    error = RuntimeError("something else entirely")

    attach_cli_stderr(error, deque(["noise"]))

    assert str(error) == "something else entirely"
