"""What a detaching invocation hands to the run that outlives it."""

from lup.devtools.harness.resolve import forwardable_arguments


def test_every_flag_but_detach_reaches_the_detached_run() -> None:
    """Re-listing the flags is what lost them; deriving them cannot.

    `--adopt-config` was the reported casualty — a moved run could not be
    resumed detached at all — but the rebuild forwarded only adapter, run id
    and answers, so `--no-issues`, `--admit*` and `--wait` went with it.
    """
    forwarded = forwardable_arguments(
        [
            "/usr/bin/lup-devtools",
            "harness",
            "resolve",
            "--adapter",
            "claude",
            "--detach",
            "--adopt-config",
            "--no-issues",
            "--wait",
            "30",
            "--admit-issue",
            "97",
            "--answer",
            "integration-assembly=approve",
        ]
    )

    assert forwarded == [
        "harness",
        "resolve",
        "--adapter",
        "claude",
        "--adopt-config",
        "--no-issues",
        "--wait",
        "30",
        "--admit-issue",
        "97",
        "--answer",
        "integration-assembly=approve",
    ]


def test_the_detaching_flag_itself_is_not_forwarded() -> None:
    """Left in, the child detaches again and nothing ever runs."""
    assert "--detach" not in forwardable_arguments(
        ["lup-devtools", "harness", "resolve", "--adapter", "claude", "--detach"]
    )


def test_the_run_id_a_resume_names_survives_forwarding() -> None:
    """The resume this exists for: a named run plus the flag it needs."""
    forwarded = forwardable_arguments(
        [
            "lup-devtools",
            "harness",
            "resolve",
            "--adapter",
            "claude",
            "--run-id",
            "resolve-9e060ad9bb53",
            "--detach",
        ]
    )

    assert forwarded == [
        "harness",
        "resolve",
        "--adapter",
        "claude",
        "--run-id",
        "resolve-9e060ad9bb53",
    ]
