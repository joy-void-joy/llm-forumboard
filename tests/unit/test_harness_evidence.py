"""Evidence-ledger version-drift trigger tests."""

from pathlib import Path

from pydantic import BaseModel, Field
import yaml

from lup.devtools.dev.commit_guard import DRIFT_COMMAND
from lup.devtools.dev.workflow import (
    CHECK_COMMAND,
    WORKFLOW_PATH,
    write_workflow,
)
from lup_template.devtools.harness.catalog import WORKFLOW
from lup.devtools.harness.evidence import (
    EVIDENCE_LEDGER,
    EvidenceEntry,
    evidence_drift,
    parse_version,
    sdk_evidence_drift,
)

STALE_LEDGER = [EvidenceEntry(capability="codex-cli", version="0.144.4")]


class WorkflowStep(BaseModel, frozen=True):
    """Workflow step fields relevant to native evidence execution."""

    name: str | None = None
    run: str | None = None


class WorkflowJob(BaseModel, frozen=True):
    """Workflow job fields that enforce evidence ordering."""

    needs: list[str] = Field(default_factory=list)
    condition: str | None = Field(default=None, alias="if")
    continue_on_error: bool = Field(default=False, alias="continue-on-error")
    steps: list[WorkflowStep]


class NativeWorkflow(BaseModel, frozen=True):
    """Validated native-workflow job graph."""

    jobs: dict[str, WorkflowJob]


def test_doctor_flags_an_installed_component_newer_than_the_ledger() -> None:
    drift = evidence_drift("codex-cli", "codex-cli 0.145.0", STALE_LEDGER)

    assert drift is not None
    assert drift.installed == "0.145.0"
    assert drift.accepted == "0.144.4"
    assert "re-probe" in drift.message
    assert "docs/native-capabilities.md" in drift.message


def test_matching_and_older_installed_versions_do_not_drift() -> None:
    assert evidence_drift("codex-cli", "codex-cli 0.144.4", STALE_LEDGER) is None
    assert evidence_drift("codex-cli", "codex-cli 0.143.9", STALE_LEDGER) is None


def test_unknown_capabilities_and_unparseable_banners_stay_silent() -> None:
    assert evidence_drift("novel-cli", "novel-cli 9.9.9", STALE_LEDGER) is None
    assert evidence_drift("codex-cli", "development build", STALE_LEDGER) is None


def test_version_parsing_handles_real_banner_shapes() -> None:
    assert parse_version("codex-cli 0.144.5") == [0, 144, 5]
    assert parse_version("2.1.211 (Claude Code)") == [2, 1, 211]
    assert parse_version("no digits here") is None


def test_longer_component_counts_compare_componentwise() -> None:
    ledger = [EvidenceEntry(capability="claude-cli", version="2.1")]

    assert evidence_drift("claude-cli", "2.1.1", ledger) is not None
    assert evidence_drift("claude-cli", "2.1.0", ledger) is None


def test_sdk_drift_reads_the_installed_distribution() -> None:
    newer = [EvidenceEntry(capability="claude-agent-sdk", version="0.0.1")]
    ancient = [EvidenceEntry(capability="claude-agent-sdk", version="999.0.0")]

    drift = sdk_evidence_drift(newer)
    assert drift is not None and drift.capability == "claude-agent-sdk"
    assert sdk_evidence_drift(ancient) is None


def test_shipping_ledger_carries_every_probed_contract() -> None:
    capabilities = [entry.capability for entry in EVIDENCE_LEDGER]

    assert capabilities == ["claude-cli", "claude-agent-sdk", "codex-cli"]


def test_native_workflow_probes_even_when_strict_evidence_fails() -> None:
    document = yaml.safe_load(
        Path(".github/workflows/native-nightly.yml").read_text(encoding="utf-8")
    )
    workflow = NativeWorkflow.model_validate(document)
    evidence = workflow.jobs["evidence"]
    native = workflow.jobs["native"]

    assert any(
        step.run == "uv run lup-devtools harness doctor all --strict-evidence"
        for step in evidence.steps
    )
    assert native.needs == ["evidence", "credentials"]
    assert native.condition is not None
    assert "always()" in native.condition
    assert "needs.credentials.outputs.live == 'true'" in native.condition
    assert native.continue_on_error is False
    assert any(step.run == "uv run pytest -m integration -v" for step in native.steps)


def test_pull_request_workflow_runs_the_same_gate_a_checkout_runs() -> None:
    """The gate is one command, so what CI enforces cannot drift from it.

    Every row this used to spell out is a row of that command, harness drift
    included; naming them again here is what let the two lists disagree. The
    drift step ahead of it is not a second list: it is the constant the commit
    hook installs, so a contributor who never armed the hook meets the same
    refusal here.
    """
    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    workflow = NativeWorkflow.model_validate(document)
    commands = [step.run for step in workflow.jobs["check"].steps if step.run]

    assert commands == ["uv sync --all-extras", DRIFT_COMMAND, CHECK_COMMAND]


def test_the_workflow_on_disk_is_the_one_the_declaration_renders() -> None:
    """Generated rather than scaffolded, so `dev check` reports it when it drifts."""
    write_workflow(WORKFLOW, check=True)
