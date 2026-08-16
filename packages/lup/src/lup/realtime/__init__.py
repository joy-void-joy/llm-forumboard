"""Realtime machinery for persistent agents, split by concern.

Not :mod:`lup.runtime` — that package is the session/turn engine underneath;
``realtime`` owns the wake -> act -> sleep lifecycle layered on top of it.

Three modules, one concern each, with a one-way dependency arrow:

- :mod:`lup.realtime.scheduler` — the in-process ``Scheduler`` state machine
  (sleep/wake, debounce, scheduled actions, reminders, delayed actions) plus
  the Stop/gate hook factories that hold an in-process persistent agent in
  its wake -> act -> sleep loop.
- :mod:`lup.realtime.models` — the tool input/output models the agent-facing
  realtime tools speak, independent of how those tools are wired.
- :mod:`lup.realtime.relay` — the subprocess transport (file mailbox, relay
  events, served tools, parent wake loop) that gives backends whose tools run
  in a separate process the same sleep/wake behavior.

The relay imports the scheduler core and the models; the core imports neither
the relay nor the models. That arrow is the whole point of the split: the
state machine stays usable on its own, and the file-transport wiring layers on
top without the core ever depending on it.
"""
