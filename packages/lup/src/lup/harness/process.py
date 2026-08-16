"""Local native-executable launching and its request/status vocabulary.

Defines the ``ProcessLauncher`` seam and implements it for everything that
must run a native CLI: devtools harness launch and doctor flows and the
resolver's git and skill invocations. Its request and status models live here
because launchers are their only producers.
"""

import os
from abc import ABC, abstractmethod
from pathlib import Path

import sh
from pydantic import BaseModel

from lup.types import EnvVars


class LaunchRequest(BaseModel, frozen=True):
    arguments: list[str]
    cwd: Path
    environment: EnvVars = {}


class ExitStatus(BaseModel, frozen=True):
    code: int
    stdout: str = ""
    stderr: str = ""


class ProcessLauncher(ABC):
    """Launch one concrete process boundary."""

    @abstractmethod
    def launch(self, request: LaunchRequest) -> ExitStatus:
        """Launch with typed arguments and environment."""


def decoded_output(value: bytes | str | None) -> str:
    """Decode output captured by ``sh`` without discarding framing."""
    if value is None:
        return ""
    return (
        value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    )


class LocalProcessLauncher(ProcessLauncher):
    """Run an explicitly named executable with captured output and no shell.

    Output is captured through pipes, never a pseudo-terminal, so children
    that branch on ``isatty`` (pagers, colorizers) emit plain machine output.
    """

    def launch(self, request: LaunchRequest) -> ExitStatus:
        if not request.arguments:
            raise ValueError("a launch request must name an executable")
        executable, *arguments = request.arguments
        command = sh.Command(executable)
        environment = {
            **os.environ,  # lup: ignore[os-environ] — inherit the process boundary
            **request.environment,
        }
        result = command(
            *arguments,
            _cwd=str(request.cwd),
            _env=environment,
            _ok_code=list(range(256)),
            _return_cmd=True,
            _tty_out=False,
        )
        if not isinstance(result, sh.RunningCommand):
            raise RuntimeError(f"process {executable!r} did not return command state")
        if result.exit_code is None:
            raise RuntimeError(f"process {executable!r} has no terminal exit status")
        return ExitStatus(
            code=result.exit_code,
            stdout=decoded_output(result.stdout),
            stderr=decoded_output(result.stderr),
        )
