"""The gates a lease holds, written where a judgment can read them back.

An identity is settled before a session starts and stays true for as long as
it runs, so the environment carries it. A grant is not like that: a worker
discovers mid-flight that it needs a gate, asks, and a human answers while
the session that asked is already running. Rendered into the environment the
answer had nowhere to land — the only channel for it belonged to a process
that had already started — so the ask and the grant were separated by a
restart nobody planned for.

So the grant lives in a document, and the environment carries only where that
document is. The reader is a hook: a separate process the runtime launches
before a tool call, whose kernel is compiled into both plugin trees and must
not learn what a resolver or a concern is. It can read a file, which is all
this asks of it. One document per lease is what makes a grant answer for the
lease it was made for and no other; reading it at judgment rather than at
launch is what makes an answer that arrives mid-lease arrive at all.

Both directions govern. A name removed from the document stops releasing its
gate at the next judgment, because the document is read then and not before —
which is what lets a grant made in error be retracted while it still matters.
Whoever composes the session decides what a narrowing means:
:class:`LeaseGrants` reads and answers, and a composition that wants a
withdrawal to be loud rather than silent says so by supplying its own.
"""

import json
from pathlib import Path

from lup.channels.models import write_atomic
from lup.policy.assets.host import document_allowances
from lup.policy.identity import ConcernAllowance
from lup.types import EnvVars

# lup: ignore[constant-declaration] — the launcher that sets it and the hook
# that reads it are different processes, so the name is an identity the two
# have to spell alike, not a preference either of them holds
ALLOWANCE_GRANTS_ENV = "LUP_ALLOWANCE_GRANTS"
"""Environment variable naming the document a session's grants are read from.

The path rather than the grants. A stale environment then names a document
whose contents are current, so it has nothing to disagree with — where a
stale copy of the grants themselves was a second answer to a question that
has one.
"""


def known_allowances() -> list[str]:
    """Every gate name a document may legitimately carry.

    The enum is the vocabulary's single source of truth, so a name outside it
    is dropped rather than trusted and hand-writing one into a document buys
    nothing.
    """
    return [member.value for member in ConcernAllowance]


def allowance_grants_environment(document: Path | None) -> EnvVars:
    """Point a session at the document its grants are read from, or at none.

    Written on every session, never omitted: runtimes merge a session's
    environment over the launching process's, so a session that stayed silent
    would read whatever document an operator happened to have exported and
    inherit grants made to somebody else's lease.
    """
    return {ALLOWANCE_GRANTS_ENV: "" if document is None else str(document)}


def write_allowance_grants(document: Path, allowances: list[ConcernAllowance]) -> None:
    """Publish the gates one lease holds, as a reader can only see whole.

    The readers are separate processes judging tool calls at whatever moment
    they arrive, and one that caught a half-written document would read no
    grants and refuse work a human had approved — so this goes through the
    library's atomic write like every other file a concurrent reader may
    catch.
    """
    write_atomic(
        document,
        json.dumps([allowance.value for allowance in allowances]).encode("utf-8"),
    )


def read_allowance_grants(document: Path | None) -> list[str]:
    """The gates a document currently grants, through the readers' one reader.

    :func:`~lup.policy.assets.host.document_allowances` is the half compiled
    into both generated dispatchers, so the canonical policy and the deployed
    hooks answer "what does this lease hold" with the same code over the same
    file rather than with two readings that can drift.
    """
    return document_allowances(
        "" if document is None else str(document), known_allowances()
    )


class LeaseGrants:
    """One lease's gates, answered afresh every time a gate is judged.

    Held by the policy rather than read into it, so the answer is the
    document's current contents at the moment of the call and not a list
    copied out of it when the judge was built. A judge built once and asked a
    hundred times is how a session-long policy object came to hold a grant
    that had been withdrawn, and how it could not hold one that arrived.

    Carrying no document is a lease with no grants rather than an error: most
    sessions are not leases, and they see the unchanged lattice.
    """

    def __init__(self, document: Path | None = None) -> None:
        self.document = document

    def granted(self) -> list[str]:
        """The gates this lease holds right now."""
        return read_allowance_grants(self.document)
