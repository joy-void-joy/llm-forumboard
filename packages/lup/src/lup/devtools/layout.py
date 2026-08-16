"""Checkout layout: where a repository keeps its sibling worktrees.

Both the worktree workflows and the native launchers resolve the same
``tree/`` directory — the workflows to place a new checkout, the launchers
to widen a sandbox write root over it — so the layout is a fact of its own
rather than a detail of either caller.
"""

from pathlib import Path

import typer


def get_tree_dir() -> Path:
    """Locate the ``tree/`` directory that holds sibling worktrees.

    Two checkout layouts are supported. In the bare-repo layout the current
    checkout is itself a worktree living inside ``tree/``, so ``tree/`` is the
    parent. Otherwise ``tree/`` sits at the current directory or an ancestor,
    so walking upward lets the command run from anywhere inside the checkout.
    """
    cwd = Path.cwd().resolve()

    if cwd.parent.name == "tree":
        return cwd.parent

    for directory in (cwd, *cwd.parents):
        tree = directory / "tree"
        if tree.is_dir():
            return tree

    typer.echo("Error: Could not find tree/ directory", err=True)
    raise typer.Exit(1)
