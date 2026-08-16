# lup: ignore[native-spelling]
# This checker necessarily owns the provider spellings it audits.
"""AST boundary rules keeping the library reusable by someone else's project.

Two directions are guarded. *Inward*: named adapter packages, tests, and
explicit application/CLI composition roots may import concrete implementations
and spell provider wire names, while every other module composes only the
portable contracts. *Outward*: a library module may not decide for its
adopters — a declared data table has to be reachable as an overridable
default, so an adopter replaces the vocabulary rather than editing the library
(``library-default``, the mechanical half of the placement criterion in
``docs/library.md``).

The same criterion holds beyond the library, where the choice is frozen for
this project's own callers rather than for an adopter, so ``constant-declaration``
judges every other module-level constant by it. The two read one enumeration
and :meth:`ConstantDeclaration.judging_rule` hands each declaration to exactly
one of them, so neither can reach a line the other owns.

A deliberate exception uses the typed ``# lup: ignore[<rule-id>]`` on the
offending line or as a file-level directive, with a reason.
"""

import ast
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import get_args

from pydantic import BaseModel

from lup.codescan.common import (
    PythonContext,
    PythonSource,
    file_level_ignore,
    ignore_rule_ids,
)
from lup.codescan.project import RuleFinding, RuleViolation, audit_suppressions
from lup.policy.kernel.edit import (
    IGNORE_RE,
    suppression_placement,
    suppression_reaches,
)
from lup.harness.contracts import NativeSpellings
from lup.harness.models import PluginLocation, TreeLocation
from lup.policy.kernel.decision import KERNEL_IMPORT_ALLOWLIST

# lup: ignore[constant-declaration] — a rule id is the rule's own identity: it is
# what a typed directive, a deny message, and the reference all name it by, so a
# caller passing a different one would be silencing a rule nobody can spell
RULE_ID = "seam-boundary"
# lup: ignore[constant-declaration] — rule identity
NATIVE_SPELLING_RULE_ID = "native-spelling"
# lup: ignore[constant-declaration] — rule identity
KERNEL_IMPORT_RULE_ID = "kernel-imports"
# lup: ignore[constant-declaration] — rule identity
LIBRARY_DEFAULT_RULE_ID = "library-default"
# lup: ignore[constant-declaration] — rule identity
CONSTANT_DECLARATION_RULE_ID = "constant-declaration"
# lup: ignore[constant-declaration] — where this library's own sources sit in the
# repository that publishes it, which is a fact about the distribution
LIBRARY_ROOT = "packages/lup/src/lup/"
KERNEL_ROOT = f"{LIBRARY_ROOT}policy/kernel/"
"""Where the decision kernel ships, derived so it cannot drift from the root."""
# lup: ignore[library-default] — the adapter packages this library ships, so the value follows lup.adapters
NATIVE_PREFIXES = ("lup.adapters.claude", "lup.adapters.codex")
# lup: ignore[library-default] — each key is literally what the provider calls the thing
NATIVE_SPELLINGS = {
    "/lup:": "Claude skill invocation",
    "$lup:": "Codex skill invocation",
    "CLAUDE_CONFIG_DIR": "Claude configuration environment",
    "CODEX_HOME": "Codex configuration environment",
    ".claude-plugin": "Claude plugin manifest path",
    ".codex-plugin": "Codex plugin manifest path",
    "PreToolUse": "native hook event",
    "PermissionRequest": "native hook event",
    "PostToolUse": "native hook event",
    "SessionStart": "native hook event",
    "thread/start": "Codex app-server method",
    "thread/resume": "Codex app-server method",
    "thread/fork": "Codex app-server method",
    "turn/start": "Codex app-server method",
    "turn/steer": "Codex app-server method",
    "turn/interrupt": "Codex app-server method",
    "account/rateLimits/read": "Codex app-server method",
    "account/usage/read": "Codex app-server method",
}


def generated_tree_paths(
    runtimes: Sequence[NativeSpellings], plugins: Sequence[str]
) -> list[str]:
    """Every path a runtime writes its own tree at, asked rather than listed.

    A generated tree is the rendering of exactly the implementations this rule
    guards, so what sanctions it is that a runtime spells it — and a runtime
    that learns a location sanctions it the same day, with no second copy here
    to keep in step.
    """
    return sorted(
        {
            *(
                runtime.tree(location)
                for runtime in runtimes
                for location in get_args(TreeLocation.__value__)
            ),
            *(
                runtime.plugin(plugin, location, None)
                for runtime in runtimes
                for plugin in plugins
                for location in get_args(PluginLocation.__value__)
            ),
        }
    )


class ApplicationRoots(BaseModel, frozen=True):
    """Where one application composes concrete implementations of the seams.

    The library guards its own package and can name nothing beyond it: an
    adopter renames the application package before writing a line, so a path
    written down here would go on naming a package that no longer exists and
    silently sanction nothing.
    """

    composition: list[str] = []
    """Repository-relative files, or directory prefixes ending in ``/``."""

    portable_prose: list[str] = []
    """Those composition roots whose prose must still name no provider — a
    declaration every tree renders is written in one of them."""

    generated: list[str] = []
    """Directory prefixes a runtime writes its own tree at.

    Nothing under one is authored, so a rule about a choice has nothing to say
    there: the artifact renders a judgement made in the declaration it is
    compiled from, which is both where the rule can report it and the only
    place a fix survives the next generation."""

    def sanctions(self, rel_path: Path) -> bool:
        """Whether this application composes natively at that path."""
        posix = rel_path.as_posix()
        return any(
            posix.startswith(root) if root.endswith("/") else posix == root
            for root in self.composition
        )

    def renders(self, rel_path: Path) -> bool:
        """Whether that path is compiled rather than written."""
        return rel_path.as_posix().startswith(tuple(self.generated))

    def sanctions_spelling(self, rel_path: Path) -> bool:
        """Whether that path may also own a provider's own wire words."""
        return self.sanctions(rel_path) and not rel_path.as_posix().startswith(
            tuple(self.portable_prose)
        )


NO_APPLICATION = ApplicationRoots()
"""What an adopter sanctions before it says so: nothing beyond the library."""

# lup: ignore[library-default] — files of this library, which no adopter relocates
LIBRARY_COMPOSITION = (
    f"{LIBRARY_ROOT}devtools/harness/composition.py",
    f"{LIBRARY_ROOT}devtools/harness/launch.py",
    f"{LIBRARY_ROOT}devtools/harness/resolve.py",
)
"""The library's own CLI composition roots.

A launcher starts a named runtime, the resolver entry drives one, and the
composition builders assemble one, so all three compose concrete adapters the
way an application's own root does. They are listed rather than sanctioned by
directory: the engines beside them — generation, drift, reconciliation — read
a declaration and must stay portable.
"""


def composes_natively(rel_path: Path) -> bool:
    """Whether this path is a composition root of the library itself."""
    posix = rel_path.as_posix()
    return "lup/adapters/" in posix or posix in LIBRARY_COMPOSITION


# The library's own roots and the adopter's are two tables, and only one of them
# is a model; `ApplicationRoots.sanctions` carries that one's half.
# lup: ignore[model-free-function] — the path is the subject, the roots its table
def path_is_sanctioned(
    rel_path: Path, application: ApplicationRoots = NO_APPLICATION
) -> bool:
    """Whether a path may import native implementations as a composition root."""
    return composes_natively(rel_path) or application.sanctions(rel_path)


def library_placement_path_is_audited(rel_path: Path) -> bool:
    """Whether a path is a neutral library module the placement rule audits.

    Adapter packages are exempt: a native spelling is canonical there by
    definition, which is the same reason they own the spellings above.
    """
    posix = rel_path.as_posix()
    return posix.startswith(LIBRARY_ROOT) and "lup/adapters/" not in posix


# lup: ignore[model-free-function] — the path is the subject, the roots its table
def native_spelling_path_is_sanctioned(
    rel_path: Path, application: ApplicationRoots = NO_APPLICATION
) -> bool:
    """Whether a path may own provider wire spellings without a suppression."""
    return composes_natively(rel_path) or application.sanctions_spelling(rel_path)


class BoundaryBreach(BaseModel):
    """One concrete native import outside a sanctioned composition root."""

    line: int
    module: str
    text: str


class BoundaryAuditFinding(BaseModel):
    """One missing, untyped, or spurious boundary-rule suppression."""

    kind: str
    line: int
    text: str
    message: str
    rule_id: str
    module: str = ""


class SourceViolation(BaseModel):
    """One unsuppressed source shape before ordinary suppression auditing.

    ``line`` is where the violation is reported, and it is the only thing this
    rule gets to say about its suppression: a declaration heading a fifty-line
    table is excused from the line above it, exactly as every other rule is,
    because a reason that does not fit inline goes to the same place whatever
    is being excused.
    """

    line: int
    text: str
    subject: str
    message: str


class ConstantDeclaration(BaseModel):
    """One module-level constant, and the shape its value is written in.

    ``entries`` counts the members of a written-out collection display — a
    vocabulary, whose judgement is which entries it holds — and is ``None``
    where the value is a single fact rather than a table.
    """

    name: str
    line: int
    end_line: int
    text: str
    entries: int | None = None

    directive_from: int = 0
    """First line a suppression may sit on to excuse this declaration.

    A directive may head its declaration, from anywhere in the comment block
    written directly above it — a reason worth reading rarely fits on one
    line. The block stops at the first line that is not a comment, so the zone
    never reaches the neighbour above: constants sit in runs, and one reason
    covering two of them would strand the second's own marker as a directive
    guarding nothing.
    """

    def judging_rule(self, library_module: bool) -> str:
        """Which of the two constant rules judges this declaration.

        Exactly one does, because this is a single total function over the one
        enumeration both rules read. A vocabulary the library freezes is
        ``library-default``'s, since it reaches an adopter only by their
        editing this repository; every other constant is
        ``constant-declaration``'s. Neither rule can reach a line the other
        owns, so no declaration is reported twice or excused by the wrong
        directive.
        """
        vocabulary = self.entries is not None and self.entries >= 2
        return (
            LIBRARY_DEFAULT_RULE_ID
            if library_module and vocabulary
            else CONSTANT_DECLARATION_RULE_ID
        )

    def judgement(self, carved: bool) -> str:
        """Why this frozen constant is reported, and what to do about it."""
        if carved:
            return (
                f"{self.name} exists only to carve a value out of text by hand — "
                "parse the value instead (datetime for a timestamp, urllib.parse "
                "for a URL, pathlib.Path for a path) and the constant goes with "
                "the surgery"
            )
        return (
            f"constant {self.name} is a judgement a second implementer with the "
            "same intent could have made differently, frozen where no caller can "
            "replace it — take it as an overridable default, or suppress it with "
            f"# lup: ignore[{CONSTANT_DECLARATION_RULE_ID}] and the reason it is "
            "canonical: a provider's wire spelling, a language's own vocabulary, "
            "an identity this repository defines"
        )


class BoundaryDirective(BaseModel):
    """One parsed inline or file-wide suppression directive."""

    line: int
    rule_ids: set[str] | None  # lup: ignore[set-shape] — rule identity membership
    file_level: bool = False


def native_module(name: str) -> bool:
    """Recognize only concrete named adapter packages."""
    return any(
        name == prefix or name.startswith(f"{prefix}.") for prefix in NATIVE_PREFIXES
    )


def import_violations(text: str) -> list[SourceViolation]:
    """Find native adapter imports through Python syntax before suppression."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines()
    violations: list[SourceViolation] = []  # lup: ignore[empty-collection]
    for node in ast.walk(tree):
        modules: list[str]
        match node:
            case ast.Import(names=names):
                modules = [item.name for item in names if native_module(item.name)]
            case ast.ImportFrom(module=str(module)) if native_module(module):
                modules = [module]
            case _:
                continue
        line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
        violations.extend(
            SourceViolation(
                line=node.lineno,
                subject=module,
                text=line.strip(),
                message=f"neutral module imports native adapter {module}",
            )
            for module in modules
        )
    return violations


def kernel_import_violations(text: str) -> list[SourceViolation]:
    """Find imports outside the hermetic policy kernel's pinned stdlib set."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines()
    violations: list[SourceViolation] = []  # lup: ignore[empty-collection]
    for node in ast.walk(tree):
        modules: list[str]
        match node:
            case ast.Import(names=names):
                modules = [
                    item.name
                    for item in names
                    if item.name not in KERNEL_IMPORT_ALLOWLIST
                ]
            # A relative import names a sibling kernel module, which carries
            # the same hermetic guarantee this rule enforces.
            case ast.ImportFrom(level=int(level)) if level > 0:
                continue
            case ast.ImportFrom(module=str(module)) if (
                module not in KERNEL_IMPORT_ALLOWLIST
            ):
                modules = [module]
            case _:
                continue
        line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
        violations.extend(
            SourceViolation(
                line=node.lineno,
                subject=module,
                text=line.strip(),
                message=f"policy kernel imports non-hermetic module {module}",
            )
            for module in modules
        )
    return violations


def literal_string(node: ast.AST) -> str | None:
    """Fold only statically known string syntax for the spelling audit."""
    match node:
        case ast.Constant(value=str(value)):
            return value
        case ast.BinOp(left=left, op=ast.Add(), right=right):
            before = literal_string(left)
            after = literal_string(right)
            return before + after if before is not None and after is not None else None
        case ast.JoinedStr(values=values):
            parts = [
                part for value in values if (part := literal_string(value)) is not None
            ]
            return "".join(parts) if len(parts) == len(values) else None
        case ast.FormattedValue(value=ast.Constant(value=str(value))):
            return value
    return None


def native_spelling_violations(text: str) -> list[SourceViolation]:
    """Find provider wire spellings in code strings outside native ownership."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines()
    context = PythonContext.parse(text)
    violations: list[SourceViolation] = []  # lup: ignore[empty-collection]
    folded_children: set[int] = set()  # lup: ignore[set-shape,empty-collection]
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp | ast.JoinedStr):
            folded_children.update(
                id(child) for child in ast.walk(node) if child is not node
            )
        if id(node) in folded_children:
            continue
        value = literal_string(node)
        line_number = getattr(node, "lineno", 0)
        if value is None or line_number in context.docstring_lines:
            continue
        line = lines[line_number - 1] if 0 < line_number <= len(lines) else ""
        for spelling, description in NATIVE_SPELLINGS.items():
            if spelling not in value:
                continue
            violations.append(
                SourceViolation(
                    line=line_number,
                    text=line.strip(),
                    subject=spelling,
                    message=(
                        f"neutral module contains {description} spelling {spelling!r}"
                    ),
                )
            )
    return violations


def collection_entries(node: ast.expr) -> int | None:
    """How many entries a collection display holds, or ``None`` if it is not one.

    Only a written-out display counts. A comprehension is derived from another
    value rather than declared, and a scalar is a single fact, not a table.

    A constructor wrapping one display is that display: ``dict.fromkeys([...])``
    and ``frozenset({...})`` write down the same vocabulary a bare display
    does, and a table that escaped judgement by naming its own container would
    be the easiest thing in the world to reach for.
    """
    match node:
        case ast.List(elts=elts) | ast.Tuple(elts=elts) | ast.Set(elts=elts):
            return len(elts)
        case ast.Dict(keys=keys):
            return len(keys)
        case ast.Call(args=[ast.expr() as only]):
            return collection_entries(only)
    return None


def frozen_literal(node: ast.expr) -> bool:
    """Whether a scalar value is decided here rather than derived from a name.

    A value that names another symbol — a constant, a call, an attribute, an
    interpolated string — follows from that symbol, so the choice it embodies
    was made where the symbol was declared and is judged there instead. A
    collection display never reaches this test: its judgement is which entries
    it holds, and that is chosen here however each entry is spelled.
    """
    match node:
        case ast.Constant():
            return True
        case ast.UnaryOp(operand=operand):
            return frozen_literal(operand)
        # A bare constructor over literals writes down a value the same way:
        # the characters `set("$*?")` holds are as much a choice as a
        # display's entries, and a name that is only the container is no name.
        # Only a plain name counts as the constructor — an attribute chain
        # such as `resources.files(...).read_text(...)` takes literal
        # arguments while its value comes from somewhere else entirely.
        case ast.Call(func=ast.Name(), args=args, keywords=keywords) if (
            args and not keywords
        ):
            return all(frozen_literal(argument) for argument in args)
    return False


def constant_declarations(text: str) -> list[ConstantDeclaration]:
    """Every module-level shouty constant whose value is a choice made here."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines()

    def declared(node: ast.stmt) -> ConstantDeclaration | None:
        match node:
            case (
                ast.Assign(targets=[ast.Name(id=name)], value=value)
                | ast.AnnAssign(target=ast.Name(id=name), value=ast.expr() as value)
            ):
                entries = collection_entries(value)
            case _:
                return None
        if not name.isupper() or (entries is None and not frozen_literal(value)):
            return None
        line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
        return ConstantDeclaration(
            name=name,
            line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            text=line.strip(),
            entries=entries,
        )

    found = [
        declaration for node in tree.body if (declaration := declared(node)) is not None
    ]
    occupied = {
        line
        for declaration in found
        for line in range(declaration.line, declaration.end_line + 1)
    }

    def heading_from(line: int) -> int:
        if line - 1 in occupied:
            return line
        start = line - 1
        while (
            start > 1
            and lines[start - 1].strip().startswith("#")
            and lines[start - 2].strip().startswith("#")
        ):
            start -= 1
        return start

    return [
        declaration.model_copy(
            update={"directive_from": heading_from(declaration.line)}
        )
        for declaration in found
    ]


def default_position_names(
    text: str,
) -> set[str]:  # lup: ignore[set-shape] — name identity membership
    """Constant names one module reaches as a caller-replaceable default.

    Four spellings count, and only these: a parameter default in a signature,
    a field default in a class body — written plainly or through pydantic's
    ``Field`` (or a ``default_factory`` lambda returning the constant) — and
    the two shapes a mutable default is written as, the ``TABLE if argument is
    None else argument`` sentinel and the ``argument or TABLE`` fallback.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()  # lup: ignore[set-shape] — an unparseable module names nothing

    def reached(node: ast.expr | None) -> list[str]:
        match node:
            case ast.Name(id=name) | ast.Lambda(body=ast.Name(id=name)):
                return [name]
        return []

    def assigned(node: ast.stmt) -> list[ast.expr | None]:
        match node:
            case ast.AnnAssign(value=value) | ast.Assign(value=value):
                return [value]
        return []

    def defaults(node: ast.AST) -> list[ast.expr | None]:
        match node:
            case ast.FunctionDef(args=args) | ast.AsyncFunctionDef(args=args):
                return [*args.defaults, *args.kw_defaults]
            # A field a class body assigns is the plain spelling of the same
            # override a `Field(default=...)` writes out, and the one a model
            # is usually written with; a subclass replaces either.
            case ast.ClassDef(body=body):
                return [value for member in body for value in assigned(member)]
            case ast.Call(keywords=keywords):
                return [
                    keyword.value
                    for keyword in keywords
                    if keyword.arg in ("default", "default_factory")
                ]
            case ast.IfExp(
                test=ast.Compare(
                    ops=[ast.Is() | ast.IsNot()], comparators=[ast.Constant(value=None)]
                ),
                body=body,
                orelse=orelse,
            ):
                return [body, orelse]
            case ast.BoolOp(op=ast.Or(), values=values):
                return list(values)
        return []

    return {
        name
        for node in ast.walk(tree)
        for default in defaults(node)
        for name in reached(default)
    }


# lup: ignore[library-default] — the string methods that carve a value out of
# text, a set the language fixes rather than this project
CARVING_CALLS = (
    "removeprefix",
    "removesuffix",
    "partition",
    "rpartition",
    "split",
    "rsplit",
    "strip",
    "lstrip",
    "rstrip",
    "replace",
)


def carved_names(
    text: str,
) -> set[str]:  # lup: ignore[set-shape] — name identity membership
    """Constant names one module hands to a string-surgery call.

    A constant reached this way exists only because a structured value is
    carved out of text by hand — the suffix a timestamp ends in, the separator
    a path is split on. Parametrizing it would freeze the surgery behind a
    nicer name, so the rule steers these to the parser that already knows the
    format instead.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()  # lup: ignore[set-shape] — an unparseable module carves nothing

    def carved(node: ast.AST) -> list[str]:
        match node:
            case ast.Call(func=ast.Attribute(attr=attribute), args=args) if (
                attribute in CARVING_CALLS
            ):
                return [
                    argument.id for argument in args if isinstance(argument, ast.Name)
                ]
        return []

    return {name for node in ast.walk(tree) for name in carved(node)}


def library_default_violations(
    text: str,
    overridable: Collection[str],
) -> list[SourceViolation]:
    """Find declared library tables no caller can replace with their own."""
    return [
        SourceViolation(
            line=constant.line,
            text=constant.text,
            subject=constant.name,
            message=(
                f"library table {constant.name} ({constant.entries} entries) is a "
                "project choice no caller can replace — take it as a default"
            ),
        )
        for constant in constant_declarations(text)
        if constant.name not in overridable
        and constant.judging_rule(library_module=True) == LIBRARY_DEFAULT_RULE_ID
    ]


# lup: ignore[model-free-function] — the audited path and text are the subject
def constant_declaration_violations(
    rel_path: Path,
    text: str,
    overridable: Collection[str],
    carved: Collection[str],
    application: ApplicationRoots = NO_APPLICATION,
) -> list[SourceViolation]:
    """Find frozen constants at one path that no caller can replace."""
    if application.renders(rel_path):
        return []
    library_module = library_placement_path_is_audited(rel_path)
    return [
        SourceViolation(
            line=constant.line,
            text=constant.text,
            subject=constant.name,
            message=constant.judgement(constant.name in carved),
        )
        for constant in constant_declarations(text)
        if constant.name not in overridable
        and constant.judging_rule(library_module) == CONSTANT_DECLARATION_RULE_ID
    ]


# lup: ignore[model-free-function] — the project's sources are the subject
def audit_constant_declarations(
    sources: list[PythonSource], application: ApplicationRoots = NO_APPLICATION
) -> list[RuleFinding]:
    """Judge every frozen constant in a project against how it is reached.

    Whether a caller can replace a constant, and whether it exists only to
    carve text by hand, are properties of the project rather than of the
    module that writes the value down — so both are pooled across every source
    before any one declaration is judged, the way the library placement sweep
    already pools the names its own callers can replace.

    A generated tree is read for what it reaches and never judged or audited:
    its files are compiled from the declarations above, so a directive in one
    is a copy of a directive that already answers for itself where it was
    written, and reporting the copy would ask for a second one nothing can act
    on.
    """
    overridable = {
        name for source in sources for name in default_position_names(source.text)
    }
    carved = {name for source in sources for name in carved_names(source.text)}
    authored = [source for source in sources if not application.renders(source.path)]
    violations = [
        RuleViolation(
            path=source.path,
            line=violation.line,
            message=violation.message,
        )
        for source in authored
        for violation in constant_declaration_violations(
            source.path, source.text, overridable, carved, application
        )
    ]
    return audit_suppressions(authored, violations, CONSTANT_DECLARATION_RULE_ID)


def audit_rule(
    text: str, rule_id: str, violations: list[SourceViolation]
) -> list[BoundaryAuditFinding]:
    """Apply ordinary inline/file suppression auditing to one boundary rule."""
    context = PythonContext.parse(text)
    file_ignore = file_level_ignore(text)
    lines = text.splitlines()
    directives: list[BoundaryDirective] = []  # lup: ignore[empty-collection]
    if file_ignore is not None:
        directives.append(
            BoundaryDirective(
                line=file_ignore.line,
                rule_ids=file_ignore.rule_ids,
                file_level=True,
            )
        )
    for line_number, line in enumerate(lines, start=1):
        if file_ignore is not None and line_number == file_ignore.line:
            continue
        match = IGNORE_RE.search(line)
        if match is None or not context.comment_at(line_number, match.start()):
            continue
        directives.append(
            BoundaryDirective(
                line=line_number,
                rule_ids=ignore_rule_ids(match),
            )
        )

    used: set[int] = set()  # lup: ignore[set-shape,empty-collection]
    untyped: set[int] = set()  # lup: ignore[set-shape,empty-collection]
    findings: list[BoundaryAuditFinding] = []  # lup: ignore[empty-collection]
    for violation in violations:
        candidates = [
            (index, directive)
            for index, directive in enumerate(directives)
            if (
                directive.file_level
                or suppression_reaches(lines, directive.line, violation.line)
            )
            and (directive.rule_ids is None or rule_id in directive.rule_ids)
        ]
        if not candidates:
            findings.append(
                BoundaryAuditFinding(
                    kind="missing",
                    line=violation.line,
                    text=violation.text,
                    message=(
                        f"{violation.message} — suppress on "
                        f"{suppression_placement(violation.line)}"
                    ),
                    rule_id=rule_id,
                    module=violation.subject,
                )
            )
            continue
        index, directive = candidates[0]
        used.add(index)
        if directive.rule_ids is None and index not in untyped:
            findings.append(
                BoundaryAuditFinding(
                    kind="untyped",
                    line=directive.line,
                    text=violation.text,
                    message=(
                        f"bare suppression covers {rule_id}; use "
                        f"# lup: ignore[{rule_id}] with a reason"
                    ),
                    rule_id=rule_id,
                    module=violation.subject,
                )
            )
            untyped.add(index)
    for index, directive in enumerate(directives):
        rule_ids = directive.rule_ids
        if index in used or rule_ids is None or rule_id not in rule_ids:
            continue
        findings.append(
            BoundaryAuditFinding(
                kind="spurious",
                line=directive.line,
                text="",
                message=f"suppression names {rule_id} but guards no violation",
                rule_id=rule_id,
            )
        )
    return findings


def audit_boundaries(text: str) -> list[BoundaryAuditFinding]:
    """Audit native imports, native spellings, and both rule suppressions."""
    return [
        *audit_rule(text, RULE_ID, import_violations(text)),
        *audit_rule(
            text,
            NATIVE_SPELLING_RULE_ID,
            native_spelling_violations(text),
        ),
    ]


# lup: ignore[model-free-function] — the audited path and text are the subject
def audit_path_boundaries(
    rel_path: Path, text: str, application: ApplicationRoots = NO_APPLICATION
) -> list[BoundaryAuditFinding]:
    """Audit only the boundary rules that apply at one repository path."""
    findings: list[BoundaryAuditFinding] = []
    if not path_is_sanctioned(rel_path, application):
        findings.extend(audit_rule(text, RULE_ID, import_violations(text)))
    if not native_spelling_path_is_sanctioned(rel_path, application):
        findings.extend(
            audit_rule(
                text,
                NATIVE_SPELLING_RULE_ID,
                native_spelling_violations(text),
            )
        )
    return findings


def audit_kernel_imports(text: str) -> list[BoundaryAuditFinding]:
    """Audit the canonical kernel against its pinned dependency allowlist."""
    return audit_rule(text, KERNEL_IMPORT_RULE_ID, kernel_import_violations(text))


def audit_library_defaults(
    text: str,
    overridable: Collection[str],
) -> list[BoundaryAuditFinding]:
    """Audit one library module's tables against the names callers can replace."""
    return audit_rule(
        text,
        LIBRARY_DEFAULT_RULE_ID,
        library_default_violations(text, overridable),
    )


def find_boundary_breaches(text: str) -> list[BoundaryBreach]:
    """Find native adapter imports through Python syntax, honoring suppressions."""
    return [
        BoundaryBreach(line=item.line, module=item.module, text=item.text)
        for item in audit_rule(text, RULE_ID, import_violations(text))
        if item.kind == "missing"
    ]


def find_native_spelling_breaches(text: str) -> list[BoundaryBreach]:
    """Find native wire spellings in neutral code, honoring suppressions."""
    return [
        BoundaryBreach(line=item.line, module=item.module, text=item.text)
        for item in audit_rule(
            text,
            NATIVE_SPELLING_RULE_ID,
            native_spelling_violations(text),
        )
        if item.kind == "missing"
    ]


def find_library_default_breaches(
    text: str,
    overridable: Collection[str],
) -> list[BoundaryBreach]:
    """Find unsuppressed baked-in library tables, honoring suppressions."""
    return [
        BoundaryBreach(line=item.line, module=item.module, text=item.text)
        for item in audit_library_defaults(text, overridable)
        if item.kind == "missing"
    ]


def find_kernel_import_breaches(text: str) -> list[BoundaryBreach]:
    """Find unsuppressed non-hermetic imports in the policy kernel."""
    return [
        BoundaryBreach(line=item.line, module=item.module, text=item.text)
        for item in audit_kernel_imports(text)
        if item.kind == "missing"
    ]
