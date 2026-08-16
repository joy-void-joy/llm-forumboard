"""Codex-native prompt, plugin, skill, agent, and guidance renderers."""

import json
import shlex
from collections.abc import Collection
from importlib import resources
from pathlib import Path

import tomlkit
from lup.adapters.codex.login import CODEX_LOGIN
from lup.codescan.antipatterns import DOCUMENT_IN_HAND, antipattern_set_for
from lup.harness.banner import (
    PROMPT_TEXT,
    REGENERATE_COMMAND,
    VERBATIM_COPY,
    GeneratedBanner,
)
from lup.harness.contracts import (
    ArtifactRenderer,
    Atom,
    Instruction,
    NativeSpellings,
    PromptRenderer,
    Spelled,
    Spelling,
    Unsupported,
)
from lup.harness.generation import argument_text
from lup.harness.prompts import (
    SPAWNED_SESSION_LOSES_SHELL,
    guidance_banner,
    sentences,
)
from lup.harness.models import (
    GUIDANCE_BYTE_BUDGET,
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
from lup.policy.kernel.decision import SandboxPlacement
from lup.policy.kernel.rows import PathRoleRow
from lup.policy.kernel.shell import sandbox_excluded
from lup.policy.refused_tools import routed_for
from lup.policy.kernel.words import (
    INTERPRETERS,
    PASS_THROUGH_WORDS,
)
from lup.policy.shell_rules import (
    RunnerTargetRule,
    ShellCommandRule,
    erase_shell_rules,
)


class CodexSpellings(NativeSpellings):
    """Spell everything portable prose names the way Codex spells it.

    Custom agents are the one location Codex keeps outside the plugin, so the
    plugin name is deliberately unused there. The model tier is declined
    outright: recorded evidence for Codex custom agents covers TOML parsing
    only, so there is no proven alias to spell a tier in.

    Reading a document whole is declined outright rather than approximated,
    saying why: nothing in this roster does it, and an approximation would
    read as an instruction the agent can follow and cost it a turn to find
    out otherwise.
    """

    @property
    def runtime_name(self) -> Atom:
        return Atom("Codex")

    @property
    def native_identifiers(self) -> list[Atom]:
        return [Atom("developers.openai.com"), Atom("learn.chatgpt.com")]

    def render(self, invocation: SkillInvocation) -> str:
        mention = f"${invocation.plugin}:{invocation.skill}"
        arguments = " ".join(
            f"{argument.name}={shlex.quote(argument_text(argument.value))}"
            for argument in invocation.arguments
        )
        return f"{mention} {arguments}" if arguments else mention

    def invocation_pattern(self, plugin: str, placeholder: str) -> Atom:
        return Atom(f"${plugin}:{placeholder}")

    def ask_user(self, question: str) -> Instruction:
        return Instruction(
            "Ask the user directly, offering concrete options, and wait "
            f"for the answer: {question}"
        )

    def delegate(self, subagent_type: QualifiedAgentName, prompt: str) -> Instruction:
        return Instruction(
            f"Delegate to the {subagent_type} custom agent with this task: {prompt}"
        )

    def request_approval(self, action: str, reason: str) -> Instruction:
        return Instruction(
            f"Request explicit user approval before {action}. Reason: {reason}."
        )

    def relocate_session(self, path: str) -> Instruction:
        return Instruction(
            f"start a session rooted at <{path}> and continue there — "
            "this runtime cannot move a running session, so work "
            "carried on here would land in the checkout it started from"
        )

    def escape_sandbox(self, reason: str) -> Spelling:
        """Spell the per-call escape Codex puts in the model's own hands.

        Read out of the source at tag ``rust-v0.145.0`` — the release
        ``docs/native-capabilities.md`` pins — rather than out of the prose
        pages, which describe the session-level flags and leave this one
        unmentioned. ``codex-rs/core/src/tools/handlers/shell_spec.rs`` puts
        ``sandbox_permissions`` on the shell tool the model calls, describing
        it to the model as a per-command sandbox override whose
        ``require_escalated`` value means unsandboxed execution, beside the
        ``justification`` the approval policy records; the value is pushed into
        that enum unconditionally, so it is
        offered on every session.
        ``codex-rs/core/src/tools/sandboxing.rs`` is where it lands, mapping
        ``requires_escalated_permissions()`` to a first attempt that bypasses
        the sandbox. Both paths are re-derivable at that tag, which is the
        point of naming them: the finding is checkable rather than inherited.

        This is the agent's route and not a hook's — a Codex policy verdict
        still places nothing, which is why
        :data:`~lup.adapters.codex.hooks.CODEX_SEMANTICS` splits the two.
        """
        return Spelled(
            words=Instruction(
                "Issue it on the first attempt with `sandbox_permissions` set to "
                "`require_escalated`, and a `justification` saying why. A "
                "semantically allowed command is auto-approved; do not add a "
                f"shell `# lup: escalate` marker. {reason}"
            )
        )

    def read_document(self, path: str) -> Spelling:
        return Unsupported(
            reason=(
                "Codex's tool roster reads a document only by running a shell "
                "command over it, and `view_image` — the one tool that takes a "
                "file whole — accepts images alone, so nothing here can be "
                "handed a PDF or an office document"
            )
        )

    def resolver_entry(self) -> Instruction:
        return Instruction(
            sentences(
                "Run `uv run lup-devtools harness resolve --adapter codex --detach`. "
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
        return Atom("the arguments supplied with this skill invocation")

    def runtime_docs(self) -> Instruction:
        return Instruction(
            "the Codex documentation at "
            "https://developers.openai.com/codex/ and "
            "https://learn.chatgpt.com/"
        )

    def project_root(self) -> str:
        # Codex substitutes nothing into a server command, but it reads this
        # config only for the project the config sits in, so the launch
        # directory is that project by construction. Naming it explicitly is
        # what makes a server started anywhere else fail instead of resolving
        # up the tree into a neighbouring checkout.
        return "."

    def model_alias(self, tier: ModelTier) -> str | None:
        return None

    def tree(self, location: TreeLocation) -> Atom:
        match location:
            case "tree_root":
                return Atom(".codex/")
            case "guidance_file":
                return Atom("AGENTS.md")
            case "ownership_manifest":
                return Atom(".codex/.lup-ownership.json")
            case "project_settings":
                return Atom(".codex/config.toml")
            case "personal_settings":
                return Atom(".codex/config.local.toml")
            case "marketplace":
                return Atom(".agents/plugins/marketplace.json")

    def plugin(self, plugin: str, location: PluginLocation, member: str | None) -> Atom:
        root = f".codex/plugins/{plugin}"
        match location:
            case "root":
                return Atom(f"{root}/")
            case "manifest":
                return Atom(f"{root}/.codex-plugin/plugin.json")
            case "skills":
                leaf = f"skills/{member}/SKILL.md" if member else "skills/"
                return Atom(f"{root}/{leaf}")
            case "agents":
                leaf = f"agents/{member}.toml" if member else "agents/"
                return Atom(f".codex/{leaf}")
            case "hooks":
                return Atom(f"{root}/hooks/")
            case "guidance_template":
                return Atom(f"{root}/TEMPLATE_AGENTS.md")


class CodexSkillRenderer(ArtifactRenderer[Skill]):
    """Render one portable declaration as a same-named Codex skill."""

    def __init__(self, prompts: PromptRenderer, plugin_name: str) -> None:
        self.prompts = prompts
        self.plugin_name = plugin_name

    def render(self, source: Skill) -> ArtifactTree:
        content = (
            "---\n"
            f"name: {source.name}\n"
            f"description: {json.dumps(source.description)}\n"
            "---\n\n"
            f"{self.prompts.render(source.prompt)}"
        )
        return ArtifactTree(
            artifacts=[
                Artifact(
                    path=Path(
                        f".codex/plugins/{self.plugin_name}/skills/{source.name}/SKILL.md"
                    ),
                    content=content,
                    semantic_id=source.id,
                    banner=PROMPT_TEXT,
                )
            ]
        )


class CodexAgentRenderer(ArtifactRenderer[Agent]):
    """Render one portable agent as project-scoped custom-agent TOML.

    The model row appears only where the vocabulary can spell the declared
    tier, and this one declines: omitting it lets the agent inherit the session
    model, which is honest where naming another runtime's alias would not be.
    """

    def __init__(self, prompts: PromptRenderer, spellings: NativeSpellings) -> None:
        self.prompts = prompts
        self.spellings = spellings

    def render(self, source: Agent) -> ArtifactTree:
        alias = (
            None if source.model is None else self.spellings.model_alias(source.model)
        )
        rows = [
            f"name = {json.dumps(source.name)}",
            f"description = {json.dumps(source.description)}",
            (
                "developer_instructions = "
                f"{json.dumps(self.prompts.render(source.prompt))}"
            ),
        ]
        if alias is not None:
            rows.append(f"model = {json.dumps(alias)}")
        return ArtifactTree(
            artifacts=[
                Artifact.generated(
                    path=Path(f".codex/agents/{source.name}.toml"),
                    body="\n".join(rows),
                    semantic_id=source.id,
                    banner=GeneratedBanner(
                        source=f"the portable agent declaration {source.id}",
                        command=REGENERATE_COMMAND,
                    ),
                )
            ]
        )


class CodexPluginManifestRenderer(ArtifactRenderer[Plugin]):
    """Render the required Codex manifest and repository marketplace entry."""

    def render(self, source: Plugin) -> ArtifactTree:
        manifest = {
            "name": source.name,
            "version": source.version,
            "description": source.description,
            "skills": "./skills/",
        }
        if source.hooks is not None:
            manifest["hooks"] = "./hooks/hooks.json"
        marketplace = {
            "name": source.marketplace,
            "plugins": [
                {
                    "name": source.name,
                    "category": "development",
                    "interface": {"displayName": "Lup"},
                    "source": {
                        "source": "local",
                        "path": f"./.codex/plugins/{source.name}",
                    },
                    "policy": {
                        "installation": "AVAILABLE",
                        "authentication": "ON_INSTALL",
                    },
                }
            ],
        }
        return ArtifactTree(
            artifacts=[
                Artifact(
                    path=Path(
                        f".codex/plugins/{source.name}/.codex-plugin/plugin.json"
                    ),
                    content=json.dumps(manifest, indent=2, sort_keys=True),
                    semantic_id=source.id,
                ),
                Artifact(
                    path=Path(".agents/plugins/marketplace.json"),
                    content=json.dumps(marketplace, indent=2, sort_keys=True),
                    semantic_id=source.id,
                ),
            ]
        )


def codex_project_config(
    source: Harness, spellings: NativeSpellings, budget: int = GUIDANCE_BYTE_BUDGET
) -> str:
    """Render the project config: enabled features, then every tool server.

    Codex keeps a project's servers in the same file as the rest of its
    project configuration, so this is one document rather than the separate
    artifact the other runtime reads.

    The guidance ceiling generation already enforces is restated here as
    ``project_doc_max_bytes``: the runtime truncates project guidance at its
    own default, so a document that passed generation would still reach the
    model short if the two disagreed.
    """
    document = tomlkit.document()
    features = tomlkit.table()
    features["hooks"] = True
    document["features"] = features
    document["project_doc_max_bytes"] = budget
    servers = tomlkit.table(is_super_table=True)
    for plugin in source.plugins:
        for server in plugin.mcp_servers:
            entry = tomlkit.table()
            entry["command"] = server.command
            entry["args"] = server.command_line(spellings)
            servers[server.name] = entry
    if servers:
        document["mcp_servers"] = servers
    return tomlkit.dumps(document)


class CodexGuidanceRenderer(ArtifactRenderer[Harness]):
    """Render root project guidance at Codex's documented repository location."""

    def __init__(
        self,
        prompts: PromptRenderer,
        spellings: NativeSpellings,
        budget: int = GUIDANCE_BYTE_BUDGET,
    ) -> None:
        self.prompts = prompts
        self.spellings = spellings
        self.budget = budget

    def render(self, source: Harness) -> ArtifactTree:
        return ArtifactTree(
            artifacts=[
                Artifact.generated(
                    path=Path("AGENTS.md"),
                    body=self.prompts.render(source.guidance),
                    semantic_id="harness.guidance",
                    banner=guidance_banner(self.prompts, source.guidance),
                ),
                Artifact.generated(
                    path=Path(".codex/config.toml"),
                    body=codex_project_config(source, self.spellings, self.budget),
                    semantic_id="harness.project-config",
                    banner=GeneratedBanner(
                        source=__name__,
                        command=REGENERATE_COMMAND,
                        notes=[
                            "Personal sandbox and approval defaults stay in "
                            "~/.codex/config.toml.",
                            "Native shell allows are generated under .codex/rules/.",
                            "Tool servers start from this project, so start "
                            "the runtime at its root.",
                        ],
                    ),
                ),
            ]
        )


CODEX_DISPATCHER = DispatcherDeclaration(
    runtime_name="Codex",
    package="lup.adapters.codex",
    managed_root_env=CODEX_LOGIN.config_home_env,
    routed_tools=["Bash", "web_fetch", "apply_patch"],
    hook_events=["PermissionRequest", "PreToolUse", "PostToolUse"],
    observation_event="PostToolUse",
    observed_tools=["apply_patch"],
    failure="stderr_exit",
    runtime_modules=["codex_patch", "policy_data"],
)
"""Everything Codex spells differently from every other runtime.

Both hook events reach the same dispatcher: the permission request carries
the approval channel an ask needs, and the pre-tool event covers the paths
that never raise one. The tools named here are both what the plugin
registers the hook for and what the compiler proves the dispatcher routes.
"""

CODEX_PATCH_RUNTIME = (
    resources.files("lup.adapters.codex").joinpath("patch.py").read_text("utf-8")
)
"""Envelope decoder, shipped beside the kernel for the dispatcher to import."""


CODEX_DYNAMIC_COMMANDS = (
    *INTERPRETERS,
    *PASS_THROUGH_WORDS,
    "awk",
    "curl",
    "find",
    "gawk",
    "git",
    "mawk",
    "rm",
    "sed",
    "uv",
    "uvx",
    "xargs",
)
"""Executables whose semantic decision cannot be represented by one prefix."""


def codex_allow_prefixes(
    rules: list[ShellCommandRule],
    runner_targets: list[RunnerTargetRule],
    dynamic: Collection[str] = CODEX_DYNAMIC_COMMANDS,
    excluded_commands: Collection[str] = (),
) -> list[list[str]]:
    """Compile semantic allows that stay allowed for every suffix.

    Codex prefix rules approve commands but do not choose sandbox placement.
    Because the same rule also approves a caller-requested escape, only forms
    placed ``outside`` can be widened into a native allow; ambient forms stay
    inside, and one placed ``inside`` is never approved for an escape. The runtime
    hook continues to classify those forms, along with
    every command whose safety depends on parsed content.

    The erased rows are what this reads rather than the declarations they came
    from, because the rows are where both axes have been resolved down the
    nesting: a level that inherited its effect is native-allowed on exactly the
    terms of one that spelled it out, which is the same reading the dispatcher
    and the canonical policy take.

    The prefix is the approval half of an escape the caller requests with
    ``sandbox_permissions=require_escalated``. A runner target declared
    ``outside`` receives that approval; an ambient target does not. The escape
    itself is chosen on the model's own call rather than compiled
    in advance — :meth:`CodexSpellings.escape_sandbox` carries it. The forms
    this cannot widen — a target behind a global flag, or one joined into a
    compound command — reach the hook, where a confined session is stopped
    with the reason rather than left to fail on the first write.
    """

    excluded = list(excluded_commands)

    def add(prefix: list[str], sandbox: SandboxPlacement) -> None:
        if sandbox != "outside" and not sandbox_excluded(shlex.join(prefix), excluded):
            return
        if prefix not in prefixes:
            prefixes.append(prefix)

    rows = erase_shell_rules(rules)
    gated = dict.fromkeys(row["command"] for row in rows if row["subcommand"])
    operational = dict.fromkeys(
        f"{row['command']} {row['subcommand']}" for row in rows if row["operation"]
    )
    prefixes: list[list[str]] = []
    for row in rows:
        if row["effect"] != "allow" or row["ask_flags"] or row["sandbox"] == "inside":
            continue
        if not row["subcommand"]:
            if row["command"] not in gated and row["command"] not in dynamic:
                add([row["command"]], row["sandbox"])
        elif row["operation"]:
            add([row["command"], row["subcommand"], row["operation"]], row["sandbox"])
        elif f"{row['command']} {row['subcommand']}" not in operational:
            add([row["command"], row["subcommand"]], row["sandbox"])
    for target in runner_targets:
        add(["uv", "run", target.name], target.sandbox)
    return sorted(prefixes)


def render_codex_rules(source: HookSet) -> str:
    """Render project-local native execution rules for prefix-safe allows."""
    rows = [
        f"prefix_rule(pattern = {json.dumps(prefix)}, decision = "
        '"allow", justification = "Allowed by Lup semantic shell policy")'
        for prefix in codex_allow_prefixes(
            source.shell_rules,
            list(source.runner_targets),
            excluded_commands=source.sandbox.excluded_commands
            if source.sandbox
            else (),
        )
    ]
    return "\n".join([*rows, ""])


class CodexHookRenderer(ArtifactRenderer[HookSet]):
    """Render Codex hooks, canonical kernel, and application policy rows."""

    def __init__(
        self, plugin_name: str, worker_identity: str, spellings: NativeSpellings
    ) -> None:
        self.plugin_name = plugin_name
        self.worker_identity = worker_identity
        self.spellings = spellings

    def render(self, source: HookSet) -> ArtifactTree:
        policy_hook = {
            "type": "command",
            "command": 'python3 "$PLUGIN_ROOT/hooks/scripts/policy.py"',
            "statusMessage": "Checking Lup policy",
            "timeout": 30,
        }
        decided = [
            {
                "matcher": "|".join(
                    routed_for(CODEX_DISPATCHER.routed_tools, source.refused_tools)
                ),
                "hooks": [policy_hook],
            }
        ]
        observed = [
            {
                "matcher": "|".join(CODEX_DISPATCHER.observed_tools),
                "hooks": [policy_hook],
            }
        ]
        hooks = {
            "hooks": {
                event: (
                    observed if event == CODEX_DISPATCHER.observation_event else decided
                )
                for event in CODEX_DISPATCHER.hook_events
            }
        }
        evidence = {
            "schemaVersion": 1,
            "policyIds": source.policy_ids,
            "askApproximation": "asks defer only on PermissionRequest; PreToolUse fails closed",
        }
        return ArtifactTree(
            artifacts=[
                Artifact(
                    path=Path(f".codex/plugins/{self.plugin_name}/hooks/hooks.json"),
                    content=json.dumps(hooks, indent=2, sort_keys=True),
                    semantic_id=source.id,
                ),
                Artifact.generated(
                    path=Path(f".codex/rules/{self.plugin_name}.rules"),
                    body=render_codex_rules(source),
                    semantic_id=source.id,
                    banner=GeneratedBanner(
                        source="lup.policy.shell_rules",
                        command=REGENERATE_COMMAND,
                    ),
                ),
                Artifact(
                    path=Path(
                        f".codex/plugins/{self.plugin_name}/hooks/scripts/policy.py"
                    ),
                    content=compile_dispatcher(CODEX_DISPATCHER),
                    semantic_id=source.id,
                    executable=True,
                    banner=dispatcher_banner(CODEX_DISPATCHER),
                ),
                *[
                    Artifact(
                        path=Path(
                            f".codex/plugins/{self.plugin_name}/hooks/runtime/"
                            f"kernel/{module.name}"
                        ),
                        content=module.source,
                        semantic_id=source.id,
                        banner=VERBATIM_COPY,
                    )
                    for module in policy_kernel_modules()
                ],
                Artifact(
                    path=Path(
                        f".codex/plugins/{self.plugin_name}/hooks/runtime/"
                        "codex_patch.py"
                    ),
                    content=CODEX_PATCH_RUNTIME,
                    semantic_id=source.id,
                    banner=VERBATIM_COPY,
                ),
                Artifact.generated(
                    path=Path(
                        f".codex/plugins/{self.plugin_name}/hooks/runtime/"
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
                        autonomous_agent_identities=[self.worker_identity],
                        path_roles=[
                            PathRoleRow(root=role.root.as_posix(), role=role.role)
                            for role in source.path_roles
                        ],
                        shell_rules=list(source.shell_rules),
                        refused_tools=list(source.refused_tools),
                        recoverable_target_limit=source.recoverable_target_limit,
                        runner_targets=list(source.runner_targets),
                        sandbox_excluded_commands=source.excluded_commands(),
                        auto_escape_prefixes=codex_allow_prefixes(
                            source.shell_rules,
                            list(source.runner_targets),
                            source.excluded_commands(),
                        ),
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
                        f".codex/plugins/{self.plugin_name}/hooks/runtime/evidence.json"
                    ),
                    content=json.dumps(evidence, indent=2, sort_keys=True),
                    semantic_id=source.id,
                ),
            ]
        )
