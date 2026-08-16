"""Pre-configured shell commands and output helpers for devtools scripts."""

import json
import logging
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import IO, Annotated, Literal, TypedDict, Unpack

import sh
import typer
from pydantic import BaseModel

from lup.gitlocks import admin_dirs, diagnose_git_admin, inspect_git_admin

logger = logging.getLogger(__name__)

# sh declares these only as stub-private aliases, which pyright refuses to
# import, so they are mirrored here. Each is narrowed where sh wrote `Any` —
# narrower still satisfies sh, and this repository does not spell `Any`.
type ShIn = str | bytes | IO[str] | IO[bytes] | sh.RunningCommand | Iterable[str]
type ShOut = str | int | IO[str] | IO[bytes] | Callable[..., object]
type ShTarget = bool | Literal["out", "err"]
type ShOkCode = int | list[int] | tuple[int, ...]
type ShDone = Callable[[sh.RunningCommand, bool, int], None]

# Git abbreviates object names to this many hex characters by default
# (`core.abbrev`), the same width as `git log --oneline`. Short shas are for
# human-readable display only; never parse or compare against them.
SHORT_SHA_LENGTH = 7


class ShKwargs(TypedDict, total=False):
    """Every special keyword sh accepts, as sh's own stub declares them.

    `LazyCommand` is a proxy, so it must pass through whatever the command it
    stands in for accepts. `**kwargs: object` cannot: `object` is assignable
    to none of the concrete parameters sh declares, so unpacking it fails
    against every one of them at once. Naming them types the proxy as wide as
    the thing proxied rather than as wide as today's call sites.

    Two of sh's forty-five are missing, both refused by `tuple-shape`, which
    admits no suppression: `_tty_size` is a `tuple[int, int]`, and
    `_arg_preprocess` returns a fixed pair. Reach for `sh.Command` directly
    if either is ever wanted.
    """

    _fg: bool
    _bg: bool
    _bg_exc: bool
    _with: bool
    _in: ShIn | None
    _out: ShOut | None
    _err: ShOut | None
    _err_to_out: bool | None
    _in_bufsize: int
    _out_bufsize: int
    _err_bufsize: int
    _internal_bufsize: int
    _env: dict[str, str] | None  # lup: ignore[dict-str-payload] — open names
    _piped: ShTarget | None
    _iter: ShTarget | None
    _iter_noblock: ShTarget | None
    _iter_poll_time: float
    _ok_code: ShOkCode
    _cwd: str | None
    _long_sep: str | None
    _long_prefix: str
    _tty_in: bool
    _tty_out: bool
    _unify_ttys: bool
    _encoding: str
    _decode_errors: str
    _timeout: float | None
    _timeout_signal: int
    _no_out: bool
    _no_err: bool
    _no_pipe: bool
    _tee: ShTarget | None
    _done: ShDone | None
    _truncate_exc: bool
    _preexec_fn: Callable[[], None] | None
    _uid: int | None
    _new_session: bool
    _new_group: bool
    _log_msg: Callable[..., str] | None
    _close_fds: bool
    _pass_fds: set[int]  # lup: ignore[set-shape] — sh's own parameter type
    _return_cmd: bool
    _async: bool


class LazyCommand:
    """Shell command that resolves its binary on first use.

    ``sh.Command`` raises ``CommandNotFound`` at construction time, so a
    module-level command for a missing binary (e.g. ``gh``) would crash
    every CLI invocation at import — including ``--help``. Resolution is
    deferred to the first call or sub-command attribute access instead.
    """

    def __init__(self, name: str, *bake_args: str, tty_out: bool = True) -> None:
        self.name = name
        self.bake_args = bake_args
        self.tty_out = tty_out
        self.resolved: sh.Command | None = None

    def resolve(self) -> sh.Command:
        if self.resolved is None:
            command = sh.Command(self.name)
            if self.bake_args or not self.tty_out:
                command = command.bake(*self.bake_args, _tty_out=self.tty_out)
            self.resolved = command
        return self.resolved

    def __call__(
        self,
        *args: str,
        **kwargs: Unpack[ShKwargs],
    ) -> sh.RunningCommand | str | None:
        return self.resolve()(*args, **kwargs)

    def __getattr__(self, attr: str) -> sh.Command:
        return getattr(self.resolve(), attr)

    def out(
        self,
        *args: str,
        **kwargs: Unpack[ShKwargs],
    ) -> str:
        """The command's stdout with its trailing-newline framing removed.

        The single-value boundary: CLIs frame every output — `git rev-parse`,
        `gh pr list --json` — with a trailing newline, and ``sh`` exposes no
        trimmed accessor, so value reads go through here instead of a per-site
        `str(...).strip()`. Not for column-significant text (`status
        --porcelain` leading columns): use :meth:`lines`.
        """
        return str(self(*args, **kwargs)).strip()

    def lines(
        self,
        *args: str,
        **kwargs: Unpack[ShKwargs],
    ) -> list[str]:
        """The command's stdout as a list of lines.

        For line-oriented output (`--format=` templates, `--porcelain`
        records): the line breaks are consumed by ``splitlines``, while
        leading columns and blank separator lines are preserved exactly.
        """
        return str(self(*args, **kwargs)).splitlines()


git = LazyCommand("git", "--no-pager", "-c", "color.ui=never", tty_out=False)
gh = LazyCommand("gh", tty_out=False)
uv = LazyCommand("uv")


def slug_from_remote(url: str) -> str:
    """The ``owner/name`` a remote names, empty when it names none.

    Read here rather than left to `gh` to infer, because a remote written
    through an SSH alias — ``<alias>:owner/name.git``, whose host ssh resolves
    from its own config — names no host `gh` recognizes, and every query then
    fails with "no known GitHub host" as though the repository were
    unreachable.

    The pair is the last two path segments in every shape a remote is written
    in, and nothing before a colon is ever one of them — which is what reads
    the scp-like form (``git@host:owner/name``) that is not a URL and has no
    parser in the standard library.
    """
    trimmed = url.removesuffix(".git")
    located = trimmed.rpartition(":")[2]  # lup: ignore[string-split] — no parser
    named = PurePosixPath(located).parts
    return "/".join(named[-2:]) if len(named) >= 2 else ""


def repository_slug() -> str:
    """The ``owner/name`` this checkout answers to, empty when unreadable."""
    try:
        return slug_from_remote(git.out("remote", "get-url", "origin"))
    except sh.ErrorReturnCode as error:
        logger.warning("no origin remote to read a slug from: %s", decode_stderr(error))
        return ""


def repository_arguments() -> list[str]:
    """The ``--repo`` a `gh` query needs, or nothing where none is readable.

    Every `gh` subcommand infers its repository from the origin remote unless
    told, and that inference is what an alias defeats. Naming it once here is
    what stops a query depending on the spelling a checkout happens to use.

    Empty where no slug is readable, which is a project with no forge rather
    than a forge that could not be reached. The two want opposite answers from
    a caller — absence is a fact in the first and unknown in the second — so
    they are not collapsed here.
    """
    slug = repository_slug()
    return ["--repo", slug] if slug else []


def decode_stderr(e: sh.ErrorReturnCode) -> str:
    """Decode a failed ``sh`` command's stderr to trimmed text.

    ``sh`` captures stderr as raw ``bytes`` and exposes no decoded accessor,
    so callers that want a readable message decode it here; the trailing
    newline the failing tool printed with is framing, not message.
    """
    raw = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)
    return raw.strip()


def git_admin_dirs(cwd: Path | None = None) -> list[Path]:
    """Every admin directory a checkout writes its configuration through.

    A worktree's own ``.git`` is a file naming its admin directory and a bare
    clone has no ``.git`` at all, so the layout is asked of git rather than
    reconstructed from the checkout — and the ask still answers when every
    write is being refused.
    """
    root = cwd if cwd is not None else Path.cwd()
    return admin_dirs(
        root, git.lines("rev-parse", "--git-dir", "--git-common-dir", _cwd=str(root))
    )


def config_lock_diagnosis(cwd: Path | None = None) -> str:
    """Why git config writes cannot run here, empty when they can."""
    try:
        admins = git_admin_dirs(cwd)
    except sh.ErrorReturnCode:
        # No repository to diagnose: whatever the caller's git failure was,
        # the lock protocol is not what it tripped on.
        return ""
    return diagnose_git_admin(admins)


def clear_stale_config_locks(cwd: Path | None = None) -> Iterator[str]:
    """Remove every lock nothing is holding, naming each one removed.

    A confinement manufactures this debris — a sandboxed git dies mid-write
    and its lock outlives it on the host — so the run that can reach the
    filesystem is the one that has to clear it, and the next unconfined run
    is not sent hunting for a failure the previous one left. Nothing that
    declines removal is touched.
    """
    try:
        admins = git_admin_dirs(cwd)
    except sh.ErrorReturnCode:
        return
    for admin in admins:
        for obstruction in inspect_git_admin(admin):
            cleared = obstruction.clear()
            if cleared:
                yield cleared


def refuse_blocked_config_writes(cwd: Path | None = None) -> None:
    """Clear what is removable, and stop before a config write that still cannot run."""
    for cleared in clear_stale_config_locks(cwd):
        typer.echo(cleared)
    diagnosis = config_lock_diagnosis(cwd)
    if diagnosis:
        typer.echo(diagnosis, err=True)
        raise typer.Exit(1)


def copy_to_clipboard(text: str) -> bool:
    """Copy text to the system clipboard. Returns True on success.

    Best-effort and cross-platform: tries each platform's clipboard tool
    in turn (Linux ``xclip``/``xsel``, macOS ``pbcopy``, Windows ``clip``)
    and stops at the first one that is installed and succeeds. Returns
    False when none is available so callers can fall back to printing the
    text for manual copying.
    """
    for command, args in (
        ("xclip", ["-selection", "clipboard"]),
        ("xsel", ["--clipboard", "--input"]),
        ("pbcopy", []),
        ("clip", []),
    ):
        try:
            sh.Command(command)(*args, _in=text)
            return True
        except (sh.ErrorReturnCode, sh.CommandNotFound):
            continue
    return False


def output_json(
    data: object,  # lup: ignore[bare-object] — pretty-printer: any serializable payload
) -> None:
    if isinstance(data, BaseModel):
        typer.echo(data.model_dump_json(indent=2))
    else:
        typer.echo(json.dumps(data, indent=2))


def short_sha(sha: str, length: int = SHORT_SHA_LENGTH) -> str:
    """Abbreviate a git object name for human-readable display.

    The single source of truth for how shas are shortened across devtools so
    every table and message uses one consistent width. Returns shorter input
    unchanged so already-abbreviated shas pass through.
    """
    return sha[:length]


def format_table(
    headers: Sequence[str],
    rows: Iterable[Sequence[str]],
    aligns: Sequence[Literal["left", "right"]] | None = None,
) -> str:
    """Render rows as a column-aligned table sized to its own contents.

    Column widths come from the widest cell in each column, so no caller has
    to guess a fixed width that later clips real data. ``aligns`` picks left
    (default) or right justification per column; a trailing column gets no
    padding so variable-length tails (paths, messages) aren't padded out.
    """
    materialized = [list(row) for row in rows]
    widths = [len(h) for h in headers]
    for row in materialized:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def render(cells: Sequence[str]) -> str:
        last = len(cells) - 1

        def pad(i: int, cell: str) -> str:
            align = aligns[i] if aligns else "left"
            if align == "right":
                return f"{cell:>{widths[i]}}"
            return cell if i == last else f"{cell:<{widths[i]}}"

        return " ".join(pad(i, cell) for i, cell in enumerate(cells))

    header_line = render(headers)
    lines = [header_line, "-" * len(header_line)]
    lines.extend(render(row) for row in materialized)
    return "\n".join(lines)


VERSION_OPT = Annotated[
    str | None,
    typer.Option("--version", "-v", help="Agent version (default: current)"),
]
ALL_VERSIONS_OPT = Annotated[
    bool,
    typer.Option("--all-versions", help="Include all versions"),
]
JSON_OPT = Annotated[
    bool,
    typer.Option("--json", help="Output as JSON"),
]
