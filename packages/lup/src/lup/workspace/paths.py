# lup: ignore[import-re, re-call]
# The session-timestamp shape check is a pattern over an id format this module
# itself defines — regex is the tool, so those rules are opted out file-wide.
"""Centralized path constants and helpers for agent session data.

Pure path layout — where things go on disk. No data discovery or disk
iteration; see :mod:`lup.workspace.history` for cross-version queries.

Paths auto-detect the project root (the nearest enclosing ``pyproject.toml``
declaring ``[tool.lup]``, from the working directory) on first access, but
can be overridden via :func:`configure`::

    from lup.workspace.paths import configure
    configure(root=Path("/my/project"), notes_dir=Path("/my/data/notes"))

Resolution is lazy and cached: importing this module never touches the
filesystem, so ``lup`` is importable outside a ``[tool.lup]`` project
(e.g. when pip-installed). Auto-detection runs on the first accessor
call; calling :func:`configure` first skips it entirely.

Layout:
    notes/traces/<version>/sessions/<session_id>/<timestamp>.json
    notes/traces/<version>/outputs/<task_id>/<timestamp>/
    notes/traces/<version>/logs/<session_id>/<timestamp>.md
    notes/feedback_loop/

Examples:
    Override paths for testing (the root does not need a pyproject.toml;
    the version falls back to "0.0.0" unless given explicitly)::

        >>> from lup.workspace.paths import configure, sessions_dir, project_root
        >>> configure(root=Path("/tmp/test-project"))
        >>> project_root()
        PosixPath('/tmp/test-project')
        >>> sessions_dir()
        PosixPath('/tmp/test-project/notes/traces/0.0.0/sessions')

    Check if a path is within allowed directories::

        >>> path_is_under("/data/sessions/12345/out.json", [Path("/data/sessions")])
        True
        >>> path_is_under("/etc/passwd", [Path("/data/sessions")])
        False

    Extract the directory prefix from a glob pattern::

        >>> extract_glob_dir("/tmp/foo/**/*.py")
        '/tmp/foo'
        >>> extract_glob_dir("**/*.py")
        ''
"""

import functools
import logging
import re
from datetime import datetime
from pathlib import Path

import tomllib
from pydantic import BaseModel

from lup.types import JsonObject

logger = logging.getLogger(__name__)


def manifest_table(pyproject: Path) -> JsonObject | None:
    """Parse one ``pyproject.toml``, or answer that it declares nothing.

    A manifest a merge stopped inside holds conflict markers and does not
    parse. Every path this module resolves reads one, and the workflow for
    repairing a conflicted manifest is reached through this same package, so
    a decode error escaping here takes the whole toolchain down at exactly
    the moment it is needed. A file that cannot be read declares nothing,
    which is the answer a missing one already gives.
    """
    if not pyproject.exists():
        return None
    try:
        with pyproject.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError:
        logger.warning("%s does not parse, so it declares nothing", pyproject)
        return None


def declares_lup(table: JsonObject | None) -> bool:
    """Whether a parsed manifest carries the ``[tool.lup]`` table."""
    match table:
        case {"tool": {"lup": _}}:
            return True
    return False


def is_template_scaffold(root: Path) -> bool:
    """Whether this repository is still the unadopted scaffold itself.

    ``[tool.lup] template = true`` says so, and initialization clears it in the
    same rewrite that renames the package. The flag is what lets one rule read
    two ways: a customization marker is inventory in the scaffold that ships
    it and outstanding work in the repository that adopted it, and nothing
    else in the tree distinguishes those two readings. Absent or false — the
    state of every downstream repository — answers false, so a repo that never
    carried the flag is treated as adopted rather than as a scaffold.
    """
    match manifest_table(root / "pyproject.toml"):
        case {"tool": {"lup": {"template": True}}}:
            return True
    return False


def declared_project_root(start: Path) -> Path | None:
    """The nearest enclosing directory whose pyproject declares ``[tool.lup]``.

    A manifest that does not parse cannot be asked what it declares, and it is
    the project's own manifest that a merge conflicts. Refusing it there does
    not make the answer safer, it only moves the failure: the walk continues
    past the project, finds no other ``[tool.lup]``, and the toolchain that
    exists to repair the conflict stops importing. So an unreadable manifest
    answers for the root it sits in — it is a manifest, in a directory, and
    while it is being repaired it is the only candidate in reach.

    It answers last, though. A manifest that does say ``[tool.lup]`` is a
    better answer wherever it sits, so the whole chain is read before an
    unreadable one is taken: a broken manifest in a subpackage must not
    shadow the project root above it.
    """
    tables = [
        (parent, manifest_table(parent / "pyproject.toml"))
        for parent in [start, *start.parents]
        if (parent / "pyproject.toml").exists()
    ]
    for parent, table in tables:
        if declares_lup(table):
            return parent
    return next((parent for parent, table in tables if table is None), None)


def find_project_root() -> Path:
    """Find the project root enclosing this installation of the library.

    Only meaningful for a source checkout, where the library lives inside the
    project it serves. Installed as a dependency it answers about the
    environment rather than the project, so :func:`resolve_state` asks the
    working directory first and reaches this only as a fallback.
    """
    root = declared_project_root(Path(__file__).resolve().parent)
    if root is None:
        raise RuntimeError(
            "Could not find project root (no pyproject.toml with [tool.lup] found)"
        )
    return root


@functools.cache
def find_nearest_pyproject() -> Path | None:
    """Find the nearest directory holding a pyproject.toml, walking up from cwd.

    Unlike :func:`find_project_root` — which anchors on this installation's
    ``[tool.lup]`` pyproject and raises when absent — this matches any
    project's ``pyproject.toml`` and returns ``None`` when there is none.
    """
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return None


def read_agent_version(root: Path) -> str:
    """Read agent_version from [tool.lup] in pyproject.toml.

    Returns "0.0.0" when the file or the [tool.lup] table is absent, so
    :func:`configure` accepts roots that are not lup projects (e.g. test
    fixtures or scratch directories). A manifest that does not parse is
    absent for this purpose too — :func:`resolve_state` reads the version
    the moment it has a root, so a conflicted manifest would otherwise take
    down every path the toolchain resolves while repairing it.
    """
    match manifest_table(root / "pyproject.toml"):
        case {"tool": {"lup": {"agent_version": str(version)}}}:
            return version
    return "0.0.0"


def read_project_name(root: Path) -> str:
    """Read the distribution name from [project] in pyproject.toml.

    Returns "lup" when the file or the [project] table is absent — or when
    the manifest does not parse — so roots that are not Python projects, and
    projects whose manifest a merge stopped inside, still yield a usable
    identifier. Callers that need a portable declaration name validate
    through ``NativeName``, which rejects a distribution name that is not
    already one.
    """
    match manifest_table(root / "pyproject.toml"):
        case {"project": {"name": str(name)}}:
            return name.lower()
    return "lup"


# -- Mutable path state -------------------------------------------------------
# Resolved lazily on first accessor call; overridable via configure().
# Internal — read it through the accessor functions, never import it by name.


class PathConfig(BaseModel):
    """Resolved path state: project root, agent version, base directories."""

    root: Path
    version: str
    notes_dir: Path
    logs_dir: Path


class PathState(BaseModel):
    """Holder for the resolved path configuration, mutated in place.

    Accessors and :func:`configure` share this one instance and assign
    its ``config`` attribute instead of rebinding a module global.
    """

    config: PathConfig | None = None


state = PathState()


def resolve_state() -> PathConfig:
    """Return the cached path state, auto-detecting the project root on first use.

    The working directory decides, because that is what names the project a
    command is being run against. Where the library happens to be installed
    answers a different question — a shared virtualenv, a container image, or
    ``UV_PROJECT_ENVIRONMENT`` all put it outside the project entirely — and
    agrees with the working directory only in a source checkout, which is the
    case where it stays a usable fallback.
    """
    config = state.config
    if config is None:
        root = declared_project_root(Path.cwd()) or find_project_root()
        config = PathConfig(
            root=root,
            version=read_agent_version(root),
            notes_dir=root / "notes",
            logs_dir=root / "logs",
        )
        state.config = config
    return config


def configure(
    *,
    root: Path | None = None,
    notes_dir: Path | None = None,
    logs_dir: Path | None = None,
    version: str | None = None,
) -> None:
    """Override auto-detected paths.

    Call before any session operations. All derived paths
    (``traces_path``, ``feedback_path``, etc.)
    update automatically since they read from this state.

    ``pyproject.toml`` is consulted only when ``root`` is given without
    ``version``, and even then it does not have to exist — a missing
    file or missing ``[tool.lup]`` table falls back to version "0.0.0".

    Args:
        root: Project root directory. Resets ``notes_dir``,
            ``logs_dir``, and ``version`` to values derived from
            the new root unless they are also specified.
        notes_dir: Override notes directory independently.
        logs_dir: Override runtime logs directory independently.
        version: Override agent version (read from [tool.lup] by default).
    """
    if root is not None:
        state.config = PathConfig(
            root=root,
            version=version if version is not None else read_agent_version(root),
            notes_dir=notes_dir if notes_dir is not None else root / "notes",
            logs_dir=logs_dir if logs_dir is not None else root / "logs",
        )
        return

    if notes_dir is None and logs_dir is None and version is None:
        return

    current = resolve_state()
    state.config = PathConfig(
        root=current.root,
        version=version if version is not None else current.version,
        notes_dir=notes_dir if notes_dir is not None else current.notes_dir,
        logs_dir=logs_dir if logs_dir is not None else current.logs_dir,
    )


# -- Public path accessors ----------------------------------------------------


def project_root() -> Path:
    """Return the project root directory."""
    return resolve_state().root


def agent_version() -> str:
    """Return the agent version ([tool.lup] in pyproject.toml unless overridden)."""
    return resolve_state().version


def notes_path() -> Path:
    """Return the notes directory (``<root>/notes`` by default)."""
    return resolve_state().notes_dir


def runtime_logs_path() -> Path:
    """Return the runtime logs directory (``<root>/logs`` by default)."""
    return resolve_state().logs_dir


def traces_path() -> Path:
    """Return ``notes/traces/``."""
    return notes_path() / "traces"


def feedback_path() -> Path:
    """Return ``notes/feedback_loop/``."""
    return notes_path() / "feedback_loop"


# -- Timestamp helpers --------------------------------------------------------

# lup: ignore[constant-declaration] — the stamp already written into every
# session directory on disk, which a reader must parse exactly as written
TIMESTAMP_FMT = "%Y%m%d_%H%M%S"
TIMESTAMP_RE = re.compile(r"\d{8}_\d{6}")


def parse_timestamp(name: str) -> datetime:
    """Parse the last YYYYMMDD_HHMMSS occurrence from a filename or string."""
    matches = TIMESTAMP_RE.findall(Path(name).stem)
    if not matches:
        raise ValueError(f"No YYYYMMDD_HHMMSS timestamp found in: {name}")
    return datetime.strptime(matches[-1], TIMESTAMP_FMT)


# -- Write paths (version-specific) ------------------------------------------


def sessions_dir(version: str | None = None) -> Path:
    """Directory for session JSONs: notes/traces/<version>/sessions/"""
    return traces_path() / (version or agent_version()) / "sessions"


def outputs_dir(version: str | None = None) -> Path:
    """Directory for agent outputs: notes/traces/<version>/outputs/"""
    return traces_path() / (version or agent_version()) / "outputs"


def trace_logs_dir(version: str | None = None) -> Path:
    """Directory for reasoning logs: notes/traces/<version>/logs/"""
    return traces_path() / (version or agent_version()) / "logs"


# -- Path utilities -----------------------------------------------------------


def extract_glob_dir(pattern: str) -> str:
    """Extract the directory prefix from a glob pattern.

    Strips everything from the first glob wildcard character onward,
    returning the longest non-glob directory prefix.

    Used by permission hooks to validate Glob tool calls where the
    agent puts the full path in the ``pattern`` parameter instead of
    using the separate ``path`` parameter.

    Examples:
        >>> extract_glob_dir("/tmp/foo/**/*.py")
        '/tmp/foo'
        >>> extract_glob_dir("**/*.py")
        ''
        >>> extract_glob_dir("/tmp/foo/bar")
        '/tmp/foo/bar'
    """
    for i, c in enumerate(pattern):
        if c in "*?[":
            return pattern[:i].rstrip("/")  # lup: ignore[string-strip] — glob prefix
    return pattern


def path_is_under(file_path: str | Path, allowed_dirs: list[Path]) -> bool:
    """Check if a file path is under one of the allowed directories.

    Used by permission hooks to enforce RW/RO access.

    Args:
        file_path: Path to check.
        allowed_dirs: List of allowed parent directories.

    Returns:
        True if the path is under one of the allowed directories.
    """
    try:
        path = Path(file_path).resolve()
    except (OSError, ValueError):
        return False

    for allowed in allowed_dirs:
        try:
            path.relative_to(allowed.resolve())
            return True
        except ValueError:
            continue
    return False
