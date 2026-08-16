<!-- Generated from lup.devtools.harness.content.docs.index by `uv run lup-devtools harness generate all` — edit the source, not this file. See docs/harness.md. -->

# Lup documentation

Lup is three components in one repository. Start with the one you are actually
touching.

| Component | What it is | Guide |
| --- | --- | --- |
| `packages/lup` | The reusable, provider-neutral library. Session and turn engine, harness compiler, permission kernel, resolver, code scanner. Published standalone; never imports the application. | [library.md](library.md) |
| `src/lup_template` | The application template. The agent you customize, the `lup-devtools` CLI, and the environment that runs a session. Built on the library. | [template.md](template.md) |
| `.claude/`, `.codex/`, `.agents/`, `AGENTS.md` | The harness — the native plugin trees. Compiler output from typed Python, committed so a checkout is launchable with no build step. Carries the roster of every skill and agent the plugin ships. | [harness.md](harness.md) |

## Reference

Subjects that span the three components, or that are large enough to own a page.

| Page | Answers |
| --- | --- |
| [architecture.md](architecture.md) | Why the seams are where they are: one capability per ABC, adapters at the edge, structured output with one mechanism. |
| [patterns.md](patterns.md) | The recurring code shapes: declaration-plus-renderer, closed-by-construction, the typed-matcher router, and the engine-versus-surface split. |
| [orchestration.md](orchestration.md) | The delegation catalog: subagent, nested, background, and deferred tools, and when to reach for each. |
| [permissions.md](permissions.md) | How a shell command, fetch, or edit becomes allow, ask, defer, or deny — and how the generated hooks decide identically without importing the library. |
| [rules.md](rules.md) | Every executable Lup rule, its matching shape, its diagnostic, and the module that enforces it. |
| [resolver.md](resolver.md) | How reviewed feedback becomes concerns, worktrees, workers, and an accepted integration branch. |
| [supervisor.md](supervisor.md) | The local page that watches a resolver run and answers its questions. |
| [platform-differentiation.md](platform-differentiation.md) | Every intended Claude/Codex difference, and the parity decision for each generated artifact family. |
| [native-capabilities.md](native-capabilities.md) | The evidence ledger: which native contracts are proven, at which versions, and the release gaps. |
| [self-improvement.md](self-improvement.md) | How to turn an observed agent failure into a durable capability change. |

## Working in this repository

| Page | Answers |
| --- | --- |
| [contributing.md](contributing.md) | How to get set up, where a change of each kind belongs, and what has to be green before it lands. |
| [conventions.md](conventions.md) | The lookup behind each code-convention rule: which library, which typed stand-in for a dict, which parser, which resolver tool. |
| [quality-pipeline.md](quality-pipeline.md) | The three check layers, and what each one uniquely catches. |
| [dev-tooling-decisions.md](dev-tooling-decisions.md) | The architectural decisions behind the development tooling, each stated against the current system. |

## Every page here is generated

Files under `docs/` are compiler output from typed Python — the pages about
the library from `packages/lup/src/lup/devtools/harness/content/docs/`, the
pages about this repository from
`src/lup_template/devtools/harness/content/docs/` — the same way the native
trees are. Each opens with a banner naming its source module. Edit the module
and regenerate; a hand-edit is preserved and reported as a conflict rather
than silently overwritten. [harness.md](harness.md) is the whole story.
