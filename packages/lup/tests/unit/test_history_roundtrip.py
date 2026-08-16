# lup: ignore[dict-str-payload]
# Test fixtures and assertions construct these shapes deliberately.
"""Session storage round-trip through the versioned notes layout."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import BaseModel

from lup.types import Usage
from lup.workspace.history import iter_session_dirs, load_session_records, save_session
from lup.workspace.paths import configure, project_root

ORIGINAL_ROOT = project_root()


class DemoResult(BaseModel):
    session_id: str
    value: int


@pytest.fixture
def isolated_root(tmp_path: Path) -> Iterator[Path]:
    configure(root=tmp_path, version="1.2.3")
    yield tmp_path
    configure(root=ORIGINAL_ROOT)


class TestSessionRoundTrip:
    def test_save_then_load_through_versioned_layout(self, isolated_root: Path) -> None:
        saved_path = save_session(
            DemoResult(session_id="s1", value=42), session_id="s1"
        )

        assert saved_path.is_relative_to(
            isolated_root / "notes" / "traces" / "1.2.3" / "sessions" / "s1"
        )
        loaded = load_session_records("s1")
        assert len(loaded) == 1
        assert loaded[0].model_extra == {"value": 42}

    def test_iter_session_dirs_filters_by_session(self, isolated_root: Path) -> None:
        save_session(DemoResult(session_id="a", value=1), session_id="a")
        save_session(DemoResult(session_id="b", value=2), session_id="b")

        dirs = list(iter_session_dirs(session_id="a"))
        assert [d.name for d in dirs] == ["a"]

        all_dirs = {d.name for d in iter_session_dirs()}
        assert all_dirs == {"a", "b"}

    def test_load_missing_session_returns_empty(self, isolated_root: Path) -> None:
        assert load_session_records("ghost") == []


class CodexResult(BaseModel):
    session_id: str
    agent_sdk: str
    token_usage: dict[str, int]


class TestBackendStamp:
    def test_codex_session_round_trips_with_backend_stamp(
        self, isolated_root: Path
    ) -> None:
        """The codex path persists through the same layout as claude: the
        agent_sdk stamp survives the round-trip and session_backend reads
        it back for the trace tooling."""
        from lup.workspace.history import session_backend

        save_session(
            CodexResult(
                session_id="cx1",
                agent_sdk="codex",
                token_usage={"input_tokens": 540, "output_tokens": 88},
            ),
            session_id="cx1",
        )

        loaded = load_session_records("cx1")
        assert loaded[0].agent_sdk == "codex"
        assert loaded[0].token_usage == Usage(input_tokens=540, output_tokens=88)

        session_dirs = list(iter_session_dirs(session_id="cx1"))
        assert session_backend(session_dirs[0]) == "codex"
