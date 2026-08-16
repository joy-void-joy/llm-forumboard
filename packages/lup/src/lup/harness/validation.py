"""Whole-tree validation of a rendered artifact tree, plus its result types.

Runs between rendering and reconciliation: ``validated_tree`` is the one gate
every command that builds output passes, and it refuses to continue on any
issue. ``ValidationResult`` is defined here because validators are the only
producers; this module also owns the ``ArtifactValidator`` seam they
implement.
"""

from abc import ABC, abstractmethod
from collections import Counter

from pydantic import BaseModel

from lup.harness.banner import ARTIFACT_COMMENT_ROUTER
from lup.harness.generation import ArtifactValidationError
from lup.harness.models import Artifact, ArtifactTree


class ValidationIssue(BaseModel, frozen=True):
    semantic_id: str
    message: str


class ValidationResult(BaseModel, frozen=True):
    issues: list[ValidationIssue] = []

    @property
    def valid(self) -> bool:
        return not self.issues


class ArtifactValidator(ABC):
    """Validate a complete in-memory artifact tree."""

    @abstractmethod
    def validate(self, tree: ArtifactTree) -> ValidationResult:
        """Return every deterministic validation issue."""


class BannerValidator(ArtifactValidator):
    """Hold every artifact whose format admits a comment to declaring one.

    ``Artifact`` already refuses content that does not open with the banner it
    declares, so what is left to catch here is the artifact that declares
    nothing: a generated file whose format could carry its provenance and
    does not.
    """

    def validate(self, tree: ArtifactTree) -> ValidationResult:
        return ValidationResult(
            issues=[
                ValidationIssue(
                    semantic_id=artifact.semantic_id,
                    message=(
                        f"artifact {artifact.path} declares no generated-from "
                        "banner, and its format admits one"
                    ),
                )
                for artifact in tree.artifacts
                if artifact.banner is None
                and ARTIFACT_COMMENT_ROUTER.route_for(artifact.path) is not None
            ]
        )


class DeterministicTreeValidator(ArtifactValidator):
    """Validate path uniqueness, ordering, identifiers, and normalized text."""

    def validate(self, tree: ArtifactTree) -> ValidationResult:
        path_counts = Counter(artifact.path.as_posix() for artifact in tree.artifacts)
        duplicate_issues = [
            ValidationIssue(
                semantic_id="artifact-tree",
                message=f"duplicate target path {path!r}",
            )
            for path, count in path_counts.items()
            if count > 1
        ]
        identifier_issues = [
            ValidationIssue(
                semantic_id="artifact-tree",
                message=f"artifact {artifact.path} has no semantic object id",
            )
            for artifact in tree.artifacts
            if not artifact.semantic_id
        ]
        newline_issues = [
            ValidationIssue(
                semantic_id=artifact.semantic_id,
                message=f"artifact {artifact.path} does not end in one LF newline",
            )
            for artifact in tree.artifacts
            if artifact.content
            and (
                not artifact.content.endswith("\n") or artifact.content.endswith("\n\n")
            )
        ]
        order = [artifact.path.as_posix() for artifact in tree.artifacts]
        order_issues = (
            []
            if order == sorted(order)
            else [
                ValidationIssue(
                    semantic_id="artifact-tree",
                    message="artifact paths are not in deterministic order",
                )
            ]
        )
        return ValidationResult(
            issues=[
                *duplicate_issues,
                *identifier_issues,
                *newline_issues,
                *order_issues,
            ]
        )


TREE_VALIDATORS: list[ArtifactValidator] = [
    DeterministicTreeValidator(),
    BannerValidator(),
]
"""Every gate a complete output tree passes unless the caller supplies its own."""


def validated_tree(
    artifacts: list[Artifact],
    validators: list[ArtifactValidator] | None = None,
) -> ArtifactTree:
    """Sort a complete output tree and reject any deterministic issue."""
    tree = ArtifactTree(
        artifacts=sorted(artifacts, key=lambda item: item.path.as_posix())
    )
    issues = [
        issue
        for validator in validators or TREE_VALIDATORS
        for issue in validator.validate(tree).issues
    ]
    if issues:
        raise ArtifactValidationError(
            "; ".join(f"{issue.semantic_id}: {issue.message}" for issue in issues)
        )
    return tree
