"""Project-indexed model-free-function rule tests.

The rule is a proxy for a judgement it cannot make — whether behaviour belongs
on the model — so what has to be pinned is the shape it stands in for, and
each narrowing that keeps it from firing where no method could go. A defect in
the shape degrades silently toward *fewer* findings rather than failing, which
is why the exclusions are named here one by one rather than left to a single
"clean file" assertion.
"""

from pathlib import Path

from lup.codescan.behaviour import audit_model_free_functions
from lup.codescan.common import PythonSource
from lup.codescan.project import RuleFinding

MODEL_MODULE = """
from pydantic import BaseModel


class Part(BaseModel):
    kind: str


class TextPart(Part):
    text: str
"""


def source(text: str, module: str = "sample") -> PythonSource:
    return PythonSource(path=Path(f"{module}.py"), module=module, text=text)


def reported(text: str, findings: list[RuleFinding]) -> list[str]:
    """Each finding as its kind and the line it names, rather than a number.

    A rule that reports the wrong line is the failure worth catching, and a
    number cannot show it; the line's own text can.
    """
    lines = text.splitlines()
    return [f"{finding.kind}:{lines[finding.line - 1].strip()}" for finding in findings]


def audit(text: str) -> list[str]:
    """Findings for one module that declares its own model and acts on it."""
    whole = MODEL_MODULE + text
    return reported(whole, audit_model_free_functions([source(whole, "models")]))


def cross_module(text: str) -> list[str]:
    """Findings for a module acting on a model another module declares."""
    return reported(
        text,
        audit_model_free_functions(
            [source(MODEL_MODULE, "models"), source(text, "convert")]
        ),
    )


def test_a_free_function_over_its_own_module_s_model_is_reported() -> None:
    assert audit("\n\ndef render(part: TextPart) -> str:\n    return part.text\n") == [
        "missing:def render(part: TextPart) -> str:"
    ]


def test_every_parameter_position_is_read() -> None:
    """Keyword-only and starred parameters are the same shape, spelled apart."""
    assert audit("\n\ndef render(*parts: TextPart) -> str:\n    return ''\n") == [
        "missing:def render(*parts: TextPart) -> str:"
    ]
    assert audit(
        "\n\ndef render(text: str, *, part: TextPart) -> str:\n    return ''\n"
    ) == ["missing:def render(text: str, *, part: TextPart) -> str:"]


def test_a_union_reports_each_model_it_names_once() -> None:
    """`Part | TextPart` is two models named, and the message says both."""
    findings = audit_model_free_functions(
        [
            source(
                MODEL_MODULE + "\n\ndef render(part: Part | TextPart) -> str:\n"
                "    return ''\n",
                "models",
            )
        ]
    )
    assert [f.kind for f in findings] == ["missing"]
    assert "Part, TextPart" in findings[0].message


def test_a_method_on_the_model_is_never_reported() -> None:
    """The shape the rule steers toward, which the conventions require."""
    assert (
        audit_model_free_functions(
            [
                source(
                    "from pydantic import BaseModel\n"
                    "\n"
                    "\n"
                    "class TextPart(BaseModel):\n"
                    "    text: str\n"
                    "\n"
                    "    def render(self, other: 'TextPart') -> str:\n"
                    "        return self.text + other.text\n",
                    "models",
                )
            ]
        )
        == []
    )


def test_a_constructor_is_not_reported() -> None:
    """A model named only in the return builds the value rather than acting.

    There is no instance to carry "build me one", so the operation has nowhere
    to move to — which is why the return annotation is not read at all.
    """
    assert (
        audit("\n\ndef parse(raw: str) -> TextPart:\n    return TextPart(text=raw)\n")
        == []
    )


def test_a_boundary_converter_is_not_reported() -> None:
    """A model another module declares is the seam, and the seam is the point.

    Moving the operation onto the model would push the converter's own
    knowledge into the code the seam exists to keep clear of it.
    """
    assert (
        cross_module(
            "from models import TextPart\n"
            "\n"
            "\n"
            "def to_wire(part: TextPart) -> str:\n"
            "    return part.text\n"
        )
        == []
    )


def test_a_container_of_models_is_a_walk_and_is_not_reported() -> None:
    """The operation a walk performs belongs to the walk, not to a member."""
    assert audit("\n\ndef longest(parts: list[TextPart]) -> int:\n    return 0\n") == []


def test_a_function_over_what_we_do_not_declare_is_not_reported() -> None:
    """Vendor payloads, standard-library nodes, and builtins are not ours."""
    assert (
        audit(
            "\n\nimport ast\n"
            "\n"
            "\n"
            "def walk(node: ast.Name, count: int) -> str:\n"
            "    return node.id\n"
        )
        == []
    )


def test_a_typed_suppression_covers_the_site() -> None:
    assert (
        audit(
            "\n\n# lup: ignore[model-free-function] — boundary\n"
            "def render(part: TextPart) -> str:\n"
            "    return part.text\n"
        )
        == []
    )


def test_a_bare_suppression_is_reported_as_untyped() -> None:
    assert audit(
        "\n\n# lup: ignore\ndef render(part: TextPart) -> str:\n    return part.text\n"
    ) == ["untyped:# lup: ignore"]


def test_a_suppression_guarding_nothing_is_reported_as_spurious() -> None:
    assert audit(
        "\n\ndef render(raw: str) -> str:  # lup: ignore[model-free-function] — stale\n"
        "    return raw\n"
    ) == [
        "spurious:def render(raw: str) -> str:  # lup: ignore[model-free-function] — stale"
    ]
