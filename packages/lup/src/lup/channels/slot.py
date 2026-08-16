"""A value that settles once, with correctable offers until it does.

Three states rather than two. A slot may be *declared* — the decision exists
and is waiting — and it holds an *offer*, which any accepted door may
overwrite right up until one counts, and finally one *settled* value,
written once and never revised.

An offer may precede its declaration. That is deliberate: it is what lets a
flag settle a decision the run has not reached yet, so starting with the
answers already in hand costs no round trip.

Nothing here locks. A run holds its state lock for its entire life, so a
door that wanted the lock could only ever reach a dead run. Every publish is
an atomic temp-and-rename instead, and settling is an exclusive create, so
"first write wins" is decided by the filesystem rather than by a race
between readers.
"""

from pathlib import Path

from pydantic import BaseModel

from lup.channels.models import (
    ChannelConflictError,
    ChannelCorruptionError,
    Door,
    DoorPolicy,
    publish_atomic,
)

# The three file names of a slot's on-disk protocol, which a writing process
# and a reading one in another interpreter must spell alike to meet at all.
DECLARATION_FILE = "declared.json"  # lup: ignore[constant-declaration] — protocol name
OFFER_FILE = "offered.json"  # lup: ignore[constant-declaration] — protocol name
SETTLED_FILE = "settled.json"  # lup: ignore[constant-declaration] — protocol name


class Slot[T: BaseModel]:
    """One decision addressed by a directory, settling at most once."""

    def __init__(
        self, root: Path, model: type[T], doors: DoorPolicy | None = None
    ) -> None:
        self.root = root
        self.model = model
        self.doors = doors or DoorPolicy()

    def read(self, name: str) -> T | None:
        path = self.root / name
        if not path.exists():
            return None
        try:
            return self.model.model_validate_json(path.read_text("utf-8"))
        except ValueError as error:
            raise ChannelCorruptionError(
                f"{path} is not a {self.model.__name__}"
            ) from error

    def publish(self, name: str, record: T) -> None:
        publish_atomic(self.root / name, record)

    def admit(self, door: Door, act: str) -> None:
        if not self.doors.accepts(door):
            raise ChannelConflictError(
                f"{self.root.name!r} does not accept {act} from {door}"
            )

    def declare(self, record: T) -> None:
        """Declare what this slot decides; redeclaring the same is a no-op."""
        existing = self.read(DECLARATION_FILE)
        if existing is not None:
            if existing != record:
                raise ChannelConflictError(
                    f"{self.root.name!r} is already declared differently"
                )
            return
        self.publish(DECLARATION_FILE, record)

    def redeclare(self, record: T) -> None:
        """Replace what this slot decides, where its writer judged it the same.

        ``declare`` refuses a different record because two writers claiming
        one slot is the defect it guards. A declaration rendered from facts
        that move is the other case: the writer is the same one, and what it
        said went stale while the slot waited to be answered.
        """
        self.publish(DECLARATION_FILE, record)

    def declared(self) -> T | None:
        return self.read(DECLARATION_FILE)

    def offer(self, record: T, door: Door = Door.CONSOLE) -> None:
        """Propose a value, replacing any earlier proposal for this slot."""
        self.admit(door, "an offer")
        self.publish(OFFER_FILE, record)

    def offered(self) -> T | None:
        return self.read(OFFER_FILE)

    def settle(self, record: T, door: Door = Door.CONSOLE) -> bool:
        """Settle this slot, or report that another writer already did.

        The exclusive create is the whole mechanism: two doors racing to
        settle the same slot both succeed at the call they make, and exactly
        one of them created the file.
        """
        self.admit(door, "a value")
        path = self.root / SETTLED_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(record.model_dump_json(indent=2) + "\n")
        except FileExistsError:
            return False
        return True

    def settled(self) -> T | None:
        return self.read(SETTLED_FILE)

    def clear(self) -> None:
        """Drop this slot entirely, so a resumed run may decide it again."""
        for name in (DECLARATION_FILE, OFFER_FILE, SETTLED_FILE):
            (self.root / name).unlink(missing_ok=True)


class SlotSet[T: BaseModel]:
    """Every slot beneath one root, addressed by name."""

    def __init__(
        self, root: Path, model: type[T], doors: DoorPolicy | None = None
    ) -> None:
        self.root = root
        self.model = model
        self.doors = doors or DoorPolicy()

    def slot(self, name: str) -> Slot[T]:
        return Slot(self.root / name, self.model, self.doors)

    def names(self) -> list[str]:
        """Every slot that exists, declared or merely offered to."""
        if not self.root.is_dir():
            return []
        return sorted(path.name for path in self.root.iterdir() if path.is_dir())

    def declared(self) -> list[T]:
        found = [self.slot(name).declared() for name in self.names()]
        return [record for record in found if record is not None]

    def offered(self) -> list[T]:
        found = [self.slot(name).offered() for name in self.names()]
        return [record for record in found if record is not None]

    def settled(self) -> list[T]:
        found = [self.slot(name).settled() for name in self.names()]
        return [record for record in found if record is not None]

    def settled_names(self) -> list[str]:
        return [name for name in self.names() if self.slot(name).settled() is not None]
