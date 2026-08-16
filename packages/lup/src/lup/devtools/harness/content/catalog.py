"""The skills and agents lup ships, and how a roster is written down.

Aggregation is explicit imports rather than a scan of the directory: a
misspelled or missing module is a type-checking error rather than a skill
that silently stops being declared.

A project composes these with its own and passes the result to its harness,
so the formatters take the roster they render instead of reading one — a
document describing "every skill this plugin ships" has to see both halves.
"""

import lup.harness.models as models
from lup.devtools.harness.content.agents.implementer import (
    AGENT as AGENT_IMPLEMENTER,
)
from lup.devtools.harness.content.agents.trace_explorer import (
    AGENT as AGENT_TRACE_EXPLORER,
)
from lup.devtools.harness.content.agents.version_explorer import (
    AGENT as AGENT_VERSION_EXPLORER,
)
from lup.devtools.harness.content.agents.version_reviewer import (
    AGENT as AGENT_VERSION_REVIEWER,
)
from lup.devtools.harness.content.skills.add_command import (
    SKILL as SKILL_ADD_COMMAND,
)
from lup.devtools.harness.content.skills.bump import SKILL as SKILL_BUMP
from lup.devtools.harness.content.skills.close import SKILL as SKILL_CLOSE
from lup.devtools.harness.content.skills.commit import SKILL as SKILL_COMMIT
from lup.devtools.harness.content.skills.create_investigator import (
    SKILL as SKILL_CREATE_INVESTIGATOR,
)
from lup.devtools.harness.content.skills.debug import SKILL as SKILL_DEBUG
from lup.devtools.harness.content.skills.fb_analyze import (
    SKILL as SKILL_FB_ANALYZE,
)
from lup.devtools.harness.content.skills.fb_implement import (
    SKILL as SKILL_FB_IMPLEMENT,
)
from lup.devtools.harness.content.skills.fb_investigate import (
    SKILL as SKILL_FB_INVESTIGATE,
)
from lup.devtools.harness.content.skills.fb_reflect import (
    SKILL as SKILL_FB_REFLECT,
)
from lup.devtools.harness.content.skills.fb_status import (
    SKILL as SKILL_FB_STATUS,
)
from lup.devtools.harness.content.skills.feedback_loop import (
    SKILL as SKILL_FEEDBACK_LOOP,
)
from lup.devtools.harness.content.skills.hooks import SKILL as SKILL_HOOKS
from lup.devtools.harness.content.skills.implementer import (
    SKILL as SKILL_IMPLEMENTER,
)
from lup.devtools.harness.content.skills.land import SKILL as SKILL_LAND
from lup.devtools.harness.content.skills.merge import SKILL as SKILL_MERGE
from lup.devtools.harness.content.skills.modify_command import (
    SKILL as SKILL_MODIFY_COMMAND,
)
from lup.devtools.harness.content.skills.principle import (
    SKILL as SKILL_PRINCIPLE,
)
from lup.devtools.harness.content.skills.rebase import SKILL as SKILL_REBASE
from lup.devtools.harness.content.skills.refactor import SKILL as SKILL_REFACTOR
from lup.devtools.harness.content.skills.refactor_tools import (
    SKILL as SKILL_REFACTOR_TOOLS,
)
from lup.devtools.harness.content.skills.report import SKILL as SKILL_REPORT
from lup.devtools.harness.content.skills.resolve import SKILL as SKILL_RESOLVE
from lup.devtools.harness.content.skills.resolve_reviewer import (
    SKILL as SKILL_RESOLVE_REVIEWER,
)
from lup.devtools.harness.content.skills.review import SKILL as SKILL_REVIEW
from lup.devtools.harness.content.skills.verify_solved import (
    SKILL as SKILL_VERIFY_SOLVED,
)

# lup: ignore[library-default] — the skills this library authors, so the list is what it ships rather than a choice made for an adopter
LIBRARY_SKILLS = [
    SKILL_ADD_COMMAND,
    SKILL_BUMP,
    SKILL_CLOSE,
    SKILL_COMMIT,
    SKILL_CREATE_INVESTIGATOR,
    SKILL_DEBUG,
    SKILL_FB_ANALYZE,
    SKILL_FB_IMPLEMENT,
    SKILL_FB_INVESTIGATE,
    SKILL_FB_REFLECT,
    SKILL_FB_STATUS,
    SKILL_FEEDBACK_LOOP,
    SKILL_HOOKS,
    SKILL_IMPLEMENTER,
    SKILL_LAND,
    SKILL_MERGE,
    SKILL_MODIFY_COMMAND,
    SKILL_PRINCIPLE,
    SKILL_REBASE,
    SKILL_REFACTOR,
    SKILL_REFACTOR_TOOLS,
    SKILL_REPORT,
    SKILL_RESOLVE,
    SKILL_RESOLVE_REVIEWER,
    SKILL_REVIEW,
    SKILL_VERIFY_SOLVED,
]
"""Every skill lup ships: the git and review loop, the resolver, code work,
harness authoring, and the feedback loop. Each automates agent work inside a
project rather than the standing up of one."""

# lup: ignore[library-default] — the agents this library authors, so the list is what it ships rather than a choice made for an adopter
LIBRARY_AGENTS = [
    AGENT_IMPLEMENTER,
    AGENT_TRACE_EXPLORER,
    AGENT_VERSION_EXPLORER,
    AGENT_VERSION_REVIEWER,
]
"""Every agent lup ships, each a generic role over library machinery."""


def skill_roster_parts(
    skills: list[models.Skill], plugin: models.NativeName
) -> list[models.PromptPart]:
    """Format a skill roster as portable bullet-list document parts.

    The invocation is issued rather than spelled, so the same roster renders
    in whichever sigil the reading runtime uses.
    """
    return [
        part
        for skill in sorted(skills, key=lambda skill: skill.name)
        for part in (
            models.TextPart(text="- "),
            models.SkillInvocation(plugin=plugin, skill=skill.name),
            models.TextPart(text=f" — {skill.description}\n"),
        )
    ]


def agent_roster_text(agents: list[models.Agent]) -> str:
    """Format an agent roster as Markdown bullet lines."""
    return "".join(
        f"- `{agent.name}` — {agent.description}\n"
        for agent in sorted(agents, key=lambda agent: agent.name)
    )
