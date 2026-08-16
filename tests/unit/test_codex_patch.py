"""Codex apply_patch decoding and the edit policy it finally reaches.

Codex hands a hook one complete patch command where Claude hands over
structured before/after text. These tests pin the native decode, the
per-file decisions it enables, and the path resolution that keeps
repo-relative rules matching inside a sibling worktree.
"""

import importlib.util
import io
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from lup.adapters.codex.patch import patched_files
from lup.types import JsonObject

GREETING = 'def greet():\n    return "hi"\n'


def update_envelope(path: str, body: str) -> str:
    return f"*** Begin Patch\n*** Update File: {path}\n@@\n{body}\n*** End Patch"


def document(text: str) -> Callable[[str], str | None]:
    return lambda _path: text


def named_document(path: str, text: str) -> Callable[[str], str | None]:
    return lambda candidate: text if candidate == path else None


def moved_document(path: str) -> str | None:
    match path:
        case "old.py":
            return "context\n"
        case "new.py":
            return "replaced\n"
        case _:
            return None


def test_update_applies_hunks_over_the_current_document() -> None:
    envelope = update_envelope(
        "module.py", ' def greet():\n-    return "hi"\n+    return "hello"'
    )

    changes = patched_files(envelope, document(GREETING))

    assert len(changes) == 1
    assert changes[0].path == "module.py"
    assert changes[0].before == GREETING
    assert changes[0].after == 'def greet():\n    return "hello"\n'


def test_add_file_carries_no_preimage() -> None:
    envelope = "*** Begin Patch\n*** Add File: new.py\n+one\n+two\n*** End Patch"

    changes = patched_files(envelope, named_document("other.py", ""))

    assert not changes[0].path_exists
    assert changes[0].before is None
    assert changes[0].after == "one\ntwo\n"


def test_delete_file_carries_no_postimage() -> None:
    envelope = "*** Begin Patch\n*** Delete File: gone.py\n*** End Patch"

    changes = patched_files(envelope, document("body\n"))

    assert changes[0].before == "body\n"
    assert changes[0].after is None
    assert changes[0].path_exists


def test_move_reports_source_deletion_and_destination_write() -> None:
    envelope = (
        "*** Begin Patch\n*** Update File: old.py\n*** Move to: new.py\n"
        "@@\n context\n*** End Patch"
    )

    changes = patched_files(envelope, moved_document)

    assert [change.path for change in changes] == ["old.py", "new.py"]
    assert changes[0].before == "context\n"
    assert changes[0].after is None
    assert changes[1].before == "replaced\n"
    assert changes[1].after == "context\n"
    assert changes[1].path_exists


def test_one_envelope_yields_every_file_it_touches() -> None:
    envelope = (
        "*** Begin Patch\n*** Add File: a.py\n+a\n*** Delete File: b.py\n*** End Patch"
    )

    changes = patched_files(envelope, named_document("b.py", "b\n"))

    assert [change.path for change in changes] == ["a.py", "b.py"]


def test_context_headers_and_end_of_file_anchor_the_native_result() -> None:
    envelope = (
        "*** Begin Patch\n*** Update File: module.py\n@@ def second():\n"
        "-    return 2\n+    return 3\n*** End of File\n*** End Patch"
    )
    before = "def first():\n    return 2\n\ndef second():\n    return 2\n"

    changes = patched_files(envelope, document(before))

    assert changes[0].after == (
        "def first():\n    return 2\n\ndef second():\n    return 3\n"
    )


@pytest.mark.parametrize(
    ("envelope", "message"),
    [
        ("no header at all", "Begin Patch"),
        ("*** Begin Patch\n*** Add File: a.py\n+x", "End Patch"),
        ("*** Begin Patch\n*** End Patch", "no file changes"),
        (
            "*** Begin Patch\n*** Add File: a.py\nnot-added\n*** End Patch",
            "no added lines",
        ),
        (
            "*** Begin Patch\n*** Mystery File: a.py\n*** End Patch",
            "unsupported patch section",
        ),
        (update_envelope("m.py", " missing"), "does not match"),
    ],
)
def test_an_envelope_the_parser_cannot_vouch_for_raises(
    envelope: str, message: str
) -> None:
    """Refusing to guess keeps the caller on its conservative branch."""
    with pytest.raises(ValueError, match=message):
        patched_files(envelope, document("other\n"))


def bundled_dispatcher() -> ModuleType:
    path = Path.cwd() / ".codex/plugins/lup/hooks/scripts/policy.py"
    spec = importlib.util.spec_from_file_location("bundled_codex_policy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def worktree(root: Path) -> Path:
    (root / ".git").mkdir(parents=True)
    return root


def patch_payload(envelope: str) -> JsonObject:
    return {"tool_name": "apply_patch", "tool_input": {"command": envelope}}


class TestDispatchedPatches:
    def test_an_ordinary_small_edit_is_allowed(self, tmp_path: Path) -> None:
        root = worktree(tmp_path / "feature")
        target = root / "module.py"
        target.write_text(GREETING, encoding="utf-8")
        envelope = update_envelope(
            str(target), ' def greet():\n-    return "hi"\n+    return "hello"'
        )

        decision = bundled_dispatcher().dispatch(patch_payload(envelope))

        assert decision.effect == "allow"

    def test_a_human_owned_file_is_still_protected_in_a_sibling_worktree(
        self, tmp_path: Path
    ) -> None:
        """The rule matches README.md repo-relative, not cwd-relative."""
        root = worktree(tmp_path / "feature")
        target = root / "README.md"
        target.write_text("# Title\n", encoding="utf-8")
        envelope = update_envelope(str(target), "-# Title\n+# Rewritten")

        decision = bundled_dispatcher().dispatch(patch_payload(envelope))

        assert decision.effect != "allow"

    def test_an_antipattern_is_denied_after_native_decoding(
        self, tmp_path: Path
    ) -> None:
        root = worktree(tmp_path / "feature")
        target = root / "module.py"
        target.write_text(GREETING, encoding="utf-8")
        envelope = update_envelope(
            str(target),
            '+import re\n def greet():\n     return "hi"',
        )

        decision = bundled_dispatcher().dispatch(patch_payload(envelope))

        assert decision.effect == "deny"
        assert "import-re" in decision.reason

    def test_every_file_in_a_safe_multi_file_patch_is_allowed(
        self, tmp_path: Path
    ) -> None:
        root = worktree(tmp_path / "feature")
        first = root / "first.py"
        second = root / "second.py"
        first.write_text("value = 1\n", encoding="utf-8")
        second.write_text("value = 2\n", encoding="utf-8")
        envelope = (
            f"*** Begin Patch\n*** Update File: {first}\n@@\n"
            "-value = 1\n+value = 3\n"
            f"*** Update File: {second}\n@@\n"
            "-value = 2\n+value = 4\n*** End Patch"
        )

        decision = bundled_dispatcher().dispatch(patch_payload(envelope))

        assert decision.effect == "allow"

    def test_a_protected_file_wins_a_multi_file_batch(self, tmp_path: Path) -> None:
        root = worktree(tmp_path / "feature")
        safe = root / "module.py"
        protected = root / "README.md"
        safe.write_text("value = 1\n", encoding="utf-8")
        protected.write_text("# Title\n", encoding="utf-8")
        envelope = (
            f"*** Begin Patch\n*** Update File: {safe}\n@@\n"
            "-value = 1\n+value = 2\n"
            f"*** Update File: {protected}\n@@\n"
            "-# Title\n+# Rewritten\n*** End Patch"
        )

        decision = bundled_dispatcher().dispatch(patch_payload(envelope))

        assert decision.effect == "ask"

    def test_an_ordinary_delete_reaches_the_deletion_policy(
        self, tmp_path: Path
    ) -> None:
        root = worktree(tmp_path / "feature")
        target = root / "obsolete.py"
        target.write_text("value = 1\n", encoding="utf-8")
        envelope = f"*** Begin Patch\n*** Delete File: {target}\n*** End Patch"

        decision = bundled_dispatcher().dispatch(patch_payload(envelope))

        assert decision.effect == "allow"

    def test_a_move_checks_the_protected_source_path(self, tmp_path: Path) -> None:
        root = worktree(tmp_path / "feature")
        source = root / "README.md"
        destination = root / "README.old.md"
        source.write_text("# Title\n", encoding="utf-8")
        envelope = (
            f"*** Begin Patch\n*** Update File: {source}\n"
            f"*** Move to: {destination}\n@@\n # Title\n*** End Patch"
        )

        decision = bundled_dispatcher().dispatch(patch_payload(envelope))

        assert decision.effect == "ask"
        assert "human-authored" in decision.reason

    def test_an_unparsable_envelope_never_reports_allow(self) -> None:
        with pytest.raises(ValueError):
            bundled_dispatcher().dispatch(patch_payload("garbage"))

    def test_an_unparsable_envelope_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = json.dumps(patch_payload("garbage"))
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

        with pytest.raises(SystemExit) as raised:
            bundled_dispatcher().main()

        assert raised.value.code == 2


def test_worktree_path_relativizes_against_the_holding_worktree(
    tmp_path: Path,
) -> None:
    root = worktree(tmp_path / "feature")

    resolved = bundled_dispatcher().worktree_path(str(root / "src" / "app.py"))

    assert resolved == "src/app.py"
