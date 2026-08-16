"""Keeping oversized tool results from reaching the provider's truncation.

A provider truncates an MCP tool result past a few hundred thousand
characters and writes the overflow to a file. An agent reads that file, which
is also too large, and is handed the same redirect again — the loop costs a
context window and produces nothing. The fix is to never let the oversized
value reach the wire: :func:`spill_oversized_result` writes each large string
field to disk and leaves a pointer in its place, so what the agent receives is
a path and a preview it can page through.

Sizes here are defaults rather than constants. What counts as oversized is a
judgement about a provider's limit and how much of the window a caller is
willing to spend, so :func:`configure` takes each one.
"""

import logging
from collections.abc import Iterator
from itertools import count, groupby
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from pydantic import BaseModel

from lup.workspace.paths import outputs_dir

logger = logging.getLogger(__name__)

parser = MarkdownIt()


class ContentSafetyConfig(BaseModel):
    """Where spilled content lands, and the sizes that trigger a spill."""

    directory: Path
    spill_threshold: int = 150_000
    max_readable_size: int = 180_000
    preview_chars: int = 500
    label_fields: list[str] = [
        "url",
        "label",
        "file_path",
        "doc_id",
        "query",
        "path",
    ]


class ContentSafetyState(BaseModel):
    """Holder for the resolved configuration, mutated in place.

    Accessors and :func:`configure` share this one instance and assign its
    ``config`` attribute instead of rebinding a module global.
    """

    config: ContentSafetyConfig | None = None


state = ContentSafetyState()


def resolve_state() -> ContentSafetyConfig:
    """Return the cached configuration, defaulting the directory on first use."""
    config = state.config
    if config is None:
        config = ContentSafetyConfig(directory=outputs_dir())
        state.config = config
    return config


def configure(
    *,
    directory: Path | None = None,
    spill_threshold: int | None = None,
    max_readable_size: int | None = None,
    preview_chars: int | None = None,
    label_fields: list[str] | None = None,
) -> None:
    """Override where content is written and the sizes that trigger a spill.

    Args:
        directory: Where saved content lands. Defaults to the version's
            ``outputs/`` directory.
        spill_threshold: String fields longer than this are written to disk
            and replaced with a pointer.
        max_readable_size: Files larger than this are split by
            :func:`ensure_readable`.
        preview_chars: How much of the content a :class:`SavedContent`
            carries inline.
        label_fields: Which tool-input fields name the document a result came
            from, tried in order, so a spilled file is recognisable.
    """
    current = resolve_state()
    state.config = ContentSafetyConfig(
        directory=directory if directory is not None else current.directory,
        spill_threshold=(
            spill_threshold if spill_threshold is not None else current.spill_threshold
        ),
        max_readable_size=(
            max_readable_size
            if max_readable_size is not None
            else current.max_readable_size
        ),
        preview_chars=(
            preview_chars if preview_chars is not None else current.preview_chars
        ),
        label_fields=(
            label_fields if label_fields is not None else current.label_fields
        ),
    )


class SavedContent(BaseModel):
    """A written file, described well enough to decide whether to read it.

    Tools return this instead of a large string, so an agent always receives
    a path and a preview rather than something that might not survive the
    wire.
    """

    path: str
    word_count: int
    char_count: int
    preview: str


class Section(BaseModel):
    """One heading and the body that runs until the next one."""

    heading: str
    text: str


class HeadingStart(BaseModel):
    """Where a heading begins in the source, and what it reads."""

    line: int
    heading: str


class SpilledField(BaseModel):
    """A field whose content moved to disk, and the pointer left behind."""

    name: str
    pointer: str


def slugify_label(label: str) -> str:
    """Turn a label into a filename slug, keeping the identifying tail.

    A URL's scheme and ``www.`` say nothing about which document it is, and
    its leading path segments are usually shared across a site, so what
    survives is the last two segments — the part that differs.
    """
    parsed = urlsplit(label)
    host = parsed.netloc.removeprefix("www.")
    segments = [part for part in PurePosixPath(parsed.path).parts if part != "/"]
    identifying = [host, *segments] if host else segments
    tail = "-".join(identifying[-2:]) if identifying else label

    separated = (
        character if character.isalnum() and character.isascii() else "-"
        for character in tail.lower()
    )
    words = [
        "".join(group)
        for is_separator, group in groupby(separated, key=lambda char: char == "-")
        if not is_separator
    ]
    return "-".join(words)[:60]


def save_content(
    tool_name: str,
    label: str,
    content: str,
    directory: Path | None = None,
    ext: str = ".md",
) -> SavedContent:
    """Write content to disk and describe it.

    Re-saving identical content under the same label reuses the file rather
    than accumulating copies; content that differs takes the next free
    suffix, so two documents never collide under one slug.
    """
    config = resolve_state()
    target = directory if directory is not None else config.directory
    target.mkdir(parents=True, exist_ok=True)

    slug = slugify_label(label)
    path = next(
        candidate
        for candidate in (
            target / f"{tool_name}_{slug}{'' if index == 0 else f'_{index}'}{ext}"
            for index in count()
        )
        if not candidate.exists() or candidate.read_text(encoding="utf-8") == content
    )
    path.write_text(content, encoding="utf-8")

    preview = content[: config.preview_chars]
    if len(content) > config.preview_chars:
        preview += "…"

    return SavedContent(
        path=str(path),
        word_count=len(content.split()),
        char_count=len(content),
        preview=preview,
    )


def spill_oversized_result[T: BaseModel](
    tool_name: str,
    label: str,
    result: T,
    directory: Path | None = None,
) -> T:
    """Replace the model's oversized string fields with pointers to disk.

    Returns the model unchanged when nothing is over the threshold, so a
    caller can apply this to every result without paying for a copy.
    """
    config = resolve_state()

    def spilled() -> Iterator[SpilledField]:
        for field_name, value in result:
            if not isinstance(value, str) or len(value) <= config.spill_threshold:
                continue
            saved = save_content(tool_name, label, value, directory)
            yield SpilledField(
                name=field_name,
                pointer=(
                    f"Content written to {saved.path} ({saved.word_count} words). "
                    f"Use Read with offset/limit to access."
                ),
            )

    pointers = {field.name: field.pointer for field in spilled()}

    if not pointers:
        return result

    logger.info(
        "Spilling %d field(s) from %s (label=%s)",
        len(pointers),
        tool_name,
        label[:80],
    )
    return result.model_copy(update=pointers)


def guard_result[T: BaseModel](
    tool_name: str,
    params: BaseModel,  # lup: ignore[bare-basemodel] — any tool's input model
    result: T,
    directory: Path | None = None,
) -> T:
    """Spill a tool result's oversized fields, naming files from its own input.

    The entry point a tool boundary calls: it picks the label out of the
    validated input, so a spilled file is recognisable, and leaves the result
    untouched when nothing is over the threshold.

    *params* is any model because every tool declares its own input type and
    adopters declare more, so there is no union to name; the label is read by
    iterating fields for whichever of :attr:`ContentSafetyConfig.label_fields`
    that tool happens to carry, and never by touching one by name.
    """
    config = resolve_state()
    named = {name: value for name, value in params if isinstance(value, str) and value}
    label = next(
        (named[field] for field in config.label_fields if field in named), tool_name
    )
    return spill_oversized_result(tool_name, label, result, directory)


def split_on_headings(content: str) -> list[Section]:
    """Split Markdown at its top three heading levels.

    Text before the first heading becomes ``Preamble``; content with no
    headings stays whole under ``Full content``.

    The document is parsed rather than scanned, so a ``#`` inside a fenced
    code block stays code instead of silently becoming a split point.
    """
    tokens = parser.parse(content)
    starts = [
        HeadingStart(line=token.map[0], heading=tokens[position + 1].content.strip())
        for position, token in enumerate(tokens)
        if token.type == "heading_open"
        and token.tag in {"h1", "h2", "h3"}
        and token.map is not None
    ]

    if not starts:
        return [Section(heading="Full content", text=content)]

    lines = content.splitlines()

    def sections() -> Iterator[Section]:
        preamble = "\n".join(lines[: starts[0].line])
        if preamble.strip():
            yield Section(heading="Preamble", text=preamble)

        for position, start in enumerate(starts):
            following = starts[position + 1 :]
            end = following[0].line if following else len(lines)
            yield Section(
                heading=start.heading, text="\n".join(lines[start.line : end])
            )

    return list(sections())


def ensure_readable(path: Path, directory: Path | None = None) -> list[Path]:
    """Split a file too large to read into per-heading chunks.

    Returns the original path alone when the file is small enough, or when it
    has no headings to split on — a caller gets a list either way and does not
    branch on which happened.
    """
    config = resolve_state()
    content = path.read_text(encoding="utf-8")
    if len(content) <= config.max_readable_size:
        return [path]

    sections = split_on_headings(content)
    if len(sections) <= 1:
        return [path]

    target = directory if directory is not None else config.directory
    target.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix or ".md"

    def written() -> Iterator[Path]:
        for index, section in enumerate(sections):
            chunk_path = target / f"{path.stem}_{index}{suffix}"
            chunk_path.write_text(section.text, encoding="utf-8")
            yield chunk_path

    return list(written())
