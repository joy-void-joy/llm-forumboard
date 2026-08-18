"""What a periodic recap is told.

Written from what was published rather than from the source, so it carries no
sensitivity test either — and it is stamped merged when it is written, because
a briefing drawn from discussions the worldview has already read would
otherwise let the worldview cite its own summary as a source.

The cadence and window are a piece replaced per call, on the same mechanism a
deployment's own overrides use.
"""

from forumboard.agent.prompts.models import Prompt, Prompts


def window_piece(cadence: str, window: str) -> Prompt:
    """Which recap this is, as the piece of the prompt that says so."""
    return Prompt(
        name="briefing-window",
        text=f"Write the {cadence} briefing covering {window}.",
    )


PROMPT = Prompts(
    pieces=[
        Prompt(name="briefing-window", text="(supplied per call)"),
        Prompt(
            name="briefing-role",
            text="""\
You are given the discussions published in that window. Recap them: what
happened, what came up repeatedly, what changed, what is outstanding. This is
a plain note somebody reads to catch up — not a worldview page, not a list of
links.

Draw only on what you were given. You are reading published, redacted
discussions; do not reach past them, and do not speculate about what was left
out. Where the window was quiet, say so briefly rather than padding.

Name anybody with something waiting on them, by the name the discussions use.""",
        ),
    ]
)
