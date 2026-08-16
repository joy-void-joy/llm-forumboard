"""Claude-native prompt and artifact renderers."""

import json
import shlex
from collections.abc import Sequence
from pathlib import Path
from lup.adapters.claude.login import CLAUDE_LOGIN
from lup.codescan.antipatterns import DOCUMENT_IN_HAND, antipattern_set_for
from lup.harness.banner import PROMPT_TEXT, VERBATIM_COPY
from lup.harness.contracts import (
    ArtifactRenderer,
    Atom,
    Instruction,
    NativeSpellings,
    PromptRenderer,
    Spelled,
    Spelling,
)
from lup.harness.generation import argument_text
from lup.harness.prompts import (
    SPAWNED_SESSION_LOSES_SHELL,
    guidance_banner,
    sentences,
)
from lup.harness.models import (
    Agent,
    Artifact,
    ArtifactTree,
    Harness,
    HookSet,
    ModelTier,
    Plugin,
    PluginLocation,
    QualifiedAgentName,
    Skill,
    SkillInvocation,
    TreeLocation,
)
from lup.policy.bundle import (
    POLICY_DATA_BANNER,
    policy_kernel_modules,
    render_policy_data,
    runtime_url_scope,
)
from lup.policy.dispatcher import (
    DispatcherDeclaration,
    compile_dispatcher,
    dispatcher_banner,
)
from lup.policy.kernel.rows import PathRoleRow
from lup.policy.refused_tools import routed_for


# lup: ignore[constant-declaration] — each value is Claude Code's own alias for
# the tier beside it, over a tier vocabulary this library closes
CLAUDE_MODEL_ALIASES: dict[ModelTier, str] = {
    "inherit": "inherit",
    "strongest": "opus",
    "balanced": "sonnet",
    "fast": "haiku",
}
"""Claude's own name for each portable tier, as agent frontmatter accepts it."""


class ClaudeSpellings(NativeSpellings):
    """Spell everything portable prose names the way Claude Code spells it."""

    @property
    def runtime_name(self) -> Atom:
        return Atom("Claude Code")

    @property
    def native_identifiers(self) -> list[Atom]:
        return [
            Atom("AskUserQuestion"),
            Atom("subagent_type"),
            Atom("EnterWorktree"),
            Atom("ExitWorktree"),
            Atom("docs.claude.com"),
            Atom("code.claude.com"),
        ]

    def render(self, invocation: SkillInvocation) -> str:
        command = f"/{invocation.plugin}:{invocation.skill}"
        arguments = " ".join(
            f"{argument.name}={shlex.quote(argument_text(argument.value))}"
            for argument in invocation.arguments
        )
        return f"{command} {arguments}" if arguments else command

    def invocation_pattern(self, plugin: str, placeholder: str) -> Atom:
        return Atom(f"/{plugin}:{placeholder}")

    def ask_user(self, question: str) -> Instruction:
        return Instruction(
            "Ask the user with the AskUserQuestion tool, offering concrete "
            f"options plus a free-text choice: {question}"
        )

    def delegate(self, subagent_type: QualifiedAgentName, prompt: str) -> Instruction:
        return Instruction(
            f"Delegate with Agent(subagent_type={json.dumps(subagent_type)}"
            f", prompt={json.dumps(prompt)})"
        )

    def request_approval(self, action: str, reason: str) -> Instruction:
        return Instruction(
            f"Request explicit user approval before {action}. Reason: {reason}."
        )

    def relocate_session(self, path: str) -> Instruction:
        return Instruction(
            f"`EnterWorktree(path=<{path}>)`, returning afterwards "
            'with `ExitWorktree(action="keep")`'
        )

    def escape_sandbox(self, reason: str) -> Spelling:
        return Spelled(
            words=Instruction(
                f"Launch it with `dangerouslyDisableSandbox: true`. {reason}"
            )
        )

    def read_document(self, path: str) -> Spelling:
        return Spelled(
            words=Instruction(
                f"Hand {path} to the `Read` tool, which takes the document itself"
            )
        )

    def resolver_entry(self) -> Instruction:
        return Instruction(
            sentences(
                "Run `uv run lup-devtools harness resolve --adapter claude --detach`. "
                "`uv run lup-devtools harness resolve intake` first prints what a "
                "run started now would plan from — every actionable note at its "
                "file and line, the deferred ones it would carry, and the ones it "
                "would leave to their generator — creating no run and leasing no "
                "worktree, so the inventory can be read before committing to it. "
                "Where this project has a run that never finished, it refuses "
                "rather than starting a second one beside it, and lists them: "
                "relay that choice — resume with `--run-id <id>`, keeping every "
                "answer already collected, or start fresh with `--new`, which "
                "re-derives the inventory and discards them. Never take it "
                "yourself. Assembling the review branch is gated on the "
                "reserved `integration-assembly` question, so approving it is "
                "`--answer integration-assembly=approve` like any other answer. "
                "It waits zero seconds by "
                "default and parks on material questions, printing each one "
                "beside the `# lup:` notes it was raised from, the concern's "
                "spec, and its acceptance criteria; rerun with the repeatable "
                "`--answer <question-id>=<value>` flag to answer them. "
                "`--admit <text>` carries work in the human's own words: it seeds "
                "a run that does not exist yet, beside whatever notes the tree "
                "holds, and joins one already moving. `--admit-note <file>:<line>` "
                "names a note written in the tree and `--admit-issue <number>` an "
                "open issue; all three are repeatable. "
                "Never pass `--wait` or `--supervise`; both hold a run open "
                "for a human instead of parking — `--wait` at the mailbox, "
                "`--supervise` at the page it opens.",
                self.escape_sandbox(SPAWNED_SESSION_LOSES_SHELL).in_prose(),
            )
        )

    def arguments_ref(self) -> Atom:
        return Atom("$ARGUMENTS")

    def runtime_docs(self) -> Instruction:
        return Instruction(
            "the Claude Code and Agent SDK documentation at "
            "https://docs.claude.com/ and https://code.claude.com/"
        )

    def project_root(self) -> str:
        # Claude Code substitutes this into a plugin-provided MCP command
        # without needing a default, so a server reaches the repository it
        # serves from whichever scope the plugin was installed in — the local
        # directory a launch verifies in place, or the marketplace cache.
        return "${CLAUDE_PROJECT_DIR}"

    def model_alias(self, tier: ModelTier) -> str | None:
        return CLAUDE_MODEL_ALIASES[tier]

    def tree(self, location: TreeLocation) -> Atom:
        match location:
            case "tree_root":
                return Atom(".claude/")
            case "guidance_file":
                return Atom(".claude/CLAUDE.md")
            case "ownership_manifest":
                return Atom(".claude/.lup-ownership.json")
            case "project_settings":
                return Atom(".claude/settings.json")
            case "personal_settings":
                return Atom(".claude/settings.local.json")
            case "marketplace":
                return Atom(".claude/plugins/.claude-plugin/marketplace.json")

    def plugin(self, plugin: str, location: PluginLocation, member: str | None) -> Atom:
        root = f".claude/plugins/{plugin}"
        match location:
            case "root":
                return Atom(f"{root}/")
            case "manifest":
                return Atom(f"{root}/.claude-plugin/plugin.json")
            case "skills":
                leaf = f"commands/{member}.md" if member else "commands/"
                return Atom(f"{root}/{leaf}")
            case "agents":
                leaf = f"agents/{member}.md" if member else "agents/"
                return Atom(f"{root}/{leaf}")
            case "hooks":
                return Atom(f"{root}/hooks/")
            case "guidance_template":
                return Atom(f"{root}/TEMPLATE_CLAUDE.md")


# lup: ignore[constant-declaration] — which built-ins Claude Code ships is the
# runtime's fact to state, not a preference a caller holds
CLAUDE_ABSENT_TOOLS = ("Glob", "Grep")
"""Built-ins the portable vocabulary names that Claude Code does not ship.

The vocabulary is deliberately a superset — adapters translate their own
tool identities onto it — so a name in it is a request, not a promise. A
granted name the runtime has no tool for grants nothing while reading as a
capability the agent has, and an agent that believes it spends a turn per
attempt discovering otherwise. Filtered where the runtime is known, so a
declaration keeps naming the portable set and a runtime that ships these
again needs one edit here rather than one per declaration.
"""


def claude_granted_tools(tools: Sequence[str]) -> list[str]:
    """Keep only the grants this runtime can actually honor."""
    return [tool for tool in tools if tool not in CLAUDE_ABSENT_TOOLS]


class ClaudeSkillRenderer(ArtifactRenderer[Skill]):
    """Render one portable skill as a Claude command Markdown artifact."""

    def __init__(self, prompts: PromptRenderer, plugin_name: str) -> None:
        self.prompts = prompts
        self.plugin_name = plugin_name

    def render(self, source: Skill) -> ArtifactTree:
        frontmatter = [f"description: {json.dumps(source.description)}"]
        granted = claude_granted_tools(source.tools)
        if granted:
            frontmatter.append("allowed-tools: " + ", ".join(granted))
        if source.argument_hint is not None:
            frontmatter.append(f"argument-hint: {json.dumps(source.argument_hint)}")
        elif source.arguments:
            arguments = "\n".join(
                f"  - name: {argument.name}\n"
                f"    description: {json.dumps(argument.description)}\n"
                f"    required: {str(argument.required).lower()}"
                for argument in source.arguments
            )
            frontmatter.append(f"arguments:\n{arguments}")
        content = (
            "---\n" + "\n".join(frontmatter) + "\n"
            "---\n\n"
            f"{self.prompts.render(source.prompt)}"
        )
        return ArtifactTree(
            artifacts=[
                Artifact(
                    path=Path(
                        f".claude/plugins/{self.plugin_name}/commands/{source.name}.md"
                    ),
                    content=content,
                    semantic_id=source.id,
                    banner=PROMPT_TEXT,
                )
            ]
        )


class ClaudeAgentRenderer(ArtifactRenderer[Agent]):
    """Render one portable agent in Claude's Markdown format."""

    def __init__(
        self, prompts: PromptRenderer, plugin_name: str, spellings: NativeSpellings
    ) -> None:
        self.prompts = prompts
        self.plugin_name = plugin_name
        self.spellings = spellings

    def render(self, source: Agent) -> ArtifactTree:
        tools = ", ".join(claude_granted_tools(source.tools))
        alias = (
            None if source.model is None else self.spellings.model_alias(source.model)
        )
        model = f"model: {alias}\n" if alias is not None else ""
        color = f"color: {source.color}\n" if source.color is not None else ""
        content = (
            "---\n"
            f"name: {source.name}\n"
            f"description: {json.dumps(source.description)}\n"
            f"tools: {tools}\n"
            f"{model}"
            f"{color}"
            "---\n\n"
            f"{self.prompts.render(source.prompt)}"
        )
        return ArtifactTree(
            artifacts=[
                Artifact(
                    path=Path(
                        f".claude/plugins/{self.plugin_name}/agents/{source.name}.md"
                    ),
                    content=content,
                    semantic_id=source.id,
                    banner=PROMPT_TEXT,
                )
            ]
        )


class ClaudePluginManifestRenderer(ArtifactRenderer[Plugin]):
    """Render Claude plugin metadata without importing shared native names."""

    def render(self, source: Plugin) -> ArtifactTree:
        payload = {
            "name": source.name,
            "version": source.version,
            "description": source.description,
        }
        marketplace = {
            "name": source.marketplace,
            "owner": {"name": "Lup"},
            "plugins": [
                {
                    "name": source.name,
                    "description": source.description,
                    "source": f"./{source.name}",
                }
            ],
        }
        return ArtifactTree(
            artifacts=[
                Artifact(
                    path=Path(
                        f".claude/plugins/{source.name}/.claude-plugin/plugin.json"
                    ),
                    content=json.dumps(payload, indent=2, sort_keys=True),
                    semantic_id=source.id,
                ),
                Artifact(
                    path=Path(".claude/plugins/.claude-plugin/marketplace.json"),
                    content=json.dumps(marketplace, indent=2, sort_keys=True),
                    semantic_id=source.id,
                ),
            ]
        )


class ClaudeMcpRenderer(ArtifactRenderer[Plugin]):
    """Render a plugin's tool servers where Claude Code reads a plugin's own.

    A plugin-provided configuration is the scope that follows the plugin: it
    starts with the plugin rather than asking the project to enable it, and it
    is the one scope whose commands substitute the project root.
    """

    def __init__(self, spellings: NativeSpellings) -> None:
        self.spellings = spellings

    def render(self, source: Plugin) -> ArtifactTree:
        servers = {
            server.name: {
                "command": server.command,
                "args": server.command_line(self.spellings),
            }
            for server in source.mcp_servers
        }
        return ArtifactTree(
            artifacts=[
                Artifact(
                    path=Path(f".claude/plugins/{source.name}/.mcp.json"),
                    content=json.dumps(
                        {"mcpServers": servers}, indent=2, sort_keys=True
                    ),
                    semantic_id=source.id,
                )
            ]
        )


class ClaudeGuidanceRenderer(ArtifactRenderer[Harness]):
    """Render project guidance at Claude's adapter-owned repository location."""

    def __init__(self, prompts: PromptRenderer) -> None:
        self.prompts = prompts

    def render(self, source: Harness) -> ArtifactTree:
        return ArtifactTree(
            artifacts=[
                Artifact.generated(
                    path=Path(".claude/CLAUDE.md"),
                    body=self.prompts.render(source.guidance),
                    semantic_id="harness.guidance",
                    banner=guidance_banner(self.prompts, source.guidance),
                )
            ]
        )


CLAUDE_DISPATCHER = DispatcherDeclaration(
    runtime_name="Claude Code",
    package="lup.adapters.claude",
    managed_root_env=CLAUDE_LOGIN.config_home_env,
    routed_tools=["Bash", "WebFetch", "Edit", "Write"],
    hook_events=["PreToolUse", "PostToolUse"],
    observation_event="PostToolUse",
    observed_tools=["Edit", "Write"],
    failure="conservative_ask",
    runtime_modules=["policy_data"],
)
"""Everything Claude Code spells differently from every other runtime.

The tools named here are both what the plugin registers the hook for and
what the compiler proves the dispatcher routes, so a tool cannot be handed
to the hook without a branch that decides it.
"""


class ClaudeHookRenderer(ArtifactRenderer[HookSet]):
    """Render Claude hooks, canonical kernel, and application policy rows."""

    def __init__(
        self, plugin_name: str, worker_identity: str, spellings: NativeSpellings
    ) -> None:
        self.plugin_name = plugin_name
        self.worker_identity = worker_identity
        self.spellings = spellings

    def render(self, source: HookSet) -> ArtifactTree:
        command = [
            {
                "type": "command",
                "command": 'python3 "$CLAUDE_PLUGIN_ROOT/hooks/scripts/policy.py"',
                "timeout": 30,
            }
        ]
        decided = [
            {
                "matcher": "|".join(
                    routed_for(CLAUDE_DISPATCHER.routed_tools, source.refused_tools)
                ),
                "hooks": command,
            }
        ]
        observed = [
            {
                "matcher": "|".join(CLAUDE_DISPATCHER.observed_tools),
                "hooks": command,
            }
        ]
        hooks = {
            "description": "Lup semantic permission policy",
            "hooks": {
                event: (
                    observed
                    if event == CLAUDE_DISPATCHER.observation_event
                    else decided
                )
                for event in CLAUDE_DISPATCHER.hook_events
            },
        }
        evidence = {"schemaVersion": 1, "policyIds": source.policy_ids}
        return ArtifactTree(
            artifacts=[
                Artifact(
                    path=Path(f".claude/plugins/{self.plugin_name}/hooks/hooks.json"),
                    content=json.dumps(hooks, indent=2, sort_keys=True),
                    semantic_id=source.id,
                ),
                Artifact(
                    path=Path(
                        f".claude/plugins/{self.plugin_name}/hooks/scripts/policy.py"
                    ),
                    content=compile_dispatcher(CLAUDE_DISPATCHER),
                    semantic_id=source.id,
                    executable=True,
                    banner=dispatcher_banner(CLAUDE_DISPATCHER),
                ),
                *[
                    Artifact(
                        path=Path(
                            f".claude/plugins/{self.plugin_name}/hooks/runtime/"
                            f"kernel/{module.name}"
                        ),
                        content=module.source,
                        semantic_id=source.id,
                        banner=VERBATIM_COPY,
                    )
                    for module in policy_kernel_modules()
                ],
                Artifact.generated(
                    path=Path(
                        f".claude/plugins/{self.plugin_name}/hooks/runtime/"
                        "policy_data.py"
                    ),
                    banner=POLICY_DATA_BANNER,
                    body=render_policy_data(
                        allowed_fetch_scopes=[
                            runtime_url_scope(
                                str(scope.origin),
                                scope.path_prefix,
                                reason=scope.reason,
                                include_subdomains=scope.include_subdomains,
                            )
                            for scope in source.allowed_fetch
                        ],
                        denied_fetch_scopes=[
                            runtime_url_scope(
                                str(scope.origin),
                                scope.path_prefix,
                                reason=scope.reason,
                                include_subdomains=scope.include_subdomains,
                            )
                            for scope in source.denied_fetch
                        ],
                        protected_roots=[
                            path.as_posix() for path in source.protected_edit_roots
                        ],
                        human_owned_files=[
                            path.as_posix() for path in source.human_owned_files
                        ],
                        autonomous_agent_identities=[
                            self.worker_identity,
                            f"{self.plugin_name}:{self.worker_identity}",
                        ],
                        path_roles=[
                            PathRoleRow(root=role.root.as_posix(), role=role.role)
                            for role in source.path_roles
                        ],
                        shell_rules=list(source.shell_rules),
                        refused_tools=list(source.refused_tools),
                        recoverable_target_limit=source.recoverable_target_limit,
                        runner_targets=list(source.runner_targets),
                        sandbox_excluded_commands=source.excluded_commands(),
                        auto_escape_prefixes=[],
                        diagnostics_command=source.diagnostics_command,
                        rules=antipattern_set_for(
                            self.spellings.read_document(DOCUMENT_IN_HAND),
                            source.rules,
                        ),
                    ),
                    semantic_id=source.id,
                ),
                Artifact(
                    path=Path(
                        f".claude/plugins/{self.plugin_name}/hooks/runtime/evidence.json"
                    ),
                    content=json.dumps(evidence, indent=2, sort_keys=True),
                    semantic_id=source.id,
                ),
            ]
        )
