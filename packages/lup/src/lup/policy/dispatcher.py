# lup: ignore[model-free-function, constant-declaration]
# Every constant here is a name in the script this module emits — its package,
# its members, its router, its entry point, its shebang. They are the compiled
# artifact's own vocabulary, so a caller passing different ones would be
# compiling a different artifact than the one the proofs below read.
# The compiler is the renderer half of declaration-plus-renderer, and its proofs
# span the halves it joins: a declaration that compiled itself would carry the
# reading of source it is checked against, and no single half can say whether
# the one script they become still resolves. What a half can say about itself
# is a method on SourceHalf.
"""Compile one hook dispatcher per native runtime from type-checked source.

A dispatcher is the one artifact whose breakage is silent: a plugin host runs
it outside the workspace, so an unresolved name or a missing branch surfaces
as a permission decision that never happens, in a session that only sees the
tool go through. Shipping it as text would put the most safety-critical file
in the repository beyond every checker and leave each runtime holding its own
copy of the host-side half, so it is compiled instead — from
:mod:`lup.policy.assets.host`, which every runtime answers identically, plus
one :class:`DispatcherDeclaration` per runtime stating what genuinely differs.

Every field of that declaration is required, so a new axis — another routed
tool, another hook event, another failure shape — becomes a field no runtime
can be constructed without answering, the way a new prompt part becomes a
method no :class:`~lup.harness.contracts.NativeSpellings` can omit. Divergence
is a construction error rather than something a reviewer has to notice.

Compilation reads modules the workspace already type-checks and lints, selects
declared members from their syntax trees, and emits their exact source. It
never authors logic, so nothing reaches a generated script that pyright has
not read, and a traceback in a hook keeps the text a reviewer is looking at.
What the compiler owns is proof: that the script imports only what a bare
script beside its runtime can resolve, that neither half repeats the other,
and that the tools routed, events named, environment read, and failure taken
are the ones the declaration promised.
"""

import ast
from importlib import resources
from pathlib import PurePath
from typing import Literal

from pydantic import BaseModel

from lup.harness.banner import REGENERATE_COMMAND, GeneratedBanner
from lup.policy.kernel.edit import file_level_line, suppression_reaches

SHARED_PACKAGE = "lup.policy"
SHARED_MEMBER = "host"
"""The half every runtime answers identically, compiled into both scripts."""

DECISIONS_MEMBER = "decisions"
"""The half every runtime answers identically, needing the kernel to answer it.

Separate from ``host`` because the two stand on different ground: the host half
resolves facts from the machine and may reach nothing but the pinned standard
library, while this one calls the kernel and is type-checked against the
generated runtime. Both are shared, so neither is a place a runtime can
diverge — which is the whole reason the kernel call sites live here.
"""

# lup: ignore[library-default] — the two members this compiler itself defines; a caller cannot splice a half the compiler does not read
SPLICED_MEMBERS = (
    SHARED_MEMBER,
    DECISIONS_MEMBER,
)
"""The halves the compiler splices in, whose imports resolve to nothing after."""

RUNTIME_MEMBER = "policy_dispatcher"
"""The half one adapter owns: its own words, and nothing another repeats."""

KERNEL_PACKAGE = "kernel"
# lup: ignore[library-default] — the stdlib a compiled dispatcher actually imports; widening it is the hazard the pin exists to prevent
DISPATCHER_STDLIB = (
    "json",
    "os",
    "sys",
    "pathlib",
    "subprocess",
)
"""The standard library a compiled dispatcher may reach.

Pinned rather than open: the script starts through a native CLI with
``python3``, outside Lup's import graph and any active virtual environment,
so a convenient project helper — or the ``lup`` package itself — would make
permissions disappear precisely where packaging differs.

``subprocess`` earns its place because asking Git whether a path is
recoverable is a question only a process can answer, and every alternative
spelling of it — ``sh``, a devtools helper — is exactly the unresolvable
import this pin exists to reject. Being genuine standard library, it cannot
produce that failure. Each further entry deserves the same argument.
"""

ROUTER = "dispatch"
ROUTER_SUBJECT = "name"
ENTRYPOINT = "main"
"""The router, the tool name it branches on, and the process entry point."""

RELATIVIZER = "worktree_path"
"""How every dispatcher makes an absolute path repo-relative.

Shared rather than declared per runtime: every repo-relative rule matches on
this answer, so a runtime free to spell its own could anchor on something
that is not the file's worktree, and each rule would then miss in silence.
"""

EVENT_KEY = "hook_event_name"
EVENT_FIELD = "hookEventName"
"""How an arriving payload and a returned envelope each name a hook event."""

SHEBANG = "#!/usr/bin/env python3"
INVOCATION = 'if __name__ == "__main__":\n    main()'

DISPATCHER_SCRIPT = PurePath("policy.py")
"""The file this compiler emits, named to spell its banner as a comment."""

DispatcherFailure = Literal["conservative_ask", "stderr_exit"]
"""What a dispatcher does with input it cannot decide from.

``conservative_ask`` returns an approval question through the hook's own
decision channel; ``stderr_exit`` has no such channel and fails closed by
writing the reason to stderr and exiting non-zero.
"""


class DispatcherDeclaration(BaseModel, frozen=True):
    """Everything one native runtime spells differently from every other.

    Each field is required, so answering the whole set is what constructing a
    runtime means. Adding a field is how a new axis of divergence is opened,
    and both runtimes have to close it before the tree compiles again.

    ``hook_events`` is every event the plugin registers, and
    ``observation_event`` names the one among them that decides nothing.
    The tools are declared apart because the two are registered for
    different sets: a deciding event covers everything the dispatcher
    routes, since a call it is not registered for is a call nobody judged,
    while the watching event covers the editing tools alone. It exists only
    to record where editing is happening, and a matcher wide enough for the
    deciding set would spawn the script after every shell command to find
    nothing worth recording.
    """

    runtime_name: str
    package: str
    managed_root_env: str
    routed_tools: list[str]
    hook_events: list[str]
    observation_event: str
    observed_tools: list[str]
    failure: DispatcherFailure
    runtime_modules: list[str]


class DispatcherImport(BaseModel, frozen=True):
    """One module a dispatcher half imports, with the names it takes."""

    module: str
    names: list[str]


class SourceHalf(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """One type-checked module the compiler reads rather than authors."""

    module: str
    text: str
    tree: ast.Module

    def imports(self) -> list[DispatcherImport]:
        """Every module this half imports, in source order."""
        return [item for node in self.tree.body for item in import_statement(node)]

    def functions(self) -> list[ast.FunctionDef]:
        """Every top-level function this half defines, in source order."""
        return [node for node in self.tree.body if isinstance(node, ast.FunctionDef)]

    def function(self, name: str) -> ast.FunctionDef | None:
        """The top-level function this half defines under this name, if any."""
        return next((node for node in self.functions() if node.name == name), None)

    def source_of(self, node: ast.FunctionDef) -> str:
        """The exact source of one function, comments and docstring intact."""
        segment = ast.get_source_segment(self.text, node)
        if segment is None:
            raise ValueError(f"{self.module} has no source for {node.name}")
        return segment

    def spliced_prologue(self, emitted: str) -> list[str]:
        """This half's own imports, less its links and what is already there.

        Emitted as whole source lines rather than as the parsed statement, so
        a marker stays attached to the import it answers for: the rules the
        generated tree is scanned against read the line, and a marker the
        compiler dropped is a violation nobody declared. That holds for either
        placement — a marker whose reason outgrew the import's line stands
        above it, and slicing from the import alone would leave it behind.
        Such a segment opens with a comment on its own line, which the
        formatter separates from the statement before it, so the blank line
        goes in here: what this compiler emits has to be formatted source and
        not merely correct source. A half
        that needs something the runtime half never imports —
        the standard library module the host half asks Git with, the kernel
        names the decisions half calls — carries it in rather than obliging
        every adapter to import what it does not use.
        """
        lines = self.text.splitlines()
        file_level = file_level_line(self.text)

        def opens_at(node: ast.Import | ast.ImportFrom) -> int:
            """The first line one import needs, a directive above it included."""
            above = node.lineno - 1
            if above != file_level and suppression_reaches(lines, above, node.lineno):
                return above
            return node.lineno

        return [
            f"\n{segment}" if segment.lstrip().startswith("#") else segment
            for node in self.tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            if not (isinstance(node, ast.ImportFrom) and node.module in SPLICED_MEMBERS)
            for segment in [
                "\n".join(lines[opens_at(node) - 1 : (node.end_lineno or node.lineno)])
            ]
            if segment and segment not in emitted
        ]

    def prologue(self) -> str:
        """This half's imports, less the shared imports the compiler links.

        A spliced half arrives as source rather than as a module beside the
        script, so the import that let a type checker read this half against
        it has nothing left to resolve once they are one file.
        """
        docstring = self.tree.body[0]
        opening = self.functions()[0].lineno
        linked = [
            number
            for node in self.tree.body
            if isinstance(node, ast.ImportFrom) and node.module in SPLICED_MEMBERS
            for number in range(node.lineno, (node.end_lineno or node.lineno) + 1)
        ]
        kept = [
            line
            for number, line in enumerate(self.text.splitlines(), start=1)
            if (docstring.end_lineno or 0) < number < opening and number not in linked
        ]
        return "\n".join(kept).strip()

    def routed_tools(self) -> list[str]:
        """Every native tool name the router branches on.

        Read from the syntax rather than trusted: a tool the declaration names
        but the router never reaches would otherwise be registered with the
        host and then silently fall through to the unclassified branch.
        """
        return sorted(
            {
                comparator.value
                for node in ast.walk(self.tree)
                if isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Name)
                and node.left.id == ROUTER_SUBJECT
                for comparator in node.comparators
                if isinstance(comparator, ast.Constant)
                and isinstance(comparator.value, str)
            }
        )

    def named_events(self) -> list[str]:
        """Every hook event the runtime half branches on or answers as.

        Read from the syntax for the same reason the routed tools are: an
        event the script recognizes but nobody registered it for is a branch
        that never runs, and an event it answers as is what the host reads the
        reply under.
        """
        return sorted(
            {
                *[
                    comparator.value
                    for node in ast.walk(self.tree)
                    if isinstance(node, ast.Compare)
                    and isinstance(node.left, ast.Subscript)
                    and isinstance(node.left.slice, ast.Constant)
                    and node.left.slice.value == EVENT_KEY
                    for comparator in node.comparators
                    if isinstance(comparator, ast.Constant)
                    and isinstance(comparator.value, str)
                ],
                *[
                    value.value
                    for node in ast.walk(self.tree)
                    if isinstance(node, ast.Dict)
                    for key, value in zip(node.keys, node.values, strict=True)
                    if isinstance(key, ast.Constant)
                    and key.value == EVENT_FIELD
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ],
            }
        )


def source_half(package: str, member: str) -> SourceHalf:
    """Read and parse one dispatcher half from its owning package."""
    text = resources.files(package).joinpath(f"assets/{member}.py").read_text("utf-8")
    return SourceHalf(
        module=f"{package}.assets.{member}", text=text, tree=ast.parse(text)
    )


def import_statement(node: ast.stmt) -> list[DispatcherImport]:
    """Read one statement as the imports it performs, if it performs any."""
    match node:
        case ast.Import(names=names):
            return [DispatcherImport(module=alias.name, names=[]) for alias in names]
        case ast.ImportFrom(module=str(module), names=names):
            return [
                DispatcherImport(module=module, names=[alias.name for alias in names])
            ]
        case _:
            return []


def string_constants(node: ast.AST) -> list[str]:
    """Every string literal beneath one node."""
    return [
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    ]


def resolvable(module: str, declaration: DispatcherDeclaration) -> bool:
    """Whether a bare script beside its runtime can resolve this module."""
    return (
        module in DISPATCHER_STDLIB
        or module == SHARED_MEMBER
        or module == KERNEL_PACKAGE
        or module.startswith(f"{KERNEL_PACKAGE}.")
        or module in declaration.runtime_modules
    )


def breach(condition: bool, message: str) -> list[str]:
    """One breach or none, so a check reads as the condition it states."""
    return [message] if condition else []


def import_breaches(
    declaration: DispatcherDeclaration,
    shared: SourceHalf,
    decisions: SourceHalf,
    runtime: SourceHalf,
) -> list[str]:
    """Imports a compiled script could not resolve on a bare interpreter.

    Every half carries its own imports into the emitted prologue, so what is
    checked is resolvability rather than whether one half remembered to import
    what another stands on. A link between halves resolves to nothing once
    they are one file, which is why those are excluded rather than judged.
    """
    return [
        f"{half.module} imports {item.module}, which a bare script cannot resolve"
        for half in (shared, decisions, runtime)
        for item in half.imports()
        if item.module not in SPLICED_MEMBERS
        and not resolvable(item.module, declaration)
    ]


def host_purity_breaches(shared: SourceHalf) -> list[str]:
    """Reaches the host half makes beyond the standard library it is pinned to.

    The host half is what resolves facts from the machine, and it may stand on
    nothing but the pinned standard library — not the kernel, not the ``lup``
    package. Type checking used to settle that incidentally, by reading the
    file somewhere the kernel did not resolve; it now reads against a
    generated runtime so the kernel-aware half beside it can be checked at
    all. A guarantee that has quietly become a configuration detail is not a
    guarantee, so it is proven here instead.
    """
    return [
        f"{SHARED_MEMBER} imports {item.module}, which is outside its pinned stdlib"
        for item in shared.imports()
        if item.module not in DISPATCHER_STDLIB
    ]


def sharing_breaches(
    shared: SourceHalf, decisions: SourceHalf, runtime: SourceHalf
) -> list[str]:
    """Places where the halves overlap instead of composing.

    A runtime half redefining something a shared half answers is the drift
    this split exists to prevent, so it is a breach wherever it appears —
    including the decisions half, which stands on the host half exactly as an
    adapter does.
    """
    offers = {SHARED_MEMBER: [node.name for node in shared.functions()]}
    offers[DECISIONS_MEMBER] = [node.name for node in decisions.functions()]
    shared_names = offers[SHARED_MEMBER] + offers[DECISIONS_MEMBER]
    return [
        *[
            f"{half.module} redefines {node.name}, which a shared half answers"
            for half in (decisions, runtime)
            for node in half.functions()
            if node.name
            in (offers[SHARED_MEMBER] if half is decisions else shared_names)
        ],
        *[
            f"{half.module} takes {name} from {item.module}, which does not offer it"
            for half in (decisions, runtime)
            for item in half.imports()
            if item.module in SPLICED_MEMBERS
            for name in item.names
            if name not in offers[item.module]
        ],
    ]


def declaration_breaches(
    declaration: DispatcherDeclaration,
    shared: SourceHalf,
    decisions: SourceHalf,
    runtime: SourceHalf,
) -> list[str]:
    """Axes the declaration promised that the runtime half does not keep."""
    constants = string_constants(runtime.tree)
    entrypoint = runtime.function(ENTRYPOINT)
    # Read from the failure handler rather than from the whole entrypoint.
    # The axis is what a dispatcher does with input it cannot decide from,
    # and an exit anywhere else answers a different question — a watching
    # event reporting what it found has no decision channel to answer
    # through, and exiting is the only way it reaches the agent at all.
    handlers = (
        [node for node in ast.walk(entrypoint) if isinstance(node, ast.ExceptHandler)]
        if entrypoint is not None
        else []
    )
    closes = any(
        isinstance(node, ast.Name) and node.id == "SystemExit"
        for handler in handlers
        for node in ast.walk(handler)
    )
    routed = runtime.routed_tools()
    declared = sorted(declaration.routed_tools)
    return [
        *breach(routed != declared, f"routes {routed}, not the declared {declared}"),
        *[
            f"names the {event} hook event it is not registered for"
            for event in runtime.named_events()
            if event not in declaration.hook_events
        ],
        *[
            f"declares {name} but defines no such function"
            for name in (ROUTER, ENTRYPOINT)
            if runtime.function(name) is None
        ],
        *breach(
            RELATIVIZER
            not in [
                name
                for item in decisions.imports()
                if item.module == SHARED_MEMBER
                for name in item.names
            ],
            f"{DECISIONS_MEMBER} never takes {RELATIVIZER} from {SHARED_MEMBER}",
        ),
        *breach(
            declaration.managed_root_env not in constants,
            f"never reads {declaration.managed_root_env}",
        ),
        *breach(
            declaration.managed_root_env in string_constants(shared.tree),
            f"leaks {declaration.managed_root_env} into the shared half",
        ),
        *breach(
            closes != (declaration.failure == "stderr_exit"),
            f"does not fail the declared {declaration.failure} way",
        ),
    ]


def dispatcher_banner(declaration: DispatcherDeclaration) -> GeneratedBanner:
    """Name both halves one runtime's script is compiled from.

    The script is one file compiled from two sources, so a reader sent to
    either alone would edit the wrong half; the banner names them together.
    """
    return GeneratedBanner(
        source=(
            f"{SHARED_PACKAGE}.assets.{SHARED_MEMBER} and "
            f"{declaration.package}.assets.{RUNTIME_MEMBER}"
        ),
        command=REGENERATE_COMMAND,
    )


def compiled_docstring(declaration: DispatcherDeclaration) -> str:
    """Say what the compiled script is, leaving its provenance to the banner."""
    return (
        f'"""{declaration.runtime_name} hook dispatcher over the canonical '
        "semantic kernel.\n"
        "\n"
        "Runs as a bare script beside its own runtime directory, reaching only\n"
        "the standard library and the kernel copied beside it.\n"
        '"""'
    )


def compile_dispatcher(declaration: DispatcherDeclaration) -> str:
    """Compile one runtime's hook dispatcher, or refuse to ship a broken one.

    Refusal is the point of the return type being a string rather than a
    report: a dispatcher that cannot be proven is not a weaker dispatcher, it
    is a session running without a permission boundary, so generation stops.
    """
    shared = source_half(SHARED_PACKAGE, SHARED_MEMBER)
    decisions = source_half(SHARED_PACKAGE, DECISIONS_MEMBER)
    runtime = source_half(declaration.package, RUNTIME_MEMBER)
    breaches = [
        *import_breaches(declaration, shared, decisions, runtime),
        *host_purity_breaches(shared),
        *sharing_breaches(shared, decisions, runtime),
        *declaration_breaches(declaration, shared, decisions, runtime),
    ]
    if breaches:
        raise ValueError(f"{runtime.module} " + "; ".join(breaches))
    header = "\n".join([SHEBANG, compiled_docstring(declaration)])
    prologue = runtime.prologue()
    carried = [
        segment
        for half in (shared, decisions)
        for segment in half.spliced_prologue(prologue)
    ]
    blocks = [
        "\n".join([f"{header}\n\n{prologue}", *carried]),
        *[shared.source_of(node) for node in shared.functions()],
        *[decisions.source_of(node) for node in decisions.functions()],
        *[runtime.source_of(node) for node in runtime.functions()],
        INVOCATION,
    ]
    script = "\n\n\n".join(blocks) + "\n"
    return dispatcher_banner(declaration).applied_to(DISPATCHER_SCRIPT, script)
