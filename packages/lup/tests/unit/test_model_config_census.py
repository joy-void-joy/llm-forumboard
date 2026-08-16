"""The configuration census, and the equivalence it exists to make provable."""

import pytest

from lup.devtools.dev.model_config import (
    ModuleCensus,
    declared_configuration,
    parse_source,
)

ALIASES = 'FROZEN = ConfigDict(frozen=True)\nFROZEN_STRICT = ConfigDict(frozen=True, extra="forbid")\n'


def module_of(path: str, source: str) -> ModuleCensus:
    """The parsed module a test wrote, or a failure naming what would not parse."""
    parsed = parse_source(path, source)
    assert parsed.module is not None, parsed.unparsed
    return parsed.module


@pytest.mark.parametrize(
    ("declaration", "shape"),
    [
        ("ConfigDict(frozen=True)", "config-dict"),
        ('SettingsConfigDict(extra="ignore")', "settings-config-dict"),
        ("FROZEN", "alias"),
        ('{"arbitrary_types_allowed": True}', "dict-literal"),
        ("BASE | ConfigDict(frozen=True)", "other"),
    ],
)
def test_census_names_each_declaration_shape(declaration: str, shape: str) -> None:
    """A census enumerates shapes rather than assuming the ones already known.

    ``other`` is a shape and not an error, so a spelling nobody anticipated is
    reported instead of being folded into a known one and silently rewritten as
    something it never meant.
    """
    source = f"{ALIASES}class A(BaseModel):\n    model_config = {declaration}\n"
    module = module_of("a.py", source)
    assert [one.shape for one in module.declarations({})] == [shape]


@pytest.mark.parametrize(
    ("declaration", "hazard"),
    [
        ("NOT_DECLARED_ANYWHERE", "alias-not-in-module"),
        ("BASE | ConfigDict(frozen=True)", "unrecognized-shape"),
        ("ConfigDict(**BASE)", "unpacked-call"),
        ("{**BASE}", "unpacked-dict"),
    ],
)
def test_a_declaration_that_cannot_be_read_says_so(
    declaration: str, hazard: str
) -> None:
    """A site a rewrite cannot resolve is flagged, never rendered as empty.

    Empty keywords and unresolved keywords look identical once written into a
    class header — the first is a class with no configuration, the second is a
    class whose configuration was dropped — so the difference has to survive as
    a hazard rather than as an absence.
    """
    source = f"{ALIASES}class A(BaseModel):\n    model_config = {declaration}\n"
    (found,) = module_of("a.py", source).declarations({})
    assert hazard in found.hazards
    assert found.keywords == []


def test_census_reads_declarations_not_text() -> None:
    """A `model_config` named in a docstring or a comment is not a site.

    The census is what a conversion's coverage is judged against, so prose
    counted as a declaration would overstate the work and prose missed as one
    would understate it.
    """
    prose = '"""Assigning model_config = ConfigDict(frozen=True) is the old form."""\n\n# model_config = FROZEN\nx = "model_config = 1"\n'
    assert list(module_of("a.py", prose).declarations({})) == []


def test_each_alias_resolves_to_its_own_configuration() -> None:
    """Two aliases that differ by one key stay different through the census.

    The failure this guards is the whole point of the census: rewriting every
    alias-bound site to the most common alias's keywords drops the keys the
    rarer ones carried, and does it without changing a line that looks wrong.
    """
    source = f"{ALIASES}class A(BaseModel):\n    model_config = FROZEN\n\nclass B(BaseModel):\n    model_config = FROZEN_STRICT\n"
    snapshot = declared_configuration([parse_source("a.py", source)])
    assert snapshot.models["a.py::A"] == {"frozen=True": "declared"}
    assert snapshot.models["a.py::B"] == {
        "frozen=True": "declared",
        'extra="forbid"': "declared",
    }


def test_an_imported_alias_resolves_to_the_module_that_declared_it() -> None:
    """An alias is shared by importing it, so resolution crosses modules.

    Resolving only within the file would classify an imported alias as
    unrecognized and leave a rewrite unable to tell which configuration it
    stood for — the site most likely to lose a key.
    """
    declaring = parse_source("packages/lup/src/lup/runtime/models.py", ALIASES)
    using = parse_source(
        "packages/lup/src/lup/harness/models.py",
        "from lup.runtime.models import FROZEN_STRICT\n\nclass A(BaseModel):\n    model_config = FROZEN_STRICT\n",
    )
    snapshot = declared_configuration([declaring, using])
    assert snapshot.models["packages/lup/src/lup/harness/models.py::A"] == {
        "frozen=True": "declared",
        'extra="forbid"': "declared",
    }


def test_a_dict_literal_keeps_every_key_it_carried() -> None:
    """A bare dict declaration converts by key, not by assumption.

    These sites carry the keys least likely to be guessed — a lone
    ``arbitrary_types_allowed`` with no ``frozen`` in sight — so reading them
    as plain frozen models is exactly how a key goes missing.
    """
    source = (
        'class A(BaseModel):\n    model_config = {"arbitrary_types_allowed": True}\n'
    )
    assert declared_configuration([parse_source("a.py", source)]).models == {
        "a.py::A": {"arbitrary_types_allowed=True": "declared"}
    }


def test_both_spellings_declare_the_same_configuration() -> None:
    """An assigned `model_config` and class keywords read as one mapping.

    What makes a snapshot taken before a rewrite comparable to one taken after
    it: if the two spellings did not read alike here, every converted class
    would look changed and the comparison would prove nothing.
    """
    assigned = 'class A(BaseModel):\n    model_config = ConfigDict(frozen=True, extra="forbid")\n'
    keywords = 'class A(BaseModel, frozen=True, extra="forbid"):\n    pass\n'
    assert (
        declared_configuration([parse_source("a.py", assigned)]).models
        == declared_configuration([parse_source("a.py", keywords)]).models
    )


def test_a_file_that_will_not_parse_is_reported() -> None:
    """Coverage that depends on nobody having written a syntax error is not coverage."""
    parsed = parse_source("a.py", "class A(BaseModel:\n")
    assert parsed.module is None
    assert parsed.unparsed.startswith("a.py:")
