"""Render a saved FLP through FL Studio's documented command-line exporter.

This is a host process adapter, separate from the live MIDI bridge. The input
is an existing project on disk; unsaved edits in the open project are never
included. A new output directory belongs to each job. Success requires both
application exit and a fully decoded WAV, not merely delivery of a command.

Image-Line documents the export switches at:
https://www.image-line.com/fl-studio-learning/fl-studio-online-manual/html/fformats_save_export.htm
"""

from __future__ import annotations

import os
import platform
import re
import secrets
import struct
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Protocol

import numpy as np
import soundfile as sf
from pydantic import BaseModel, ConfigDict, Field


FL_STUDIO_PATH_ENV = "POSTFADER_FL_STUDIO_PATH"
MAX_RENDER_JOBS = 32
MAX_WAV_BYTES = 8 * 1024**3
RenderStatus = Literal[
    "queued", "rendering", "output_ready", "completed", "failed", "cancelled", "timed_out"
]
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timed_out"}


class SavedProjectRenderError(RuntimeError):
    """A saved-project render cannot be prepared or its job is unknown."""


class RenderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class SavedProjectRenderRequest(RenderModel):
    project_path: str = Field(min_length=1, max_length=4096)
    output_directory: str = Field(min_length=1, max_length=4096)
    fl_studio_path: str | None = Field(default=None, min_length=1, max_length=4096)
    timeout_seconds: float = Field(default=600.0, ge=1.0, le=7200.0)


class RenderedWav(RenderModel):
    path: str
    size_bytes: int = Field(gt=0)
    modified_ns: int = Field(ge=0)
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    frames: int = Field(gt=0)
    duration_seconds: float = Field(gt=0)
    subtype: str
    decoded_at: datetime
    fully_decoded: Literal[True] = True


class SavedProjectRenderJob(RenderModel):
    job_id: str
    status: RenderStatus
    project_path: str
    output_directory: str
    output_path: str
    platform: Literal["macos", "windows"]
    launch_method: Literal["macos_open_new_instance", "windows_executable"]
    command: list[str]
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested: bool = False
    application_exit_observed: bool = False
    process_return_code: int | None = None
    renderer_may_be_running: bool = False
    result: RenderedWav | None = None
    error: str | None = Field(default=None, max_length=2048)
    warnings: list[str] = Field(default_factory=list)
    includes_unsaved_changes: Literal[False] = False
    process_local: Literal[True] = True

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class RenderProcess(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


RenderLauncher = Callable[[list[str], Path], RenderProcess]


def _launch(command: list[str], directory: Path) -> RenderProcess:
    if os.environ.get("FL_BRIDGE_SANDBOXED") == "1":
        raise SavedProjectRenderError("FL launch is disabled in the offline test environment")
    return subprocess.Popen(
        command,
        cwd=directory,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(value) for value in re.findall(r"\d+", str(path)))


def resolve_fl_studio_path(
    configured: str | None = None, *, system: str | None = None
) -> tuple[Literal["macos", "windows"], Path]:
    """Find an installed app bundle/executable without launching it."""

    system = system or platform.system()
    if system not in {"Darwin", "Windows"}:
        raise SavedProjectRenderError("Saved-project rendering supports macOS and Windows")
    kind: Literal["macos", "windows"] = "macos" if system == "Darwin" else "windows"
    supplied = configured or os.environ.get(FL_STUDIO_PATH_ENV)
    if supplied:
        candidates = [Path(supplied).expanduser()]
    elif kind == "macos":
        candidates = sorted(
            [
                candidate
                for directory in (Path("/Applications"), Path.home() / "Applications")
                for candidate in directory.glob("FL Studio*.app")
            ],
            key=_version_key,
            reverse=True,
        )
    else:
        candidates = sorted(
            [
                executable
                for variable in ("ProgramFiles", "ProgramFiles(x86)")
                if os.environ.get(variable)
                for directory in (Path(os.environ[variable]) / "Image-Line").glob("FL Studio*")
                for name in ("FL64.exe", "FL.exe")
                for executable in [directory / name]
                if executable.is_file()
            ],
            key=lambda item: (_version_key(item.parent), item.name == "FL64.exe"),
            reverse=True,
        )
    for candidate in candidates:
        if not candidate.is_absolute():
            continue
        path = candidate.resolve()
        if kind == "macos":
            if path.suffix.lower() == ".app" and (path / "Contents" / "Info.plist").is_file():
                return kind, path
        elif path.suffix.lower() == ".exe" and path.is_file():
            return kind, path
    raise SavedProjectRenderError(
        "FL Studio was not found. Supply fl_studio_path as an absolute .app bundle "
        f"on macOS or .exe path on Windows, or set {FL_STUDIO_PATH_ENV}."
    )


def build_render_command(
    *,
    kind: Literal["macos", "windows"],
    application: Path,
    project: Path,
    output: Path,
) -> list[str]:
    """Build an argv vector; project paths never pass through a shell.

    The macOS single-project -F form follows Image-Line's documented example.
    ``open -n`` requests a fresh app instance, while ``-W`` waits for that app
    to close. Ending the open helper does not end the FL Studio application.
    """

    if kind == "macos":
        return [
            "/usr/bin/open", "-n", "-W", "-a", str(application), "--args",
            f"-R{output}", "-Ewav", f"-O{output.parent}", f"-F{project}",
        ]
    return [str(application), f"/R{output}", "/Ewav", f"/O{output.parent}", str(project)]


def _stat_signature(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
        return info.st_size, info.st_mtime_ns
    except FileNotFoundError:
        return None


def _complete_wave_container(path: Path) -> None:
    """Reject unfinished/truncated RIFF and RF64 files before decoding.

    libsndfile can silently shorten a truncated WAV's reported frame count.
    Checking the container's declared lengths prevents that shortened file
    from becoming a verified render merely because it can be opened.
    """

    size = path.stat().st_size
    if not 44 <= size <= MAX_WAV_BYTES:
        raise ValueError("WAV size is incomplete or exceeds the 8 GiB render bound")
    with path.open("rb") as stream:
        header = stream.read(12)
        if header[:4] not in {b"RIFF", b"RF64"} or header[8:12] != b"WAVE":
            raise ValueError("Output is not a RIFF/RF64 WAV")
        declared = struct.unpack("<I", header[4:8])[0]
        rf64 = header[:4] == b"RF64"
        rf64_data_size: int | None = None
        if not rf64 and declared + 8 != size:
            raise ValueError("WAV container length does not match the complete file")
        found_data = False
        found_format = False
        chunk_count = 0
        while stream.tell() < size:
            chunk_count += 1
            if chunk_count > 100000:
                raise ValueError("WAV has too many chunks")
            chunk = stream.read(8)
            if len(chunk) != 8:
                raise ValueError("WAV chunk header is incomplete")
            chunk_id, chunk_size = struct.unpack("<4sI", chunk)
            if chunk_id == b"ds64" and rf64:
                if chunk_size < 28:
                    raise ValueError("RF64 length metadata is incomplete")
                values = stream.read(28)
                if len(values) != 28:
                    raise ValueError("RF64 length metadata is truncated")
                riff_size, rf64_data_size, _samples, _table = struct.unpack("<QQQI", values)
                if riff_size + 8 != size:
                    raise ValueError("RF64 container length does not match the complete file")
                stream.seek(-28, 1)
            if chunk_id == b"data":
                found_data = True
                if rf64 and chunk_size == 0xFFFFFFFF:
                    if rf64_data_size is None:
                        raise ValueError("RF64 data lacks length metadata")
                    chunk_size = rf64_data_size
                if chunk_size == 0:
                    raise ValueError("WAV contains no rendered samples yet")
            if chunk_id == b"fmt ":
                found_format = True
            padded_size = chunk_size + (chunk_size % 2)
            if stream.tell() + padded_size > size:
                raise ValueError("WAV sample or metadata chunk is truncated")
            stream.seek(padded_size, 1)
        if not found_data or not found_format or (rf64 and rf64_data_size is None):
            raise ValueError("WAV is missing format, sample data, or RF64 lengths")


class _Cancelled(Exception):
    pass


def decode_rendered_wav(
    path: Path,
    *,
    cancel: threading.Event | None = None,
    deadline: float | None = None,
) -> RenderedWav:
    """Read every frame in bounded blocks and reject incomplete/nonfinite audio."""

    before = _stat_signature(path)
    _complete_wave_container(path)
    with sf.SoundFile(str(path)) as audio:
        if audio.frames <= 0 or audio.samplerate <= 0 or audio.channels <= 0:
            raise ValueError("WAV contains no readable audio")
        count = 0
        for block in audio.blocks(blocksize=65536, dtype="float32", always_2d=True):
            if cancel is not None and cancel.is_set():
                raise _Cancelled()
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Render verification exceeded the job deadline")
            if not np.isfinite(block).all():
                raise ValueError("WAV contains nonfinite audio samples")
            count += len(block)
        if count != audio.frames:
            raise ValueError("WAV decoder did not read every declared frame")
        after = _stat_signature(path)
        if before is None or before != after:
            raise ValueError("WAV changed while its samples were being decoded")
        return RenderedWav(
            path=str(path), size_bytes=before[0], modified_ns=before[1],
            sample_rate=audio.samplerate, channels=audio.channels,
            frames=count, duration_seconds=count / audio.samplerate,
            subtype=audio.subtype, decoded_at=_now(),
        )


@dataclass
class _Job:
    snapshot: SavedProjectRenderJob
    timeout: float
    cancel: threading.Event = field(default_factory=threading.Event)
    future: Future[None] | None = None


class SavedProjectRenderJobs:
    """One monitored job at a time, bounded history, asynchronous polling.

    The launcher seam allows deterministic tests without starting FL Studio.
    macOS cancellation stops this job's waiter and leaves its detached FL app
    alone. Windows cancellation terminates only this job's owned process.
    """

    def __init__(
        self,
        *,
        launcher: RenderLauncher = _launch,
        system: str | None = None,
        max_jobs: int = MAX_RENDER_JOBS,
        poll_seconds: float = 0.25,
        settle_seconds: float = 1.0,
    ) -> None:
        if not 1 <= max_jobs <= MAX_RENDER_JOBS:
            raise ValueError("max_jobs must be within 1..32")
        if not 0 < poll_seconds <= 5 or not 0 <= settle_seconds <= 30:
            raise ValueError("Invalid render polling interval")
        self._launcher = launcher
        self._system = system
        self._max_jobs = max_jobs
        self._poll_seconds = poll_seconds
        self._settle_seconds = settle_seconds
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="postfader-render")
        self._closed = False

    def start(self, request: SavedProjectRenderRequest) -> SavedProjectRenderJob:
        """Launch a render of an existing saved project into a fresh directory."""

        project = Path(request.project_path).expanduser()
        directory = Path(request.output_directory).expanduser()
        if not project.is_absolute() or not directory.is_absolute():
            raise SavedProjectRenderError("project_path and output_directory must be absolute")
        project = project.resolve()
        if project.suffix.lower() != ".flp" or not project.is_file():
            raise SavedProjectRenderError("project_path must name an existing saved .flp file")
        with project.open("rb") as stream:
            if stream.read(4) != b"FLhd":
                raise SavedProjectRenderError("The selected file does not have an FLP header")
        kind, application = resolve_fl_studio_path(request.fl_studio_path, system=self._system)
        with self._lock:
            if self._closed:
                raise SavedProjectRenderError("The render job manager is closed")
            active = next((job for job in self._jobs.values() if not job.snapshot.terminal), None)
            if active is not None:
                raise SavedProjectRenderError(f"A render is already active: {active.snapshot.job_id}")
            if len(self._jobs) >= self._max_jobs:
                self._jobs.pop(next(iter(self._jobs)))
            job_id = f"render-{secrets.token_hex(10)}"
            destination = directory.resolve() / f"{project.stem[:64]}-{job_id}"
            destination.mkdir(parents=True, exist_ok=False)
            output = destination / f"{project.stem[:64]}.wav"
            command = build_render_command(
                kind=kind, application=application, project=project, output=output
            )
            warnings = [
                "Renders the selected FLP as saved on disk; unsaved edits in the live project are not included.",
                "FL Studio's saved/default render settings determine quality and range; actual audio properties are reported after decoding.",
            ]
            if kind == "macos":
                warnings.append(
                    "A separate FL Studio app instance is requested. Cancellation stops monitoring; it does not close that app."
                )
            snapshot = SavedProjectRenderJob(
                job_id=job_id, status="queued", project_path=str(project),
                output_directory=str(destination), output_path=str(output),
                platform=kind,
                launch_method="macos_open_new_instance" if kind == "macos" else "windows_executable",
                command=command, created_at=_now(), warnings=warnings,
            )
            job = _Job(snapshot=snapshot, timeout=request.timeout_seconds)
            self._jobs[job_id] = job
            job.future = self._executor.submit(self._run, job)
            return snapshot.model_copy(deep=True)

    def _update(self, job: _Job, **changes: object) -> None:
        with self._lock:
            job.snapshot = job.snapshot.model_copy(update=changes)

    def status(self, job_id: str) -> SavedProjectRenderJob:
        with self._lock:
            try:
                return self._jobs[job_id].snapshot.model_copy(deep=True)
            except KeyError as exc:
                raise SavedProjectRenderError(f"Unknown render job: {job_id}") from exc

    def list(self) -> list[SavedProjectRenderJob]:
        with self._lock:
            return [job.snapshot.model_copy(deep=True) for job in self._jobs.values()]

    def cancel(self, job_id: str) -> SavedProjectRenderJob:
        with self._lock:
            snapshot = self.status(job_id)
            if snapshot.terminal:
                return snapshot
            job = self._jobs[job_id]
            job.cancel.set()
            self._update(job, cancel_requested=True)
            return self.status(job_id)

    def wait(self, job_id: str, *, timeout: float = 30.0) -> SavedProjectRenderJob:
        with self._lock:
            self.status(job_id)
            future = self._jobs[job_id].future
        if future is not None:
            future.result(timeout=timeout)
        return self.status(job_id)

    @staticmethod
    def _stop_owned_process(process: RenderProcess) -> bool:
        """Stop only the process launched by this job, never search/kill by name."""

        if process.poll() is not None:
            return True
        try:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
            return process.poll() is not None
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _run(self, job: _Job) -> None:
        process: RenderProcess | None = None
        deadline = time.monotonic() + job.timeout
        output = Path(job.snapshot.output_path)
        previous: tuple[int, int] | None = None
        stable_since = time.monotonic()
        last_decode_error: str | None = None
        try:
            if job.cancel.is_set():
                raise _Cancelled()
            self._update(job, status="rendering", started_at=_now())
            process = self._launcher(job.snapshot.command.copy(), Path(job.snapshot.project_path).parent)
            self._update(job, renderer_may_be_running=True)
            while True:
                if job.cancel.is_set():
                    raise _Cancelled()
                now = time.monotonic()
                if now >= deadline:
                    raise TimeoutError("FL Studio did not complete a verified render before the job deadline")
                return_code = process.poll()
                if return_code is not None:
                    # On macOS open -W succeeds only when the newly launched
                    # application exits. A nonzero launcher exit proves no
                    # such thing: the app may still exist.
                    exited = return_code == 0 or job.snapshot.platform == "windows"
                    self._update(job, process_return_code=return_code,
                                 application_exit_observed=exited,
                                 renderer_may_be_running=not exited)
                signature = _stat_signature(output)
                if signature != previous:
                    previous = signature
                    stable_since = now
                    last_decode_error = None
                    self._update(job, result=None, status="rendering")
                if signature is not None and now - stable_since >= self._settle_seconds:
                    if job.snapshot.result is None and last_decode_error is None:
                        try:
                            result = decode_rendered_wav(output, cancel=job.cancel, deadline=deadline)
                            self._update(job, result=result, status="output_ready")
                            last_decode_error = None
                        except (ValueError, OSError, sf.LibsndfileError) as exc:
                            last_decode_error = str(exc)[:1024]
                    if return_code == 0 and job.snapshot.result is not None:
                        self._update(job, status="completed", finished_at=_now())
                        return
                if return_code is not None:
                    if return_code != 0:
                        raise SavedProjectRenderError(f"FL Studio command exited with code {return_code}")
                    if signature is None:
                        raise SavedProjectRenderError("FL Studio exited without creating the requested WAV")
                    if now - stable_since >= self._settle_seconds and last_decode_error:
                        raise SavedProjectRenderError(f"FL Studio output could not be verified: {last_decode_error}")
                job.cancel.wait(self._poll_seconds)
        except _Cancelled:
            self._finish_stopped(job, process, "cancelled", None)
        except TimeoutError as exc:
            self._finish_stopped(job, process, "timed_out", str(exc))
        except Exception as exc:
            self._finish_stopped(job, process, "failed", f"{type(exc).__name__}: {exc}")

    def _finish_stopped(
        self,
        job: _Job,
        process: RenderProcess | None,
        status: Literal["cancelled", "timed_out", "failed"],
        error: str | None,
    ) -> None:
        detached = job.snapshot.platform == "macos"
        was_running = process is not None and process.poll() is None
        stopped = process is not None and self._stop_owned_process(process)
        # Terminating macOS's open waiter cannot establish that its FL app
        # stopped. A launch failure can also leave app completion unknown.
        may_run = process is not None and (
            (detached and not job.snapshot.application_exit_observed)
            or (was_running and not stopped)
        )
        exited = job.snapshot.application_exit_observed or (
            not detached and process is not None and stopped
        )
        result = job.snapshot.result
        if result is not None and _stat_signature(Path(result.path)) != (
            result.size_bytes, result.modified_ns
        ):
            result = None
        self._update(
            job, status=status, finished_at=_now(), error=error[:2048] if error else None,
            renderer_may_be_running=may_run, application_exit_observed=exited,
            process_return_code=process.poll() if process is not None else None,
            result=result,
        )

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
            for job in self._jobs.values():
                if not job.snapshot.terminal:
                    job.cancel.set()
                    self._update(job, cancel_requested=True)
        self._executor.shutdown(wait=wait)


_DEFAULT_JOBS: SavedProjectRenderJobs | None = None
_DEFAULT_JOBS_LOCK = threading.Lock()


def get_saved_project_render_jobs() -> SavedProjectRenderJobs:
    global _DEFAULT_JOBS
    with _DEFAULT_JOBS_LOCK:
        if _DEFAULT_JOBS is None:
            _DEFAULT_JOBS = SavedProjectRenderJobs()
        return _DEFAULT_JOBS


def shutdown_saved_project_render_jobs() -> None:
    """Release an existing manager at MCP shutdown without creating one."""

    global _DEFAULT_JOBS
    with _DEFAULT_JOBS_LOCK:
        manager = _DEFAULT_JOBS
        _DEFAULT_JOBS = None
    if manager is not None:
        manager.shutdown(wait=True)
