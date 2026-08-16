"""Independent configuration, profile, and routing capabilities.

``ProfileSelector`` is the concrete surface a consumer holds over profile
resolution; the ABCs here are the engines it and ``ModelRouter`` compose.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable

from lup.runtime.factory import SessionFactory


class ConfigTransform[C](ABC):
    """Apply one immutable configuration transformation.

    A transform holds no shared behaviour of its own: it is a pure function
    over config, an ABC only so implementations can be named, stacked, and
    inspected before any provider resource exists. There is nothing for a
    composing surface to home, so applications stack transforms directly.
    """

    @abstractmethod
    def apply(self, config: C) -> C:
        """Return the transformed configuration."""


class ProfileResolver[C](ABC):
    """Resolve an optional profile name to a configuration transform."""

    @abstractmethod
    def resolve(self, name: str | None) -> ConfigTransform[C]:
        """Resolve explicit, active, or default profile selection."""


type ConfiguredFactory[C] = Callable[[C], SessionFactory]


class ProfileSelector[C]:
    """Resolve a profile selection and construct the session it configures."""

    def __init__(
        self, resolver: ProfileResolver[C], build: ConfiguredFactory[C]
    ) -> None:
        self.resolver = resolver
        self.build = build

    def transform(self, name: str | None = None) -> ConfigTransform[C]:
        """Resolve the selection as a transform, before any construction.

        The transform is the primitive rather than an intermediate step:
        selections compose with other config transforms and can be inspected
        or dry-run while no provider resource exists yet. Callers that only
        want the configured session use :meth:`session_factory`.
        """
        return self.resolver.resolve(name)

    def session_factory(self, base: C, name: str | None = None) -> SessionFactory:
        """Resolve the selection, apply it to the base config, and construct."""
        return self.build(self.transform(name).apply(base))


class ModelMatcher(ABC):
    """Decide whether one immutable route accepts a model name."""

    @abstractmethod
    def matches(self, model: str) -> bool:
        """Return whether this matcher accepts the model."""
