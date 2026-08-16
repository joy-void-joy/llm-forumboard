"""Persistent in-container Python REPL over Docker's exec socket.

Holds the JSON-line protocol server script that runs inside the container,
the variant-shape socket helpers docker-py hands back, and the ``ReplSession``
that streams code to the REPL and reads its multiplexed frames. State (the
Python namespace) persists across calls like notebook cells.
"""

import json
import logging
import time
from importlib import resources
from typing import Protocol

from pydantic import ValidationError

try:
    import docker
except ImportError as exc:
    raise ImportError(
        "lup.sandbox requires the 'docker' extra. Install with: uv sync --extra docker"
    ) from exc
from docker.models.containers import Container
from docker.utils.socket import SocketError, next_frame_header, read_exactly

from lup.sandbox.models import (
    CodeExecutionTimeoutError,
    ExecuteCodeResult,
    ReplCrashedError,
    SandboxNotInitializedError,
)
from lup.sandbox.process import compute_deadline
from lup.types import EnvVars

logger = logging.getLogger(__name__)


class ExecSocket(Protocol):
    """Opaque handle to docker-py's exec stream.

    docker-py hands back a different concrete socket per platform and
    version (a raw ``socket.socket``, a ``SocketIO`` wrapper, an SSH/npipe
    socket) with no shared typed surface, so the REPL holds the handle
    opaquely and reaches close, read, write, and timeout — which differ by
    variant — dynamically through the module helpers below.
    """


def socket_send(sock: ExecSocket, data: bytes) -> None:
    """Write bytes to a docker exec socket across its variant shapes.

    A raw socket exposes ``sendall`` (on itself or the wrapped ``_sock``);
    a ``SocketIO`` wrapper exposes ``write`` instead.
    """
    raw = getattr(sock, "_sock", sock)
    sender = getattr(raw, "sendall", None)
    if sender is not None:
        sender(data)
    else:
        getattr(sock, "write")(data)


def socket_close(sock: ExecSocket) -> None:
    """Close a docker exec socket and the buffered response behind it."""
    response = getattr(sock, "_response", None)
    if response is not None:
        response.close()
    getattr(sock, "close")()


def socket_set_timeout(sock: ExecSocket, timeout: float | None) -> None:
    """Set the timeout on whichever variant carries ``settimeout``.

    ``timeout=None`` restores blocking mode. The raw socket may be the
    handle itself or the wrapped ``_sock``.
    """
    for candidate in (sock, getattr(sock, "_sock", None)):
        setter = getattr(candidate, "settimeout", None)
        if setter is not None:
            setter(timeout)
            return


REPL_SERVER_SCRIPT = (
    resources.files("lup.sandbox").joinpath("repl_server.py").read_text("utf-8")
)
"""Source of :mod:`lup.sandbox.repl_server`, copied into the container."""


class ReplSession:
    """Persistent Python REPL inside a Docker container.

    Communicates over Docker's exec socket API using a JSON-line protocol.
    The REPL maintains a shared namespace across code executions, so
    variables and imports persist between calls.
    """

    def __init__(
        self,
        client: docker.DockerClient,
        container: Container,
        environment: EnvVars,
    ) -> None:
        self.client = client
        self.container = container
        self.environment = environment
        self.sock: ExecSocket | None = None
        self.exec_id: str | None = None

    def start(self) -> None:
        """Start the REPL process and verify it responds."""
        created = self.client.api.exec_create(
            self.container.id,
            ["python", "-u", "/workspace/.repl_server.py"],
            stdin=True,
            stdout=True,
            stderr=True,
            tty=False,
            workdir="/workspace",
            environment=self.environment or None,
        )
        exec_id: str = created["Id"]
        self.exec_id = exec_id
        self.sock = self.client.api.exec_start(self.exec_id, socket=True)
        result = self.execute("pass", timeout_seconds=10)
        if result.exit_code != 0:
            raise RuntimeError(f"REPL startup failed: {result.stderr}")
        logger.info("Persistent REPL started")

    def stop(self) -> None:
        """Close the socket connection to the REPL."""
        if self.sock is not None:
            try:
                socket_close(self.sock)
            except (OSError, ValueError):
                # Socket-layer close failures (already closed, broken pipe)
                # are expected during teardown; anything else propagates.
                logger.debug("Closing REPL connection failed", exc_info=True)
            self.sock = None
        self.exec_id = None

    def execute(self, code: str, timeout_seconds: int) -> ExecuteCodeResult:
        """Send code to the REPL and return the result.

        Non-positive ``timeout_seconds`` disables both the in-sandbox
        SIGALRM and the host-side deadline — the call blocks until the
        code finishes.
        """
        if self.sock is None:
            raise SandboxNotInitializedError("REPL not connected")

        request = json.dumps({"code": code, "timeout": timeout_seconds}) + "\n"
        self.send(request.encode("utf-8"))

        deadline = compute_deadline(timeout_seconds)
        try:
            response = self.recv_response(deadline)
        except (SocketError, OSError) as e:
            raise ReplCrashedError(f"REPL exited: {e}") from e

        if response.exit_code == 124:
            raise CodeExecutionTimeoutError(
                f"Code execution timed out after {timeout_seconds} seconds"
            )

        return response

    def send(self, data: bytes) -> None:
        """Write raw bytes to the exec socket stdin."""
        if self.sock is None:
            raise ReplCrashedError("REPL not connected")
        try:
            socket_send(self.sock, data)
        except (BrokenPipeError, OSError) as e:
            raise ReplCrashedError(f"REPL write failed: {e}") from e

    def recv_response(self, deadline: float | None) -> ExecuteCodeResult:
        """Read Docker multiplex frames until a complete JSON line arrives.

        ``deadline=None`` blocks indefinitely (no-timeout requests).
        """
        stdout_buf = b""
        while True:
            if deadline is None:
                self.set_socket_timeout(None)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ReplCrashedError("Timed out waiting for REPL response")
                self.set_socket_timeout(remaining)

            stream_type, size = next_frame_header(self.sock)
            if size < 0:
                raise ReplCrashedError("REPL EOF")

            data = read_exactly(self.sock, size)
            match stream_type:
                case 1:  # stdout
                    stdout_buf += data
                    if b"\n" in stdout_buf:
                        # Wire line framing: json parses each framed line.
                        line, _, _ = stdout_buf.partition(  # lup: ignore[string-split]
                            b"\n"
                        )
                        text = line.decode("utf-8", errors="replace")
                        try:
                            return ExecuteCodeResult.model_validate_json(text)
                        except ValidationError as e:
                            raise ReplCrashedError(
                                f"REPL returned non-JSON: {text[:200]}"
                            ) from e
                case 2:  # stderr
                    logger.debug(
                        "REPL stderr: %s", data.decode("utf-8", errors="replace")
                    )

    def set_socket_timeout(self, timeout: float | None) -> None:
        """Set timeout on the underlying socket (None = blocking mode)."""
        if self.sock is not None:
            socket_set_timeout(self.sock, timeout)
