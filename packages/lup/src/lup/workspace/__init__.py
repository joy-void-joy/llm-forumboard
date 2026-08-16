"""Session workspace: where a run's data lives and how it is addressed.

`paths` resolves version-aware locations under the project root; `context`
carries the active :class:`SessionContext` across a process boundary for
subprocess-served tools; `history` stores, retrieves, and iterates session
data across versions; `notes` lays out the RO/RW directory structure a
session may touch.
"""
