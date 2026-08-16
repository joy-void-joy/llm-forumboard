"""Resilience primitives for calling flaky or rate-limited services.

`throttle` bounds concurrency and enforces a minimum interval between calls;
`retry` re-runs a coroutine with exponential backoff on transient failures.
"""
