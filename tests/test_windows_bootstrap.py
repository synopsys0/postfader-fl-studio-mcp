"""Hermetic Windows bootstrap, launcher, and client-config checks."""

from __future__ import annotations

import json
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - supported Python 3.10 uses tomli.
    import tomli as tomllib

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, os.fspath(ROOT))

from fl_studio_mcp.client_config import (  # noqa: E402
    configuration_facts,
    render_claude_json,
    render_codex_add_command,
    render_codex_toml,
    write_new_configuration,
)
from fl_studio_mcp.host_config import HostConfigurationError  # noqa: E402


class ClientConfigurationTests(unittest.TestCase):
    def facts(self, root: Path, *, transport="automatic", midi_port=None):
        interpreter = root / "venv with spaces" / "Scripts" / "python.exe"
        user_data = root / "Documents" / "Image-Line" / "FL Studio"
        return configuration_facts(
            repository_root=root,
            interpreter=interpreter,
            user_data_dir=user_data,
            transport=transport,
            midi_port=midi_port,
            environ={},
            platform_name="win32",
        )

    def test_automatic_transport_does_not_silently_enable_midi_or_writes(self):
        with tempfile.TemporaryDirectory(prefix="postfader-config-") as raw:
            facts = self.facts(Path(raw))
        self.assertEqual(
            set(facts.environment), {"FL_STUDIO_USER_DATA_DIR"}
        )
        self.assertNotIn("FL_BRIDGE_ENABLE_WRITES", facts.environment)

    def test_midi_transport_emits_both_explicit_transport_facts(self):
        with tempfile.TemporaryDirectory(prefix="postfader-config-") as raw:
            facts = self.facts(
                Path(raw), transport="midi", midi_port="Chosen Loopback"
            )
        self.assertEqual(facts.environment["FL_BRIDGE_ENABLE_MIDI"], "1")
        self.assertEqual(
            facts.environment["FL_BRIDGE_MIDI_PORT"], "Chosen Loopback"
        )
        self.assertNotIn("FL_BRIDGE_ENABLE_WRITES", facts.environment)

    def test_midi_port_without_midi_transport_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="postfader-config-") as raw:
            with self.assertRaisesRegex(
                HostConfigurationError, "requires --transport midi"
            ):
                self.facts(Path(raw), midi_port="Must Not Be Enabled")

    def test_codex_toml_and_claude_json_share_absolute_facts(self):
        with tempfile.TemporaryDirectory(prefix="postfader-config-") as raw:
            facts = self.facts(
                Path(raw), transport="midi", midi_port="Chosen Loopback"
            )
            codex = tomllib.loads(render_codex_toml(facts))
            claude = json.loads(render_claude_json(facts))

        codex_server = codex["mcp_servers"]["fl-studio"]
        claude_server = claude["mcpServers"]["fl-studio"]
        self.assertTrue(Path(codex_server["command"]).is_absolute())
        self.assertTrue(Path(codex_server["cwd"]).is_absolute())
        self.assertEqual(codex_server["command"], claude_server["command"])
        self.assertEqual(codex_server["cwd"], claude_server["cwd"])
        self.assertEqual(codex_server["env"], claude_server["env"])
        self.assertNotIn("FL_BRIDGE_ENABLE_WRITES", codex_server["env"])

    def test_codex_command_uses_documented_add_shape_and_absolute_python(self):
        with tempfile.TemporaryDirectory(prefix="postfader-config-") as raw:
            facts = self.facts(
                Path(raw), transport="midi", midi_port="Chosen Loopback"
            )
            command = render_codex_add_command(facts)
        self.assertTrue(command.startswith("codex mcp add fl-studio "))
        self.assertIn("--env 'FL_BRIDGE_ENABLE_MIDI=1'", command)
        self.assertIn("-- '" + os.fspath(facts.interpreter), command)
        self.assertNotIn("FL_BRIDGE_ENABLE_WRITES", command)

    def test_explicit_output_creates_once_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="postfader-config-") as raw:
            destination = Path(raw) / "new output" / "codex.toml"
            write_new_configuration(destination, "first\n")
            self.assertEqual(destination.read_text(encoding="utf-8"), "first\n")
            with self.assertRaises(FileExistsError):
                write_new_configuration(destination, "second\n")
            self.assertEqual(destination.read_text(encoding="utf-8"), "first\n")

    def test_generator_cli_emits_absolute_codex_environment(self):
        with tempfile.TemporaryDirectory(prefix="postfader generator ") as raw:
            root = Path(raw)
            python = root / "venv" / "Scripts" / "python.exe"
            user_data = root / "Documents" / "Image-Line" / "FL Studio"
            completed = subprocess.run(
                [
                    sys.executable,
                    os.fspath(ROOT / "scripts" / "generate_mcp_config.py"),
                    "--format",
                    "codex-toml",
                    "--repository-root",
                    os.fspath(root),
                    "--python",
                    os.fspath(python),
                    "--user-data-dir",
                    os.fspath(user_data),
                    "--transport",
                    "midi",
                    "--midi-port",
                    "Chosen Loopback",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        server = tomllib.loads(completed.stdout)["mcp_servers"]["fl-studio"]
        self.assertTrue(Path(server["command"]).is_absolute())
        self.assertTrue(Path(server["cwd"]).is_absolute())
        self.assertTrue(Path(server["env"]["FL_STUDIO_USER_DATA_DIR"]).is_absolute())
        self.assertEqual(server["env"]["FL_BRIDGE_MIDI_PORT"], "Chosen Loopback")
        self.assertNotIn("FL_BRIDGE_ENABLE_WRITES", server["env"])

    def test_generator_default_interpreter_is_native_to_selected_platform(self):
        path = ROOT / "scripts" / "generate_mcp_config.py"
        spec = importlib.util.spec_from_file_location("postfader_config_cli", path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)
        self.assertEqual(
            module.default_interpreter("win32"),
            ROOT / ".venv" / "Scripts" / "python.exe",
        )
        self.assertEqual(
            module.default_interpreter("darwin"),
            ROOT / ".venv" / "bin" / "python",
        )


@unittest.skipUnless(os.name == "nt", "native PowerShell checks require Windows")
class NativePowerShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.powershell = shutil.which("powershell.exe")
        if cls.powershell is None:
            raise unittest.SkipTest("Windows PowerShell is unavailable")

    def run_script(self, script: str, *arguments: str):
        return subprocess.run(
            [
                self.powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                os.fspath(ROOT / "scripts" / script),
                *arguments,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_installer_dry_run_resolves_absolute_paths_without_writes(self):
        with tempfile.TemporaryDirectory(prefix="postfader install facts ") as raw:
            user_data = Path(raw) / "FL Studio User Data"
            result = self.run_script(
                "install.ps1",
                "-DryRun",
                "-Python",
                sys.executable,
                "-UserDataDir",
                os.fspath(user_data),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        facts = json.loads(result.stdout)
        self.assertEqual(Path(facts["user_data_dir"]), user_data)
        self.assertTrue(Path(facts["repository_root"]).is_absolute())
        self.assertFalse(facts["would_write_client_configuration"])
        self.assertFalse(facts["persistent_environment_changes"])
        self.assertTrue(facts["guided_setup_command"].startswith("& '"))
        self.assertIn("postfader.exe' setup", facts["guided_setup_command"])
        self.assertIn(os.fspath(ROOT), facts["guided_setup_command"])

    def test_release_bootstrap_defers_user_data_and_bridge_to_guided_setup(self):
        result = self.run_script(
            "install.ps1",
            "-DryRun",
            "-SkipBridgeDeployment",
            "-Python",
            sys.executable,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        facts = json.loads(result.stdout)
        self.assertIsNone(facts["user_data_dir"])
        self.assertFalse(facts["would_deploy_packaged_bridge"])
        self.assertFalse(facts["would_refuse_bridge_replacement"])

    def test_installer_rejects_relative_user_data_before_work(self):
        result = self.run_script(
            "install.ps1",
            "-DryRun",
            "-Python",
            sys.executable,
            "-UserDataDir",
            "relative\\FL Studio",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be an absolute", result.stderr)

    def test_installer_dry_run_refuses_injected_running_fl_studio(self):
        with tempfile.TemporaryDirectory(prefix="postfader running fl ") as raw:
            result = self.run_script(
                "install.ps1",
                "-DryRun",
                "-Python",
                sys.executable,
                "-UserDataDir",
                os.fspath(Path(raw) / "FL Studio User Data"),
                "-TestRunningFLStudioProcessId",
                "4242",
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        facts = json.loads(result.stdout)
        self.assertEqual(facts["detected_fl_studio_process_ids"], [4242])
        self.assertTrue(facts["would_refuse_bridge_replacement"])
        self.assertFalse(facts["running_fl_studio_override"])

    def test_installer_running_override_is_explicit_and_narrow(self):
        with tempfile.TemporaryDirectory(prefix="postfader running override ") as raw:
            result = self.run_script(
                "install.ps1",
                "-DryRun",
                "-Python",
                sys.executable,
                "-UserDataDir",
                os.fspath(Path(raw) / "FL Studio User Data"),
                "-TestRunningFLStudioProcessId",
                "4242",
                "-AllowBridgeReplacementWhileFLStudioRunning",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        facts = json.loads(result.stdout)
        self.assertFalse(facts["would_refuse_bridge_replacement"])
        self.assertTrue(facts["running_fl_studio_override"])

    def test_launcher_dry_run_is_read_only_by_default_and_write_is_explicit(self):
        with tempfile.TemporaryDirectory(prefix="postfader fake fl ") as raw:
            executable = Path(raw) / "FL Studio 2026" / "FakeFL64.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"not executed")
            read_only = self.run_script(
                "launch_fl_studio.ps1",
                "-DryRun",
                "-Executable",
                os.fspath(executable),
            )
            write_mode = self.run_script(
                "launch_fl_studio.ps1",
                "-DryRun",
                "-EnableWrites",
                "-Executable",
                os.fspath(executable),
            )
        self.assertEqual(read_only.returncode, 0, read_only.stderr)
        self.assertEqual(write_mode.returncode, 0, write_mode.stderr)
        read_facts = json.loads(read_only.stdout)
        write_facts = json.loads(write_mode.stdout)
        self.assertEqual(read_facts["child_write_mode"], "read-only")
        self.assertIsNone(
            read_facts["child_environment"]["FL_BRIDGE_ENABLE_WRITES"]
        )
        self.assertEqual(
            write_facts["child_environment"]["FL_BRIDGE_ENABLE_WRITES"], "1"
        )
        self.assertFalse(write_facts["persistent_environment_changes"])
        self.assertIn("would_refuse_real_launch_while_running", write_facts)
        self.assertFalse(write_facts["would_kill_or_restart_existing_process"])


class BootstrapSourceSafetyTests(unittest.TestCase):
    def test_installers_never_overwrite_client_configuration(self):
        powershell = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
        shell = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        combined = powershell + shell
        self.assertNotIn(".mcp.json\"", combined)
        self.assertNotIn("config.toml", combined)
        self.assertNotIn("claude_desktop_config", combined)
        self.assertIn("postfader.exe\"\n$GuidedSetupCommand", powershell)
        self.assertIn('"$VENV/bin/postfader"', shell)
        self.assertIn("(3, 10)", combined)
        self.assertIn("(3, 15)", combined)
        self.assertIn("--skip-bridge-deployment", shell)
        self.assertIn("SkipBridgeDeployment", powershell)
        for version in ("3.14", "3.13", "3.12", "3.11", "3.10"):
            self.assertIn("python" + version, shell)
            self.assertIn('"-' + version + '"', powershell)

    def test_launcher_never_persists_or_kills(self):
        source = (ROOT / "scripts" / "launch_fl_studio.ps1").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('"User"', source)
        self.assertNotIn('"Machine"', source)
        self.assertNotIn("Stop-Process", source)
        self.assertNotIn("taskkill", source.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
