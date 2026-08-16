"""Deterministic concern-DAG validation and scheduling."""

from collections.abc import Collection

from lup.resolver.models import Concern


class ConcernGraphError(ValueError):
    """The concern graph references a missing node or contains a cycle."""


class ConcernGraph:
    """Validated immutable graph with deterministic topological batches."""

    def __init__(self, concerns: list[Concern]) -> None:
        self.concerns = {concern.id: concern for concern in concerns}
        if len(self.concerns) != len(concerns):
            raise ConcernGraphError("concern ids must be unique")
        missing = sorted(
            {
                dependency
                for concern in concerns
                for dependency in concern.dependencies
                if dependency not in self.concerns
            }
        )
        if missing:
            raise ConcernGraphError(
                f"concern graph references missing nodes: {', '.join(missing)}"
            )
        self.topological_batches()

    def topological_batches(self) -> list[list[Concern]]:
        """Return independent roots together and every child after its parents."""
        remaining = {
            identifier: set(concern.dependencies)  # lup: ignore[set-shape]
            for identifier, concern in self.concerns.items()
        }
        batches: list[list[Concern]] = []  # lup: ignore[empty-collection]
        completed: set[str] = set()  # lup: ignore[set-shape,empty-collection]
        while remaining:
            ready_ids = sorted(
                identifier
                for identifier, dependencies in remaining.items()
                if dependencies <= completed
            )
            if not ready_ids:
                cycle = ", ".join(sorted(remaining))
                raise ConcernGraphError(f"concern graph contains a cycle: {cycle}")
            batches.append([self.concerns[identifier] for identifier in ready_ids])
            completed.update(ready_ids)
            for identifier in ready_ids:
                del remaining[identifier]
        return batches

    def ancestors(self, concern_id: str) -> list[Concern]:
        """Return the complete transitive dependency set in topological order."""
        if concern_id not in self.concerns:
            raise ConcernGraphError(f"unknown concern {concern_id!r}")
        wanted: set[str] = set()  # lup: ignore[set-shape,empty-collection]
        pending = list(self.concerns[concern_id].dependencies)
        while pending:
            identifier = pending.pop()
            if identifier in wanted:
                continue
            wanted.add(identifier)
            pending.extend(self.concerns[identifier].dependencies)
        return [
            concern
            for batch in self.topological_batches()
            for concern in batch
            if concern.id in wanted
        ]

    def approved(self) -> list[Concern]:
        """Return eligible approved nodes whose entire ancestry is approved."""
        approved_ids = {
            concern.id
            for concern in self.concerns.values()
            if concern.eligible and concern.integration_approved
        }
        return self.transitively_approved(approved_ids)

    def transitively_approved(self, approved_ids: Collection[str]) -> list[Concern]:
        """Filter an explicit decision set through complete approved ancestry."""
        return [
            concern
            for batch in self.topological_batches()
            for concern in batch
            if concern.id in approved_ids
            and all(
                ancestor.id in approved_ids for ancestor in self.ancestors(concern.id)
            )
        ]
