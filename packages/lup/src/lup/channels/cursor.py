"""Where each reader of a stream has consumed to, across processes.

A stream is replayable from any offset, so who has read what is the
reader's own business — and a reader that keeps that position in memory
resumes at whatever the head happens to be the next time it is
constructed. Every record written while it was away is then skipped rather
than delivered late, which is indistinguishable from the stream having been
empty.

The position is therefore a file, named for the reader, holding the offset
that consumes exactly what that reader has been handed. An unreadable one
is refused rather than read as zero or as the head: one would replay a
run's whole history at a reader and the other would silently drop it, and
neither is a thing to decide on a reader's behalf.
"""

from pathlib import Path

from pydantic import BaseModel

from lup.channels.models import ChannelCorruptionError, publish_atomic


class ReaderPosition(BaseModel, frozen=True):
    """One reader's committed offset into one stream."""

    offset: int


class StreamCursors:
    """Every reader's durable position in one stream, one file each."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, reader: str) -> Path:
        if not reader or Path(reader).name != reader:
            raise ValueError(f"stream reader {reader!r} is not a path-safe name")
        return self.root / f"{reader}.json"

    def offset(self, reader: str) -> int:
        """Where this reader resumes, which for one that never read is zero."""
        path = self.path(reader)
        if not path.exists():
            return 0
        try:
            return ReaderPosition.model_validate_json(path.read_text("utf-8")).offset
        except ValueError as error:
            raise ChannelCorruptionError(f"{path} is not a reader position") from error

    def commit(self, reader: str, offset: int) -> None:
        """Record that this reader has been handed everything through ``offset``."""
        publish_atomic(self.path(reader), ReaderPosition(offset=offset))

    def readers(self) -> list[str]:
        """Every reader that has committed a position, for a caller listing them."""
        if not self.root.is_dir():
            return []
        return sorted(path.stem for path in self.root.glob("*.json"))
