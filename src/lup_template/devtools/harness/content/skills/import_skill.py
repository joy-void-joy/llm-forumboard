"""Canonical declaration for the import skill."""

import lup.harness.models as models

SKILL = models.Skill(
    id="skill.import",
    name="import",
    description="Import a specific pattern from a tracked downstream repo",
    arguments=[
        models.Argument(
            name="arguments",
            description="Optional arguments supplied with the skill invocation",
            required=False,
        ),
    ],
    tools=[
        "Bash(git:*, uv run lup-devtools:*)",
        "Read",
        "Grep",
        "Glob",
        "Edit",
        "Write",
        "AskUserQuestion",
        "Skill(lup:commit)",
    ],
    argument_hint="<project> <pattern description>",
    prompt=models.PromptDocument(
        parts=[
            models.TextPart(
                text=r"""# Import Pattern from Downstream

Import a specific pattern, feature, or approach from a tracked downstream repository into this project. Unlike `"""
            ),
            models.SkillInvocation(plugin="lup", skill="update"),
            models.TextPart(
                text=r"""` which reviews all new commits, this command targets a specific pattern the user wants to port.

## Your Task

**Arguments provided**: """
            ),
            models.ArgumentsRef(),
            models.TextPart(
                text=r"""

### Parse Arguments

The first word is the **project name** (must match a tracked project in `sync.json` or `sync.json.local`). Everything after is the **pattern description** — a natural language description of what to import.

**Examples:**

- `"""
            ),
            models.SkillInvocation(plugin="lup", skill="import"),
            models.TextPart(
                text=r""" forecaster version reviewer pattern` — project is `forecaster`, pattern is "version reviewer pattern"
- `"""
            ),
            models.SkillInvocation(plugin="lup", skill="import"),
            models.TextPart(
                text=r""" myapp lib/cache TTL invalidation` — project is `myapp`, pattern is "lib/cache TTL invalidation"

If no arguments provided, """
            ),
            models.AskUser(
                question="which project to import from, and what pattern to import"
            ),
            models.TextPart(
                text=r"""

If only a project name is provided (single word, no description), """
            ),
            models.AskUser(question="what pattern to import from that project"),
            models.TextPart(
                text=r"""

## Steps

### 1. Commit pending changes

Invoke `"""
            ),
            models.SkillInvocation(plugin="lup", skill="commit"),
            models.TextPart(
                text=r"""` to commit any uncommitted work before importing.

### 2. Resolve the project

```bash
uv run lup-devtools sync status
```

Verify the project exists and has a local path. If not configured:

```bash
uv run lup-devtools sync setup <project> <path>
```

Ensure the local copy is available:

```bash
# The sync log command will ensure_local as a side effect
uv run lup-devtools sync log <project>
```

### 3. Search the downstream repo for the pattern

Use the pattern description to locate relevant code. The downstream repo is accessible via `refs/<project>` (symlink created by sync tooling).

Search strategy:

- **`grep`** through `Bash` for keywords from the pattern description across the downstream repo
- **`find`** through `Bash` for file paths if the description mentions specific files or directories
- **Read** promising matches to understand the full implementation

Cast a wide net — search file names, function names, class names, commit messages, and comments. The user's description may not match the exact naming in the downstream repo.

```bash
# Search commit messages for the pattern
cd refs/<project> && git log --all --oneline --grep="<keyword>" | head -20
```

### 4. Present what you found

Present the relevant code you found, organized by file. For each piece:

- Show which file(s) contain the pattern
- Summarize what the code does
- Note any dependencies it pulls in

If you found multiple possible matches, present them and ask which one the user means.

If nothing matches, say so and ask the user to clarify or point you to specific files.

### 5. Analyze portability

Before porting, analyze what needs to change:

- **Domain-specific → generic**: Identify names, types, and logic tied to the downstream domain
- **Dependencies**: Check if the pattern requires packages not in the current project
- **Integration points**: Where does this pattern connect to the rest of the codebase?
- **Conflicts**: Does anything in the current project overlap with or contradict this pattern?

Present your analysis and proposed adaptation plan, then """
            ),
            models.RequestApproval(
                action="making any change to this repository",
                reason="the pattern comes from another project and has to be adapted, "
                "not copied",
            ),
            models.TextPart(
                text=r"""

### 6. Port the pattern

For approved imports:

1. Read the full source files in the downstream repo
2. Adapt the code:
   - Replace domain-specific identifiers with template-appropriate equivalents
   - Adjust import paths to match the current project structure
   - Follow the current project's coding conventions (see its guidance file)
   - Remove domain-specific logic, replace with generic scaffolding or placeholders
3. Write or edit files in the current project
4. Verify:
   ```bash
   uv run lup-devtools dev check
   ```

### 7. Optionally commit

Offer to commit the imported pattern:

```bash
git add <changed-files>
git commit -m "feat(<scope>): port <pattern> from <project>"
```

## Guidelines

- **Generalize, don't copy** — The downstream code is domain-specific. Port the _pattern_, not the domain details.
- **Preserve intent** — Understand _why_ the downstream repo built this pattern, not just _what_ it does. The adaptation should serve the same purpose.
- **Check for existing work** — Before porting, search the current project for similar patterns that could be extended instead of duplicated.
- **Minimal dependencies** — If the pattern pulls in new packages, flag this to the user and ask whether to proceed.
- **Test after porting** — Always run pyright/ruff/pytest after applying changes.
"""
            ),
        ]
    ),
)
