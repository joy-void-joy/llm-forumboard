"""Data models and error types for the sandbox.

The tool input schemas, code/install result shapes, the container mount
topology, and the sandbox's exception hierarchy — the shared vocabulary the
REPL transport and the container lifecycle both speak. No Docker dependency,
so importing this never requires the ``docker`` extra.
"""

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

NetworkMode = Literal["filtered", "bridge", "none"]
MountMode = Literal["rw", "ro"]

DEFAULT_PRE_INSTALL: tuple[str, ...] = (
    "requests",
    "pandas",
    "numpy",
    "beautifulsoup4",
    "lxml",
)
"""Packages pre-installed in new containers by default."""


class ExecuteCodeInput(BaseModel):
    """Input schema for the execute_code tool.

    A cell arrives either as ``code`` written inline or as a ``file`` already
    on disk, never as both. The file form exists so a program long enough to
    be worth reviewing can be written, read, and diffed as a file instead of
    escaped into a JSON string argument, and it is named in whichever
    spelling the caller has — host or container, translated on arrival.
    """

    code: str | None = Field(default=None, min_length=1)
    file: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def one_source_of_code(self) -> Self:
        if (self.code is None) == (self.file is None):
            raise ValueError("pass exactly one of code or file")
        return self


class InstallPackageInput(BaseModel):
    """Input schema for the install_package tool."""

    packages: list[str] = Field(min_length=1)


class SandboxReplayInput(BaseModel):
    """Input schema for the sandbox_replay tool.

    Empty because the tool has exactly one thing to replay — the journal
    this sandbox has been keeping — and no lever worth offering over it. A
    confirmation field would be the obvious candidate and is deliberately
    absent: re-running recorded cells is no more consequential than the
    execution the agent already commands at will, and a flag whose only
    honest default is "yes" documents a gate that is not there.
    """


class Mount(BaseModel):
    """One entry in the sandbox's container filesystem topology.

    Names a container-side path, what backs it (a host directory for a
    bind, a Docker named volume otherwise), the access mode, and how the
    in-sandbox agent should use it. This is the single source of truth for
    both the Docker ``volumes`` mapping and the code-execution tool
    description, so what the agent is told always matches what is mounted.
    """

    container_path: str = Field(description="Absolute path inside the container")
    source: str = Field(description="Host directory (bind) or Docker volume name")
    kind: Literal["bind", "volume"]
    mode: MountMode
    purpose: str = Field(description="What the agent uses this path for")


class ExecuteCodeResult(BaseModel):
    """One executed cell's outcome, from REPL wire to tool result.

    Parsed straight off the REPL's JSON line, returned by
    :meth:`Sandbox.run_code`, and handed back as the ``execute_code`` tool
    output — one declaration for all three, so a field cannot reach the
    agent under one name and the library under another. The defaults exist
    for the wire leg: a truncated or malformed response degrades to
    failure rather than to a silent success.
    """

    exit_code: int = 1
    stdout: str = ""
    stderr: str = ""
    result: str | None = Field(
        default=None,
        description=(
            "repr() of the cell's last expression, or null when the cell "
            "ends in a statement or the expression evaluated to None"
        ),
    )
    duration_ms: int = 0


class InstallPackageResult(BaseModel):
    """Result from installing packages in the sandbox.

    Both the library return type and the ``install_package`` tool output.
    Constructed complete by :meth:`Sandbox.run_install`, never parsed off a
    wire, so its fields carry no defaults.
    """

    exit_code: int
    output: str
    packages: list[str]


class SandboxNotInitializedError(RuntimeError):
    """Raised when sandbox operations are called on an inactive sandbox."""


class DockerUnreachableError(RuntimeError):
    """Raised when the Docker daemon cannot be reached at all."""


class RootfulDaemonError(RuntimeError):
    """Raised when a caller requiring the rootless boundary does not have it."""


class DockerDaemonInfo(BaseModel, extra="ignore"):
    """The part of Docker's daemon-info response this library reads.

    Validated rather than indexed, so a daemon that stops reporting the field
    fails as a missing declaration instead of silently reading as unhardened.
    """

    security_options: list[str] = Field(validation_alias="SecurityOptions", default=[])


class CodeExecutionTimeoutError(RuntimeError):
    """Raised when code execution exceeds the timeout."""


class ReplCrashedError(RuntimeError):
    """Raised when the persistent REPL process has exited unexpectedly."""


class PathNotMountedError(RuntimeError):
    """Raised when a named file is reachable from neither side of the boundary.

    Carries the topology's own explanation of where the caller could have put
    it instead, because a caller that named an unmounted path has just proved
    it does not know which paths cross.
    """
