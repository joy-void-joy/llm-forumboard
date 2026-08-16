"""Codex-specific profile and compatible-endpoint transforms."""

from pathlib import Path

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr

from lup.adapters.codex.login import CODEX_LOGIN
from lup.adapters.codex.runtime import (
    CodexSessionConfig,
    create_codex_session_factory,
)
from lup.runtime.config import ConfigTransform, ProfileResolver, ProfileSelector
from lup.types import JsonObject

OPENAI_COMPAT_API_KEY_ENV = "LUP_OPENAI_COMPAT_API_KEY"


class CodexProfileSelection(BaseModel, frozen=True):
    """A Codex account home and optional independently named config overlay."""

    codex_home: Path | None = None
    named_profile: str | None = None


class CodexProfileRegistry(BaseModel, frozen=True):
    """Immutable Codex profile selection supplied by an application."""

    profiles: dict[str, CodexProfileSelection] = {}
    active: str | None = None
    default: CodexProfileSelection = Field(default_factory=CodexProfileSelection)


class CodexProfileTransform(ConfigTransform[CodexSessionConfig]):
    """Apply account-home and named-overlay inputs without conflating them."""

    def __init__(self, selection: CodexProfileSelection) -> None:
        self.selection = selection

    def apply(self, config: CodexSessionConfig) -> CodexSessionConfig:
        environment = dict(config.environment)
        if self.selection.codex_home is not None:
            environment.update(CODEX_LOGIN.environment(self.selection.codex_home))
        return config.model_copy(
            update={
                "environment": environment,
                "named_profile": self.selection.named_profile,
            }
        )


class CodexProfileResolver(ProfileResolver[CodexSessionConfig]):
    """Resolve explicit, active, then default Codex profile selection."""

    def __init__(self, registry: CodexProfileRegistry) -> None:
        self.registry = registry

    def resolve(self, name: str | None) -> ConfigTransform[CodexSessionConfig]:
        selected = name or self.registry.active
        if selected is None:
            return CodexProfileTransform(self.registry.default)
        try:
            profile = self.registry.profiles[selected]
        except KeyError as error:
            raise KeyError(f"unknown Codex profile {selected!r}") from error
        return CodexProfileTransform(profile)


# The registry declares which profile is selected; naming the resolver and the
# session factory that act on it would put both inside the declaration.
# lup: ignore[model-free-function] — composition root over the registry
def codex_profile_selector(
    registry: CodexProfileRegistry,
) -> ProfileSelector[CodexSessionConfig]:
    """The surface a consumer holds over Codex profile selection."""
    return ProfileSelector(CodexProfileResolver(registry), create_codex_session_factory)


class CodexCompatibleEndpoint(BaseModel, frozen=True):
    """All configuration owned by one OpenAI-compatible model provider."""

    identifier: str = "lup_openai_compat"
    name: str | None = None
    base_url: AnyHttpUrl
    api_key: SecretStr | None = None
    api_key_environment: str = OPENAI_COMPAT_API_KEY_ENV

    def native_config(self) -> JsonObject:
        provider: JsonObject = {
            "name": self.name or self.identifier,
            "base_url": str(self.base_url),
        }
        if self.api_key is not None:
            provider["env_key"] = self.api_key_environment
        return {
            "model_provider": self.identifier,
            "model_providers": {self.identifier: provider},
        }


class CodexCompatibilityTransform(ConfigTransform[CodexSessionConfig]):
    """Attach a structured provider definition and its credential binding."""

    def __init__(self, endpoint: CodexCompatibleEndpoint) -> None:
        self.endpoint = endpoint

    def apply(self, config: CodexSessionConfig) -> CodexSessionConfig:
        environment = dict(config.environment)
        if self.endpoint.api_key is not None:
            environment[self.endpoint.api_key_environment] = (
                self.endpoint.api_key.get_secret_value()
            )
        return config.model_copy(
            update={
                "model_provider": self.endpoint.identifier,
                "provider_config": self.endpoint.native_config(),
                "environment": environment,
            }
        )
