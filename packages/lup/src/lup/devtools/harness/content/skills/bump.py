"""Canonical declaration for the bump skill."""

import lup.harness.models as models

SKILL = models.Skill(
    id="skill.bump",
    name="bump",
    description="Review changes since last bump and bump agent version",
    arguments=[
        models.Argument(
            name="arguments",
            description="Optional arguments supplied with the skill invocation",
            required=False,
        ),
    ],
    tools=[
        "Bash(uv run lup-devtools:*)",
        "Read",
        "Grep",
        "Glob",
        "AskUserQuestion",
        "Skill(lup:commit)",
    ],
    argument_hint="[patch|minor|major]",
    prompt=models.PromptDocument(
        parts=[
            models.TextPart(
                text=r"""# Version Bump

Review changes since the last version bump and bump the agent version (`[tool.lup] agent_version` in `pyproject.toml`) accordingly.

## Input

**Bump level** (optional): """
            ),
            models.ArgumentsRef(),
            models.TextPart(
                text=r"""

If no level is provided, determine the appropriate level from the changes.

## Process

### 1. Commit pending changes

Invoke `"""
            ),
            models.SkillInvocation(plugin="lup", skill="commit"),
            models.TextPart(
                text=r"""` to commit any uncommitted work before bumping.

### 2. Gather context

```bash
uv run lup-devtools version --json
```

This shows the current version, latest tag, and recent history.

### 3. Classify changes

```bash
uv run lup-devtools version changelog --json
```

Read through the commits and categorize:

- **Behavior changes** (require a bump): prompt changes, new/modified tools, scoring logic, subagent changes
- **Data changes** (no bump needed): session outputs, notes, resolution updates
- **Infrastructure changes** (no bump needed): dependencies, CI, scripts, `"""
            ),
            models.NativePath(location="guidance_file"),
            models.TextPart(
                text=r"""`

If there are NO behavior changes since the last bump, inform the user and stop.

### 4. Determine bump level

| Level     | When                                          | Examples                                     |
| --------- | --------------------------------------------- | -------------------------------------------- |
| **patch** | Bug fixes, config tweaks, tool fixes          | Fixed API error handling, adjusted timeout   |
| **minor** | Prompt changes, new tools, tool modifications | Added web search tool, rewrote system prompt |
| **major** | Architecture changes                          | New LLM, new framework, fundamental redesign |

If the user provided a level in `"""
            ),
            models.ArgumentsRef(),
            models.TextPart(text=r"""`, use it. Otherwise recommend a level, then """),
            models.AskUser(question="which bump level to apply"),
            models.TextPart(
                text=r"""

### 5. Apply the bump

```bash
uv run lup-devtools version bump <level>
```

### 6. Report

Show the user what was bumped and the behavioral changes that warranted it.

## Guidelines

- **Only bump for behavior changes** -- Data, docs, and infra commits don't warrant a bump
- **Summarize what changed for the agent**, not the codebase
- **When in doubt, ask** -- put an ambiguous level to the user rather than guessing
"""
            ),
        ]
    ),
)
