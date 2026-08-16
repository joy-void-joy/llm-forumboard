"""Project-indexed capability architecture rule tests."""

from pathlib import Path

import pytest

from lup.codescan.capabilities import audit_capabilities
from lup.codescan.common import PythonSource


def source(text: str, name: str = "sample") -> PythonSource:
    return PythonSource(path=Path(f"{name}.py"), module=name, text=text)


@pytest.mark.parametrize("count", [1, 2, 3])
def test_cohesive_one_to_three_method_capabilities_pass(count: int) -> None:
    methods = "\n".join(
        f"    @abstractmethod\n    def method_{index}(self) -> None: ..."
        for index in range(count)
    )
    findings = audit_capabilities(
        [source(f"from abc import ABC, abstractmethod\nclass Good(ABC):\n{methods}\n")]
    )
    assert findings == []


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("    pass", "has 0 abstract behavior methods"),
        (
            "\n".join(
                f"    @abstractmethod\n    def m{index}(self) -> None: ..."
                for index in range(4)
            ),
            "has 4 abstract behavior methods",
        ),
        (
            "    @property\n    @abstractmethod\n    def value(self) -> int: ...",
            "declares abstract property value",
        ),
        (
            "    @abstractmethod\n    def run(self) -> None: ...\n"
            "    def helper(self) -> None: pass",
            "has concrete callable helper",
        ),
    ],
)
def test_invalid_capability_shapes_fail(body: str, message: str) -> None:
    findings = audit_capabilities(
        [source(f"from abc import ABC, abstractmethod\nclass Bad(ABC):\n{body}\n")]
    )
    assert any(message in finding.message for finding in findings)


def test_cross_module_inheritance_and_multiple_capabilities_fail() -> None:
    capabilities = source(
        "from abc import ABC, abstractmethod\n"
        "class Read(ABC):\n"
        "    @abstractmethod\n"
        "    def read(self) -> str: ...\n"
        "class Write(ABC):\n"
        "    @abstractmethod\n"
        "    def write(self) -> None: ...\n",
        "caps",
    )
    implementation = source(
        "from caps import Read, Write\n"
        "class Both(Read, Write):\n"
        "    def read(self) -> str: return ''\n"
        "    def write(self) -> None: pass\n",
        "impl",
    )
    findings = audit_capabilities([capabilities, implementation])
    assert any("implements multiple capabilities" in item.message for item in findings)


def test_inherited_abstract_method_keeps_subclass_abstract() -> None:
    findings = audit_capabilities(
        [
            source(
                "from abc import ABC, abstractmethod\n"
                "class Capability(ABC):\n"
                "    @abstractmethod\n"
                "    def run(self) -> None: ...\n"
                "class InvalidChild(Capability):\n"
                "    pass\n"
            )
        ]
    )

    assert any(
        finding.line == 5 and "inherits capability Capability" in finding.message
        for finding in findings
    )


def test_implemented_inherited_abstract_method_is_concrete() -> None:
    findings = audit_capabilities(
        [
            source(
                "from abc import ABC, abstractmethod\n"
                "class Capability(ABC):\n"
                "    @abstractmethod\n"
                "    def run(self) -> None: ...\n"
                "class Implementation(Capability):\n"
                "    def run(self) -> None: return None\n"
            )
        ]
    )

    assert findings == []


def test_transitive_implementation_inheritance_fails() -> None:
    findings = audit_capabilities(
        [
            source(
                "from abc import ABC, abstractmethod\n"
                "class Capability(ABC):\n"
                "    @abstractmethod\n"
                "    def run(self) -> None: ...\n"
                "class First(Capability):\n"
                "    def run(self) -> None: pass\n"
                "class Second(First):\n"
                "    pass\n"
                "class Third(Second):\n"
                "    pass\n"
            )
        ]
    )

    inherited = [
        finding
        for finding in findings
        if "inherits an implementation" in finding.message
    ]
    assert [finding.line for finding in inherited] == [7, 9]


def test_typed_suppression_is_used_and_spurious_one_is_reported() -> None:
    used = source(
        "from abc import ABC\n"
        "class Marker(ABC):  # lup: ignore[abc-capability]\n"
        "    pass\n",
        "used",
    )
    spurious = source(
        "class Plain:  # lup: ignore[abc-capability]\n    pass\n",
        "spurious",
    )
    findings = audit_capabilities([used, spurious])
    assert len(findings) == 1
    assert findings[0].kind == "spurious"


def test_bare_suppression_is_audited_as_untyped() -> None:
    findings = audit_capabilities(
        [source("from abc import ABC\nclass Marker(ABC):  # lup: ignore\n    pass\n")]
    )
    assert len(findings) == 1
    assert findings[0].kind == "untyped"


def test_abstract_overloads_count_as_one_capability_operation() -> None:
    findings = audit_capabilities(
        [
            source(
                "from abc import ABC, abstractmethod\n"
                "from typing import overload\n"
                "class Capability(ABC):\n"
                "    @overload\n"
                "    @abstractmethod\n"
                "    def run(self, value: int) -> int: ...\n"
                "    @overload\n"
                "    @abstractmethod\n"
                "    def run(self, value: str) -> str: ...\n"
                "    @abstractmethod\n"
                "    def run(self, value: int | str) -> int | str: ...\n"
            )
        ]
    )

    assert findings == []
