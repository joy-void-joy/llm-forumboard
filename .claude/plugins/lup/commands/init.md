---
description: "Initialize the self-improvement loop for a specific domain"
allowed-tools: Bash(git:*, uv run lup-devtools:*, uv sync:*, uv run pyright:*, uv run ruff:*, uv run pytest:*), Read, Edit, Write, AskUserQuestion
---

# Initialize Self-Improvement Loop

This command sets up the project identity, renames the source package, and customizes the feedback collection, metrics, and trace analysis for your specific agent domain.

**This project builds on an agent SDK, not raw model API calls.** The SDK is the default and expected framework. If the user wants bare API calls instead, ask them to explain why -- the SDK provides structured outputs, tool use, subagents, and hooks out of the box.

## Your Task

Interview the user about their domain, rename the source package, and generate the appropriate scaffolding.

### The branch you start from is the library you get

This checkout is a clone of lup, so the branch it stands on *is* the library
version the project begins at: `packages/lup/` is that branch's code, and the
acquisition mode settled in Phase 2 pins that same ref. A feature branch
carries work the stable branch has not reviewed, and nothing downstream
announces that. Resolve both before Phase 0, while `origin` still points at lup
rather than at the project's own repository:

- `git rev-parse --abbrev-ref HEAD` — the branch the library would come from
- `git symbolic-ref --short refs/remotes/origin/HEAD` — what the remote treats as stable

When they differ, Ask the user with the AskUserQuestion tool, offering concrete options plus a free-text choice: whether to proceed from the checkout's current branch, which carries work the stable branch has not reviewed, or from the stable branch instead

Record the branch and the commit the answer settles on. Everything below is
about that commit — the acquisition mode pins its branch and the upstream
checkpoint is taken at it — so the checkout supplying the library has to be
standing there before you go on.

This checkout is the one supplying the library, and `packages/lup/` is whatever
branch is checked out — so if the answer was the stable branch, `git switch` to
it now, before Phase 0 reads anything.

## Phase 0: Check for DESIGN.md

Before starting the interview, check if `DESIGN.md` exists in the project root. If it does:

1. Read it thoroughly
2. Use it as context for the entire init process -- it contains design decisions from a `/lup:brainstorm` session
3. Still run the full interview, but reference design decisions when asking questions (e.g., "DESIGN.md mentions you want a persistent agent with sleep/wake -- does that still hold?")
4. Skip questions whose answers are unambiguously covered in the design doc
5. If no DESIGN.md exists, proceed normally

## Phase 1: Project Identity

Determine the project name by asking:

### 1. Project Name

- What should the project be called? This becomes the Python package name.
- Must be a valid Python identifier (lowercase, underscores, no hyphens or spaces).
- Examples: `aib`, `forecast_bot`, `coach`, `game_agent`

### 2. Agent Purpose

- What does the agent do? (forecasting, coaching, game playing, task completion, etc.)
- What is a "session" or "run"? (one forecast, one conversation, one game, one task)

### 3. Ground Truth & Success Metrics

- How do you know if the agent did well?
  - **External ground truth**: Outcomes that resolve later (predictions, game wins, task success)
  - **Human feedback**: Ratings, corrections, preferences
  - **Proxy metrics**: Engagement time, task completion, coherence scores
  - **Self-assessment**: Agent's own meta-reflection quality
  - **No clear ground truth**: Focus on process quality and trace analysis

### 4. What to Track

- What outputs should be saved per session?
- What metrics matter? (accuracy, cost, time, tool usage, user satisfaction)
- What trace data is valuable? (reasoning, tool calls, intermediate states)

### 5. Feedback Sources

- Where does feedback come from?
  - Resolution/outcome data
  - User ratings or corrections
  - Comparison against baselines
  - Expert review
  - Automated quality checks

### 6. Task Format

- How are tasks provided to the agent? (free text, IDs, files, API calls)
- Should the `loop` CLI command batch-process them?
- What does auto-commit look like for this domain?

## Interviewing Style

Ask extensively -- don't make assumptions about the domain. Ask open-ended questions first, then drill into specifics. Example questions (adapt based on context):

- "What should this project be called? (valid Python package name, e.g., 'aib', 'forecast_bot')"
- "What does your agent do and what does a single session look like?"
- "How do you know if the agent did well? Is there ground truth that resolves later?"
- "What metrics matter most to you?"
- "How are tasks provided -- free text, IDs, files, API calls?"
- "Should results auto-commit after each session?"
- "What tools or APIs will the agent need?"

Let the conversation flow naturally. The goal is to understand the domain well enough to customize the template files below.

## Phase 1.5: Prune Scaffolding

Before customizing, decide which optional patterns this domain actually needs. The template ships them all wired; most domains use a subset, and **deleting the rest is the goal, not a failure** (see the guidance file's § Scaffolding Is a Menu, Not a Mandate). From the interview answers, classify each as KEEP-and-customize or DELETE-the-files:

- **Reflection** (`agent/tools/reflect.py` + the gate wiring in `core.py`) — keep only if the agent commits a consequential, judgment-bearing output where self-critique helps.
- **Realtime / persistent mode** (`agent/tools/realtime.py`, `lup.realtime*`, the Stop-hook/sleep-wake wiring) — keep only for agents that live over time (chat, monitoring, games); delete for one-shot agents.
- **Feedback loop** (`devtools/feedback/`, the feedback-loop command) — keep only if ground truth or a feedback signal resolves over time.
- **Commit loop** (auto-commit in `environment/cli/__main__.py`) — keep only if each run yields a data artifact worth versioning. Session data is gitignored by default (the `notes/*` lines in `.gitignore`), so traces and outputs stay local; keeping this pattern means removing those two lines so session data can be committed. When deleting the pattern, leave the ignore lines in place.

Confirm the keep/delete set with the user, then **delete the files and their wiring** for everything not kept before proceeding. The customization steps below apply only to what you kept.

## Phase 2: Rename Package

Run the devtool to rename the package. Preview first with `--dry-run`, then execute:

```bash
uv run lup-devtools dev init rename-package <project> --dry-run
uv run lup-devtools dev init rename-package <project>
```

This handles directory rename (`src/lup_template/` -> `src/<project>/`), import updates, pyproject.toml entry points, CLI app name, and the plugin marketplace name -- all in one shot. The marketplace registration in each tree (.claude/plugins/.claude-plugin/marketplace.json under Claude Code, .agents/plugins/marketplace.json under Codex) is named `<project>` so it doesn't collide in the global marketplace namespace, while the plugin entry stays `lup` (so `/lup:*` is identical everywhere). Framework vocabulary (`lup_tool`, `lup-devtools`, `.lup/`, etc.) is preserved automatically.

### After renaming:

#### 1. Declare how the project obtains lup

The template ships the library vendored under `packages/lup/`, which makes the
project a fork of it. The rename is what allows leaving that mode: `dev library`
refuses to un-vendor while `src/lup_template/` is present, because an
uninitialized template and the lup repository are the same bytes and nothing
else separates them.

A project depends on `lup` as a package rather than keeping a copy of the
library's source. Half the answer is a fact to look up rather than a
preference — whether a release exists at all, and which:

```
uv run lup-devtools dev library release
```

It reports the released version, or that none is published yet, and prints the
command that declares what it found — so the release number is read from the
index rather than guessed at.

The other half is a judgement about what this project is to lup, and the
look-up does not make it. Ask the user which of these describes them:

| Mode | The project it is for | Command |
| --- | --- | --- |
| published | A consumer of the library: it takes releases and upgrades on its own schedule | `uv run lup-devtools dev library use published --version <release>` |
| **git** | Either nothing is published yet, or the project works *on* lup as well as with it — running a branch to dogfood it and sending changes back | `uv run lup-devtools dev library git --branch <branch>` |
| linked | The library is being developed alongside this project, in a checkout on the same disk | `uv run lup-devtools dev library link <checkout>` |

With nothing published, git is the only mode that resolves, so the look-up
settles it. Once a release exists, published is the quieter default and git
stays a live choice: a project that reads the library's own diffs, or that
expects to send work back, is better served by the branch it is improving than
by the last release cut from it. All three hand the project a real package, so
its `packages/lup/` stays absent and nothing has to be merged later. Vendoring
is not on this list — a vendored copy is a fork with all the reconciliation
that implies, and is only right for a project that genuinely intends to modify
library source.

The git mode resolves `subdirectory = "packages/lup"`, because the distribution sits inside the repository rather than at its root, and pins whichever ref you name. **The ref resolves against the remote, not against any checkout on disk**: uv fetches the branch as the remote has it, so work the remote has not seen is not in what you pinned. Before declaring a git source, read what the remote's branch actually resolves to — `git ls-remote origin <branch>` names that tip — and if it is not the recorded commit, say so rather than pinning a dependency whose contents you have not accounted for.

The extras come from what the project runs: `claude` and/or `codex` for the
adapters it drives, `docker` for the code-execution sandbox, `web` for the
session API. Name them in the requirement (`lup[claude,codex,docker]`).

The command prints the `uv sync` and the regeneration it wants next. Run both
before anything reads the project's types.

#### 2. Merge the guidance template into the guidance declaration

The merge lands in `src/<project>/devtools/harness/content/guidance.py`, never in a tree's guidance file (.claude/CLAUDE.md under Claude Code, AGENTS.md under Codex): those are generation's outputs, and an edit made directly to one is undone the next time the harness runs. Take the sections from that tree's template flavor (.claude/plugins/lup/TEMPLATE_CLAUDE.md under Claude Code, .codex/plugins/lup/TEMPLATE_AGENTS.md under Codex), covering every tree the project commits:

1. Read the template and replace `<project>` placeholders with the actual project name
2. Read the existing declaration
3. Use the `<!-- section: ... -->` markers in the template to identify independent merge units
4. Compare sections: for each marked section, check whether the declaration already composes it (by heading match)
5. Add missing sections to the declaration
6. Leave existing sections untouched -- don't overwrite content the project already has
7. Regenerate with `uv run lup-devtools harness generate all`, which is what carries the merged sections into every tree

Which runtimes the project carries is not a choice made here: every tree
arrives with the clone, and generation writes each one it finds. Dropping a
runtime is a later removal somebody decides on its own terms.

#### 3. Initialize upstream sync

Baseline the upstream checkpoint at *the recorded commit*, not at whatever the
remote's default branch points to. `--synced` reads the checkpoint from the
named checkout's HEAD, so that checkout has to be standing at the recorded
commit when this runs:

```
uv run lup-devtools sync setup lup <lup-checkout> --branch <branch> --synced
```

`setup` records that checkout, the branch settled on above, and its HEAD as the checkpoint, so `/lup:update` only shows commits that land afterward. Plain `sync mark-synced lup` is wrong here: the shipped `sync.json` entry carries a URL and no branch, so it clones the remote's default branch and checkpoints *that* HEAD — so every commit the project already carries comes back as unported work once the branch merges.

That checkout is one you provide: clone the library beside the project, then
`git switch --detach <commit>` it to the recorded commit. Not this project's
own checkout — it stands at that commit too, and naming it makes the review
read the project's own history as upstream work. The linked mode's checkout can
serve when it already stands there, but it is someone's working checkout and is
not yours to move.

A recorded path is read in place and never fetched, so whichever checkout you
name is the one to update before a review. The branch may also have advanced
since this project was cloned, and a checkpoint taken from its tip marks the
commits in between as already reviewed when the project does not carry them.

#### 4. Verify

```bash
uv sync
uv run pyright
uv run ruff check .
uv run pytest
<project> --help
```

## Phase 3: Generate Scaffolding

**Start by gathering every customization point.** Each decision the template leaves to a domain carries a `# lup: template:` marker with a one-line description of the decision. Collect them all:

```bash
uv run lup-devtools dev todos --json
```

Walk the collected decision points one by one — each entry gives the file, line, decision text, and surrounding context. For every marker, either customize the code it points at and remove the marker, or delete it along with scaffolding pruned in Phase 1.5. The numbered steps below give domain guidance for the major ones, but the gathered list is the source of truth: a marker you never reach is a decision silently defaulted.

Based on the answers from Phase 1, generate or modify:

### 1. `src/<project>/agent/models.py`

Customize AgentOutput for the domain:

- Add domain-specific fields (probability, move, response, etc.)

### 2. `src/<project>/agent/prompts.py`

Update the system prompt template for the domain. Focus on what the agent does and how to reason -- tools self-document via their descriptions, so listing them in the prompt creates a second source of truth that drifts as tools change.

### 3. `src/<project>/agent/subagents.py`

Create domain-appropriate subagents (researcher, analyzer, etc.)

### 4. `src/<project>/environment/cli/__main__.py`

Customize the CLI for the domain's task format:

- Update the `loop` command to accept domain-specific task inputs
- Customize `_commit_results()` message format (e.g., `data(forecasts):` instead of `data(sessions):`)
- Configure auto-commit behavior: enable/disable by default, target branch (main for data-only commits, or a dedicated branch) — requires the `notes/` ignore lines removed in Phase 1.5
- Add domain-specific CLI commands if needed

### 5. Agent Version

Set `agent_version` under `[tool.lup]` in `pyproject.toml` and explain bump rules for this domain.

### 6. Reflection (only if kept in Phase 1.5)

If this domain has no consequential, judgment-bearing output, you already deleted `reflect.py` and its gate — skip this step. Otherwise customize `src/<project>/agent/tools/reflect.py`:

- Extend `ReflectInput` with domain-specific fields (factor analysis, move evaluation, etc.)
- Customize the reviewer prompt for the domain's common failure modes
- The reviewer runs on the strongest aux model available (see the guidance file's § Model Selection); pass `skip_reviewer=True` per call for speed-sensitive or trivial tasks

The reflection gate (`lup.reflect`) is domain-neutral and doesn't need modification. Only the tool and its input model are domain-specific.

### 7. `devtools/feedback/state.py`

The feedback collection module (exposed via `uv run lup-devtools feedback collect`). Customize `load_outcomes()` and `compute_metrics()` for the domain's ground truth type.

### 8. Update the guidance

Edit `src/<project>/devtools/harness/content/guidance.py`, then regenerate with `uv run lup-devtools harness generate all` -- .claude/CLAUDE.md under Claude Code, AGENTS.md under Codex are its outputs, and editing them directly is undone by the next generation.

The guidance should already carry the template sections from the Phase 2 merge. Now add domain-specific content based on the interview answers:

- Fill in the Project Overview placeholder with the domain description
- Add domain-specific commands and examples
- Add metrics and feedback collection instructions relevant to this domain
- Add any domain-specific context sections (Important Context, data sources, constraints)

### 9. Tool Description Standards

The agent discovers tools through their descriptions -- a terse description means the agent can't tell when or why to use it. Each description should answer:

1. **What** -- What does this tool do? (concrete behavior, not vague summary)
2. **When** -- When should the agent reach for this tool? (triggers, conditions)
3. **Why** -- Why does this tool exist? (what problem it solves, what gap it fills)

See `src/<project>/agent/tools/example.py` for the pattern.

### 10. Setup Wizard (`src/<project>/devtools/setup.py`)

Customize the interactive setup wizard for the domain's integrations:

- Replace the template integrations (Slack, Google, Notion, Example API) with the domain's actual services
- Update the `INTEGRATIONS` list — each entry is an `Integration(name, env_keys, setup_func, status_func)`
- Add corresponding `@app.command()` subcommands for individual integration setup
- Update env var names in `config.py` to match what the setup wizard writes to `.env.local`
- Verify `lup-devtools dashboard` exposes the same registry: declarative fields become browser forms, while bespoke flows link back to their CLI command

The framework (env helpers, status table, mask, clipboard, browser open, wizard flow) is reusable — only the integration functions and registry need customization.

Ask the user:

- What external services does the agent use? (APIs, databases, messaging platforms)
- Which require OAuth flows vs simple API keys?
- Are there any credentials files to manage? (JSON tokens, certificates)

### 11. Update `feedback-loop.md`

Customize the feedback loop command for the domain's specific:

- Ground truth type
- Metrics to analyze
- Trace inspection approach

## Phase 4: Verify Setup

After generating files:

1. Run `uv run lup-devtools dev todos` -- any remaining `# lup: template:` marker is a decision not yet made; resolve or consciously defer each one. Resolving one means writing this domain's code where the placeholder stood and deleting the marker: it is not feedback, so it takes no `solved:` claim. Renaming the package cleared `[tool.lup] template`, so from here on `dev check` lists every marker still standing -- park one you mean to leave with `# lup: defer:` rather than letting it sit unexplained
2. Run `uv run pyright` to check types
3. Run `uv run ruff check .` to check lint
4. Run `uv run lup --help` to verify CLI
5. Verify the feedback loop command references the right scripts
6. Regenerate both harnesses and check that the rendered guidance accurately describes the domain

## After Initialization

Once the scaffolding is generated, guide the user to:

1. Run a few sessions: `uv run lup loop "task1" "task2"`
2. Review traces in `notes/traces/`
3. Use `/lup:feedback-loop` to analyze and improve
4. Iterate on the feedback collection as patterns emerge

## Key Files to Customize

- `src/<project>/agent/models.py` -- Output schemas (AgentOutput, SessionResult)
- `src/<project>/agent/subagents.py` -- Specialized subagents
- `src/<project>/agent/tool_policy.py` -- Tool availability and MCP servers
- `src/<project>/agent/core.py` -- Options building and orchestration
- `src/<project>/agent/tools/reflect.py` -- Reflection tool and nested reviewer agent
- `src/<project>/agent/prompts.py` -- System prompt templates
- `src/<project>/environment/cli/__main__.py` -- CLI with loop + auto-commit
- `src/<project>/devtools/setup.py` -- Setup wizard (integrations, env vars)
- `src/<project>/devtools/feedback/` -- Feedback collection
