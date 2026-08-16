"""Claude-specific profile and compatible-endpoint transforms."""

from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr

from lup.adapters.claude.runtime import (
    ClaudeSessionConfig,
    create_claude_session_factory,
)
from lup.adapters.claude.config_home import default_config_home
from lup.adapters.claude.login import CLAUDE_LOGIN
from lup.runtime.config import ConfigTransform, ProfileResolver, ProfileSelector

PLACEHOLDER_CREDENTIAL = "dummy"


class ClaudeProfileSelection(BaseModel, frozen=True):
    """One complete Claude account/configuration home."""

    config_directory: Path


class ClaudeProfileRegistry(BaseModel, frozen=True):
    """Immutable account selection state supplied by an application."""

    profiles: dict[str, ClaudeProfileSelection] = {}
    active: str | None = None
    default: ClaudeProfileSelection = Field(
        default_factory=lambda: ClaudeProfileSelection(
            config_directory=default_config_home()
        )
    )


class ClaudeConfigDirectoryTransform(ConfigTransform[ClaudeSessionConfig]):
    """Select a Claude config home without mutating the source config."""

    def __init__(self, selection: ClaudeProfileSelection) -> None:
        self.selection = selection

    def apply(self, config: ClaudeSessionConfig) -> ClaudeSessionConfig:
        environment = dict(config.environment)
        environment.update(CLAUDE_LOGIN.environment(self.selection.config_directory))
        return config.model_copy(update={"environment": environment})


class ClaudeProfileResolver(ProfileResolver[ClaudeSessionConfig]):
    """Resolve explicit, active, then default Claude account selection."""

    def __init__(self, registry: ClaudeProfileRegistry) -> None:
        self.registry = registry

    def resolve(self, name: str | None) -> ConfigTransform[ClaudeSessionConfig]:
        selected = name or self.registry.active
        if selected is None:
            return ClaudeConfigDirectoryTransform(self.registry.default)
        try:
            profile = self.registry.profiles[selected]
        except KeyError as error:
            raise KeyError(f"unknown Claude profile {selected!r}") from error
        return ClaudeConfigDirectoryTransform(profile)


# The registry declares which account is selected; naming the resolver and the
# session factory that act on it would put both inside the declaration.
# lup: ignore[model-free-function] — composition root over the registry
def claude_profile_selector(
    registry: ClaudeProfileRegistry,
) -> ProfileSelector[ClaudeSessionConfig]:
    """The surface a consumer holds over Claude account selection."""
    return ProfileSelector(
        ClaudeProfileResolver(registry), create_claude_session_factory
    )


class ClaudeCompatibleEndpoint(BaseModel, frozen=True):
    """All configuration owned by an Anthropic-compatible endpoint."""

    base_url: AnyHttpUrl
    api_key: SecretStr | None = None
    auth_style: Literal["auth_token", "api_key"] = "auth_token"
    map_model_aliases: bool = True


class ClaudeCompatibilityTransform(ConfigTransform[ClaudeSessionConfig]):
    """Point Claude scaffolding at one compatible endpoint."""

    def __init__(self, endpoint: ClaudeCompatibleEndpoint) -> None:
        self.endpoint = endpoint

    def apply(self, config: ClaudeSessionConfig) -> ClaudeSessionConfig:
        environment = dict(config.environment)
        environment["ANTHROPIC_BASE_URL"] = str(self.endpoint.base_url)
        credential = (
            self.endpoint.api_key.get_secret_value()
            if self.endpoint.api_key is not None
            else PLACEHOLDER_CREDENTIAL
        )
        if self.endpoint.auth_style == "auth_token":
            environment["ANTHROPIC_AUTH_TOKEN"] = credential
            environment["ANTHROPIC_API_KEY"] = ""
        else:
            environment["ANTHROPIC_API_KEY"] = credential
            environment["ANTHROPIC_AUTH_TOKEN"] = ""
        if self.endpoint.map_model_aliases and config.model is not None:
            environment.update(
                {
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": config.model,
                    "ANTHROPIC_DEFAULT_SONNET_MODEL": config.model,
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": config.model,
                }
            )
        environment.update(
            {
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_TELEMETRY": "1",
                "DISABLE_ERROR_REPORTING": "1",
                "DISABLE_BUG_COMMAND": "1",
            }
        )
        return config.model_copy(update={"environment": environment})
