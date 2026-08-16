"""Session commit operations: stage and commit per-session result files.

Backs ``lup-devtools feedback commit`` — one ``data(sessions):`` commit per
session, matched against the *configured* trace root so a relocated
``AGENT_NOTES_PATH`` keeps working.
"""

import logging
from pathlib import Path

import sh
import typer

from lup.workspace.history import iter_session_dirs, latest_session_record
from lup.workspace.paths import project_root, traces_path
from lup.devtools.utils import git

logger = logging.getLogger(__name__)


def get_uncommitted_session_ids() -> list[str]:
    """Find session IDs with uncommitted result files, deduplicated, file order.

    Paths are matched against the *configured* trace root (``lup.workspace.paths``),
    so a relocated ``AGENT_NOTES_PATH`` keeps ``feedback commit`` working.
    The layout below the root is ``<version>/(sessions|logs)/<session_id>/``.
    Uses ``-z`` so paths with spaces or quoting never shear.
    """
    try:
        traces_rel = traces_path().relative_to(project_root())
    except ValueError:
        # Trace root configured outside the repo — nothing for git to commit
        return []

    status = str(git.status("--porcelain", "-z", "--", str(traces_rel), _ok_code=[0]))
    return session_ids_from_status(status, traces_rel)


def session_ids_from_status(
    status: str,
    traces_rel: Path,
) -> list[str]:
    """Parse ``git status --porcelain -z`` output into deduplicated session IDs.

    Only paths under ``traces_rel`` with the versioned layout
    (``<version>/(sessions|logs)/<session_id>/...``) count; rename/copy
    entries contribute their target path and their source is discarded.
    """
    session_ids: list[str] = []  # lup: ignore[empty-collection] — record fold
    chunks = iter(status.split("\0"))  # lup: ignore[string-split] — -z records
    for chunk in chunks:
        if len(chunk) < 4:
            continue
        code, file_path = chunk[:2], chunk[3:]
        if code.startswith(("R", "C")):
            next(chunks, None)  # discard the rename/copy source path
        relative = Path(file_path)
        if not relative.is_relative_to(traces_rel):
            continue
        match relative.relative_to(traces_rel).parts:
            case (_, "sessions" | "logs", session_id, *_):
                session_ids.append(session_id)

    return list(dict.fromkeys(session_ids))


def get_session_summary(session_id: str) -> str:
    """Read summary from the latest session JSON across all versions."""
    record = latest_session_record(session_id)
    if record is None:
        return f"session {session_id}"
    match record.output:
        case {"summary": str(summary)}:
            return summary[:50]
    return f"session {session_id}"


def commit_session(session_id: str, *, dry_run: bool = False) -> bool:
    """Stage and commit files for a single session ID."""
    paths = [
        str(session_dir) for session_dir in iter_session_dirs(session_id=session_id)
    ]

    if traces_path().exists():
        for ver_dir in traces_path().iterdir():
            if not ver_dir.is_dir():
                continue
            log_dir = ver_dir / "logs" / session_id
            if log_dir.exists():
                paths.append(str(log_dir))

    if not paths:
        return False

    if dry_run:
        summary = get_session_summary(session_id)
        typer.echo(f"  Would commit {session_id}: {summary}")
        for p in paths:
            typer.echo(f"    {p}")
        return True

    for path in paths:
        try:
            git.add(path)
        except sh.ErrorReturnCode as e:
            logger.warning("Failed to stage %s: %s", path, e)

    if not git.out("diff", "--cached", "--stat", _ok_code=[0, 1]):
        return False

    summary = get_session_summary(session_id)
    slug = summary[:50].strip().rstrip(".")  # lup: ignore[string-strip] — prose slug
    git.commit("-m", f"data(sessions): {slug}")
    typer.echo(f"  Committed {session_id}: {slug}")
    return True
