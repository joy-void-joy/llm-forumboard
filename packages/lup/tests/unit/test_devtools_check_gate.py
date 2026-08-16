"""What the pre-flight gate is answerable for when it runs inside a lease.

A resolver lease holds one concern's changes and is judged by `dev check`. An
unscoped anti-pattern gate makes that verdict depend on the whole repository,
so one finding nobody in the run introduced blocks every lease at once and no
revision round can converge on it. These pin the split.
"""

from lup.devtools.dev.antipatterns import FoundAntiPattern
from lup.devtools.dev.check import owned_findings


def finding(file: str, kind: str = "missing") -> FoundAntiPattern:
    return FoundAntiPattern(
        file=file, kind=kind, line=23, text="", message="", rule_id="abc-capability"
    )


def test_an_unscoped_gate_answers_for_every_finding() -> None:
    # CI asks whether the tree is clean, so nothing is out of its scope.
    findings = [finding("a.py"), finding("b.py")]

    scoped = owned_findings(findings, None)

    assert scoped.owned == findings
    assert scoped.outside == []


def test_a_finding_outside_the_changed_paths_does_not_block() -> None:
    changed = finding("packages/lup/src/lup/touched.py")
    pre_existing = finding("packages/lup/src/lup/runtime/profiles.py")

    scoped = owned_findings(
        [changed, pre_existing], ["packages/lup/src/lup/touched.py"]
    )

    assert scoped.owned == [changed]
    assert scoped.outside == [pre_existing]


def test_a_lease_that_changed_nothing_the_rules_hit_is_green() -> None:
    # The case that deadlocked run resolve-9e060ad9bb53: every lease read the
    # same pre-split ProfileStore, and the gate failed identically in all of
    # them however much the worker changed elsewhere.
    scoped = owned_findings(
        [finding("packages/lup/src/lup/runtime/profiles.py")],
        ["packages/lup/src/lup/devtools/dev/worktree.py"],
    )

    assert scoped.owned == []
    assert [item.file for item in scoped.outside] == [
        "packages/lup/src/lup/runtime/profiles.py"
    ]
