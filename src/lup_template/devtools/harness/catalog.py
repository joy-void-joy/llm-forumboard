# lup: ignore[constant-declaration]
# Every constant here is this repository's own composition — which runtimes it
# builds for, and what it calls its own harness session. A composition root is
# where a judgement is finally made rather than passed on, so there is no
# caller above it to take these from.
"""Root of the project-owned harness declaration graph.

The declaration leaves — skills, agents, prompt documents, settings, and
assets — live under ``content/``, aggregated by ``content.catalog``. This
module assembles those leaves with the hook policy and the resolver spec
into the portable ``Harness`` that ``generate`` compiles into both native
trees. Generated guidance and the adopter docs point here as the file that
owns URL scopes, protected edit roots, and the shell vocabulary declared in
``content.shell_vocabulary``, so its path is part of the documented surface.
"""

from pathlib import Path

from pydantic import AnyHttpUrl

from lup.harness.models import (
    Harness,
    HookPathRole,
    HookSandbox,
    HookSet,
    HookUrlScope,
    LiteralWord,
    McpServer,
    Plugin,
    ProjectRootWord,
    ResolveSpec,
    SkillInvocation,
)
from lup.adapters.claude.harness import ClaudeSpellings
from lup.adapters.codex.harness import CodexSpellings
from lup.codescan.boundaries import ApplicationRoots, generated_tree_paths
from lup.devtools.dev.workflow import WorkflowSpec
from lup.devtools.project import DevProject
from lup.harness.contracts import NativeSpellings
from lup.policy.kernel.rows import PathRoleRow
from lup.policy.refused_tools import RefusedTool
from lup.workspace.paths import project_root, read_project_name
from lup_template.agent.toolsets import tool_group_names
from lup_template.devtools.harness.content.catalog import AGENTS, SKILLS
from lup_template.devtools.harness.content.guidance import document as guidance_document
from lup_template.devtools.harness.content.shell_vocabulary import (
    RUNNER_TARGETS,
    SHELL_RULES,
)

EXCLUDED_COMMANDS = [
    # `py eval` is the rung the guidance points at for computing anything, and
    # it computes by talking to the Docker daemon over a Unix socket the
    # isolation blocks outright — the one incompatibility the runtime's own
    # documentation names. Excluding the rung leaves the expression evaluated
    # where it always was, in a container; granting the socket instead would
    # hand every sandboxed command the host through it.
    "uv run lup-devtools py eval *",
    # Egress the sandbox proxy cannot carry: it allowlists hostnames over
    # HTTP, and the transport underneath a git remote is SSH on port 22. No
    # narrower lever reaches this — a credential path takes a mode and not a
    # command, and narrowing the path would still leave ssh unable to read the
    # key it authenticates with.
    "ssh *",
    "git *",
    # `gh` reaches hosts the allowlist already admits, but it drives `git` for
    # anything touching a remote, and a child of a confined command is
    # confined too — so excluding git without it moves the same failure one
    # call deeper.
    "gh *",
]
"""Commands this project runs with no OS boundary beneath them.

Each is a requirement the boundary cannot express any other way, and the
count is the point: an exclusion is not a widened rule but a removed one, so
the list stays as short as the toolchain's actual incompatibilities."""

ARTIFACT_REFUSAL = (
    "publishing a page leaves the repository, and this project already owns"
    " surfaces that do not — run `uv run lup-devtools report` for everything"
    " left to implement, or the report skill to write it whole to tmp/report.md"
)
"""Why an artifact is the wrong reflex here, and what answers the same need.

The redirect is the point rather than the refusal, exactly as the
generated-tree refusal names the source to edit instead of only saying no. A
report that leaves the repository is one nothing in this project can read
back; the report surface is where the same question is answered in a place
every later session, scan, and gate can reach.
"""

REFUSED_TOOLS = [
    RefusedTool(tool="Artifact", reason=ARTIFACT_REFUSAL),
    RefusedTool(tool="Skill", specifier="artifact-design", reason=ARTIFACT_REFUSAL),
]
"""The calls this project has decided against, each naming what to reach for.

Both are Claude Code's spellings, and it is there the reflex they stop exists.
Every runtime consults the table all the same, because which names are worth
refusing is this declaration's answer rather than an adapter's — so a name
Codex does offer would be refused there by writing one line here. Neither is
walled off — a deliberate use escalates with the marker the shell lattice
already uses, and gets an approval question carrying its own stated reason.
"""

HARNESS_SESSION = "harness"
"""The session a natively launched tool server opens for itself.

One name per worktree, shared by every group's process, so the tools of one
native session write where the next one will find them."""


def agent_tool_servers() -> list[McpServer]:
    """Offer this project's own agent tools to whichever runtime is reading.

    The groups come from the same registry the in-process and subprocess
    backends assemble from, so a group added there reaches a native session
    too rather than only the ones this program launches itself. Realtime is
    the relay mode of a persistent run and belongs to no interactive session,
    so its group is not among them.
    """
    return [
        McpServer(
            id=f"mcp.{name}",
            name=name,
            description=f"Agent tools in the {name} group, served over stdio",
            command="uv",
            arguments=[
                LiteralWord(text="run"),
                LiteralWord(text="--directory"),
                ProjectRootWord(),
                LiteralWord(text="lup-devtools"),
                LiteralWord(text="agent"),
                LiteralWord(text="serve-tools"),
                LiteralWord(text="--server"),
                LiteralWord(text=name),
                LiteralWord(text="--session"),
                LiteralWord(text=HARNESS_SESSION),
            ],
        )
        for name in tool_group_names(realtime=False)
    ]


def declared_plugin() -> Plugin:
    """The one plugin this project publishes, as generation renders it.

    Its name and marketplace are decided once, here, so a command that has
    to spell either reads the declaration rather than repeating it.
    """
    return portable_harness().plugins[0]


def declared_hook_set() -> HookSet:
    """The hook set this project declares, for a session composed in process.

    Generated plugins read it off the harness they are compiled from. A
    session this program builds itself has to reach the same declaration, or
    it enforces something the generated tree does not.
    """
    return portable_harness().declared_hooks


WORKFLOW = WorkflowSpec(branches=["main", "dev"])
"""This project's gate: the two-tier model, where `dev` integrates and `main`
carries what has landed, so both deserve a run of their own."""


NATIVE_RUNTIMES: list[NativeSpellings] = [ClaudeSpellings(), CodexSpellings()]
"""Every runtime this project generates a tree for."""


def application_roots() -> ApplicationRoots:
    """Where this project composes concrete native implementations.

    The generated trees are asked of the runtimes rather than written down, so
    a location a runtime learns sanctions its own tree. The rest are this
    project's own homes, derived from where this package actually sits, so
    renaming it during initialization moves them instead of leaving the rule
    pointing at a package that is gone.
    """
    package = Path(__file__).resolve().parents[2].relative_to(project_root()).as_posix()
    harness = f"{package}/devtools/harness/"
    plugins = [plugin.name for plugin in portable_harness().plugins]
    generated = generated_tree_paths(NATIVE_RUNTIMES, plugins)
    return ApplicationRoots(
        generated=generated,
        composition=[
            *generated,
            "tests/",
            "packages/lup/tests/",
            "examples/",
            f"{package}/agent/core.py",
            # Which backend's sub-apps this project takes is a composition
            # decision like any other: `usage` reads each backend's own
            # account, and a project runs on the backends it names here.
            f"{package}/devtools/subapps.py",
            harness,
            f"{package}/devtools/setup.py",
        ],
        portable_prose=[f"{harness}content/"],
    )


def dev_project() -> DevProject:
    """What this project tells the shared development tooling about itself.

    The package name is derived from where this file actually sits rather
    than written down, so initialization renaming the package moves the
    scans with it instead of leaving them resolving against a name that is
    gone. The roles and the rule selection come from the same hook set the
    generated trees enforce, so a scan and a hook cannot disagree about what
    a path is for, nor about which rules are live here.
    """
    hooks = declared_hook_set()
    return DevProject(
        package=Path(__file__).resolve().parents[2].name,
        roots=application_roots(),
        rules=hooks.rules,
        path_roles=[
            PathRoleRow(root=role.root.as_posix(), role=role.role)
            for role in hooks.path_roles
        ],
    )


def portable_harness(version: str = "0.2.0", root: Path | None = None) -> Harness:
    """Build the canonical declaration graph consumed by every adapter.

    Deliberately one declaration, not one per platform: every intended
    Claude/Codex difference is a rendering decision in the adapters
    (``compile_claude`` / ``compile_codex``) or a support artifact in the
    generation recipes, mapped in ``docs/platform-differentiation.md``.
    Per-platform declarations overriding a shared default were rejected
    because they would let semantic content fork silently.
    """
    plugin = Plugin(
        id="plugin.lup",
        name="lup",
        marketplace=f"{read_project_name(root or project_root())}-repository",
        version=version,
        description=(
            "Self-improvement harness with feedback, review, and safe resolution flows"
        ),
        skills=SKILLS,
        agents=AGENTS,
        mcp_servers=agent_tool_servers(),
        hooks=HookSet(
            id="hooks.lup-policy",
            policy_ids=["fetch", "shell", "edit", "unknown-tool"],
            allowed_fetch=[
                HookUrlScope(origin=AnyHttpUrl("https://docs.claude.com")),
                HookUrlScope(origin=AnyHttpUrl("http://docs.claude.com")),
                HookUrlScope(origin=AnyHttpUrl("https://code.claude.com")),
                HookUrlScope(origin=AnyHttpUrl("http://code.claude.com")),
                # docs.claude.com now redirects the Agent SDK and API paths
                # here, so the route the guidance prescribes leaves the
                # declared scopes one hop in.
                HookUrlScope(origin=AnyHttpUrl("https://platform.claude.com")),
                HookUrlScope(origin=AnyHttpUrl("http://platform.claude.com")),
                # Where a session publishes a settled classification for a
                # later one to read back, so a briefing can cite the artifact
                # rather than restate it.
                HookUrlScope(origin=AnyHttpUrl("https://claude.ai")),
                HookUrlScope(origin=AnyHttpUrl("https://ai.pydantic.dev")),
                HookUrlScope(origin=AnyHttpUrl("http://ai.pydantic.dev")),
                HookUrlScope(origin=AnyHttpUrl("https://learn.chatgpt.com")),
                HookUrlScope(origin=AnyHttpUrl("http://learn.chatgpt.com")),
                HookUrlScope(origin=AnyHttpUrl("https://developers.openai.com")),
                HookUrlScope(origin=AnyHttpUrl("http://developers.openai.com")),
                HookUrlScope(origin=AnyHttpUrl("https://github.com")),
                HookUrlScope(origin=AnyHttpUrl("https://api.github.com")),
                HookUrlScope(
                    origin=AnyHttpUrl("https://githubusercontent.com"),
                    include_subdomains=True,
                ),
                HookUrlScope(origin=AnyHttpUrl("https://pypi.org")),
                HookUrlScope(origin=AnyHttpUrl("https://files.pythonhosted.org")),
                # This machine's own services: the setup dashboard, the
                # resolver supervisor, and whatever a session is running to
                # look at. Reaching one is how a session establishes that it
                # came up at all, and asking for that is asking about a
                # process the same session just started. No port is named
                # because every one of these surfaces takes `--port`, and a
                # scope that went stale on a flag would put the question back.
                *(
                    HookUrlScope(
                        origin=AnyHttpUrl(f"http://{host}"),
                        any_port=True,
                        reason="this machine's own pages, on whatever port they took",
                    )
                    for host in ("127.0.0.1", "localhost")
                ),
            ],
            protected_edit_roots=[
                Path(".claude"),
                Path("pyproject.toml"),
                Path("sync.json"),
                Path("downstream.json"),
            ],
            path_roles=[
                HookPathRole(root=Path("tests"), role="test"),
                HookPathRole(root=Path("packages/lup/tests"), role="test"),
                # Scratch is "disposable by construction", and a build product
                # qualifies as squarely as a scratchpad does: every one of
                # these is reproduced by a command, so destroying one costs
                # the command rather than any information. Leaving them
                # production made `rm` and `cp` ask about caches and virtual
                # environments, which is an approval that teaches nobody
                # anything.
                HookPathRole(root=Path("tmp"), role="scratch"),
                HookPathRole(root=Path(".venv"), role="scratch"),
                HookPathRole(root=Path(".ruff_cache"), role="scratch"),
                HookPathRole(root=Path(".pytest_cache"), role="scratch"),
                HookPathRole(root=Path("build"), role="scratch"),
                HookPathRole(root=Path("dist"), role="scratch"),
                HookPathRole(root=Path("htmlcov"), role="scratch"),
                HookPathRole(root=Path("node_modules"), role="scratch"),
            ],
            human_owned_files=[Path("README.md")],
            refused_tools=REFUSED_TOOLS,
            # Which checker answers for an edit is this project's toolchain,
            # not the library's: the path is relative to the checkout that
            # holds the edited file, so a worktree runs its own.
            diagnostics_command=[".venv/bin/pyright", "--outputjson"],
            shell_rules=SHELL_RULES,
            # This project's toolchain: what `uv run <target>` may reach here
            # without a question, which is nothing any other project inherits.
            # The group places `lup-devtools` outside the sandbox, because
            # every command of it that opens an agent session is unusable
            # confined.
            runner_targets=RUNNER_TARGETS,
            sandbox=HookSandbox(
                extra_domains=["api.anthropic.com"],
                # A read deny inside the boundary, which is where it belongs:
                # the commands that legitimately need these keys are the ones
                # excluded below, and they never enter it.
                credential_paths=["~/.ssh", "~/.aws/credentials"],
                # Every command in this project reaches its toolchain through
                # `uv`, which locks its cache whenever it resolves dependencies
                # — which a changed pyproject.toml forces, and an integration
                # merge is what changes pyproject.toml.
                writable_paths=["~/.cache/uv"],
                excluded_commands=EXCLUDED_COMMANDS,
            ),
        ),
    )
    return Harness(
        generator_version=version,
        source_evidence={"content": "typed-python"},
        plugins=[plugin],
        guidance=guidance_document(plugin.hooks.rules if plugin.hooks else None),
        resolver=ResolveSpec(
            id="resolver.lup",
            worker_identity="resolver-worker",
            worker_skill=SkillInvocation(plugin="lup", skill="implementer"),
            review_skill=SkillInvocation(plugin="lup", skill="resolve-reviewer"),
            merge_skill=SkillInvocation(plugin="lup", skill="merge"),
        ),
    )
