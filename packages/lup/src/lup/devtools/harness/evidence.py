"""Typed native-evidence ledger backing the doctor's version-drift trigger.

`docs/native-capabilities.md` records the CLI and SDK versions each native
contract was last probed against. This module is the machine-readable row set
behind that prose: `lup-devtools harness doctor` compares the *installed*
versions against these accepted ones and surfaces a drift warning whenever an
installed component is newer — the trigger to re-probe the native contracts
and refresh the ledger. Locally the drift is a warning; the nightly lane runs
doctor with `--strict-evidence`, turning drift into a nonzero exit so evidence
re-probes have a schedule instead of a habit.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as installed_package_version

from pydantic import BaseModel

# lup: ignore[constant-declaration] — the date this evidence was last read from
# the vendors, which is a fact about the reading rather than a value to pick
EVIDENCE_REFRESHED = "2026-08-05"


class EvidenceEntry(BaseModel, frozen=True):
    """One accepted contract version, keyed by its probe capability name."""

    capability: str
    version: str


EVIDENCE_LEDGER = [
    EvidenceEntry(capability="claude-cli", version="2.1.222"),
    EvidenceEntry(capability="claude-agent-sdk", version="0.2.89"),
    EvidenceEntry(capability="codex-cli", version="0.145.0"),
]


class EvidenceDrift(BaseModel, frozen=True):
    """One installed component that is newer than its accepted evidence."""

    capability: str
    installed: str
    accepted: str

    @property
    def message(self) -> str:
        return (
            f"{self.capability} {self.installed} is newer than the evidence "
            f"ledger's {self.accepted} (refreshed {EVIDENCE_REFRESHED}); re-probe "
            "the native contracts and update docs/native-capabilities.md"
        )


def parse_version(text: str) -> list[int] | None:
    """Extract the first dotted-numeric version from a CLI banner or metadata."""
    for token in text.split():
        parts = token.split(".")  # lup: ignore[string-split] — dotted version token
        if len(parts) >= 2 and all(part.isdigit() for part in parts):
            return [int(part) for part in parts]
    return None


def newer_than(installed: list[int], accepted: list[int]) -> bool:
    """Compare versions componentwise, padding the shorter with zeros."""
    width = max(len(installed), len(accepted))
    left = installed + [0] * (width - len(installed))
    right = accepted + [0] * (width - len(accepted))
    return left > right


def evidence_drift(
    capability: str,
    version_text: str,
    ledger: list[EvidenceEntry] | None = None,
) -> EvidenceDrift | None:
    """Report drift when an installed component is newer than its evidence row."""
    entries = EVIDENCE_LEDGER if ledger is None else ledger
    accepted = next(
        (entry for entry in entries if entry.capability == capability), None
    )
    if accepted is None:
        return None
    installed = parse_version(version_text)
    expected = parse_version(accepted.version)
    if installed is None or expected is None:
        return None
    if not newer_than(installed, expected):
        return None
    return EvidenceDrift(
        capability=capability,
        installed=".".join(str(part) for part in installed),
        accepted=accepted.version,
    )


def sdk_evidence_drift(
    ledger: list[EvidenceEntry] | None = None,
) -> EvidenceDrift | None:
    """Compare the installed Claude Agent SDK package against its evidence row."""
    try:
        installed = installed_package_version("claude-agent-sdk")
    except PackageNotFoundError:
        return None
    return evidence_drift("claude-agent-sdk", installed, ledger)
