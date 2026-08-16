# lup: ignore[import-re]
# ``re`` is imported only for the ``re.Pattern[str]`` annotation on the
# helper — the tests exercise the renamer's own regexes, not ad-hoc parsing.
"""Tests for the package renamer's matchers and leftover reporting.

The import matcher alone once shipped a broken downstream CLI: dotted
module-path string anchors like ``resources.files("lup_template.devtools")``
survived the rename and crashed at runtime with ``ModuleNotFoundError``.
These tests pin the string-anchor pass and the stale-reference report
that make such misses impossible or at least visible.
"""

import re
from pathlib import Path

from lup_template.devtools.dev.init import (
    PACKAGE_IMPORT_RE,
    PACKAGE_STRING_ANCHOR_RE,
    find_stale_references,
    is_renamer_module,
    rename_pattern_in_file,
)


def apply_rename(tmp_path: Path, source: str, pattern: re.Pattern[str]) -> str:
    path = tmp_path / "mod.py"
    path.write_text(source)
    rename_pattern_in_file(path, pattern, "syra", dry_run=False)
    return path.read_text()


class TestStringAnchorMatcher:
    def test_renames_resources_files_anchor(self, tmp_path: Path) -> None:
        result = apply_rename(
            tmp_path,
            'ASSETS = resources.files("lup_template.devtools.dashboard")\n',
            PACKAGE_STRING_ANCHOR_RE,
        )
        assert result == 'ASSETS = resources.files("syra.devtools.dashboard")\n'

    def test_renames_single_quoted_entry_point_string(self, tmp_path: Path) -> None:
        result = apply_rename(
            tmp_path,
            "ENTRY = 'lup_template.devtools.main:app'\n",
            PACKAGE_STRING_ANCHOR_RE,
        )
        assert result == "ENTRY = 'syra.devtools.main:app'\n"

    def test_renames_mock_patch_target(self, tmp_path: Path) -> None:
        result = apply_rename(
            tmp_path,
            'with mock.patch("lup_template.agent.config.load"):\n    pass\n',
            PACKAGE_STRING_ANCHOR_RE,
        )
        assert 'mock.patch("syra.agent.config.load")' in result

    def test_leaves_bare_string_literal_alone(self, tmp_path: Path) -> None:
        source = 'NAME = "lup_template"\n'
        assert apply_rename(tmp_path, source, PACKAGE_STRING_ANCHOR_RE) == source

    def test_leaves_framework_marker_lines_alone(self, tmp_path: Path) -> None:
        source = 'EP = "lup_template.devtools.main:app"  # lup-devtools\n'
        assert apply_rename(tmp_path, source, PACKAGE_STRING_ANCHOR_RE) == source

    def test_leaves_unquoted_prose_alone(self, tmp_path: Path) -> None:
        source = '"""Doc table: `from lup_template.*` becomes target imports."""\n'
        assert apply_rename(tmp_path, source, PACKAGE_STRING_ANCHOR_RE) == source


class TestImportMatcherThroughSharedCore:
    def test_renames_from_import(self, tmp_path: Path) -> None:
        result = apply_rename(
            tmp_path,
            "from lup_template.agent.config import Settings\n",
            PACKAGE_IMPORT_RE,
        )
        assert result == "from syra.agent.config import Settings\n"

    def test_import_pass_ignores_string_anchors(self, tmp_path: Path) -> None:
        source = 'ASSETS = resources.files("lup_template.devtools.dev")\n'
        assert apply_rename(tmp_path, source, PACKAGE_IMPORT_RE) == source


class TestRenamerSelfExclusion:
    def test_recognizes_the_renamer_module(self) -> None:
        assert is_renamer_module(Path("src/pkg/devtools/dev/init.py"))

    def test_ignores_sibling_modules(self) -> None:
        assert not is_renamer_module(Path("src/pkg/devtools/dev/app.py"))


class TestStaleReferenceReport:
    def test_reports_surviving_references_with_locations(self, tmp_path: Path) -> None:
        module = tmp_path / "src" / "pkg" / "mod.py"
        module.parent.mkdir(parents=True)
        module.write_text('"""Paths live under src/lup_template/devtools/."""\n')
        (tmp_path / "pyproject.toml").write_text('name = "x"\n')

        stale = find_stale_references(tmp_path)

        assert len(stale) == 1
        assert "mod.py:1:" in stale[0]
        assert "src/lup_template/devtools/" in stale[0]

    def test_skips_the_renamer_module(self, tmp_path: Path) -> None:
        renamer = tmp_path / "src" / "pkg" / "devtools" / "dev" / "init.py"
        renamer.parent.mkdir(parents=True)
        renamer.write_text('OLD = "lup_template"\n')

        assert find_stale_references(tmp_path) == []
