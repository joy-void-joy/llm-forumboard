"""Translating a path across the sandbox boundary, in both directions.

These build a topology directly or from a Sandbox that never starts Docker,
so they run in the unit suite. What is under test is the partiality: most
paths on either side have no counterpart, and saying so precisely is the
whole point of the surface.
"""

import traceback
from pathlib import Path

import pytest
from pydantic import ValidationError

from lup.sandbox.container import Sandbox, file_cell
from lup.sandbox.repl_server import run_cell
from lup.sandbox.models import ExecuteCodeInput, Mount, PathNotMountedError
from lup.sandbox.translation import MountTopology


def topology_of(sandbox: Sandbox) -> MountTopology:
    return MountTopology(mounts=sandbox.mount_topology())


def make_sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(session_id="translate-test", shared_dir=tmp_path / "shared")


class TestContainerToHost:
    """What the host calls a path the container named."""

    def test_a_shared_path_resolves_to_the_bound_host_directory(
        self, tmp_path: Path
    ) -> None:
        crossing = topology_of(make_sandbox(tmp_path)).to_host("/shared/out.csv")

        assert crossing.resolved == str((tmp_path / "shared").resolve() / "out.csv")
        assert crossing.writable

    def test_the_bare_mount_point_resolves_to_the_host_directory(
        self, tmp_path: Path
    ) -> None:
        crossing = topology_of(make_sandbox(tmp_path)).to_host("/shared")

        assert crossing.resolved == str((tmp_path / "shared").resolve())

    def test_a_workspace_path_has_no_host_name(self, tmp_path: Path) -> None:
        """The case that must not be papered over: a Docker volume is bytes
        the host cannot reach by any path, so inventing one would send a
        reader to a directory that does not exist."""
        crossing = topology_of(make_sandbox(tmp_path)).to_host("/workspace/scratch.db")

        assert crossing.resolved is None
        assert not crossing.writable

    def test_an_unreachable_path_is_told_where_it_could_have_written(
        self, tmp_path: Path
    ) -> None:
        crossing = topology_of(make_sandbox(tmp_path)).to_host("/workspace/scratch.db")

        assert "/shared" in crossing.explanation

    def test_a_sibling_of_a_mount_point_does_not_match_it(self, tmp_path: Path) -> None:
        """``/sharedx`` starts with ``/shared`` as characters and shares no
        directory with it, so matching on the string would resolve a path
        that is nowhere near the mount."""
        crossing = topology_of(make_sandbox(tmp_path)).to_host("/sharedx/out.csv")

        assert crossing.resolved is None

    def test_the_most_specific_mount_wins(self, tmp_path: Path) -> None:
        """A source root nested under another mount would otherwise resolve
        through the shallower one, into a host directory not holding the file."""
        sandbox = Sandbox(
            session_id="nested",
            shared_dir=tmp_path / "shared",
            rw_mounts={tmp_path / "outer": "/data", tmp_path / "inner": "/data/inner"},
        )
        crossing = topology_of(sandbox).to_host("/data/inner/file.txt")

        assert crossing.resolved == str((tmp_path / "inner").resolve() / "file.txt")

    def test_a_read_only_root_reports_that_it_is_not_writable(
        self, tmp_path: Path
    ) -> None:
        """The crossing exists and the host filesystem would permit the write:
        the mount's mode is the only record that it was meant to be refused."""
        sandbox = Sandbox(
            session_id="ro",
            shared_dir=tmp_path / "shared",
            source_roots={"lup": tmp_path / "lup"},
        )
        crossing = topology_of(sandbox).to_host("/sources/lup/mod.py")

        assert crossing.resolved == str((tmp_path / "lup").resolve() / "mod.py")
        assert not crossing.writable


class TestHostToContainer:
    """What the container calls a path the host named."""

    def test_a_host_path_under_the_shared_dir_resolves(self, tmp_path: Path) -> None:
        shared = (tmp_path / "shared").resolve()
        crossing = topology_of(make_sandbox(tmp_path)).to_container(
            str(shared / "in.json")
        )

        assert crossing.resolved == "/shared/in.json"

    def test_an_unmounted_host_path_has_no_container_name(self, tmp_path: Path) -> None:
        crossing = topology_of(make_sandbox(tmp_path)).to_container(
            str(tmp_path / "elsewhere" / "secret.env")
        )

        assert crossing.resolved is None
        assert "not mounted" in crossing.explanation

    def test_a_source_root_resolves_read_only(self, tmp_path: Path) -> None:
        sandbox = Sandbox(
            session_id="ro",
            shared_dir=tmp_path / "shared",
            source_roots={"lup": tmp_path / "lup"},
        )
        crossing = topology_of(sandbox).to_container(
            str((tmp_path / "lup").resolve() / "mod.py")
        )

        assert crossing.resolved == "/sources/lup/mod.py"
        assert not crossing.writable

    def test_a_path_crosses_back_to_where_it_started(self, tmp_path: Path) -> None:
        topology = topology_of(make_sandbox(tmp_path))
        out = topology.to_host("/shared/nested/out.csv")

        assert out.resolved is not None
        assert topology.to_container(out.resolved).resolved == "/shared/nested/out.csv"


class TestUnifiedSpelling:
    """Mounting the exchange at its own host path removes the question."""

    def test_a_path_translates_to_itself(self, tmp_path: Path) -> None:
        shared = (tmp_path / "shared").resolve()
        sandbox = Sandbox(
            session_id="unified", shared_dir=shared, shared_path=str(shared)
        )
        topology = topology_of(sandbox)
        named = str(shared / "out.csv")

        assert topology.to_host(named).resolved == named
        assert topology.to_container(named).resolved == named

    def test_the_default_spelling_still_answers(self, tmp_path: Path) -> None:
        """Code the sandbox runs carries paths too, and reaches no boundary
        where a stale spelling could be corrected — so both stay true."""
        shared = (tmp_path / "shared").resolve()
        sandbox = Sandbox(
            session_id="unified", shared_dir=shared, shared_path=str(shared)
        )
        topology = topology_of(sandbox)

        assert (
            topology.to_host("/shared/out.csv").resolved
            == topology.to_host(str(shared / "out.csv")).resolved
            == str(shared / "out.csv")
        )

    def test_a_default_sandbox_gains_no_second_mount(self, tmp_path: Path) -> None:
        paths = [m.container_path for m in make_sandbox(tmp_path).mount_topology()]

        assert paths.count("/shared") == 1

    def test_the_default_keeps_the_two_spellings_apart(self, tmp_path: Path) -> None:
        """Unifying is the caller's choice, not something taken from them."""
        topology = topology_of(make_sandbox(tmp_path))

        assert topology.to_host("/shared/out.csv") != "/shared/out.csv"

    def test_a_unified_sandbox_runs_a_file_named_either_way(
        self, tmp_path: Path
    ) -> None:
        shared = (tmp_path / "shared").resolve()
        sandbox = Sandbox(
            session_id="unified", shared_dir=shared, shared_path=str(shared)
        )

        assert sandbox.container_spelling(str(shared / "job.py")) == str(
            shared / "job.py"
        )


class TestExchanges:
    """Where an agent that asked for an impossible crossing should go."""

    def test_only_writable_binds_are_offered(self, tmp_path: Path) -> None:
        sandbox = Sandbox(
            session_id="mixed",
            shared_dir=tmp_path / "shared",
            source_roots={"lup": tmp_path / "lup"},
        )

        assert topology_of(sandbox).exchanges() == ["/shared"]

    def test_a_volume_is_never_offered_as_an_exchange(self, tmp_path: Path) -> None:
        """``/workspace`` is writable and persistent, and reaches the host
        nowhere — which is exactly the confusion this list exists to end."""
        assert "/workspace" not in topology_of(make_sandbox(tmp_path)).exchanges()


class TestFileForm:
    """A cell named as a file, in whichever spelling the caller holds."""

    def test_a_host_path_is_translated_for_the_container(self, tmp_path: Path) -> None:
        shared = (tmp_path / "shared").resolve()
        sandbox = make_sandbox(tmp_path)

        assert sandbox.container_spelling(str(shared / "job.py")) == "/shared/job.py"

    def test_a_container_path_is_left_alone(self, tmp_path: Path) -> None:
        sandbox = make_sandbox(tmp_path)

        assert sandbox.container_spelling("/shared/job.py") == "/shared/job.py"

    def test_a_workspace_path_is_runnable_though_it_has_no_host_name(
        self, tmp_path: Path
    ) -> None:
        """The volume is unreachable from the host and perfectly ordinary
        inside the container, so refusing it would deny a valid path."""
        sandbox = make_sandbox(tmp_path)

        assert sandbox.container_spelling("/workspace/job.py") == "/workspace/job.py"

    def test_an_unmounted_host_path_is_refused_rather_than_copied_in(
        self, tmp_path: Path
    ) -> None:
        """Isolation must not depend on whether a cell arrived as text or as
        a file: an unmounted file is exactly what the container cannot see."""
        sandbox = make_sandbox(tmp_path)

        with pytest.raises(PathNotMountedError) as refusal:
            sandbox.container_spelling(str(tmp_path / "elsewhere" / "secret.env"))

        assert "/shared" in str(refusal.value)


class TestFileCell:
    """The cell a file is run through, checked against the real REPL.

    ``run_cell`` is the same pure-stdlib function that runs inside the
    container, so these hold the actual execution semantics to account
    without needing a Docker daemon.
    """

    def test_a_file_defines_its_names_into_the_session(self, tmp_path: Path) -> None:
        """The promise that makes the file form worth having: a script leaves
        the session in the state a caller's next cell expects."""
        script = tmp_path / "job.py"
        script.write_text("answer = 6 * 7\n", encoding="utf-8")
        namespace: dict[str, object] = {}

        run_cell(file_cell(str(script)), namespace)

        assert namespace["answer"] == 42

    def test_a_file_sees_names_the_session_already_holds(self, tmp_path: Path) -> None:
        script = tmp_path / "job.py"
        script.write_text("doubled = seed * 2\n", encoding="utf-8")
        namespace: dict[str, object] = {"seed": 21}

        run_cell(file_cell(str(script)), namespace)

        assert namespace["doubled"] == 42

    def test_a_traceback_names_the_file_rather_than_the_cell(
        self, tmp_path: Path
    ) -> None:
        script = tmp_path / "job.py"
        script.write_text("raise ValueError('boom')\n", encoding="utf-8")

        with pytest.raises(ValueError) as failure:
            run_cell(file_cell(str(script)), {})

        rendered = "".join(traceback.format_exception(failure.value))
        assert str(script) in rendered
        assert "boom" in str(failure.value)

    def test_a_quote_in_the_path_stays_an_argument(self, tmp_path: Path) -> None:
        """Embedding the path as a literal is what keeps a hostile name from
        becoming syntax; pasting it would end the string and run the rest."""
        odd = tmp_path / "it's a job.py"
        odd.write_text("marker = 1\n", encoding="utf-8")
        namespace: dict[str, object] = {}

        run_cell(file_cell(str(odd)), namespace)

        assert namespace["marker"] == 1


class TestExecuteCodeInput:
    """Exactly one of the two ways to name a cell."""

    def test_inline_code_alone_is_accepted(self) -> None:
        assert ExecuteCodeInput(code="1 + 1").file is None

    def test_a_file_alone_is_accepted(self) -> None:
        assert ExecuteCodeInput(file="/shared/job.py").code is None

    def test_both_together_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            ExecuteCodeInput(code="1 + 1", file="/shared/job.py")

    def test_neither_is_refused(self) -> None:
        """The shape that would otherwise run an empty cell and report success."""
        with pytest.raises(ValidationError):
            ExecuteCodeInput()


class TestIndependenceFromDocker:
    """The permission dispatcher runs as a standalone script."""

    def test_a_topology_round_trips_through_serialization(self) -> None:
        """A hook receives the table as data, not as a live Sandbox."""
        topology = MountTopology(
            mounts=[
                Mount(
                    container_path="/shared",
                    source="/host/shared",
                    kind="bind",
                    mode="rw",
                    purpose="exchange",
                )
            ]
        )
        revived = MountTopology.model_validate_json(topology.model_dump_json())

        assert revived.to_host("/shared/a.txt").resolved == "/host/shared/a.txt"
