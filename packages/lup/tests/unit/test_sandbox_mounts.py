"""Sandbox mount topology is the single source for volumes and tool docs.

These construct a Sandbox but never start Docker, so they run in the unit
suite: mount_topology() and the tool description are derived from names set
in __init__.
"""

from pathlib import Path

from lup.sandbox.container import Sandbox
from lup.sandbox.models import NetworkMode


def make_sandbox(tmp_path: Path, network_mode: NetworkMode = "bridge") -> Sandbox:
    return Sandbox(
        session_id="mounts-test",
        shared_dir=tmp_path / "shared",
        network_mode=network_mode,
    )


class TestMountTopology:
    """The topology names both container paths and where they come from."""

    def test_shared_binds_the_host_dir_read_write(self, tmp_path: Path) -> None:
        sandbox = make_sandbox(tmp_path)
        by_path = {m.container_path: m for m in sandbox.mount_topology()}

        shared = by_path["/shared"]
        assert shared.kind == "bind"
        assert shared.mode == "rw"
        assert shared.source == str((tmp_path / "shared").resolve())

    def test_workspace_is_a_persistent_volume(self, tmp_path: Path) -> None:
        sandbox = make_sandbox(tmp_path)
        by_path = {m.container_path: m for m in sandbox.mount_topology()}

        workspace = by_path["/workspace"]
        assert workspace.kind == "volume"
        assert workspace.source == sandbox.volume_name

    def test_topology_drives_the_docker_mount_specification(
        self, tmp_path: Path
    ) -> None:
        """The specification start_container passes must round-trip the
        topology, so a bind dropped there surfaces as a test failure here."""
        sandbox = make_sandbox(tmp_path)
        by_target = {m["Target"]: m for m in sandbox.docker_mounts()}

        assert by_target["/shared"]["Source"] == str((tmp_path / "shared").resolve())
        assert by_target["/shared"]["Type"] == "bind"
        assert not by_target["/shared"]["ReadOnly"]
        assert by_target["/workspace"]["Source"] == sandbox.volume_name
        assert by_target["/workspace"]["Type"] == "volume"

    def test_a_read_only_root_reaches_docker_as_read_only(self, tmp_path: Path) -> None:
        sandbox = Sandbox(
            session_id="ro",
            shared_dir=tmp_path / "shared",
            source_roots={"lup": tmp_path / "lup"},
        )
        by_target = {m["Target"]: m for m in sandbox.docker_mounts()}

        assert by_target["/sources/lup"]["ReadOnly"]

    def test_one_directory_at_two_paths_keeps_both(self, tmp_path: Path) -> None:
        """The mapping Docker also accepts is keyed by host path, so it would
        keep whichever of these came last and say nothing about the other."""
        shared = (tmp_path / "shared").resolve()
        sandbox = Sandbox(session_id="both", shared_dir=shared, shared_path=str(shared))
        targets = [m["Target"] for m in sandbox.docker_mounts()]

        assert str(shared) in targets
        assert "/shared" in targets
        assert [m["Source"] for m in sandbox.docker_mounts()].count(str(shared)) == 2


class TestDeclaredMounts:
    """A caller's own mounts land at the container path the caller named."""

    def test_a_read_only_mount_goes_where_it_was_asked_to(self, tmp_path: Path) -> None:
        notes = tmp_path / "notes"
        sandbox = Sandbox(
            session_id="declared",
            shared_dir=tmp_path / "shared",
            read_only_mounts={notes: "/notes"},
        )
        by_path = {mount.container_path: mount for mount in sandbox.mount_topology()}

        assert by_path["/notes"].source == str(notes.resolve())
        assert by_path["/notes"].mode == "ro"
        assert by_path["/notes"].kind == "bind"

    def test_a_writable_mount_can_keep_its_host_path(self, tmp_path: Path) -> None:
        """The case the parameter exists for: one path names the file on
        both sides, so a prompt naming it stays true inside the container."""
        out = tmp_path / "out"
        sandbox = Sandbox(
            session_id="declared",
            shared_dir=tmp_path / "shared",
            rw_mounts={out: str(out)},
        )
        by_path = {mount.container_path: mount for mount in sandbox.mount_topology()}

        assert by_path[str(out)].source == str(out.resolve())
        assert by_path[str(out)].mode == "rw"

    def test_declared_mounts_reach_the_docker_volume_mapping(
        self, tmp_path: Path
    ) -> None:
        notes = tmp_path / "notes"
        sandbox = Sandbox(
            session_id="declared",
            shared_dir=tmp_path / "shared",
            read_only_mounts={notes: "/notes"},
        )
        volumes = {
            mount.source: {"bind": mount.container_path, "mode": mount.mode}
            for mount in sandbox.mount_topology()
        }

        assert volumes[str(notes.resolve())] == {"bind": "/notes", "mode": "ro"}

    def test_a_sandbox_declaring_none_mounts_only_its_own(self, tmp_path: Path) -> None:
        paths = {
            mount.container_path for mount in make_sandbox(tmp_path).mount_topology()
        }

        assert paths == {"/workspace", "/shared"}


class TestExecuteCodeDescription:
    """The tool description must name the mounted paths so the in-sandbox
    agent never has to guess where host files live."""

    def test_description_names_both_mounts_and_host_dir(self, tmp_path: Path) -> None:
        sandbox = make_sandbox(tmp_path)
        execute_code = next(
            t for t in sandbox.create_tools() if t.name == "execute_code"
        )

        assert "/shared" in execute_code.description
        assert "/workspace" in execute_code.description
        assert str((tmp_path / "shared").resolve()) in execute_code.description

    def test_description_reflects_disabled_network(self, tmp_path: Path) -> None:
        sandbox = make_sandbox(tmp_path, network_mode="none")
        execute_code = next(
            t for t in sandbox.create_tools() if t.name == "execute_code"
        )

        assert "no network access" in execute_code.description

    def test_a_usage_note_reaches_the_agent_reading_the_tool(
        self, tmp_path: Path
    ) -> None:
        """What the mounted files are *for* is the caller's to say."""
        execute_code = next(
            t
            for t in make_sandbox(tmp_path).create_tools(
                usage_notes="The plan is at /notes/plan.json."
            )
            if t.name == "execute_code"
        )

        assert "The plan is at /notes/plan.json." in execute_code.description
