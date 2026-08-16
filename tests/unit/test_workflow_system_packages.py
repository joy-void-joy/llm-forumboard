"""System packages a generated gate installs before it runs.

A project whose code shells out to a binary needs that binary on the runner,
and no lock file reaches it. The failure without one is indirect — a test
asserting whatever the missing binary would have produced — so what is pinned
here is that declaring it renders a step, and that declaring nothing renders
none.
"""

from lup.devtools.dev.workflow import WorkflowSpec


def test_a_declared_package_reaches_the_generated_gate() -> None:
    rendered = WorkflowSpec(system_packages=["poppler-utils"]).body()

    assert "sudo apt-get install -y poppler-utils" in rendered


def test_several_packages_share_one_install() -> None:
    """One apt call, because two would pay the update cost twice."""
    rendered = WorkflowSpec(system_packages=["poppler-utils", "pandoc"]).body()

    assert "install -y poppler-utils pandoc" in rendered
    assert rendered.count("apt-get install") == 1


def test_declaring_none_renders_no_step() -> None:
    """The common case carries no apt call a reader would have to read past."""
    rendered = WorkflowSpec().body()

    assert "apt-get" not in rendered


def test_the_install_precedes_the_sync() -> None:
    """uv sync can build a package that needs the system library present."""
    rendered = WorkflowSpec(system_packages=["poppler-utils"]).body()

    assert rendered.index("apt-get") < rendered.index("uv sync")
