"""Rendering a runtime's project settings from what the harness declares.

Everything here is derived rather than written down: the marketplace key, the
enabled plugin, the tool grants, and the sandbox's network and filesystem
boundaries all come off the ``Plugin`` and its ``HookSet``. A project supplies
only what is genuinely its own — which official plugins it enables, which
tools it grants outright, which reads it refuses — through :class:`Settings`.

The derivation is the point. A settings file written by hand beside a hook
declaration can disagree with it, and the disagreement is invisible until a
session is denied something the policy allows.
"""

from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from lup.harness.models import HookSet, HookUrlScope, Plugin
from lup.types import JsonObject, JsonValue


class Settings(BaseModel, frozen=True):
    """The half of a settings artifact that is a project's own judgement."""

    base: JsonObject = Field(
        default={},
        description=(
            "Runtime settings that are neither derived from the declaration "
            "nor permissions — the editor integrations and session defaults a "
            "project chooses for itself."
        ),
    )
    official_plugins: JsonObject = Field(
        default={},
        description="Vendor-published plugins this project enables by name.",
    )
    allowed: list[JsonValue] = Field(
        default=[],
        description="Tool patterns granted outright, before the served-tool grants.",
    )
    denied: list[JsonValue] = Field(
        default=[],
        description="Tool patterns refused regardless of what else allows them.",
    )


def served_tool_grants(plugin: Plugin) -> list[str]:
    """Grant every tool the plugin's own servers serve.

    A declared server is the project's own code, wired in deliberately, so
    asking per call would make the declaration a suggestion. A new group in
    the toolsets registry is granted by being declared, with nothing in the
    settings to extend.

    The scoped name is what a runtime addresses a plugin's server by; the bare
    key it is declared under matches nothing.
    """
    return [f"mcp__plugin_{plugin.name}_{server.name}" for server in plugin.mcp_servers]


def allowed_network_domains(hooks: HookSet) -> list[str]:
    """Collect fetch-scope hostnames and declared extras, first-seen order.

    A scope that includes subdomains contributes both its apex host and the
    ``*.host`` wildcard, so the OS boundary admits exactly what the semantic
    fetch policy already allows.
    """
    if hooks.sandbox is None:
        return []

    def sandbox_domains(scope: HookUrlScope) -> list[str]:
        host = urlsplit(str(scope.origin)).hostname
        if host is None:
            return []
        return [host, f"*.{host}"] if scope.include_subdomains else [host]

    merged = [
        domain for scope in hooks.allowed_fetch for domain in sandbox_domains(scope)
    ]
    merged.extend(hooks.sandbox.extra_domains)
    return list(dict.fromkeys(merged))


# lup: ignore[model-free-function] — renderer over a declaration and a plugin
def project_settings(declared: Settings, plugin: Plugin | None) -> JsonObject:
    """Render the settings artifact, deriving every block it can.

    The sandbox stays permissive where the semantic policy already judges
    (escapes re-enter the deny lattice) and hardens what shell writers could
    otherwise bypass: human-owned files become OS-level write denials and the
    declared credential paths become sandbox read denials. Both are array
    keys the runtime merges across settings scopes, so a repository states
    its own requirement without displacing the user's or the organization's.
    """
    settings: JsonObject = dict(declared.base)
    settings["enabledPlugins"] = dict(declared.official_plugins)
    if plugin is not None:
        # Marketplace names share one global namespace, so this must be the
        # per-project name the declaration carries. A literal here would
        # register every adopter under one key, and whichever repo installed
        # last would serve its plugin to all of them.
        settings["extraKnownMarketplaces"] = {
            str(plugin.marketplace): {
                "source": {"path": "./.claude/plugins", "source": "directory"}
            }
        }
        settings["enabledPlugins"] = {
            **declared.official_plugins,
            f"{plugin.name}@{plugin.marketplace}": True,
        }
    grants: list[JsonValue] = list(served_tool_grants(plugin)) if plugin else []
    settings["permissions"] = {
        "allow": [*declared.allowed, *grants],
        "deny": list(declared.denied),
    }
    hooks = plugin.hooks if plugin is not None else None
    if hooks is None or hooks.sandbox is None:
        return settings
    domains: list[JsonValue] = list(allowed_network_domains(hooks))
    settings["sandbox"] = {
        "enabled": True,
        "excludedCommands": list(hooks.sandbox.excluded_commands),
        "network": {"allowedDomains": domains},
        "filesystem": {
            "denyWrite": [path.as_posix() for path in hooks.human_owned_files],
            "allowWrite": list(hooks.sandbox.writable_paths),
        },
        "credentials": {
            "files": [
                {"path": path, "mode": "deny"}
                for path in hooks.sandbox.credential_paths
            ]
        },
    }
    return settings
