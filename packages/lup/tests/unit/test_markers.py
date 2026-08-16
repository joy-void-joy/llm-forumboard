"""Behavior tests for the marker scanner (`lup.codescan.markers`).

Pins the load-bearing rule that distinguishes a real note from code: in
Python a `# lup:` counts only inside a comment or docstring, never inside
an ordinary string literal. The scanner's own "no notes" echo strings are the
canonical false positive that line-scanning used to report. The same scan,
parameterized over the marker regex, backs the `TEMPLATE:` customization
todos that `dev todos` gathers for `/lup:init`.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from lup.codescan.common import file_level_ignore, ignore_rule_ids
from lup.policy.kernel.edit import IGNORE_RE
from lup.codescan.markers import (
    MarkerComment,
    NoteTarget,
    ScanMode,
    find_feedback,
    restore_claims,
    retire_claims,
    scan_mode_for,
)


def texts(source: str, mode: str) -> list[str]:
    return [c.text for c in find_feedback(source, mode)]


def todo_texts(source: str, mode: str) -> list[str]:
    return [c.text for c in find_feedback(source, mode) if c.kind == "template"]


def test_python_ignores_marker_inside_ordinary_string() -> None:
    source = 'def f() -> None:\n    print("No # lup: comments to commit.")\n'
    assert find_feedback(source, ScanMode.PYTHON) == []


def test_python_reports_real_comment() -> None:
    source = 'x = 1  # lup: real note\nprint("# lup: not a note")\n'
    assert texts(source, ScanMode.PYTHON) == ["real note"]


def test_python_reports_marker_inside_docstring() -> None:
    source = (
        '"""Module summary.\n\nKey idea — explained #lup: clarify this please\n"""\n'
    )
    assert texts(source, ScanMode.PYTHON) == ["clarify this please"]


def test_python_skips_backtick_quoted_syntax_reference() -> None:
    source = '"""Docs that mention the `# lup:` marker syntax inline."""\n'
    assert find_feedback(source, ScanMode.PYTHON) == []


def test_python_skips_double_backtick_quoted_syntax_reference() -> None:
    # reStructuredText quotes inline code with double backticks, whose even
    # run length defeats single-backtick parity alone.
    source = '"""Docs that mention the ``# lup:`` marker syntax rst-style."""\n'
    assert find_feedback(source, ScanMode.PYTHON) == []


def test_syntax_error_does_not_swallow_a_real_note() -> None:
    source = "def broken(:\n# lup: still surfaced despite syntax error\n"
    assert texts(source, ScanMode.PYTHON) == ["still surfaced despite syntax error"]


def test_text_mode_line_scans_what_python_would_treat_as_a_string() -> None:
    # Text has no lexer, so a line-level marker is taken verbatim — even one
    # that Python mode would dismiss as living inside a string literal.
    source = 'print("# lup: surfaced in text mode")\n'
    assert texts(source, ScanMode.TEXT) == ['surfaced in text mode")']
    assert find_feedback(source, ScanMode.PYTHON) == []


def test_markdown_skips_fenced_code() -> None:
    source = "intro\n\n```\n# lup: inside a fence, not a note\n```\n"
    assert find_feedback(source, ScanMode.MARKDOWN) == []


def test_note_mentioning_the_ignore_hatch_in_prose_is_still_a_note() -> None:
    # The ignore check is anchored to the marker that opens the line, not a
    # substring search — so a note whose prose talks about `# lup: ignore`
    # is feedback, not an ignore directive, and must surface.
    source = "# lup: real note\n# lup: we should remove every # lup: ignore\n"
    assert texts(source, ScanMode.MARKDOWN) == [
        "real note",
        "we should remove every # lup: ignore",
    ]


def test_inline_ignore_directive_is_still_skipped() -> None:
    source = "x = 1  # lup: ignore\ny = 2  # lup: real note\n"
    assert texts(source, ScanMode.PYTHON) == ["real note"]


def test_typed_ignore_directive_is_not_feedback() -> None:
    # A typed `# lup: ignore[id]` is an escape hatch, not a review note, so it
    # is skipped by the feedback scan just like the bare form.
    source = "x = 1  # lup: ignore[dict-get]\ny = 2  # lup: real note\n"
    assert texts(source, ScanMode.PYTHON) == ["real note"]


def test_ignore_rule_ids_parses_bare_and_typed() -> None:
    bare = IGNORE_RE.search("# lup: ignore")
    assert bare is not None and ignore_rule_ids(bare) is None
    typed = IGNORE_RE.search("x = 1  # lup: ignore[dict-get, tuple-shape]")
    assert typed is not None
    assert ignore_rule_ids(typed) == {"dict-get", "tuple-shape"}


def test_file_level_ignore_reads_bare_typed_and_absent() -> None:
    bare = file_level_ignore("# lup: ignore\nx = 1\n")
    assert bare is not None and bare.rule_ids is None
    typed = file_level_ignore("# lup: ignore[dict-get]\nx = 1\n")
    assert typed is not None and typed.rule_ids == {"dict-get"}
    assert file_level_ignore("x = 1\n") is None


def test_file_level_ignore_carries_a_reason_after_the_ids() -> None:
    for line in (
        "# lup: ignore[dict-get] — the receiver is an HTTP client\n",
        "# lup: ignore[dict-get]: the receiver is an HTTP client\n",
        "# lup: ignore[dict-get] - the receiver is an HTTP client\n",
    ):
        stated = file_level_ignore(line + "x = 1\n")
        assert stated is not None and stated.rule_ids == {"dict-get"}
    bare = file_level_ignore("# lup: ignore — every rule, and here is why\nx = 1\n")
    assert bare is not None and bare.rule_ids is None


def test_file_level_ignore_still_refuses_a_trailing_inline_directive() -> None:
    assert file_level_ignore("x = 1  # lup: ignore[dict-get]\n") is None


def test_scan_mode_for_routes_by_suffix() -> None:
    assert scan_mode_for(Path("a.py")) == ScanMode.PYTHON
    assert scan_mode_for(Path("a.pyi")) == ScanMode.PYTHON
    assert scan_mode_for(Path("README.md")) == ScanMode.MARKDOWN
    assert scan_mode_for(Path("trace.json")) == ScanMode.JSON
    assert scan_mode_for(Path("trace.jsonl")) == ScanMode.JSON
    assert scan_mode_for(Path("notes.txt")) == ScanMode.TEXT


def test_template_comment_marker_is_a_todo() -> None:
    source = "# lup: template: replace these fields for your domain\nx = 1\n"
    assert todo_texts(source, ScanMode.PYTHON) == [
        "replace these fields for your domain"
    ]


def test_template_docstring_marker_carries_the_lup_prefix() -> None:
    # A docstring customization point spells the marker in full, the same way
    # every other note in a docstring does — there is no bare-keyword form.
    source = '"""Setup flow.\n\n# lup: template: Replace with your API scopes.\n"""\n'
    assert todo_texts(source, ScanMode.PYTHON) == ["Replace with your API scopes."]


def test_template_marker_inside_ordinary_string_is_code() -> None:
    source = 'MESSAGE = "# lup: template: not a decision point"\n'
    assert todo_texts(source, ScanMode.PYTHON) == []


def test_bare_template_keyword_is_no_longer_a_todo() -> None:
    # The standalone `TEMPLATE:` convention is gone: a customization point is
    # a `# lup:` note whose head says so, so the old spelling scans as nothing.
    source = "# TEMPLATE: replace these fields for your domain\n"
    assert todo_texts(source, ScanMode.PYTHON) == []
    assert find_feedback(source, ScanMode.PYTHON) == []


def test_template_mention_mid_note_is_ordinary_feedback() -> None:
    # The head has to open the note's text. A note that merely goes on to talk
    # about templates is feedback somebody is owed, not a customization point.
    source = "# lup: gathered via the template: convention\n"
    assert todo_texts(source, ScanMode.PYTHON) == []
    (note,) = find_feedback(source, ScanMode.PYTHON)
    assert note.kind == "note"


def test_file_level_ignore_is_not_itself_a_note() -> None:
    # A file-level `# lup: ignore` opts the file out of anti-pattern checks;
    # it is an ignore directive, so neither the feedback listing nor the
    # customization todos report the line itself.
    source = "# lup: ignore\n# lup: template: still a decision point\n"
    assert todo_texts(source, ScanMode.PYTHON) == ["still a decision point"]


def test_feedback_note_surfaces_despite_file_level_ignore() -> None:
    # The file-level opt-out silences anti-pattern checks, never feedback:
    # a real note in an opted-out file (e.g. lup.codescan.markers itself) must reach
    # `dev comments`, or review feedback silently disappears.
    source = "# lup: ignore\nx = 1  # lup: real note\n"
    assert texts(source, ScanMode.PYTHON) == ["real note"]


def test_template_continuation_merges_and_stops_at_decoration() -> None:
    source = (
        "# =========================================\n"
        "# lup: template: pick your integrations —\n"
        "# one entry per service\n"
        "# =========================================\n"
        "X = 1\n"
    )
    assert todo_texts(source, ScanMode.PYTHON) == [
        "pick your integrations — one entry per service"
    ]


def test_decoration_line_ends_a_feedback_note_too() -> None:
    source = "# lup: note inside a banner\n# ----\nx = 1\n"
    assert texts(source, ScanMode.PYTHON) == ["note inside a banner"]


def test_note_quoting_the_marker_spelling_mid_body_stays_one_note() -> None:
    # A spelling written into a continuation line's prose is a mention, not a
    # marker: the run absorbs it and the note keeps its full text and its
    # true end line, with no phantom note left behind.
    source = (
        "# lup: the escape hatch and a note look alike, because the hatch is\n"
        "# written # lup: ignore[rule-id] on the offending line, and a note\n"
        "# that says so has to stay one note.\n"
        "x = 1\n"
    )
    (note,) = find_feedback(source, ScanMode.PYTHON)
    assert note.text == (
        "the escape hatch and a note look alike, because the hatch is written "
        "# lup: ignore[rule-id] on the offending line, and a note that says so "
        "has to stay one note."
    )
    assert (note.start_line, note.end_line) == (1, 3)


def test_line_scanned_note_quoting_the_spelling_emits_no_phantom() -> None:
    # Text has no lexer to say where a comment opens, so the position of the
    # match on the line is the whole reading — a quoted spelling mid-prose
    # must not split the note into a truncated one and an invented one.
    source = (
        "# lup: a plain-text note, where no lexer vouches for\n"
        "# anything, so a body that writes # lup: out in the open\n"
        "# still must not split into two.\n"
    )
    (note,) = find_feedback(source, ScanMode.TEXT)
    assert note.text == (
        "a plain-text note, where no lexer vouches for anything, so a body "
        "that writes # lup: out in the open still must not split into two."
    )
    assert note.end_line == 3


def test_url_before_a_trailing_note_does_not_swallow_it() -> None:
    # A marker trailing code is taken at its word, because nothing outside
    # Python can tell a comment's `//` from the one in `https://`. Reading a
    # real note as a mention is how feedback disappears unnoticed.
    shell = "curl https://example.com  # lup: pin this endpoint\n"
    assert texts(shell, ScanMode.TEXT) == ["pin this endpoint"]
    js = 'const at = "https://example.com";  // lup: pin this endpoint\n'
    assert texts(js, ScanMode.JS) == ["pin this endpoint"]


def test_json_holds_no_comment_so_a_marker_in_one_is_never_a_note() -> None:
    # Neither introducer opens a comment in JSON, so every marker in one is
    # string content — a note a trace recorded, not feedback left on the trace.
    recorded = '{"text": "# lup: fix the retry backoff"}\n'
    assert find_feedback(recorded, ScanMode.JSON) == []
    assert texts(recorded, ScanMode.TEXT) == ['fix the retry backoff"}']


def test_doubled_introducer_opens_a_note_and_ends_the_one_above() -> None:
    # `##` and `///` open one comment between them, so a marker written on the
    # run is on the opener, not quoted inside prose.
    assert texts("## lup: the doubled form is a note\n", ScanMode.MARKDOWN) == [
        "the doubled form is a note"
    ]
    # And it ends the note above rather than joining it, which is what keeps
    # removing that note from taking the suppression with it.
    source = "# lup: a note in a line-scanned file\n## lup: ignore[dict-get]\n"
    (note,) = find_feedback(source, ScanMode.TEXT)
    assert note.text == "a note in a line-scanned file"
    assert note.end_line == 1
    # Python is stricter, because a note there is placed by the tokenizer's
    # own comment column rather than by the run: a doubled marker in a `.py`
    # comment is code, as it has always been.
    assert find_feedback("## lup: not a note here\n", ScanMode.PYTHON) == []


def test_hash_in_docstring_prose_does_not_swallow_a_later_marker() -> None:
    # A docstring line opens no comment, so the marker in it is where its
    # prose begins no matter what characters came first.
    source = '"""Ids.\n\nUse #1 through #4. # lup: document the ids.\n"""\n'
    assert texts(source, ScanMode.PYTHON) == ["document the ids."]


def test_slash_note_absorbs_a_continuation_quoting_the_hash_spelling() -> None:
    # A run ends at a marker sitting on the line's own introducer, which the
    # mapper knows exactly — so a `//` note may quote the `#` spelling.
    source = (
        "// lup: the Python half of the tree writes\n"
        "// its suppressions as # lup: ignore[rule-id], which this note\n"
        "// has to be able to say.\n"
    )
    (note,) = find_feedback(source, ScanMode.JS)
    assert note.text == (
        "the Python half of the tree writes its suppressions as "
        "# lup: ignore[rule-id], which this note has to be able to say."
    )
    assert note.end_line == 3


def test_two_adjacent_notes_stay_separate() -> None:
    # The second marker opens its own comment, so it ends the first note's
    # run rather than joining it.
    source = (
        "# lup: the first note, which runs\n"
        "# onto a second line\n"
        "# lup: the second note, opening its own comment\n"
        "x = 1\n"
    )
    notes = find_feedback(source, ScanMode.PYTHON)
    assert [note.text for note in notes] == [
        "the first note, which runs onto a second line",
        "the second note, opening its own comment",
    ]
    assert [(note.start_line, note.end_line) for note in notes] == [(1, 2), (3, 3)]


def test_ignore_directive_below_a_note_ends_it_rather_than_joining_it() -> None:
    # A suppression opens its own comment too, so the note stops above it —
    # absorbing it would make removing the note take the directive along.
    source = (
        "# lup: the receiver is a vendor payload\n"
        "# lup: ignore[dict-get]\n"
        'value = payload.get("id")\n'
    )
    (note,) = find_feedback(source, ScanMode.PYTHON)
    assert note.text == "the receiver is a vendor payload"
    assert note.end_line == 1


def test_defer_note_parses_kind_condition_and_text() -> None:
    source = "# lup: defer[until v2 ships]: rework the cache layer\n"
    (note,) = find_feedback(source, ScanMode.PYTHON)
    assert note.kind == "defer"
    assert note.condition == "until v2 ships"
    assert note.text == "rework the cache layer"


def test_a_wake_condition_may_run_past_the_line_it_starts_on() -> None:
    """A gate worth stating is often longer than one line leaves room for.

    The head is parsed from a note's assembled text and continuation lines
    join it with a space, so a condition spanning them is one condition — the
    same reason a note's own message may run on. A gate that has to fit on one
    line is a gate written shorter than it needed to be, which is how a real
    externally-checkable condition decays into restating that this code might
    change again.
    """
    source = (
        "# lup: defer[until the v2 API ships and every caller of the old\n"
        "# endpoint has migrated off it]: rework the cache layer\n"
    )
    (note,) = find_feedback(source, ScanMode.PYTHON)
    assert note.kind == "defer"
    assert note.condition == (
        "until the v2 API ships and every caller of the old endpoint has "
        "migrated off it"
    )
    assert note.text == "rework the cache layer"


def test_bare_defer_parks_without_a_condition() -> None:
    source = "# lup: defer: rework the cache layer\n"
    (note,) = find_feedback(source, ScanMode.PYTHON)
    assert note.kind == "defer"
    assert note.condition is None
    assert note.text == "rework the cache layer"


def test_ordinary_note_has_note_kind_and_no_condition() -> None:
    source = "# lup: plain feedback\n"
    (note,) = find_feedback(source, ScanMode.PYTHON)
    assert note.kind == "note"
    assert note.condition is None


def test_defer_head_requires_its_colon() -> None:
    # Prose that merely starts with "defer" is ordinary feedback; the head's
    # colon — after a bracketed gate (whose syntax mirrors
    # `# lup: ignore[rule-id]`) or on its own — is the discriminator.
    source = "# lup: defer this until later\n# lup: deferred work is tracked inline\n"
    notes = find_feedback(source, ScanMode.MARKDOWN)
    assert [note.kind for note in notes] == ["note", "note"]


def test_bare_defer_parks_in_a_slash_comment() -> None:
    source = "const x = 1;  // lup: defer: split this module\n"
    (note,) = find_feedback(source, ScanMode.JS)
    assert (note.kind, note.condition) == ("defer", None)
    assert note.text == "split this module"


def test_bare_defer_parks_inside_a_docstring() -> None:
    source = '"""Module summary.\n\n#lup: defer: fold both scanners\n"""\n'
    (note,) = find_feedback(source, ScanMode.PYTHON)
    assert (note.kind, note.condition) == ("defer", None)
    assert note.text == "fold both scanners"


def test_bare_defer_continuation_lines_merge_into_the_message() -> None:
    source = "# lup: defer: fold both scanners\n# into one shared pass\nx = 1\n"
    (note,) = find_feedback(source, ScanMode.PYTHON)
    assert (note.kind, note.condition) == ("defer", None)
    assert note.text == "fold both scanners into one shared pass"


def test_defer_condition_may_contain_bracketed_rule_ids() -> None:
    # The condition syntax mirrors `ignore[rule-id]`, so conditions naming
    # such directives are expected input and must survive the round trip.
    source = "# lup: defer[when ignore[dict-get] sites migrate]: refine the rule\n"
    (note,) = find_feedback(source, ScanMode.PYTHON)
    assert note.kind == "defer"
    assert note.condition == "when ignore[dict-get] sites migrate"
    assert note.text == "refine the rule"
    assert note.marker_text() == (
        "defer[when ignore[dict-get] sites migrate]: refine the rule"
    )


def test_defer_head_ends_at_the_first_bracket_colon_delimiter() -> None:
    source = "# lup: defer[until v2]: see ignore[dict-get]: both sites\n"
    (note,) = find_feedback(source, ScanMode.PYTHON)
    assert note.kind == "defer"
    assert note.condition == "until v2"
    assert note.text == "see ignore[dict-get]: both sites"


def test_defer_with_empty_condition_stays_an_ordinary_note() -> None:
    # A bracket opened is a bracket that has to say something; parking behind
    # no gate at all is spelled `defer:` and parks like any other deferral.
    source = "# lup: defer[]: no wake condition given\n# lup: defer[ ]: blank\n"
    notes = find_feedback(source, ScanMode.MARKDOWN)
    assert [note.kind for note in notes] == ["note", "note"]


def test_defer_head_without_a_colon_stays_an_ordinary_note() -> None:
    # The documented grammar is `defer[<condition>]: <text>`; a head that
    # never closes with `]:` degrades to an ordinary red note rather than
    # a silently mangled condition.
    source = "# lup: defer[until v2] rework the cache\n"
    (note,) = find_feedback(source, ScanMode.PYTHON)
    assert note.kind == "note"
    assert note.condition is None


def test_defer_head_with_an_unclosed_bracket_stays_an_ordinary_note() -> None:
    # An optional bracket must not let a half-written gate fall back to the
    # bare spelling: the colon the bare head needs sits inside the bracket
    # that was opened, so the head is malformed and stays visible.
    source = "# lup: defer[until v2 ships: rework the cache\n"
    (note,) = find_feedback(source, ScanMode.PYTHON)
    assert note.kind == "note"
    assert note.condition is None


def test_defer_continuation_lines_merge_into_the_message() -> None:
    source = (
        "# lup: defer[until the sweep lands]: fold both scanners\n"
        "# into one shared pass\n"
        "x = 1\n"
    )
    (note,) = find_feedback(source, ScanMode.PYTHON)
    assert note.kind == "defer"
    assert note.condition == "until the sweep lands"
    assert note.text == "fold both scanners into one shared pass"


def test_typed_ignore_is_still_skipped_not_classified_as_defer() -> None:
    source = "x = 1  # lup: ignore[dict-get]\ny = 2  # lup: defer[never]: parked\n"
    (note,) = find_feedback(source, ScanMode.PYTHON)
    assert note.kind == "defer"
    assert note.start_line == 2


def test_gitignore_style_hash_comment_carries_a_defer_note() -> None:
    assert scan_mode_for(Path(".gitignore")) == ScanMode.TEXT
    source = (
        "notes/*\n"
        "!notes/.gitkeep\n"
        "# lup: defer[until branches merge]: purge notes history\n"
        "worktrees/\n"
    )
    (note,) = find_feedback(source, ScanMode.TEXT)
    assert note.kind == "defer"
    assert note.condition == "until branches merge"
    assert note.text == "purge notes history"
    assert (note.start_line, note.end_line) == (3, 3)


def test_marker_text_reconstitutes_the_defer_head() -> None:
    source = (
        "# lup: defer[until launch]: tighten the budget\n"
        "# lup: defer: tighten the budget\n"
        "# lup: plain note\n"
    )
    gated, bare, plain = find_feedback(source, ScanMode.MARKDOWN)
    assert gated.marker_text() == "defer[until launch]: tighten the budget"
    assert bare.marker_text() == "defer: tighten the budget"
    assert plain.marker_text() == "plain note"


def test_template_todos_are_never_classified_as_deferred() -> None:
    # The head that matches first wins: a customization marker whose text goes
    # on to mention a deferral is a template note carrying those words, never
    # parked work, so it cannot slip out of the customization walk.
    source = "# lup: template: defer[x]: choose your integrations\n"
    (todo,) = find_feedback(source, ScanMode.PYTHON)
    assert todo.kind == "template"
    assert todo.condition is None
    assert todo.text == "defer[x]: choose your integrations"


def test_defer_classification_survives_model_roundtrip() -> None:
    source = (
        "# lup: defer[until reviewed]: keep this parked\n"
        "# lup: defer: keep this parked\n"
    )
    parked = find_feedback(source, ScanMode.MARKDOWN)
    assert [note.condition for note in parked] == ["until reviewed", None]
    for note in parked:
        restored = MarkerComment.model_validate_json(note.model_dump_json())
        assert restored == note
        assert restored.marker_text() == note.marker_text()


def test_marker_comment_rejects_incoherent_kind_condition_pairs() -> None:
    def build(kind: str, condition: str | None) -> MarkerComment:
        return MarkerComment.model_validate(
            {
                "start_line": 1,
                "end_line": 1,
                "read_start": 1,
                "read_end": 1,
                "text": "body",
                "kind": kind,
                "condition": condition,
            }
        )

    with pytest.raises(ValidationError):
        build("note", "until then")
    with pytest.raises(ValidationError):
        build("solved", "until then")
    assert build("defer", "until then").condition == "until then"
    assert build("defer", None).condition is None


def test_fstring_contents_are_code_not_notes() -> None:
    # Since 3.12 an f-string lexes as start/middle/end tokens; only STRING
    # used to be masked, so marker text inside an f-string read as a comment.
    source = 'message = f"# lup: not feedback {value}"\n'
    assert find_feedback(source, ScanMode.PYTHON) == []


CLAIMS_SOURCE = """\
alpha = 1
# lup: solved: rework this section
# across the lines below
beta = 2
# lup: still open feedback
gamma = 3  # lup: solved: rename gamma
# lup: defer: parked work
delta = 4
"""


def test_retire_removes_only_solved_claims() -> None:
    revision = retire_claims(
        CLAIMS_SOURCE,
        ScanMode.PYTHON,
        [
            NoteTarget(line=2),
            NoteTarget(line=5),
            NoteTarget(line=7),
            NoteTarget(line=99),
        ],
    )
    assert [note.start_line for note in revision.revised] == [2]
    assert sorted(note.kind for note in revision.refused) == ["defer", "note"]
    assert [target.line for target in revision.missing] == [99]
    assert "rework this section" not in revision.text
    assert "still open feedback" in revision.text
    assert "defer: parked work" in revision.text


def test_retire_inline_claim_keeps_its_code() -> None:
    revision = retire_claims(CLAIMS_SOURCE, ScanMode.PYTHON, [NoteTarget(line=6)])
    assert "gamma = 3" in revision.text
    assert "rename gamma" not in revision.text


def test_restore_strips_the_head_and_keeps_continuations() -> None:
    revision = restore_claims(CLAIMS_SOURCE, ScanMode.PYTHON, [NoteTarget(line=2)])
    restored = [
        note
        for note in find_feedback(revision.text, ScanMode.PYTHON)
        if note.start_line == 2
    ]
    assert restored[0].kind == "note"
    assert restored[0].text == "rework this section across the lines below"
    assert "# across the lines below" in revision.text


def test_restore_narrowed_keeps_only_the_outstanding_part() -> None:
    revision = restore_claims(
        CLAIMS_SOURCE, ScanMode.PYTHON, [NoteTarget(line=2)], "the second half"
    )
    restored = [
        note
        for note in find_feedback(revision.text, ScanMode.PYTHON)
        if note.start_line == 2
    ]
    assert restored[0].kind == "note"
    assert restored[0].text == "the second half"
    assert "across the lines below" not in revision.text


def test_restore_refuses_notes_that_are_not_claims() -> None:
    revision = restore_claims(
        CLAIMS_SOURCE, ScanMode.PYTHON, [NoteTarget(line=5), NoteTarget(line=7)]
    )
    assert revision.revised == []
    assert sorted(note.kind for note in revision.refused) == ["defer", "note"]
    assert revision.text == CLAIMS_SOURCE
