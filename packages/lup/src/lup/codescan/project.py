# lup: ignore[dict-str-payload, set-shape, empty-collection, string-split]
# Symbol tables, graph sets, and qualified-name resolution are this engine's domain.
"""Typed AST engine shared by every project-wide Lup rule.

A line regex sees characters; the rules that need to tell one construct from a
look-alike need names. This module reads the tree once and answers the two
questions those rules ask: *what does this name resolve to* — through imports,
aliases, and relative packages, into a project-wide class index — and *is this
site suppressed*, through the same `# lup: ignore` grammar the line scanners
use.

`lup.codescan.capabilities` and `lup.codescan.dispatch` are its customers.
Both hand it :class:`PythonSource` values, read :func:`build_symbol_index` for
resolved class shapes, and hand their mechanical violations back to
:func:`audit_suppressions`, which classifies each one as covered, missing,
bare, or guarding nothing at all.
"""

import ast
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from lup.codescan.common import (
    PythonContext,
    PythonSource,
    RuleStrength,
    file_level_ignore,
    ignore_rule_ids,
)
from lup.policy.kernel.edit import (
    IGNORE_RE,
    suppression_placement,
    suppression_reaches,
)

type FindingKind = Literal["missing", "untyped", "spurious"]
"""How a violation and the suppressions around it ended up related."""


class ClassSymbol(BaseModel, frozen=True):
    """Resolved class shape retained by the project-wide symbol index."""

    qualified_name: str
    module: str
    name: str
    path: Path
    line: int
    bases: list[str]
    abstract_methods: list[str] = []
    abstract_properties: list[str] = []
    concrete_callables: list[str] = []
    member_lines: dict[str, int] = {}


class RuleViolation(BaseModel, frozen=True):
    """One mechanical shape violation before suppression auditing.

    Where its suppression may sit is not a field, and deliberately so: a rule
    that could name its own accepted lines is a rule whose markers read
    differently from every other rule's, which is how one marker shape ends up
    valid here and spurious there. ``line`` is where the violation is, and
    :func:`~lup.policy.kernel.edit.suppression_reaches` decides the rest for
    every rule alike.
    """

    path: Path
    line: int
    message: str


class RuleFinding(BaseModel, frozen=True):
    """One missing, untyped, or spurious suppression verdict for a rule."""

    kind: FindingKind
    path: Path
    line: int
    message: str
    rule_id: str


class Directive(BaseModel, frozen=True):
    """One actual comment suppression that may cover a rule's violations."""

    path: Path
    line: int
    rule_ids: set[str] | None
    file_level: bool = False


def dotted_name(node: ast.expr) -> str | None:
    """Return a dotted syntax name without evaluating it."""
    match node:
        case ast.Name(id=name):
            return name
        case ast.Attribute(value=value, attr=attribute):
            parent = dotted_name(value)
            return f"{parent}.{attribute}" if parent is not None else None
        case ast.Subscript(value=value):
            return dotted_name(value)
    return None


def named_types(node: ast.expr) -> list[str]:
    """Every type name one expression names, without evaluating it.

    Handles the tuple and ``|`` spellings of "any of these types", so a
    narrowing call that checks several and an annotation that unions several
    both report each one. A subscript reports the container it names rather
    than its members: `list[TextPart]` is a list.
    """
    match node:
        case ast.Tuple(elts=elements):
            return [name for element in elements for name in named_types(element)]
        case ast.BinOp(left=left, op=ast.BitOr(), right=right):
            return [*named_types(left), *named_types(right)]
    name = dotted_name(node)
    return [] if name is None else [name]


def imported_names(tree: ast.Module, module: str) -> dict[str, str]:
    """Resolve local import aliases to absolute symbols or modules."""
    aliases: dict[str, str] = {}
    package = module.split(".")
    for node in tree.body:
        match node:
            case ast.Import(names=names):
                for item in names:
                    local = item.asname or item.name.split(".")[0]
                    aliases[local] = item.name if item.asname else local
            case ast.ImportFrom(module=target, names=names, level=level):
                if level:
                    prefix = package[:-level]
                    resolved_module = ".".join(
                        [*prefix, *([target] if target is not None else [])]
                    )
                else:
                    resolved_module = target or ""
                for item in names:
                    local = item.asname or item.name
                    aliases[local] = f"{resolved_module}.{item.name}"
    return aliases


def resolve_name(name: str, module: str, aliases: dict[str, str]) -> str:
    """Resolve one base/decorator name through imports and local classes."""
    head, separator, tail = name.partition(".")
    if head in aliases:
        resolved = aliases[head]
        return f"{resolved}.{tail}" if separator else resolved
    if separator:
        return name
    return f"{module}.{name}"


def has_decorator(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    expected: str,
    module: str,
    aliases: dict[str, str],
) -> bool:
    """Recognize a decorator by its resolved terminal symbol."""
    for decorator in node.decorator_list:
        name = dotted_name(decorator)
        if name is None:
            continue
        if resolve_name(name, module, aliases).rsplit(".", 1)[-1] == expected:
            return True
    return False


def declaration_line(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Where a member's declaration starts, decorators included.

    A decorated ``def`` opens at its first decorator, and that is the line a
    violation about the whole declaration belongs on. Reporting the ``def``
    would leave the accepted placement above it wedged between a decorator and
    the function it decorates — a legal comment nobody would write.
    """
    return min([node.lineno, *(item.lineno for item in node.decorator_list)])


def build_symbol_index(sources: list[PythonSource]) -> dict[str, ClassSymbol]:
    """Build import-resolved class symbols for all parseable supplied modules."""
    symbols: dict[str, ClassSymbol] = {}
    for source in sources:
        try:
            tree = ast.parse(source.text)
        except SyntaxError:
            continue
        aliases = imported_names(tree, source.module)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            abstract_methods: list[str] = []
            abstract_properties: list[str] = []
            concrete_callables: list[str] = []
            member_lines: dict[str, int] = {}
            for member in node.body:
                if not isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                member_lines[member.name] = declaration_line(member)
                abstract = has_decorator(
                    member, "abstractmethod", source.module, aliases
                )
                property_member = has_decorator(
                    member, "property", source.module, aliases
                )
                if abstract and property_member:
                    if member.name not in abstract_properties:
                        abstract_properties.append(member.name)
                elif abstract:
                    if member.name not in abstract_methods:
                        abstract_methods.append(member.name)
                else:
                    if member.name not in concrete_callables:
                        concrete_callables.append(member.name)
            bases = [
                resolve_name(name, source.module, aliases)
                for base in node.bases
                if (name := dotted_name(base)) is not None
            ]
            qualified = f"{source.module}.{node.name}"
            symbols[qualified] = ClassSymbol(
                qualified_name=qualified,
                module=source.module,
                name=node.name,
                path=source.path,
                line=node.lineno,
                bases=bases,
                abstract_methods=abstract_methods,
                abstract_properties=abstract_properties,
                concrete_callables=concrete_callables,
                member_lines=member_lines,
            )
    return symbols


def descendants_of(symbols: dict[str, ClassSymbol], ancestors: set[str]) -> set[str]:
    """Every indexed class whose resolved base chain reaches `ancestors`.

    Bases are matched on their resolved qualified name and on their terminal
    symbol, so a class reached through ``from pydantic import BaseModel`` and
    one reached through ``pydantic.BaseModel`` land in the same closure.
    """
    terminals = {name.rsplit(".", 1)[-1] for name in ancestors}
    reached = {
        name
        for name, symbol in symbols.items()
        if any(
            base in ancestors or base.rsplit(".", 1)[-1] in terminals
            for base in symbol.bases
        )
    }
    while True:
        expanded = reached | {
            name
            for name, symbol in symbols.items()
            if any(base in reached for base in symbol.bases)
        }
        if expanded == reached:
            return reached
        reached = expanded


def directives_for(source: PythonSource) -> list[Directive]:
    """Collect actual inline and file-level suppression comments."""
    context = PythonContext.parse(source.text)
    file_ignore = file_level_ignore(source.text)
    directives: list[Directive] = []
    if file_ignore is not None:
        directives.append(
            Directive(
                path=source.path,
                line=file_ignore.line,
                rule_ids=file_ignore.rule_ids,
                file_level=True,
            )
        )
    for line_number, line in enumerate(source.text.splitlines(), start=1):
        if file_ignore is not None and line_number == file_ignore.line:
            continue
        match = IGNORE_RE.search(line)
        if match is None or not context.comment_at(line_number, match.start()):
            continue
        directives.append(
            Directive(
                path=source.path,
                line=line_number,
                rule_ids=ignore_rule_ids(match),
            )
        )
    return directives


def audit_suppressions(
    sources: list[PythonSource],
    violations: list[RuleViolation],
    rule_id: str,
    strength: RuleStrength = "soft",
) -> list[RuleFinding]:
    """Pair a rule's violations with the suppressions written against it.

    A directive covers a violation from the violation's own line or from the
    line standing directly above it, and from nowhere else — the one policy
    :func:`~lup.policy.kernel.edit.suppression_reaches` holds for every rule,
    the line scanners, and the edit hook alike. A refused violation names both
    lines, so a marker written where nothing reads it says where to go.

    Each violation is covered, or reported "missing"; a bare `# lup: ignore`
    that covers one is reported "untyped" once, so the migration to typed
    directives stays visible; a directive naming `rule_id` that guards nothing
    is reported "spurious", so a rule cannot rot behind dead markers.

    A ``strong`` rule admits no directive at all: every violation is reported
    missing, and a directive written anyway covers nothing and so falls to the
    same spurious sweep. This mirrors what :func:`lup.codescan.antipatterns.
    audit_text` does for the line rules, so both surfaces refuse alike.
    """
    directives = [
        directive for source in sources for directive in directives_for(source)
    ]
    lines_of = {source.path: source.text.splitlines() for source in sources}
    used: set[int] = set()
    untyped_reported: set[int] = set()
    findings: list[RuleFinding] = []
    refusal = " (no suppression: write the replacement)" if strength == "strong" else ""
    for violation in violations:
        # A refusal that does not say where the marker belongs is the reported
        # failure itself: a directive goes spurious on one line while the
        # violation stays missing on another, and neither message connects them.
        expected = (
            f" — suppress on {suppression_placement(violation.line)}"
            f": `# lup: ignore[{rule_id}] — <why>`"
        )
        covering = [
            (index, directive)
            for index, directive in enumerate(directives)
            if strength == "soft"
            and directive.path == violation.path
            and (
                directive.file_level
                or suppression_reaches(
                    lines_of[violation.path], directive.line, violation.line
                )
            )
            and (directive.rule_ids is None or rule_id in directive.rule_ids)
        ]
        if not covering:
            findings.append(
                RuleFinding(
                    kind="missing",
                    path=violation.path,
                    line=violation.line,
                    message=f"{violation.message}{refusal or expected}",
                    rule_id=rule_id,
                )
            )
            continue
        index, directive = covering[0]
        used.add(index)
        if directive.rule_ids is None and index not in untyped_reported:
            findings.append(
                RuleFinding(
                    kind="untyped",
                    path=directive.path,
                    line=directive.line,
                    message=(
                        f"bare suppression covers {rule_id}; use "
                        f"# lup: ignore[{rule_id}] with a reason"
                    ),
                    rule_id=rule_id,
                )
            )
            untyped_reported.add(index)
    for index, directive in enumerate(directives):
        if (
            index in used
            or directive.rule_ids is None
            or rule_id not in directive.rule_ids
        ):
            continue
        findings.append(
            RuleFinding(
                kind="spurious",
                path=directive.path,
                line=directive.line,
                message=f"suppression names {rule_id} but guards no violation",
                rule_id=rule_id,
            )
        )
    return sorted(
        findings, key=lambda item: (item.path.as_posix(), item.line, item.kind)
    )
