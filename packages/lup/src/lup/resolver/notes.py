"""Clearance of a concern's own review notes from its leased worktree.

An editor is given a generalized spec, not the note that produced it, so the
notes it owns leave the lease before it ever runs. That is what makes the
edit policy's marker gate cost nothing here: the worker never changes a
marker count, because there is no marker of its own left to change.

Clearance is scoped by note identity rather than by file. A lease removes
only spans whose body is byte-identical to one of its own persisted notes,
so a sibling concern's note sharing the same file — or the same function —
survives. Losing a foreign note is a review finding, never a side effect of
clearing.
"""

from collections import defaultdict
from pathlib import Path

from lup.codescan.markers import NoteTarget, remove_notes, scan_mode_for
from lup.resolver.models import Concern, NoteClearance, ReviewNote


def concern_note_targets(concern: Concern) -> dict[Path, list[NoteTarget]]:
    """Group a concern's notes into per-file targets carrying their text."""
    grouped: defaultdict[Path, list[NoteTarget]] = defaultdict(list)
    for note in concern.notes:
        grouped[note.file].append(NoteTarget(line=note.line, text=note.text))
    return dict(grouped)


def clear_concern_notes(root: Path, concern: Concern) -> NoteClearance:
    """Strip this concern's notes from one lease, reporting what was absent.

    An unreadable file and a note whose code a parent already deleted are the
    same outcome: the note is not there to clear. Both are recorded as missing
    rather than raised, because neither blocks the concern's actual work.
    """
    cleared: list[ReviewNote] = []
    missing: list[ReviewNote] = []
    for relative, targets in concern_note_targets(concern).items():
        path = root / relative
        found = {(target.line, target.text) for target in targets}
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            missing.extend(
                ReviewNote(file=relative, line=line, text=body or "")
                for line, body in sorted(found)
            )
            continue
        removal = remove_notes(text, scan_mode_for(path), targets)
        if removal.removed:
            path.write_text(removal.text, encoding="utf-8")
        cleared.extend(
            ReviewNote(file=relative, line=note.start_line, text=note.marker_text())
            for note in removal.removed
        )
        missing.extend(
            ReviewNote(file=relative, line=target.line, text=target.text or "")
            for target in removal.missing
        )
    return NoteClearance(concern_id=concern.id, cleared=cleared, missing=missing)
