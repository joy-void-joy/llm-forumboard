"""The usage display, and the report shape every runtime fills it from.

An account's metered windows and its day-by-day tokens are the same two
questions on every runtime that bills a subscription, so the report, the
pacing bars, the machine-readable snapshot, and the three display modes live
here once. What differs — a credential, an endpoint, a wire shape, how a
model family is named — reaches this through a :class:`~lup.usage.models
.UsageReader` in the adapter that owns it.
"""
