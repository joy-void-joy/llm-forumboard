"""Path-role resolution: what a repository path is for, decided lexically."""

from lup.policy.kernel.edit import decide_edit
from lup.policy.kernel.roles import path_role
from lup.policy.kernel.rows import PathRoleRow

ROLES = [
    PathRoleRow(root="tests", role="test"),
    PathRoleRow(root="tmp", role="scratch"),
]


def test_a_declared_root_and_everything_beneath_it_carries_its_role() -> None:
    assert path_role("tests", ROLES) == "test"
    assert path_role("tests/unit/test_thing.py", ROLES) == "test"
    assert path_role("tmp/briefing.md", ROLES) == "scratch"


def test_an_undeclared_path_is_production() -> None:
    assert path_role("src/module.py", ROLES) == "production"
    assert path_role("packages/lup/src/lup/x.py", ROLES) == "production"
    assert path_role("anything", []) == "production"


def test_a_root_never_matches_a_sibling_that_merely_shares_its_prefix() -> None:
    assert path_role("tests_helpers/x.py", ROLES) == "production"
    assert path_role("tmpfile.py", ROLES) == "production"


def test_traversal_cannot_carry_a_role_out_of_its_root() -> None:
    """`tmp/../src/x.py` is production, so a scratch grant cannot reach it."""
    assert path_role("tmp/../src/x.py", ROLES) == "production"
    assert path_role("tests/../packages/lup/x.py", ROLES) == "production"
    assert path_role("../outside/x.py", ROLES) == "production"
    assert path_role("..", ROLES) == "production"


def test_an_absolute_path_holds_no_repository_role() -> None:
    """Roots are repository-relative, so an absolute path is never one of them."""
    assert path_role("/tmp/x", ROLES) == "production"
    assert path_role("/home/user/tests/x.py", ROLES) == "production"


def test_traversal_that_lands_back_inside_a_root_keeps_its_role() -> None:
    """Normalization decides, so a path is judged by where it actually points."""
    assert path_role("tmp/run/../out.json", ROLES) == "scratch"


def test_the_session_scratchpad_is_scratch_though_no_root_declares_it() -> None:
    """The harness names this root, not the repository.

    It sits outside every worktree, so no repo-relative declaration could
    reach it, and a path there is as disposable as the declared `tmp`.
    """
    assert path_role("/tmp/claude-1000/session/scratchpad/note.md", ROLES) == "scratch"
    assert path_role("$TMPDIR/note.md", ROLES) == "scratch"
    assert path_role("${TMPDIR}/nested/note.md", ROLES) == "scratch"


def test_the_scratchpad_role_survives_no_traversal_out_of_it() -> None:
    assert path_role("/tmp/claude-1000/../../etc/passwd", ROLES) == "production"
    assert path_role("$TMPDIR/../../etc/passwd", ROLES) == "production"
    assert path_role("$TMPDIR/$OTHER/x", ROLES) == "production"


def test_a_scratchpad_lookalike_is_not_the_scratchpad() -> None:
    assert path_role("/tmp/claudex/x", ROLES) == "production"
    assert path_role("/tmp/x", ROLES) == "production"


def authoring(path: str) -> str:
    """The verdict on creating a file that does not exist yet."""
    return decide_edit(
        path,
        None,
        "whatever the agent wrote",
        path_exists=False,
        path_rules=[],
        antipattern_rows=[],
        path_roles=ROLES,
    ).effect


def test_the_edit_gate_consumes_the_role_resolution_produces() -> None:
    """The resolution above is wired to the gate, not merely computed.

    Authoring in the scratchpad costs a reviewer nothing — the file
    reaches no diff — so it carries the same verdict as the declared
    `tmp`, while production still buys its full write with a question.
    """
    assert authoring("/tmp/claude-1000/session/scratchpad/note.md") == "allow"
    assert authoring("tmp/briefing.md") == "allow"
    assert authoring("src/module.py") == "ask"
