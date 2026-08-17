"""The roster as something an operator can act on, in a terminal.

The listing was already derived once so that no two surfaces could disagree
about who is enrolled. This is the other half of that: the acts are derived
once too, so the terminal offers exactly what the served page offers, and
picking a profile means pointing at a row rather than retyping a name the
listing is already showing.

Redrawn from :func:`forumboard.profiles.profile_states` on every pass, never
from a copy held across an act. What was just done to a profile is visible in
the next listing because the next listing is a fresh read, not because
something remembered to update a row.
"""

import asyncio
from itertools import count
from pathlib import Path

import typer
from rich.padding import Padding

from lup.devtools.setup import console

from forumboard.enrolment import ProfileAct, ProfileActs, ProfileTarget
from forumboard.profiles import ProfileState, profile_states


def show_listing(states: list[ProfileState]) -> None:
    """Print every profile against the number that selects it."""
    for number, state in enumerate(states, start=1):
        console.print(f"  [bold]{number}[/]  {state.describe()}")
    syncable = [state.name for state in states if state.syncable()]
    console.print(
        f"\n  A sync pass reads {', '.join(syncable)}."
        if syncable
        else "\n  A sync pass reads nobody yet."
    )


def choose_profile(states: list[ProfileState]) -> str:
    """Ask which profile to act on, or "" when the operator is done.

    Adding somebody is a name typed here rather than a mode of its own: what
    comes back is a name either way, and a name nobody has heard of has a state
    like anyone else's — all three facts false — so the menu that follows is
    built the same way for both.
    """
    numbered = "Number to act on, " if states else ""
    answer = typer.prompt(
        f"\n  {numbered}[a]dd somebody, [q] done",
        default="q",
        show_default=False,
    ).strip()
    match answer:
        case "q" | "":
            return ""
        case "a":
            return typer.prompt("  Profile name").strip()
        case digits if digits.isdigit() and 1 <= int(digits) <= len(states):
            return states[int(digits) - 1].name
        case _:
            console.print("  [yellow]Not one of those.[/]")
            return choose_profile(states)


def choose_act(offered: list[ProfileAct], state: ProfileState) -> ProfileAct | None:
    """Ask which act to run on one profile, or None to go back."""
    console.print(f"\n  [bold]{state.describe()}[/]")
    for number, act in enumerate(offered, start=1):
        console.print(f"  [bold]{number}[/]  {act.label}")
        # Padded rather than indented by hand: what an act costs is the part
        # worth reading, and a consequence long enough to wrap would otherwise
        # continue at column zero where it reads as a line of its own.
        console.print(Padding(f"[dim]{act.consequence}[/]", (0, 0, 0, 7)))
    back = len(offered) + 1
    console.print(f"  [bold]{back}[/]  Back")
    answer = typer.prompt("  Choose", default=str(back), show_default=False).strip()
    match answer:
        case digits if digits.isdigit() and 1 <= int(digits) <= len(offered):
            return offered[int(digits) - 1]
        case _:
            return None


def answered(act: ProfileAct) -> ProfileAct:
    """The act carrying whatever it asks for, prompted for here.

    A destructive act asks for its own profile name, and it is the act that
    checks what came back — so declining at this prompt and mistyping at it
    reach the same refusal rather than two different ones.
    """
    if not act.asks:
        return act
    return act.model_copy(update={"answer": typer.prompt(f"  {act.asks}", default="")})


def act_on(target: ProfileTarget, acts: ProfileActs) -> None:
    """Offer what this profile's state allows, and run what was chosen."""
    state = target.state()
    offered = acts.offered(state)
    chosen = choose_act(offered, state)
    if chosen is None:
        return
    outcome = asyncio.run(answered(chosen).perform(target))
    style = "green" if outcome.changed else "yellow"
    console.print(f"  [{style}]{outcome.message}[/]")


def edit_roster(
    project_root: Path, profiles: Path, acts: ProfileActs | None = None
) -> None:
    """Show the roster and act on it until the operator is done.

    Both roots are given rather than derived, as everything reading this
    listing takes them, so a test drives a whole editing session against a
    scratch workspace.
    """
    catalogue = acts or ProfileActs()
    for _ in count():
        states = profile_states(project_root, profiles)
        show_listing(states)
        name = choose_profile(states)
        if not name:
            return
        act_on(
            ProfileTarget(project_root=project_root, profiles_root=profiles, name=name),
            catalogue,
        )
