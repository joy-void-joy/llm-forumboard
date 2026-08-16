"""Telemetry: what a run records about itself for later analysis.

`trace` accumulates the markdown session trace and its machine-readable
event sidecar; `display` renders content blocks to the console with
color-coded tool pairing; `blocks` holds the block-extraction and
truncation helpers the two share. `metrics` tracks per-tool call counts and
timings, with a file-backed flush for subprocess tools.
"""
