"""Integration tests for the persistent REPL sandbox.

Requires Docker to be running. Tests exercise Sandbox.run_code() and
Sandbox.run_install() directly — no LLM involved.
"""

from pathlib import Path

import pytest

from lup.sandbox.container import Sandbox
from lup.sandbox.models import PathNotMountedError

pytestmark = pytest.mark.integration


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    """Create a sandbox with pre-install disabled for speed."""
    return Sandbox(
        session_id="test-repl-integration",
        shared_dir=tmp_path / "shared",
        pre_install=None,
    )


class TestReplPersistence:
    """Variables and imports persist across execute_code calls."""

    def test_variable_persists(self, sandbox: Sandbox) -> None:
        with sandbox:
            r1 = sandbox.run_code("x = 42\nprint(x)")
            assert r1.exit_code == 0
            assert "42" in r1.stdout

            r2 = sandbox.run_code("print(x + 1)")
            assert r2.exit_code == 0
            assert "43" in r2.stdout

    def test_import_persists(self, sandbox: Sandbox) -> None:
        with sandbox:
            r1 = sandbox.run_code("import json")
            assert r1.exit_code == 0

            r2 = sandbox.run_code('print(json.dumps({"a": 1}))')
            assert r2.exit_code == 0
            assert '{"a": 1}' in r2.stdout

    def test_trailing_expression_echoes_across_the_wire(self, sandbox: Sandbox) -> None:
        """The echo survives the real container, not just the in-process server."""
        with sandbox:
            r1 = sandbox.run_code("x = 41\nx + 1")
            assert r1.result == "42"
            assert r1.stdout == ""

            r2 = sandbox.run_code("print('side effect')")
            assert r2.result is None
            assert r2.stdout == "side effect\n"

            # r2's expression was None, so it never displaced `_`.
            r3 = sandbox.run_code("_")
            assert r3.result == "42"

    def test_multiline_computation(self, sandbox: Sandbox) -> None:
        """Multi-step computation with numpy."""
        with sandbox:
            sandbox.run_install(["numpy"])
            r1 = sandbox.run_code(
                "import numpy as np\n"
                "M = np.random.default_rng(0).random((5, 5))\n"
                "print('shape:', M.shape)"
            )
            assert r1.exit_code == 0
            assert "(5, 5)" in r1.stdout

            r2 = sandbox.run_code(
                "eigvals = np.linalg.eigvals(M)\nprint('count:', len(eigvals))"
            )
            assert r2.exit_code == 0
            assert "count: 5" in r2.stdout

            r3 = sandbox.run_code(
                "trace = np.trace(M)\n"
                "eigsum = eigvals.sum().real\n"
                "print(f'match: {abs(trace - eigsum) < 1e-10}')"
            )
            assert r3.exit_code == 0
            assert "match: True" in r3.stdout


class TestSharedMount:
    """The documented /shared path actually exchanges files with the host."""

    def test_host_file_readable_at_shared_path(self, tmp_path: Path) -> None:
        """A file written to the host shared_dir is readable at /shared, the
        container path the tool description advertises."""
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "input.txt").write_text("from-host")

        sandbox = Sandbox(
            session_id="test-shared-mount", shared_dir=shared, pre_install=None
        )
        with sandbox:
            mounts = {m.container_path: m for m in sandbox.mount_topology()}
            assert mounts["/shared"].source == str(shared.resolve())

            result = sandbox.run_code("print(open('/shared/input.txt').read(), end='')")
            assert result.exit_code == 0
            assert result.stdout == "from-host"

    def test_sandbox_write_visible_on_host(self, tmp_path: Path) -> None:
        """A file the sandbox writes under /shared appears on the host."""
        shared = tmp_path / "shared"
        shared.mkdir()

        sandbox = Sandbox(
            session_id="test-shared-write", shared_dir=shared, pre_install=None
        )
        with sandbox:
            result = sandbox.run_code(
                "open('/shared/output.txt', 'w').write('from-sandbox')"
            )
            assert result.exit_code == 0

        assert (shared / "output.txt").read_text() == "from-sandbox"


class TestFileForm:
    """A cell named as a file, run against a real container."""

    def test_a_file_named_by_its_host_path_runs_and_persists(
        self, tmp_path: Path
    ) -> None:
        """The whole point of the form: the caller names the file the way the
        host does and the session keeps what the file defined."""
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "job.py").write_text("answer = 6 * 7\n")

        sandbox = Sandbox(
            session_id="test-file-form", shared_dir=shared, pre_install=None
        )
        with sandbox:
            ran = sandbox.run_file(str(shared / "job.py"))
            assert ran.exit_code == 0

            echoed = sandbox.run_code("answer")
            assert echoed.result == "42"

    def test_a_file_named_by_its_container_path_runs(self, tmp_path: Path) -> None:
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "job.py").write_text("print('ran')\n")

        sandbox = Sandbox(
            session_id="test-file-container", shared_dir=shared, pre_install=None
        )
        with sandbox:
            ran = sandbox.run_file("/shared/job.py")

            assert ran.exit_code == 0
            assert "ran" in ran.stdout

    def test_a_unified_mount_answers_to_one_spelling(self, tmp_path: Path) -> None:
        """With the exchange mounted at its own host path, the path the host
        wrote is the path the container opens — no translation involved."""
        shared = (tmp_path / "shared").resolve()
        shared.mkdir()
        (shared / "job.py").write_text("marker = 'unified'\n")

        sandbox = Sandbox(
            session_id="test-file-unified",
            shared_dir=shared,
            shared_path=str(shared),
            pre_install=None,
        )
        with sandbox:
            assert sandbox.run_file(str(shared / "job.py")).exit_code == 0
            assert sandbox.run_code("marker").result == "'unified'"

    def test_an_unmounted_file_is_refused(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "job.py").write_text("marker = 1\n")

        sandbox = Sandbox(
            session_id="test-file-refused",
            shared_dir=tmp_path / "shared",
            pre_install=None,
        )
        with sandbox, pytest.raises(PathNotMountedError):
            sandbox.run_file(str(outside / "job.py"))


class TestInstallPackage:
    """install_package makes packages available to the REPL."""

    def test_install_and_import(self, sandbox: Sandbox) -> None:
        """Install a package, then import it in the existing REPL."""
        with sandbox:
            r1 = sandbox.run_code(
                "data = [['Alice', 90], ['Bob', 85]]\nprint(len(data))"
            )
            assert r1.exit_code == 0
            assert "2" in r1.stdout

            r2 = sandbox.run_code("from tabulate import tabulate")
            assert r2.exit_code != 0, "tabulate should not be available yet"

            install = sandbox.run_install(["tabulate"])
            assert install.exit_code == 0

            r3 = sandbox.run_code(
                "from tabulate import tabulate\n"
                "print(tabulate(data, headers=['Name', 'Score']))"
            )
            assert r3.exit_code == 0
            assert "Alice" in r3.stdout

    def test_state_survives_error(self, sandbox: Sandbox) -> None:
        """Variables defined before an error still exist after it."""
        with sandbox:
            sandbox.run_code("keeper = 'still here'")

            r_err = sandbox.run_code("raise ValueError('boom')")
            assert r_err.exit_code != 0

            r_ok = sandbox.run_code("print(keeper)")
            assert r_ok.exit_code == 0
            assert "still here" in r_ok.stdout
