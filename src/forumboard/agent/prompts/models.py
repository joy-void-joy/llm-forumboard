"""What a prompt is made of, and how a deployment changes one.

A prompt here is not a string. It is an ordered composition of named pieces,
because the unit a deployment wants to change is never the whole instruction —
it is one paragraph of it, and usually the paragraph stating a fact about that
deployment rather than about this software.

Replacement is by name and produces a new composition. Nothing edits a prompt
in place, so what a pass is told is always the catalog's declaration plus a
recorded list of replacements, and both halves can be read.

``override`` deliberately ignores a name it does not hold. One list of
replacements is applied to every composition, and a piece shared between the
reviewer and the editor should land in both without the caller knowing which
compositions contain it. What that leaves — a name matching nothing anywhere,
which is a typo silently changing no prompt at all — is caught once by the
loader, which can see every composition at once and is the only place that
can tell "not here" from "nowhere".
"""

from pydantic import BaseModel


class Prompt(BaseModel, frozen=True):
    """One named piece of a prompt."""

    name: str
    """Stable identity a replacement targets. Never shown to the model."""
    text: str


class Prompts(BaseModel, frozen=True):
    """An ordered composition of named pieces."""

    pieces: list[Prompt]

    def holds(self, name: str) -> bool:
        """Whether this composition has a piece by that name."""
        return any(piece.name == name for piece in self.pieces)

    def override(self, replacements: list[Prompt]) -> "Prompts":
        """This composition with each named piece replaced, in place and in order.

        Order is the declaration's, never the replacement list's: a deployment
        rewording the audience must not thereby move it above the sentence that
        introduces it.
        """
        by_name = {piece.name: piece for piece in replacements}
        return Prompts(
            pieces=[
                by_name[piece.name] if piece.name in by_name else piece
                for piece in self.pieces
            ]
        )

    def render(self) -> str:
        """The pieces as the model receives them."""
        return "\n\n".join(piece.text for piece in self.pieces)
