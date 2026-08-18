"""Every declared prompt, and the deployment's own replacements over them.

This is the composition root for what the passes are told. The catalog holds
what ships; the local file holds what one deployment changed; and the only
thing that ever reaches a model is the first with the second applied.

The local file is Python rather than data because a prompt is prose that has
to be composed, and prose in JSON is a single escaped line nobody will edit
twice. It lives outside the package, under a path that is gitignored, for the
reason the roster is: a deployment's own text says who this board is for, and
that is exactly what a published file must not carry.

A replacement naming nothing at all is an error rather than a no-op. Silently
ignoring it would let a typo in the one file that softens a safety instruction
report success while changing nothing — the failure most worth designing out,
because nothing downstream would ever look wrong.
"""

import importlib.util
import logging
from functools import cache
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

import lup.workspace.paths

from forumboard.agent.config import settings
from forumboard.agent.models import ReviewPlan
from forumboard.agent.prompts import briefing, editor, reviewer, worldview
from forumboard.agent.prompts.models import Prompt, Prompts

logger = logging.getLogger(__name__)


class Declared(BaseModel, frozen=True):
    """One pass's prompt, under the name an operator asks for it by."""

    name: str
    composition: Prompts


# lup: ignore[constant-declaration] — a composition root: which prompts this
# deployment has is decided here rather than passed on, and the replacements a
# caller would otherwise supply are what the local file already is
BY_NAME = [
    Declared(name="reviewer", composition=reviewer.PROMPT),
    Declared(name="editor", composition=editor.PROMPT),
    Declared(name="worldview", composition=worldview.PROMPT),
    Declared(name="briefing", composition=briefing.PROMPT),
]
"""Every composition a replacement could name, and what to call each one."""


def local_path() -> Path:
    """Where this deployment's own replacements live, absolute."""
    declared = Path(settings.prompts_local_path)
    if declared.is_absolute():
        return declared
    return lup.workspace.paths.project_root() / declared


def read_local(path: Path) -> list[Prompt]:
    """The replacements one Python file declares, or none where it declares none."""
    specification = importlib.util.spec_from_file_location("forumboard_prompts", path)
    if specification is None or specification.loader is None:
        raise ValueError(f"{path} is not a Python module this can load")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    declared = getattr(module, "OVERRIDES", None)
    if declared is None:
        logger.warning("%s declares no OVERRIDES; no prompt is replaced", path)
        return list[Prompt]()
    return TypeAdapter(list[Prompt]).validate_python(declared)


@cache
def overrides() -> tuple[Prompt, ...]:
    """This deployment's replacements, validated against what is declared.

    Cached because it reads a file and every pass asks for it. A test moving
    the path calls ``overrides.cache_clear()``.
    """
    path = local_path()
    if not path.is_file():
        return ()
    replacements = read_local(path)
    unmatched = [
        piece.name
        for piece in replacements
        if not any(each.composition.holds(piece.name) for each in BY_NAME)
    ]
    if unmatched:
        known = sorted(
            piece.name for each in BY_NAME for piece in each.composition.pieces
        )
        raise ValueError(
            f"{path} replaces prompt piece(s) nothing declares: "
            f"{', '.join(unmatched)}. Declared pieces are: {', '.join(known)}"
        )
    logger.info(
        "%s replaces %d prompt piece(s): %s",
        path,
        len(replacements),
        ", ".join(piece.name for piece in replacements),
    )
    return tuple(replacements)


def composed(declaration: Prompts) -> Prompts:
    """One declared composition with this deployment's replacements applied."""
    return declaration.override(list(overrides()))


def reviewer_prompt() -> str:
    """The system prompt for the pass that decides whether to publish."""
    return composed(reviewer.PROMPT).render()


def editor_prompt(plan: ReviewPlan) -> str:
    """The system prompt for the pass that produces what gets published.

    The plan is applied after the deployment's replacements, so a local file
    cannot displace the brief this particular call is working from.
    """
    return composed(editor.PROMPT).override([editor.plan_piece(plan)]).render()


def worldview_prompt() -> str:
    """The system prompt for the pass that maintains the current worldview."""
    return composed(worldview.PROMPT).render()


def briefing_prompt(cadence: str, window: str) -> str:
    """The system prompt for a periodic recap."""
    replaced = composed(briefing.PROMPT)
    return replaced.override([briefing.window_piece(cadence, window)]).render()


def rendered(name: str) -> str | None:
    """One pass's prompt as the model receives it, or None where no pass is named.

    The per-call pieces — the editor's plan, the briefing's window — render as
    the placeholders they are declared with. What this answers is what the
    deployment's own text did, and that is the same either way.
    """
    for each in BY_NAME:
        if each.name == name:
            return composed(each.composition).render()
    return None


def get_system_prompt() -> str:
    """The prompt for a tool-using session, which is the worldview pass."""
    return worldview_prompt()
