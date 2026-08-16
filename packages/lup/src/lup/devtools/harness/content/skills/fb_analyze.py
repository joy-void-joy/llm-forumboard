"""Canonical declaration for the fb-analyze skill."""

import lup.harness.models as models

SKILL = models.Skill(
    id="skill.fb-analyze",
    name="fb-analyze",
    description="Aggregate tool health, capability gaps, and reasoning patterns across sessions",
    tools=[
        "Bash(uv run lup-devtools:*)",
        "Read",
        "Grep",
        "Glob",
        "Task",
        "AskUserQuestion",
    ],
    prompt=models.PromptDocument(
        parts=[
            models.TextPart(
                text=r"""# Analyze: Tool Health & Capability Gaps

Aggregate findings across sessions to identify systemic patterns.

## Process

### 1. Structured analysis report

```bash
uv run lup-devtools feedback analyze
```

This produces a JSON report with three sections:
- **tool_health**: per-tool call counts, error counts, error rate
- **error_patterns**: sessions with high error rates, grouped by error type
- **capability_gaps**: agent capability requests extracted from traces

Identify: which tools fail most? Are failures transient or systematic? What does the agent say it needs?

Trust capability requests — the agent knows what it lacks. Quote the agent's exact words so you can act on them.

### 2. Reasoning patterns

From investigation findings:
- Are there systematic reasoning errors, or just individual misjudgments?
- Does the agent consistently struggle with certain task types?
- Are expensive tools providing proportional value?

### 3. Version comparison (if relevant)

If comparing across versions, get code-level diffs: """
            ),
            models.Delegate(
                subagent_type="lup:version-explorer",
                prompt="Compare vX.Y.Z and vA.B.C",
            ),
            models.TextPart(
                text=r"""

### 4. Summarize

From tool-health and capability-gap data:
- Which tools consistently fail? What's the root cause?
- What does the agent need but doesn't have?
- Are there reasoning quality patterns across sessions?
"""
            ),
        ]
    ),
)
