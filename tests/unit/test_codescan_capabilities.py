"""Module naming, which decides whether cross-module lookups find anything.

A wrong name here fails invisibly: the scanner resolves fewer symbols and
reports fewer findings, which reads exactly like a clean repository.
"""

from pathlib import Path

from lup.codescan.common import module_name
from lup.devtools.dev.antipatterns import scanned_roots
from lup.devtools.project import DevProject


def test_a_package_under_a_distribution_directory_resolves_to_the_package() -> None:
    """`packages/lup` is the distribution; the second `lup` is the package.

    Taking the first match named the distribution directory, so every module
    in the library resolved to `lup.src.lup.*` and matched nothing.
    """
    assert module_name(Path("packages/lup/src/lup/resolver/core.py")) == (
        "lup.resolver.core"
    )


def test_an_application_module_resolves_from_its_own_root() -> None:
    """The library knows only its own root; the application supplies the rest."""
    application = DevProject(package="lup_template")
    assert module_name(
        Path("src/lup_template/devtools/app.py"), scanned_roots(application)
    ) == ("lup_template.devtools.app")
    assert module_name(Path("src/lup_template/devtools/app.py")) == (
        "src.lup_template.devtools.app"
    )


def test_a_package_init_names_the_package_itself() -> None:
    assert module_name(Path("packages/lup/src/lup/channels/__init__.py")) == (
        "lup.channels"
    )


def test_a_path_naming_no_package_root_is_taken_whole() -> None:
    assert module_name(Path("tests/unit/test_thing.py")) == "tests.unit.test_thing"
