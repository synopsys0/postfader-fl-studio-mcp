"""Focused tests for safe, shell-free Codex MCP registration."""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from fl_studio_mcp import bridge_install, client_config, codex_installer, setup_wizard


TEMP_ROOT = Path(tempfile.gettempdir()).resolve()


def _facts(root: Path) -> client_config.ClientConfigurationFacts:
    return client_config.configuration_facts(
        repository_root=root,
        interpreter=root / "venv with spaces" / "python's.exe",
        user_data_dir=root / "FL Data & Projects",
        transport="midi",
        midi_port="Loop'; Remove-Item *",
    )


def _server(facts: client_config.ClientConfigurationFacts) -> dict[str, object]:
    return {
        "name": facts.server_name,
        "enabled": True,
        "transport": {
            "type": "stdio",
            "command": os.fspath(facts.interpreter),
            "args": ["-m", "fl_studio_mcp.mcp_server"],
            "env": facts.environment,
            "env_vars": [],
            "cwd": None,
        },
    }


def _completed(arguments, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(arguments, returncode, stdout, stderr)


class CodexInstallerTests(unittest.TestCase):
    def test_add_argv_and_powershell_rendering_keep_hostile_values_safe(self):
        facts = _facts(TEMP_ROOT / "PostFader root")
        arguments = client_config.codex_add_argv(facts)
        rendered = client_config.render_codex_add_command(facts)

        self.assertEqual(arguments[:4], ("codex", "mcp", "add", "fl-studio"))
        self.assertIn("FL_BRIDGE_MIDI_PORT=Loop'; Remove-Item *", arguments)
        self.assertIn(os.fspath(facts.interpreter), arguments)
        self.assertNotIn("FL_BRIDGE_ENABLE_WRITES=1", arguments)
        self.assertTrue(rendered.startswith("codex mcp add fl-studio --env "))
        self.assertIn("Loop''; Remove-Item *'", rendered)
        self.assertIn("python''s.exe'", rendered)

    def test_dry_run_is_inert(self):
        facts = _facts(TEMP_ROOT / "PostFader")
        result = codex_installer.register_codex(
            facts,
            apply=False,
            runner=lambda *_args, **_kwargs: self.fail("dry-run invoked Codex"),
        )
        self.assertEqual(result["status"], "planned")
        self.assertFalse(result["changed"])

    def test_missing_server_is_added_and_verified_without_a_shell(self):
        facts = _facts(TEMP_ROOT / "PostFader")
        calls = []
        responses = [
            _completed([], stdout="[]"),
            _completed([]),
            _completed([], stdout=json.dumps([_server(facts)])),
        ]

        def runner(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return responses.pop(0)

        result = codex_installer.register_codex(facts, apply=True, runner=runner)

        self.assertEqual(result["status"], "added")
        self.assertEqual(calls[1][0], list(client_config.codex_add_argv(facts)))
        self.assertTrue(all("shell" not in kwargs for _args, kwargs in calls))

    def test_production_path_resolves_the_codex_executable(self):
        facts = _facts(TEMP_ROOT / "PostFader")
        executable = os.fspath(TEMP_ROOT / "Codex App" / "codex.exe")
        calls = []
        responses = [
            _completed([], stdout="[]"),
            _completed([]),
            _completed([], stdout=json.dumps([_server(facts)])),
        ]

        def runner(arguments, **_kwargs):
            calls.append(arguments)
            return responses.pop(0)

        with (
            mock.patch.object(codex_installer.shutil, "which", return_value=executable),
            mock.patch.object(codex_installer.subprocess, "run", side_effect=runner),
        ):
            result = codex_installer.register_codex(facts, apply=True)

        self.assertEqual(result["status"], "added")
        self.assertTrue(all(call[0] == executable for call in calls))

    def test_identical_server_is_idempotent_but_inherited_env_is_a_conflict(self):
        facts = _facts(TEMP_ROOT / "PostFader")
        current = _server(facts)
        calls = []

        def runner(arguments, **_kwargs):
            calls.append(arguments)
            return _completed(arguments, stdout=json.dumps([current]))

        result = codex_installer.register_codex(facts, apply=True, runner=runner)
        self.assertEqual(result["status"], "current")
        self.assertEqual(len(calls), 1)

        current["transport"]["env_vars"] = ["PATH"]
        result = codex_installer.register_codex(facts, apply=True, runner=runner)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(len(calls), 2)

        current["transport"]["env_vars"] = []
        current["transport"]["cwd"] = []
        result = codex_installer.register_codex(facts, apply=True, runner=runner)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(len(calls), 3)

    def test_conflicting_server_is_preserved(self):
        facts = _facts(TEMP_ROOT / "PostFader")
        conflicting = _server(facts)
        conflicting["transport"] = {
            **conflicting["transport"],
            "command": "/other/python",
        }
        calls = []

        def runner(arguments, **_kwargs):
            calls.append(arguments)
            return _completed(arguments, stdout=json.dumps([conflicting]))

        result = codex_installer.register_codex(facts, apply=True, runner=runner)
        self.assertEqual(result["status"], "conflict")
        self.assertFalse(result["changed"])
        self.assertEqual(len(calls), 1)

    def test_missing_codex_cli_returns_manual_toml(self):
        facts = _facts(TEMP_ROOT / "PostFader")

        def runner(*_args, **_kwargs):
            raise FileNotFoundError("codex")

        result = codex_installer.register_codex(facts, apply=True, runner=runner)
        self.assertEqual(result["status"], "manual")
        self.assertIn("[mcp_servers.fl-studio]", result["manual_toml"])

    def test_failed_verification_never_removes_an_unverified_entry(self):
        facts = _facts(TEMP_ROOT / "PostFader")
        calls = []
        responses = [
            _completed([], stdout="[]"),
            _completed([]),
            _completed([], stdout="[]"),
        ]

        def runner(arguments, **_kwargs):
            calls.append(arguments)
            return responses.pop(0)

        with self.assertRaisesRegex(
            codex_installer.CodexRegistrationError,
            "inspect the saved entry",
        ):
            codex_installer.register_codex(facts, apply=True, runner=runner)
        self.assertEqual(len(calls), 3)
        self.assertFalse(any("remove" in call for call in calls))

    def test_add_failure_never_removes_or_overwrites(self):
        facts = _facts(TEMP_ROOT / "PostFader")
        calls = []
        responses = [
            _completed([], stdout="[]"),
            _completed([], returncode=1, stderr="config write failed"),
        ]

        def runner(arguments, **_kwargs):
            calls.append(arguments)
            return responses.pop(0)

        with self.assertRaisesRegex(
            codex_installer.CodexRegistrationError,
            "config write failed",
        ):
            codex_installer.register_codex(facts, apply=True, runner=runner)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("remove", calls[-1])

    def test_noninteractive_registration_needs_separate_confirmation(self):
        observed = []
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = setup_wizard.main(
                ["--non-interactive", "--register-codex"],
                midi_probe=lambda: observed.append("midi") or {"in": [], "out": []},
            )
        self.assertEqual(status, 1)
        self.assertEqual(observed, [])

    def test_confirmed_setup_registers_after_safe_setup(self):
        with tempfile.TemporaryDirectory(prefix="postfader-codex-") as raw:
            root = Path(raw)
            user_data = root / "FL Data"
            target = bridge_install.target_path(os.fspath(user_data))
            target.parent.mkdir(parents=True)
            target.write_bytes(b"bridge")
            registration = {
                "status": "added",
                "changed": True,
                "server_name": "fl-studio",
                "manual_toml": "",
            }
            stderr = io.StringIO()
            with (
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
                mock.patch.object(
                    bridge_install,
                    "expected_bridge_deployment",
                    return_value=(b"bridge", "a" * 64),
                ),
                mock.patch.object(
                    codex_installer,
                    "register_codex",
                    return_value=registration,
                ) as register,
            ):
                status = setup_wizard.main(
                    [
                        "--non-interactive",
                        "--register-codex",
                        "--yes-register-codex",
                        "--user-data-dir",
                        os.fspath(user_data),
                        "--midi-port",
                        "PostFader Loop",
                    ],
                    midi_probe=lambda: {
                        "in": ["PostFader Loop"],
                        "out": ["PostFader Loop"],
                    },
                    doctor_collector=lambda **_kwargs: {
                        "overall": "pass",
                        "live": {
                            "status": "connected",
                            "bridge_mode": "read_only",
                            "verified_writes_enabled": False,
                            "read_only_bridge": True,
                        },
                        "failures": [],
                    },
                )

        self.assertEqual(status, 0, stderr.getvalue())
        register.assert_called_once()
        self.assertTrue(register.call_args.kwargs["apply"])
        self.assertIn("Registered PostFader in Codex", stderr.getvalue())

    def test_interactive_confirmation_sees_the_resolved_plan_first(self):
        with tempfile.TemporaryDirectory(prefix="postfader-codex-plan-") as raw:
            root = Path(raw)
            user_data = root / "FL Data"
            target = bridge_install.target_path(os.fspath(user_data))
            target.parent.mkdir(parents=True)
            target.write_bytes(b"bridge")
            stderr = io.StringIO()
            prompt_snapshots = []

            def prompt(message):
                prompt_snapshots.append((message, stderr.getvalue()))
                return "n" if message.startswith("Register this resolved") else ""

            with (
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
                mock.patch.object(
                    bridge_install,
                    "expected_bridge_deployment",
                    return_value=(b"bridge", "a" * 64),
                ),
                mock.patch.object(codex_installer, "register_codex") as register,
            ):
                status = setup_wizard.main(
                    [
                        "--interactive",
                        "--register-codex",
                        "--user-data-dir",
                        os.fspath(user_data),
                        "--midi-port",
                        "PostFader Loop",
                    ],
                    prompt=prompt,
                    midi_probe=lambda: {
                        "in": ["PostFader Loop"],
                        "out": ["PostFader Loop"],
                    },
                    doctor_collector=lambda **_kwargs: {
                        "overall": "pass",
                        "live": {
                            "status": "connected",
                            "bridge_mode": "read_only",
                            "verified_writes_enabled": False,
                            "read_only_bridge": True,
                        },
                        "failures": [],
                    },
                )

        self.assertEqual(status, 2)
        register.assert_not_called()
        registration_prompt = next(
            snapshot
            for message, snapshot in prompt_snapshots
            if message.startswith("Register this resolved")
        )
        self.assertIn("Codex registration plan:", registration_prompt)
        self.assertIn('"server_name": "fl-studio"', registration_prompt)
        self.assertIn('"args": [', registration_prompt)
        self.assertNotIn('"cwd"', registration_prompt)
        self.assertIn(os.fspath(user_data), registration_prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
