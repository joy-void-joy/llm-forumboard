"""Agent module for the self-improving loop.

This subpackage contains the core agent code that the feedback loop improves:
- core.py: Main agent orchestration (dispatches to the SDK adapter)
- config.py: Configuration via pydantic-settings
- models.py: Output models (AgentOutput, Factor, AgentSessionResult)
- prompts.py: System prompt sections and composition
- subagents.py: Subagent definitions (SDK-agnostic SubagentSpec)
- tool_policy.py: Conditional tool availability (tag-based filtering)
- tools/: Domain MCP tools (example, reflect, realtime)
"""
