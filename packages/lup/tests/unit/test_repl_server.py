"""Behavior tests for the in-container REPL server's wire protocol.

``serve`` is importable host-side by design (it only runs under
``__main__`` in the container), so the protocol loop can be driven with
in-memory streams: feed JSON lines, read JSON responses.
"""

import json
import sys
from io import StringIO

from lup.sandbox.models import ExecuteCodeResult
from lup.sandbox.repl_server import serve


def request(code: str) -> str:
    """One protocol request line, so cells can be written as real source."""
    return json.dumps({"code": code})


def run_server_raw(lines: list[str]) -> list[str]:
    originals = sys.stdin, sys.stdout, sys.stderr
    out = StringIO()
    sys.stdin = StringIO("".join(f"{line}\n" for line in lines))
    sys.stdout = out
    try:
        serve()
    finally:
        hijacked = [sys.stdin, sys.stdout, sys.stderr]
        sys.stdin, sys.stdout, sys.stderr = originals
        for handle in hijacked:
            if handle is not out and handle not in originals:
                handle.close()
    return out.getvalue().splitlines()


def run_server(lines: list[str]) -> list[ExecuteCodeResult]:
    return [
        ExecuteCodeResult.model_validate_json(line) for line in run_server_raw(lines)
    ]


def test_malformed_requests_error_without_killing_the_server() -> None:
    responses = run_server(
        [
            "not json",
            "[1, 2]",
            '{"timeout": 5}',
            '{"code": "x = 1 + 1"}',
            '{"code": "print(x + 1)"}',
        ]
    )
    assert [r.exit_code for r in responses] == [1, 1, 1, 0, 0]
    for rejected in responses[:3]:
        assert rejected.stderr.startswith("Malformed request")
        assert rejected.stdout == ""
    assert responses[4].stdout == "3\n"


def test_failing_cell_reports_traceback_and_later_cells_run() -> None:
    responses = run_server(['{"code": "1 / 0"}', '{"code": "print(\'alive\')"}'])
    assert responses[0].exit_code == 1
    assert "ZeroDivisionError" in responses[0].stderr
    assert responses[1] == ExecuteCodeResult(
        exit_code=0,
        stdout="alive\n",
        stderr="",
        result=None,
        duration_ms=responses[1].duration_ms,
    )


def test_server_emits_exactly_the_models_fields() -> None:
    """The in-container script is the one copy of the shape it cannot import.

    It runs on pure stdlib, so it hand-writes the response dict. Parsing
    alone would not catch a drift — the model's fields all carry defaults,
    so a dropped key would validate into a silent default. Compare the
    emitted keys against the model directly.
    """
    (line,) = run_server_raw([request("1")])

    assert set(json.loads(line)) == set(ExecuteCodeResult.model_fields)


def test_trailing_expression_is_echoed_without_touching_stdout() -> None:
    responses = run_server([request("x = 41\nx + 1")])

    assert responses[0].result == "42"
    assert responses[0].stdout == ""
    assert responses[0].exit_code == 0


def test_repr_not_str_is_reported() -> None:
    responses = run_server([request("'quoted'")])

    assert responses[0].result == "'quoted'"


def test_none_valued_expression_reports_no_result() -> None:
    responses = run_server([request("print('hi')")])

    assert responses[0].stdout == "hi\n"
    assert responses[0].result is None


def test_statement_final_cell_reports_no_result() -> None:
    responses = run_server([request("y = 7")])

    assert responses[0].result is None
    assert responses[0].exit_code == 0


def test_underscore_carries_the_value_into_the_next_cell() -> None:
    responses = run_server([request("6 * 7"), request("_ + 1")])

    assert responses[0].result == "42"
    assert responses[1].result == "43"


def test_broken_repr_is_named_rather_than_failing_the_cell() -> None:
    cell = (
        "class Hostile:\n"
        "    def __repr__(self):\n"
        "        raise ValueError('nope')\n"
        "Hostile()"
    )
    responses = run_server([request(cell)])

    assert responses[0].exit_code == 0
    assert responses[0].result is not None
    assert "unrepresentable Hostile: ValueError" in responses[0].result


def test_traceback_line_numbers_survive_the_statement_split() -> None:
    responses = run_server([request("a = 1\nb = 2\n1 / 0")])

    assert responses[0].exit_code == 1
    assert "line 3, in <module>" in responses[0].stderr
