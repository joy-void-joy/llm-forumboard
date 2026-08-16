"""Running one ``py eval`` expression inside the sandbox container.

Split from :mod:`lup.devtools.py.app` because importing it means importing
:mod:`lup.sandbox`, which raises without the ``docker`` extra. The command
catches that import and says what to do instead, so a checkout without
Docker keeps every ``py`` subcommand that only reads code.
"""

from pathlib import Path

import tomlkit
from pydantic import BaseModel

from lup.devtools.py.evaluate import sandbox_program
from lup.sandbox.container import Sandbox
from lup.workspace.paths import project_root

# lup: ignore[constant-declaration] — the container's own session name, which a
# later invocation must spell alike to reuse the warm one
EVAL_SESSION = "py-eval"
"""One session name, so repeated expressions reuse a warm container."""


def import_roots(root: Path) -> dict[str, Path]:
    """The source directories a checkout offers the sandbox, if they exist.

    Only directories that hold importable source. The repository root holds
    its `.env` files too, and this sandbox reaches the network by default,
    so mounting the checkout whole would be a way out with the secrets.

    Named here rather than derived from the directory: both roots are called
    `src`, so the leaf name would have collided them onto one mount point.
    """
    candidates = {"library": root / "packages" / "lup" / "src", "project": root / "src"}
    return {name: path for name, path in candidates.items() if path.is_dir()}


class ProjectTable(BaseModel):
    """The one field this reads out of a distribution's `[project]` table."""

    dependencies: list[str] = []


class Manifest(BaseModel):
    """A `pyproject.toml` as far as this needs to understand one."""

    project: ProjectTable = ProjectTable()


def library_dependencies(root: Path) -> list[str]:
    """What the mounted library needs installed to import at all.

    Read off the library's own manifest rather than listed again here: the
    source arrives by mount, so nothing installs it and nothing would have
    resolved its dependencies. A second list would go stale the first time
    the library took a new one.
    """
    manifest = root / "packages" / "lup" / "pyproject.toml"
    if not manifest.is_file():
        return []
    parsed = Manifest.model_validate(
        tomlkit.parse(manifest.read_text(encoding="utf-8")).unwrap()
    )
    return parsed.project.dependencies


def evaluate_in_sandbox(expression: str, root: Path | None = None) -> str:
    """Evaluate one expression in the container and return what it printed."""
    checkout = root or project_root()
    with Sandbox(
        session_id=EVAL_SESSION,
        shared_dir=checkout / "tmp" / "py-eval",
        source_roots=import_roots(checkout),
        pre_install=library_dependencies(checkout),
    ) as sandbox:
        result = sandbox.run_code(sandbox_program(expression))
    if result.exit_code != 0:
        return result.stderr or result.stdout
    return result.stdout.removesuffix("\n")
