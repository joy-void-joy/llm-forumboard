"""Provider-neutral session/turn engine: how any one agent turn runs.

Not :mod:`lup.realtime` — that package is the sleep/wake lifecycle machinery
for persistent agents; ``runtime`` is the layer underneath it, opening
provider sessions and driving typed turns through them. Modules map to
concerns:

- :mod:`lup.runtime.contracts` — the narrow capability seams (session
  factory, turn, event stream, interrupt, steer, fork, output binding);
  imports are type-only.
- :mod:`lup.runtime.models` — the immutable turn vocabulary every
  implementation shares (identifiers, blocks, events, requests, results,
  capability handles).
- :mod:`lup.runtime.errors` — typed turn and session-transition failures.
- :mod:`lup.runtime.composition` — builds sessions and turns from adapter
  callbacks and capabilities.
- :mod:`lup.runtime.wrappers` — decorators around whole logical turns
  (timeouts, budgets, retries, persistence, tracing, usage, display).
- :mod:`lup.runtime.routing` and :mod:`lup.runtime.config` — first-match
  selection of a configured factory recipe.
- :mod:`lup.runtime.output` — validated submitted-output stores and the
  portable submission tool binding.
- :mod:`lup.runtime.query` — the one-shot single-turn convenience.
- :mod:`lup.runtime.usage` — portable usage arithmetic and pricing.
- :mod:`lup.runtime.background` — a debounced consumer of the session
  capabilities that coalesces state wakes into turns on one persistent
  session.
- :mod:`lup.runtime.threads` — blocking calls on a process-lifetime executor,
  so work in flight outlives any one loop's teardown.
- :mod:`lup.runtime.quota` — waiting out a provider account allowance, as
  distinct from a budget: the work is still wanted, just not yet runnable.
- :mod:`lup.runtime.budget` — a durable, shared spending ceiling over a
  rolling period, which waits for the window rather than failing the turn.

``contracts`` and ``models`` define the vocabulary; every other module
composes them; the named adapter packages implement them.
"""
