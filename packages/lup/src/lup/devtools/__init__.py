"""The development CLI a project built on lup inherits rather than forks.

These sub-apps are the workflow, not the domain: worktrees and branches, a
pre-flight check, trace and usage readers, Python introspection, the resolver
supervisor, the sync registry, version bookkeeping. None of them knows what
the application it serves is for, which is why an adopter should receive
improvements to them the way it receives the rest of the library — by
upgrading — instead of merging them into a copy it took at fork time.

An application composes these into its own entry point and adds whatever only
it needs; :mod:`lup.devtools.subapps` carries the roster and how one is added.
"""
