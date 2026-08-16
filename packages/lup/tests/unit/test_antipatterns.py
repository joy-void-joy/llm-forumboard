# lup: ignore[import-re, re-call, set-shape, string-replace]
"""The anti-pattern set is single-sourced for the auditor and policy bundle.

`lup.codescan.antipatterns` is the importable source of truth. Harness generation
embeds its rows into the dependency-free policy runtime; these tests pin that
projection and audit missing, untyped, and spurious suppressions.
"""

import re

import pytest

from lup.codescan.antipatterns import (
    PYTHON_ANTI_PATTERNS,
    TS_ANTI_PATTERNS,
    AntiPattern,
    audit_text,
    python_anti_patterns,
)
from lup.harness.contracts import Spelled, Unsupported
from lup.policy.bundle import bundled_antipattern_rows
from lup.policy.kernel.edit import (
    antipattern_decision,
    default_factory_exempt_lines,
    dict_get_exempt_lines,
    empty_collection_exempt_lines,
    refiner_named,
)
from lup.policy.kernel.rows import AntiPatternRow
from lup.policy.rules import antipattern_row


def lib_rows(patterns: list[AntiPattern]) -> list[AntiPatternRow]:
    return [antipattern_row(ap) for ap in patterns]


def rule_named(patterns: list[AntiPattern], rule_id: str) -> AntiPattern:
    return next(rule for rule in patterns if rule.id == rule_id)


def test_python_table_matches_generated_bundle() -> None:
    assert lib_rows(PYTHON_ANTI_PATTERNS) == bundled_antipattern_rows()[".py"]


def test_ts_table_matches_generated_bundle() -> None:
    assert lib_rows(TS_ANTI_PATTERNS) == bundled_antipattern_rows()[".ts"]


STRONG_RULE_IDS = {
    "generic-base",
    "tuple-shape",
    "typing-generics",
    "typing-union",
    "utcnow",
    "var-declaration",
}
"""The rules whose replacement the language itself provides.

Most are successor spellings, where the replacement is the same type written
the modern way. ``tuple-shape`` is the one that changes the type rather than
its spelling: a `TypedDict` names what each position meant. It is strong only
because its pattern was narrowed to fixed arity first — `tuple[X, ...]` is an
immutable sequence with no field names to give, and a rule that demanded them
would be demanding something that does not exist.

Pinned so that promoting a rule stays a decision someone made rather than one
that arrived with a sweep, and so demoting one cannot pass unremarked.
"""


def test_the_strong_classification_is_the_declared_one() -> None:
    declared = {
        rule.id
        for table in (PYTHON_ANTI_PATTERNS, TS_ANTI_PATTERNS)
        for rule in table
        if rule.strength == "strong"
    }

    assert declared == STRONG_RULE_IDS


@pytest.mark.parametrize("rule_id", sorted(STRONG_RULE_IDS))
def test_the_hook_refuses_a_directive_the_audit_would_refuse(rule_id: str) -> None:
    """Both gates decide alike, or an admitted edit fails `dev check`.

    The kernel matches erased rows, so a strength the declaration carries and
    the projection drops would leave the hook honouring a directive the audit
    reports spurious — an edit permitted at the point of writing and rejected
    at the point of checking.
    """
    suffix, matching = {
        "generic-base": (".py", "class Box(Generic[T]): ..."),
        "tuple-shape": (".py", "pair: tuple[int, str] = (1, 2)"),
        "typing-generics": (".py", "values: List[int] = []"),
        "typing-union": (".py", "value: Optional[str] = None"),
        "utcnow": (".py", "stamp = datetime.utcnow()"),
        "var-declaration": (".ts", "var count = 1;"),
    }[rule_id]
    rows = [row for row in bundled_antipattern_rows()[suffix] if row["id"] == rule_id]
    assert rows, rule_id
    assert rows[0]["strength"] == "strong"

    decision = antipattern_decision(
        None,
        f"{matching}  // lup: ignore[{rule_id}]\n"
        if suffix == ".ts"
        else f"{matching}  # lup: ignore[{rule_id}]\n",
        rows,
        python_source=suffix == ".py",
    )

    assert decision is not None
    assert decision.effect == "deny"
    assert "write the replacement" in decision.reason


def test_a_soft_rule_is_still_honoured_by_the_hook() -> None:
    rows = [row for row in bundled_antipattern_rows()[".py"] if row["id"] == "any-type"]
    decision = antipattern_decision(
        None, "value: Any = 1  # lup: ignore[any-type]\n", rows, python_source=True
    )

    assert decision is None or decision.effect != "deny"


def suppression_rows() -> list[AntiPatternRow]:
    """One soft rule, so the suppression gate is what decides."""
    return [row for row in bundled_antipattern_rows()[".py"] if row["id"] == "any-type"]


def test_relocating_an_approved_marker_is_not_a_new_suppression() -> None:
    """Concern `suppression-placement-uniformity`, criterion spu-4, verbatim.

    Adopting a placement policy necessarily rewrites the markers the old one
    allowed. The gate demanded `antipattern-suppression` for the rewrite —
    an allowance that would equally authorize genuinely new suppressions, so
    a narrow, checkable action bought a wide, uncheckable permission.
    """
    before = (
        "class NativeSpellings:  # lup: ignore[any-type] — deliberately wider\n"
        "    first: Any = 1\n"
        "    second: Any = 2\n"
    )
    after = (
        "class NativeSpellings:\n"
        "    first: Any = 1  # lup: ignore[any-type] — deliberately wider\n"
        "    second: Any = 2  # lup: ignore[any-type] — deliberately wider\n"
    )

    decision = antipattern_decision(
        before, after, suppression_rows(), python_source=True
    )

    assert decision is None or decision.effect != "ask"


def test_a_violation_no_directive_covers_outranks_the_suppression_beside_it() -> None:
    """A new directive buys no approval for the violation it does not silence.

    The added line declares `dict-get` and trips `any-type`, so the directive
    covers nothing and the violation stands unsuppressed. Approving the edit
    for the suppression it declared would carry that violation through on a
    reason naming only the directive.

    That a genuinely new suppression is itself a judgement is unchanged, and
    the two tests beside this one pin it — each declares a directive that does
    cover the line it sits on, and each still asks.
    """
    before = "first: Any = 1  # lup: ignore[any-type]\n"
    after = (
        "first: Any = 1  # lup: ignore[any-type]\n"
        "second: Any = 2  # lup: ignore[dict-get]\n"
    )

    decision = antipattern_decision(
        before, after, suppression_rows(), python_source=True
    )

    assert decision is not None
    assert decision.effect == "deny"


def test_a_directive_heading_a_two_line_reason_guards_what_follows_it() -> None:
    """The placement a reason too long for the column budget has to take.

    The gate reads coverage in both directions and they disagreed: the
    forward check walks the whole comment block, while the check that decides
    whether a directive guards anything offered it a fixed pair of lines and
    so could not see a violation two below. The edit was admitted here and
    refused by `dev check` — a marker reported spurious while the violation it
    covers was reported missing.

    Asks rather than allows, because the suppression is newly declared and
    that is a judgement. What matters is that it is not denied as guarding
    nothing.
    """
    before = "first = 1\n"
    after = (
        "first = 1\n"
        "# lup: ignore[any-type] — the reason this exception is the right\n"
        "# call, which does not fit beside the line it justifies\n"
        "value: Any = 1\n"
    )

    decision = antipattern_decision(
        before, after, suppression_rows(), python_source=True
    )

    assert decision is not None
    assert decision.effect == "ask"


def test_a_typed_marker_widened_to_a_bare_one_still_asks() -> None:
    """Bare covers every rule, so going bare suppresses more than it did."""
    before = "value: Any = 1  # lup: ignore[any-type]\n"
    after = "value: Any = 1  # lup: ignore\n"

    decision = antipattern_decision(
        before, after, suppression_rows(), python_source=True
    )

    assert decision is not None
    assert decision.effect == "ask"


def test_a_marker_added_where_the_edit_removed_none_still_asks() -> None:
    """Nothing was re-sited: this is a first suppression, wherever it sits."""
    decision = antipattern_decision(
        "value: Any = 1\n",
        "value: Any = 1  # lup: ignore[any-type]\n",
        suppression_rows(),
        python_source=True,
    )

    assert decision is not None
    assert decision.effect == "ask"


def test_rule_ids_are_unique_kebab_case() -> None:
    """Every rule id is a distinct kebab-case token a typed ignore can target."""
    for table in (PYTHON_ANTI_PATTERNS, TS_ANTI_PATTERNS):
        ids = [ap.id for ap in table]
        assert len(ids) == len(set(ids))
        for rule_id in ids:
            assert re.fullmatch(r"[a-z][a-z0-9-]*", rule_id), rule_id


def test_audit_flags_unguarded_match() -> None:
    findings = audit_text("x: Any = 1\n", PYTHON_ANTI_PATTERNS)
    assert [f.kind for f in findings] == ["missing"]
    assert findings[0].line == 1


def test_audit_flags_bare_ignore_as_untyped() -> None:
    # A bare `# lup: ignore` still silences the rule (stays valid) but is
    # surfaced as "untyped" so the migration to typed directives is gradual;
    # the message names the rule it should narrow to.
    findings = audit_text("x: Any = 1  # lup: ignore\n", PYTHON_ANTI_PATTERNS)
    assert [f.kind for f in findings] == ["untyped"]
    assert "any-type" in findings[0].message


def test_audit_typed_ignore_silences_exactly_its_rule() -> None:
    findings = audit_text("x: Any = 1  # lup: ignore[any-type]\n", PYTHON_ANTI_PATTERNS)
    assert findings == []


def test_audit_typed_ignore_naming_wrong_rule_still_flags() -> None:
    # Names tuple-shape, but the line trips any-type: the real hit is missing
    # and the tuple-shape directive guards nothing (spurious).
    findings = audit_text(
        "x: Any = 1  # lup: ignore[tuple-shape]\n", PYTHON_ANTI_PATTERNS
    )
    kinds = {f.kind for f in findings}
    assert kinds == {"missing", "spurious"}


def test_audit_flags_spurious_bare_marker() -> None:
    findings = audit_text("x: int = 1  # lup: ignore\n", PYTHON_ANTI_PATTERNS)
    assert [f.kind for f in findings] == ["spurious"]


def test_audit_flags_typed_ignore_guarding_nothing() -> None:
    findings = audit_text(
        "x: int = 1  # lup: ignore[tuple-shape]\n", PYTHON_ANTI_PATTERNS
    )
    assert [f.kind for f in findings] == ["spurious"]
    assert "tuple-shape" in findings[0].message


def test_audit_leaves_foreign_scanner_ids_alone() -> None:
    # seam-boundary belongs to the boundary scan; the auditor owns no rule
    # by that id and must not call its typed ignore spurious.
    foreign = "import x.claude  # lup: ignore[seam-boundary]\n"
    assert audit_text(foreign, PYTHON_ANTI_PATTERNS) == []

    unowned = "import x.claude  # lup: ignore[no-such-rule]\n"
    assert [f.kind for f in audit_text(unowned, PYTHON_ANTI_PATTERNS)] == ["spurious"]


def test_audit_reports_bare_file_level_ignore_as_advisory() -> None:
    # The whole-file opt-out still disables every rule, but the audit
    # surfaces it as one untyped (advisory) finding instead of silence.
    findings = audit_text("# lup: ignore\nx: Any = 1\n", PYTHON_ANTI_PATTERNS)
    assert [f.kind for f in findings] == ["untyped"]
    assert findings[0].line == 1
    assert "whole file" in findings[0].message


def test_audit_reports_dead_file_level_rule_as_spurious() -> None:
    # dict-get is named file-wide but nothing in the file calls .get —
    # the dead id reports spurious at the directive line.
    source = "# lup: ignore[dict-get, any-type]\nx: Any = 1\n"
    findings = audit_text(source, PYTHON_ANTI_PATTERNS)
    assert [(f.kind, f.rule_id, f.line) for f in findings] == [
        ("spurious", "dict-get", 1)
    ]


def test_audit_reports_inline_covered_file_level_rule_as_spurious() -> None:
    # Every .get hit carries its own inline directive, so the file-wide
    # dict-get opt-out silences nothing an inline marker does not — dead.
    source = "# lup: ignore[dict-get]\nname = data.get(k)  # lup: ignore[dict-get]\n"
    findings = audit_text(source, PYTHON_ANTI_PATTERNS)
    assert [(f.kind, f.rule_id, f.line) for f in findings] == [
        ("spurious", "dict-get", 1)
    ]


def test_audit_keeps_foreign_ids_out_of_file_level_verdicts() -> None:
    # seam-boundary belongs to the boundary scan; a file-wide opt-out naming
    # it is that scanner's business, never reported dead here.
    source = "# lup: ignore[seam-boundary]\nx = 1\n"
    assert audit_text(source, PYTHON_ANTI_PATTERNS) == []


def test_audit_skips_plain_comment_lines() -> None:
    findings = audit_text("# a comment mentioning Any in prose\n", PYTHON_ANTI_PATTERNS)
    assert findings == []


@pytest.mark.parametrize(
    "declaration",
    [
        "    model_config = ConfigDict(frozen=True)",
        '    model_config = SettingsConfigDict(extra="ignore")',
        "    model_config = FROZEN",
        '    model_config = {"arbitrary_types_allowed": True}',
        "    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)",
    ],
)
def test_model_config_rule_fires_on_every_declaration_shape(declaration: str) -> None:
    """Each shape a repository census turned up is a declaration the rule sees.

    The shapes differ only in what sits right of the `=`, so a rule anchored on
    the assignment catches a constructor, a shared alias, a bare dict and an
    annotated form alike — including one nobody enumerated in advance.
    """
    findings = audit_text(f"{declaration}\n", PYTHON_ANTI_PATTERNS)
    assert [f.rule_id for f in findings] == ["model-config"]


def test_model_config_rule_names_the_class_keyword_replacement() -> None:
    """The message says what to write instead, not only what is wrong."""
    message = rule_named(PYTHON_ANTI_PATTERNS, "model-config").message
    assert "class keywords" in message
    assert "BaseModel, frozen=True" in message


def test_model_config_rule_ignores_the_identifier_in_prose() -> None:
    """A `model_config` named in a comment or a string is not a declaration."""
    prose = '# model_config = ConfigDict(frozen=True) in a comment\nx = "model_config = 1"\n'
    assert audit_text(prose, PYTHON_ANTI_PATTERNS) == []


def test_comment_context_covers_exactly_the_directive_rules() -> None:
    """Only comment-directive rules scan comments; every other rule sees code."""
    python_comment = {ap.id for ap in PYTHON_ANTI_PATTERNS if ap.context == "comment"}
    ts_comment = {ap.id for ap in TS_ANTI_PATTERNS if ap.context == "comment"}
    assert python_comment == {"type-ignore", "pyright-ignore", "noqa"}
    assert ts_comment == {
        "ts-ignore",
        "ts-expect-error",
        "ts-nocheck",
        "eslint-disable",
        "eslint-disable-block",
        "tslint-disable",
    }


def test_audit_ignores_identifiers_quoted_in_trailing_comments() -> None:
    # Prose in a trailing comment is comment text, not code: the token-masked
    # code scan no longer false-positives on it as the raw line scan did.
    clean = (
        "x = compute()  # may return Any when unset\n"
        "entry = lookup(key)  # like registry.get(key)\n"
        "value = parse(raw)  # a tuple[int, str] semantically\n"
    )
    assert audit_text(clean, PYTHON_ANTI_PATTERNS) == []


def test_audit_ignores_type_comment_prose() -> None:
    # A legacy `# type: List[Any]` comment carries no `ignore` directive and
    # is masked for code rules, so neither any-type nor typing-generics trips.
    prose = "# type: List[Any] was this field's old shape\n"
    assert audit_text(prose, PYTHON_ANTI_PATTERNS) == []


def test_audit_catches_directive_comments_wherever_they_sit() -> None:
    # Comment-context rules see comments intact: a standalone suppression
    # comment line (pyright's ignore, flake8's noqa) is a directive to flag,
    # not skippable prose. The noqa fixture is split so ruff's own
    # line-oriented directive scan does not read this source as suppressed.
    for line, rule_id in (
        ("# pyright: ignore\n", "pyright-ignore"),
        ("# " + "noqa\n", "noqa"),
        ("x = 1  # type: ignore\n", "type-ignore"),
    ):
        findings = audit_text(line, PYTHON_ANTI_PATTERNS)
        assert [(f.kind, f.rule_id) for f in findings] == [("missing", rule_id)], line


# ── empty-collection AST refiner ──────────────────────────────────────────

INIT_STATE = """\
class Scheduler:
    def __init__(self) -> None:
        self.reminders = []
        self.actions: list[str] = []
"""

CLASS_FIELD = """\
class Settings:
    table: dict[str, list[str]] = {}
"""

CALL_KWARG = "result = Report(files=[], errors=0)\n"

MODULE_DECLARATION = "DENIED_SCOPES: list[str] = []\n"

LOCAL_SEED = """\
def build() -> list[int]:
    items = []
    for i in range(3):
        items.append(i)
    return items
"""

MODULE_SEED = "REGISTRY = {}\n"

EXCEPT_FALLBACK = """\
try:
    entries = read_dir(path)
except OSError:
    entries = []
"""

EXCEPT_NESTED_SEED = """\
try:
    entries = read_dir(path)
except OSError:
    for path in retry_paths:
        entries = []
"""

TOLERANT_FOLD = """\
def load(paths):
    records: list[int] = []
    for path in paths:
        try:
            records.append(parse(path))
        except ValueError:
            log(path)
    return records
"""

MIXED_FEEDING = """\
def load(paths, extras):
    records = []
    for path in paths:
        try:
            records.append(parse(path))
        except ValueError:
            log(path)
    for extra in extras:
        records.append(extra)
    return records
"""

CONDITIONAL_BUILD = """\
def build_args(model, prompt):
    args: list[str] = []
    if model:
        args.extend(["--model", model])
    if prompt:
        args.append(prompt)
    return args
"""

HELPER_FILLED = """\
def collect():
    rows = []
    fill(rows)
    return rows
"""

CLOSURE_ACCUMULATOR = """\
def collector():
    captured = []

    def capture(value):
        captured.append(value)

    return captured, capture
"""

LOOP_RESET = """\
def blocks(lines):
    out = []
    body = []
    for line in lines:
        if not line:
            out.append(body)
            body = []
        else:
            body.append(line)
    return out
"""


ROUTE_DECORATOR = """\
@app.get("/api/runs")
async def read_runs() -> None:
    payload.get("key")


@app.websocket(
    "/api/stream",
)
def stream() -> None:
    pass
"""


def test_refiner_exempts_route_decorators() -> None:
    """`.get(` on a decorator names a route; the call below it is real."""
    assert dict_get_exempt_lines(ROUTE_DECORATOR) == {1, 6, 7, 8}


def test_refiner_survives_a_fragment_it_cannot_parse() -> None:
    assert dict_get_exempt_lines("@app.get(\n") == set()


def test_declared_refiners_are_the_kernel_refiners() -> None:
    """A rule's refiner is the same object the hook applies from its row.

    The rule holds the function and its row carries the name, because a row
    projected into the hermetic runtime cannot carry a callable. Nothing but
    this keeps them the same: a rule refined on one side only is how a marker
    becomes one the audit demands gone and the hook refuses to remove.
    """
    for rule in PYTHON_ANTI_PATTERNS:
        expected = None if rule.refiner is None else rule.refiner.exempt
        assert refiner_named(antipattern_row(rule)["refiner"]) is expected, rule.id


def test_refiner_exempts_deliberate_defaults() -> None:
    assert empty_collection_exempt_lines(INIT_STATE) == {3, 4}
    assert empty_collection_exempt_lines(CLASS_FIELD) == {2}
    assert empty_collection_exempt_lines(CALL_KWARG) == {1}
    # An annotated declaration states a shape; only the bare MODULE_SEED
    # assignment below reads as a fold waiting for its appends.
    assert empty_collection_exempt_lines(MODULE_DECLARATION) == {1}


def test_refiner_exempts_except_body_fallback() -> None:
    # Degrade-to-empty in a handler is a fallback value, not a fold seed.
    assert empty_collection_exempt_lines(EXCEPT_FALLBACK) == {4}
    # Only DIRECT handler statements: a seed nested in a loop still trips.
    assert empty_collection_exempt_lines(EXCEPT_NESTED_SEED) == set()


def test_refiner_exempts_tolerant_folds() -> None:
    # Per-item try/except is exactly what a comprehension cannot express.
    assert empty_collection_exempt_lines(TOLERANT_FOLD) == {2}
    # One tolerant and one plain feeding loop: the plain fold keeps tripping.
    assert empty_collection_exempt_lines(MIXED_FEEDING) == set()


def test_refiner_exempts_loop_free_seeds() -> None:
    # No loop feeds these, so there is no comprehension to prefer.
    assert empty_collection_exempt_lines(CONDITIONAL_BUILD) == {2}
    assert empty_collection_exempt_lines(CLOSURE_ACCUMULATOR) == {2}
    # Deliberate: mutation through a callee is invisible to the refiner.
    assert empty_collection_exempt_lines(HELPER_FILLED) == {2}


def test_refiner_exempts_in_loop_resets() -> None:
    # The reset inside the loop is machinery, not a seed; both function-level
    # seeds feed an unguarded loop and still trip.
    assert empty_collection_exempt_lines(LOOP_RESET) == {7}


def test_refiner_keeps_flagging_seeds() -> None:
    assert empty_collection_exempt_lines(LOCAL_SEED) == set()
    assert empty_collection_exempt_lines(MODULE_SEED) == set()


def test_refiner_unparseable_source_exempts_nothing() -> None:
    assert empty_collection_exempt_lines("def broken(:\n") == set()


def test_audit_exempt_line_needs_no_marker() -> None:
    assert audit_text(INIT_STATE, PYTHON_ANTI_PATTERNS) == []


def test_audit_marker_on_exempt_line_is_spurious() -> None:
    marked = INIT_STATE.replace(
        "self.reminders = []",
        "self.reminders = []  # lup: ignore[empty-collection]",
    )
    findings = audit_text(marked, PYTHON_ANTI_PATTERNS)
    assert [f.kind for f in findings] == ["spurious"]


def test_audit_local_seed_still_flags() -> None:
    findings = audit_text(LOCAL_SEED, PYTHON_ANTI_PATTERNS)
    assert [f.kind for f in findings] == ["missing"]
    assert findings[0].rule_id == "empty-collection"


def test_audit_skips_docstring_prose() -> None:
    # Prose is not code, and no inline directive could ever guard a docstring
    # line — a comment cannot open inside a string. Code outside stays audited.
    text = '"""Unlike ``Any``, a set(x) here is\njust prose."""\nx: Any = 1\n'
    findings = audit_text(text, PYTHON_ANTI_PATTERNS)
    assert [(f.kind, f.line, f.rule_id) for f in findings] == [
        ("missing", 3, "any-type")
    ]


def test_audit_skips_attribute_docstrings() -> None:
    # A bare string statement after a field or alias is the attribute-docstring
    # convention — documentation by construction. Assigned string contents are
    # data too, so examples inside them are not scanned as executable code.
    prose = 'x: int = 1\n"""Unlike ``Any`` this field is honest."""\n'
    assert audit_text(prose, PYTHON_ANTI_PATTERNS) == []
    data = 'x = "fixture: Any = cast(str, 1)"\n'
    assert audit_text(data, PYTHON_ANTI_PATTERNS) == []


def test_audit_skips_fstring_prose_but_not_its_interpolations() -> None:
    # An f-string lexes as start/middle/end tokens since 3.12; masking only
    # the STRING type left its prose scanned as code, so converting a prompt
    # r-string to an rf-string exposed its English to the rule set. Middle
    # fragments are data; the interpolations between them are code and stay
    # audited.
    prose = 'text = rf"the words re.split(x) here are prose {name}"\n'
    assert audit_text(prose, PYTHON_ANTI_PATTERNS) == []
    code = "text = f\"total {name.replace('-', '_')}\"\n"
    assert [f.rule_id for f in audit_text(code, PYTHON_ANTI_PATTERNS)] == [
        "string-replace"
    ]


def test_atomic_renames_are_exempt_from_replace_rule() -> None:
    # Path-receiver `.replace` is an atomic rename, not string surgery, so the
    # string-replace rule leaves it alone. (os.replace is redirected to
    # os-file-ops — see test_audit_flags_os_file_ops_and_environ.)
    source = "tmp_path.replace(target)\nPath.replace(a, b)\n"
    assert audit_text(source, PYTHON_ANTI_PATTERNS) == []


def test_string_replace_still_flagged() -> None:
    findings = audit_text('name.replace("-", "_")\n', PYTHON_ANTI_PATTERNS)
    assert [f.kind for f in findings] == ["missing"]


def test_bare_split_is_exempt_from_split_rule() -> None:
    assert audit_text("fields = raw.split()\n", PYTHON_ANTI_PATTERNS) == []


def test_split_on_separator_still_flagged() -> None:
    findings = audit_text('parts = raw.split(",")\n', PYTHON_ANTI_PATTERNS)
    assert [f.kind for f in findings] == ["missing"]


def test_docstring_mention_of_ignore_is_not_a_guard() -> None:
    source = '"""Audit `# lup: ignore` markers."""\nx = 1\n'
    assert audit_text(source, PYTHON_ANTI_PATTERNS) == []


def test_ignore_inside_string_literal_is_not_a_guard() -> None:
    source = 'fixture = "x: Any = 1  # lup: ignore"\n'
    findings = audit_text(source, PYTHON_ANTI_PATTERNS)
    assert findings == []


def test_note_quoting_ignore_is_not_a_guard() -> None:
    source = "x = 1  # lup: should we remove every # lup: ignore?\n"
    assert audit_text(source, PYTHON_ANTI_PATTERNS) == []


def test_audit_exempts_argless_strip() -> None:
    # Whitespace framing has no parser alternative — argless .strip() passes,
    # exactly like argless .split().
    assert audit_text("name = raw.strip()\n", PYTHON_ANTI_PATTERNS) == []


def test_audit_flags_separator_strip_like_split() -> None:
    findings = audit_text("name = raw.strip('/')\n", PYTHON_ANTI_PATTERNS)
    assert [f.kind for f in findings] == ["missing"]
    assert ".strip(chars)" in findings[0].message


def test_audit_flags_string_keyed_dict_annotation() -> None:
    findings = audit_text("env: dict[str, str]\n", PYTHON_ANTI_PATTERNS)
    assert [f.kind for f in findings] == ["missing"]
    assert findings[0].rule_id == "dict-str-payload"


def test_audit_accepts_non_string_keyed_dict() -> None:
    assert audit_text("counts: dict[int, str]\n", PYTHON_ANTI_PATTERNS) == []


def test_audit_flags_bare_object_annotations() -> None:
    for line in (
        "def probe(value: object) -> None: ...\n",
        "def load() -> object: ...\n",
    ):
        findings = audit_text(line, PYTHON_ANTI_PATTERNS)
        assert [f.kind for f in findings] == ["missing"], line
        assert "object" in findings[0].message


def test_audit_accepts_underscore_object_params() -> None:
    line = "def handler(_context: object) -> None: ...\n"
    assert audit_text(line, PYTHON_ANTI_PATTERNS) == []


def test_audit_flags_bare_basemodel_annotations() -> None:
    for line in (
        "def show(result: BaseModel, as_json: bool) -> None: ...\n",
        "def load() -> BaseModel: ...\n",
    ):
        findings = audit_text(line, PYTHON_ANTI_PATTERNS)
        assert [f.kind for f in findings] == ["missing"], line
        assert "concrete union" in findings[0].message


def test_audit_accepts_basemodel_bounds_and_unions() -> None:
    clean = (
        "def read[T: BaseModel](model: type[T]) -> T | None: ...\n"
        "class Tool[I: BaseModel, O: BaseModel]: ...\n"
        "def dump(data: BaseModel | Sequence[int]) -> None: ...\n"
    )
    assert audit_text(clean, PYTHON_ANTI_PATTERNS) == []


def test_audit_flags_typing_and_stdlib_modernization() -> None:
    for line, needle in (
        ("x: Optional[int] = None\n", "PEP 604"),
        ("pairs: Union[int, str] = 1\n", "PEP 604"),
        ("items: List[str]\n", "lowercase builtin"),
        ("base = os.path.dirname(p)\n", "pathlib.Path"),
        ("value = eval(expr)\n", "eval()"),
        ("os.system('ls')\n", "`sh` library"),
        ("now = datetime.utcnow()\n", "naive"),
        ("def bump() -> None:\n    global counter\n", "`global`"),
    ):
        findings = audit_text(line, PYTHON_ANTI_PATTERNS)
        assert [f.kind for f in findings] == ["missing"], line
        assert needle in findings[0].message, line


def test_audit_accepts_prefixed_eval_and_method_calls() -> None:
    clean = "value = ast.literal_eval(expr)\nglobal_config = 1\n"
    assert audit_text(clean, PYTHON_ANTI_PATTERNS) == []


def test_audit_flags_ts_additions() -> None:
    for line in (
        "const name = user!.name;\n",
        "var total = 0;\n",
        "let cb: Function;\n",
        "console.log('debug');\n",
    ):
        findings = audit_text(line, TS_ANTI_PATTERNS)
        assert [f.kind for f in findings] == ["missing"], line


def test_audit_accepts_strict_equality_and_typed_ts() -> None:
    clean = "const ok = a !== b;\nconst v: string = name;\n"
    assert audit_text(clean, TS_ANTI_PATTERNS) == []


def test_audit_flags_string_keyed_mapping_with_scalar_value() -> None:
    findings = audit_text("data: Mapping[str, int]\n", PYTHON_ANTI_PATTERNS)
    assert [f.kind for f in findings] == ["missing"]
    assert findings[0].rule_id == "dict-str-payload"


def test_audit_accepts_string_keyed_dict_with_concrete_value() -> None:
    # The relaxed rule permits concrete class/callable value types: these are
    # registries/routers whose open, data-driven key set is the point.
    clean = (
        "engines: dict[str, Engine]\n"
        "tools: dict[str, LupMcpTool]\n"
        "factories: Mapping[str, Callable[[], Engine]]\n"
    )
    assert audit_text(clean, PYTHON_ANTI_PATTERNS) == []


def test_audit_accepts_jsonvalue_dicts() -> None:
    clean = "payload: dict[str, JsonValue]\nargs: Mapping[str, JsonValue]\n"
    assert audit_text(clean, PYTHON_ANTI_PATTERNS) == []


def test_audit_flags_tuple_variable_and_attribute_annotations() -> None:
    # The rule catches every declared fixed-arity shape, not just return types.
    for line in (
        "pair: tuple[int, str] = (1, 'a')\n",
        "class Holder:\n    pair: tuple[int, str] = (1, 'a')\n",
        "def f() -> tuple[int, str]: ...\n",
    ):
        findings = audit_text(line, PYTHON_ANTI_PATTERNS)
        assert [f.kind for f in findings] == ["missing"], line
        assert findings[0].rule_id == "tuple-shape"


def test_audit_leaves_variadic_tuples_alone() -> None:
    """`tuple[X, ...]` is an immutable sequence, not a shape wanting field names.

    Nesting is why the tree decides this and not a wider regex: the trailing
    ellipsis in `tuple[dict[str, int], ...]` sits behind a bracket no character
    class can step over.
    """
    for line in (
        "names: tuple[str, ...] = ()\n",
        "def f() -> tuple[int, ...]: ...\n",
        "rows: tuple[dict[str, JsonValue], ...] = ()\n",
    ):
        assert audit_text(line, PYTHON_ANTI_PATTERNS) == [], line


def test_a_line_mixing_both_tuple_shapes_keeps_its_finding() -> None:
    """A variadic neighbour does not clear a fixed-arity shape on the same line."""
    mixed = "def f(rows: tuple[str, ...]) -> tuple[int, str]: ...\n"
    findings = audit_text(mixed, PYTHON_ANTI_PATTERNS)
    assert [f.rule_id for f in findings] == ["tuple-shape"]


def test_source_that_does_not_parse_reports_no_tuple_shape() -> None:
    """A strong rule cannot deny on a verdict it has no tree to justify.

    There is no directive that would rescue the site, so "cannot tell" has to
    fail toward silence; the audit sees it again once the file parses.
    """
    assert audit_text("def f(  # unclosed\n", PYTHON_ANTI_PATTERNS) == []


def test_audit_flags_declared_frozenset() -> None:
    for line in (
        "FRAMEWORK_TOOLS: frozenset[str] = frozenset({'x'})\n",
        "TOKENS = frozenset({'a', 'b'})\n",
    ):
        findings = audit_text(line, PYTHON_ANTI_PATTERNS)
        assert findings and findings[0].rule_id == "frozenset-shape"


def test_audit_flags_bare_set_shape() -> None:
    # A declared/constructed set is flagged like frozenset; `frozenset` itself
    # trips only frozenset-shape, since its "set" is not a standalone word.
    for line in (
        "names: set[str]\n",
        "seen = set(values)\n",
        "def f(items: set) -> set:\n    return items\n",
    ):
        rule_ids = {f.rule_id for f in audit_text(line, PYTHON_ANTI_PATTERNS)}
        assert "set-shape" in rule_ids, line
    frozen = audit_text("TOKENS = frozenset({'a'})\n", PYTHON_ANTI_PATTERNS)
    assert [f.rule_id for f in frozen] == ["frozenset-shape"]


def test_audit_accepts_set_methods_and_prose() -> None:
    # `.set()` is a method call, not a declared set shape, and the word "set"
    # in a message string carries neither the bracket nor the annotation form.
    clean = 'self.wake_event.set()\nmsg = "when set, the debounce window opens"\n'
    assert audit_text(clean, PYTHON_ANTI_PATTERNS) == []


def test_audit_flags_empty_collection_literals() -> None:
    for line in ("cache = {}\n", "items = []\n", "buffer = set()\n"):
        rule_ids = {f.rule_id for f in audit_text(line, PYTHON_ANTI_PATTERNS)}
        assert "empty-collection" in rule_ids, line


def test_audit_accepts_populated_and_compared_collections() -> None:
    # Non-empty literals are fine, and a `==` comparison is not an assignment,
    # so the empty-collection rule leaves it alone.
    clean = "cache = {'k': 1}\nitems = [1, 2]\nif payload == {}:\n    pass\n"
    assert audit_text(clean, PYTHON_ANTI_PATTERNS) == []


def test_audit_flags_os_exec_as_shell() -> None:
    for line in ("os.execv(path, args)\n", "os.execvp('ls', argv)\n"):
        findings = audit_text(line, PYTHON_ANTI_PATTERNS)
        assert findings and findings[0].rule_id == "os-shell", line


def test_audit_flags_both_re_import_forms() -> None:
    for line in ("import re\n", "from re import compile\n"):
        findings = audit_text(line, PYTHON_ANTI_PATTERNS)
        assert findings and findings[0].rule_id == "import-re", line


def test_audit_flags_os_file_ops_and_environ() -> None:
    for line in (
        "entries = os.listdir(path)\n",
        "os.makedirs(path)\n",
        "os.replace(tmp, dst)\n",
    ):
        findings = audit_text(line, PYTHON_ANTI_PATTERNS)
        assert findings and findings[0].rule_id == "os-file-ops", line
    for line in (
        "value = os.environ['KEY']\n",
        "home = os.getenv('HOME')\n",
    ):
        findings = audit_text(line, PYTHON_ANTI_PATTERNS)
        assert findings and findings[0].rule_id == "os-environ", line


def test_audit_flags_every_dict_get() -> None:
    findings = audit_text("name = payload.get('name')\n", PYTHON_ANTI_PATTERNS)
    assert [f.kind for f in findings] == ["missing"]
    assert findings[0].rule_id == "dict-get"


def test_audit_dict_get_silenced_by_typed_ignore() -> None:
    source = "name = registry.get(key)  # lup: ignore[dict-get]\n"
    assert audit_text(source, PYTHON_ANTI_PATTERNS) == []


def test_a_strong_rule_refuses_every_suppression() -> None:
    """A reasoned exception is a soft rule's mechanism, not a strong one's.

    Soft rules name a shape that is usually wrong and occasionally the only
    thing that works, so a directive there is graded rather than obeyed. A
    strong rule's replacement is right every time, which leaves a directive
    nothing to express but a decision to keep the defect.
    """
    strong = AntiPattern(
        id="probe-strong",
        pattern=re.compile(r"\bforbidden\b"),
        message="use the replacement",
        strength="strong",
    )

    for source in (
        "value = forbidden()\n",
        "value = forbidden()  # lup: ignore[probe-strong]\n",
        "value = forbidden()  # lup: ignore\n",
        "# lup: ignore[probe-strong]\nvalue = forbidden()\n",
    ):
        findings = audit_text(source, [strong])
        missing = [
            f for f in findings if f.rule_id == "probe-strong" and f.kind == "missing"
        ]

        assert len(missing) == 1, source
        assert "write the replacement" in missing[0].message
        # A directive written anyway is additionally reported as spurious,
        # which is what it is: the hit beside it was never silenced.
        assert all(f.kind in {"missing", "spurious"} for f in findings), source


def test_a_soft_rule_still_honours_its_suppression() -> None:
    soft = AntiPattern(
        id="probe-soft",
        pattern=re.compile(r"\bforbidden\b"),
        message="prefer something else",
    )

    assert audit_text("value = forbidden()  # lup: ignore[probe-soft]\n", [soft]) == []
    assert [f.kind for f in audit_text("value = forbidden()\n", [soft])] == ["missing"]


def test_audit_file_level_typed_ignore_disables_only_that_rule() -> None:
    # `# lup: ignore[dict-get]` at the top silences dict-get file-wide, but
    # every other rule stays live.
    source = "# lup: ignore[dict-get]\nname = data.get(k)\nx: Any = 1\n"
    findings = audit_text(source, PYTHON_ANTI_PATTERNS)
    assert [f.kind for f in findings] == ["missing"]
    assert findings[0].rule_id == "any-type"


DEFAULT_FACTORY_FIELD = (
    "from pydantic import BaseModel, Field\n"
    "\n"
    "\n"
    "class Record(BaseModel):\n"
    "    items: list[int] = Field(default_factory=list)\n"
)

LITERAL_DEFAULT_FIELD = (
    "from pydantic import BaseModel\n"
    "\n"
    "\n"
    "class Record(BaseModel):\n"
    "    items: list[int] = []\n"
)

WORKING_FACTORY_FIELD = (
    "from pydantic import BaseModel, Field\n"
    "\n"
    "\n"
    "class Record(BaseModel):\n"
    "    stamp: Moment = Field(default_factory=Moment)\n"
)


def test_default_factory_flags_the_empty_collection_form() -> None:
    findings = audit_text(DEFAULT_FACTORY_FIELD, PYTHON_ANTI_PATTERNS)
    assert [(f.kind, f.line, f.rule_id) for f in findings] == [
        ("missing", 5, "default-factory")
    ]


def test_default_factory_clears_a_factory_that_does_work() -> None:
    """The near miss: a factory no annotated literal could have said."""
    assert audit_text(WORKING_FACTORY_FIELD, PYTHON_ANTI_PATTERNS) == []
    assert default_factory_exempt_lines(WORKING_FACTORY_FIELD) == {5}
    assert default_factory_exempt_lines(DEFAULT_FACTORY_FIELD) == set()


def test_default_factory_and_empty_collection_never_share_a_line() -> None:
    """The two divide pydantic's ground; neither doubles up on the other's.

    The rule prescribes the literal default, and that literal sits on an
    annotated class declaration — precisely what the other rule's refiner
    clears. Were it otherwise, the replacement one gate demands would be the
    line the other refuses.
    """
    for source in (DEFAULT_FACTORY_FIELD, LITERAL_DEFAULT_FIELD):
        rules = {
            finding.rule_id for finding in audit_text(source, PYTHON_ANTI_PATTERNS)
        }
        assert rules <= {"default-factory"}, source


def test_the_prescribed_literal_default_is_an_edit_the_hook_admits() -> None:
    """Writing the replacement must pass the gate that judged the original.

    A rule whose remedy the edit hook denies is unfollowable: the audit asks
    for a change the point of writing refuses, and no revision converges.
    """
    decision = antipattern_decision(
        DEFAULT_FACTORY_FIELD,
        LITERAL_DEFAULT_FIELD,
        bundled_antipattern_rows()[".py"],
        python_source=True,
    )

    assert decision is None or decision.effect == "allow", decision


def test_the_set_shapes_name_the_structure_they_collapse() -> None:
    """Both messages have to say what is lost, not that the type is too much.

    A reader told a `set` is overkill reaches for a `list`, which loses the
    same field the set did. The reason is the `dict[...]` the members were
    keying, so the message that carries it is the one that gets the rewrite
    right.
    """
    messages = {
        rule.id: rule.message
        for rule in PYTHON_ANTI_PATTERNS
        if rule.id in ("set-shape", "frozenset-shape")
    }

    assert set(messages) == {"set-shape", "frozenset-shape"}  # lup: ignore[set-shape]
    for rule_id, message in messages.items():
        assert "dict[" in message, rule_id
        assert "overkill" not in message, rule_id


def test_pdf_extraction_flags_every_text_extractor() -> None:
    for line in (
        "import fitz\n",
        "import pymupdf\n",
        "from pypdf import PdfReader\n",
        "import PyPDF2\n",
        "import pdfplumber\n",
        "from pdfminer.high_level import extract_text\n",
    ):
        rule_ids = {f.rule_id for f in audit_text(line, PYTHON_ANTI_PATTERNS)}
        assert "pdf-extraction" in rule_ids, line


def test_pdf_extraction_leaves_the_neighbouring_pdf_names_alone() -> None:
    """The near miss: naming a PDF is not extracting text from one.

    The rule is about the libraries that pull text out of a document, so a
    module of our own with `pdf` in its name and a path that ends in one are
    both untouched — flagging those would teach that PDFs are the problem
    rather than the silent empty extraction.
    """
    clean = "import pdf_report\nfrom lup.pdfs import stamp\nname = 'fitz.pdf'\n"

    assert audit_text(clean, PYTHON_ANTI_PATTERNS) == []


def test_pdf_extraction_names_no_tool_until_a_runtime_spells_one() -> None:
    """The rule ships into every plugin tree, so the reader is asked for.

    Naming one runtime's tool in the portable message would tell the other to
    use something it does not have; a runtime that declines contributes no
    sentence, and the failure mode still reads on its own.
    """
    neutral = rule_named(PYTHON_ANTI_PATTERNS, "pdf-extraction")
    spelled = rule_named(
        python_anti_patterns(Spelled(words="Hand the path to the Read tool.")),
        "pdf-extraction",
    )
    declined = rule_named(
        python_anti_patterns(Unsupported(reason="no tool takes a document")),
        "pdf-extraction",
    )

    assert "Read" not in neutral.message
    assert neutral.message == declined.message
    assert spelled.message.endswith("Hand the path to the Read tool.")
    assert spelled.message.startswith(declined.message)
