"""Where a published page can be read, from the run that wrote it onwards.

Notion returns a page's URL beside its id, and a pass that keeps only the id
reports "1 published" while leaving the one thing it produced unreachable from
every command this project has. The counts were never the result; the page is.

Two surfaces answer, because two questions get asked. The run's own summary
answers "what just happened", and the stored record answers it again days
later through ``forumboard profile status``, when the terminal that printed it
is long gone.
"""

from datetime import datetime, timezone

from forumboard.agent.models import PublishedDiscussion, SkipReview
from forumboard.pipeline.sync import SyncOutcome
from forumboard.store import ConversationRecord

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
WHERE = "https://www.notion.so/3c07a4cdee1a81e487a7c2a5480b0a25"


def test_a_published_page_reports_where_it_can_be_read() -> None:
    """The counts say a page exists; only the URL says where."""
    outcome = SyncOutcome(profile="CeSIA", fetched=6, published=1, skipped=5)
    assert WHERE not in outcome.describe()

    reported = outcome.model_copy(update={"pages": [WHERE]})
    assert WHERE in reported.describe()
    assert "1 published" in reported.describe()


def test_a_profile_summary_gathers_every_page_it_wrote() -> None:
    """Two published pages are two links, not a count that hides one."""
    outcome = SyncOutcome(profile="CeSIA", published=2, pages=[WHERE, f"{WHERE}-two"])
    described = outcome.describe()
    assert WHERE in described
    assert f"{WHERE}-two" in described


def test_a_note_still_answers_alone() -> None:
    """An expired session says so and nothing else; no page went with it."""
    outcome = SyncOutcome(profile="CeSIA", note="session expired")
    assert outcome.describe() == "CeSIA: session expired"


def test_a_record_carries_its_page_to_the_status_listing() -> None:
    """`profile status` is where an operator looks for a page written days ago."""
    record = ConversationRecord(
        conversation_id="665349d9-d3a4-4639-90b2-7e2476460214",
        profile="CeSIA",
        title="A discussion",
        reviewed_at=NOW,
        review=SkipReview(reason="unused here"),
        edit=PublishedDiscussion(title="A discussion"),
        page_id="3c07a4cd",
        page_url=WHERE,
    )
    assert WHERE in record.describe()
    assert record.published()


def test_a_skipped_record_offers_no_link() -> None:
    """Nothing was written, so there is nowhere to point — and no empty line."""
    record = ConversationRecord(
        conversation_id="b8247875-b78c-47e4-9632-b1fd5f0fa05a",
        profile="CeSIA",
        title="French translation",
        reviewed_at=NOW,
        review=SkipReview(reason="too slight to publish"),
    )
    described = record.describe()
    assert "too slight to publish" in described
    assert "\n" not in described
    assert not record.published()
