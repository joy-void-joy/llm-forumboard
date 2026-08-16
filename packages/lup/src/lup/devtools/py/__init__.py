"""Python introspection tools.

Vetted alternatives to ``python -c`` for package, type, and value exploration.

Examples::

    $ uv run lup-devtools py info claude_agent_sdk.types.ToolUseBlock
    $ uv run lup-devtools py info pydantic.BaseModel --schema
    $ uv run lup-devtools py source lup.mcp.lup_tool
    $ uv run lup-devtools py source claude_agent_sdk --tree
    $ uv run lup-devtools py eval "importlib.metadata.version('pydantic')"
    $ uv run lup-devtools py imports lup.mcp
    $ uv run lup-devtools py imports lup.mcp --reverse
    $ uv run lup-devtools py search ToolUseBlock
"""
