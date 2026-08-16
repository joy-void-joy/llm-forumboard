# Runtime composition examples

These modules are executable compositions, not abbreviated fragments. Run
them from the repository root with `uv`; provider-backed examples use your
existing CLI credentials and make a real model call.

```bash
uv run -m examples.one_shot
uv run -m examples.wrapper_stack
uv run -m examples.background_agent
uv run -m examples.profile_transform
uv run -m examples.model_route
```

`profile_transform` expects a Claude configuration home at `~/.claude-work`.
Change that path to a profile you own. `compatible_endpoint` expects an
Anthropic-compatible service on `http://localhost:4000`:

```bash
uv run -m examples.compatible_endpoint
```

The policy pair wires the semantic policies into a session's hooks, so the
call a policy denies is a call the session refuses — the fetch path and the
tool-call path of the same declared origin table:

```bash
uv run -m examples.semantic_policy
uv run -m examples.semantic_policy_shell
```

Both make a real model call. Their enforcement is checked without one by
`tests/unit/test_policy_examples.py`, which drives each example's own
session configuration through the hooks the SDK would invoke.

Each composition keeps provider construction at the application boundary.
The query, wrappers, background scheduler, and router depend only on narrow
runtime contracts once the factory has been built.
