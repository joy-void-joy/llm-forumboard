"""Behavior tests for the rule that keeps platforms out of portable prose.

The vocabulary is derived from the runtimes rather than written down, so these
tests check the derivation itself as much as the declarations it judges.
"""

import pytest

from lup.adapters.claude.harness import ClaudeSpellings, claude_granted_tools
from lup.adapters.claude.hooks import CLAUDE_SEMANTICS
from lup.adapters.codex.harness import CodexSpellings
from lup.adapters.codex.hooks import CODEX_SEMANTICS
from lup.adapters.harness import compile_codex
from lup.codescan.portable import native_vocabulary, prose_breaches
from lup.harness.contracts import NativeSpellings
from lup.harness.models import PromptDocument, TextPart
from lup.harness.prompts import SPAWNED_SESSION_LOSES_SHELL
from lup_template.devtools.harness.catalog import portable_harness

RUNTIMES: list[NativeSpellings] = [ClaudeSpellings(), CodexSpellings()]

MARK = "<supplied by the caller>"

REMAINING_PROSE_BREACHES: list[str] = []
"""Declarations whose prose still names a platform.

Empty, and the compilers now refuse a breach outright, so this is a second
reading of the same invariant rather than a worklist: it names which
declaration regressed, where the compile error names only the spelling."""


def instruction_text(runtime: NativeSpellings) -> str:
    """Everything this runtime says when it frames a caller's own words."""
    return " ".join(
        [
            runtime.ask_user(MARK),
            runtime.delegate("plugin:agent", MARK),
            runtime.request_approval(MARK, MARK),
            runtime.relocate_session(MARK),
            runtime.resolver_entry(),
            runtime.runtime_docs(),
            runtime.escape_sandbox(MARK).in_prose(),
            runtime.read_document(MARK).in_prose(),
        ]
    )


def test_declared_identifiers_occur_in_what_the_runtime_spells() -> None:
    """A declared identifier that no instruction contains describes nothing."""
    for runtime in RUNTIMES:
        spelled = instruction_text(runtime)
        for identifier in runtime.native_identifiers:
            assert identifier in spelled, (
                f"{runtime.runtime_name} declares {identifier!r} but never spells it"
            )


def test_a_runtime_that_cannot_spell_an_idea_says_why_rather_than_nothing() -> None:
    """Declining is an answer here, and an answer without a reason is silence.

    Both spellings a runtime may decline are asked of both runtimes, so the
    seam being closed is checked as behavior and not only as an abstract
    method somebody remembered to implement.
    """
    for runtime in RUNTIMES:
        for spelling in [runtime.escape_sandbox(MARK), runtime.read_document(MARK)]:
            audited = spelling.audited()
            assert audited, f"{runtime.runtime_name} answers with nothing at all"
            assert audited != "unsupported — ", (
                f"{runtime.runtime_name} declines without saying why"
            )


def test_the_resolver_entry_asks_its_own_vocabulary_about_the_sandbox() -> None:
    """Neither entry may hardcode an escape, and neither may invent one.

    Both runtimes spell one, in words of their own that nothing here knows,
    so what the two entries share is the asking. The load-bearing assertion is
    the second: an entry may name the sandbox exactly when its own vocabulary
    spelled something, which is what an entry that grew a flag would break —
    and what an entry still naming the sandbox would break on a runtime that
    later declines.
    """
    for runtime in RUNTIMES:
        escape = runtime.escape_sandbox(SPAWNED_SESSION_LOSES_SHELL).in_prose()
        entry = runtime.resolver_entry()

        if escape:
            assert escape in entry, (
                f"{runtime.runtime_name} spells an escape its own entry drops"
            )
        assert ("sandbox" in entry.lower()) == bool(escape), (
            f"{runtime.runtime_name} names the sandbox outside escape_sandbox"
        )


def test_the_prose_seam_and_the_decision_seam_agree_about_the_agent_escape() -> None:
    """One fact reaches two seams, so a runtime cannot answer it both ways.

    ``agent_escalates`` decides whether an ``escalable`` verdict offers the
    way out and ``escape_sandbox`` supplies the words for taking it. Split,
    they drift: a runtime that spells an escape its verdicts will not offer
    keeps words nobody is told they may use, and one that offers an escape it
    will not spell sends an agent looking for words that are not there.

    ``escapable`` is deliberately not what this compares against. That asks
    whether a verdict can place a call itself, which Codex answers no while
    answering yes here — the divergence that makes the two fields two.
    """
    seams = [(ClaudeSpellings(), CLAUDE_SEMANTICS), (CodexSpellings(), CODEX_SEMANTICS)]

    for runtime, semantics in seams:
        spelled = bool(runtime.escape_sandbox(MARK).in_prose())
        assert spelled == semantics.agent_escalates, (
            f"{runtime.runtime_name} spells an escape its decisions disagree with"
        )


def test_vocabulary_follows_the_locations_a_runtime_can_spell() -> None:
    """Derivation, not a table: every location reachable is forbidden in prose."""
    vocabulary = native_vocabulary(ClaudeSpellings(), ["lup"])

    assert ".claude/CLAUDE.md" in vocabulary
    assert ".claude/plugins/lup/commands/" in vocabulary
    assert ".claude/.lup-ownership.json" in vocabulary
    assert "Claude Code" in vocabulary
    assert "AskUserQuestion" in vocabulary


def test_compiling_refuses_prose_that_names_a_platform() -> None:
    """The gate has to bite at the seam, not only in this file's inventory."""
    harness = portable_harness()
    leaked = harness.model_copy(
        update={
            "guidance": PromptDocument(
                parts=[TextPart(text="Edit .claude/settings.json by hand")]
            )
        }
    )

    with pytest.raises(ValueError, match="must name no platform"):
        compile_codex(leaked)


def test_portable_prose_names_no_platform_beyond_the_known_inventory() -> None:
    breaches = prose_breaches(portable_harness(), RUNTIMES)
    remaining = sorted(dict.fromkeys(breach.declaration_id for breach in breaches))

    assert remaining == sorted(REMAINING_PROSE_BREACHES), (
        "portable prose inventory changed; converted declarations must leave "
        f"the list. Current: {remaining}"
    )


def test_a_grant_claude_cannot_honor_never_reaches_the_rendered_tree() -> None:
    """The portable vocabulary is a superset, and the tree gets what exists.

    Claude Code ships no `Glob` and no `Grep`. Granting them is inert, which
    is worse than harmless: it reads as search the agent has, and the agent
    spends a turn per attempt learning otherwise. Declarations keep naming
    the portable set, and the filter is where the runtime is known.
    """
    assert claude_granted_tools(["Read", "Grep", "Glob", "Bash"]) == ["Read", "Bash"]
    assert claude_granted_tools(["Bash(git:*)", "Read"]) == ["Bash(git:*)", "Read"]

    declared = [
        (declaration.id, declaration.tools)
        for plugin in portable_harness().plugins
        for declaration in [*plugin.skills, *plugin.agents]
    ]
    assert declared, "the harness declares no skills or agents to check"
    for identifier, tools in declared:
        assert claude_granted_tools(tools) == [
            tool for tool in tools if tool not in ("Glob", "Grep")
        ], identifier
