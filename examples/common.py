"""Shared typed output used by the runtime examples."""

from pydantic import BaseModel, Field


class Summary(BaseModel, frozen=True):
    """A minimal structured result submitted by an example agent."""

    summary: str = Field(min_length=1)
