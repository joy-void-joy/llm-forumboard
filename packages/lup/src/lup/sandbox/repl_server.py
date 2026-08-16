"""Persistent Python REPL server — runs inside the Docker container.

Protocol (JSON-line over stdin/stdout):
  Request:  ``{"code": "...", "timeout": 30}``
  Response: ``{"exit_code": 0, "stdout": "...", "stderr": "...",
             "result": "42", "duration_ms": 42}``

- All exec() calls share a single namespace, so variables and imports
  persist across requests (like notebook cells).
- A cell ending in an expression reports that expression's ``repr`` as
  ``result`` and binds it to ``_``, the way an interactive REPL echoes.
  Cells ending in a statement, and expressions evaluating to None, report
  a null result.
- sys.stdin/stdout/stderr are redirected to /dev/null so user code cannot
  interfere with the JSON protocol; the original streams stay bound to
  protocol I/O inside :func:`serve`. User code cannot reach the server's
  own state either — it executes with the shared namespace as its globals,
  and ``serve``'s locals are invisible to it.
- SIGALRM enforces per-request timeouts (exit_code 124 on expiry).
- stdout/stderr are capped at 1 MB to prevent memory blowouts.

Pure stdlib, and it only runs as ``__main__``: the sandbox copies this file
into the container and launches ``python -u`` on it, while ``lup.sandbox.repl``
merely ships its source text — importing it host-side starts nothing.
"""

import ast
import builtins
import json
import signal
import sys
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from types import FrameType

MAX_OUTPUT = 1_048_576


class CellTimeout(Exception):
    """Raised by the SIGALRM handler when a request exceeds its timeout."""


def raise_timeout(signum: int, frame: FrameType | None) -> None:
    raise CellTimeout()


def echo_repr(
    value: object,  # lup: ignore[bare-object] — any cell value
    max_output: int = MAX_OUTPUT,
) -> str:
    """``repr`` a cell's value, surviving a hostile ``__repr__``.

    The value comes from arbitrary agent code, so its ``__repr__`` may
    raise. A cell that computed successfully must not be reported as
    failed just because its result could not be rendered, so the failure
    is named in place of the repr rather than propagated.
    """
    try:
        return repr(value)[:max_output]
    except Exception as e:
        return f"<unrepresentable {type(value).__name__}: {type(e).__name__}>"


def run_cell(
    code: str,
    namespace: dict[str, object],  # lup: ignore[dict-str-object] — the REPL namespace
) -> str | None:
    """Run one cell, returning the ``repr`` of its trailing expression.

    Statements execute under ``exec``; a trailing expression is compiled
    separately in ``eval`` mode so its value can be echoed and bound to
    ``_``. Nodes keep the line numbers ``ast.parse`` gave them, so a
    traceback still points into the cell as written.

    Returns None when the cell ends in a statement or when the trailing
    expression evaluated to None — matching an interactive REPL, which
    prints nothing in both cases.
    """
    tree = ast.parse(code, "<cell>", "exec")
    last = tree.body[-1] if tree.body else None
    if not isinstance(last, ast.Expr):
        exec(  # lup: ignore[eval-exec] — the REPL's job
            compile(tree, "<cell>", "exec"), namespace
        )
        return None

    statements = ast.Module(body=tree.body[:-1], type_ignores=tree.type_ignores)
    exec(  # lup: ignore[eval-exec] — the REPL's job
        compile(statements, "<cell>", "exec"), namespace
    )
    value = eval(  # lup: ignore[eval-exec] — the REPL's job
        compile(ast.Expression(body=last.value), "<cell>", "eval"), namespace
    )
    if value is None:
        return None
    namespace["_"] = value
    return echo_repr(value)


def serve() -> None:
    # Hijack standard streams: keep the originals for protocol I/O so user
    # code (print, input) cannot corrupt the JSON wire format.
    proto_in = sys.stdin
    proto_out = sys.stdout
    sys.stdin = open("/dev/null", "r")
    sys.stdout = open("/dev/null", "w")
    sys.stderr = open("/dev/null", "w")

    # Live Python objects of any type — the whole point of the namespace.
    namespace: dict[str, object] = {  # lup: ignore[dict-str-object]
        "__builtins__": builtins
    }

    for raw_line in proto_in:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            request = None
        if not isinstance(request, dict) or "code" not in request:
            proto_out.write(
                json.dumps(
                    {
                        "exit_code": 1,
                        "stdout": "",
                        "stderr": 'Malformed request: expected {"code": ..., "timeout": ...}',
                        "duration_ms": 0,
                    }
                )
                + "\n"
            )
            proto_out.flush()
            continue

        code = str(request["code"])
        timeout = int(request.get("timeout", 30))  # lup: ignore[dict-get] — wire
        out_buf = StringIO()
        err_buf = StringIO()
        exit_code = 0
        result: str | None = None
        started = time.perf_counter()
        old_alarm = signal.signal(signal.SIGALRM, raise_timeout)
        try:
            if timeout > 0:
                signal.alarm(timeout)
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                result = run_cell(code, namespace)
        except CellTimeout:
            exit_code = 124
            err_buf.write(f"Execution timed out after {timeout} seconds\n")
        except SystemExit as e:
            exit_code = e.code if isinstance(e.code, int) else 1
        except BaseException:  # lup: ignore[except-baseexception] — cell isolation
            exit_code = 1
            err_buf.write(traceback.format_exc())
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_alarm)

        duration_ms = int((time.perf_counter() - started) * 1000)
        proto_out.write(
            json.dumps(
                {
                    "exit_code": exit_code,
                    "stdout": out_buf.getvalue()[:MAX_OUTPUT],
                    "stderr": err_buf.getvalue()[:MAX_OUTPUT],
                    "result": result,
                    "duration_ms": duration_ms,
                }
            )
            + "\n"
        )
        proto_out.flush()


if __name__ == "__main__":
    serve()
