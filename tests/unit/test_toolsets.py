"""The tool registry: what is served, and what is deliberately not.

Two things worth holding. The served group names must match what the registry
builds, because the Codex path selects groups by name and a mismatch serves an
empty server rather than failing. And the worldview tools must be withheld
when Notion is unconfigured — a tool that could only fail is worse than an
absent one, because the agent will try it and then reason about the error.
"""

import pytest

from forumboard.agent.config import settings
from forumboard.agent.tool_policy import ToolPolicy
from forumboard.agent.toolsets import (
    WORLDVIEW_GROUP,
    build_session_toolset,
    tool_group_names,
)


def test_served_names_match_built_groups() -> None:
    """A name the CLI can select is a group the registry actually builds."""
    built = set(build_session_toolset()["groups"])
    assert set(tool_group_names()) == built


def test_a_session_without_notion_tools_still_has_the_group() -> None:
    """An empty group is the honest answer, not a missing key.

    Callers index the group by name; making it absent would turn "Notion is
    not configured" into a KeyError somewhere unrelated.
    """
    toolset = build_session_toolset()
    assert toolset["groups"][WORLDVIEW_GROUP] == []


def test_notion_tools_are_withheld_without_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The policy, not the caller, is what decides a tool is unreachable."""
    monkeypatch.setattr(settings, "notion_token", None)
    policy = ToolPolicy(settings)
    assert "requires:notion" in policy.excluded_tags


def test_notion_tools_are_offered_once_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With both a token and a database, nothing withholds the group."""
    monkeypatch.setattr(settings, "notion_token", "ntn_test")
    monkeypatch.setattr(settings, "notion_worldview_database_id", "d" * 32)
    policy = ToolPolicy(settings)
    assert "requires:notion" not in policy.excluded_tags
    assert "requires:worldview-db" not in policy.excluded_tags


def test_a_configured_worldview_database_is_its_own_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token alone does not make the write tools reachable.

    Withholding them separately is what turns "no database configured" into a
    tool the agent never sees, rather than a write that fails at the end of a
    pass it has already paid for.
    """
    monkeypatch.setattr(settings, "notion_token", "ntn_test")
    monkeypatch.setattr(settings, "notion_worldview_database_id", None)
    policy = ToolPolicy(settings)
    assert "requires:notion" not in policy.excluded_tags
    assert "requires:worldview-db" in policy.excluded_tags
