"""The README capability matrix must match the probed adapter behavior.

This is what turns the parity matrix from prose into a contract: change
what an adapter refuses or supports and this test fails until the README
table is regenerated, so documentation cannot drift from code.
"""

from lup.adapters.capabilities import (
    canonical_capability_matrix,
    capability_matrix,
)
from lup.workspace.paths import project_root


def test_readme_capability_matrix_is_current() -> None:
    readme = (project_root() / "README.md").read_text(encoding="utf-8")
    expected = capability_matrix(canonical_capability_matrix()).text_payload

    assert expected in readme, (
        "README capability matrix is stale. Regenerate with "
        "`uv run lup-devtools agent capabilities --markdown` and paste it "
        "into the runtime capability section."
    )


def test_each_kind_of_fact_reaches_the_table_as_the_matrix_shows_it() -> None:
    """The three renderings, pinned where the README copy cannot pin them.

    The test above is the contract with the README and goes red whenever that
    copy is stale. This one holds the composition — supported, absent, and
    qualified — so a red README window never leaves the rendering unwatched.
    """
    table = capability_matrix(canonical_capability_matrix())
    rows = {row[0].render(): [cell.render() for cell in row[1:]] for row in table.rows}

    assert table.headers == [
        "Capability",
        "claude-sdk-0.2.89",
        "codex-app-server-0.144.4",
    ]
    assert rows["steer"] == ["—", "✅"]
    assert rows["resume"] == ["✅", "without a fresh dynamic tool"]


def test_probed_matrix_is_consistent() -> None:
    """Every adapter column probes the same rows, and known facts hold."""
    matrix = canonical_capability_matrix()

    assert [entry.name for entry in matrix] == [
        "claude-sdk-0.2.89",
        "codex-app-server-0.144.4",
    ]
    rows = [cell.capability for cell in matrix[0].cells]
    for entry in matrix:
        assert [cell.capability for cell in entry.cells] == rows

    by_name = {
        entry.name: {cell.capability: cell.value for cell in entry.cells}
        for entry in matrix
    }
    assert by_name["claude-sdk-0.2.89"]["live_events"] is True
    assert by_name["codex-app-server-0.144.4"]["live_events"] is True
    assert by_name["claude-sdk-0.2.89"]["interrupt"] is True
    assert by_name["codex-app-server-0.144.4"]["fork"] is True
    assert by_name["claude-sdk-0.2.89"]["fork"] is True
