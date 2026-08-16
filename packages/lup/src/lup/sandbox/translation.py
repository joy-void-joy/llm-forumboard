"""Naming one path on the other side of the sandbox boundary.

The container and the host name the same bytes differently, and the mapping is
partial in both directions: ``/workspace`` is a Docker volume with no host path
at all, and most of the host tree is mounted nowhere. An agent holding tools on
both sides cannot see from a path which side it belongs to, so it reads on the
host what only exists in the container, or writes into the container what it
then cannot open. Telling it the topology does not fix this — the sandbox's own
tool description already carries the whole table, and the confusion survives it.

:class:`MountTopology` answers the question instead of documenting it, from the
same mount table the container is built from, so an answer cannot drift from
what is actually mounted. Nothing here imports Docker: the permission
dispatcher runs as a standalone script and needs these answers too.

A crossing that exists is not automatically a crossing that is allowed, which
is why :attr:`Translation.writable` is reported separately. A read-only source
root translates to a host path the host filesystem would happily let anyone
write — the mount's declared mode is the only record that writing it was meant
to be refused.
"""

from pathlib import PurePosixPath

from pydantic import BaseModel

from lup.sandbox.models import Mount


class Translation(BaseModel):
    """One path, as the far side of the boundary would name it.

    ``resolved`` is ``None`` when no crossing exists, which is a real answer
    rather than a failure: a path under a Docker volume names bytes the host
    cannot reach by any name. ``explanation`` is populated either way, because
    the caller that has to report this — a denial, a tool result, a hint beside
    a traceback — needs a sentence more than it needs a flag.
    """

    requested: str
    resolved: str | None = None
    mount: Mount | None = None
    writable: bool = False
    explanation: str


class MountTopology(BaseModel):
    """The mount table, asked what a path is called on the other side.

    Constructed from :meth:`Sandbox.mount_topology`, which is valid before the
    container starts — so this answers from configuration alone and needs no
    running container, which is what lets a hook process hold one.
    """

    mounts: list[Mount]

    def binds(self) -> list[Mount]:
        """The mounts that have a host side at all.

        A volume is backed by Docker rather than by a directory, so its
        ``source`` names a volume and translating through it would invent a
        host path that does not exist.
        """
        return [mount for mount in self.mounts if mount.kind == "bind"]

    def exchanges(self) -> list[str]:
        """Container paths that reach the host and accept writes.

        What to tell an agent that asked for a crossing there is none of: these
        are the paths where a file it writes will still be there afterwards.
        """
        return sorted(
            mount.container_path for mount in self.binds() if mount.mode == "rw"
        )

    def contains(self, container_path: str) -> bool:
        """Whether any mount, volume or bind, holds this container path.

        A different question from what the host calls it: a path under the
        workspace volume has no host name and is still a perfectly good path
        to run code against, so :meth:`to_host` returning nothing for it must
        not be read as the path being unusable.
        """
        requested = PurePosixPath(container_path)
        return any(
            requested.is_relative_to(PurePosixPath(mount.container_path))
            for mount in self.mounts
        )

    def to_host(self, container_path: str) -> Translation:
        """Name a container path as the host names it."""
        requested = PurePosixPath(container_path)
        under = [
            mount
            for mount in self.binds()
            if requested.is_relative_to(PurePosixPath(mount.container_path))
        ]
        if not under:
            return Translation(
                requested=container_path,
                explanation=(
                    f"{container_path} exists only inside the container: it is "
                    "under no bind mount, so the host has no name for it. Write "
                    f"to one of {', '.join(self.exchanges())} to reach the host."
                ),
            )
        # Longest match wins: a source root nested under another mount's path
        # is the more specific answer, and the shallower one would silently
        # resolve to a host directory that does not contain the file.
        mount = max(under, key=lambda m: len(PurePosixPath(m.container_path).parts))
        resolved = PurePosixPath(mount.source) / requested.relative_to(
            mount.container_path
        )
        return Translation(
            requested=container_path,
            resolved=str(resolved),
            mount=mount,
            writable=mount.mode == "rw",
            explanation=(
                f"{container_path} is {resolved} on the host, mounted "
                f"{mount.mode} in the container."
            ),
        )

    def to_container(self, host_path: str) -> Translation:
        """Name a host path as the container names it."""
        requested = PurePosixPath(host_path)
        under = [
            mount
            for mount in self.binds()
            if requested.is_relative_to(PurePosixPath(mount.source))
        ]
        if not under:
            return Translation(
                requested=host_path,
                explanation=(
                    f"{host_path} is not mounted into the container, so code "
                    "running there cannot open it. Copy it under one of "
                    f"{', '.join(self.exchanges())} first."
                ),
            )
        mount = max(under, key=lambda m: len(PurePosixPath(m.source).parts))
        resolved = PurePosixPath(mount.container_path) / requested.relative_to(
            mount.source
        )
        return Translation(
            requested=host_path,
            resolved=str(resolved),
            mount=mount,
            writable=mount.mode == "rw",
            explanation=(
                f"{host_path} is {resolved} inside the container, mounted "
                f"{mount.mode} there."
            ),
        )
