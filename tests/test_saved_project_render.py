"""Saved-FLP CLI rendering checks; every FL launch is a synthetic runner."""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fl_studio_mcp.saved_project_render import (  # noqa: E402
    _launch,
    FL_STUDIO_PATH_ENV,
    SavedProjectRenderError,
    SavedProjectRenderJobs,
    SavedProjectRenderRequest,
    build_render_command,
    decode_rendered_wav,
    resolve_fl_studio_path,
)


class FakeProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.terminated = 0
        self.killed = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated += 1
        self.returncode = -15

    def kill(self) -> None:
        self.killed += 1
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise AssertionError("synthetic process is still running")
        return self.returncode


def write_wav(path: Path, *, frames: int = 480) -> None:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        audio.writeframes(struct.pack("<hh", 1234, -1234) * frames)


def command_output(command: list[str]) -> Path:
    return Path(next(value[2:] for value in command if value.startswith(("/R", "-R"))))


class SavedProjectRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="postfader-render-test-")
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "Mix $(echo hello) & take's 1.flp"
        self.project.write_bytes(b"FLhd" + bytes(30))
        self.executable = self.root / "FL64.exe"
        self.executable.write_bytes(b"synthetic runner path, never executed")
        self.bundle = self.root / "FL Studio 2026.app"
        (self.bundle / "Contents").mkdir(parents=True)
        (self.bundle / "Contents" / "Info.plist").write_text("synthetic app, never opened")
        self.output_root = self.root / "renders"
        self.managers: list[SavedProjectRenderJobs] = []
        # Even an accidental missed injection must not launch a real process.
        self.popen_guard = patch(
            "fl_studio_mcp.saved_project_render.subprocess.Popen",
            side_effect=AssertionError("a safe test attempted a real subprocess"),
        )
        self.popen_guard.start()

    def tearDown(self) -> None:
        for manager in self.managers:
            manager.shutdown(wait=True)
        self.popen_guard.stop()
        self.temporary.cleanup()

    def request(self, *, mac: bool = False, timeout: float = 2.0) -> SavedProjectRenderRequest:
        return SavedProjectRenderRequest(
            project_path=str(self.project), output_directory=str(self.output_root),
            fl_studio_path=str(self.bundle if mac else self.executable),
            timeout_seconds=timeout,
        )

    def jobs(self, launcher, *, mac: bool = False, max_jobs: int = 32) -> SavedProjectRenderJobs:
        manager = SavedProjectRenderJobs(
            launcher=launcher, system="Darwin" if mac else "Windows",
            poll_seconds=0.005, settle_seconds=0.02, max_jobs=max_jobs,
        )
        self.managers.append(manager)
        return manager

    def until(self, manager: SavedProjectRenderJobs, identifier: str, status: str):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            snapshot = manager.status(identifier)
            if snapshot.status == status:
                return snapshot
            if snapshot.terminal:
                self.fail(f"Expected {status}, got {snapshot.status}: {snapshot.error}")
            time.sleep(0.005)
        self.fail(f"Timed out waiting for synthetic job to become {status}")

    def test_offline_environment_prevents_real_launcher_dispatch(self) -> None:
        with patch.dict(os.environ, {"FL_BRIDGE_SANDBOXED": "1"}):
            with self.assertRaisesRegex(SavedProjectRenderError, "offline test environment"):
                _launch(["never-run"], self.root)

    def test_windows_render_requires_decoded_audio_and_process_exit(self) -> None:
        commands = []

        def launcher(command, directory):
            commands.append((command, directory))
            write_wav(command_output(command))
            return FakeProcess(0)

        manager = self.jobs(launcher)
        original = self.project.read_bytes()
        job = manager.start(self.request())
        complete = manager.wait(job.job_id, timeout=2.0)
        self.assertEqual(complete.status, "completed")
        self.assertTrue(complete.application_exit_observed)
        self.assertFalse(complete.renderer_may_be_running)
        self.assertFalse(complete.includes_unsaved_changes)
        self.assertEqual(complete.result.frames, 480)
        self.assertEqual(complete.result.sample_rate, 48000)
        self.assertEqual(complete.result.channels, 2)
        self.assertAlmostEqual(complete.result.duration_seconds, 0.01)
        self.assertEqual(self.project.read_bytes(), original)
        self.assertEqual(commands[0][0][-1], str(self.project))
        self.assertEqual(commands[0][1], self.project.parent)
        self.assertEqual(len(commands[0][0]), 5)

    def test_macos_waits_for_app_exit_after_output_is_ready(self) -> None:
        process = FakeProcess()

        def launcher(command, directory):
            self.assertEqual(command[:6], ["/usr/bin/open", "-n", "-W", "-a", str(self.bundle), "--args"])
            self.assertEqual(command[-1], f"-F{self.project}")
            write_wav(command_output(command))
            return process

        manager = self.jobs(launcher, mac=True)
        job = manager.start(self.request(mac=True))
        ready = self.until(manager, job.job_id, "output_ready")
        self.assertFalse(ready.application_exit_observed)
        self.assertTrue(ready.renderer_may_be_running)
        self.assertFalse(ready.terminal)
        self.assertTrue(ready.result.fully_decoded)
        process.returncode = 0
        complete = manager.wait(job.job_id, timeout=2.0)
        self.assertEqual(complete.status, "completed")
        self.assertTrue(complete.application_exit_observed)
        self.assertEqual(process.terminated, 0)

    def test_changed_output_is_redecoded_before_completion(self) -> None:
        process = FakeProcess()

        def launcher(command, directory):
            write_wav(command_output(command), frames=480)
            return process

        manager = self.jobs(launcher)
        job = manager.start(self.request())
        ready = self.until(manager, job.job_id, "output_ready")
        self.assertEqual(ready.result.frames, 480)
        write_wav(Path(job.output_path), frames=960)
        process.returncode = 0
        complete = manager.wait(job.job_id, timeout=2.0)
        self.assertEqual(complete.status, "completed")
        self.assertEqual(complete.result.frames, 960)

    def test_successful_exit_without_new_audio_is_failure(self) -> None:
        self.output_root.mkdir()
        existing = self.output_root / f"{self.project.stem}.wav"
        write_wav(existing)
        old_bytes = existing.read_bytes()
        manager = self.jobs(lambda command, directory: FakeProcess(0))
        complete = manager.wait(manager.start(self.request()).job_id)
        self.assertEqual(complete.status, "failed")
        self.assertIsNone(complete.result)
        self.assertIn("without creating", complete.error)
        self.assertEqual(existing.read_bytes(), old_bytes)

    def test_truncated_wav_is_rejected_even_if_decoder_can_open_it(self) -> None:
        def launcher(command, directory):
            output = command_output(command)
            write_wav(output)
            output.write_bytes(output.read_bytes()[:-100])
            return FakeProcess(0)

        manager = self.jobs(launcher)
        complete = manager.wait(manager.start(self.request()).job_id)
        self.assertEqual(complete.status, "failed")
        self.assertIsNone(complete.result)
        self.assertIn("container length", complete.error)

    def test_float_wav_is_decoded_and_nonfinite_samples_are_rejected(self) -> None:
        path = self.root / "float.wav"
        sf.write(path, np.ones((100, 2), dtype=np.float32) * 0.1, 44100, subtype="FLOAT")
        result = decode_rendered_wav(path)
        self.assertEqual(result.subtype, "FLOAT")
        self.assertEqual(result.frames, 100)
        sf.write(path, np.full((100, 2), np.nan, dtype=np.float32), 44100, subtype="FLOAT")
        with self.assertRaisesRegex(ValueError, "nonfinite"):
            decode_rendered_wav(path)

    def test_cancel_windows_terminates_owned_process(self) -> None:
        process = FakeProcess()
        launched = threading.Event()

        def launcher(command, directory):
            launched.set()
            return process

        manager = self.jobs(launcher)
        job = manager.start(self.request())
        self.assertTrue(launched.wait(1))
        manager.cancel(job.job_id)
        final = manager.wait(job.job_id)
        self.assertEqual(final.status, "cancelled")
        self.assertEqual(process.terminated, 1)
        self.assertFalse(final.renderer_may_be_running)
        self.assertTrue(final.application_exit_observed)

    def test_cancel_macos_waiter_does_not_claim_app_exit(self) -> None:
        waiter = FakeProcess()

        def launcher(command, directory):
            write_wav(command_output(command))
            return waiter

        manager = self.jobs(launcher, mac=True)
        job = manager.start(self.request(mac=True))
        self.until(manager, job.job_id, "output_ready")
        manager.cancel(job.job_id)
        final = manager.wait(job.job_id)
        self.assertEqual(final.status, "cancelled")
        self.assertIsNotNone(final.result)
        self.assertTrue(final.renderer_may_be_running)
        self.assertFalse(final.application_exit_observed)
        self.assertEqual(waiter.terminated, 1)
        self.assertEqual(manager.cancel(job.job_id), final)

    def test_timeout_preserves_available_audio_without_claiming_completion(self) -> None:
        waiter = FakeProcess()

        def launcher(command, directory):
            write_wav(command_output(command))
            return waiter

        manager = self.jobs(launcher, mac=True)
        job = manager.start(self.request(mac=True, timeout=1.0))
        final = manager.wait(job.job_id)
        self.assertEqual(final.status, "timed_out")
        self.assertIsNotNone(final.result)
        self.assertTrue(final.renderer_may_be_running)
        self.assertFalse(final.application_exit_observed)

    def test_invalid_inputs_do_not_launch_or_create_output(self) -> None:
        manager = self.jobs(lambda command, directory: self.fail("invalid request launched FL"))
        for request in (
            self.request().model_copy(update={"project_path": "relative.flp"}),
            self.request().model_copy(update={"output_directory": "relative"}),
            self.request().model_copy(update={"project_path": str(self.root / "absent.flp")}),
        ):
            with self.assertRaises(SavedProjectRenderError):
                manager.start(request)
        self.project.write_bytes(b"not an FLP")
        with self.assertRaisesRegex(SavedProjectRenderError, "FLP header"):
            manager.start(self.request())
        self.assertFalse(self.output_root.exists())

    def test_one_active_render_and_status_snapshots_are_isolated(self) -> None:
        process = FakeProcess()
        manager = self.jobs(lambda command, directory: process)
        job = manager.start(self.request())
        job.command.clear()
        job.warnings.clear()
        self.assertTrue(manager.status(job.job_id).command)
        self.assertTrue(manager.status(job.job_id).warnings)
        with self.assertRaisesRegex(SavedProjectRenderError, "already active"):
            manager.start(self.request())
        manager.cancel(job.job_id)
        manager.wait(job.job_id)

    def test_terminal_history_is_bounded_and_each_render_uses_fresh_directory(self) -> None:
        def launcher(command, directory):
            write_wav(command_output(command))
            return FakeProcess(0)

        manager = self.jobs(launcher, max_jobs=1)
        first = manager.start(self.request())
        manager.wait(first.job_id)
        second = manager.start(self.request())
        manager.wait(second.job_id)
        self.assertNotEqual(first.output_directory, second.output_directory)
        self.assertEqual(len(manager.list()), 1)
        self.assertTrue(Path(first.output_path).is_file())
        with self.assertRaises(SavedProjectRenderError):
            manager.status(first.job_id)

    def test_launch_failure_is_terminal_and_does_not_claim_running_renderer(self) -> None:
        def launcher(command, directory):
            raise OSError("synthetic launch failure")

        manager = self.jobs(launcher)
        final = manager.wait(manager.start(self.request()).job_id)
        self.assertEqual(final.status, "failed")
        self.assertFalse(final.renderer_may_be_running)
        self.assertFalse(final.application_exit_observed)
        self.assertIn("synthetic launch failure", final.error)

    def test_nonzero_exit_is_failure_even_with_a_wav(self) -> None:
        def launcher(command, directory):
            write_wav(command_output(command))
            return FakeProcess(7)

        manager = self.jobs(launcher)
        final = manager.wait(manager.start(self.request()).job_id)
        self.assertEqual(final.status, "failed")
        self.assertEqual(final.process_return_code, 7)
        self.assertTrue(final.application_exit_observed)

    def test_path_resolution_supports_environment_and_explicit_override(self) -> None:
        with patch.dict(os.environ, {FL_STUDIO_PATH_ENV: str(self.executable)}):
            self.assertEqual(resolve_fl_studio_path(system="Windows"), ("windows", self.executable))
            self.assertEqual(
                resolve_fl_studio_path(str(self.bundle), system="Darwin"), ("macos", self.bundle)
            )
        with self.assertRaises(SavedProjectRenderError):
            resolve_fl_studio_path(str(self.executable), system="Linux")
        with self.assertRaises(SavedProjectRenderError):
            resolve_fl_studio_path("relative.exe", system="Windows")

    def test_windows_discovery_prefers_newest_installed_version(self) -> None:
        installation = self.root / "Program Files" / "Image-Line"
        for version in ("21", "2025", "2026"):
            target = installation / f"FL Studio {version}" / "FL64.exe"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"fake")
        with patch.dict(os.environ, {
            FL_STUDIO_PATH_ENV: "", "ProgramFiles": str(installation.parent), "ProgramFiles(x86)": ""
        }):
            kind, path = resolve_fl_studio_path(system="Windows")
        self.assertEqual(kind, "windows")
        self.assertEqual(path, installation / "FL Studio 2026" / "FL64.exe")

    def test_command_builder_keeps_paths_as_literal_single_arguments(self) -> None:
        output = self.root / "out & $stuff's.wav"
        command = build_render_command(
            kind="windows", application=self.executable, project=self.project, output=output
        )
        self.assertEqual(command[1], f"/R{output}")
        self.assertEqual(command[-1], str(self.project))
        self.assertEqual(command[2], "/Ewav")

    def test_shutdown_cancels_active_job_and_prevents_new_jobs(self) -> None:
        manager = self.jobs(lambda command, directory: FakeProcess())
        job = manager.start(self.request())
        manager.shutdown(wait=True)
        self.assertEqual(manager.status(job.job_id).status, "cancelled")
        with self.assertRaisesRegex(SavedProjectRenderError, "closed"):
            manager.start(self.request())


if __name__ == "__main__":
    unittest.main(verbosity=2)
