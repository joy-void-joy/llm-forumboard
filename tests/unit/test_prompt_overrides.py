"""How a deployment changes what a pass is told, and what it may not change.

Whether a conversation is published at all is the one decision that cannot be
made without knowing who receives the page. A draft still being circulated is
disclosed by a public board and unremarkable on an internal one, and a pass
left to infer which infers a different readership on different runs — which is
how the same transcript is skipped as pre-circulation working product one run
and published the next. So the readership is stated, and stated where a
deployment can say its own without committing it.

The mechanism is general because the audience is not the only such fact, and a
setting per fact would be a new field every time somebody wanted a different
paragraph. What ships is a composition of named pieces; what a deployment
writes is a list of replacements over it.
"""

from pathlib import Path

import pytest

from forumboard.agent.config import settings
from forumboard.agent.models import ReviewPlan
from forumboard.agent.prompts import editor, reviewer, shared, worldview
from forumboard.agent.prompts.catalog import (
    briefing_prompt,
    composed,
    editor_prompt,
    overrides,
    reviewer_prompt,
    worldview_prompt,
)
from forumboard.agent.prompts.models import Prompt, Prompts

PLAN = ReviewPlan(title="A discussion", through_line="what it is about")


@pytest.fixture(autouse=True)
def no_local_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A deployment that has written no overrides, unless a test writes some."""
    monkeypatch.setattr(settings, "prompts_local_path", str(tmp_path / "absent.py"))
    overrides.cache_clear()


def local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str) -> None:
    """Write a deployment's own overrides file and point the catalog at it."""
    path = tmp_path / "prompts.py"
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(settings, "prompts_local_path", str(path))
    overrides.cache_clear()


def test_a_composition_replaces_a_piece_by_name() -> None:
    """Replacement is by name, and produces a new composition."""
    declared = Prompts(
        pieces=[Prompt(name="one", text="first"), Prompt(name="two", text="second")]
    )
    replaced = declared.override([Prompt(name="two", text="rewritten")])
    assert replaced.render() == "first\n\nrewritten"
    assert declared.render() == "first\n\nsecond"


def test_replacement_keeps_the_declaration_s_order() -> None:
    """Rewording the audience must not move it above what introduces it."""
    declared = Prompts(
        pieces=[Prompt(name="one", text="first"), Prompt(name="two", text="second")]
    )
    replaced = declared.override(
        [Prompt(name="two", text="B"), Prompt(name="one", text="A")]
    )
    assert replaced.render() == "A\n\nB"


def test_a_composition_ignores_a_name_it_does_not_hold() -> None:
    """One list of replacements is applied to every composition.

    A piece the reviewer has and the worldview does not must not make applying
    the same list to the worldview an error — that is the loader's job, which
    can see every composition at once.
    """
    only_worldview = composed(worldview.PROMPT)
    assert only_worldview.render() == worldview.PROMPT.render()


def test_a_shared_piece_reaches_every_pass_that_holds_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The audience is declared once, so a deployment states it once."""
    local(
        monkeypatch,
        tmp_path,
        "from forumboard.agent.prompts.models import Prompt\n"
        'OVERRIDES = [Prompt(name="audience", text="Read by Example Org.")]\n',
    )
    assert "Read by Example Org." in reviewer_prompt()
    assert "Read by Example Org." in editor_prompt(PLAN)
    assert "Read by Example Org." not in worldview_prompt()


def test_a_replacement_naming_nothing_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A typo in the file that softens a safety instruction must not pass.

    Silently ignoring it would report success while changing nothing, and
    nothing downstream would ever look wrong.
    """
    local(
        monkeypatch,
        tmp_path,
        "from forumboard.agent.prompts.models import Prompt\n"
        'OVERRIDES = [Prompt(name="audiance", text="typo")]\n',
    )
    with pytest.raises(ValueError, match="nothing declares"):
        reviewer_prompt()


def test_the_error_says_what_could_have_been_named(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A refusal an operator cannot act on is a refusal they will work around."""
    local(
        monkeypatch,
        tmp_path,
        "from forumboard.agent.prompts.models import Prompt\n"
        'OVERRIDES = [Prompt(name="audiance", text="typo")]\n',
    )
    with pytest.raises(ValueError, match="audience"):
        reviewer_prompt()


def test_a_deployment_may_replace_a_whole_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every piece is replaceable, so replacing all of them replaces the prompt."""
    pieces = ", ".join(
        f'Prompt(name="{piece.name}", text="X")' for piece in reviewer.PROMPT.pieces
    )
    local(
        monkeypatch,
        tmp_path,
        f"from forumboard.agent.prompts.models import Prompt\nOVERRIDES = [{pieces}]\n",
    )
    assert reviewer_prompt() == "\n\n".join(["X"] * len(reviewer.PROMPT.pieces))


def test_the_plan_is_applied_after_a_deployment_s_replacements(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A local file must not displace the brief this call is working from."""
    local(
        monkeypatch,
        tmp_path,
        "from forumboard.agent.prompts.models import Prompt\n"
        'OVERRIDES = [Prompt(name="editor-plan", text="not this")]\n',
    )
    rendered = editor_prompt(PLAN)
    assert "what it is about" in rendered
    assert "not this" not in rendered


def test_the_briefing_window_is_applied_the_same_way() -> None:
    """The per-call piece and the deployment's own use one mechanism."""
    assert "Write the weekly briefing covering last week." in briefing_prompt(
        "weekly", "last week"
    )


def test_the_shipped_audience_names_no_organisation() -> None:
    """This file is published; the roster is gitignored for one reason.

    A default naming the organisation running this deployment would put into
    committed source exactly what profiles.py keeps out of it.
    """
    text = shared.AUDIENCE.text
    assert "organisation" in text
    assert "not public" in text
    assert "CeSIA" not in text


def test_both_transcript_passes_are_told_the_audience() -> None:
    """The editor may still refuse, so it needs the boundary that refusal uses."""
    assert reviewer.PROMPT.holds("audience")
    assert editor.PROMPT.holds("audience")
