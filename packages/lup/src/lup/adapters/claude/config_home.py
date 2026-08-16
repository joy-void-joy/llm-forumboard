# lup: ignore[constant-declaration]
# Every constant here is what Claude Code itself calls one of these things, so
# each docstring below states the value's own provenance and a caller passing
# a different one would be reading a home no runtime writes.
"""Claude Code's configuration document, and a private one per workspace.

Where that document sits is Claude's own rule rather than a shape any
portable contract carries: with ``CLAUDE_CONFIG_DIR`` set it is
``.config.json`` inside the named directory, and with the variable unset it
is ``~/.claude.json`` beside the home instead of in it. Both answers matter,
because a session whose document was derived from the wrong one starts
holding no record of the projects it is trusted in — which is the same
degraded state as having no document at all.

Trust is recorded here too, and only for a workspace lup itself created.
Invoking a run against a repository is an explicit act of trust by whoever
ran it; lup extends exactly that to the checkouts it makes of that same
repository, and to nothing else it happens to open a session in.
"""

import json
from pathlib import Path

from pydantic import BaseModel

from lup.adapters.claude.login import CLAUDE_CONFIG_DIR, CLAUDE_LOGIN
from lup.channels.models import write_atomic
from lup.runtime.session_home import SessionHomeLayout, SessionHomes
from lup.types import EnvVars, JsonObject

CLAUDE_CONFIG_FILE = ".config.json"
"""What Claude Code calls its configuration document inside a config home."""

CLAUDE_HOME_DOCUMENT = ".claude.json"
"""What it calls the same document when no config home is named: a file in
the user's home directory rather than an entry inside ``~/.claude``."""

CLAUDE_HOME_DIR = ".claude"
"""The configuration home a session falls back to, named in the user's home."""

WORKSPACE_SETTINGS = "settings.json"
"""A workspace's own settings, inside the directory Claude reads it from."""

CLAUDE_BACKUP_DIR = "backups"
"""Where Claude Code copies a document it could not read, beside the home."""

TRUST_FIELD = "hasTrustDialogAccepted"
"""The field a project entry carries once its workspace has been trusted."""

CLAUDE_HOME_LAYOUT = SessionHomeLayout(private_files=[CLAUDE_CONFIG_FILE])
"""Claude keeps one document, and it is the one a startup rewrites."""


class ClaudeConfigUnreadable(RuntimeError):
    """A configuration document exists but does not parse as one.

    Raised rather than treated as absent: an unreadable document holds every
    project the profile has been trusted in, so continuing from an empty one
    would start each session with the repository's declared permissions
    silently dropped — the exact degradation a caller reads this to avoid.
    """


def default_config_home() -> Path:
    """The configuration home a session opens under when none is named."""
    return Path.home() / CLAUDE_HOME_DIR


class ClaudeConfigHome(BaseModel, frozen=True):
    """One selected configuration home, and the document it reads."""

    directory: Path
    document: Path

    def configuration_fault(self) -> str | None:
        """Why no session can be opened under this home yet, if anything.

        Every private home a run derives is seeded from this one document, so a
        run that cannot read it cannot open a session anywhere — a fact about
        the environment rather than about any one piece of work. Answered once
        and up front, it is a single message before anything is leased, instead
        of the same fault rediscovered by every session that races to start.
        """
        try:
            load_document(self.document)
        except ClaudeConfigUnreadable as error:
            return f"{error}. {restoration_advice(self.directory)}"
        return None


def selected_config_home(environment: EnvVars) -> ClaudeConfigHome:
    """The home and document a session opened under this environment reads."""
    named = environment.get(  # lup: ignore[dict-get] — open env-var map
        CLAUDE_CONFIG_DIR
    )
    if named is None:
        return ClaudeConfigHome(
            directory=default_config_home(),
            document=Path.home() / CLAUDE_HOME_DOCUMENT,
        )
    directory = Path(named).expanduser()
    return ClaudeConfigHome(
        directory=directory, document=directory / CLAUDE_CONFIG_FILE
    )


def load_document(path: Path) -> JsonObject:
    """Read one Claude JSON document, or answer that there is not one yet."""
    if not path.exists():
        return {}
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ClaudeConfigUnreadable(
            f"Claude configuration at {path} does not parse: {error}"
        ) from error
    if not isinstance(decoded, dict):
        raise ClaudeConfigUnreadable(
            f"Claude configuration at {path} is not a JSON object"
        )
    return decoded


def save_document(path: Path, document: JsonObject) -> None:
    """Write one Claude JSON document so no reader can catch it half-written."""
    write_atomic(path, json.dumps(document).encode("utf-8"))


def project_entries(document: JsonObject) -> JsonObject:
    """The per-project section of one document, empty when it carries none."""
    projects = document.get(  # lup: ignore[dict-get] — Claude's own document
        "projects"
    )
    return dict(projects) if isinstance(projects, dict) else {}


def project_entry(document: JsonObject, workspace: Path) -> JsonObject:
    """One workspace's entry in a document, empty when it has none yet."""
    found = project_entries(document).get(  # lup: ignore[dict-get] — path-keyed
        str(workspace.resolve())
    )
    return dict(found) if isinstance(found, dict) else {}


def restorable_backups(directory: Path) -> list[Path]:
    """Every backup of one home's document that could actually restore it.

    Claude Code answers a document it cannot parse with a hint naming the
    backup it just wrote, and the backup it wrote for a truncated document
    was zero bytes — so following the hint replaces a since-healed
    configuration with an empty one. What makes a backup worth restoring is
    not that it exists but that it parses and still carries the project
    entries trust and permissions live in, which is what is answered here.

    Newest first, by the time the file was written rather than by the
    timestamp in its name, so a caller naming one names the least lost.
    """

    def restores(path: Path) -> bool:
        """Whether one backup parses and carries what a restore would need."""
        try:
            return bool(project_entries(load_document(path)))
        except ClaudeConfigUnreadable:
            return False

    backups = directory / CLAUDE_BACKUP_DIR
    if not backups.is_dir():
        return []
    return sorted(
        (path for path in backups.iterdir() if path.is_file() and restores(path)),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def restoration_advice(directory: Path) -> str:
    """How to get one unreadable configuration home back, in this state."""
    restorable = restorable_backups(directory)
    if not restorable:
        return (
            f"No backup under {directory / CLAUDE_BACKUP_DIR} both parses and "
            "carries project entries, so none of them can restore it: move the "
            "document aside and open one session to write a fresh one, which "
            "starts out trusting nothing and knowing no projects"
        )
    return (
        f"Restore it from {restorable[0]}, the most recently written backup "
        "that both parses and carries project entries"
    )


def trusts(document: JsonObject, workspace: Path) -> bool:
    """Whether one document already records that workspace as trusted."""
    entry = project_entry(document, workspace)
    return entry.get(TRUST_FIELD) is True  # lup: ignore[dict-get] — vendor field


def record_trust(path: Path, workspace: Path) -> None:
    """Record one workspace as trusted, in a document lup owns.

    Trust has no project-level home to write to. Claude Code keeps it in the
    user-level configuration document and offers nowhere else, so this is a
    deliberate, narrow exception to the convention that lup writes harness
    settings project-level and never user-level. It stays narrow in two
    ways: the document written is the private per-session one derived under
    the profile rather than the operator's own, and the only workspace ever
    recorded is a worktree lup created for this run.
    """
    document = load_document(path)
    entries = project_entries(document)
    entry = project_entry(document, workspace)
    entry[TRUST_FIELD] = True
    entries[str(workspace.resolve())] = entry
    document["projects"] = entries
    save_document(path, document)


def declared_allowances(workspace: Path, settings_dir: str = CLAUDE_HOME_DIR) -> int:
    """How many ``permissions.allow`` entries a workspace declares for itself.

    Counted rather than listed because a caller reports a loss, not a
    policy: what a reader needs to know is that entries exist and are being
    ignored, and the workspace's own settings file is where to read them.
    """
    match load_document(workspace / settings_dir / WORKSPACE_SETTINGS):
        case {"permissions": {"allow": [*entries]}}:
            return len(entries)
    return 0


def untrusted_degradation(workspace: Path, document: Path) -> str | None:
    """What a session opened in this workspace would silently lose, if anything.

    An untrusted workspace does not fail: Claude drops the repository's
    declared permissions, warns once into that session's own stderr, and
    carries on. A run made of many sessions therefore reports a changed
    permission posture only as noise interleaved with progress, so the fact
    is derived here for a caller that can refuse to open the session at all.
    """
    declared = declared_allowances(workspace)
    if declared == 0 or trusts(load_document(document), workspace):
        return None
    return (
        f"{workspace} is not a trusted workspace, so the {declared} "
        "permissions.allow entries its .claude/settings.json declares are "
        "ignored by every session opened there. Sessions still run, under a "
        "different permission posture than the repository declares."
    )


def workspace_config_environment(
    environment: EnvVars,
    workspace: Path,
    *,
    trust: bool = False,
    layout: SessionHomeLayout = CLAUDE_HOME_LAYOUT,
) -> EnvVars:
    """Point the sessions of one workspace at a configuration home of their own.

    Returns the variables to merge over ``environment``. The home is derived
    under whichever one that environment already selects, so a profile
    naming the account still decides the account: this narrows what a
    session writes, never which login it writes under.
    """
    selected = selected_config_home(environment)
    home = SessionHomes(selected.directory, layout).derive(workspace)
    document = home / CLAUDE_CONFIG_FILE
    if not document.exists():
        save_document(document, load_document(selected.document))
    if trust:
        record_trust(document, workspace)
    return CLAUDE_LOGIN.environment(home)
