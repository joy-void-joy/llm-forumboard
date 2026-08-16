# lup: ignore[constant-declaration]
# Every constant here is one env var name in the relay contract below, which
# the producer and the consumer must spell identically. A caller replacing one
# would be naming a variable the other half does not read.
"""Session context relay: parent process to a tool-serving subprocess.

Backends whose tools run outside the agent process expose lup's MCP
tools through an external stdio subprocess (``lup-devtools agent
serve-tools``). The Codex runtime does not pass the parent's shell env to
that subprocess, so session state the tools need — reflect, submit_output,
sandbox, realtime relay — crosses the boundary as env vars.

:class:`SessionContext` is that contract. The producer (the subprocess
adapter builder) and the consumer (serve-tools via
:func:`read_session_context`) share this one definition, so the env vars
cannot drift apart. In-process backends run these tools in the agent
process and never touch the relay.
"""

from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

from lup.types import EnvVars

SESSION_DIR_ENV = "LUP_SESSION_DIR"
OUTPUTS_DIR_ENV = "LUP_OUTPUTS_DIR"
GATE_FLAG_ENV = "LUP_GATE_FLAG"
SESSION_ID_ENV = "LUP_SESSION_ID"
TASK_ID_ENV = "LUP_TASK_ID"
REALTIME_DIR_ENV = "LUP_REALTIME_DIR"


class SessionContext(BaseModel):
    """Session context relayed to tool-serving subprocesses via env vars."""

    session_dir: Path
    outputs_dir: Path | None = None
    gate_flag: Path | None = None
    session_id: str | None = None
    task_id: str | None = None
    realtime_dir: Path | None = None

    def to_env(self) -> EnvVars:
        """Serialize to the env vars consumed by read_session_context()."""
        env = {SESSION_DIR_ENV: str(self.session_dir)}
        if self.outputs_dir is not None:
            env[OUTPUTS_DIR_ENV] = str(self.outputs_dir)
        if self.gate_flag is not None:
            env[GATE_FLAG_ENV] = str(self.gate_flag)
        if self.session_id:
            env[SESSION_ID_ENV] = self.session_id
        if self.task_id:
            env[TASK_ID_ENV] = self.task_id
        if self.realtime_dir is not None:
            env[REALTIME_DIR_ENV] = str(self.realtime_dir)
        return env


class SessionEnv(BaseSettings):
    """The relay's consumer side, parsed from the environment by pydantic-settings.

    Each field's alias is the same constant :meth:`SessionContext.to_env`
    writes, so the producer and consumer cannot drift apart.
    """

    session_dir: Path | None = Field(default=None, validation_alias=SESSION_DIR_ENV)
    outputs_dir: Path | None = Field(default=None, validation_alias=OUTPUTS_DIR_ENV)
    gate_flag: Path | None = Field(default=None, validation_alias=GATE_FLAG_ENV)
    session_id: str | None = Field(default=None, validation_alias=SESSION_ID_ENV)
    task_id: str | None = Field(default=None, validation_alias=TASK_ID_ENV)
    realtime_dir: Path | None = Field(default=None, validation_alias=REALTIME_DIR_ENV)


def read_session_context() -> SessionContext | None:
    """Read a SessionContext from env vars, or None when not in a session.

    Returns None when ``LUP_SESSION_DIR`` is unset — the marker that the
    process was not launched by an adapter (e.g. plain devtools usage).
    """
    env = SessionEnv()
    if env.session_dir is None:
        return None
    return SessionContext(
        session_dir=env.session_dir,
        outputs_dir=env.outputs_dir,
        gate_flag=env.gate_flag,
        session_id=env.session_id or None,
        task_id=env.task_id or None,
        realtime_dir=env.realtime_dir,
    )
