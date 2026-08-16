# lup

Lup 0.2 is a typed capability-composition library for Claude SDK and Codex
app-server agents. Importing `lup` loads no optional provider SDK.

The package-root API is intentionally small: `SessionFactory`, typed turn
request/result and capability-handle models, `turn_request()`, and `query()`.
Narrow capabilities live in `lup.runtime.contracts`; concrete provider config
and composition roots live in `lup.adapters.claude` and `lup.adapters.codex`.

```python
from pathlib import Path

from pydantic import BaseModel

from lup.adapters.codex.runtime import (
    CodexSessionConfig,
    create_codex_session_factory,
)


class Summary(BaseModel):
    title: str
    points: list[str]


factory = create_codex_session_factory(
    CodexSessionConfig(model="gpt-5.5", cwd=Path.cwd())
)
result = await factory.query("Summarize the findings", Summary)
summary = result.output
```

`SessionFactory` is a concrete class over one typed `SessionOpener` callable;
`query()` is a member of it, and `lup.query` is that same function reached as a
free alias, so `lup.query(factory, "Summarize the findings", Summary)` accepts
everything the method does. Both take a prompt string, a `TurnInput`, or a
prepared `turn_request(...)`, and each spelling infers one exact
`TurnResult[T]`. `turn_request()` builds the request when a caller needs to hold
one — a background agent mapping state to requests, or a session started by
hand. `SessionFactory.open()` yields a transparent `SessionHandle`. Await
`handle.session.start(request)` to receive a `TurnHandle`, then await
`turn.turn.result()`. Live events, interruption, steering, and forking are
optional capabilities on those handles; absence is represented by `None`.

Structured results use one portable mechanism: the session binds an exact
Pydantic schema to a fresh `submit_output` store before native input is
accepted. Native structured-output modes are disabled. A successful
`TurnResult[T]` therefore always contains a validated `T`; no submission is a
typed error.

Other modules provide independently composable runtime decorators, semantic
policy, deterministic harness generation/reconciliation, the persisted
resolver, MCP helpers, workspace/history support, scheduling, telemetry, and
sandboxing. See the repository's
[library guide](https://github.com/joy-void-joy/lup/blob/main/docs/library.md)
and
[architecture guide](https://github.com/joy-void-joy/lup/blob/main/docs/architecture.md).
Runnable factory, wrapper, background, profile, endpoint, route, and policy
compositions live in the repository
[examples](https://github.com/joy-void-joy/lup/tree/main/examples).
