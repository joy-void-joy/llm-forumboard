# lup: ignore[empty-collection, import-re, re-call, set-shape]
"""Single importable source of truth for the codebase's anti-pattern set.

The generated hermetic edit policy denies an edit whose added lines match one
of these patterns unless the line carries a `# lup: ignore`. Harness generation
projects these rows into its dependency-free runtime; a test asserts that
projection stays identical, and the `lup-devtools dev check --antipatterns`
auditor consumes them to scan the whole tree after the fact (catching lines that slipped in past the hook and
`# lup: ignore` markers that no longer guard anything).

Each rule carries a stable kebab-case ``id``. A directive names rules
pyright-style — `# lup: ignore[dict-get]` silences only ``dict-get`` on that
line, `# lup: ignore[a, b]` a list — so one open site opts out of one rule
without blinding the others. The bare `# lup: ignore` stays valid (it silences
every rule) but the auditor surfaces it as "untyped" so the migration to typed
directives is gradual.

The set is a syntax-aware linter pass, not a raw grep: every rule declares the
syntactic ``context`` it inspects, and Python source is tokenized once (via
`lup.codescan.common.LineProjections`) so "code" rules scan lines with string
literals and comments blanked while "comment" directive rules see comments
intact. The detectors themselves stay regexes because that is the primitive
form the hermetic hook runtime can carry (ruff has no plugin API, and engines
that do — flake8, pylint, semgrep — could not run inside the hook).

Refiners sharpen individual rules on top of that floor, and what a refiner
needs decides who can run it. `refined_exempt_lines` reads the AST alone, so
the hook applies it too and both gates judge a line the same way — a broad
regex the kernel keeps flagging while the audit calls its marker spurious is
a change one gate demands and the other refuses. `lup.codescan.grammar` goes
further and resolves what a matched receiver is declared on, so `dict-get` can
distinguish a mapping from an HTTP client; that needs a type oracle and stays
audit-side. Both return `Refutation` rows this module drops — and reports the
surviving directives for. Regex alone remains where a rule is text-shaped. The
`lup.codescan.registry` index and the generated `docs/rules.md` reference list
this family beside the boundary, spelling, and architecture rules.

Most messages read the same to everyone, so most of the table is a literal. A
rule that has to name a native tool asks the runtime for the words instead:
`pdf-extraction` reaches `NativeSpellings.read_document`, and
:func:`antipattern_set_for` compiles the table once per plugin so a runtime
with no such tool ships the rule without one named.

Each entry pairs a stable id and a compiled regex with the message the hook and
auditor show. Beyond `pydantic` and the harness seam that spells those words,
this module imports only the standard library (directly and through
`lup.codescan.common`) so the auditor can load it cheaply;
`# lup:` marker detection stays in `lup.codescan.markers`, and the shared scan
core — ignore matching, comment-column tokenization, the masked line
projections, the line cursor — in `lup.codescan.common`, which this set's
consumers and the auditor import directly.
"""

import re
from collections.abc import Callable

from pydantic import BaseModel, Field

from lup.codescan.behaviour import RULE_ID as MODEL_FREE_FUNCTION_RULE_ID
from lup.codescan.boundaries import (
    CONSTANT_DECLARATION_RULE_ID,
    LIBRARY_DEFAULT_RULE_ID,
    NATIVE_SPELLING_RULE_ID,
    RULE_ID as SEAM_BOUNDARY_RULE_ID,
)
from lup.codescan.capabilities import RULE_ID as ABC_CAPABILITY_RULE_ID
from lup.codescan.dispatch import RULE_ID as OWN_MODEL_DISPATCH_RULE_ID
from lup.codescan.common import (
    LineProjections,
    PythonContext,
    Refutation,
    RuleContext,
    RuleSelection,
    RuleStrength,
    file_level_ignore,
    ignore_rule_ids,
)
from lup.harness.contracts import Spelling, Unsupported
from lup.policy.kernel.edit import (
    IGNORE_RE,
    default_factory_exempt_lines,
    dict_get_exempt_lines,
    empty_collection_exempt_lines,
    suppression_placement,
    suppression_reaches,
    tuple_shape_exempt_lines,
)


class Refiner(BaseModel, arbitrary_types_allowed=True):
    """An AST context that narrows one rule, and what a cleared match really is.

    ``exempt`` returns the lines the context clears, and ``evidence`` says why
    in the words a refuted finding carries. The kernel owns these functions
    because the hook must apply them with no types and no dependencies to
    hand; the rule holds the same object so the narrowing is visible where
    the rule is declared.
    """

    exempt: Callable[[str], set[int]]
    evidence: str


class AntiPattern(BaseModel, arbitrary_types_allowed=True):
    """One forbidden code shape: a stable id, the regex that detects it, and why.

    ``id`` is a stable kebab-case name a typed `# lup: ignore[id]` directive
    targets, so a single site can silence exactly one rule without opting out
    of the rest. Ids are pinned alongside the pattern and message by
    ``tests/unit/test_antipatterns.py`` and must stay in step with the hook.

    ``context`` declares the syntactic surface the pattern inspects. A "code"
    rule is matched against token-masked source — string literals and comments
    both blanked — so an identifier quoted in prose never trips it; a
    "comment" rule targets comment directives (`# type: ignore`, `# noqa`) and
    is matched with comments intact. Where no tokenizer applies (the
    TypeScript-family table, text that fails to tokenize) every rule scans the
    raw line — those rules are genuinely text-shaped.

    ``refiner`` is present when the regex is wider than the defect the rule
    names and an AST context settles the difference. It carries the function
    itself, so reading the rule tells you what narrows it rather than only
    that something does. The row carries its name, which
    :func:`lup.policy.kernel.edit.refiner_named` resolves back to the function,
    because a row projected into the hermetic runtime is primitive and cannot
    carry a callable; ``test_declared_refiners_are_the_kernel_refiners`` pins
    the two to the same objects, since a rule refined on one side only is
    exactly the split that makes a marker unremovable.
    """

    id: str
    pattern: re.Pattern[str]
    message: str
    context: RuleContext = "code"
    refiner: Refiner | None = None
    strength: RuleStrength = "soft"
    """Whether a `# lup: ignore` may silence this rule at all.

    Soft by default, because most of these name a shape that is usually wrong
    and occasionally the only thing that works, and the audit exists to grade
    those exceptions. A rule is ``strong`` only when its replacement is right
    every time — then a suppression is not a reasoned exception but the defect
    with a comment on it, and this refuses to be silenced.
    """


PORTABLE_PYTHON_ANTI_PATTERNS: list[AntiPattern] = [
    AntiPattern(
        id="any-type",
        pattern=re.compile(r"\bAny\b"),
        message="Never use Any — use specific types, TypedDict, or BaseModel",
    ),
    AntiPattern(
        id="type-ignore",
        pattern=re.compile(r"#\s*type:\s*ignore"),
        message="Never use # type: ignore — fix the type error properly",
        context="comment",
    ),
    AntiPattern(
        id="pyright-ignore",
        pattern=re.compile(r"#\s*pyright:\s*ignore"),
        message="Never use # pyright: ignore — fix the type error properly",
        context="comment",
    ),
    AntiPattern(
        id="noqa",
        pattern=re.compile(r"#\s*noqa\b"),
        message="Never use # noqa — fix the lint issue properly",
        context="comment",
    ),
    AntiPattern(
        id="generic-base",
        strength="strong",
        pattern=re.compile(r"\bGeneric\["),
        message="Use Python 3.12+ class[T] syntax instead of Generic[T]",
    ),
    AntiPattern(
        id="typing-union",
        strength="strong",
        pattern=re.compile(r"\b(?:Optional|Union)\["),
        message="Use PEP 604 unions — X | None instead of Optional, X | Y instead of Union",
    ),
    AntiPattern(
        id="typing-generics",
        strength="strong",
        pattern=re.compile(r"\b(?:List|Dict|Tuple|Set)\["),
        message="Use lowercase builtin generics — list, dict, tuple, set — "
        "instead of the capitalized typing aliases",
    ),
    AntiPattern(
        id="all-export",
        pattern=re.compile(r"__all__\s*[=:]"),
        message="No __all__ — import directly from the defining module",
    ),
    AntiPattern(
        id="dict-str-object",
        pattern=re.compile(r"\b(?:dict|Mapping)\[\s*str\s*,\s*object\s*\]"),
        message="Never use dict[str, object] or Mapping[str, object] — use TypedDict or BaseModel",
    ),
    AntiPattern(
        # Flags a string-keyed dict/Mapping only when the VALUE is a scalar/
        # payload type (str, int, float, bool, bytes, complex, or a union that
        # opens with one). Concrete class and callable value types are left
        # alone: `dict[str, SessionFactory]`, `dict[str, LupMcpTool]`, `dict[str,
        # Callable[...]]` are registries/routers whose open, data-driven key
        # set IS the point. The smell is a CLOSED, enumerable key set with a
        # scalar value (config-shaped) — that wants a BaseModel or
        # dict[Literal[...], V]; JsonValue stays the escape for arbitrary JSON.
        id="dict-str-payload",
        pattern=re.compile(
            r"\b(?:dict|Mapping|MutableMapping)\[\s*str\s*,"
            r"\s*(?:str|int|float|bool|bytes|complex)\b"
        ),
        message="String-keyed dict with a scalar value hides shape when the keys are a "
        "CLOSED, enumerable set — use a BaseModel or dict[Literal[...], V]. When the keys "
        "are open and data-driven (a registry/cache/counter keyed by external data) this is "
        "legitimate: add `# lup: ignore[dict-str-payload]`. Concrete class/callable value "
        "types (dict[str, SessionFactory]) are already accepted; JsonValue covers arbitrary JSON",
    ),
    AntiPattern(
        # Flags every `.get(` — the user's explicit broad choice over a narrow
        # rule. On payload/TypedDict-shaped data use typed attribute access; on
        # a genuinely open dict (a registry or cache) it is one comment; a
        # typed non-mapping receiver takes none, since the audit refutes it.
        id="dict-get",
        pattern=re.compile(r"\.get\s*\("),
        refiner=Refiner(
            exempt=dict_get_exempt_lines,
            evidence="a decorator naming a route, not payload access",
        ),
        message="`.get(` on payload/TypedDict-shaped data hides the schema — use typed "
        "attribute access (BaseModel/TypedDict). On a genuinely open dict (registry, cache) "
        "add `# lup: ignore[dict-get]`. On a typed non-mapping receiver (an SDK client, a "
        "route decorator) add nothing — the audit resolves the declaration and refutes it, "
        "and a marker here is reported spurious",
    ),
    AntiPattern(
        id="bare-object",
        pattern=re.compile(r"(?:(?<!\w)(?!_)\w+\s*:|->)\s*object\b"),
        message="Bare `object` says nothing about the value — use a concrete type, "
        "TypedDict, or BaseModel, and narrow at untyped boundaries",
    ),
    AntiPattern(
        id="bare-basemodel",
        pattern=re.compile(r"(?:(?<!\[)\b\w+\s*:|->)\s*BaseModel\b(?!\s*[\]|])"),
        message="A parameter or return annotated exactly BaseModel accepts any model — "
        "name the concrete union of models or make the function generic",
    ),
    AntiPattern(
        id="tuple-shape",
        strength="strong",
        pattern=re.compile(r"\btuple\["),
        refiner=Refiner(
            exempt=tuple_shape_exempt_lines,
            evidence="an immutable sequence, not a positional shape",
        ),
        message="A fixed-arity `tuple[...]` hides what each position means — name the "
        "fields with a BaseModel. Fall back to a TypedDict only where a model cannot go: "
        "the hermetic kernel, which has no pydantic, or a field that must stay the caller's "
        "own object, which validation would copy. `tuple[X, ...]` is a sequence and never "
        "trips this",
    ),
    AntiPattern(
        # Mirrors tuple-shape for frozenset: every declared frozenset annotation
        # or constructed constant. A fixed name set constant wants a dict or a
        # purpose-built structure; an immutable-default-argument use is the one
        # legitimate site — `# lup: ignore[frozenset-shape]` marks it.
        id="frozenset-shape",
        pattern=re.compile(r"\bfrozenset\b"),
        message="A declared `frozenset[...]` shape or constant collapses structure a "
        "`dict[...]` keeps — each member is a bare name, and whatever it keyed has nowhere "
        "left to live. Use a dict, frozen once 3.15 ships `frozendict`, or a purpose-built "
        "structure. For a genuinely immutable default argument add "
        "`# lup: ignore[frozenset-shape]`",
    ),
    AntiPattern(
        # A declared or constructed set — `set(...)`/`set[...]` (no space
        # before the bracket; ruff never formats one in) or a `: set`/`-> set`
        # annotation. The dot lookbehind keeps `.set()` method calls out, and
        # prose about "set" never carries the bracket or annotation shape.
        # `frozenset` is caught by frozenset-shape and never trips this, since
        # its "set" is not a standalone word.
        id="set-shape",
        pattern=re.compile(r"(?<!\.)\bset[\[(]|(?::|->)\s*set\b"),
        message="A declared `set` collapses structure a `dict[...]` keeps — a bare set "
        "of strings is a record that lost its other fields, so whatever each member "
        "keyed has nowhere left to live. Use a dict when the members key something, or "
        "a `list[BaseModel]` when each carries more than its own name. Reach for "
        "membership on a local set comprehension "
        "instead of declaring the set as the interface. For a genuinely "
        "set-shaped value add `# lup: ignore[set-shape]`",
    ),
    AntiPattern(
        # The refiner clears every factory that does work a literal cannot say,
        # so what reaches a verdict is the empty collection the annotation
        # already names. A pydantic field is an annotated class declaration,
        # which empty-collection's refiner exempts, so no line trips both:
        # this rule owns the factory spelling and that one owns the seed.
        id="default-factory",
        pattern=re.compile(r"\bdefault_factory\s*="),
        refiner=Refiner(
            exempt=default_factory_exempt_lines,
            evidence="a factory doing work no annotated literal expresses",
        ),
        message="`Field(default_factory=list)` states in a factory what the annotation "
        "already declares — write the default as a literal, `items: list[B] = []`, which "
        "pydantic copies per instance. A factory that does real work (reads another "
        "declaration, stamps a value, builds a model) is cleared by both gates on its "
        "own, and a marker there is reported spurious",
    ),
    AntiPattern(
        # The refiner exempts deliberate defaults — __init__ state, call
        # kwargs, annotated module and class declarations — so what reaches a
        # verdict is the build-then-append seed. The annotated class
        # declaration among those is a pydantic field, whose factory spelling
        # default-factory owns. The lookbehind keeps `==`/`!=`/`<=`/`>=` out.
        id="empty-collection",
        pattern=re.compile(r"(?<![=!<>])=\s*(?:\{\}|\[\]|set\(\))"),
        refiner=Refiner(
            exempt=empty_collection_exempt_lines,
            evidence="a deliberate default, not a build-then-append seed",
        ),
        message="Empty-collection literals (`= {}`, `= []`, `= set()`) usually seed an "
        "append/mutate loop — build the collection with a comprehension, or, when the "
        "loop carries control flow a comprehension cannot, `yield` the items from a "
        "nested function and let its caller collect them. Add "
        "`# lup: ignore[empty-collection]` only for a fold neither expresses",
    ),
    AntiPattern(
        id="cast",
        pattern=re.compile(r"\bcast\s*\("),
        message="`cast(...)` is a code smell — narrow with isinstance or a type guard, "
        "or fix the annotation so the cast is unnecessary",
    ),
    AntiPattern(
        id="import-re",
        pattern=re.compile(r"\bimport\s+re\b|\bfrom\s+re\s+import\b"),
        message="`import re` / `from re import` is a code smell — parse structured data with "
        "its own API instead: JSON -> json.loads, paths -> pathlib.Path, URLs -> urllib.parse, "
        "XML/HTML -> xml.etree.ElementTree / lxml, dates -> datetime",
    ),
    AntiPattern(
        id="re-call",
        pattern=re.compile(
            r"\bre\.(compile|search|match|fullmatch|sub|findall|split)\s*\("
        ),
        message="Avoid regex for structured data — reach for its parser instead: "
        "JSON -> json.loads, paths -> pathlib.Path, URLs -> urllib.parse, "
        "XML/HTML -> xml.etree.ElementTree / lxml, dates -> datetime",
    ),
    AntiPattern(
        # `.replace` on `os`, `Path`, or a `*path` receiver is pathlib/os's
        # atomic file rename, not string surgery — the lookbehinds keep the
        # codebase's path-named receivers out of the net. os.replace as an atomic
        # rename is steered toward pathlib by os-file-ops instead, so the two
        # rules stay coherent: this one is only about string surgery.
        id="string-replace",
        pattern=re.compile(r"(?<!\bos)(?<![Pp]ath)\.replace\s*\("),
        message="Avoid .replace() for structured data — edit it through its parser instead "
        "(pathlib.Path for paths, urllib.parse for URLs, json for JSON)",
    ),
    AntiPattern(
        # Only separator-form `.split(sep)` is flagged — `.rsplit` and
        # `.partition`/`.rpartition` included, so the variants are not a dodge
        # (partition always takes a separator, so it always trips). A separator
        # implies structure with a real parser alternative (csv, pathlib,
        # urllib, json, datetime). Argument-less `.split()` is whitespace
        # tokenization of free text, for which no parser exists — the negative
        # lookahead exempts it.
        id="string-split",
        pattern=re.compile(r"\.r?split\s*\((?!\s*\))|\.r?partition\s*\("),
        message="Avoid .split(sep)/.rsplit/.partition for structured data — parse it "
        "instead (urllib.parse for URLs, pathlib.Path for paths, json for JSON, "
        "datetime for dates)",
    ),
    AntiPattern(
        # Only separator-form `.strip(chars)` is flagged — `.lstrip`/`.rstrip`
        # included, so the variants are not a dodge. Naming the characters to
        # strip implies field extraction from structured text; argless
        # stripping is whitespace framing, for which no parser exists — the
        # negative lookahead exempts it, mirroring string-split's argless rule.
        id="string-strip",
        pattern=re.compile(r"\.[lr]?strip\s*\((?!\s*\))"),
        message="Avoid .strip(chars)/.lstrip/.rstrip for structured data — parse it "
        "instead (urllib.parse for URLs, pathlib.Path for paths, json for JSON, "
        "datetime for dates)",
    ),
    AntiPattern(
        id="bare-except",
        pattern=re.compile(r"\bexcept\s*:"),
        message="Bare `except:` catches SystemExit/KeyboardInterrupt — name the exception",
    ),
    AntiPattern(
        id="except-baseexception",
        pattern=re.compile(r"\bexcept\s+BaseException\b"),
        message="except BaseException catches KeyboardInterrupt — use Exception or narrower",
    ),
    AntiPattern(
        id="suppress",
        pattern=re.compile(r"\bcontextlib\.suppress\b"),
        message="contextlib.suppress silently swallows exceptions — log, handle, or re-raise",
    ),
    AntiPattern(
        id="suppress-import",
        pattern=re.compile(r"\bfrom\s+contextlib\s+import\b.*\bsuppress\b"),
        message="contextlib.suppress silently swallows exceptions — log, handle, or re-raise",
    ),
    AntiPattern(
        id="dataclass",
        pattern=re.compile(
            r"@dataclass|\bimport\s+dataclasses\b|\bfrom\s+dataclasses\s+import\b"
        ),
        message="Use Pydantic BaseModel (or TypedDict) instead of dataclasses",
    ),
    AntiPattern(
        # Same dodge as dataclass: a typed record that ducks pydantic
        # validation. Catches the typing.NamedTuple class form and the
        # collections.namedtuple factory alike.
        id="namedtuple",
        pattern=re.compile(r"\bNamedTuple\b|\bnamedtuple\b"),
        message="Use Pydantic BaseModel (or TypedDict) instead of NamedTuple/namedtuple",
    ),
    AntiPattern(
        # Anchored at statement level so a `model_config` mention in a call's
        # keyword or a payload key is not a declaration. Prose cannot reach it
        # at all: a "code" rule reads token-masked source, so the identifier
        # inside a docstring or comment is already blank by the time it matches.
        id="model-config",
        pattern=re.compile(r"^\s*model_config\s*[:=]"),
        message="Declare pydantic configuration as class keywords — "
        "class A(BaseModel, frozen=True, extra='forbid') — instead of assigning "
        "model_config, so the configuration reads in the header beside the class "
        "it configures. Every key carries over under its own name; a shared "
        "ConfigDict alias inlines into each header rather than being imported",
    ),
    AntiPattern(
        id="subprocess",
        pattern=re.compile(r"\bimport\s+subprocess\b|\bfrom\s+subprocess\s+import\b"),
        message="Use the `sh` library instead of subprocess",
    ),
    AntiPattern(
        id="os-shell",
        pattern=re.compile(r"\bos\.(?:system|popen|exec[lv]\w*)\s*\("),
        message="Use the `sh` library instead of os.system()/os.popen()/os.exec*()",
    ),
    AntiPattern(
        id="argparse",
        pattern=re.compile(r"\bimport\s+argparse\b|\bfrom\s+argparse\s+import\b"),
        message="Use `typer` instead of argparse",
    ),
    AntiPattern(
        id="rich-progress",
        pattern=re.compile(r"\brich\.progress\b|\bfrom\s+rich\.progress\s+import\b"),
        message="Use `tqdm` instead of rich progress bars",
    ),
    AntiPattern(
        id="os-path",
        pattern=re.compile(r"\bos\.path\b"),
        message="Use pathlib.Path instead of os.path",
    ),
    AntiPattern(
        # A scoped list of os file/dir operations that all have a pathlib.Path
        # equivalent (iterdir, mkdir, unlink, rename, replace, stat, ...).
        # Config access (os.environ/os.getenv) and process launching (os.system/
        # os.exec*/os.popen) are covered by os-environ and os-shell instead.
        id="os-file-ops",
        pattern=re.compile(
            r"\bos\.(?:getcwd|chdir|listdir|scandir|walk|mkdir|makedirs|rmdir|"
            r"removedirs|remove|unlink|rename|renames|replace|link|symlink|"
            r"readlink|stat|lstat|chmod|chown)\s*\("
        ),
        message="Use pathlib.Path for file/dir operations instead of os.* "
        "(Path.iterdir/mkdir/unlink/rename/replace/stat/...)",
    ),
    AntiPattern(
        id="os-environ",
        pattern=re.compile(r"\bos\.(?:environ|getenv)\b"),
        message="Read configuration through pydantic-settings, not os.environ/os.getenv",
    ),
    AntiPattern(
        id="eval-exec",
        pattern=re.compile(r"(?<![.\w])(?:eval|exec)\s*\("),
        message="Never use eval()/exec() — parse the data (ast.literal_eval for "
        "literals) or dispatch explicitly",
    ),
    AntiPattern(
        id="utcnow",
        strength="strong",
        pattern=re.compile(r"\butcnow\s*\("),
        message="datetime.utcnow() is naive and deprecated — use datetime.now(timezone.utc)",
    ),
    AntiPattern(
        id="global-statement",
        pattern=re.compile(r"^global\s+\w"),
        message="No `global` statements — mutate a module-level holder object or pass "
        "state explicitly",
    ),
    AntiPattern(
        id="private-function",
        pattern=re.compile(r"\bdef\s+_[a-zA-Z]"),
        message="No `_` prefix on functions/methods — nothing is private (nest inside caller if needed)",
    ),
    AntiPattern(
        id="private-class",
        pattern=re.compile(r"\bclass\s+_[A-Z]"),
        message="No `_` prefix on classes — nothing is private",
    ),
    AntiPattern(
        id="private-variable",
        pattern=re.compile(r"^_[a-zA-Z]\w*\s*(?::[^=]*)?=(?!=)(?!.*,\s*$)"),
        message="No `_` prefix on variables/constants — nothing is private "
        "(unused `_` function parameters are exempt)",
    ),
]
"""Python rules whose message reads the same whatever runtime is shown it."""


# lup: ignore[constant-declaration] — the bare noun a rule's own sentence uses,
# declared with the rule rather than chosen per caller
DOCUMENT_IN_HAND = "the file"
"""How the document rule names the file for a runtime to place in its sentence.

A rule speaks about no particular path, so what a runtime interpolates here is
the bare noun; whatever it says about handing a document over whole is the
runtime's own sentence to make.
"""

NO_RUNTIME_READER = Unsupported(
    reason="this table was compiled for no runtime, so none of them is speaking here"
)
"""The reader the neutral table carries: the repository audit and the rule
reference are read by people rather than by a runtime, and inventing a tool
name for them would be the platform leak the seam exists to prevent."""


def pdf_extraction_rule(document_reader: Spelling) -> AntiPattern:
    """The rule against PDF text extractors, completed by one runtime's reader.

    The extractor's failure is silent: a scanned or image-only page yields an
    empty string, which reads downstream as an empty document rather than as
    an extraction that did not happen. Handing the file whole to the runtime's
    own reader has no such mode.

    Which tool that is belongs to the runtime, and this rule ships into every
    plugin tree — so the sentence is asked for rather than written here, and a
    runtime with nothing that takes a document contributes none, leaving the
    failure mode stated and no tool named.
    """
    return AntiPattern(
        id="pdf-extraction",
        pattern=re.compile(
            r"\b(?:import|from)\s+"
            r"(?:fitz|pymupdf|pypdf|PyPDF2|PyPDF4|pdfplumber|pdfminer|pypdfium2)\b"
        ),
        message=" ".join(
            words
            for words in (
                "A PDF text extractor comes back empty from a scanned or image-only "
                "page, and an empty string reads as an empty document rather than as "
                "an extraction that failed — read the document whole instead of "
                "pulling text out of it.",
                document_reader.in_prose(),
            )
            if words
        ),
    )


def python_anti_patterns(
    document_reader: Spelling,
    portable: list[AntiPattern] = PORTABLE_PYTHON_ANTI_PATTERNS,
) -> list[AntiPattern]:
    """The Python table one runtime is shown, in its own words where it has them.

    The portable rows are this library's reading of the conventions rather than
    anything Python settles, so a project holding itself to a different set
    passes its own — the same reason :class:`AntiPatternSet` takes its tables
    instead of naming them.
    """
    return [*portable, pdf_extraction_rule(document_reader)]


PYTHON_ANTI_PATTERNS: list[AntiPattern] = python_anti_patterns(NO_RUNTIME_READER)
"""Anti-patterns checked against added lines of `.py` files (mirrors the hook)."""


TS_ANTI_PATTERNS: list[AntiPattern] = [
    AntiPattern(
        id="as-any",
        pattern=re.compile(r"\bas\s+any\b"),
        message="Never use `as any` — use proper types or type guards",
    ),
    AntiPattern(
        id="as-unknown",
        pattern=re.compile(r"\bas\s+unknown\b"),
        message="Never use `as unknown` — use type guards or proper types",
    ),
    AntiPattern(
        id="any-annotation",
        pattern=re.compile(r":\s*any\b"),
        message="Never use `any` type annotation — use specific types, generics, or `unknown`",
    ),
    AntiPattern(
        id="any-assertion",
        pattern=re.compile(r"<any>"),
        message="Never use `<any>` type assertion — use proper types",
    ),
    AntiPattern(
        id="ts-ignore",
        pattern=re.compile(r"@ts-ignore"),
        message="Never use @ts-ignore — fix the type error properly",
        context="comment",
    ),
    AntiPattern(
        id="ts-expect-error",
        pattern=re.compile(r"@ts-expect-error"),
        message="Never use @ts-expect-error — fix the type error properly",
        context="comment",
    ),
    AntiPattern(
        id="ts-nocheck",
        pattern=re.compile(r"@ts-nocheck"),
        message="Never use @ts-nocheck — fix the type errors in the file",
        context="comment",
    ),
    AntiPattern(
        id="eslint-disable",
        pattern=re.compile(r"//\s*eslint-disable"),
        message="Never use eslint-disable — fix the lint issue properly",
        context="comment",
    ),
    AntiPattern(
        id="eslint-disable-block",
        pattern=re.compile(r"/\*\s*eslint-disable"),
        message="Never use eslint-disable — fix the lint issue properly",
        context="comment",
    ),
    AntiPattern(
        id="tslint-disable",
        pattern=re.compile(r"//\s*tslint:disable"),
        message="Never use tslint:disable — migrate to eslint and fix the issue",
        context="comment",
    ),
    AntiPattern(
        id="non-null-assertion",
        pattern=re.compile(r"[\w\)\]]!\."),
        message="Postfix `!.` non-null assertion hides a possible null/undefined — "
        "narrow the type or handle the missing case",
    ),
    AntiPattern(
        id="var-declaration",
        strength="strong",
        pattern=re.compile(r"\bvar\s+[A-Za-z_$]"),
        message="Use `const` or `let` instead of `var` — var is function-scoped and hoisted",
    ),
    AntiPattern(
        id="function-object-type",
        pattern=re.compile(r":\s*(?:Function|Object)\b"),
        message="Never use `Function` or `Object` as a type — declare the call "
        "signature or the object shape",
    ),
    AntiPattern(
        id="console-log",
        pattern=re.compile(r"\bconsole\.log\s*\("),
        message="console.log is a debug leftover — remove it or route through a logger",
    ),
]
"""Anti-patterns checked against added lines of TypeScript/JavaScript files."""


# lup: ignore[library-default] — Python's own source suffixes
PY_SUFFIXES = (".py", ".pyi")
# lup: ignore[library-default] — the suffixes those ecosystems compile
TS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte")

# lup: ignore[library-default] — the ids other codescan scanners own, so the set
# follows those rules' own identities rather than any taste of this module's
FOREIGN_RULE_IDS: frozenset[str] = frozenset(  # lup: ignore[frozenset-shape]
    {
        ABC_CAPABILITY_RULE_ID,
        CONSTANT_DECLARATION_RULE_ID,
        LIBRARY_DEFAULT_RULE_ID,
        MODEL_FREE_FUNCTION_RULE_ID,
        NATIVE_SPELLING_RULE_ID,
        OWN_MODEL_DISPATCH_RULE_ID,
        SEAM_BOUNDARY_RULE_ID,
    }
)
"""Rule ids owned by other codescan scanners.

A typed ``# lup: ignore[...]`` naming one of these is judged by that
scanner (the boundary scan honors its own id), so this auditor never
reports it spurious — while an id no scanner owns still is.
"""


def refined_refutations(text: str, patterns: list[AntiPattern]) -> list[Refutation]:
    """Every AST refiner's exemptions, as refutations.

    Projecting them here means the audit has exactly one notion of "matched,
    but refuted", whether the proof came from the AST alone or from the typed
    grammar's type oracle. The exemptions are the kernel's own, so a line the
    hook stops flagging is the same line the audit stops demanding a marker
    for — which is what keeps one gate from requiring a change the other
    refuses. Each rule carries its own refiner, so nothing here maps an id
    back to a function that lives elsewhere.
    """
    lines = text.splitlines()
    return [
        Refutation(
            rule_id=rule.id,
            line=line,
            subject=lines[line - 1].strip()[:80],
            evidence=rule.refiner.evidence,
        )
        for rule in patterns
        if rule.refiner is not None
        for line in sorted(rule.refiner.exempt(text))
        if line <= len(lines)
    ]


class AntiPatternSet(BaseModel, frozen=True, arbitrary_types_allowed=True):
    """Which anti-patterns a project checks, by the language they read.

    The rules a project holds itself to are its own conventions written down,
    so the tables this library ships are what a caller starts from rather than
    what it is stuck with — one set reaches the edit hook, the whole-file
    audit, and the generated rule reference together, so a project that
    replaces it replaces all three at once.
    """

    python: list[AntiPattern] = Field(default_factory=lambda: PYTHON_ANTI_PATTERNS)
    typescript: list[AntiPattern] = Field(default_factory=lambda: TS_ANTI_PATTERNS)

    def for_suffix(self, suffix: str) -> list[AntiPattern] | None:
        """The table that applies to a file suffix, or None to skip it.

        Mirrors the hook's split: Python files are checked against the Python
        table, TS/JS-family files against the TS table, and any other suffix
        is not scanned (the hook only gates those two families).
        """
        if suffix in PY_SUFFIXES:
            return self.python
        if suffix in TS_SUFFIXES:
            return self.typescript
        return None

    def selected(self, selection: RuleSelection) -> "AntiPatternSet":
        """This set with the rules a project retired taken out of both tables.

        Narrowing the table rather than filtering findings afterwards is what
        keeps a retired rule from reaching any surface at all: it does not
        fire, it does not render into the compiled plugin, and a directive
        naming it is not graded against a rule that stopped existing here.
        """
        return AntiPatternSet(
            python=[rule for rule in self.python if selection.keeps(rule.id)],
            typescript=[rule for rule in self.typescript if selection.keeps(rule.id)],
        )


def antipattern_set_for(
    document_reader: Spelling, selection: RuleSelection | None = None
) -> AntiPatternSet:
    """The tables one native plugin ships, its own reader spelled into them.

    Generation reaches this once per runtime, so the rows compiled into a
    plugin say what that runtime can actually do — and a runtime that declines
    ships the rule with its reason stated and no tool it does not have named.

    The project's selection narrows the same tables here, so a retired rule is
    absent from the compiled plugin rather than enforced by a hook the sweep
    stopped agreeing with.
    """
    return AntiPatternSet(python=python_anti_patterns(document_reader)).selected(
        selection or RuleSelection()
    )


# `AntiPatternSet.for_suffix` is the operation; this binds the default table to it.
# lup: ignore[model-free-function] — the suffix is the subject, the set its table
def patterns_for_suffix(
    suffix: str, rules: AntiPatternSet | None = None
) -> list[AntiPattern] | None:
    """The anti-pattern table one file suffix is checked against."""
    return (rules or AntiPatternSet()).for_suffix(suffix)


def line_hits(
    lines: LineProjections, line_no: int, patterns: list[AntiPattern]
) -> list[AntiPattern]:
    """Every anti-pattern one line trips, each matched in its declared context.

    Tokenized Python is scanned per rule context: a "code" rule sees string
    literals and comments blanked, so identifiers in prose never match, while
    a "comment" directive rule sees comments intact — a standalone `# noqa`
    line included. Untokenized text (a non-Python file, a fragment) keeps the
    conservative whole-line scan, skipping blank lines and pure comments that
    carry no `type:` directive — aligned with what the hook would decide.
    """
    if not lines.tokenized:
        raw = lines.commented[line_no - 1].strip()
        if not raw or (raw.startswith("#") and "type:" not in raw):
            return []
        return [ap for ap in patterns if ap.pattern.search(raw)]
    return [
        ap
        for ap in patterns
        if (text := lines.scan_text(line_no, ap.context)) and ap.pattern.search(text)
    ]


class AntiPatternFinding(BaseModel):
    """One auditor result about a single line's anti-pattern guarding.

    ``kind`` is:
    - "missing": the line trips a rule with no `# lup: ignore` covering it.
    - "spurious": a `# lup: ignore[id]` (or a bare one) guards a rule the line
      does not trip — a dead directive to delete.
    - "untyped": a bare `# lup: ignore` validly silences the line but names no
      rule; it stays valid, and is surfaced so migration to typed directives is
      gradual (advisory, not a blocker).

    ``rule_id`` is the rule the finding concerns (empty for a bare marker that
    guards nothing). ``line`` is 1-based.
    """

    kind: str
    line: int
    text: str
    message: str
    rule_id: str = ""


def audit_text(
    text: str,
    patterns: list[AntiPattern],
    refutations: list[Refutation] | None = None,
) -> list[AntiPatternFinding]:
    """Audit one file's current text for per-rule ignore-marker health.

    A bare file-level `# lup: ignore` opts the whole file out (matching the
    hook); the audit reports it as a single advisory "untyped" finding and
    scans nothing else. A typed file-level `# lup: ignore[id]` opts out only
    the named rule file-wide — and when nothing in the file still needs an
    id (no line trips it that an inline directive would not already cover),
    that id reports "spurious" at the directive line, so refiner and rule
    evolution cannot leave dead file-wide opt-outs behind. Docstring lines are skipped
    entirely: prose is not code, and no inline directive could ever guard it —
    a comment cannot open inside a string. (The hook still scans docstring
    lines at edit time; it sees only text fragments it cannot tokenize, so the
    hook stays strictly stricter than this audit.) Then, per line and per rule:

    - a tripped rule with no covering ignore -> "missing";
    - a typed `# lup: ignore[id]` naming a rule the line does not trip, or a
      bare ignore on a line that trips nothing -> "spurious";
    - a bare `# lup: ignore` that does silence the line -> "untyped".

    A hit a refiner refuted is not a trip at all, so a directive naming that
    rule there reports "spurious" — the audit drives the cleanup of markers
    refinement made unnecessary. Two sources feed this: the kernel's own AST
    exemptions (see :func:`refined_exempt_lines`), computed here, and
    whatever `refutations` the caller resolved — the typed grammar in
    `lup.codescan.grammar` passes the sites whose receiver a type oracle
    proved outside the rule's family. With no refutations supplied and no
    oracle behind them, every broad regex verdict stands.

    An ignore counts as a guard only where a comment actually starts (per the
    tokenizer), so a docstring or string literal that merely *mentions*
    `# lup: ignore` — and a note whose prose quotes it — guards nothing. Text
    that does not tokenize as Python falls back to a plain substring check.

    Where it may be written is one policy for every rule, line-shaped and
    AST-shaped alike: the line it guards, or standing alone directly above —
    the placement a reason too long for the column budget needs, since a
    comment is the one thing the formatter cannot wrap. A directive is graded
    against the lines it reaches rather than the line it sits on, so the
    overflow placement is read where it applies instead of being reported as
    guarding nothing.
    """
    file_ignore = file_level_ignore(text)
    file_disabled: set[str] = set()
    if file_ignore is not None:
        if file_ignore.rule_ids is None:
            return [  # bare file-level opt-out disables every rule — surface it
                AntiPatternFinding(
                    kind="untyped",
                    line=file_ignore.line,
                    text="# lup: ignore",
                    message="bare file-level `# lup: ignore` opts the whole file out of "
                    "every rule — name the rules it needs: `# lup: ignore[rule, ...]`",
                )
            ]
        file_disabled = file_ignore.rule_ids

    context = PythonContext.parse(text)
    refuted = {
        (refutation.rule_id, refutation.line)
        for refutation in refined_refutations(text, patterns) + (refutations or [])
    }

    file_ignore_line = file_ignore.line if file_ignore is not None else 0
    original_lines = text.splitlines()
    projections = LineProjections.parse(text)

    def written_directive(line_no: int) -> re.Match[str] | None:
        """The directive actually written on one line, if a comment opens it.

        The whole-file opt-out is not one of these. It stands alone like the
        overflow placement does, so reading it as one would quietly make it
        the guard for whatever line happens to follow the header.
        """
        if line_no < 1 or line_no > len(original_lines) or line_no == file_ignore_line:
            return None
        match = IGNORE_RE.search(original_lines[line_no - 1])
        if match is None or not context.comment_at(line_no, match.start()):
            return None
        return match

    def guarding_directive(line_no: int) -> re.Match[str] | None:
        """The directive covering `line_no`, wherever the one policy puts it.

        The same placement every project-wide rule accepts, so a marker that
        reads correctly against an AST rule reads correctly here. A directive
        found above is the overflow placement: the line it guards had no room
        left for the reason that justifies it, and a reason worth reading
        often needs more than one line of its own.

        Every line up to the head of the comment block, because that is what
        `suppression_reaches` accepts and this only asks it where to look. A
        fixed pair of candidates was exactly complete while the policy was
        capped at the line directly above, and stopped being the moment it
        widened.
        """
        return next(
            (
                match
                for candidate in range(line_no, 0, -1)
                if (match := written_directive(candidate)) is not None
                and suppression_reaches(original_lines, candidate, line_no)
            ),
            None,
        )

    def guarded_lines(line_no: int) -> list[int]:
        """Every audited line the directive written on `line_no` reaches.

        The mirror of :func:`guarding_directive`, and what keeps the reported
        failure from recurring: a directive is judged against the lines it
        actually covers, so one standing above its violation is read there
        rather than reported as guarding nothing where it sits.

        Asked of every audited line rather than the next one, so the two
        directions agree. Where they disagreed, one marker was reported
        spurious here and its violation reported missing there — the failure
        this pair exists to prevent, produced by the pair itself.
        """
        return [
            candidate
            for candidate in sorted(hits_by_line)
            if candidate >= line_no
            and suppression_reaches(original_lines, line_no, candidate)
        ]

    hits_by_line = {
        number: [
            ap
            for ap in line_hits(projections, number, patterns)
            if (ap.id, number) not in refuted
        ]
        for number in range(1, len(original_lines) + 1)
        # The file-level directive line is not itself audited, and docstring
        # prose is not code — no comment can open inside a string to guard it.
        if number != file_ignore_line and number not in context.docstring_lines
    }

    def preview_of(line_no: int) -> str:
        return original_lines[line_no - 1].strip()[:80]

    file_live: set[str] = set()
    findings: list[AntiPatternFinding] = []
    for index, hits in hits_by_line.items():
        directive = guarding_directive(index)
        covered_ids = ignore_rule_ids(directive) if directive is not None else None
        for ap in hits:
            if ap.strength == "strong":
                # No directive reaches this one. A soft rule's suppression is a
                # reasoned exception the audit then grades; a strong rule has a
                # replacement that is right every time, so the same comment
                # would only record a decision to keep the defect.
                findings.append(
                    AntiPatternFinding(
                        kind="missing",
                        line=index,
                        text=preview_of(index),
                        message=f"{ap.message} (no suppression: write the replacement)",
                        rule_id=ap.id,
                    )
                )
                continue
            if ap.id in file_disabled:
                # Live only when the file-level directive is the sole silencer;
                # an inline-covered hit does not keep the file-wide id alive.
                if directive is None or not (
                    covered_ids is None or ap.id in covered_ids
                ):
                    file_live.add(ap.id)
                continue
            if directive is not None and (covered_ids is None or ap.id in covered_ids):
                continue
            findings.append(
                AntiPatternFinding(
                    kind="missing",
                    line=index,
                    text=preview_of(index),
                    message=f"{ap.message} — suppress on {suppression_placement(index)}",
                    rule_id=ap.id,
                )
            )

    # Every directive is graded where it is written, against the lines it
    # reaches rather than the one it sits on. Those differ for exactly the
    # overflow placement, and conflating them is what reported a marker
    # standing above its violation as guarding nothing.
    for index in range(1, len(original_lines) + 1):
        directive = written_directive(index)
        if directive is None:
            continue
        named = ignore_rule_ids(directive)
        guarded = [
            ap for line_no in guarded_lines(index) for ap in hits_by_line[line_no]
        ]
        reached = {ap.id for ap in guarded}
        silenced = {
            ap.id
            for ap in guarded
            if ap.strength != "strong" and ap.id not in file_disabled
        }
        if named is not None:
            findings.extend(
                AntiPatternFinding(
                    kind="spurious",
                    line=index,
                    text=preview_of(index),
                    message=f"`# lup: ignore[{rid}]` guards a line that does not trip `{rid}` — remove it",
                    rule_id=rid,
                )
                for rid in sorted(named - reached - FOREIGN_RULE_IDS)
            )
            continue
        covered = sorted(rid for rid in reached if rid not in file_disabled)
        if silenced:
            findings.append(
                AntiPatternFinding(
                    kind="untyped",
                    line=index,
                    text=preview_of(index),
                    message="`# lup: ignore` is untyped — name the rule(s) it silences: "
                    f"`# lup: ignore[{', '.join(covered)}]`",
                    rule_id=covered[0] if covered else "",
                )
            )
        else:
            findings.append(
                AntiPatternFinding(
                    kind="spurious",
                    line=index,
                    text=preview_of(index),
                    message="`# lup: ignore` guards a line that matches no anti-pattern — remove it",
                )
            )

    if file_ignore is not None:
        directive_text = text.splitlines()[file_ignore.line - 1].strip()[:80]
        for rid in sorted(file_disabled - file_live - FOREIGN_RULE_IDS):
            findings.append(
                AntiPatternFinding(
                    kind="spurious",
                    line=file_ignore.line,
                    text=directive_text,
                    message=f"file-level `# lup: ignore[{rid}]` names a rule nothing "
                    "in the file needs — remove it",
                    rule_id=rid,
                )
            )
    return findings
