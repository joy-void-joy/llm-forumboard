"""Canonical repository guidance.

The portable conventions are composed from ``lup.devtools.harness.content
.conventions`` rather than restated here, so this document holds only what is
true of *this* repository: its two-package layout, its tooling paths, and the
placement rule that follows from having both halves in one tree.

What earns space here is what an agent needs before it knows to look: a norm
no gate fires on, or a mechanism it must recognise the first time one stops
it. Anything a denial names at the moment it matters is left to the denial
and to the generated reference behind it — a second copy in always-loaded
prose can only fall behind the registry that actually runs, and is redundant
for as long as it agrees.
"""

import lup.devtools.harness.content.conventions as conventions
import lup.harness.models as models
from lup.codescan.common import RuleSelection


def guidance_parts(selection: RuleSelection) -> list[models.PromptPart]:
    """This repository's guidance, naming only the rules it still enforces."""
    return [
        models.TextPart(
            text=r"""# forumboard repository guidance

forumboard reads the claude.ai conversations of people who have enrolled, decides what may be published, removes what may not, and keeps a browsable picture of what is currently happening. It is built on the `lup` library, which arrives as a pinned dependency rather than as source in this tree.

## What This Repository Is About

Everything here serves one sequence, and the order of its gates is the design:

1. **Sync** — per enrolled profile, read the conversations that changed since that profile's cursor. A profile's session is a stored browser context, so an expired cookie is re-read rather than stalling the sync silently.
2. **Review** (Opus, no tools) — publish or skip, what must not appear, and a plan. The plan steers the editor and is **never published**.
3. **Edit** (Opus, no tools) — produce the page, including the keypoints that *are* published. The editor may abort outright and may redact past what the plan asked for; close reading finds what a skim misses, which is why it gets the second look.
4. **Publish** — one Notion page per conversation, keyed on the conversation id, rewritten in place when the conversation grows.
5. **Worldview** — an agent reads what is unmerged and rewrites the topic pages it touches, whole.
6. **Briefings** — periodic recaps, written from what was published.

### Two Invariants

**Redaction removes; it never marks.** A `[REDACTED]` beside a name announces that this person's something was sensitive, and often what kind. The contract is that the text must read as though the removed part was never said. It is declared once, in `agent/prompts/shared.py`, and composed into every prompt that touches a transcript — never restated in prose that could drift from it.

**A prompt is a composition, not a string.** `agent/prompts/` declares each pass as named pieces, and a gitignored `.lup/config/prompts.py` replaces any of them by name. Who reads this board lives there rather than in committed source, for the reason the roster does — and because a pass left to infer its audience infers a different one on different runs, which is how the same conversation is skipped as confidential once and published the next time. A replacement naming no declared piece raises rather than silently doing nothing. `forumboard prompts list` says what is declared and what this deployment replaced; `forumboard prompts show <pass>` prints what the model actually receives.

**The worldview is rewritten, never appended to.** It answers "what is happening now". A page that accumulates becomes a log, and a log cannot answer that. `write_topic` replaces a page; there is deliberately no append.

### Where Things Are

- `agent/` — the models each pass decides in, the prompts, and the composition root over lup's runtime.
- `claudeai/` — the unofficial conversation API, the per-profile browser session, and the streamed remote login. The whole fragile surface is here so an upstream change has one place to land.
- `notion/` — the workspace client, the database schema declared once, and the two repositories over it.
- `pipeline/` — the passes and the loops that drive them.
- `profiles.py` — the enrolment roster. A profile directory existing is **not** enrolment; `.lup/config/roster.json` is, and it is gitignored so who is being read is never published.

### Things That Will Catch You Out

- **Notion's API moved.** Since 2025-09-03 a database holds *data sources*, and queries, schemas, and page parents all name the data source rather than the database. Configuration records database ids because that is what a person can copy from a URL; `NotionWorkspace.data_source_of` is the one place they are bridged.
- **Shrinking a Notion page is opt-in.** `allow_deleting_content` defaults to false, so a rewrite that removes redacted material would silently no-op without it. Every rewrite here sets it.
- **Page bodies are Markdown.** Notion accepts and returns it directly — there is no block-conversion layer, and adding one would be re-solving a solved problem.
- **`.get(` trips the edit hook** even on a typed non-mapping receiver. Where the receiver is an SDK client, spell it another way (`client.request("GET", …)`) rather than reaching for a directive the whole-file audit would then call spurious.

"""
        ),
        *conventions.PLAN_AT_AGENT_SPEED,
        *conventions.AGENT_VOCABULARY,
        *conventions.THE_GATES,
        models.TextPart(text=r"""Change the policy those gates enforce with """),
        models.SkillInvocation(plugin="lup", skill="hooks"),
        models.TextPart(
            text=r""", which edits the canonical inputs in `lup.policy` and the `HookSet` in `devtools/harness/catalog.py`, regenerates both native plugins, and runs the shared fixture suite. Harness generation compiles one hermetic dispatcher and runtime per plugin, so never edit a generated dispatcher or runtime.

Harness settings stay project-level, in the tree the harness owns ("""
        ),
        models.NativePath(location="project_settings"),
        models.TextPart(
            text=r"""), which holds only the native settings outside that semantic policy boundary — never user-level.

### The `# lup:` Marker Vocabulary

A `# lup:` (or `// lup:`) comment is **actionable review feedback** left in the code for the agent to address — a quick bug remark, a feature idea, anything whose subject is the code and small enough that the site it concerns is the right place to keep it. Three flavors carry feedback, and only the removal rules differ. Two more spellings share the namespace without being feedback, and both go when what they annotate does: `# lup: ignore[<rule>]` is the anti-pattern hatch above, and `# lup: template: <decision>` marks a customization point this scaffold leaves to whoever adopts it.

| Marker | Removing it |
|---|---|
| `# lup: <text>` — open feedback | **denied**; resolve it into a claim instead |
| `# lup: solved: <text>` — a claim you addressed it | **denied**; only the verify-solved review pass retires one |
| `# lup: defer: <text>` — parked work | **denied** while parked |
| `# lup: template: <text>` — a customization point | **allowed**; nobody is owed an answer to a placeholder |

A customization marker is answered by writing this domain's own code where the scaffold's example stood, which leaves no original ask for a claim to be checked against — so it is deleted rather than converted, exactly as `ignore` is. `uv run lup-devtools dev todos` lists every one still standing (an alias for `dev comments --kind template`), and initialization walks them one by one.

The same marker reads two ways, and `[tool.lup] template` in `pyproject.toml` says which. While that flag stands, this repository is the scaffold itself and its markers are inventory: `dev check` counts them and says no more. Initialization clears the flag in the same rewrite that renames the package, and from then on every marker still standing lists in `dev check` as a decision this domain has not made. Advisory either way — a domain that means to leave one standing writes `# lup: defer:` and says why.

Resolve open feedback by fixing what it points at, or, for a question, by answering it definitively in the code, the docs, or a recorded user decision. Then rewrite the marker as **`# lup: solved: <the note's original words>`**, text unchanged, so the claim sits beside what it claims to fix and can be checked against what was asked. `docs/contributing.md` carries the full lifecycle (use `"""
        ),
        models.SkillInvocation(plugin="lup", skill="resolve"),
        models.TextPart(
            text=r"""`).

### Deferred Work

**Never create tracking files.** A `TODO.md`, backlog, or roadmap file parks a decision where no workflow will surface it again — deferral by tracking file is delegation to nobody. Work that is not being done now lives in one of three places, chosen by what it is attached to:

- **A `# lup: defer: <text>` note**, when the work belongs to a site in this code, where `dev check` keeps it visible until somebody wakes it. Default to the bare `defer:`; a bracketed `defer[<gate>]: <text>` states a real, externally-checkable gate, never that this code might change again.
- **A GitHub issue**, when the subject is the tooling misbehaving rather than the code — friction, a command that half-completes, a classifier reporting a failed probe as fact, output that makes no sense. Nothing in the tree owns that, so a note would have nowhere to sit; Reporting Friction below says what to record.
- **A question to the user**, when whether to defer at all is itself the open question.

`docs/contributing.md` carries the first and the last, and the one exception to all three — a `tmp/` briefing, which starts a fresh session on a situation this one cannot finish, and is rewritten whole rather than appended to.

---

## Development Workflow

Work in a **git worktree**, not a branch switched in place, and never commit _code_ directly to `dev`. Create one with `uv run lup-devtools dev worktree create feat-name` — it lands as a sibling under `tree/`, never nested inside another checkout — and then """
        ),
        models.RelocateSession(path="the path it prints"),
        models.TextPart(
            text=r""", because creating a worktree does not move the session, and edits left in the old checkout never reach the branch.

`docs/contributing.md` carries the two-tier branch model, the commit-type table, and the loop from a fresh worktree to a merged pull request.

"""
        ),
        *conventions.MERGE_CONFLICT_RESOLUTION,
        *conventions.COMMIT_GUIDELINES,
        models.TextPart(
            text=r"""---

## Code Conventions

Build on claude-agent-sdk and pydantic; `docs/conventions.md` names each library and what it is for, and puts each typed form beside the raw dict it replaces — including tool inputs, which are BaseModel classes with `Field(description=...)` that give both the `@tool` schema and the validation.

Use existing libraries from PyPI before writing raw HTTP or rebuilding a wheel.

**Model selection.** Default to the **strongest** tier for the main agent, every subagent, reviewer, and background agent. This runs on a subscription where the best model is the point: reach for a **balanced** tier only when latency or cost provably dominates and quality is non-critical, and for the **fast** tier almost never. A role that genuinely warrants a cheaper model declares that tier explicitly with a reason; otherwise it inherits the strongest default. Agent declarations state the tier, not a model id — each runtime spells the tier in its own lineup.

**Error handling.** A `@lup_tool` handler takes a validated model and returns one; raise `ToolError` to send a recoverable failure back as an MCP error, with a message saying what to do about it. The `is_error` envelope and the input-validation reply are the decorator's, not yours to assemble. Elsewhere, agent code raises for unrecoverable errors, wraps transient failures in `with_retry`, and validates inputs early with Pydantic. Never swallow one silently — log it, handle it, or re-raise. A catch-all `except Exception` is fine at a boundary that does one of those, such as a task loop or subagent delegation, which is why no rule refuses it.

**Placement, in this repository.** Everything this project writes belongs in `src/forumboard/`. `lup` is a pinned dependency, not source in this tree, so there is no second package to weigh a utility against: if logic already exists in `lup`, import it rather than copying it, and if something here would genuinely serve another project on lup, that is a pull request to lup rather than a file moved locally. `docs/library.md` carries the criterion.

"""
        ),
        *conventions.design_principles(selection),
        *conventions.SANCTIONED_EXCEPTIONS,
        models.TextPart(
            text=r"""---

## Tooling

`uv` is the package manager — `uv add <package>`, never edit pyproject.toml directly. Formatting and linting are ruff, type checking is pyright; `docs/contributing.md` carries the commands that have to be green.

When policy says a command genuinely has to run outside the sandbox, put that command through the runtime's native per-call sandbox escalation on its first attempt. Do not replace the whole session with an unsandboxed one; the semantic policy still judges the escalated call, so an allowed command can be approved at that narrower boundary.

### lup-devtools

Development tooling is exposed as the `lup-devtools` CLI entry point, composed in `src/forumboard/devtools/main.py` from two halves: the workflow commands the `lup` dependency ships, and what only this repository has beside them. The application's own CLI is `forumboard` (`src/forumboard/environment/cli/`), which also mounts the setup wizard so an operator never needs both. **Always use `lup-devtools` instead of ad-hoc commands.** Never use `uv run python -c "..."` or bare `python`/`python3` — these are denied by the Bash permission hook.

If you find yourself running the same command repeatedly, **add a command** to `src/forumboard/devtools/`. Where another project on lup would want it too, add it here first and offer it upstream separately — this tree cannot edit the library.

`tmp/` is scratch: gitignored, so nothing written there reaches a diff, a reviewer, or the human — which is why it does not execute. Match the rung to the question: to **read** code, `py info`/`py source`/`py search`/`py imports` plus the codeintel tools answer without running anything; to **compute** something, `lup-devtools py eval '<expression>'` auto-imports and evaluates in the sandbox; with no sandbox available, add a devtools command. `docs/contributing.md` carries the rest of the ladder, down to a heredoc behind a `# lup: escalate: <why>` marker. The argument is reviewability, not power — an agent may already edit `devtools/` and run it.

Run `uv run lup-devtools --help` for the command tree; `docs/template.md` lists the sub-apps, rendered from the same typed roster the CLI itself wires.

### Generated Trees

`lup-devtools harness generate all` regenerates and reconciles every native plugin; `harness <runtime>` regenerates one and launches it. `docs/harness.md` carries the rest of the loop, how a launch reaches the plugin on each runtime, and the roster of every skill and agent this plugin ships. Personal cache, trust, and session state are never committed.

Both rosters are rendered from typed declarations: what is about agent work the `lup` dependency declares, and what is this repository's lives in `src/forumboard/devtools/harness/content/catalog.py`, which composes both. Change the catalog that owns the subject, then regenerate. The shell vocabulary — including which `uv run` targets are admitted, and that `forumboard` runs outside the sandbox — is `harness/content/shell_vocabulary.py`.

### Code Intelligence

The `codeintel` tool group answers questions about code by *resolving* it, through a language server. **Prefer them over grep for anything about a name**, and prefer `rename_symbol` over an edit with `replace_all`, which cannot tell one scope from another; apply the edits it reports yourself. `docs/conventions.md` lists what each tool answers. `grep` is still right for what is genuinely characters: a string literal, a comment, a non-Python file.

---

## Configuration

`.env` holds defaults; `.env.local` holds secrets, is gitignored, and overrides them. Configuration is loaded through pydantic-settings in `src/forumboard/agent/config.py`, which is the only module that reads the environment — `NOTION_TOKEN`, the two database ids, the loop intervals, the briefing cadences, and the transcript size past which the editor chunks. `forumboard setup notion` writes the Notion half; `forumboard doctor` says whether it took.

---

## Process & Communication

**Wait on pushed tool output rather than polling for it.** When a long-lived command returns a resumable tool call and the runtime offers an event-driven waiter, keep that call live and yield to the waiter. Repeated reads of a shell session are polling even when each read has a long timeout; they spend turns asking whether anything changed instead of resuming only when something did.

**Always surface a question as a question**, through whatever structured question facility the harness gives you, rather than as narration the user has to notice. This applies to clarifying requirements, offering choices, confirming destructive actions, proposing changes, and any situation needing user input. Even for open-ended questions, attach concrete options plus a free-form one — structured answers are what downstream notification parsing reads. When in doubt, ask.

**Explain from scratch, and walk through the options.** A finding handed over as a verdict cannot be judged, only accepted or refused on trust. Explain the underlying problem as though the reader has none of your context, then the options, then your own recommendation marked as yours. Propose rather than assume: show the relevant current state, give the rationale, offer alternatives. Prefer being slow and complete over being brief — the reader is deciding, and a decision made without the reasoning is one they have to re-derive later.

This is what makes a claim checkable rather than plausible. Verify each one against **what was actually asked** — the note's own words, the issue's own report — not against a title, a commit subject, or your own earlier summary. A claim that survives that check is worth stating plainly; one that does not is worth correcting out loud, including when the claim was yours.

**After every command invocation**, compare how it was actually used against how it is documented, and propose an update as a question the user answers. A user who supplies external docs, corrects your approach, asks for something the command should have covered, or ignores a section is telling you the command should evolve.

### Reporting Friction

When the tooling fights you, **open a GitHub issue against this repository** rather than only working around it and moving on. A workaround that lives in one session's narration teaches nobody; the issue is what survives the session.

File one whenever a command half-completes and leaves inconsistent state, a classifier reports a failed probe as though it were a fact, a sandbox or permission boundary blocks an operation the documented workflow prescribes, or a recovery needed steps the workflow never named.

Record what you observed rather than what you concluded: the exact command, the exact error, the state it left behind, and what the recovery cost. Name the component that owns the fix. A friction report is evidence, which is worth more than a guess at the cause — and evidence is what the self-improvement loop below consumes.

File that evidence with `uv run lup-devtools dev report-friction`; its required fields preserve the report shape and its repository target comes from this checkout. Pass `--issue NUMBER` to correct a report in place instead of duplicating it. Read `--help` for the exact options.

### External Resources

When a question is about the harness you are running under, its agent SDK, or its model API, read that runtime's own documentation rather than answering from memory: delegate to the documentation subagent your harness ships where it has one, or fetch the vendor's documentation directly — """
        ),
        models.RuntimeDocs(),
        models.TextPart(
            text=r""". The fetch scopes the permission policy admits are declared in `harness/catalog.py`. When the user provides documentation links, incorporate that knowledge into the guidance source or the relevant skill declaration.

---

## Self-Improvement Loop

`docs/self-improvement.md` carries the full loop: how to diagnose a failure through the pipeline, the three levels of analysis, what to track per session, and the anti-patterns to avoid. Read it when running the feedback-loop, review, or meta skills — each of them works from it.

"""
        ),
        *conventions.FAILURE_ANALYSIS,
        models.TextPart(
            text=r"""The durable fix is a capability, not a rule: trace the failure to the missing input or the workflow step where the wrong decision entered, and change that. A prompt rule coexists peacefully with the failure it warns about.
"""
        ),
    ]


def document(selection: RuleSelection | None = None) -> models.PromptDocument:
    """The guidance as one document, built against the project's selection.

    Taking the selection rather than reading one keeps the catalog free to
    import this module: the catalog owns the declaration and hands it down,
    so nothing here reaches back up for it.
    """
    return models.PromptDocument(
        source=__name__, parts=guidance_parts(selection or RuleSelection())
    )
