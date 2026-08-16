"""Current-tree classification and the write/delete/conflict proposal.

``FilesystemCurrentTreeReader`` classifies what is on disk under prior
ownership proof; ``DeterministicReconciler`` compares that against the desired
tree and proposes writes, proven deletions, and explicit conflicts — without
side effects. The proposal row types are defined here because reconciliation
is their only producer; :mod:`lup.harness.materialization` applies them and
the devtools generation flow composes both. The source-patch digests guard
the backpropagation flow persisted by :mod:`lup.harness.proposals`.
"""

import hashlib
import json
import shlex
from pathlib import Path

from pydantic import BaseModel

from lup.harness.contracts import CurrentTreeReader, Reconciler
from lup.harness.generation import artifact_map
from lup.harness.models import Artifact, ArtifactTree
from lup.harness.ownership import OwnershipCategory, OwnershipManifest


class CurrentArtifact(BaseModel, frozen=True):
    path: Path
    content: str
    category: OwnershipCategory
    sha256: str
    executable: bool = False


class CurrentTree(BaseModel, frozen=True):
    root: Path
    artifacts: list[CurrentArtifact]

    def digest(self) -> str:
        """Hash classified paths and content hashes without exposing contents."""
        rows = [
            {
                "path": artifact.path.as_posix(),
                "sha256": artifact.sha256,
                "category": artifact.category,
            }
            for artifact in sorted(
                self.artifacts, key=lambda item: item.path.as_posix()
            )
        ]
        encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ProposedWrite(BaseModel, frozen=True):
    artifact: Artifact
    previous_sha256: str | None = None
    previous_executable: bool | None = None


class ProposedDelete(BaseModel, frozen=True):
    path: Path
    prior_ownership_sha256: str


class ReconciliationConflict(BaseModel, frozen=True):
    path: Path
    category: OwnershipCategory
    message: str
    sensitive: bool = False


class ReconciliationProposal(BaseModel, frozen=True):
    id: str
    root: Path
    writes: list[ProposedWrite] = []
    deletes: list[ProposedDelete] = []
    conflicts: list[ReconciliationConflict] = []
    base_digest: str


class SourcePreimageRow(BaseModel, frozen=True):
    """One named source path and its optional preimage hash."""

    path: str
    sha256: str | None


def source_patch_preimages(content: str) -> list[Path]:
    """Read canonical preimage paths from a git-format source patch."""
    paths: list[Path] = []  # lup: ignore[empty-collection]
    old_candidate: Path | None = None
    new_candidate: Path | None = None
    awaiting_old_header = False
    for line in content.splitlines():
        if line.startswith("diff --git "):
            if awaiting_old_header:
                raise ValueError("source patch diff is missing its old-file header")
            fields = shlex.split(line)  # lup: ignore[string-split] — shell lexer
            if len(fields) != 4:
                raise ValueError("malformed git source-patch header")
            old_name, new_name = fields[2:]
            if not old_name.startswith("a/") or not new_name.startswith("b/"):
                raise ValueError("source-patch paths must be repository-relative")
            old_candidate = Path(old_name[2:])
            new_candidate = Path(new_name[2:])
            awaiting_old_header = True
            continue
        if not awaiting_old_header or not line.startswith("--- "):
            continue
        header = line[4:]
        if header == "/dev/null":
            if new_candidate is None:
                raise ValueError("new-file source patch has no destination")
            paths.append(new_candidate)
        elif (
            old_candidate is not None
            and header.startswith("a/")
            and old_candidate == Path(header[2:])
        ):
            paths.append(old_candidate)
        else:
            raise ValueError("source patch old-file header does not match its diff")
        awaiting_old_header = False
        old_candidate = None
        new_candidate = None
    if awaiting_old_header:
        raise ValueError("source patch diff is missing its old-file header")
    if not paths:
        raise ValueError("source patch contains no git file entries")
    unique = sorted(dict.fromkeys(paths), key=Path.as_posix)
    for path in unique:
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("source-patch path escapes the repository")
    return unique


def source_patch_base_digest(root: Path, content: str) -> str:
    """Hash every source-patch preimage, including required file absence."""
    resolved_root = root.resolve()
    rows = [
        source_preimage_row(root, resolved_root, relative)
        for relative in source_patch_preimages(content)
    ]
    encoded = json.dumps(
        [row.model_dump(mode="json") for row in rows],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def source_preimage_row(
    root: Path, resolved_root: Path, relative: Path
) -> SourcePreimageRow:
    """Validate and hash one repository-relative source preimage."""
    path = (root / relative).resolve()
    if not path.is_relative_to(resolved_root):
        raise ValueError("source-patch path escapes the repository")
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return SourcePreimageRow(path=relative.as_posix(), sha256=digest)


class FilesystemCurrentTreeReader(CurrentTreeReader):
    """Classify a root from prior ownership proof and explicit local paths."""

    def __init__(
        self,
        manifest: OwnershipManifest | None,
        *,
        local_only: list[Path] | None = None,
        sensitive_local_only: list[Path] | None = None,
        managed_paths: list[Path] | None = None,
    ) -> None:
        self.manifest = manifest
        self.local_only = list(local_only or [])
        self.sensitive_local_only = list(sensitive_local_only or [])
        self.managed_paths = list(managed_paths) if managed_paths is not None else None

    def read(self, root: Path) -> CurrentTree:
        resolved_root = root.resolve()
        owned = (
            {item.path.as_posix(): item for item in self.manifest.files}
            if self.manifest is not None
            else {}
        )
        local = {path.as_posix() for path in self.local_only}
        sensitive = {path.as_posix() for path in self.sensitive_local_only}
        artifacts: list[CurrentArtifact] = []
        if not root.exists():
            return CurrentTree(root=root, artifacts=[])
        if self.managed_paths is None:
            paths = sorted(item for item in root.rglob("*") if item.is_file())
        else:
            paths = sorted(
                root / relative
                for relative in self.managed_paths
                if (root / relative).is_file()
            )
        for path in paths:
            relative = path.relative_to(root)
            key = relative.as_posix()
            if not path.resolve().is_relative_to(resolved_root):
                artifacts.append(
                    CurrentArtifact(
                        path=relative,
                        content="",
                        category="sensitive_local_only",
                        sha256="",
                    )
                )
                continue
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            executable = bool(path.stat().st_mode & 0o111)
            if key in sensitive:
                category: OwnershipCategory = "sensitive_local_only"
                content = ""
            elif key in local:
                category = "local_only"
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    category = "unknown_conflict"
                    content = ""
            elif key in owned:
                record = owned[key]
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    category = "unknown_conflict"
                    content = ""
                else:
                    category = (
                        "generated"
                        if record.sha256 == digest
                        else "backpropagation_candidate"
                    )
            else:
                category = "unknown_conflict"
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    content = ""
            artifacts.append(
                CurrentArtifact(
                    path=relative,
                    content=content,
                    category=category,
                    sha256=digest,
                    executable=executable,
                )
            )
        return CurrentTree(root=root, artifacts=artifacts)


class DeterministicReconciler(Reconciler):
    """Propose safe writes and proven obsolete deletions without side effects."""

    def __init__(self, *, adopt_exact_backpropagation: bool = True) -> None:
        self.adopt_exact_backpropagation = adopt_exact_backpropagation

    def propose(
        self, current: CurrentTree, desired: ArtifactTree
    ) -> ReconciliationProposal:
        present = {artifact.path.as_posix(): artifact for artifact in current.artifacts}
        wanted = artifact_map(desired)
        writes: list[ProposedWrite] = []  # lup: ignore[empty-collection]
        conflicts: list[ReconciliationConflict] = []  # lup: ignore[empty-collection]
        for key, artifact in wanted.items():
            existing = present.get(key)  # lup: ignore[dict-get]
            if existing is None:
                writes.append(ProposedWrite(artifact=artifact))
                continue
            if existing.category == "generated":
                if (
                    existing.content != artifact.content
                    or existing.executable != artifact.executable
                ):
                    writes.append(
                        ProposedWrite(
                            artifact=artifact,
                            previous_sha256=existing.sha256,
                            previous_executable=existing.executable,
                        )
                    )
                continue
            # An owned path whose content is not what the generator wants is
            # regenerated rather than adjudicated. The manifest says the
            # generator owns it, and a divergence is far more often a textual
            # merge of a derived file than a hand-edit: a resolver join merges
            # both plugin trees and their ownership proof, leaving digests that
            # describe neither parent, which read here as an edit nobody made
            # and refused generation with no recovery a caller could reach.
            # The equal-content case below keeps its native-override nuance,
            # since re-owning what already matches is the different question.
            if (
                existing.category == "backpropagation_candidate"
                and existing.content != artifact.content
            ):
                writes.append(
                    ProposedWrite(
                        artifact=artifact,
                        previous_sha256=existing.sha256,
                        previous_executable=existing.executable,
                    )
                )
                continue
            if (
                existing.category == "unknown_conflict"
                or (
                    existing.category == "backpropagation_candidate"
                    and self.adopt_exact_backpropagation
                )
            ) and existing.content == artifact.content:
                if existing.executable != artifact.executable:
                    writes.append(
                        ProposedWrite(
                            artifact=artifact,
                            previous_sha256=existing.sha256,
                            previous_executable=existing.executable,
                        )
                    )
                continue
            if existing.category in {"local_only", "sensitive_local_only"}:
                conflicts.append(
                    ReconciliationConflict(
                        path=artifact.path,
                        category=existing.category,
                        message="desired output collides with preserved local configuration",
                        sensitive=existing.category == "sensitive_local_only",
                    )
                )
                continue
            conflicts.append(
                ReconciliationConflict(
                    path=artifact.path,
                    category=existing.category,
                    message="current content requires explicit reconciliation",
                )
            )
        deletes = [
            ProposedDelete(
                path=artifact.path,
                prior_ownership_sha256=artifact.sha256,
            )
            for artifact in current.artifacts
            if artifact.category == "generated"
            and artifact.path.as_posix() not in wanted
        ]
        base_digest = current.digest()
        proposal_rows = {
            "base": base_digest,
            "writes": [write.artifact.path.as_posix() for write in writes],
            "deletes": [delete.path.as_posix() for delete in deletes],
            "conflicts": [conflict.path.as_posix() for conflict in conflicts],
        }
        proposal_id = hashlib.sha256(
            json.dumps(proposal_rows, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()[:16]
        return ReconciliationProposal(
            id=proposal_id,
            root=current.root,
            writes=writes,
            deletes=deletes,
            conflicts=conflicts,
            base_digest=base_digest,
        )
