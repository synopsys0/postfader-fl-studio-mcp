"""Focused safety tests for the guided first-time setup core."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from subprocess import CompletedProcess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from fl_studio_mcp import bridge_install, cli, setup_wizard


ROOT = Path(__file__).resolve().parents[1]


def _doctor(**_kwargs):
    return {"overall": "fail", "live": {"status": "unavailable"}, "failures": []}


def _ready_doctor(**_kwargs):
    return {
        "overall": "pass",
        "live": {
            "status": "connected",
            "bridge_mode": "read_only",
            "verified_writes_enabled": False,
            "read_only_bridge": True,
        },
        "failures": [],
    }


class SetupWizardTests(unittest.TestCase):
    def test_top_level_postfader_command_dispatches_setup(self):
        with mock.patch.object(setup_wizard, "main", return_value=7) as delegated:
            self.assertEqual(cli.main(["setup", "--dry-run"]), 7)
        delegated.assert_called_once_with(["--dry-run"])

    def test_top_level_postfader_command_has_bounded_help_and_unknown_error(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(cli.main(["--help"]), 0)
            self.assertEqual(cli.main(["unknown"]), 2)
        self.assertIn("postfader setup", stdout.getvalue())
        self.assertIn("unknown PostFader command", stderr.getvalue())

    def test_python_315_is_rejected_before_setup_mutation(self):
        completed = CompletedProcess(
            args=[], returncode=0, stdout="3.15.0\n", stderr=""
        )
        with mock.patch.object(setup_wizard.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(setup_wizard.SetupError, "3.10 through 3.14"):
                setup_wizard._python_version(Path(sys.executable))

    def test_windows_process_inventory_recognizes_fl_executables(self):
        completed = CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                '"explorer.exe","100","Console","1","10,000 K"\n'
                '"FL64.exe","4242","Console","1","200,000 K"\n'
                '"FL Studio.exe","5252","Console","1","200,000 K"\n'
            ),
            stderr="",
        )
        with mock.patch.object(setup_wizard.subprocess, "run", return_value=completed):
            self.assertEqual(
                setup_wizard._windows_running_fl_studio_process_ids(),
                (4242, 5252),
            )

    def test_unsupported_platform_is_refused_before_midi_enumeration(self):
        called = []
        with (
            redirect_stderr(io.StringIO()),
            mock.patch.object(setup_wizard, "platform_family", return_value="other"),
        ):
            status = setup_wizard.main(
                ["--non-interactive", "--midi-port", "unused", "--client", "codex-toml"],
                midi_probe=lambda: called.append(True) or {"in": [], "out": []},
            )
        self.assertEqual(status, 1)
        self.assertEqual(called, [])

    def test_inventory_exposes_only_unique_exact_bidirectional_names(self):
        inventory = setup_wizard.inventory_midi_endpoints(
            lambda: {
                "in": ["PostFader Loop", "Input only", "Duplicate", "duplicate"],
                "out": ["postfader loop", "Output only", "Duplicate"],
            }
        )
        self.assertEqual(inventory.inputs[0], "PostFader Loop")
        self.assertEqual(inventory.outputs[0], "postfader loop")
        self.assertEqual(
            [choice.as_dict() for choice in inventory.choices],
            [
                {
                    "name": "PostFader Loop",
                    "input": "PostFader Loop",
                    "output": "postfader loop",
                }
            ],
        )
        self.assertEqual(
            setup_wizard.select_midi_choice("POSTFADER LOOP", inventory),
            inventory.choices[0],
        )
        with self.assertRaisesRegex(setup_wizard.SetupError, "not an exact"):
            setup_wizard.select_midi_choice("PostFader", inventory)

    def test_noninteractive_dry_run_is_plannable_and_never_writes(self):
        with tempfile.TemporaryDirectory(prefix="postfader-setup-") as raw:
            root = Path(raw)
            user_data = root / "FL Data"
            (user_data / "Settings" / "Hardware").mkdir(parents=True)
            output = root / "new-config.toml"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                mock.patch.object(
                    bridge_install,
                    "expected_bridge_deployment",
                    return_value=(b"bridge", "a" * 64),
                ),
            ):
                status = setup_wizard.main(
                    [
                        "--non-interactive",
                        "--dry-run",
                        "--user-data-dir",
                        os.fspath(user_data),
                        "--midi-port",
                        "PostFader Loop",
                        "--client",
                        "codex-toml",
                        "--output",
                        os.fspath(output),
                    ],
                    midi_probe=lambda: {
                        "in": ["PostFader Loop"],
                        "out": ["PostFader Loop"],
                    },
                    deployer=lambda _path: self.fail("dry-run deployed the bridge"),
                    doctor_collector=lambda **_kwargs: self.fail(
                        "dry-run contacted the live doctor"
                    ),
                )
            self.assertEqual(status, 0, stderr.getvalue())
            self.assertFalse(output.exists())
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("Would create client configuration", stderr.getvalue())
            self.assertIn("doctor: not_run", stderr.getvalue())
            self.assertIn(
                "Rerun postfader setup without --dry-run", stderr.getvalue()
            )
            self.assertIn("add --yes", stderr.getvalue())

    def test_bridge_change_requires_yes_before_any_file_is_created(self):
        with tempfile.TemporaryDirectory(prefix="postfader-setup-") as raw:
            root = Path(raw)
            user_data = root / "FL Data"
            (user_data / "Settings" / "Hardware").mkdir(parents=True)
            output = root / "new-config.json"
            called = []
            with (
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
                mock.patch.object(
                    bridge_install,
                    "expected_bridge_deployment",
                    return_value=(b"bridge", "b" * 64),
                ),
            ):
                status = setup_wizard.main(
                    [
                        "--non-interactive",
                        "--user-data-dir",
                        os.fspath(user_data),
                        "--midi-port",
                        "PostFader Loop",
                        "--client",
                        "claude-json",
                        "--output",
                        os.fspath(output),
                    ],
                    midi_probe=lambda: {
                        "in": ["PostFader Loop"],
                        "out": ["PostFader Loop"],
                    },
                    deployer=lambda path: called.append(path),
                    doctor_collector=_doctor,
                )
            self.assertEqual(status, 1)
            self.assertEqual(called, [])
            self.assertFalse(output.exists())

    def test_windows_bridge_change_is_refused_while_fl_studio_is_running(self):
        with tempfile.TemporaryDirectory(prefix="postfader-setup-") as raw:
            root = Path(raw)
            user_data = root / "FL Data"
            (user_data / "Settings" / "Hardware").mkdir(parents=True)
            inventory = setup_wizard.inventory_midi_endpoints(
                lambda: {"in": ["PostFader Loop"], "out": ["PostFader Loop"]}
            )
            output = root / "new-config.toml"
            request = setup_wizard.SetupRequest(
                user_data_dir=user_data,
                midi_choice=inventory.choices[0],
                client_format="codex-toml",
                repository_root=ROOT,
                interpreter=Path(sys.executable),
                output=output,
                confirmed=True,
                platform_name="win32",
            )
            with (
                mock.patch.object(
                    bridge_install,
                    "expected_bridge_deployment",
                    return_value=(b"bridge", "b" * 64),
                ),
                mock.patch.object(
                    setup_wizard,
                    "_windows_running_fl_studio_process_ids",
                    return_value=(4242,),
                ),
            ):
                with self.assertRaisesRegex(setup_wizard.SetupError, "PID.*4242"):
                    setup_wizard.execute_setup(
                        request,
                        inventory,
                        deployer=lambda _path: self.fail(
                            "bridge changed while FL Studio was running"
                        ),
                    )
            self.assertFalse(output.exists())

    def test_yes_deploys_and_stdout_contains_selected_safe_config(self):
        with tempfile.TemporaryDirectory(prefix="postfader-setup-") as raw:
            root = Path(raw)
            user_data = root / "FL Data"
            (user_data / "Settings" / "Hardware").mkdir(parents=True)
            target = bridge_install.target_path(os.fspath(user_data))
            deployments = []

            def deploy(path):
                deployments.append(path)
                return {
                    "changed": True,
                    "target": target,
                    "digest": "c" * 64,
                    "backup": root / "previous-bridge.py.bak",
                }

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                mock.patch.object(
                    bridge_install,
                    "expected_bridge_deployment",
                    return_value=(b"bridge", "c" * 64),
                ),
            ):
                status = setup_wizard.main(
                    [
                        "--non-interactive",
                        "--yes",
                        "--user-data-dir",
                        os.fspath(user_data),
                        "--midi-port",
                        "PostFader Loop",
                        "--client",
                        "codex-toml",
                    ],
                    midi_probe=lambda: {
                        "in": ["PostFader Loop"],
                        "out": ["PostFader Loop"],
                    },
                    deployer=deploy,
                    doctor_collector=_doctor,
                )
            self.assertEqual(status, 2, stderr.getvalue())
            self.assertEqual(deployments, [os.fspath(user_data)])
            self.assertIn('FL_BRIDGE_MIDI_PORT = "PostFader Loop"', stdout.getvalue())
            self.assertNotIn("FL_BRIDGE_ENABLE_WRITES", stdout.getvalue())
            self.assertIn("Bridge: current; doctor: fail", stderr.getvalue())
            self.assertIn("Bridge backup:", stderr.getvalue())
            last_line = stderr.getvalue().rstrip().splitlines()[-1]
            self.assertTrue(last_line.startswith("Next FL action:"))

    def test_completed_doctor_is_the_success_exit_condition(self):
        with tempfile.TemporaryDirectory(prefix="postfader-setup-") as raw:
            root = Path(raw)
            user_data = root / "FL Data"
            target = bridge_install.target_path(os.fspath(user_data))
            target.parent.mkdir(parents=True)
            target.write_bytes(b"bridge")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                mock.patch.object(
                    bridge_install,
                    "expected_bridge_deployment",
                    return_value=(b"bridge", "e" * 64),
                ),
            ):
                status = setup_wizard.main(
                    [
                        "--non-interactive",
                        "--user-data-dir",
                        os.fspath(user_data),
                        "--midi-port",
                        "PostFader Loop",
                        "--client",
                        "codex-command",
                    ],
                    midi_probe=lambda: {
                        "in": ["PostFader Loop"],
                        "out": ["PostFader Loop"],
                    },
                    deployer=lambda _path: self.fail("current bridge was redeployed"),
                    doctor_collector=_ready_doctor,
                )
        self.assertEqual(status, 0, stderr.getvalue())
        self.assertIn("codex mcp add", stdout.getvalue())
        self.assertIn("PowerShell", stderr.getvalue())
        self.assertIn("ask PostFader to inspect", stderr.getvalue())

    def test_write_enabled_doctor_cannot_complete_first_time_setup(self):
        with tempfile.TemporaryDirectory(prefix="postfader-setup-") as raw:
            root = Path(raw)
            user_data = root / "FL Data"
            target = bridge_install.target_path(os.fspath(user_data))
            target.parent.mkdir(parents=True)
            target.write_bytes(b"bridge")
            stderr = io.StringIO()
            write_enabled_doctor = {
                "overall": "pass",
                "live": {
                    "status": "connected",
                    "bridge_mode": "write_test",
                    "verified_writes_enabled": True,
                    "read_only_bridge": False,
                },
                "failures": [],
            }
            with (
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
                mock.patch.object(
                    bridge_install,
                    "expected_bridge_deployment",
                    return_value=(b"bridge", "e" * 64),
                ),
            ):
                status = setup_wizard.main(
                    [
                        "--non-interactive",
                        "--user-data-dir",
                        os.fspath(user_data),
                        "--midi-port",
                        "PostFader Loop",
                        "--client",
                        "codex-command",
                    ],
                    midi_probe=lambda: {
                        "in": ["PostFader Loop"],
                        "out": ["PostFader Loop"],
                    },
                    doctor_collector=lambda **_kwargs: write_enabled_doctor,
                )
        self.assertEqual(status, 2)
        self.assertIn("without write mode enabled", stderr.getvalue())

    def test_doctor_process_environment_is_forced_read_only_and_restored(self):
        with tempfile.TemporaryDirectory(prefix="postfader-setup-") as raw:
            root = Path(raw)
            user_data = root / "FL Data"
            target = bridge_install.target_path(os.fspath(user_data))
            target.parent.mkdir(parents=True)
            target.write_bytes(b"bridge")
            inventory = setup_wizard.inventory_midi_endpoints(
                lambda: {"in": ["PostFader Loop"], "out": ["PostFader Loop"]}
            )
            request = setup_wizard.SetupRequest(
                user_data_dir=user_data,
                midi_choice=inventory.choices[0],
                client_format="codex-toml",
                repository_root=ROOT,
                interpreter=Path(sys.executable),
                confirmed=True,
            )
            observed = []

            def doctor(**kwargs):
                observed.append(kwargs["environ"]["FL_BRIDGE_ENABLE_WRITES"])
                return _ready_doctor()

            with (
                mock.patch.dict(
                    os.environ, {"FL_BRIDGE_ENABLE_WRITES": "1"}, clear=False
                ),
                mock.patch.object(
                    bridge_install,
                    "expected_bridge_deployment",
                    return_value=(b"bridge", "e" * 64),
                ),
            ):
                result = setup_wizard.execute_setup(
                    request, inventory, doctor_collector=doctor
                )
                restored = os.environ["FL_BRIDGE_ENABLE_WRITES"]

        self.assertEqual(result["status"], "ready")
        self.assertEqual(observed, ["0"])
        self.assertEqual(restored, "1")

    def test_identical_output_file_is_accepted_when_resuming_setup(self):
        with tempfile.TemporaryDirectory(prefix="postfader-setup-") as raw:
            root = Path(raw)
            user_data = root / "FL Data"
            target = bridge_install.target_path(os.fspath(user_data))
            target.parent.mkdir(parents=True)
            target.write_bytes(b"bridge")
            output = root / "codex.toml"
            arguments = [
                "--non-interactive",
                "--user-data-dir",
                os.fspath(user_data),
                "--midi-port",
                "PostFader Loop",
                "--client",
                "codex-toml",
                "--output",
                os.fspath(output),
            ]
            probe = lambda: {
                "in": ["PostFader Loop"],
                "out": ["PostFader Loop"],
            }
            with mock.patch.object(
                bridge_install,
                "expected_bridge_deployment",
                return_value=(b"bridge", "f" * 64),
            ):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    first = setup_wizard.main(
                        arguments, midi_probe=probe, doctor_collector=_doctor
                    )
                original = output.read_bytes()
                stderr = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    resumed = setup_wizard.main(
                        arguments, midi_probe=probe, doctor_collector=_doctor
                    )
                final = output.read_bytes()

        self.assertEqual(first, 2)
        self.assertEqual(resumed, 2)
        self.assertEqual(final, original)
        self.assertIn("already current", stderr.getvalue())

    def test_different_output_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory(prefix="postfader-setup-") as raw:
            root = Path(raw)
            user_data = root / "FL Data"
            (user_data / "Settings" / "Hardware").mkdir(parents=True)
            output = root / "codex.toml"
            output.write_text("user-owned\n", encoding="utf-8")
            stderr = io.StringIO()
            with (
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
                mock.patch.object(
                    bridge_install,
                    "expected_bridge_deployment",
                    return_value=(b"bridge", "f" * 64),
                ),
            ):
                status = setup_wizard.main(
                    [
                        "--non-interactive",
                        "--yes",
                        "--user-data-dir",
                        os.fspath(user_data),
                        "--midi-port",
                        "PostFader Loop",
                        "--client",
                        "codex-toml",
                        "--output",
                        os.fspath(output),
                    ],
                    midi_probe=lambda: {
                        "in": ["PostFader Loop"],
                        "out": ["PostFader Loop"],
                    },
                    deployer=lambda _path: self.fail(
                        "different output was not rejected before deployment"
                    ),
                    doctor_collector=lambda **_kwargs: self.fail(
                        "different output reached the doctor"
                    ),
                )
            final = output.read_text(encoding="utf-8")

        self.assertEqual(status, 1)
        self.assertEqual(final, "user-owned\n")
        self.assertIn("refusing to overwrite different", stderr.getvalue())

    def test_non_utf8_output_file_is_cleanly_refused(self):
        with tempfile.TemporaryDirectory(prefix="postfader-setup-") as raw:
            root = Path(raw)
            user_data = root / "FL Data"
            (user_data / "Settings" / "Hardware").mkdir(parents=True)
            output = root / "foreign-config.bin"
            output.write_bytes(b"\xff\xfe")
            stderr = io.StringIO()
            with (
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
                mock.patch.object(
                    bridge_install,
                    "expected_bridge_deployment",
                    return_value=(b"bridge", "f" * 64),
                ),
            ):
                status = setup_wizard.main(
                    [
                        "--non-interactive",
                        "--yes",
                        "--user-data-dir",
                        os.fspath(user_data),
                        "--midi-port",
                        "PostFader Loop",
                        "--client",
                        "codex-toml",
                        "--output",
                        os.fspath(output),
                    ],
                    midi_probe=lambda: {
                        "in": ["PostFader Loop"],
                        "out": ["PostFader Loop"],
                    },
                )
                preserved = output.read_bytes()

        self.assertEqual(status, 1)
        self.assertEqual(preserved, b"\xff\xfe")
        self.assertIn("could not inspect existing", stderr.getvalue())

    def test_interactive_apply_pauses_for_manual_fl_action_before_doctor(self):
        with tempfile.TemporaryDirectory(prefix="postfader-setup-") as raw:
            root = Path(raw)
            user_data = root / "FL Data"
            (user_data / "Settings" / "Hardware").mkdir(parents=True)
            inventory = setup_wizard.inventory_midi_endpoints(
                lambda: {"in": ["PostFader Loop"], "out": ["PostFader Loop"]}
            )
            events = []

            def prompt(message):
                events.append(("prompt", message))
                return "yes" if "[y/N]" in message else ""

            def deploy(_path):
                events.append(("deploy", None))
                return {
                    "changed": True,
                    "target": bridge_install.target_path(os.fspath(user_data)),
                    "digest": "d" * 64,
                    "backup": None,
                }

            def doctor(**_kwargs):
                events.append(("doctor", None))
                return {
                    "overall": "fail",
                    "live": {"status": "unavailable"},
                    "failures": [
                        {"action": "Start FL Studio, then rerun setup."}
                    ],
                }

            request = setup_wizard.SetupRequest(
                user_data_dir=user_data,
                midi_choice=inventory.choices[0],
                client_format="codex-toml",
                repository_root=ROOT,
                interpreter=Path(sys.executable),
                confirmed=False,
                interactive=True,
            )
            with mock.patch.object(
                bridge_install,
                "expected_bridge_deployment",
                return_value=(b"bridge", "d" * 64),
            ):
                result = setup_wizard.execute_setup(
                    request,
                    inventory,
                    prompt=prompt,
                    deployer=deploy,
                    doctor_collector=doctor,
                )

        manual_prompt = next(
            index
            for index, event in enumerate(events)
            if event[0] == "prompt" and "Options > MIDI settings" in event[1]
        )
        doctor_event = events.index(("doctor", None))
        self.assertLess(manual_prompt, doctor_event)
        self.assertEqual(result["status"], "needs_action")
        self.assertEqual(result["next_fl_action"], "Start FL Studio, then rerun setup.")


if __name__ == "__main__":
    unittest.main()
