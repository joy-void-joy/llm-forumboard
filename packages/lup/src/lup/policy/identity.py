"""The identity a launcher declares for the session it is about to start.

Edit autonomy is a property of *how a session was launched*, not of a name
one runtime happens to put in a hook payload. Claude Code fills `agent_type`
only for native subagent dispatch, so a resolver worker — an ordinary
top-level session — could never be recognized by it, and Codex has no such
field at all. Declaring the identity in the session environment lets the
actor that knows the claim to be true be the actor that makes it, on every
runtime.

This mirrors `LUP_SANDBOX_ACTIVE`: set exactly when the launcher verified
what it announces. A hook script is spawned by the runtime CLI with the
CLI's own environment, so an agent exporting this inside a shell tool call
cannot reach the dispatcher that judges it.
"""

from enum import StrEnum

from lup.types import EnvVars

# lup: ignore[constant-declaration] — the launcher that sets it and the hook that
# reads it are different processes, so the name is an identity, not a preference
AGENT_IDENTITY_ENV = "LUP_AGENT_IDENTITY"
"""Environment variable naming the declared identity of a launched session."""


class ConcernAllowance(StrEnum):
    """One edit gate a concern needs, which only a human can grant it.

    These gates exist because the decision is a human's. Naming what a plan
    needs at plan time moves that decision to where the human is already
    deciding, instead of parking the run to ask again for work they just
    approved — and a need nobody could have foreseen is asked for mid-lease
    and granted to the session that asked.

    This enum is the vocabulary's single source of truth: whoever grants a
    gate names it from here, the compiled dispatchers honour exactly its
    members, and a name outside it is dropped rather than trusted.

    Where a lease's current grants live, and why they cannot live here, is
    :mod:`lup.policy.grants`: a gate is granted while the session it is
    granted to is already running, so unlike an identity it is not something
    a launcher can settle in advance.
    """

    NEW_DEVTOOLS_MODULE = "new-devtools-module"
    ANTIPATTERN_SUPPRESSION = "antipattern-suppression"


def agent_identity_environment(identity: str) -> EnvVars:
    """Declare one session's identity, or clear it with an empty name.

    Non-autonomous sessions must set the empty value rather than omit the
    variable: runtimes merge a session's environment over the launching
    process's, so an operator with this exported would otherwise hand their
    own autonomy to a session that was never granted it.
    """
    return {AGENT_IDENTITY_ENV: identity}
