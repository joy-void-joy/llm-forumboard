"""Project-indexed own-model dispatch rule tests.

The rule's whole claim is a discrimination: dispatch over a union this
repository declares is reported, and narrowing untyped data at a boundary is
not. Both halves are pinned here, along with the path-to-module resolution the
discrimination rests on — a defect there degrades silently to *fewer*
findings rather than failing, so it needs a test that names the answer.
"""

from pathlib import Path

from lup.codescan.dispatch import audit_own_model_dispatch
from lup.codescan.common import PythonSource, module_name
from lup.devtools.dev.antipatterns import scanned_roots
from lup.devtools.project import DevProject

MODEL_MODULE = """
from pydantic import BaseModel


class Part(BaseModel):
    kind: str


class TextPart(Part):
    text: str
"""


def source(text: str, module: str = "sample") -> PythonSource:
    return PythonSource(path=Path(f"{module}.py"), module=module, text=text)


def audit(text: str) -> list[str]:
    """Findings for one dispatching module read against the model module."""
    findings = audit_own_model_dispatch(
        [source(MODEL_MODULE, "models"), source(text, "walk")]
    )
    return [f"{finding.kind}:{finding.line}" for finding in findings]


def test_module_name_uses_the_import_root_src_introduces() -> None:
    """A distribution repeats its name above ``src``; the later one is the root.

    Taking the first matching segment resolves this path to
    ``lup.src.lup.harness.models``, a name no import produces — every
    cross-module symbol lookup against it misses and the rule quietly reports
    less than it should.
    """
    assert (
        module_name(Path("packages/lup/src/lup/harness/models.py"))
        == "lup.harness.models"
    )
    assert (
        module_name(
            Path("src/lup_template/devtools/dev/check.py"),
            scanned_roots(DevProject(package="lup_template")),
        )
        == "lup_template.devtools.dev.check"
    )
    assert module_name(Path("packages/lup/src/lup/__init__.py")) == "lup"


def test_isinstance_on_a_declared_model_is_reported() -> None:
    assert audit(
        "from models import TextPart\n"
        "def walk(part):\n"
        "    return isinstance(part, TextPart)\n"
    ) == ["missing:3"]


def test_case_arm_on_a_declared_model_is_reported() -> None:
    assert audit(
        "from models import TextPart\n"
        "def walk(part):\n"
        "    match part:\n"
        "        case TextPart():\n"
        "            return 1\n"
    ) == ["missing:4"]


def test_assert_never_is_reported() -> None:
    assert audit(
        "from typing import assert_never\ndef walk(part):\n    assert_never(part)\n"
    ) == ["missing:3"]


def test_narrowing_untyped_data_at_a_boundary_is_not_reported() -> None:
    """Builtins, standard-library nodes, and vendor types are not ours."""
    assert (
        audit(
            "import ast\n"
            "def walk(payload, node):\n"
            "    if isinstance(payload, dict | list | str):\n"
            "        return isinstance(node, ast.Name)\n"
            "    return isinstance(payload, BaseException)\n"
        )
        == []
    )


def test_every_type_in_a_multi_type_check_is_resolved_separately() -> None:
    """A tuple or ``|`` check reports its declared models and skips the rest."""
    assert audit(
        "from models import Part, TextPart\n"
        "def walk(part):\n"
        "    return isinstance(part, (str, TextPart, Part))\n"
    ) == ["missing:3", "missing:3"]


def test_a_typed_suppression_covers_the_site() -> None:
    assert (
        audit(
            "from models import TextPart\n"
            "def walk(part):\n"
            "    return isinstance(part, TextPart)  "
            "# lup: ignore[own-model-dispatch] — boundary\n"
        )
        == []
    )


def test_a_bare_suppression_is_reported_as_untyped() -> None:
    assert audit(
        "from models import TextPart\n"
        "def walk(part):\n"
        "    return isinstance(part, TextPart)  # lup: ignore\n"
    ) == ["untyped:3"]


def test_a_suppression_guarding_nothing_is_reported_as_spurious() -> None:
    assert audit(
        "def walk(part):\n    return part  # lup: ignore[own-model-dispatch] — stale\n"
    ) == ["spurious:2"]
