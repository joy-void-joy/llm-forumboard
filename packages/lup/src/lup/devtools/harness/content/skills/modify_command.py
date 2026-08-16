"""Canonical declaration for the modify-command skill."""

import lup.harness.models as models

SKILL = models.Skill(
    id="skill.modify-command",
    name="modify-command",
    description="Modify an existing slash command based on a description or delta",
    arguments=[
        models.Argument(
            name="arguments",
            description="Optional arguments supplied with the skill invocation",
            required=False,
        ),
    ],
    tools=["Read", "Edit", "Write", "Glob", "Grep", "AskUserQuestion"],
    argument_hint="[command-name] [delta or description] [--args hint1 hint2]",
    prompt=models.PromptDocument(
        parts=[
            models.TextPart(
                text=r"""# Modify Existing Slash Command

## Your Task

The user wants to MODIFY an existing slash command. Parse the arguments to determine which command to change and how.

**Arguments provided**: """
            ),
            models.ArgumentsRef(),
            models.TextPart(
                text=r"""

### How to Parse Arguments

The first word is the **command name** to modify. Everything after is the **delta** (what to change) or a **new description** (replace the command's behavior entirely), with an optional `--args` flag.

**Basic:** `"""
            ),
            models.SkillInvocation(plugin="lup", skill="modify-command"),
            models.TextPart(
                text=r""" commit Add a step that runs ruff format before committing`

- Command name: `commit`
- Delta: "Add a step that runs ruff format before committing"

**With args:** `"""
            ),
            models.SkillInvocation(plugin="lup", skill="modify-command"),
            models.TextPart(
                text=r""" debug Add verbose flag --args [error] [--verbose]`

- Command name: `debug`
- Delta: "Add verbose flag"
- New argument hints: `[error] [--verbose]`

When `--args` is provided, update the command's `argument-hint` frontmatter to the specified hints and ensure `"""
            ),
            models.ArgumentsRef(),
            models.TextPart(
                text=r"""` is handled in the command body.

### If No Arguments Provided

If `"""
            ),
            models.ArgumentsRef(),
            models.TextPart(
                text=r"""` is empty, ask the user:

- Which command should be modified?
- What changes should be made?

### Steps

1. **Parse** the command name and delta from the arguments
2. **Find** the source -- search in these locations, in order:
   - `packages/lup/src/lup/devtools/harness/content/skills/<name>.py`, then `src/lup_template/devtools/harness/content/skills/<name>.py` (lup skills, including every `lup:name` variant -- the underscored module name; the library half holds the skills about agent work, this repository's the ones about being a template)
   - a command the project or the person defined natively, outside any plugin

   Files under """
            ),
            models.PluginPath(plugin="lup", location="skills", scope="every_tree"),
            models.TextPart(
                text=r""" are generated from the declarations -- read them to see the rendered result, never to edit.
3. **Read** the current declaration in full
4. **Analyze** the delta -- determine whether the user wants to:
   - **Add** new behavior (append steps, add sections)
   - **Change** existing behavior (modify instructions, update tools)
   - **Remove** behavior (simplify, strip sections)
   - **Replace** entirely (new description overrides old)
5. **Show the user** the proposed changes: summarize what will change, show
   before/after for key sections if helpful, then """
            ),
            models.RequestApproval(
                action="writing the changed declaration",
                reason="the change reaches every tree the declaration renders into",
            ),
            models.TextPart(
                text=r"""
6. **Apply** the changes -- edit the declaration's prompt parts
7. **Update the `tools` list** if needed (e.g., new grants for added functionality)
8. **Regenerate** with `uv run lup-devtools harness claude` and `harness codex` when the source was a lup skill
9. **Confirm** the modification and show a summary

### Guidelines

- **Preserve the declaration's structure and style** -- match the raw-string and part-splitting patterns of the existing module
- **Don't over-modify** -- only change what the delta requires. If the user says "add X", don't also reorganize unrelated sections.
- **Update the `tools` list** if the delta introduces new tool requirements (e.g., adding a git step requires `Bash(git:*)`)
- **Keep the skill self-contained** -- it should work without requiring the user to remember the delta
- **Preserve working behavior** -- don't break existing functionality unless the user explicitly asks to replace it
"""
            ),
        ]
    ),
)
