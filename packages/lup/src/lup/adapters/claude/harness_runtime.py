"""Independent Claude CLI capability probes for harness diagnostics."""

from pathlib import Path

import sh
from pydantic import BaseModel

from lup.harness.contracts import CapabilityProbe
from lup.harness.models import CapabilityEvidence


class ClaudeCliEvidence(BaseModel, frozen=True):
    executable: Path
    arguments: list[str] = []
    output: str = ""


class ClaudeCapabilityProbe(CapabilityProbe[ClaudeCliEvidence]):
    """Probe exactly one named Claude CLI capability."""

    def __init__(
        self,
        capability: str,
        arguments: list[str],
        executable: Path = Path("claude"),
    ) -> None:
        self.capability = capability
        self.arguments = list(arguments)
        self.executable = executable

    def probe(self) -> CapabilityEvidence[ClaudeCliEvidence]:
        try:
            command = sh.Command(str(self.executable))
            output = str(command(*self.arguments))
        except (sh.CommandNotFound, sh.ErrorReturnCode):
            return CapabilityEvidence(
                capability=self.capability,
                supported=False,
                evidence=ClaudeCliEvidence(
                    executable=self.executable,
                    arguments=self.arguments,
                ),
                version="missing",
            )
        try:
            version = (
                output if self.arguments == ["--version"] else str(command("--version"))
            )
        except (sh.CommandNotFound, sh.ErrorReturnCode):
            version = "unknown"
        return CapabilityEvidence(
            capability=self.capability,
            supported=True,
            evidence=ClaudeCliEvidence(
                executable=self.executable,
                arguments=self.arguments,
                output=output,
            ),
            version=version.strip(),
        )


def claude_capability_probes(
    plugin_root: Path,
    executable: Path = Path("claude"),
) -> list[ClaudeCapabilityProbe]:
    """Compose version, plugin, and hook-validation evidence independently."""
    return [
        ClaudeCapabilityProbe("claude-cli", ["--version"], executable),
        ClaudeCapabilityProbe("plugins", ["plugin", "--help"], executable),
        ClaudeCapabilityProbe(
            "hooks",
            ["plugin", "validate", str(plugin_root)],
            executable,
        ),
    ]
