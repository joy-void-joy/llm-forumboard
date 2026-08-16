(All code in this repository has been reviewed by humans. This README has been written by a human)

# Lup

A meta repository for speed-boosting your Claude Code and Codex development and create self-improving [ClaudeAgentSDK] and [CodexSDK](https://learn.chatgpt.com/docs/codex-sdk) applications

<img width="1535" height="863" alt="image" src="https://github.com/user-attachments/assets/d5159e28-1669-433b-8f89-e012c3abfa1c" />
<img width="1532" height="554" alt="image" src="https://github.com/user-attachments/assets/6f272a6e-71b6-4720-89bf-6b811081343d" />

# Why this repo?

I believe that claude code may be underappreciated right now. Not just the agent, but its [SDK](https://platform.claude.com/docs/en/agent-sdk/overview). The SDK of claude code allows it to:

- Think through a task by using many tools to fetch information, and decompose it carefully
- Delegate part of its task to other agents through tool calls that augment or gatekeep the results
- React in real time through auto-deny hooks and get_current_state tools

... all of that through your claude code subscription, without having to pay any extra API cost or setup.

The basic pattern is to create a ClaudeAgentSDK client, connect many tools to it (like fetching from APIs, searching the web, executing code, interacting with the world) and let claude code decide when to call them.

I've found developing those SDK applications with the help of Claude Code to work very well, as well as using Claude Code to improve the whole development scaffolding, using it to add /-commands that speeds up my development, to document general principles and developing devtools to help me or itself navigate it faster. More importantly, Claude Code can review results from past sessions, and tweak the agent based on it, be it its tools, prompts, or all aspect of the pipeline and workflow.

This repository is focused on this sort of agent-improvement and meta-self-improvement. It contains tools for storing the traces of all past agents, versioning the current agent, commands for reviewing them and seeing how to improve based on it, common multi-agents patterns I've found useful, as well as meta-commands to add commands or review your own development with Claude Code.

Over writing and reusing this technique over the past month, I have come to find that having a template and plugin as a base can really speed up the development and the coherence of Claude. This repository is a sort of extract of all the common patterns and plugin command and scripts I have found useful.

It is a template to help bootstrap this pattern and create your own ClaudeAgentSDK easily.

# Examples

Some examples of things I'm using this self-scaffolding for (still in early WIP):

- [joy.void.joy-bot]: Not yet opensource. A forecasting agent written for the [FutureEval] tournament. Basically this repo with news-searching and many API-fetching tools, and using the feedback-loop mechanism on newly resolved/retrodicted forecast
- [harmon]: real-time discord bot focused on presence and reactivity/helpfulness, as well as background tasks. The tools here are things like reply, follow_ups, sleep, and contains a gate that forbids it from replying if it hasn't read the new messages first.
- [mettle]: A bot whose main tool is writing its own tools
- [botc]: Having bots compete with one another while playing [Blood on the Clocktower]

But you could use it for so much more. Real-time monitoring, mathematical proofs or formal verification or for [[AIMO3]], anything that can be automated where the kind of resources or tools it needs is easy to explore and refine.

# Getting started

To start using this repo either:

- For a fresh repository: Use the "Use this template" button on github, or clone this repository. In the newly cloned repository, use /lup:init [description of your project] or /lup:brainstorm to first flesh out the broad shape of it
- For an already existing repository, clone lup inside it, and either use /lup:install to install the bare plugin, or /lup:install --interactive to install the plugin and walk through which pieces of the scaffolding (hooks, commands, devtools, CLAUDE.md sections) to bring over

You will need to install [uv] for python management, and [fzf] and [jq] for fuzzy-file matching. Docker is an additional dependency if you plan to use the sandboxing capabilities (set `AGENT_SANDBOX_ENABLED=false` to run without it).

To run the inner agent once everything is synced:

```bash
uv run lup run "your task here"            # single session
uv run lup loop "task1" "task2"            # batch with auto-commit
AGENT_SDK=codex AGENT_MODEL=gpt-5.5 uv run lup run "same task, Codex backend"
```

The application selects one provider only in its concrete composition root and
passes a configured `SessionFactory` everywhere else. A turn is started with a
typed `TurnRequest`; structured output is accepted only through the turn-bound
`submit_output` tool. Missing or invalid submissions are errors, never empty
successes. Compatibility endpoints and profiles are immutable adapter config
transforms rather than alternate runtimes.

### Runtime capabilities

Optional behavior is present in `SessionHandle` and `TurnHandle` or absent as
`None`; there are no unsupported-operation stubs and completed output is not
advertised as a live stream. This checked-in evidence targets Claude Agent SDK
0.2.89 and Codex CLI/app-server 0.144.4; regenerate it with
`uv run lup-devtools agent capabilities --markdown` when native evidence
changes.

| Capability | claude-sdk-0.2.89 | codex-app-server-0.144.4 |
| --- | --- | --- |
| live_events | ✅ | ✅ |
| interrupt | ✅ | ✅ |
| steer | — | ✅ |
| fork | ✅ | ✅ |
| resume | ✅ | without a fresh dynamic tool |
| typed_submission | reconnect per turn | thread-start schema only |
| background | ✅ | ✅ |

The Codex app server currently accepts dynamic tools only on `thread/start`.
Lup therefore rejects a typed-schema transition or typed resume before input
instead of silently using a stale schema or losing conversation identity.

The development harness is generated from one typed catalog. Run
`uv run lup-devtools harness claude` or `uv run lup-devtools harness codex`;
the old `lup-devtools claude` launcher was removed. Both generated plugins run
the same hermetic semantic shell/edit/fetch policy without importing this
checkout at hook time. See the [documentation index](docs/README.md),
[architecture](docs/architecture.md),
[harness authoring](docs/harness.md),
the [application guide](docs/template.md),
[runtime examples](examples/README.md),
[resolver lifecycle](docs/resolver.md),
the [generated rule reference](docs/rules.md),
[native capability ledger](docs/native-capabilities.md),
and [contributing](docs/contributing.md).
Release history is in [CHANGELOG.md](CHANGELOG.md).

The intended workflow while using this repository is to:

- Have it cloned as a bare repo with worktrees as siblings under `tree/`: `git clone --bare <url> myproject.git && cd myproject.git && git worktree add tree/main main`
- When working on a new feature, branching off with `uv run lup-devtools dev worktree create <branch-name>`
- Going into this new worktree, and working on it there
- Then /lup:commit it
- When it works and you've tested it works well /lup:rebase it
- Review it in github before merging it
- Call /lup:close on the merged branch or /lup:clean-gone on any branch (like main) to keep the worktree clean

# Overview

This repository contains many elements and code template that are designed to make creating your own scaffolding with ClaudeAgentSDK seamless:

- Code template and utilities to create a ClaudeAgentSDK with appropriate tools and hooks from scratch
- Many quality of life improvement to the claude code experience through a lup plugin
- devtools aimed at both human use and agent use
- Feedback loop and note-taking mechanisms for auditing and improving your agent

## Intended workflow

### Meta development

The Claude Code setup itself is treated as part of the product. Whenever a pain point shows up in your workflow, you fix the workflow, not just the instance: /lup:add-command and /lup:modify-command create and evolve slash commands, /lup:hooks edits the permission hook patterns, /lup:meta reviews the whole .claude structure and brainstorms improvements, and /lup:principle propagates a general principle across the entire repo. Downstream projects can pull improvements from this template with /lup:update, and patterns that emerged downstream flow back here with /lup:import.

### Worktree management

Development happens in git worktrees — one directory per branch, siblings under `tree/` — so several features can be worked on in parallel without switching files in place. `uv run lup-devtools dev worktree create <name>` creates one (and syncs dependencies and plugins), `lup-devtools dev check` runs the pre-flight (format, lint, pyright, pytest) before a PR, /lup:rebase pushes the branch and opens a PR with a cleaned-up history, /lup:close merges the approved PR, and /lup:clean-gone prunes worktrees whose branches are gone. `uv run lup-devtools dev branches` and `dev survey` show branch containment and PR status at a glance.

### Feedback loop

Every agent session writes its traces, outputs, and session JSONs under `notes/traces/<version>/`. This data stays out of git by default (`notes/*` is gitignored); domains that want each run's outputs versioned opt in during /lup:init, which removes the ignore lines and keeps the auto-commit loop. /lup:feedback-loop orchestrates the analysis over them: collect metrics (`lup-devtools feedback collect`), read traces deeply (`lup-devtools trace show`), classify what failed and why, then implement changes — tools first, prompts last. /lup:bump versions the agent (`[tool.lup] agent_version` in pyproject.toml) so results stay comparable across behavior changes.

# More thorough description

## Code template

### lib

`packages/lup` is the standalone library — a uv workspace member that any project can depend on without modification. It contains narrow runtime capabilities and typed handles (`lup.runtime`), concrete Claude/Codex configs and factories (`lup.adapters`), semantic policy and native hook boundaries (`lup.policy`), deterministic harness compilation (`lup.harness`), the persisted DAG resolver (`lup.resolver`), MCP tool creation (`lup.mcp`), workspace/history support, scheduling, telemetry, and the Docker sandbox. It is configured through validated component-owned models and explicit factory composition, never a broad options object or global engine registry.

### agent

`src/lup_template/agent` is the part the feedback loop improves: the orchestration (`core.py`), the system prompts (`prompts.py`), the output models (`models.py`), the SDK-agnostic subagent specs (`subagents.py`), the MCP tools (`tools/`), and the tag-based tool policy (`tool_policy.py`) that excludes tools whose API keys are missing. /lup:init renames and customizes this package for your domain.

### Environment

`src/lup_template/environment` is the domain scaffolding around the agent — user interaction, game logic, application flow. It exposes the `lup` CLI entry point (`uv run lup run "task"`, `uv run lup loop "task1" "task2"`) that runs sessions and auto-commits their results. It evolves with your application's requirements, but not via the feedback loop.

## Claude code plugin

This repository contains many quality of life improvements over the barebone claude code experience:

- Hooks for automatically approving and denying edition and code executions: I am too worried with potential prompt injections and hallucination to let Claude Code run python unprompted. Likewise, I have found that letting claude code in auto-edit mode makes a patch of code that's quite unreadable with many questionable decision, no matter the initial direction and content of Claude.md. On the other hand, manually reviewing everything is exhausting and leads to counterproductive decision-fatigue where you just approve everything repeatedly. I have found that auto-denying python calls while pre-approving investigative commands (see #devtools) means it's manageable, and same for auto-accepting small edits.
- Commands and meta-commands for modifying your experience whenever you find a pain point (like /lup:add-command or /lup:meta)
- subagents specialized in reading the traces and the different versions of your project, and understanding the strength of one version over another
- fzf fuzzy matching for @ file references

### Subagents

The plugin ships five subagents that do context-heavy work in their own window and return a compact report:

- **trace-explorer** — reads many session traces in bulk and returns cross-cutting patterns (tool failures, capability gaps, reasoning quality)
- **version-explorer** — retrieves and diffs agent code across version tags
- **version-reviewer** — holistic review of one agent version: its prompt, its performance data, what to keep and what to change
- **implementer** — TDD implementer that writes production code to make tests pass but will not touch test files
- **resolve-editor** — implements one persisted resolver concern inside its isolated leased worktree

### Hooks

One semantic policy suite drives both generated native dispatchers. It parses
every shell segment, normalizes URL scopes, evaluates every edit in a batch,
and applies the canonical marker, protected-path, size, and anti-pattern rules.
The generated plugin runtimes are hermetic snapshots; `/lup:hooks` changes the
canonical policy inputs and regenerates both targets. Approval effects that a
native boundary cannot represent fail closed.

### Claude commands

To speed up development, many claude commands and meta-commands are built in this repository:

- add-command
- modify-command
- meta
- principle

- bump

- commit
- rebase
- merge
- clean-gone
- close

- create-investigator
- debug
- review

- feedback-loop (and its fb-status / fb-investigate / fb-analyze / fb-reflect / fb-implement phases)

- hooks
- meta
- principle
- review

- refactor
- refactor-tools

- update
- import
- brainstorm
- install
- init

## Devtools

All development tooling is exposed as the `lup-devtools` CLI (run `uv run lup-devtools --help` for the full tree), aimed at both human use and agent use:

- `agent` — agent introspection and debugging (inspect, serve-tools, repl)
- `harness` — deterministic Claude/Codex generation, reconciliation, and launch
- `py` — Python module introspection (info, source, eval, imports, search)
- `dev` — worktrees, branches, PRs, conflicts, and pre-flight checks
- `feedback` — feedback state, metrics, and session commits
- `setup` — interactive setup wizard for integrations and API keys
- `sync` — upstream sync tracking against the lup template
- `trace` — trace display, search, and analysis
- `usage` — Claude Code usage display
- `version` — agent version, changelog, and bump
