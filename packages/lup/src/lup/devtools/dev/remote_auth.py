"""Remote-auth probing: can git push/PR operations reach the origin remote?

ssh-style remotes are probed with ``ssh -T`` (GitHub answers with exit 1,
GitLab with 0), https remotes via ``gh auth status``; local and
unrecognized remotes pass. Callers gate remote operations on
:func:`check_remote_auth` so a missing key or logged-out ``gh`` surfaces
as one actionable message instead of a failed push.
"""

from urllib.parse import urlparse

import sh
import typer
from pydantic import BaseModel

from lup.devtools.utils import gh, git


class RemoteRef(BaseModel):
    """A git remote reduced to what auth probing needs."""

    scheme: str
    destination: str


def parse_remote(remote_url: str) -> RemoteRef | None:
    """Parse a git remote into (scheme, destination) for auth probing.

    URL forms (https://host/org/repo, ssh://git@host/org/repo) are parsed
    with urllib. SCP form ``[user@]host:path`` is git-equivalent to
    ``ssh://[user@]host/path`` and is detected by a colon preceding the
    first slash; it is normalized to ``ssh://`` and parsed the same way.
    Returns None for local paths and unrecognized remotes.
    """
    parsed = urlparse(remote_url)
    if not parsed.scheme:
        colon, slash = remote_url.find(":"), remote_url.find("/")
        if colon == -1 or (slash != -1 and slash < colon):
            return None
        normalized = "ssh://" + remote_url[:colon] + "/" + remote_url[colon + 1 :]
        parsed = urlparse(normalized)
    if parsed.scheme and parsed.hostname:
        user_prefix = f"{parsed.username}@" if parsed.username else ""
        return RemoteRef(
            scheme=parsed.scheme, destination=f"{user_prefix}{parsed.hostname}"
        )
    return None


def probe_ssh_auth(destination: str, remote_url: str) -> bool:
    """Probe SSH auth with ``ssh -T``. GitHub answers with exit 1, GitLab with 0."""
    try:
        ssh = sh.Command("ssh").bake(_tty_out=False)
    except sh.CommandNotFound:
        typer.echo("ssh binary not found; skipping remote checks", err=True)
        return False
    probe = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-T", destination]
    try:
        ssh(*probe, _ok_code=[0, 1])
        return True
    except sh.ErrorReturnCode:
        identity = ""
        lines = str(ssh("-G", destination, _ok_code=[0])).splitlines()
        for line in lines:
            if line.startswith("identityfile "):
                identity = line.removeprefix("identityfile ")
                break
        msg = f"SSH authentication failed for remote '{remote_url}'."
        if identity:
            msg += f"\nLoad the key with:  ssh-add {identity}"
        typer.echo(msg, err=True)
        return False


def probe_gh_auth(remote_url: str) -> bool:
    """Verify GitHub CLI auth, used for https remotes where ssh -T means nothing."""
    try:
        gh("auth", "status", _ok_code=[0])
        return True
    except (sh.ErrorReturnCode, sh.CommandNotFound):
        typer.echo(
            f"gh auth failed for remote '{remote_url}'. Run: gh auth login",
            err=True,
        )
        return False


def check_remote_auth() -> bool:
    """Verify auth for the origin remote. Returns True if remote ops can proceed.

    ssh-style remotes are probed with ``ssh -T``; https remotes are verified
    via ``gh auth status``. Local and unrecognized remotes pass.
    """
    remote_url = git.out("remote", "get-url", "origin")
    remote = parse_remote(remote_url)
    if remote is None:
        return True
    match remote.scheme:
        case "ssh" | "git":
            return probe_ssh_auth(remote.destination, remote_url)
        case "http" | "https":
            return probe_gh_auth(remote_url)
        case _:
            return True
