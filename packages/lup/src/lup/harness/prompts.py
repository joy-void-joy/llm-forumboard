"""The one prompt renderer, spelling each part through a runtime's vocabulary.

Rendering a prompt document is the same walk for every runtime: which parts
appear and in what order is portable, and only the words differ. The walk asks
each part to spell itself and hands it the vocabulary to do so, which keeps a
new runtime a :class:`NativeSpellings` rather than another copy of the walk, a
new kind of part one class rather than an arm here, and this module free of
both any platform's spelling and any part's identity. The guidance banner is
composed here for the same reason: what it has to say about the other trees is
a spelling question.
"""

from lup.harness.banner import REGENERATE_COMMAND, GeneratedBanner
from lup.harness.contracts import NativeSpellings, PromptRenderer
from lup.harness.models import LocatedPart, NativePath, PromptDocument


def sentences(*parts: str) -> str:
    """Join what each part says, leaving no gap where one says nothing.

    A vocabulary that declines an idea contributes an empty string, and the
    sentence around it should read as though the idea was never raised rather
    than carry the space where its words would have gone.
    """
    return " ".join(part for part in parts if part)


# lup: ignore[constant-declaration] — one reason every tree renders identically,
# declared with the prompt it belongs to rather than chosen per caller
SPAWNED_SESSION_LOSES_SHELL = (
    "Every session the run opens is a child of this call, and a session "
    "spawned inside a sandbox cannot create the per-session state its own "
    "shell needs — so each of its shell calls dies on a read-only filesystem, "
    "leaving planners and workers unable to run a single command while still "
    "appearing to work."
)
"""Why entering the resolver wants to run outside the sandbox.

The need is the same wherever the resolver is entered from, and only the
escape differs — one runtime spells a per-call flag, another has nothing to
spell — so both entries state the need from here and let their own vocabulary
answer it.
"""


def guidance_banner(
    prompts: PromptRenderer, guidance: PromptDocument
) -> GeneratedBanner:
    """Name the one guidance source, and every tree that renders a copy of it.

    Each runtime reads guidance under its own name, so a reader who opens one
    copy has to be told the other is not a second document to keep in step.
    """
    every_tree = prompts.location(
        NativePath(location="guidance_file", scope="every_tree")
    )
    return GeneratedBanner(
        source=guidance.declared_source(),
        command=REGENERATE_COMMAND,
        notes=[f"Deliberately rendered as {every_tree}."],
    )


class SpelledPromptRenderer(PromptRenderer):
    """Render every part through the vocabulary of the runtime that reads it.

    ``every`` carries the other runtimes so a location can teach every tree at
    once. That rendering is identical whichever runtime produces it, because
    the product names come from the vocabularies themselves.
    """

    def __init__(self, own: NativeSpellings, every: list[NativeSpellings]) -> None:
        self.own = own
        self.every = every

    def render(self, prompt: PromptDocument) -> str:
        text = "".join(part.spell(self) for part in prompt.parts)
        return text if text.endswith("\n") else text + "\n"

    def location(self, part: LocatedPart) -> str:
        """Spell one location for the reader, or for every runtime at once."""
        match part.scope:
            case "this_tree":
                return part.spell_in(self.own)
            case "every_tree":
                return ", ".join(
                    f"{part.spell_in(runtime)} under {runtime.runtime_name}"
                    for runtime in self.every
                )
