"""Immutable first-match routing to explicit configured factory recipes."""

from collections.abc import Callable

from pydantic import BaseModel

from lup.runtime.config import ModelMatcher
from lup.runtime.factory import SessionFactory


class ExactModelMatcher(ModelMatcher):
    """Match exactly one model name."""

    def __init__(self, expected: str) -> None:
        if not expected:
            raise ValueError("an exact model matcher cannot be empty")
        self.expected = expected

    def matches(self, model: str) -> bool:
        return model == self.expected


class PrefixModelMatcher(ModelMatcher):
    """Match a non-empty model-name prefix."""

    def __init__(self, prefix: str) -> None:
        if not prefix:
            raise ValueError("a prefix model matcher cannot be empty")
        self.prefix = prefix

    def matches(self, model: str) -> bool:
        return model.startswith(self.prefix)


type FactoryRecipe = Callable[[], SessionFactory]


class ModelRoute(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """One immutable matcher and configured factory recipe."""

    name: str
    matcher: ModelMatcher
    recipe: FactoryRecipe


class ModelRouter:
    """Select explicit recipes first, then the first model match."""

    def __init__(self, routes: list[ModelRoute]) -> None:
        names = [route.name for route in routes]
        if len(names) != len(dict.fromkeys(names)):
            raise ValueError("model route names must be unique")
        self.routes = tuple(routes)

    def resolve(self, model: str, recipe: str | None = None) -> SessionFactory:
        if recipe is not None:
            selected = next(
                (route for route in self.routes if route.name == recipe), None
            )
            if selected is None:
                raise LookupError(f"unknown factory recipe {recipe!r}")
            return selected.recipe()
        selected = next(
            (route for route in self.routes if route.matcher.matches(model)), None
        )
        if selected is None:
            raise LookupError(f"no configured route accepts model {model!r}")
        return selected.recipe()
