# lup: ignore[empty-collection]
# Resolution caches and per-file groupings are keyed folds over a walk that
# cannot be expressed as a comprehension.
"""Typed AST grammar: rules that judge a site by what its subject resolves to.

The anti-pattern set detects *spellings*. A spelling is all the hermetic edit
hook can see — it classifies fragments of a proposed edit, which carry no
types and often do not parse — so `.get(` there means every `.get(` on every
receiver. The whole-file audit reads finished, parseable source and can do
better: it knows the AST, and through the `lup.codescan.oracle` port it can
ask a type checker what a name actually resolves to. `payload.get(...)` on a
mapping is the schema-hiding access the rule is about; the identical spelling
on an HTTP client or a route decorator is not, and the audit should say so
instead of leaving a contributor to suppress by reflex.

A rule here is three things: a **selector** that walks an AST and yields the
sites it is about, a **family** of classes the site's subject must belong to
for the rule to stand, and the prose explaining the refinement in the rule
reference. Selectors are typed callables rather than a matcher mini-language,
so a new rule is a function over `ast` rather than an extension to a schema —
`isinstance` narrowing over a project model union, for instance, selects call
arguments where this rule selects call receivers, and reuses everything else.

Resolution is deliberately structural. The oracle answers *where* a symbol is
declared, never what its type is named, and the engine reads the declaring
class out of that file's AST. So membership is decided by real declarations —
`dict` in typeshed's `builtins.pyi`, `_Environ(MutableMapping[...])` in
`os`'s stub — instead of by pattern-matching rendered type strings, and a
receiver whose declaration the checker cannot find yields no verdict at all.

That last case is the engine's default. A rule refutes a line only when every
site on it resolves to a declaration proven outside the family; unresolved,
unparseable, and oracle-less runs all leave the broad regex verdict standing,
so the refinement can only ever remove false positives and never hide a real
one.
"""

import ast
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from lup.codescan.common import PythonSource, Refutation
from lup.codescan.oracle import DefinitionOracle, DefinitionSite, SourcePosition


class MatchSite(BaseModel, frozen=True):
    """One AST site a selector chose, and the symbol that decides its fate.

    ``line`` is where the finding and any `# lup: ignore` guarding it sit —
    the line the broad regex would flag. ``query_line``/``query_column`` point
    at the symbol whose declaration settles the site, which is usually on that
    same line but need not be. ``subject`` is the unparsed source of the
    expression the verdict is about, quoted back as evidence.
    """

    line: int
    query_line: int
    query_column: int
    subject: str


type SiteSelector = Callable[[ast.Module], list[MatchSite]]
"""Walks one parsed module and yields every site a rule is about."""


class TypeFamily(BaseModel, frozen=True):
    """A named set of declaring classes membership is decided against.

    ``classes`` names the declarations that constitute the family. A subject
    belongs when its declaring class is one of them, or inherits one directly
    — which is what carries `os._Environ(MutableMapping[AnyStr, AnyStr])` and
    a project's own `dict` subclass into the mapping family without listing
    either.
    """

    name: str
    classes: list[str]


class GrammarRule(BaseModel, frozen=True):
    """One typed AST rule: the sites it selects and the family they must be in.

    ``id`` is the anti-pattern rule id this refines, so one vocabulary spans
    the hook's deny message, the `# lup: ignore[id]` directive, and this
    refinement — a rule is never two rules because it gained a type-aware
    verdict.
    """

    id: str
    select: SiteSelector
    family: TypeFamily
    refinement: str


class ClassOrigin(BaseModel, frozen=True):
    """The class a resolved declaration belongs to, and where it was found.

    ``bases`` holds each base's declared name without the module path that
    qualifies it, because family membership is about the class a stub names —
    `MutableMapping`, whether spelled bare or as `collections.abc.MutableMapping`.
    """

    name: str
    bases: list[str]
    path: Path
    line: int

    def in_family(self, family: TypeFamily) -> bool:
        """Whether this declaring class is the family, or directly inherits it."""
        return self.name in family.classes or any(
            base in family.classes for base in self.bases
        )


def base_name(node: ast.expr) -> str | None:
    """The declared name of one base class, unqualified and unsubscripted."""
    match node:
        case ast.Name(id=name):
            return name
        case ast.Attribute(attr=attribute):
            return attribute
        case ast.Subscript(value=value):
            return base_name(value)
    return None


MAPPING_FAMILY = TypeFamily(
    name="mapping",
    classes=[
        "dict",
        "Mapping",
        "MutableMapping",
        "MappingProxyType",
        "TypedDict",
        "_TypedDict",
        "UserDict",
        "Counter",
        "OrderedDict",
        "defaultdict",
        "ChainMap",
    ],
)
"""Declarations that make a `.get` receiver a keyed lookup rather than a client."""


def attribute_call_sites(attribute: str) -> SiteSelector:
    """Select every `receiver.<attribute>(...)` call, keyed on the receiver.

    The queried symbol is the attribute itself: asking where `get` is declared
    resolves through the receiver's type to the class that defines it, which
    is the fact the rule needs and the one a checker answers most directly.
    """

    def site_of(node: ast.Attribute) -> MatchSite:
        line = node.end_lineno or node.lineno
        return MatchSite(
            line=line,
            query_line=line,
            query_column=(node.end_col_offset or 0) - len(attribute),
            subject=ast.unparse(node.value),
        )

    def select(tree: ast.Module) -> list[MatchSite]:
        return [
            site_of(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == attribute
        ]

    return select


GRAMMAR_RULES: list[GrammarRule] = [
    GrammarRule(
        id="dict-get",
        select=attribute_call_sites("get"),
        family=MAPPING_FAMILY,
        refinement=(
            "The whole-file audit resolves what the receiver's `get` is declared "
            "on and drops the finding when that class is proven outside the "
            "mapping family — an HTTP client, a route decorator. The edit hook "
            "keeps flagging every `.get(`, because an edit fragment carries no "
            "types and the hermetic kernel may not reach a checker."
        ),
    ),
]
"""Every anti-pattern rule the typed grammar sharpens, in evaluation order."""


def class_at(tree: ast.Module, line: int) -> ast.ClassDef | None:
    """The innermost class whose body spans `line`, or None outside any class."""
    enclosing = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and node.lineno <= line <= (node.end_lineno or node.lineno)
    ]
    return max(enclosing, key=lambda node: node.lineno) if enclosing else None


def origin_of(
    site: DefinitionSite, trees: dict[str, ast.Module | None]
) -> ClassOrigin | None:
    """Read the class a declaration belongs to out of the file declaring it."""
    key = site.path.as_posix()
    if key not in trees:
        try:
            trees[key] = ast.parse(site.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            trees[key] = None
    tree = trees[key]
    if tree is None:
        return None
    node = class_at(tree, site.line)
    if node is None:
        return None
    return ClassOrigin(
        name=node.name,
        bases=[name for base in node.bases if (name := base_name(base)) is not None],
        path=site.path,
        line=node.lineno,
    )


class SelectedSite(BaseModel, frozen=True):
    """One site a rule selected, tagged with the file and rule it came from."""

    file: str
    rule: GrammarRule
    site: MatchSite


def selected_sites(
    sources: list[PythonSource], rules: list[GrammarRule]
) -> list[SelectedSite]:
    """Run every rule's selector over every parseable source, in file order."""
    selected: list[SelectedSite] = []
    for source in sources:
        try:
            tree = ast.parse(source.text)
        except (SyntaxError, ValueError):
            continue  # the audit's regex pass still covers text that will not parse
        selected.extend(
            SelectedSite(file=source.path.as_posix(), rule=rule, site=site)
            for rule in rules
            for site in rule.select(tree)
        )
    return selected


def refute(
    sources: list[PythonSource],
    oracle: DefinitionOracle | None,
    rules: list[GrammarRule] | None = None,
) -> dict[str, list[Refutation]]:
    """Refute every line whose sites all resolve outside their rule's family.

    Returns the surviving refutations per repository-relative posix path, for
    `lup.codescan.antipatterns.audit_text` to drop and for the auditor to
    report. Without an oracle — the checker is not installed, or a caller
    chose not to pay for it — nothing resolves and nothing is refuted, which
    is exactly the broad regex behaviour.

    A line carrying several sites of one rule is refuted only when every one
    of them is: one mapping access among three client calls still hides a
    schema, and the directive guarding that line still guards something.
    """
    active = GRAMMAR_RULES if rules is None else rules
    if oracle is None or not active:
        return {}

    selected = selected_sites(sources, active)
    resolved = oracle.definitions(
        [
            SourcePosition(
                path=Path(chosen.file),
                line=chosen.site.query_line,
                column=chosen.site.query_column,
            )
            for chosen in selected
        ]
    )

    trees: dict[str, ast.Module | None] = {}

    def refutation_for(
        chosen: SelectedSite, definitions: list[DefinitionSite]
    ) -> Refutation | None:
        origins = [
            origin
            for definition in definitions
            if (origin := origin_of(definition, trees)) is not None
        ]
        if not origins or any(
            origin.in_family(chosen.rule.family) for origin in origins
        ):
            return None
        foreign = origins[0]
        return Refutation(
            rule_id=chosen.rule.id,
            line=chosen.site.line,
            subject=chosen.site.subject,
            evidence=(
                f"`{chosen.site.subject}` resolves to `{foreign.name}` declared "
                f"at {foreign.path.as_posix()}:{foreign.line}, outside the "
                f"{chosen.rule.family.name} family"
            ),
        )

    judged = [
        (chosen, refutation_for(chosen, definitions))
        for chosen, definitions in zip(selected, resolved, strict=True)
    ]
    standing = {
        (chosen.file, chosen.rule.id, chosen.site.line)
        for chosen, refutation in judged
        if refutation is None
    }

    refutations: dict[str, list[Refutation]] = {}
    for chosen, refutation in judged:
        if refutation is None:
            continue
        if (chosen.file, chosen.rule.id, chosen.site.line) in standing:
            continue
        refutations.setdefault(chosen.file, []).append(refutation)
    return {
        file: sorted(rows, key=lambda row: (row.line, row.rule_id))
        for file, rows in refutations.items()
    }
