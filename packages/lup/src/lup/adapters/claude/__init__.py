"""Claude-specific capability implementations and composition roots.

The runtime side: ``runtime`` opens Claude SDK sessions behind the
:mod:`lup.runtime` contracts, ``config`` holds profile and compatible-endpoint
transforms, and ``profile_store`` is the personal account registry the CLI
composition roots read. The harness side: ``harness`` renders canonical
declarations into the ``.claude`` plugin tree (including the generated policy
dispatcher), ``harness_runtime`` probes the installed CLI for doctor
evidence, ``native`` decodes hook payloads into :mod:`lup.policy` events and
renders decisions back to the wire, and ``hooks`` translates neutral hook
configs into SDK handlers.

Every behavior class in this package fills a neutral library contract:
artifact, prompt, invocation, and probe capabilities from
:mod:`lup.harness.contracts`; session, turn, and binding capabilities from
:mod:`lup.runtime.contracts`; config transforms and profile resolution from
:mod:`lup.runtime.config`; and native event decoding and decision rendering
from :mod:`lup.policy.native`. Frozen Pydantic models are the adapter-owned
configuration and evidence data those implementations consume.
``create_claude_session_factory`` is the named runtime composition root;
module-level builders, converters, and conversation state are its typed
internals.

Deliberately Claude-only, with no neutral contract:

- :class:`~lup.adapters.claude.profile_store.AccountFile` persists
  personal named config-directory selections because the Claude CLI has no
  native profile registry. It projects into ``ClaudeProfileRegistry``, which
  the ``ProfileResolver`` filling consumes. The Codex CLI owns account homes
  and named config overlays natively, so no Codex counterpart exists.
- :mod:`~lup.adapters.claude.hooks` translates portable Lup hooks into
  in-process SDK hook callbacks, a mechanism only the Claude SDK exposes;
  Codex hooks exist solely as generated plugin command artifacts.
"""
