# lup: ignore[dict-str-payload, empty-collection, set-shape, string-split]
# Resolved-name sets and the import alias table over the shared symbol index are
# this AST rule's domain, splitting a qualified name to its terminal symbol is
# how it reports one, and the violation list accumulates across a walk whose
# unparseable files are skipped rather than mapped.
"""Project-wide AST enforcement against behaviour written beside a model.

A model we declare answers questions itself: the base names the operation and
each variant answers or declines it. A free function that takes one — `def
parse_model(part: TextPart) -> str` — puts that operation somewhere the model
cannot see, so what a type can do is spread across the modules that happen to
call it rather than readable in one place, and composing two of them means
composing two module namespaces instead of two objects.

The rule is about *free* functions only. A method inside a model body is the
shape this steers toward, not away from, and the conventions require it — so
the walk reads a module's own top level and never descends into a class. What
a name resolves to decides the rest: the project-wide class index in
:mod:`lup.codescan.project` says whether an annotation names a class this
repository defines that inherits `pydantic.BaseModel`, so a function over a
vendor payload, an `ast` node, or a builtin is never reported.

Two narrowings past that are the shape rather than accidents of it, and both
are about what a method could even mean. A **return** does not count, because
a function whose return names a model is a constructor: it builds the value
rather than acting on one, so there is no instance to carry "build me one" and
the operation has nowhere to move to. A parameter whose model is declared in
**another module** does not count either. The case that narrowing is drawn for
is the boundary converter — a function sitting on one side of a seam over a
model that belongs to neither, where moving the operation onto the model would
push the converter's own knowledge into code the seam exists to keep clear of
it — but same-module is a proxy for it rather than the thing itself, and it is
the wider of the two: it also clears a function whose model simply lives
elsewhere, where a method would have read perfectly well. That is the recall
the narrowing is bought with, and it is deliberate, because the alternative
reports every seam in the repository.

So the shape is a proxy twice over, and worth naming as one: the defect is
behaviour that *belongs* on the model, no syntactic rule can decide belonging,
and neither narrowing above is coextensive with the case it was drawn for.
This shape is the closest mechanical stand-in, so it over-reports by design in
one direction and under-reports in the other — a site it names that genuinely
reads better as a free function answers with a typed
`# lup: ignore[model-free-function]` and the reason, which is the judgement
being recorded rather than the rule being evaded, and a site it stays silent
on is not thereby endorsed.
"""

import ast

from lup.codescan.common import PythonSource
from lup.codescan.project import (
    RuleFinding,
    RuleViolation,
    audit_suppressions,
    build_symbol_index,
    descendants_of,
    imported_names,
    named_types,
    resolve_name,
)

# lup: ignore[constant-declaration] — the rule's own identity, what a typed
# directive and every deny message name it by
RULE_ID = "model-free-function"

# lup: ignore[constant-declaration] — the import path pydantic publishes
MODEL_BASES = {"pydantic.BaseModel"}
"""Roots whose project-defined descendants count as models we declare."""

# lup: ignore[constant-declaration] — the rule's own sentence, declared with what
# it detects rather than chosen per caller
REMEDY = (
    "declare it on the model — or on the ABC the model composes — so the type "
    "carries what can be done with it"
)


def annotated_models(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    models: set[str],
    module: str,
    aliases: dict[str, str],
) -> list[str]:
    """Every model this signature takes as a parameter.

    Only what an annotation names directly, through the union and tuple
    spellings of "any of these". A container of models is a collection the
    function walks, and the operation a walk performs belongs to the walk
    rather than to any one member it visits. The return annotation is not read
    at all: naming a model there makes the function that model's constructor,
    which is the one operation an instance method cannot express.
    """
    annotations = [
        argument.annotation
        for group in (node.args.posonlyargs, node.args.args, node.args.kwonlyargs)
        for argument in group
    ]
    annotations.extend(
        argument.annotation
        for argument in (node.args.vararg, node.args.kwarg)
        if argument is not None
    )
    named = [
        resolve_name(name, module, aliases)
        for annotation in annotations
        if annotation is not None
        for name in named_types(annotation)
    ]
    return sorted(
        {resolved.rsplit(".", 1)[-1] for resolved in named if resolved in models}
    )


def free_function_violations(
    sources: list[PythonSource], models: set[str]
) -> list[RuleViolation]:
    """Find every module-level function taking a model its own module declares."""
    violations: list[RuleViolation] = []
    for source in sources:
        try:
            tree = ast.parse(source.text)
        except SyntaxError:
            continue
        aliases = imported_names(tree, source.module)
        own = {name for name in models if name.startswith(f"{source.module}.")}
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            named = annotated_models(node, own, source.module, aliases)
            if not named:
                continue
            # The decorated definition opens at its first decorator, and that is
            # where the violation is reported: a directive guards what it stands
            # above, so reporting at the `def` would put the accepted placement
            # inside the decoration rather than in front of it.
            decorators = node.decorator_list
            violations.append(
                RuleViolation(
                    path=source.path,
                    line=decorators[0].lineno if decorators else node.lineno,
                    message=(
                        f"{node.name} is a free function over "
                        f"{', '.join(named)}, a model we declare — {REMEDY}"
                    ),
                )
            )
    return violations


def audit_model_free_functions(sources: list[PythonSource]) -> list[RuleFinding]:
    """Build the project index, enforce the rule, and audit its suppressions."""
    symbols = build_symbol_index(sources)
    violations = free_function_violations(sources, descendants_of(symbols, MODEL_BASES))
    return audit_suppressions(sources, violations, RULE_ID)
