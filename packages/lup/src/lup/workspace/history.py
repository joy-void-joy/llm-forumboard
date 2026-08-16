# lup: ignore[import-re, re-call]
# The semver parser is a three-integer pattern over version directory names —
# regex is the tool, so those rules are opted out file-wide.
"""Session history storage, retrieval, and cross-version data discovery.

This module handles:
1. Saving session results to notes/traces/<version>/sessions/
2. Loading past sessions for context or analysis (across versions)
3. Tracking session metadata (submitted, outcome, etc.)
4. Cross-version iteration over sessions, outputs, and trace logs
5. Version scope resolution with progressive semver fallback

The feedback loop scripts read from this storage.

All functions accept :class:`pydantic.BaseModel` instances and work
with raw JSON dicts — no dependency on domain-specific models.
The ``format_history_for_context`` function accepts a pluggable
formatter so downstream projects can display domain-specific fields
without modifying this module.

Examples:
    Save and load session results::

        >>> from pydantic import BaseModel
        >>> class MyResult(BaseModel):
        ...     summary: str
        ...     confidence: float
        >>> path = save_session(MyResult(summary="done", confidence=0.9), session_id="s1")
        >>> path.exists()
        True

    Load past sessions for analysis::

        >>> sessions = load_session_records("s1")
        >>> len(sessions)
        1
        >>> sessions[0].output["summary"]
        'done'

    Iterate sessions across versions::

        >>> for session_dir in iter_session_dirs(session_id="my-session"):
        ...     print(session_dir)
        PosixPath('.../notes/traces/0.1.0/sessions/my-session')

    Resolve version scope with progressive fallback::

        >>> resolve_version("0.1.0").versions
        ['0.1.0']
"""

import json
import logging
import re
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, SerializeAsAny, ValidationError

from lup.types import JsonObject, JsonValue, Usage
from lup.telemetry.metrics import MetricsSummary
from lup.workspace.paths import (
    TIMESTAMP_FMT,
    agent_version,
    parse_timestamp,
    sessions_dir,
    traces_path,
)

logger = logging.getLogger(__name__)


class SessionResult[OutputT: BaseModel](BaseModel):
    """Complete result of an agent session.

    Generic over the output type so domain-specific agent output
    models can be used without modifying this module.

    This captures everything needed for the feedback loop:
    - The structured output
    - Metadata (timing, cost, token usage)
    - Tool metrics for analysis
    """

    session_id: str
    task_id: str | None = Field(default=None, description="Domain-specific task ID")
    agent_version: str = Field(
        default="", description="Agent version that produced this result"
    )
    agent_sdk: str | None = Field(
        default=None,
        description="Provider adapter that ran the session; "
        "None on results predating the stamp",
    )
    sdk_session_id: str | None = Field(
        default=None,
        description="Provider-native session id — pass it as SessionId to "
        "SessionFactory.open(); None when the provider reported none",
    )
    timestamp: str
    output: OutputT
    reasoning: str = Field(default="", description="Raw reasoning text")
    sources_consulted: list[str] = []
    duration_seconds: float | None = None
    cost_usd: float | None = None
    token_usage: SerializeAsAny[Usage] | None = None
    tool_metrics: MetricsSummary | None = None
    outcome: str | None = Field(default=None, description="Outcome after resolution")


class SessionRecord(BaseModel, extra="allow"):
    """A session result read back from disk, tolerant of domain variation.

    The read-side counterpart of :class:`SessionResult`: every core field
    is defaulted (old sessions may predate it), ``output`` stays raw JSON
    because the domain's output model is not known at read time, and
    fields a domain adds to its result model survive via ``extra="allow"``.
    """

    session_id: str = ""
    task_id: str | None = None
    agent_version: str = ""
    agent_sdk: str | None = None
    sdk_session_id: str | None = None
    timestamp: str = ""
    output: JsonObject = {}
    reasoning: str = ""
    sources_consulted: list[str] = []
    duration_seconds: float | None = None
    cost_usd: float | None = None
    token_usage: Usage | None = None
    tool_metrics: MetricsSummary | None = None
    outcome: JsonValue = None

    def markdown_summary(self) -> str:
        """Format this session record as a markdown summary.

        Extracts common fields that most domains will have. Downstream
        projects can provide a custom formatter for domain-specific display.
        """
        stamp = self.timestamp or "unknown"
        lines: list[str] = [f"### {stamp}"]

        output = self.output
        if "summary" in output:
            lines.append(f"**Summary**: {str(output['summary'])[:200]}...")
        if "confidence" in output:
            confidence = output["confidence"]
            if isinstance(confidence, (int, float)):
                lines.append(f"**Confidence**: {confidence:.1%}")

        if self.outcome:
            lines.append(f"**Outcome**: {self.outcome}")

        lines.append("")
        return "\n".join(lines)


def save_session(
    result: BaseModel,  # lup: ignore[bare-basemodel] — any domain's result model
    *,
    session_id: str,
) -> Path:
    """Save a session result to disk.

    Args:
        result: Any Pydantic model representing a session result.
        session_id: Unique session identifier.

    Returns:
        Path to the saved file.
    """
    session_dir = sessions_dir() / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime(TIMESTAMP_FMT)
    filepath = session_dir / f"{timestamp}.json"

    filepath.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    logger.info("Saved session %s to %s", session_id, filepath)

    return filepath


def session_backend(session_dir: Path) -> str | None:
    """Read the ``agent_sdk`` stamp from a session dir's newest result JSON.

    Returns None when no result JSON exists or none carries the stamp
    (sessions predating it) — display code renders that as unknown
    rather than guessing a backend.
    """
    for filepath in sorted(session_dir.glob("[0-9]*.json"), reverse=True):
        try:
            data = json.loads(filepath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        try:
            record = SessionRecord.model_validate(data)
        except ValidationError:
            continue
        if record.agent_sdk:
            return record.agent_sdk
    return None


def load_session_records(session_id: str) -> list[SessionRecord]:
    """Load all session records for a given ID across all versions.

    Core fields are typed via :class:`SessionRecord`; domain-added
    fields ride along as extras, so this function still has no
    dependency on domain-specific model classes.

    Args:
        session_id: The session identifier.

    Returns:
        List of session records, sorted by timestamp (oldest first).
    """
    sessions: list[SessionRecord] = []

    for session_dir in iter_session_dirs(session_id=session_id):
        # Only timestamp-named files are session records (save_session's
        # format); sibling artifacts like review.json share the directory.
        for filepath in sorted(session_dir.glob("[0-9]*.json")):
            try:
                data = json.loads(filepath.read_text(encoding="utf-8"))
                sessions.append(SessionRecord.model_validate(data))
            except (json.JSONDecodeError, OSError, ValidationError) as e:
                logger.warning("Failed to load session from %s: %s", filepath, e)

    sessions.sort(key=lambda s: s.timestamp)
    return sessions


def latest_session_record(session_id: str) -> SessionRecord | None:
    """Get the most recent session record for an ID.

    Args:
        session_id: The session identifier.

    Returns:
        The most recent session record, or None if no sessions exist.
    """
    sessions = load_session_records(session_id)
    return sessions[-1] if sessions else None


def update_session_metadata(
    session_id: str,
    *,
    outcome: str | None = None,
    submitted_at: str | None = None,
) -> bool:
    """Update metadata for the latest session.

    Args:
        session_id: The session identifier.
        outcome: Outcome value to set (e.g., "success", "failure").
        submitted_at: ISO timestamp when submitted.

    Returns:
        True if a session was updated, False if not found.
    """
    all_files = [
        filepath
        for session_dir in iter_session_dirs(session_id=session_id)
        for filepath in session_dir.glob("*.json")
    ]

    if not all_files:
        return False

    def file_timestamp(path: Path) -> datetime:
        """Sort key: parsed filename timestamp (non-timestamped names sort first)."""
        try:
            return parse_timestamp(path.name)
        except ValueError:
            return datetime.min

    # Latest by parsed timestamp — a lexicographic full-path sort would
    # order version directories wrong (0.10.0 < 0.9.0).
    latest_file = max(all_files, key=file_timestamp)

    try:
        data: JsonObject = json.loads(latest_file.read_text(encoding="utf-8"))

        if outcome is not None:
            data["outcome"] = outcome
        if submitted_at is not None:
            data["submitted_at"] = submitted_at

        latest_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info("Updated metadata for session %s", session_id)
        return True

    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to update session %s: %s", session_id, e)
        return False


def format_history_for_context(
    sessions: list[SessionRecord],
    *,
    max_sessions: int = 5,
    formatter: Callable[[SessionRecord], str] | None = None,
) -> str:
    """Format past sessions as context for the agent.

    Args:
        sessions: List of session records (from :func:`load_session_records`).
        max_sessions: Maximum number of sessions to include.
        formatter: Callable that formats a single session record into
            a markdown string. Uses a default formatter if ``None``.

    Returns:
        Markdown-formatted summary of past sessions.
    """
    if not sessions:
        return ""

    fmt = formatter or SessionRecord.markdown_summary

    lines = ["## Past Sessions\n"]
    for session in sessions[-max_sessions:]:
        lines.append(fmt(session))

    return "\n".join(lines)


# -- Cross-version data discovery ---------------------------------------------


def version_dirs() -> list[Path]:
    """Return all version directories under notes/traces/, sorted."""
    tp = traces_path()
    if not tp.exists():
        return []
    return sorted(d for d in tp.iterdir() if d.is_dir() and not d.name.startswith("."))


def iter_session_dirs(
    session_id: str | None = None,
    version: str | None = None,
) -> Iterator[Path]:
    """Iterate over session directories across all (or filtered) versions.

    Yields paths like: notes/traces/0.1.0/sessions/my-session/
    """
    ver_dirs = [traces_path() / version] if version else version_dirs()

    for ver_dir in ver_dirs:
        sessions_base = ver_dir / "sessions"
        if not sessions_base.exists():
            continue
        if session_id is not None:
            candidate = sessions_base / session_id
            if candidate.exists() and candidate.is_dir():
                yield candidate
        else:
            for d in sessions_base.iterdir():
                if d.is_dir():
                    yield d


def iter_output_dirs(
    task_id: str | None = None,
    version: str | None = None,
) -> Iterator[Path]:
    """Iterate over output directories across all (or filtered) versions.

    Yields paths like: notes/traces/0.1.0/outputs/my-task/
    """
    ver_dirs = [traces_path() / version] if version else version_dirs()

    for ver_dir in ver_dirs:
        outputs_base = ver_dir / "outputs"
        if not outputs_base.exists():
            continue
        if task_id is not None:
            candidate = outputs_base / task_id
            if candidate.exists() and candidate.is_dir():
                yield candidate
        else:
            for d in outputs_base.iterdir():
                if d.is_dir():
                    yield d


def iter_trace_log_files(
    session_id: str | None = None,
    version: str | None = None,
) -> Iterator[Path]:
    """Iterate reasoning log files across all (or filtered) versions."""
    ver_dirs = [traces_path() / version] if version else version_dirs()

    for ver_dir in ver_dirs:
        logs_base = ver_dir / "logs"
        if not logs_base.exists():
            continue
        if session_id is not None:
            session_logs = logs_base / session_id
            if session_logs.exists():
                yield from session_logs.glob("*.md")
        else:
            yield from logs_base.rglob("*.md")


def list_all_session_ids(version: str | None = None) -> list[str]:
    """Return all session IDs across versions, deduplicated."""
    return sorted({d.name for d in iter_session_dirs(version=version)})


# -- Version scope resolution ------------------------------------------------

MIN_VERSION_DATAPOINTS = 10

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class Semver(BaseModel):
    """A parsed X.Y.Z version."""

    major: int
    minor: int
    patch: int


def parse_semver(version: str) -> Semver | None:
    """Parse 'X.Y.Z' into a :class:`Semver`, or None if invalid."""
    m = SEMVER_RE.match(version)
    if not m:
        return None
    return Semver(major=int(m.group(1)), minor=int(m.group(2)), patch=int(m.group(3)))


class VersionScope(BaseModel):
    """A resolved version scope: the versions to include (None = all), and the
    widening warning to show, if the requested scope was too sparse."""

    versions: list[str] | None
    warning: str | None


def count_sessions_for_versions(versions: list[str]) -> int:
    """Count total session directories across a set of version directories."""
    return sum(sum(1 for _ in iter_session_dirs(version=v)) for v in versions)


def resolve_version(
    version: str | None,
    all_versions: bool = False,
    min_datapoints: int = MIN_VERSION_DATAPOINTS,
) -> VersionScope:
    """Resolve effective version scope with progressive semver fallback.

    Fallback chain: exact version → X.Y.* → X.* → all versions.
    Widens when the narrower scope has fewer than ``min_datapoints`` sessions.

    Returns ``(version_list, warning_message)``.
    ``version_list`` is ``None`` when all versions should be included.
    """
    if all_versions:
        return VersionScope(versions=None, warning=None)

    effective = version if version is not None else agent_version()
    semver = parse_semver(effective)
    available = [d.name for d in version_dirs()]

    # Level 1: exact version
    exact = [effective] if effective in available else []
    exact_count = count_sessions_for_versions(exact)
    if exact_count >= min_datapoints:
        return VersionScope(versions=exact, warning=None)

    if semver is None:
        all_count = count_sessions_for_versions(available)
        if all_count == 0:
            return VersionScope(versions=None, warning=None)
        return VersionScope(
            versions=None,
            warning=f"v{effective} has only {exact_count} sessions "
            f"(need {min_datapoints}) — including all versions",
        )

    major, minor = semver.major, semver.minor

    # Level 2: same minor (X.Y.*)
    minor_matches = [
        v
        for v in available
        if (sv := parse_semver(v)) is not None
        and sv.major == major
        and sv.minor == minor
    ]
    minor_count = count_sessions_for_versions(minor_matches)
    if minor_count >= min_datapoints:
        return VersionScope(
            versions=minor_matches,
            warning=f"v{effective} has only {exact_count} sessions "
            f"— widening to v{major}.{minor}.* ({minor_count} sessions)",
        )

    # Level 3: same major (X.*)
    major_matches = [
        v
        for v in available
        if (sv := parse_semver(v)) is not None and sv.major == major
    ]
    major_count = count_sessions_for_versions(major_matches)
    if major_count >= min_datapoints:
        return VersionScope(
            versions=major_matches,
            warning=f"v{major}.{minor}.* has only {minor_count} sessions "
            f"— widening to v{major}.* ({major_count} sessions)",
        )

    # Level 4: all versions
    all_count = count_sessions_for_versions(available)
    if all_count == 0:
        return VersionScope(versions=None, warning=None)
    return VersionScope(
        versions=None,
        warning=f"v{major}.* has only {major_count} sessions "
        f"(need {min_datapoints}) — including all versions",
    )
