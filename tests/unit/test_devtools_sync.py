"""Registry resolution tests for `lup-devtools sync`.

The sync registry contract: sync.json(.local) is the canonical pair, and a
repo still carrying the legacy downstream.json(.local) names is read as a
fallback with a deprecation warning that tells the user to rename the file.
"""

import json
import logging
from pathlib import Path

import pytest

from lup.devtools import sync


def write_registry(path: Path) -> None:
    path.write_text(json.dumps({"projects": [{"name": "lup"}]}) + "\n")


@pytest.fixture
def registry_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(sync, "project_root", lambda: tmp_path)
    return tmp_path


def test_registry_prefers_sync_json_over_legacy(registry_root: Path) -> None:
    write_registry(registry_root / "sync.json")
    write_registry(registry_root / "downstream.json")

    assert sync.sync_file() == registry_root / "sync.json"


def test_registry_falls_back_to_legacy_name_with_deprecation_warning(
    registry_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_registry(registry_root / "downstream.json")

    with caplog.at_level(logging.WARNING):
        resolved = sync.sync_file()

    assert resolved == registry_root / "downstream.json"
    assert "downstream.json is deprecated; rename it to sync.json" in caplog.text


def test_local_registry_falls_back_to_legacy_name(
    registry_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write_registry(registry_root / "downstream.json.local")

    with caplog.at_level(logging.WARNING):
        resolved = sync.local_file()

    assert resolved == registry_root / "downstream.json.local"
    assert "rename it to sync.json.local" in caplog.text


def test_missing_registries_resolve_to_sync_names(registry_root: Path) -> None:
    assert sync.sync_file() == registry_root / "sync.json"
    assert sync.local_file() == registry_root / "sync.json.local"
    assert sync.load_projects() == []


def test_local_entries_override_tracked_entries_by_name(registry_root: Path) -> None:
    (registry_root / "sync.json").write_text(
        json.dumps({"projects": [{"name": "lup", "url": "https://example.test/lup"}]})
    )
    (registry_root / "sync.json.local").write_text(
        json.dumps({"projects": [{"name": "lup", "ignore": True}]})
    )

    projects = sync.load_projects()

    assert projects == [
        {"name": "lup", "url": "https://example.test/lup", "ignore": True}
    ]
