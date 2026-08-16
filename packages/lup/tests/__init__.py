"""Behavior tests for the ``lup`` library, runnable without the application.

Every test here imports only ``lup``. That is the whole point of the split:
the package ships to an index, and an adopter who installs it gets no
``src/lup_template``, so a suite that reached for one would prove the library
works only where it was written. Running ``uv run pytest`` from
``packages/lup`` exercises exactly this set against exactly what the
distribution contains, which is what catches a library depending on a
fixture only the template supplies.

Tests needing the application live in the repository-root suite instead.
``-m integration`` adds the live Docker and native-CLI lanes in both.
"""
