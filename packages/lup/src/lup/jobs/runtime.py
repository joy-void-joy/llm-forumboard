"""Durable containerized jobs with filesystem-backed state and artifacts.

A sandbox cell runs inside the caller's own process: the agent waits, and if
the process dies the work dies with it. Some work does not fit that shape — a
search that runs for hours, a computation an agent wants to start and come
back to. A job is that work: submitted, left running, and asked about later,
possibly by a different process than the one that queued it.

Everything durable is on the filesystem rather than in memory, because "a
different process" includes one started after a crash. The scheduler's own
view of a job is one file it replaces atomically; the runner's terminal result
is another, written by the container itself. The two are separate on purpose —
the scheduler never writes into the directory the job can write to, so a job
cannot forge its own completion.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import docker
from docker.errors import APIError, DockerException, NotFound
from pydantic import BaseModel, Field

from lup.sandbox.container import Sandbox
from lup.sandbox.models import NetworkMode

logger = logging.getLogger(__name__)

# Runs as pid 1 inside the job container. It writes its result to a temporary
# name and renames it, so a reader either sees a complete terminal artifact or
# none — never a half-written one it would read as authoritative.
# lup: ignore[constant-declaration] — the runner program this library ships
JOB_RUNNER_SCRIPT = """\
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

input_dir = Path("/job/input")
output_dir = Path("/job/output")
spec = json.loads((input_dir / "spec.json").read_text(encoding="utf-8"))
started_at = datetime.now().astimezone().isoformat()
stdout_path = output_dir / "stdout.txt"
stderr_path = output_dir / "stderr.txt"
install_path = output_dir / "install.txt"
packages = spec["packages"]
install_exit_code = 0
if packages:
    installed = subprocess.run(
        ["uv", "pip", "install", "--system", *packages],
        capture_output=True,
        text=True,
        check=False,
    )
    install_exit_code = installed.returncode
    install_path.write_text(
        installed.stdout + installed.stderr,
        encoding="utf-8",
    )
if install_exit_code == 0:
    with stdout_path.open("w", encoding="utf-8") as stdout:
        with stderr_path.open("w", encoding="utf-8") as stderr:
            completed = subprocess.run(
                [sys.executable, str(input_dir / "job.py")],
                cwd="/workspace",
                stdout=stdout,
                stderr=stderr,
                check=False,
            )
    exit_code = completed.returncode
else:
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text(
        "Package installation failed; see install.txt.",
        encoding="utf-8",
    )
    exit_code = install_exit_code
result = {
    "exit_code": exit_code,
    "started_at": started_at,
    "finished_at": datetime.now().astimezone().isoformat(),
    "stdout_path": stdout_path.name,
    "stderr_path": stderr_path.name,
    "install_path": install_path.name if packages else None,
}
temporary = output_dir / ".result.json.tmp"
temporary.write_text(json.dumps(result), encoding="utf-8")
temporary.replace(output_dir / "result.json")
raise SystemExit(exit_code)
"""

type JobState = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class JobSpec(BaseModel, frozen=True):
    """One isolated request to run code out of process."""

    code: str = Field(min_length=1)
    packages: list[str] = []
    correlation_id: str | None = Field(
        default=None,
        description=(
            "A caller's own identifier for whatever this job belongs to, "
            "carried through onto the record so a result can be matched back"
        ),
    )


class JobRecord(BaseModel, frozen=True):
    """Scheduler metadata, stored outside the paths the job itself can write."""

    job_id: str
    state: JobState
    submitted_at: datetime
    updated_at: datetime
    container_name: str
    correlation_id: str | None = None
    message: str = ""
    exit_code: int | None = None

    @property
    def settled(self) -> bool:
        """Whether this job will never change state again."""
        return self.state in {"succeeded", "failed", "cancelled"}


class JobExecutionResult(BaseModel, frozen=True):
    """Terminal artifact index, written atomically by the job runner."""

    exit_code: int
    started_at: datetime
    finished_at: datetime
    stdout_path: Path
    stderr_path: Path
    install_path: Path | None = None


class JobOutput(BaseModel, frozen=True):
    """Full terminal output, returned on explicit collection."""

    record: JobRecord
    stdout: str
    stderr: str
    install_output: str | None = None


class DockerJobConfig(BaseModel, frozen=True):
    """Docker and storage configuration for one job backend.

    The stricter defaults are deliberate and differ from a plain sandbox's:
    this runs generated code unattended, with nobody watching the output, so
    the containment worth having interactively is worth insisting on here.
    Both remain a caller's to override.
    """

    root: Path
    docker_image: str = Sandbox.DEFAULT_DOCKER_IMAGE
    network_mode: NetworkMode = "filtered"
    egress_proxy_image: str = Sandbox.DEFAULT_EGRESS_PROXY_IMAGE
    require_rootless: bool = True


class DockerContainerState(BaseModel):
    """The terminal part of a Docker inspect state."""

    status: str = Field(validation_alias="Status")
    exit_code: int = Field(default=-1, validation_alias="ExitCode")


class DockerContainerInspect(BaseModel):
    """The part of a Docker container inspection this backend reads."""

    state: DockerContainerState = Field(validation_alias="State")


class JobStore:
    """One file per job, replaced atomically so any reader sees a whole one."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def directory(self, job_id: str) -> Path:
        """The controlled root for one opaque job identifier.

        Validated rather than trusted, because a job id reaching here from a
        tool call would otherwise be able to name a path outside the store.
        """
        if not job_id or "/" in job_id or job_id in {".", ".."}:
            raise ValueError(f"invalid job id {job_id!r}")
        return self.root / job_id

    def record_path(self, job_id: str) -> Path:
        return self.directory(job_id) / "record.json"

    def result_path(self, job_id: str) -> Path:
        return self.directory(job_id) / "output" / "result.json"

    def write_record(self, record: JobRecord) -> None:
        """Atomically replace the scheduler's own view of one job."""
        destination = self.record_path(record.job_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(destination)  # lup: ignore[string-replace] — atomic rename

    def read_record(self, job_id: str) -> JobRecord:
        """Read one required scheduler record."""
        path = self.record_path(job_id)
        if not path.exists():
            raise KeyError(f"unknown job {job_id!r}")
        return JobRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def read_execution(self, job_id: str) -> JobExecutionResult | None:
        """Read the runner's terminal result, once its atomic marker exists."""
        path = self.result_path(job_id)
        if not path.exists():
            return None
        return JobExecutionResult.model_validate_json(path.read_text(encoding="utf-8"))

    def list_records(self) -> list[JobRecord]:
        """Every durable record, newest first."""
        if not self.root.exists():
            return []
        return sorted(
            (
                JobRecord.model_validate_json(path.read_text(encoding="utf-8"))
                for path in self.root.glob("*/record.json")
            ),
            key=lambda record: record.submitted_at,
            reverse=True,
        )


class DockerJobBackend:
    """Submit, inspect, collect, and cancel durable jobs."""

    JOB_LABEL = "lup.job.id"

    def __init__(self, config: DockerJobConfig) -> None:
        self.config = config
        self.store = JobStore(config.root)

    def sandbox(self, job_id: str) -> Sandbox:
        """Rebuild one job's infrastructure names from its identifier alone.

        Derived rather than remembered, so a later process — which holds the
        job id and nothing else — addresses the same container and network.
        """
        return Sandbox(
            session_id=f"job-{job_id}",
            shared_dir=self.store.directory(job_id) / "output",
            docker_image=self.config.docker_image,
            network_mode=self.config.network_mode,
            egress_proxy_image=self.config.egress_proxy_image,
            require_rootless=self.config.require_rootless,
            durable=True,
            pre_install=None,
        )

    def submit(self, spec: JobSpec) -> JobRecord:
        """Persist the inputs, launch the container, and return immediately."""
        job_id = uuid4().hex
        directory = self.store.directory(job_id)
        input_dir = directory / "input"
        output_dir = directory / "output"
        input_dir.mkdir(parents=True)
        output_dir.mkdir()
        (input_dir / "job.py").write_text(spec.code, encoding="utf-8")
        (input_dir / "spec.json").write_text(
            spec.model_dump_json(exclude={"code"}), encoding="utf-8"
        )
        (input_dir / "runner.py").write_text(JOB_RUNNER_SCRIPT, encoding="utf-8")
        now = datetime.now().astimezone()
        sandbox = self.sandbox(job_id)
        # Recorded before the container exists, so a crash between the two
        # leaves a job that can be found and cleaned rather than an orphan.
        record = JobRecord(
            job_id=job_id,
            state="queued",
            submitted_at=now,
            updated_at=now,
            container_name=sandbox.container_name,
            correlation_id=spec.correlation_id,
        )
        self.store.write_record(record)
        client = docker.from_env()
        sandbox.docker_client = client
        try:
            self.launch(sandbox, job_id, input_dir, output_dir)
        except (APIError, DockerException, OSError, RuntimeError) as error:
            logger.exception("Failed to launch durable job %s", job_id)
            sandbox.destroy_container()
            self.store.write_record(
                record.model_copy(
                    update={
                        "state": "failed",
                        "updated_at": datetime.now().astimezone(),
                        "message": str(error),
                    }
                )
            )
            raise
        finally:
            client.close()
        running = record.model_copy(
            update={"state": "running", "updated_at": datetime.now().astimezone()}
        )
        self.store.write_record(running)
        return running

    def launch(
        self, sandbox: Sandbox, job_id: str, input_dir: Path, output_dir: Path
    ) -> None:
        """Start the job container, with its inputs mounted read-only."""
        if sandbox.docker_client is None:
            raise RuntimeError("Docker client not created")
        sandbox.verify_rootless_daemon()
        sandbox.remove_stale_container()
        sandbox.sweep_orphaned_containers()
        filtered = sandbox.network_mode == "filtered"
        if filtered:
            sandbox.start_filtered_egress()
        sandbox.docker_client.containers.run(
            sandbox.docker_image,
            name=sandbox.container_name,
            command=["python", "/job/input/runner.py"],
            detach=True,
            volumes={
                str(input_dir): {"bind": "/job/input", "mode": "ro"},
                str(output_dir): {"bind": "/job/output", "mode": "rw"},
                sandbox.volume_name: {"bind": "/workspace", "mode": "rw"},
            },
            working_dir="/workspace",
            mem_limit="1g",
            pids_limit=256,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            environment=sandbox.proxy_environment() if filtered else {},
            network_mode=sandbox.network_name if filtered else sandbox.network_mode,
            labels=sandbox.infrastructure_labels()
            | {sandbox.VOLUME_LABEL: sandbox.volume_name, self.JOB_LABEL: job_id},
        )

    def status(self, job_id: str) -> JobRecord:
        """Refresh one job from its terminal artifact, collecting when it ends."""
        record = self.store.read_record(job_id)
        if record.settled:
            return record
        execution = self.store.read_execution(job_id)
        if execution is None:
            return self.status_without_artifact(record)
        terminal = record.model_copy(
            update={
                "state": "succeeded" if execution.exit_code == 0 else "failed",
                "updated_at": execution.finished_at,
                "exit_code": execution.exit_code,
                "message": "job completed",
            }
        )
        self.store.write_record(terminal)
        self.cleanup(job_id)
        return terminal

    def status_without_artifact(self, record: JobRecord) -> JobRecord:
        """Decide a job whose runner left no result behind.

        A container that has exited without writing one did not finish: it was
        killed, or died before its own atomic rename. Reported as failed rather
        than left running forever, since nothing will write the artifact now.
        """
        client = docker.from_env()
        try:
            container = client.containers.get(record.container_name)
            container.reload()
            inspected = DockerContainerInspect.model_validate(container.attrs)
        except NotFound:
            inspected = DockerContainerInspect.model_validate(
                {"State": {"Status": "missing", "ExitCode": -1}}
            )
        except (APIError, DockerException):
            logger.exception("Could not inspect running job %s", record.job_id)
            return record
        finally:
            client.close()
        if inspected.state.status not in {"exited", "dead", "missing"}:
            return record
        failed = record.model_copy(
            update={
                "state": "failed",
                "updated_at": datetime.now().astimezone(),
                "exit_code": inspected.state.exit_code,
                "message": (
                    "job container exited without a terminal artifact "
                    f"({inspected.state.status})"
                ),
            }
        )
        self.store.write_record(failed)
        self.cleanup(record.job_id)
        return failed

    def list(self) -> list[JobRecord]:
        """Refresh and list every job, newest first."""
        return [self.status(record.job_id) for record in self.store.list_records()]

    def result(self, job_id: str) -> JobOutput:
        """The full terminal stdout, stderr, and package-install output."""
        record = self.status(job_id)
        if record.state not in {"succeeded", "failed"}:
            raise RuntimeError(f"job {job_id} is {record.state}")
        execution = self.store.read_execution(job_id)
        if execution is None:
            raise RuntimeError(f"job {job_id} has no terminal artifact")
        output_dir = self.store.directory(job_id) / "output"
        # Rejoined by name against the controlled directory, so a runner that
        # reported an absolute path cannot make this read somewhere else.
        install_path = (
            output_dir / execution.install_path.name
            if execution.install_path is not None
            else None
        )
        return JobOutput(
            record=record,
            stdout=(output_dir / execution.stdout_path.name).read_text(
                encoding="utf-8"
            ),
            stderr=(output_dir / execution.stderr_path.name).read_text(
                encoding="utf-8"
            ),
            install_output=(
                install_path.read_text(encoding="utf-8")
                if install_path is not None
                else None
            ),
        )

    def cancel(self, job_id: str) -> JobRecord:
        """Force-stop one running job, keeping whatever it already produced."""
        record = self.store.read_record(job_id)
        if record.settled:
            return record
        self.cleanup(job_id)
        cancelled = record.model_copy(
            update={
                "state": "cancelled",
                "updated_at": datetime.now().astimezone(),
                "message": "cancelled by request",
            }
        )
        self.store.write_record(cancelled)
        return cancelled

    def cleanup(self, job_id: str) -> None:
        """Remove one job's Docker resources, retaining its host artifacts."""
        sandbox = self.sandbox(job_id)
        client = docker.from_env()
        sandbox.docker_client = client
        try:
            sandbox.remove_stale_container()
            sandbox.destroy_container()
        except (APIError, DockerException) as error:
            logger.warning("Job %s infrastructure cleanup failed: %s", job_id, error)
        finally:
            client.close()
