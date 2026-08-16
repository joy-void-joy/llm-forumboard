"""Codex-specific capability implementations and composition roots.

The runtime side: ``app_server`` is the typed JSON-RPC stdio transport,
``runtime`` opens app-server sessions behind the :mod:`lup.runtime`
contracts, and ``config`` holds profile and compatible-endpoint transforms.
The harness side: ``harness`` renders canonical declarations into the
``.codex`` plugin tree (including the generated policy dispatcher),
``harness_runtime`` probes the CLI, verifies the separately installed plugin
cache, and installs it explicitly, and ``native`` decodes hook payloads into
:mod:`lup.policy` events and renders decisions back to the wire.

Every behavior class in this package fills a neutral library contract:
artifact, prompt, invocation, and probe capabilities from
:mod:`lup.harness.contracts`; session, turn, and binding capabilities from
:mod:`lup.runtime.contracts`; config transforms and profile resolution from
:mod:`lup.runtime.config`; and native event decoding and decision rendering
from :mod:`lup.policy.native`. Frozen Pydantic models are the adapter-owned
configuration and evidence data those implementations consume.
``create_codex_session_factory`` is the named runtime composition root;
module-level decoders, channels, and conversation state are its typed
internals.

Deliberately Codex-only, with no neutral contract:

- :class:`~lup.adapters.codex.app_server.CodexAppServer` is the JSON-RPC
  stdio transport to ``codex app-server``. The Claude counterpart is the
  external ``claude_agent_sdk`` package, so no second in-repository
  implementation exists to justify a transport contract.
- :class:`~lup.adapters.codex.harness_runtime.CodexPluginInstaller` and the
  plugin cache-evidence helpers install and digest-verify the separately
  cached plugin copy the Codex CLI executes. The Claude launcher runs the
  verified in-repository plugin directory in place, so an installer contract
  would have exactly one possible implementation.
"""
