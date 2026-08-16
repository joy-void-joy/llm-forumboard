"""Helpers for ``py source`` — package file trees."""

from pathlib import Path

# ---------------------------------------------------------------------------
# py source — view source code and package trees
# ---------------------------------------------------------------------------


def format_tree(root: Path, prefix: str = "") -> list[str]:
    """Build a tree display of Python files under a directory."""
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    dirs = [
        e
        for e in entries
        if e.is_dir() and not e.name.startswith(".") and e.name != "__pycache__"
    ]
    files = [e for e in entries if e.is_file() and e.suffix == ".py"]
    items: list[Path] = dirs + files
    lines: list[str] = []  # lup: ignore[empty-collection] — tree-render fold
    for i, item in enumerate(items):
        is_last = i == len(items) - 1
        connector = "└── " if is_last else "├── "
        suffix = "/" if item.is_dir() else ""
        lines.append(f"{prefix}{connector}{item.name}{suffix}")
        if item.is_dir():
            extension = "    " if is_last else "│   "
            lines.extend(format_tree(item, prefix + extension))
    return lines
