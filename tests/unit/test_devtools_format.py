# lup: ignore[set-shape]
# Test fixtures and assertions construct these shapes deliberately.
"""Tests for the shared display helpers in ``devtools.utils``.

These pin the contract the table/sha helpers replaced hard-coded widths and
slice lengths with: columns size to their own contents (a cell wider than its
header is never clipped), per-column alignment is honored, and short shas come
from one width so every table abbreviates identically.
"""

from lup.devtools.utils import SHORT_SHA_LENGTH, format_table, short_sha


def test_columns_fit_the_widest_cell() -> None:
    # A value far wider than its header must appear in full, not clipped.
    long_branch = "feature/some-very-long-branch-name-that-exceeds-any-fixed-width"
    table = format_table(("Branch", "State"), [(long_branch, "open")])
    assert long_branch in table


def test_cells_align_under_their_header() -> None:
    table = format_table(("Branch", "State"), [("main", "merged")])
    header, _sep, row = table.splitlines()
    # Left-justified first column means the second column starts at one offset.
    assert header.index("State") == row.index("merged")


def test_separator_spans_the_header_width() -> None:
    table = format_table(("A", "Bee"), [("xx", "y")])
    header, sep = table.splitlines()[:2]
    assert set(sep) == {"-"}
    assert len(sep) == len(header)


def test_right_align_pads_on_the_left() -> None:
    table = format_table(
        ("name", "count"),
        [("a", "5"), ("bb", "1000")],
        aligns=("left", "right"),
    )
    rows = table.splitlines()[2:]
    # Column 1 sizes to "name" (4), column 2 to "count" (5); the right-aligned
    # numbers therefore share a common right edge regardless of value length.
    assert rows == ["a        5", "bb    1000"]


def test_header_only_table_has_no_rows() -> None:
    table = format_table(("Project", "Behind"), [])
    assert len(table.splitlines()) == 2


def test_short_sha_uses_the_shared_width() -> None:
    full = "0123456789abcdef0123456789abcdef01234567"
    assert short_sha(full) == full[:SHORT_SHA_LENGTH]


def test_short_sha_leaves_shorter_input_untouched() -> None:
    assert short_sha("abc") == "abc"
