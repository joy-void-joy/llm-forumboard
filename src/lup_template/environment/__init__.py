"""Environment harness — how the outside world reaches the agent.

The environment is the domain-specific *boundary* around ``lup_template.agent``:
it owns user/system I/O and turns external events into agent runs. The agent
decides; the environment carries inputs in and outputs out. Examples:
- a CLI that takes a task string (``cli/__main__.py``)
- a web UI / HTTP endpoint feeding user input from a site into a session
- a Slack or webhook listener, a game loop, a scheduled poller

**What goes here vs in the agent:** anything about *delivery* — reading a
request, rendering or posting a reply, session lifecycle, auto-commit — lives
here; anything about *deciding* (reasoning, tools, prompts, output shape) lives
in ``agent/``. The common mistake is dumping I/O and orchestration into the
agent; keeping it here leaves the agent a pure decision-maker the feedback loop
can improve in isolation. This code evolves with application requirements; the
feedback loop does not touch it.

Structure:
- cli/__main__.py: Typer CLI — the ``lup`` entry point (run + loop with auto-commit)
"""
