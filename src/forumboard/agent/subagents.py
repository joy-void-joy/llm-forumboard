# lup: ignore[constant-declaration]
# The prompt here is the subagent's own standing prose and the roster is which
# subagents this application declares — both are what this module is for, and a
# project wanting others writes them here.
"""Subagent definitions.

One role, because there is one job worth delegating. The worldview agent has
to hold every unmerged discussion in mind to decide what the topics *are*, but
writing any single topic needs only the discussions that touch it. A pass over
forty topics that drafted each one inline would carry every transcript it ever
read into every subsequent decision; delegating the drafting keeps the deciding
context small and lets the drafts happen independently.

The drafter cannot write to Notion. It reads and returns prose, and the
worldview agent calls ``write_topic`` with what comes back — so the one place a
topic page is replaced stays the one place, and a subagent cannot half-apply a
rewrite it was in the middle of.

Subagents are one of several agent shapes — ``docs/orchestration.md`` is the
full catalog. The siblings this project uses: the reviewer and the editor are
one-shot structured queries (``pipeline/passes.py``), and the worldview pass
itself is an ordinary tool-using session (``pipeline/worldview.py``).
"""

from lup.types import SubagentSpec


def drafting_tools() -> list[str]:
    """The tools a topic drafter may call.

    Reading only. A drafter that could write would be a second path to
    replacing a page, and the value of having one path is that a crash
    mid-rewrite leaves a page either wholly old or wholly new.
    """
    return [
        "mcp__worldview__read_discussion",
        "mcp__worldview__list_unmerged_discussions",
        "mcp__worldview__read_topic",
        "mcp__worldview__list_people",
    ]


TOPIC_DRAFTER_PROMPT = """\
You draft one topic page for a current-worldview board. You do not write it —
you return the text, and the agent that asked for it writes the page.

You are told which topic, and which discussions bear on it. Read the topic's
current page if it has one, and read the discussions you are pointed at.

Return the complete page in Markdown, answering three questions:

- **What is happening** — the current state, not how it got there.
- **What is waiting on us** — decisions unmade, work blocked, questions open.
- **What each person needs to know** — named, specific, actionable.

Write for somebody who has been away two weeks and wants to know where things
stand. They do not want a chronology.

This text REPLACES the page. Carry forward whatever is still true; drop what
has stopped mattering. Never write "Update:", never keep an outdated paragraph
for the record, never write "previously we thought". If something from weeks
ago still shapes the present, describe the present shape rather than the event.

You are reading published, redacted discussions. Do not speculate about what
might have been removed, and do not reach past what you were given.

End with a short line naming anyone who should look at this now, drawn only
from `list_people`.
"""

topic_drafter = SubagentSpec(
    name="topic-drafter",
    description=(
        "Drafts one worldview topic page from the discussions that bear on it. "
        "Reads only; returns the page text for the caller to write."
    ),
    prompt=TOPIC_DRAFTER_PROMPT,
    tools=drafting_tools(),
    model="claude-opus-5",
)


ALL_SPECS: list[SubagentSpec] = [topic_drafter]


def get_subagent_specs() -> list[SubagentSpec]:
    """Return all subagent specs (SDK-agnostic).

    Each adapter converts these into its native primitive at build time.
    """
    return list(ALL_SPECS)
