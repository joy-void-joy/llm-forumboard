"""Native CLI capability probes and the Codex plugin-cache install gate.

The harness decides whether it may launch a native plugin from these
seams: a probe must classify a missing or failing CLI as unsupported
(never crash the doctor), a present CLI must yield its banner as
evidence, and :class:`CodexPluginInstaller` must short-circuit on a
verified cache, reinstall a stale one, and refuse to report success
when the installed digest still differs from the committed source.
Temporary installations must be removed even when their session fails.
Fake executables on disk stand in for the real CLIs, so every branch
runs offline.
"""

import json
from pathlib import Path

import pytest

from lup.adapters.claude.harness import ClaudePluginManifestRenderer
from lup.adapters.codex.harness import CodexPluginManifestRenderer
from lup.harness.models import Plugin
from lup.adapters.claude.harness_runtime import ClaudeCapabilityProbe
from lup.adapters.codex.harness_runtime import (
    CodexCapabilityProbe,
    CodexPluginInstaller,
    PluginCacheConfig,
)


def fake_cli(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


MARKETPLACE = "lup-template-repository"

VERSIONED_CLI = """\
if [ "$1" = "--version" ]; then
  echo "9.9.9 (Fake CLI)"
else
  echo "subcommand help"
fi"""


class TestCapabilityProbes:
    def test_missing_executable_reports_unsupported_not_a_crash(self) -> None:
        for evidence in (
            ClaudeCapabilityProbe("claude-cli", ["--version"], Path("no-such")).probe(),
            CodexCapabilityProbe("codex-cli", ["--version"], Path("no-such")).probe(),
        ):
            assert evidence.supported is False
            assert evidence.version == "missing"

    def test_failing_subcommand_reports_unsupported(self, tmp_path: Path) -> None:
        executable = fake_cli(tmp_path, "claude", "exit 1")

        evidence = ClaudeCapabilityProbe("plugins", ["plugin"], executable).probe()

        assert evidence.supported is False
        assert evidence.version == "missing"

    def test_present_cli_yields_banner_output_and_version(self, tmp_path: Path) -> None:
        executable = fake_cli(tmp_path, "claude", VERSIONED_CLI)

        version = ClaudeCapabilityProbe("claude-cli", ["--version"], executable).probe()
        plugins = ClaudeCapabilityProbe(
            "plugins", ["plugin", "--help"], executable
        ).probe()

        assert version.supported and version.version == "9.9.9 (Fake CLI)"
        assert plugins.supported and "subcommand help" in plugins.evidence.output
        assert plugins.version == "9.9.9 (Fake CLI)"

    def test_codex_probe_shares_the_classification_contract(
        self, tmp_path: Path
    ) -> None:
        executable = fake_cli(tmp_path, "codex", VERSIONED_CLI)

        evidence = CodexCapabilityProbe(
            "app-server", ["app-server", "--help"], executable
        ).probe()

        assert evidence.supported and evidence.evidence.installed
        assert evidence.version == "9.9.9 (Fake CLI)"


def write_plugin_source(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    manifest_dir = root / ".codex-plugin"
    manifest_dir.mkdir(exist_ok=True)
    (manifest_dir / "plugin.json").write_text(
        '{"name": "lup", "version": "9.9.9"}\n', encoding="utf-8"
    )
    (root / "hook.py").write_text("DECISION = 'ask'\n", encoding="utf-8")


class TestPluginInstallGate:
    def cache_root(self, config: PluginCacheConfig) -> Path:
        # Mirrors codex's layout: the cache segment is the manifest version.
        return (
            config.codex_home
            / "plugins"
            / "cache"
            / config.marketplace
            / config.plugin
            / "9.9.9"
        )

    def test_verified_cache_short_circuits_without_invoking_codex(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "source"
        write_plugin_source(source)
        config = PluginCacheConfig(
            codex_home=tmp_path / "codex-home", marketplace=MARKETPLACE
        )
        write_plugin_source(self.cache_root(config))
        exploding = fake_cli(tmp_path, "codex", "exit 97")

        evidence = CodexPluginInstaller(config, exploding).ensure(source, tmp_path)

        assert evidence.ready
        assert evidence.installed_digest == evidence.source_digest

    def test_stale_cache_is_removed_then_reinstalled(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        write_plugin_source(source)
        config = PluginCacheConfig(
            codex_home=tmp_path / "codex-home", marketplace=MARKETPLACE
        )
        cache = self.cache_root(config)
        write_plugin_source(cache)
        (cache / "hook.py").write_text("DECISION = 'allow'\n", encoding="utf-8")
        log = tmp_path / "calls.log"
        installer_cli = fake_cli(
            tmp_path,
            "codex",
            f'echo "$1 $2 $3" >> "{log}"\n'
            'if [ "$2" = "add" ]; then\n'
            f'  rm -rf "{cache}" && mkdir -p "{cache}" && cp -R "{source}/." "{cache}/"\n'
            "fi",
        )

        evidence = CodexPluginInstaller(config, installer_cli).ensure(source, tmp_path)

        assert evidence.ready
        selector = f"{config.plugin}@{config.marketplace}"
        assert log.read_text(encoding="utf-8").splitlines() == [
            "plugin marketplace remove",
            "plugin marketplace add",
            f"plugin remove {selector}",
            f"plugin add {selector}",
        ]

    def test_marketplace_bound_to_another_source_is_repointed(
        self, tmp_path: Path
    ) -> None:
        """Codex refuses a marketplace name already bound to a different root.

        Every sibling worktree and template-derived project is such a root,
        so the name is dropped before it is re-pointed at this one.
        """
        source = tmp_path / "source"
        write_plugin_source(source)
        config = PluginCacheConfig(
            codex_home=tmp_path / "codex-home", marketplace=MARKETPLACE
        )
        cache = self.cache_root(config)
        bound = tmp_path / "bound-elsewhere"
        bound.write_text(config.marketplace, encoding="utf-8")
        installer_cli = fake_cli(
            tmp_path,
            "codex",
            f'if [ "$3" = "remove" ]; then rm -f "{bound}"; fi\n'
            f'if [ "$3" = "add" ] && [ -f "{bound}" ]; then\n'
            f"  echo \"marketplace '{config.marketplace}' is already added"
            ' from a different source" >&2\n'
            "  exit 1\n"
            "fi\n"
            'if [ "$2" = "add" ]; then\n'
            f'  rm -rf "{cache}" && mkdir -p "{cache}" && cp -R "{source}/." "{cache}/"\n'
            "fi",
        )

        evidence = CodexPluginInstaller(config, installer_cli).ensure(source, tmp_path)

        assert evidence.ready
        assert not bound.exists()

    def test_temporary_installation_is_removed_when_the_session_exits(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "source"
        write_plugin_source(source)
        config = PluginCacheConfig(
            codex_home=tmp_path / "codex-home", marketplace=MARKETPLACE
        )
        cache = self.cache_root(config)
        log = tmp_path / "calls.log"
        installer_cli = fake_cli(
            tmp_path,
            "codex",
            f'echo "$1 $2 $3" >> "{log}"\n'
            'if [ "$2" = "add" ]; then\n'
            f'  mkdir -p "{cache}" && cp -R "{source}/." "{cache}/"\n'
            "fi\n"
            'if [ "$2" = "remove" ]; then\n'
            f'  rm -rf "{cache}"\n'
            "fi",
        )
        installer = CodexPluginInstaller(config, installer_cli)

        with pytest.raises(RuntimeError, match="session exited"):
            with installer.temporary(source, tmp_path) as evidence:
                assert evidence.ready
                assert cache.exists()
                raise RuntimeError("session exited")

        selector = f"{config.plugin}@{config.marketplace}"
        assert not cache.exists()
        assert log.read_text(encoding="utf-8").splitlines() == [
            "plugin marketplace remove",
            "plugin marketplace add",
            f"plugin add {selector}",
            f"plugin remove {selector}",
            "plugin marketplace remove",
        ]

    def test_install_that_leaves_a_differing_digest_is_refused(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "source"
        write_plugin_source(source)
        config = PluginCacheConfig(
            codex_home=tmp_path / "codex-home", marketplace=MARKETPLACE
        )
        lying_cli = fake_cli(tmp_path, "codex", "exit 0")

        with pytest.raises(RuntimeError, match="digest differs"):
            CodexPluginInstaller(config, lying_cli).ensure(source, tmp_path)

    def test_missing_source_tree_is_an_explicit_error(self, tmp_path: Path) -> None:
        config = PluginCacheConfig(
            codex_home=tmp_path / "codex-home", marketplace=MARKETPLACE
        )
        idle_cli = fake_cli(tmp_path, "codex", "exit 0")

        with pytest.raises(FileNotFoundError, match="plugin source does not exist"):
            CodexPluginInstaller(config, idle_cli).ensure(tmp_path / "ghost", tmp_path)


def test_both_adapters_name_the_marketplace_the_plugin_declares() -> None:
    """A marketplace name is per-project, and every adapter must honour it.

    Claude Code keys its machine-wide marketplace registry by name, so a
    library rendering one fixed name hands every adopter the same key. Two
    lup projects on one machine then resolve each other's plugin, and a
    session is judged by a gate its own worktree does not contain.
    """
    plugin = Plugin(
        id="plugin.lup",
        name="lup",
        marketplace="acme-repository",
        version="0.1.0",
        description="fixture",
        skills=[],
        agents=[],
    )

    for renderer in (ClaudePluginManifestRenderer(), CodexPluginManifestRenderer()):
        names = [
            json.loads(artifact.content)["name"]
            for artifact in renderer.render(plugin).artifacts
            if artifact.path.name == "marketplace.json"
        ]
        assert names == ["acme-repository"], type(renderer).__name__
