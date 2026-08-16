"""Canonical harness generation and native launch commands.

The application side of :mod:`lup.harness`: ``content/`` holds the typed
skill, agent, and guidance declarations; ``catalog`` composes them with the
application-owned hook policy into one canonical ``Harness``; ``generate``
runs the ownership-safe generation pipeline over it; ``evidence`` tracks
recorded native CLI/SDK versions for the doctor; ``app`` is the Typer surface
wiring all of it to ``lup-devtools harness ...``.
"""
