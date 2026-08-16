# lup: ignore[set-shape, string-split]
# Resolved-name sets over the shared symbol index are this AST rule's domain,
# and splitting a qualified name to its terminal symbol is how it reports one.
"""Project-wide AST enforcement against dispatching on our own model types.

A union we declare answers questions through its members: the base names the
operation and each variant answers or declines it, so a new variant is one
class instead of an edit to every walk that would have to notice it. Branching
on the variant's *type* instead — `isinstance`, a `case ClassName()` arm, an
`assert_never` net — moves that knowledge out into the walks, where a filter
goes stale by omission the moment a variant is added.

Narrowing untyped data is the opposite case and stays legitimate: a vendor
payload, a `JsonValue`, an `ast` node are alternatives that are not ours to
give a method to. A line regex cannot tell the two apart, because they are the
same characters — the difference is only in what the named type *resolves to*.
This rule resolves it, through the project-wide class index in
:mod:`lup.codescan.project`: it fires only when the matched type is a class
this repository defines that inherits `pydantic.BaseModel`, and stays silent
on every builtin, standard-library, and vendor type.
"""

import ast

from lup.codescan.common import PythonSource
from lup.codescan.project import (
    RuleFinding,
    RuleViolation,
    audit_suppressions,
    build_symbol_index,
    descendants_of,
    dotted_name,
    imported_names,
    named_types,
    resolve_name,
)

# lup: ignore[constant-declaration] — the rule's own identity, what a typed
# directive and every deny message name it by
RULE_ID = "own-model-dispatch"

# lup: ignore[constant-declaration] — the import path pydantic publishes
MODEL_BASES = {"pydantic.BaseModel"}
"""Roots whose project-defined descendants count as models we declare."""

# lup: ignore[library-default] — Python's own narrowing builtins; the value follows the language, not a project's taste
NARROWING_CALLS = {"isinstance", "issubclass"}
"""Builtins that branch on a runtime type rather than asking the value."""

# lup: ignore[constant-declaration] — typing's own name for it, not ours
EXHAUSTIVENESS_CALL = "assert_never"
"""The static net that only exists to catch a union gaining a member."""

# lup: ignore[constant-declaration] — the rule's own sentence, declared with what
# it detects rather than chosen per caller
REMEDY = (
    "declare the operation on the union's base and let each variant answer or "
    "decline it"
)


def dispatch_violations(
    sources: list[PythonSource], models: set[str]
) -> list[RuleViolation]:
    """Find every site that branches on a project model's own type."""
    violations: list[RuleViolation] = []
    for source in sources:
        try:
            tree = ast.parse(source.text)
        except SyntaxError:
            continue
        aliases = imported_names(tree, source.module)

        def report(node: ast.expr | ast.pattern, message: str) -> None:
            violations.append(
                RuleViolation(path=source.path, line=node.lineno, message=message)
            )

        def matched_model(name: str) -> str | None:
            resolved = resolve_name(name, source.module, aliases)
            return resolved.rsplit(".", 1)[-1] if resolved in models else None

        for node in ast.walk(tree):
            match node:
                case ast.Call(func=ast.Name(id=call), args=[_, checked, *_]) if (
                    call in NARROWING_CALLS
                ):
                    for name in named_types(checked):
                        if (model := matched_model(name)) is not None:
                            report(
                                node,
                                f"{call} branches on {model}, a model we declare "
                                f"— {REMEDY}",
                            )
                case ast.MatchClass(cls=pattern):
                    name = dotted_name(pattern)
                    if name is not None and (model := matched_model(name)) is not None:
                        report(
                            node,
                            f"case arm matches {model}, a model we declare — {REMEDY}",
                        )
                case ast.Call(func=ast.Name(id=call)) if call == EXHAUSTIVENESS_CALL:
                    report(
                        node,
                        f"{EXHAUSTIVENESS_CALL} nets a union we declare — a base "
                        "that declines by default leaves nothing to be exhaustive "
                        "about",
                    )
    return violations


def audit_own_model_dispatch(sources: list[PythonSource]) -> list[RuleFinding]:
    """Build the project index, enforce the rule, and audit its suppressions."""
    symbols = build_symbol_index(sources)
    violations = dispatch_violations(sources, descendants_of(symbols, MODEL_BASES))
    return audit_suppressions(sources, violations, RULE_ID)
