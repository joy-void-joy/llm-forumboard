"""Codex's half of the compiled hook dispatcher.

:mod:`lup.policy.dispatcher` compiles this module together with the shared
host half into the plugin's `hooks/scripts/policy.py`, so this is not itself
a script. It holds only what Codex spells for itself: the environment naming
the home it installs trusted packages beneath, relativization against the
worktree rather than the launch directory, the tools it routes, the patch
envelope it decodes into per-file edits, and the fail-closed exit it takes
where the command-hook boundary offers no way to ask.

A call none of those three decode is put to the refusal table before it earns
the unclassified ask, so this file never names a tool it has no semantics for.
Which names a runtime offers is that runtime's own fact; which of them a
project has decided against is the application's — so the tools a refusal adds
to the routed set come from the declaration rather than from a list here.

The imports below resolve against the generated runtime the compiled script
sits beside, which is why this file is type-checked against that tree rather
than against the workspace.
"""

import json
import os
import sys
from pathlib import Path

# The hook is launched as a bare script, promised no cwd, PYTHONPATH, or
# interpreter environment, and `runtime/` is a plain sibling directory holding
# the kernel package and this plugin's policy data rather than an installed
# distribution. Naming it as a search path is what lets the imports below
# resolve, for the interpreter and for a type checker alike.
sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
from codex_patch import patched_files
from decisions import (
    bash_decision,
    edit_decision,
    fetch_decision,
    refused_tool_decision,
)
from host import (
    declared_identity,
    publish_edition,
    read_document,
    sandbox_active,
)
from kernel.decision import KernelDecision, SANDBOX_TRAPPED_REASON
from kernel.shell import auto_escape_matches
from policy_data import AUTO_ESCAPE_PREFIXES
from policy_data import AGENT_IDENTITY_ENV, AUTONOMOUS_AGENT_IDENTITIES


def managed_root():
    """The home Codex installs and trusts packages beneath."""
    environ = os.environ  # lup: ignore[os-environ]
    return Path(environ["CODEX_HOME"]) if "CODEX_HOME" in environ else None


def spent_escape(tool_input):
    """Whether this call requested Codex's per-command sandbox escape."""
    return (
        "sandbox_permissions" in tool_input
        and tool_input["sandbox_permissions"] == "require_escalated"
    )


def joined(decisions):
    """Join one envelope's files: deny beats ask beats defer beats allow."""
    for effect in ("deny", "ask", "defer"):
        for decision in decisions:
            if decision.effect == effect:
                return decision
    return KernelDecision("allow", "every patched file is declared safe")


def dispatch(payload, permission_request=False):
    name = payload["tool_name"]
    tool_input = payload["tool_input"]
    if name == "Bash":
        requested_escape = spent_escape(tool_input)
        escaped = requested_escape or auto_escape_matches(
            tool_input["command"], AUTO_ESCAPE_PREFIXES
        )
        decision = bash_decision(
            tool_input["command"],
            managed_root(),
            False if permission_request else sandbox_active(),
            interactive=permission_request,
            # A native prefix rule can auto-escape one simple command; an explicit
            # request is the other supported route. Both are checked against
            # semantic placement before the hook lets the native boundary act.
            escapable=escaped,
        )
        # PreToolUse can neither see nor place every native escape. Let Codex's
        # sandbox run a confined call or raise the PermissionRequest where this
        # same policy can judge the requested escape instead of preempting it.
        if not permission_request and decision.reason == SANDBOX_TRAPPED_REASON:
            return KernelDecision("defer", decision.reason)
        if (
            requested_escape
            and decision.effect == "allow"
            and decision.sandbox != "outside"
        ):
            return KernelDecision(
                "deny",
                f"call requested outside but policy places it {decision.sandbox}; remove sandbox_permissions and retry",
            )
        return decision
    if name == "web_fetch":
        return fetch_decision(tool_input["url"])
    if name == "apply_patch":
        autonomous = (
            declared_identity(AGENT_IDENTITY_ENV) in AUTONOMOUS_AGENT_IDENTITIES
        )
        return joined(
            [
                edit_decision(
                    change.path,
                    change.before,
                    change.after,
                    change.path_exists,
                    autonomous,
                )
                for change in patched_files(tool_input["command"], read_document)
            ]
        )
    # Asked of whatever reached here rather than of a listed few, exactly as
    # the Claude half asks it: which tools are worth refusing is the
    # declaration's answer, and a runtime that shipped the table without
    # consulting it would read as a refusal in force while the call went
    # through. The branches above keep their calls, which have semantics.
    refused = refused_tool_decision(
        name, [value for value in tool_input.values() if isinstance(value, str)]
    )
    if refused is not None:
        return refused
    return KernelDecision("ask", f"unknown tool {name!r} is not covered by policy")


def observe(payload):
    """Record which checkout an edit landed in, and decide nothing.

    Codex reads the directory it ran in, where Claude reads the edited file.
    Not a lesser answer here, and not an available one either way: Codex
    names its files inside the patch envelope, and the parser that decodes
    one validates its context against the document on disk — which this
    event runs after the patch already rewrote. Re-decoding it here would
    fail on exactly the edits it was called for. Codex hands over its
    working directory instead, which Claude's hook is promised nothing
    about, and that is the same fact one step coarser.

    Coarser is enough to say which checkout is being edited and not enough
    to type-check what changed, so Codex records the edition and Claude also
    reports diagnostics. Checking the directory instead would answer every
    patch with every finding in the tree, most of them about files this
    edit never touched.
    """
    root = payload["cwd"] if "cwd" in payload else ""
    if root:
        publish_edition(root)


def main():
    try:
        payload = json.load(sys.stdin)
        # Watching and deciding are separate events, and this one returns
        # before a verdict exists: the patch has already applied, so there is
        # nothing left to permit, and the fail-closed exit below would refuse
        # a call that already happened.
        event = payload["hook_event_name"] if "hook_event_name" in payload else ""
        if event == "PostToolUse":
            observe(payload)
            return
        permission_request = event == "PermissionRequest"
        # A verdict from here places nothing: this hook answers, and the call
        # runs with the arguments the model wrote, so a placement is degraded
        # to its plain effect rather than carrying an intent no channel here
        # performs. The agent's own escape is the other question and Codex
        # does have one, so a permission to escalate survives as reason text.
        decision = dispatch(payload, permission_request).placed(
            escapable=False, agent_escalates=True
        )
    # Every way this can fail means one thing — the call went unjudged — and
    # one answer is right for all of them. Naming the exceptions instead is
    # what let a plain unreadable file escape, and a traceback exit is not the
    # fail-closed exit this boundary takes, so the call proceeded ungoverned.
    # Nothing is swallowed: the reason carries whatever went wrong, and an
    # interrupt still passes through as the BaseException it is.
    except Exception as error:
        sys.stderr.write(f"Malformed hook input requires approval: {error}")
        raise SystemExit(2) from error
    if permission_request and decision.effect == "allow":
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "allow"},
                }
            },
            sys.stdout,
        )
        return
    if permission_request and decision.effect == "ask":
        return
    if decision.effect in ("allow", "defer"):
        return
    sys.stderr.write(decision.reason)
    raise SystemExit(2)
