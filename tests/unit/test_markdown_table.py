"""A generated table escapes what it holds, wherever the value came from.

Escaping used to be the caller's to remember, so the interesting cases are the
ones a caller would have had to think about: a pipe, which ends a cell early;
either line ending, which ends the row; and markup, which a reader would
otherwise see interpreted rather than quoted. A link's destination is checked
too, since it is the one value a cell does not otherwise escape. None of them
reaches a caller here — the part
is handed the literal value and renders it — so what these pin is that every
kind of cell escapes, not that some helper was called correctly.
"""

import pytest
from pydantic import ValidationError

from lup.adapters.harness import claude_prompt_renderer, codex_prompt_renderer
from lup.harness.models import MarkdownTable, PromptDocument, TextPart
from lup.markdown import CodeCell, HtmlCodeCell, LinkCell, PlainCell


def test_a_pipe_or_a_newline_cannot_break_out_of_the_cell_holding_it() -> None:
    """Every kind of cell, and the header, against both structural characters."""
    table = MarkdownTable(
        headers=["Rule | id", "Diagnostic"],
        rows=[
            [CodeCell(text="dict | get"), PlainCell(text="one\ntwo")],
            [
                HtmlCodeCell(text="`a | b`"),
                LinkCell(text="a | b", target="a|b.md"),
            ],
            [PlainCell(text="one\r\ntwo"), PlainCell(text="plain")],
        ],
    )

    lines = table.text_payload.splitlines()

    assert lines == [
        r"| Rule \| id | Diagnostic |",
        "| --- | --- |",
        r"| `dict \| get` | one two |",
        r"| <code>`a \| b`</code> | [a \| b](a\|b.md) |",
        "| one  two | plain |",
    ]
    # What has to hold is that no pipe left in a line is one a row splits on.
    assert all(line.replace(r"\|", "").count("|") == 3 for line in lines)


def test_a_code_span_carries_markup_as_it_reads_where_a_raw_element_escapes_it() -> (
    None
):
    """Opposite treatments, because one destination decodes an escape and one does not.

    Inside backticks nothing is read as markup and nothing is decoded, so `<`
    needs no help and `&lt;` written there is shown as the four characters it
    is. The raw `<code>` element is the other way about: its content is HTML,
    read by something that does decode.
    """
    table = MarkdownTable(
        headers=["Example"],
        rows=[[CodeCell(text='<b x="1">')], [HtmlCodeCell(text='<b x="1">')]],
    )

    assert '| `<b x="1">` |' in table.text_payload
    assert "| <code>&lt;b x=&quot;1&quot;&gt;</code> |" in table.text_payload


def test_markup_in_a_value_is_shown_rather_than_interpreted() -> None:
    table = MarkdownTable(headers=["Example"], rows=[[PlainCell(text='<b x="1">')]])

    assert "| &lt;b x=&quot;1&quot;&gt; |" in table.text_payload


def test_a_table_composes_into_a_document_like_any_other_part() -> None:
    """The point of the part: both runtimes read one table, spliced by nobody."""
    document = PromptDocument(
        parts=[
            TextPart(text="## Pages\n\n"),
            MarkdownTable(
                headers=["Page"],
                rows=[[LinkCell(text="rules.md", target="rules.md")]],
            ),
        ]
    )

    claude = claude_prompt_renderer().render(document)

    assert claude == codex_prompt_renderer().render(document)
    assert claude == "## Pages\n\n| Page |\n| --- |\n| [rules.md](rules.md) |\n"


def test_a_row_that_does_not_fit_the_header_is_refused() -> None:
    """A ragged row renders as a broken table rather than as an error."""
    with pytest.raises(ValidationError, match="under 2 headers"):
        MarkdownTable(headers=["Page", "Answers"], rows=[[PlainCell(text="alone")]])
