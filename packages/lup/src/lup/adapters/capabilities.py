"""Versioned evidence-backed runtime capability matrix.

The table describes capabilities supplied in ``SessionHandle`` and
``TurnHandle`` values. Unsupported capabilities are absent; diagnostics never
invoke operations and catch an unsupported-operation exception.

It sits beside the adapters rather than in an application because that is
what it is evidence *about*: each column is one native contract at the
version this library supports, so a release that moves an adapter moves the
row describing it. Held downstream instead, the matrix would keep describing
whichever SDK the adopter first vendored while the code beneath it changed.

Distinct from :mod:`lup.harness.evidence`, which records the CLI versions
installed on one machine to trigger the doctor's drift check. This records
what a contract can do; that records what is currently sitting on disk.
"""

from pydantic import BaseModel

from lup.harness.models import MarkdownTable
from lup.markdown import PlainCell, TableCell


class CapabilityCell(BaseModel, frozen=True):
    """One supported, absent, or qualified capability fact."""

    capability: str
    value: bool | str


class AdapterCapabilities(BaseModel, frozen=True):
    """One adapter evidence column at its tested native contract version."""

    name: str
    cells: list[CapabilityCell]


def cells(**facts: bool | str) -> list[CapabilityCell]:
    """Preserve declaration order while constructing one evidence column."""
    return [
        CapabilityCell(capability=capability, value=value)
        for capability, value in facts.items()
    ]


def canonical_capability_matrix() -> list[AdapterCapabilities]:
    """Return the checked-in evidence for the supported native contracts."""
    return [
        AdapterCapabilities(
            name="claude-sdk-0.2.89",
            cells=cells(
                live_events=True,
                interrupt=True,
                steer=False,
                fork=True,
                resume=True,
                typed_submission="reconnect per turn",
                background=True,
            ),
        ),
        AdapterCapabilities(
            name="codex-app-server-0.144.4",
            cells=cells(
                live_events=True,
                interrupt=True,
                steer=True,
                fork=True,
                resume="without a fresh dynamic tool",
                typed_submission="thread-start schema only",
                background=True,
            ),
        ),
    ]


def capability_cell(value: bool | str) -> TableCell:
    """One evidence fact as the matrix shows it: supported, absent, qualified."""
    match value:
        case True:
            return PlainCell(text="✅")
        case False:
            return PlainCell(text="—")
        case str(qualification):
            return PlainCell(text=qualification)


def capability_matrix(adapters: list[AdapterCapabilities]) -> MarkdownTable:
    """One row per capability and one column per adapter, as a table part."""
    return MarkdownTable(
        headers=["Capability", *[adapter.name for adapter in adapters]],
        rows=[
            [
                PlainCell(text=cell.capability),
                *[capability_cell(adapter.cells[row].value) for adapter in adapters],
            ]
            for row, cell in enumerate(adapters[0].cells)
        ],
    )
