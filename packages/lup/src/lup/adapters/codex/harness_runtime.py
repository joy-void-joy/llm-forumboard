"""Codex CLI evidence, cache verification, and explicit plugin installation."""

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sh
from pydantic import BaseModel, Field

from lup.adapters.codex.login import CODEX_LOGIN
from lup.harness.contracts import CapabilityProbe
from lup.harness.models import CapabilityEvidence
from lup.types import EnvVars


class CodexCliEvidence(BaseModel, frozen=True):
    executable: Path
    arguments: list[str] = []
    installed: bool
    output: str = ""


class PluginCacheConfig(BaseModel, frozen=True):
    codex_home: Path = Field(default_factory=lambda: Path.home() / ".codex")
    # Required for explicit shared homes and for a stable installed-cache path.
    marketplace: str
    plugin: str = "lup"
    # None derives the cache segment from the plugin manifest — codex caches
    # an installed plugin under its declared version.
    version: str | None = None


class PluginCacheEvidence(BaseModel, frozen=True):
    source_root: Path
    installed_root: Path
    source_digest: str
    installed_digest: str | None = None
    ready: bool


def directory_digest(root: Path) -> str | None:
    """Hash deployable relative paths, modes, and bytes for one plugin tree."""
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.relative_to(root).parts
        and path.suffix not in {".pyc", ".pyo"}
    )
    for path in files:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(b"x" if path.stat().st_mode & 0o111 else b"-")
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def plugin_manifest_version(source_root: Path) -> str:
    """The version segment codex caches this plugin under, from its manifest."""
    manifest = source_root / ".codex-plugin" / "plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    match data:
        case {"version": str(version)}:
            return version
    raise ValueError(f"Codex plugin manifest lacks a version: {manifest}")


# lup: ignore[model-free-function] — digests the source tree the config locates
def plugin_cache_evidence(
    source_root: Path, config: PluginCacheConfig
) -> PluginCacheEvidence:
    """Compare the committed package to the separately installed cached copy."""
    source = directory_digest(source_root)
    if source is None:
        raise FileNotFoundError(f"Codex plugin source does not exist: {source_root}")
    installed_root = (
        config.codex_home
        / "plugins"
        / "cache"
        / config.marketplace
        / config.plugin
        / (config.version or plugin_manifest_version(source_root))
    )
    installed = directory_digest(installed_root)
    return PluginCacheEvidence(
        source_root=source_root,
        installed_root=installed_root,
        source_digest=source,
        installed_digest=installed,
        ready=installed == source,
    )


class CodexCapabilityProbe(CapabilityProbe[CodexCliEvidence]):
    """Probe exactly one named Codex CLI capability through its native command."""

    def __init__(
        self,
        capability: str = "codex-cli",
        arguments: list[str] | None = None,
        executable: Path = Path("codex"),
    ) -> None:
        self.capability = capability
        self.arguments = list(arguments or ["--version"])
        self.executable = executable

    def probe(self) -> CapabilityEvidence[CodexCliEvidence]:
        try:
            command = sh.Command(str(self.executable))
            output = str(command(*self.arguments))
        except (sh.CommandNotFound, sh.ErrorReturnCode):
            evidence = CodexCliEvidence(
                executable=self.executable,
                arguments=self.arguments,
                installed=False,
            )
            return CapabilityEvidence(
                capability=self.capability,
                supported=False,
                evidence=evidence,
                version="missing",
            )
        try:
            version = (
                output if self.arguments == ["--version"] else str(command("--version"))
            )
        except (sh.CommandNotFound, sh.ErrorReturnCode):
            version = "unknown"
        evidence = CodexCliEvidence(
            executable=self.executable,
            arguments=self.arguments,
            installed=True,
            output=output,
        )
        return CapabilityEvidence(
            capability=self.capability,
            supported=True,
            evidence=evidence,
            version=version.strip(),
        )


def codex_capability_probes(
    executable: Path = Path("codex"),
) -> list[CodexCapabilityProbe]:
    """Compose independent probes without a provider-wide support table."""
    return [
        CodexCapabilityProbe("codex-cli", ["--version"], executable),
        CodexCapabilityProbe("app-server", ["app-server", "--help"], executable),
        CodexCapabilityProbe("plugins", ["plugin", "--help"], executable),
        CodexCapabilityProbe(
            "hooks",
            ["--enable", "hooks", "features", "list"],
            executable,
        ),
    ]


class CodexPluginInstaller:
    """Install only missing/stale local packages through the official CLI."""

    def __init__(
        self,
        config: PluginCacheConfig,
        executable: Path = Path("codex"),
    ) -> None:
        self.config = config
        self.executable = executable

    def plugin_environment(self) -> EnvVars:
        """Environment shared by every Codex plugin lifecycle command."""
        self.config.codex_home.mkdir(parents=True, exist_ok=True)
        return {
            **os.environ,  # lup: ignore[os-environ] — exact child-process inheritance
            **CODEX_LOGIN.environment(self.config.codex_home),
        }

    def ensure(
        self, source_root: Path, cwd: Path, force: bool = False
    ) -> PluginCacheEvidence:
        before = plugin_cache_evidence(source_root, self.config)
        if before.ready and not force:
            return before
        environment = self.plugin_environment()
        # The marketplace definition lives in the repository
        # (`.agents/plugins/marketplace.json`). The CLI refuses a name already
        # registered from a different source — which every sibling worktree and
        # project is — so the name is dropped before it is re-pointed here.
        sh.Command(str(self.executable))(
            "plugin",
            "marketplace",
            "remove",
            self.config.marketplace,
            _cwd=str(cwd),
            _env=environment,
            _ok_code=[0, 1],
        )
        sh.Command(str(self.executable))(
            "plugin",
            "marketplace",
            "add",
            str(cwd),
            _cwd=str(cwd),
            _env=environment,
        )
        selector = f"{self.config.plugin}@{self.config.marketplace}"
        if before.installed_digest is not None:
            sh.Command(str(self.executable))(
                "plugin",
                "remove",
                selector,
                _cwd=str(cwd),
                _env=environment,
                _ok_code=[0, 1],
            )
        sh.Command(str(self.executable))(
            "plugin",
            "add",
            selector,
            "--json",
            _cwd=str(cwd),
            _env=environment,
        )
        after = plugin_cache_evidence(source_root, self.config)
        if not after.ready:
            raise RuntimeError(
                "Codex reported installation success but cached plugin digest differs"
            )
        return after

    def remove(self, cwd: Path) -> None:
        """Remove the installed plugin before its temporary marketplace."""
        environment = self.plugin_environment()
        selector = f"{self.config.plugin}@{self.config.marketplace}"
        sh.Command(str(self.executable))(
            "plugin",
            "remove",
            selector,
            _cwd=str(cwd),
            _env=environment,
            _ok_code=[0, 1],
        )
        sh.Command(str(self.executable))(
            "plugin",
            "marketplace",
            "remove",
            self.config.marketplace,
            _cwd=str(cwd),
            _env=environment,
            _ok_code=[0, 1],
        )

    @contextmanager
    def temporary(
        self, source_root: Path, cwd: Path, force: bool = False
    ) -> Iterator[PluginCacheEvidence]:
        """Install a plugin for one native Codex process."""
        try:
            yield self.ensure(source_root, cwd, force)
        finally:
            self.remove(cwd)
