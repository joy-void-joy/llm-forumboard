"""Read Claude Code usage: the live OAuth windows, and the local daily cache.

The ``/api/oauth/usage`` endpoint gives real-time utilization for every window
the plan meters, and ``stats-cache.json`` supplements it with per-day and
per-model detail. The display, the pacing bars, and the snapshot are shared;
only what is read here is Anthropic's.

Examples::

    $ uv run lup-devtools usage claude
    $ uv run lup-devtools usage claude --no-detail
    $ uv run lup-devtools usage claude --json
    $ uv run lup-devtools usage claude --watch --interval 300
"""
