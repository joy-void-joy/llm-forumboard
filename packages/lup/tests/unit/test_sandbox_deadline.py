"""compute_deadline: non-positive timeouts must disable the host deadline."""

import time

from lup.sandbox.process import compute_deadline


def test_positive_timeout_adds_grace_to_the_deadline() -> None:
    before = time.monotonic()
    deadline = compute_deadline(10)
    after = time.monotonic()

    assert deadline is not None
    assert before + 15 <= deadline <= after + 15


def test_non_positive_timeouts_disable_the_deadline() -> None:
    assert compute_deadline(0) is None
    assert compute_deadline(-1) is None


def test_custom_grace_period() -> None:
    before = time.monotonic()
    deadline = compute_deadline(1, grace_seconds=0.5)
    assert deadline is not None
    assert deadline >= before + 1.5
