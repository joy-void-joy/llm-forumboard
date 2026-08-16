"""Architectural decisions behind the development tooling."""

import lup.harness.models as models

DOCUMENT = models.PromptDocument(
    source=__name__,
    parts=[
        models.TextPart(
            text=r"""# Development-tooling decisions

These records capture the architectural constraints behind Lup's development
tooling. Each decision is stated against the current system; repository
history belongs in commits, not in generated code comments.

## ADR-001: Commit deterministic native artifacts

Context: Native CLIs discover Markdown, manifests, settings, agents, scripts,
and hooks from fixed filesystem locations. Hook execution must not depend on
the source checkout or virtual environment.

Decision: Commit deterministic `.claude`, `.codex`, `.agents`, and root
guidance artifacts with an ownership manifest. Generate locally before review
and verify drift read-only in CI.

Consequences: A source change often produces a larger mechanical diff, but a
checkout is immediately launchable and policy hooks remain hermetic. Reviewers
compare canonical declarations with their rendered effects.

## ADR-002: Author prose as typed Python content

Context: Encoded native-file catalogs made prompt bodies unreadable and left
no safe path from a native edit back to source.

Decision: Store each skill and agent in one module as `Skill`, `Agent`, and
`PromptDocument` values. Store guidance and templates as typed documents or
plain assets. Aggregate them through explicit imports. Both native trees render
from those declarations; byte parity with a retired catalog is not a contract.

Consequences: Prompt changes are ordinary string diffs. Semantic parts such as
`ArgumentsRef` and `SkillInvocation` preserve intent across native spellings.
Adding a prompt-part variant requires renderer support from every target or a
clear validation failure naming the semantic object. Portable content is
scanned for native wire spellings; a document that teaches a native boundary
declares a typed, audited exception at its source.

## ADR-003: Keep reconciliation patch-only and explicit

Context: Arbitrary rendered Markdown or TOML cannot be reliably converted into
a Python AST edit. Silent reverse engineering would turn formatting accidents
into canonical source changes.

Decision: Reconciliation classifies native differences and preserves conflicts.
A source-aware producer may persist a Git-format canonical-source patch; a
separate command verifies its identity, digest, preimages, and applicability,
shows it, asks for confirmation, then regenerates.

Consequences: Native body edits are not automatically imported. The safe path
is to edit the content module. External source-aware tooling has a narrow,
auditable patch transport without gaining direct write authority.

## ADR-004: Execute one dependency-free policy kernel

Context: Canonical Pydantic policies and generated hooks once carried parallel
control flow. Shared fixtures detected divergence only after two
implementations had already changed.

Decision: Put shell, fetch, edit, token context, marker, and anti-pattern
control flow in `lup.policy.kernel`. Canonical adapters erase validated models
to primitive rows and wrap its decision. Both plugins receive a verbatim
kernel copy plus generated policy data.

Consequences: A policy behavior change has one implementation site. The kernel
uses a deliberately small plain-data boundary; Pydantic validation stays above
it. Generated data may differ by application configuration, while kernel bytes
must remain identical.

## ADR-005: Pin the hook hermeticity floor

Context: A hook starts through the native CLI with `python3`, outside Lup's
import graph. Importing project packages or assuming the active virtual
environment would make permissions disappear precisely when packaging differs.

Decision: Restrict kernel imports to a statically audited standard-library
allowlist, run assembled policy fixtures under `python3 -I -S`, and run native
CI on Python 3.14, the declared package and hook syntax floor.

Consequences: The kernel cannot use Pydantic or convenient project helpers.
The assembler owns data projection. A traceback retains canonical kernel line
numbers because generation copies the file rather than reconstructing source.

## ADR-006: Use semantic hooks with native decoders and renderers

Context: Claude and Codex send different tool payloads and support different
permission effects. Shared policy must not branch on provider or tool names.

Decision: Each adapter decodes native events into edit, shell, fetch, search,
or unknown semantic values. Shared policy returns allow, ask, or deny. Each
adapter renders that decision into the native boundary; an unrepresentable ask
fails closed.

Consequences: Provider vocabulary stays at the adapter seam. Fixture tables
exercise both decoders and the same policy. An unsupported native effect is
visible evidence, never a permissive fallback.

## ADR-007: Treat Codex blocked edits as fail-closed tool rejection

Context: The Codex command-hook boundary has no portable approval-prompt effect
for an `ask` decision. The user-visible behavior of `apply_patch` must still be
pinned against the real CLI.

Decision: Render non-allow Codex edit decisions as exit code 2. The scheduled
live smoke installs the generated plugin in an isolated home, attempts an
anti-pattern patch, and requires the file to remain unchanged while the native
session reports the rejection.

Consequences: Codex can continue the conversation after a blocked tool call,
but it cannot turn an ask into an edit. Version drift triggers a repeat of this
observation. A future native approval effect would require a new evidence row
and renderer design, not a shared-policy branch.

## ADR-008: Keep one resolver core

Context: Resolution scheduling, questions, verification, joins, leases, and
human acceptance are safety state, not provider presentation.

Decision: One persisted resolver core owns the lifecycle. Thin native entries
supply configured session factories, an invocation renderer, a question
broker, and process launcher.

Consequences: Claude and Codex cannot drift in merge authority or recovery
semantics. Adapter capability gaps remain at factory construction, and the
resolver never merges directly into the user's branch.

## ADR-009: Separate Codex source, cache, trust, and credentials

Context: A project plugin source directory is not the installed plugin cache,
and hook trust is user security state.

Decision: Install through the native plugin CLI only when the separately
cached digest is absent or stale. Generate source and ownership metadata only.
Never generate trust, credentials, profiles, or active state.

Consequences: Launch has an explicit installation step and verifies exact
bytes. CI can use an isolated Codex home; local trust decisions remain personal
and survive generation.

## ADR-010: Use submitted output as the typed result mechanism

Context: Provider-native output schemas and an MCP submission tool can diverge
or race when both are active.

Decision: Bind one fresh `submit_output` tool and store per typed turn. Validate
the Pydantic value and optional submission gate before persistence. Do not
enable a second native structured-output mechanism on the same turn.

Consequences: A typed result has one validation history and one ownership
boundary. Missing or incompatible submission raises a typed error rather than
appearing as an empty success.

## ADR-011: Keep runtime capabilities independent

Context: A broad client/options object couples provider construction, optional
turn behavior, wrappers, routing, profiles, and background scheduling.

Decision: Use narrow one-to-three-method contracts, immutable capability
handles, explicit factory recipes, config transforms, and concrete decorators.
Unsupported behavior is absent from the handle.

Consequences: Applications compose only the capabilities they need. A third
adapter implements contracts without joining a shared provider registry, and
neutral orchestration never probes a backend name.

## ADR-012: Split diagnostics between Ruff and the Lup checker

Context: Ruff does not support third-party linter plugins
([Ruff FAQ](https://docs.astral.sh/ruff/faq/#can-i-write-my-own-linter-plugins-for-ruff)),
and Lup's conventions — capability-ABC shape, native-spelling boundaries,
anti-pattern edits — need project-aware analysis with project-owned rule
identifiers.

Decision: Ruff owns standard Python diagnostics. The typed Lup checker owns
repository-specific rules under stable kebab-case identifiers with audited
`# lup: ignore[rule-id]` suppressions, and `docs/rules.md` is generated from
the executable rule objects. `# noqa` stays forbidden as a Lup rule of its
own.

Consequences: The two rule sets do not duplicate diagnostics. Both generated
hook runtimes and the repository auditor share one semantic checker, and the
suppression audit reports bare, stale, and spurious ignores.

## ADR-013: Put the drift check on the path a commit must cross

Context: The generated-artifact drift check was correct and still let two
stale commits land, because nothing made it run before history was written.
The `.pre-commit-config.yaml` meant to be the commit-time layer declared a
hook for a framework the project never depended on, so no clone had it
installed. The sources compiled into both plugin trees are copied there
verbatim, so rewording a comment in one of them drifted both without
changing anything either does — and a path pattern deciding when to check is
one more belief that can be wrong about which commits matter.

Decision: A git `pre-commit` hook, written by `dev commit-guard install` and
armed by `dev worktree create`, whose body is `harness check all`. The
pipeline runs that same command as its own step, spelled from the same
constant, and `dev check` reads the same `DriftVerdict` that command reads.
The `pre-commit` framework config is dropped: it named a framework nothing
depended on, it wanted the same hook path, and it regenerated where a gate
should read.

Consequences: Skipping `dev check` no longer skips the drift check. The three
refusing paths cannot disagree, because there is one computation and one
failure message naming the command that settles it. The check is whole-tree
and unscoped, which the sub-second cost affords. Two cases stay the
pipeline's, which is why the local layer does not replace it: the hook reads
the worktree rather than the index, so a commit staging canonical source
while leaving its regenerated artifacts unstaged passes it, and `--no-verify`
skips it outright. Both reach CI, where the same command refuses them.

## ADR-014: Judge devtools placement by what an adopter keeps receiving

Context: `lup` is a published dependency and `src/<project>` is a copied,
renamed template, so the two halves reach a downstream project by different
routes: a library change arrives through `uv lock --upgrade-package lup`,
while a template change arrives only through a hand-reviewed replay of
upstream commits onto a diverged copy. The devtools CLI grew entirely on the
template side without that difference ever being decided — thirteen prior
records govern generation, policy, and the resolver, and none of them
mentions where the tooling lives.

Decision: Placement is judged by the placement test the conventions already
state — would another project built on this template want this module — and
the `application placement` row in `dev check` reports every devtools module
whose imports never reach the application package. Declared prose and the
harness declaration are exempt, because a module holding this project's own
judgement as data is exactly where it belongs when it imports nothing.

Consequences: The row is advisory and names debt rather than failing, since
moving a module is a change with its own review. It is the mirror of
`library placement`, which asks whether a library module baked in a choice
an adopter cannot replace; together they bound the boundary from both sides
instead of only the one a library author notices.

[README.md](README.md) indexes every guide these decisions govern.
"""
        )
    ],
)
