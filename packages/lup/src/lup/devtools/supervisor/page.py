"""What the supervisor page is, for callers that never serve it.

Three callers need to know which port the page answers on: the server itself,
the ``--supervise-port`` flag on a resolver run, and the spawn config that
opens a page beside one. Only the first wants FastAPI, and ``web`` is an
optional extra of this package — so the number lives here rather than beside
the server, and the two launchers reach it without the extra installed.
"""

SUPERVISOR_PORT = 8766
"""Where the supervisor page listens when nothing says otherwise.

A port is this library's judgement rather than anyone's convention, so it
reaches an adopter as the default a ``--port`` flag replaces, never a value
they have to fork to change.
"""
