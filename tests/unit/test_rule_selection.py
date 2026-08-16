"""A project's rule selection, and the surfaces it has to reach.

The failure this guards against is a seam that exists in one place and is
quietly not honoured in another: the promise that a project may retire a rule
is only worth anything if the sweep, the compiled plugin, and the generated
reference all stop naming it together. Each is pinned separately here, because
each was reached by a different call path and any one of them could be added
back without the others noticing.
"""

from lup.adapters.claude.harness import ClaudeSpellings
from lup.adapters.harness import claude_prompt_renderer
from lup.codescan.antipatterns import (
    DOCUMENT_IN_HAND,
    AntiPatternSet,
    antipattern_set_for,
)
from lup.codescan.common import RuleSelection
from lup.codescan.registry import all_rules
from lup.devtools.dev.rules import rule_reference_document

RETIRED = RuleSelection(retired=["model-config", "default-factory"])


def test_a_retired_rule_leaves_the_table_the_sweep_reads() -> None:
    kept = AntiPatternSet().selected(RETIRED)

    assert [rule.id for rule in kept.python if rule.id in set(RETIRED.retired)] == []
    assert any(rule.id == "import-re" for rule in kept.python)


def test_selecting_nothing_keeps_every_rule_the_library_ships() -> None:
    """The default has to be the whole table, or adopting the seam changes it."""
    whole = AntiPatternSet()

    assert whole.selected(RuleSelection()).python == whole.python


def test_a_retired_rule_leaves_the_compiled_plugin() -> None:
    """The hook is compiled separately from the sweep and drifted before."""
    compiled = antipattern_set_for(
        ClaudeSpellings().read_document(DOCUMENT_IN_HAND), RETIRED
    )

    assert [
        rule.id for rule in compiled.python if rule.id in set(RETIRED.retired)
    ] == []


def test_a_retired_rule_leaves_the_generated_reference() -> None:
    """A page documenting a rule nothing enforces is worse than no page."""
    cards = {rule.id for rule in all_rules(selection=RETIRED)}

    assert cards.isdisjoint(RETIRED.retired)
    assert "constant-declaration" in cards


def test_the_reference_document_renders_from_the_selection() -> None:
    rendered = claude_prompt_renderer().render(rule_reference_document(RETIRED))

    assert "`model-config`" not in rendered
    assert "`constant-declaration`" in rendered


def test_a_structural_rule_can_be_retired_too() -> None:
    """Structural rules reach the sweep by a path the anti-pattern table misses."""
    cards = {
        rule.id
        for rule in all_rules(selection=RuleSelection(retired=["model-free-function"]))
    }

    assert "model-free-function" not in cards
    assert "abc-capability" in cards
