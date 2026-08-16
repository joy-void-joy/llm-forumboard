"""Provider-neutral resolution of reviewed code concerns, split by concern.

Drives worker, review, and merge skill runs over a DAG of concerns, each on
its own branch in a leased worktree:

- :mod:`lup.resolver.models` — the schema-versioned semantic records
  (concerns, verdicts, questions, run state).
- :mod:`lup.resolver.dag` — concern-DAG validation and scheduling order.
- :mod:`lup.resolver.state` — atomic, file-locked persistence of run state.
- :mod:`lup.resolver.contracts` — the user-question delivery seam.
- :mod:`lup.resolver.orchestrator` — the git side effects: leases,
  worktrees, commits, and dependency bases, through the harness
  ``ProcessLauncher`` seam.
- :mod:`lup.resolver.core` — the state machine composing all of the above
  with the native skill invocations.

``models`` is the shared vocabulary the other modules import; ``core`` is
the only module that composes its siblings, so everything below it stays
independently testable.
"""
