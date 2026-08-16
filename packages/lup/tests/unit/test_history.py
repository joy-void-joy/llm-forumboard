# lup: ignore[set-shape]
# Test fixtures and assertions construct these shapes deliberately.
"""Version resolution fallback and latest-session selection.

Includes the regression for cross-version "latest" selection:
update_session_metadata must rank candidate files by parsed timestamp,
not by lexicographic path order (where 0.10.0 < 0.9.0).
"""

import json
from pathlib import Path

from lup.workspace.history import (
    latest_session_record,
    load_session_records,
    resolve_version,
    update_session_metadata,
)


def seed_sessions(root: Path, version: str, count: int) -> None:
    for i in range(count):
        session_dir = (
            root / "notes" / "traces" / version / "sessions" / f"s-{version}-{i}"
        )
        session_dir.mkdir(parents=True)
        (session_dir / "20250101_000000.json").write_text(
            json.dumps({"timestamp": f"2025-01-01T00:00:0{i % 10}"}), encoding="utf-8"
        )


def test_exact_version_with_enough_data(tmp_lup_project: Path) -> None:
    seed_sessions(tmp_lup_project, "1.2.3", 3)
    scope = resolve_version("1.2.3", min_datapoints=2)
    assert scope.versions == ["1.2.3"]
    assert scope.warning is None


def test_widens_to_same_minor(tmp_lup_project: Path) -> None:
    seed_sessions(tmp_lup_project, "1.2.3", 1)
    seed_sessions(tmp_lup_project, "1.2.0", 3)
    scope = resolve_version("1.2.3", min_datapoints=3)
    assert set(scope.versions or []) == {"1.2.0", "1.2.3"}
    assert scope.warning is not None
    assert "v1.2.*" in scope.warning


def test_widens_to_same_major(tmp_lup_project: Path) -> None:
    seed_sessions(tmp_lup_project, "1.2.3", 1)
    seed_sessions(tmp_lup_project, "1.0.0", 4)
    scope = resolve_version("1.2.3", min_datapoints=5)
    assert set(scope.versions or []) == {"1.0.0", "1.2.3"}
    assert scope.warning is not None
    assert "v1.*" in scope.warning


def test_falls_back_to_all_versions(tmp_lup_project: Path) -> None:
    seed_sessions(tmp_lup_project, "1.2.3", 1)
    seed_sessions(tmp_lup_project, "2.0.0", 1)
    scope = resolve_version("1.2.3", min_datapoints=10)
    assert scope.versions is None
    assert scope.warning is not None
    assert "all versions" in scope.warning


def test_no_data_includes_all_versions_quietly(tmp_lup_project: Path) -> None:
    scope = resolve_version("1.2.3", min_datapoints=10)
    assert scope.versions is None
    assert scope.warning is None


def test_all_versions_flag_short_circuits(tmp_lup_project: Path) -> None:
    seed_sessions(tmp_lup_project, "1.2.3", 1)
    scope = resolve_version(None, all_versions=True)
    assert scope.versions is None and scope.warning is None


# ---------------------------------------------------------------------------
# Latest-session selection across versions
# ---------------------------------------------------------------------------


def test_update_session_metadata_targets_newest_timestamp(
    tmp_lup_project: Path,
) -> None:
    # 0.10.0 holds the newer file; lexicographic full-path order would
    # rank ".../0.9.0/..." last and update the stale file instead.
    sid = "shared-session"
    older_dir = tmp_lup_project / "notes" / "traces" / "0.9.0" / "sessions" / sid
    newer_dir = tmp_lup_project / "notes" / "traces" / "0.10.0" / "sessions" / sid
    older_dir.mkdir(parents=True)
    newer_dir.mkdir(parents=True)

    older_file = older_dir / "20250101_120000.json"
    newer_file = newer_dir / "20250601_120000.json"
    older_file.write_text(
        json.dumps({"timestamp": "2025-01-01T12:00:00"}), encoding="utf-8"
    )
    newer_file.write_text(
        json.dumps({"timestamp": "2025-06-01T12:00:00"}), encoding="utf-8"
    )

    assert update_session_metadata(sid, outcome="success") is True

    assert json.loads(newer_file.read_text(encoding="utf-8"))["outcome"] == "success"
    assert "outcome" not in json.loads(older_file.read_text(encoding="utf-8"))


def test_update_session_metadata_missing_session(tmp_lup_project: Path) -> None:
    assert update_session_metadata("missing", outcome="x") is False


def test_load_and_latest_session_json_order_by_timestamp(
    tmp_lup_project: Path,
) -> None:
    sid = "ordered"
    session_dir = tmp_lup_project / "notes" / "traces" / "1.2.3" / "sessions" / sid
    session_dir.mkdir(parents=True)
    (session_dir / "20250101_000000.json").write_text(
        json.dumps({"timestamp": "2025-01-01T00:00:00", "n": 1}), encoding="utf-8"
    )
    (session_dir / "20250301_000000.json").write_text(
        json.dumps({"timestamp": "2025-03-01T00:00:00", "n": 2}), encoding="utf-8"
    )

    sessions = load_session_records(sid)
    assert [s.timestamp for s in sessions] == [
        "2025-01-01T00:00:00",
        "2025-03-01T00:00:00",
    ]

    latest = latest_session_record(sid)
    assert latest is not None
    assert latest.timestamp == "2025-03-01T00:00:00"
