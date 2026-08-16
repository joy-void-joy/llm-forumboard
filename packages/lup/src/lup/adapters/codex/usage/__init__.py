"""Read Codex usage: the plan's metered windows, and its daily tokens.

Both come from the app-server's own account calls — ``account/rateLimits/read``
and ``account/tokenUsage/read`` — which is the runtime asking on its own
credential rather than this reading one. The display, the pacing bars, and the
snapshot are shared; only what is read here is Codex's.

Examples::

    $ uv run lup-devtools usage codex
    $ uv run lup-devtools usage codex --no-detail
    $ uv run lup-devtools usage codex --json
    $ uv run lup-devtools usage codex --watch --interval 300
"""
