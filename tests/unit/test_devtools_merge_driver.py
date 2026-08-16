"""Behavior tests for the ownership-manifest merge driver.

The manifests are generated digest proofs, so two branches always disagree on
``source_digest`` and on every ``sha256`` a regeneration touched. Merging that
text has no meaning: the driver keeps one side so the merge resolves, and
regeneration settles the digests against the merged tree afterwards.
"""

import json
from pathlib import Path

import pytest
import sh

from lup.devtools.dev import worktree
from tests.unit.repos import initialized_repo

ATTRIBUTES = Path(__file__).parents[2] / ".gitattributes"
MANIFESTS = (".claude/.lup-ownership.json", ".codex/.lup-ownership.json")


def manifest(digest: str) -> str:
    """Render a manifest shaped like the real proof, differing only in digests."""
    return json.dumps(
        {
            "schema_version": 1,
            "generator_version": "0.2.0",
            "source_digest": digest,
            "target_requirements": ["claude-code"],
            "files": [
                {
                    "path": ".claude/CLAUDE.md",
                    "category": "generated",
                    "sha256": digest,
                    "semantic_id": "harness.guidance",
                    "executable": False,
                }
            ],
        },
        indent=2,
    )


def bare_git(work: Path) -> sh.Command:
    return sh.Command("git").bake(
        "-C", str(work), "-c", "commit.gpgsign=false", _tty_out=False
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    work = tmp_path / "repo"
    (work / ".claude").mkdir(parents=True)
    (work / ".codex").mkdir(parents=True)
    git = initialized_repo(work, tmp_path / "no-hooks")
    (work / ".gitattributes").write_text(
        ATTRIBUTES.read_text(encoding="utf-8"), encoding="utf-8"
    )
    for name in MANIFESTS:
        (work / name).write_text(manifest("base"), encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "chore: base")

    git("switch", "-c", "feature")
    for name in MANIFESTS:
        (work / name).write_text(manifest("feature"), encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "feat: regenerate on the feature branch")

    git("switch", "main")
    for name in MANIFESTS:
        (work / name).write_text(manifest("main"), encoding="utf-8")
    (work / "unrelated.txt").write_text("main moved on\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "feat: regenerate on main")

    git("switch", "feature")
    return work


def unmerged(work: Path) -> list[str]:
    out = bare_git(work)("diff", "--name-only", "--diff-filter=U", _ok_code=[0, 1])
    return str(out).split()


def drop_driver(work: Path) -> None:
    bare_git(work)(
        "config", "--unset-all", "merge.lup-ownership.driver", _ok_code=[0, 5]
    )


def test_the_attributes_cover_every_manifest() -> None:
    """A manifest the attributes do not name keeps conflicting silently."""
    declared = ATTRIBUTES.read_text(encoding="utf-8")
    for name in MANIFESTS:
        assert f"{name} merge=lup-ownership" in declared


def test_a_divergent_manifest_merges_without_conflict(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    worktree.register_merge_driver()

    bare_git(repo)("merge", "main", "-m", "Merge branch 'main' into feature")

    assert unmerged(repo) == []
    assert (repo / "unrelated.txt").exists()
    for name in MANIFESTS:
        kept = json.loads((repo / name).read_text(encoding="utf-8"))
        assert kept["source_digest"] == "feature"


def test_without_the_driver_the_manifest_conflicts(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git resolves driver names from config, so an unregistered clone degrades."""
    monkeypatch.chdir(repo)
    drop_driver(repo)

    with pytest.raises(sh.ErrorReturnCode):
        bare_git(repo)("merge", "main", "-m", "Merge branch 'main' into feature")

    assert sorted(unmerged(repo)) == sorted(MANIFESTS)


def test_a_rebase_replays_across_a_divergent_manifest(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conflict that stops a rebase mid-replay is the one that has to go.

    Each commit carries a source change beside its regenerated manifest, the
    shape a real one has — the manifest moved *because* the source did.
    """
    monkeypatch.chdir(repo)
    worktree.register_merge_driver()
    git = bare_git(repo)

    for step in ("second", "third"):
        (repo / f"{step}.py").write_text(f"# {step}\n", encoding="utf-8")
        for name in MANIFESTS:
            (repo / name).write_text(manifest(step), encoding="utf-8")
        git("add", "-A")
        git("commit", "-m", f"feat: {step}")

    git("rebase", "main")

    assert unmerged(repo) == []
    assert str(git("rev-parse", "--abbrev-ref", "HEAD")).strip() == "feature"
    subjects = str(git("log", "--format=%s", "main..HEAD")).splitlines()
    assert subjects == ["feat: third", "feat: second"]
    assert (repo / "unrelated.txt").exists()


def test_a_manifest_only_commit_does_not_survive_a_rebase(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keeping one side empties a patch that carried nothing else, so git drops it.

    A commit whose whole content is a digest refresh has nothing left to say
    once the digests are settled by regeneration, and the manifest it would
    have written is reproduced from the merged tree either way.
    """
    monkeypatch.chdir(repo)
    worktree.register_merge_driver()
    git = bare_git(repo)

    git("rebase", "main")

    assert unmerged(repo) == []
    assert str(git("log", "--format=%s", "main..HEAD")).splitlines() == []
