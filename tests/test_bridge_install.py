"""Cover the packaged bridge deployment used by postfader-install-bridge.

Everything here runs against temporary directories. Nothing touches a real FL
Studio user-data folder, CoreMIDI, or the IAC bus.

The point of these checks is that a package install and a clone install put the
*same bytes* in the same place. `scripts/install.sh` delegates to this module
precisely so the two cannot drift, and that guarantee is only worth anything if
something asserts it.
"""

from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

# Importing the advisory surface loads NumPy and SciPy. A fresh Windows runner
# can spend well over ten seconds loading those native modules for the first
# time, especially while Defender scans newly installed wheels. Keep this
# subprocess bounded without making a cold, otherwise-successful import flaky.
COLD_IMPORT_TIMEOUT_SECONDS = 60

from fl_studio_mcp import bridge_install  # noqa: E402
from fl_studio_mcp import host_config  # noqa: E402
from fl_studio_mcp.bridge_stamp import stamp_bridge_source  # noqa: E402


class BridgeSourceTests(unittest.TestCase):
    def test_the_bridge_ships_inside_the_package(self):
        # A pip user has no repository, so the script FL loads has to travel
        # with the package or the install is unusable.
        source = bridge_install.bridge_source_path()
        self.assertTrue(source.is_file())
        self.assertEqual(source.name, "device_UniversalBridge.py")
        self.assertEqual(source.parent.name, "_bridge")
        self.assertEqual(source.parent.parent.name, "fl_studio_mcp")

    def test_the_bridge_directory_has_no_package_initializer(self):
        # Python may expose this directory as a namespace, but the controller
        # module body depends on FL-only modules. Keep it as deployable data;
        # the MCP process must not treat it as a normal Python subpackage.
        self.assertFalse((bridge_install.bridge_source_path().parent / "__init__.py").exists())

    def test_the_packaged_bridge_is_ascii(self):
        # FL Studio loads MIDI scripts through an ASCII code path.
        raw = bridge_install.bridge_source_path().read_bytes()
        self.assertTrue(all(byte < 128 for byte in raw))

    def test_expected_deployment_uses_the_same_stamped_bytes_and_digest(self):
        source = bridge_install.bridge_source_path().read_bytes()
        expected_bytes, expected_digest = stamp_bridge_source(source)

        deployed_bytes, deployed_digest = bridge_install.expected_bridge_deployment()

        self.assertEqual(deployed_bytes, expected_bytes)
        self.assertEqual(deployed_digest, expected_digest)
        self.assertEqual(len(deployed_digest), 64)


class UserDataResolutionTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("FL_STUDIO_USER_DATA_DIR")
        os.environ.pop("FL_STUDIO_USER_DATA_DIR", None)
        self._tmp = tempfile.TemporaryDirectory(
            prefix="flmcp-user-data-resolution-"
        )
        self.root = Path(self._tmp.name)

    def tearDown(self):
        os.environ.pop("FL_STUDIO_USER_DATA_DIR", None)
        if self._saved is not None:
            os.environ["FL_STUDIO_USER_DATA_DIR"] = self._saved
        self._tmp.cleanup()

    def test_default_location(self):
        self.assertEqual(
            bridge_install.user_data_dir(),
            host_config.default_fl_studio_user_data_dir(),
        )

    def test_environment_override(self):
        configured = self.root / "some-fl-folder"
        os.environ["FL_STUDIO_USER_DATA_DIR"] = os.fspath(configured)
        self.assertEqual(bridge_install.user_data_dir(), configured)

    def test_explicit_argument_beats_the_environment(self):
        environment = self.root / "from-env"
        explicit = self.root / "from-argument"
        os.environ["FL_STUDIO_USER_DATA_DIR"] = os.fspath(environment)
        self.assertEqual(
            bridge_install.user_data_dir(explicit),
            explicit,
        )

    def test_target_sits_in_fl_studios_controller_folder(self):
        user_data = self.root / "fl"
        target = bridge_install.target_path(user_data)
        self.assertEqual(
            target,
            user_data
            / "Settings/Hardware/Universal Bridge/device_UniversalBridge.py",
        )


class HostConfigurationTests(unittest.TestCase):
    def test_relative_explicit_user_data_path_is_rejected(self):
        with self.assertRaises(host_config.HostConfigurationError) as raised:
            host_config.fl_studio_user_data_selection(
                "relative/FL Studio", environ={}
            )
        message = str(raised.exception)
        self.assertIn("explicit --user-data-dir", message)
        self.assertIn("absolute", message)
        self.assertIn("working directories", message)

    def test_relative_environment_user_data_path_is_rejected(self):
        with self.assertRaises(host_config.HostConfigurationError) as raised:
            host_config.fl_studio_user_data_selection(
                environ={"FL_STUDIO_USER_DATA_DIR": "relative/FL Studio"}
            )
        message = str(raised.exception)
        self.assertIn("FL_STUDIO_USER_DATA_DIR", message)
        self.assertIn("absolute", message)
        self.assertIn("working directories", message)

    def test_absolute_configured_user_data_path_need_not_exist(self):
        with tempfile.TemporaryDirectory(prefix="flmcp-nonexistent-root-") as raw:
            configured = Path(raw) / "does-not-exist" / "FL Studio"
            selected = host_config.fl_studio_user_data_selection(
                configured, environ={}
            )
        self.assertEqual(selected.path, configured)
        self.assertEqual(selected.source, "explicit")

    def test_redirected_and_unredirected_windows_documents(self):
        with tempfile.TemporaryDirectory(prefix="flmcp-windows-home-") as raw:
            home = Path(raw) / "Profile With Spaces"
            redirected = home / "Cloud Drive" / "Documents"
            with mock.patch.object(
                host_config,
                "windows_known_documents_dir",
                return_value=redirected,
            ):
                selected = host_config.fl_studio_user_data_selection(
                    environ={}, platform_name="win32", home=home
                )
            self.assertEqual(
                selected.path, redirected / "Image-Line" / "FL Studio"
            )
            self.assertEqual(selected.source, "windows-known-documents")

            unredirected = home / "Documents"
            with mock.patch.object(
                host_config,
                "windows_known_documents_dir",
                return_value=unredirected,
            ):
                selected = host_config.fl_studio_user_data_selection(
                    environ={}, platform_name="win32", home=home
                )
            self.assertEqual(
                selected.path, unredirected / "Image-Line" / "FL Studio"
            )

            with mock.patch.object(
                host_config,
                "windows_known_documents_dir",
                return_value=None,
            ):
                fallback = host_config.fl_studio_user_data_selection(
                    environ={}, platform_name="win32", home=home
                )
            self.assertEqual(
                fallback.path,
                home / "Documents" / "Image-Line" / "FL Studio",
            )
            self.assertEqual(fallback.source, "home-documents")

    def test_explicit_then_environment_then_platform_default_precedence(self):
        with tempfile.TemporaryDirectory(prefix="flmcp-config-paths-") as raw:
            root = Path(raw)
            explicit = root / "Explicit FL Folder"
            environment = root / "Environment FL Folder"
            known_documents = root / "Redirected Documents"
            values = {"FL_STUDIO_USER_DATA_DIR": os.fspath(environment)}
            with mock.patch.object(
                host_config,
                "windows_known_documents_dir",
                return_value=known_documents,
            ):
                first = host_config.fl_studio_user_data_selection(
                    explicit, environ=values, platform_name="win32"
                )
                second = host_config.fl_studio_user_data_selection(
                    environ=values, platform_name="win32"
                )
                third = host_config.fl_studio_user_data_selection(
                    environ={}, platform_name="win32"
                )

            self.assertEqual((first.path, first.source), (explicit, "explicit"))
            self.assertEqual(
                (second.path, second.source), (environment, "environment")
            )
            self.assertEqual(
                (third.path, third.source),
                (
                    known_documents / "Image-Line" / "FL Studio",
                    "windows-known-documents",
                ),
            )

    def test_macos_keeps_iac_default_and_windows_requires_a_port(self):
        with tempfile.TemporaryDirectory(prefix="flmcp-macos-home-") as raw:
            mac_home = Path(raw) / "Mac Home With Spaces"
            mac_user_data = host_config.fl_studio_user_data_selection(
                environ={}, platform_name="darwin", home=mac_home
            )
            windows_user_data = Path(raw) / "Windows FL Data"
            with self.assertRaises(host_config.HostConfigurationError):
                host_config.mcp_server_environment(
                    user_data_dir=windows_user_data,
                    environ={},
                    platform_name="win32",
                )
        self.assertEqual(
            mac_user_data.path,
            mac_home / "Documents" / "Image-Line" / "FL Studio",
        )
        self.assertEqual(mac_user_data.source, "home-documents")
        self.assertEqual(
            host_config.midi_port_query(environ={}, platform_name="darwin"),
            "IAC Driver",
        )
        self.assertIsNone(
            host_config.midi_port_query(environ={}, platform_name="win32")
        )
        self.assertIn("IAC", bridge_install.midi_setup_epilog("darwin"))
        with mock.patch.dict(os.environ, {}, clear=True):
            windows_help = bridge_install.midi_setup_epilog("win32")
        self.assertIn("FL_BRIDGE_MIDI_PORT", windows_help)
        self.assertIn("does not install or configure", windows_help)
        self.assertNotIn("IAC", windows_help)

    def test_midi_port_explicit_value_beats_the_environment(self):
        values = {"FL_BRIDGE_MIDI_PORT": "Environment Loopback"}
        self.assertEqual(
            host_config.midi_port_query(
                "Explicit Loopback", environ=values, platform_name="win32"
            ),
            "Explicit Loopback",
        )
        self.assertEqual(
            host_config.midi_port_query(
                environ=values, platform_name="win32"
            ),
            "Environment Loopback",
        )

    def test_generated_mcp_environment_carries_shared_paths_and_endpoint(self):
        with tempfile.TemporaryDirectory(prefix="flmcp-mcp-env-") as raw:
            user_data = Path(raw) / "FL Data With Spaces"
            environment = host_config.mcp_server_environment(
                user_data_dir=user_data,
                midi_port="Configured Loopback With Spaces",
                environ={},
                platform_name="win32",
            )
        self.assertEqual(
            environment,
            {
                "FL_BRIDGE_ENABLE_MIDI": "1",
                "FL_BRIDGE_MIDI_PORT": "Configured Loopback With Spaces",
                "FL_STUDIO_USER_DATA_DIR": os.fspath(user_data),
            },
        )
        self.assertTrue(
            Path(environment["FL_STUDIO_USER_DATA_DIR"]).is_absolute()
        )
        self.assertNotIn("FL_BRIDGE_ENABLE_WRITES", environment)

    def test_missing_windows_endpoint_is_refused_without_a_native_probe(self):
        from fl_studio_mcp import diagnostics

        saved_problems = list(diagnostics.problems)
        diagnostics.problems.clear()
        try:
            with (
                mock.patch.object(diagnostics, "MIDI_PORT_QUERY", None),
                mock.patch.object(
                    diagnostics.subprocess,
                    "run",
                    side_effect=AssertionError("native MIDI probe spawned"),
                ) as probe,
            ):
                diagnostics.check_midi_ports()
            self.assertTrue(
                any("endpoint" in problem for problem in diagnostics.problems)
            )
            probe.assert_not_called()
        finally:
            diagnostics.problems[:] = saved_problems

    @unittest.skipUnless(os.name == "nt", "Windows Known Documents host proof")
    def test_native_windows_known_documents_is_absolute_and_used_by_installer(self):
        documents = host_config.windows_known_documents_dir()
        self.assertIsNotNone(documents)
        self.assertTrue(documents.is_absolute())
        self.assertEqual(
            bridge_install.user_data_dir(),
            documents / "Image-Line" / "FL Studio",
        )

    def test_user_data_override_reaches_every_runtime_consumer(self):
        with tempfile.TemporaryDirectory(prefix="flmcp-shared-root-") as raw:
            user_data = Path(raw) / "FL Studio User Data With Spaces"
            child_environment = os.environ.copy()
            child_environment.update(
                {
                    "FL_BRIDGE_ENABLE_MIDI": "0",
                    "FL_BRIDGE_SANDBOXED": "1",
                    "FL_STUDIO_USER_DATA_DIR": os.fspath(user_data),
                }
            )
            child_environment.pop("FL_BRIDGE_MAILBOX", None)
            source = """
import json
import os
from fl_studio_mcp import advisory, bridge_client, bridge_install, diagnostics
root = bridge_install.user_data_dir()
print(json.dumps({
    'installer': os.fspath(root),
    'diagnostics': diagnostics.FL_STUDIO_USER_DATA_DIR,
    'advisory': os.fspath(advisory.FL_STUDIO_USER_ROOT),
    'mailboxes': bridge_client.MAILBOX_CANDIDATES,
}))
"""
            completed = subprocess.run(
                [sys.executable, "-B", "-c", source],
                cwd=ROOT,
                env=child_environment,
                capture_output=True,
                text=True,
                timeout=COLD_IMPORT_TIMEOUT_SECONDS,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        expected = os.fspath(user_data)
        self.assertEqual(payload["installer"], expected)
        self.assertEqual(payload["diagnostics"], expected)
        self.assertEqual(payload["advisory"], expected)
        self.assertIn(
            os.fspath(user_data / "Settings" / "Hardware" / "Universal Bridge"),
            payload["mailboxes"],
        )
        self.assertIn(os.fspath(user_data / "Settings"), payload["mailboxes"])

    def test_clone_installer_uses_shared_paths_without_overwriting_client_config(self):
        source = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("fl_studio_user_data_dir", source)
        self.assertNotIn("$HOME/Documents/Image-Line/FL Studio", source)
        self.assertNotIn(".mcp.json", source)
        self.assertIn('"$VENV/bin/postfader"', source)
        self.assertIn("--skip-bridge-deployment", source)


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fl = Path(self.tmp.name) / "FL Studio"
        (self.fl / "Settings" / "Hardware").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_hardware_folder_is_refused_with_an_actionable_message(self):
        empty = Path(self.tmp.name) / "never-launched"
        empty.mkdir()
        with self.assertRaises(bridge_install.BridgeInstallError) as refused:
            bridge_install.deploy(str(empty))
        message = str(refused.exception)
        self.assertIn("Hardware folder", message)
        self.assertIn("Launch FL Studio once", message)

    def test_first_deploy_writes_the_stamped_bridge(self):
        outcome = bridge_install.deploy(str(self.fl))
        self.assertTrue(outcome["changed"])
        self.assertIsNone(outcome["backup"])
        installed = outcome["target"].read_bytes()
        expected, digest = stamp_bridge_source(
            bridge_install.bridge_source_path().read_bytes())
        self.assertEqual(installed, expected)
        self.assertEqual(outcome["digest"], digest)
        # The stamp is what lets a client detect a stale deployed copy.
        self.assertIn(digest.encode(), installed)

    def test_redeploying_unchanged_makes_no_backup(self):
        bridge_install.deploy(str(self.fl))
        outcome = bridge_install.deploy(str(self.fl))
        self.assertFalse(outcome["changed"])
        self.assertIsNone(outcome["backup"])
        strays = list(outcome["target"].parent.glob("*.bak-*"))
        self.assertEqual(strays, [], strays)

    def test_a_different_installed_bridge_is_backed_up_before_replacement(self):
        outcome = bridge_install.deploy(str(self.fl))
        outcome["target"].write_bytes(b"# someone else's bridge\n")
        second = bridge_install.deploy(str(self.fl))
        self.assertTrue(second["changed"])
        self.assertIsNotNone(second["backup"])
        self.assertEqual(second["backup"].read_bytes(), b"# someone else's bridge\n")

    def test_deploy_creates_the_controller_subfolder(self):
        controller = self.fl / "Settings" / "Hardware" / "Universal Bridge"
        self.assertFalse(controller.exists())
        bridge_install.deploy(str(self.fl))
        self.assertTrue(controller.is_dir())


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fl = Path(self.tmp.name) / "FL Studio"
        (self.fl / "Settings" / "Hardware").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_command_succeeds_and_is_repeatable(self):
        self.assertEqual(bridge_install.main(["--user-data-dir", str(self.fl)]), 0)
        self.assertEqual(bridge_install.main(["--user-data-dir", str(self.fl)]), 0)

    def test_command_reports_failure_without_raising(self):
        empty = Path(self.tmp.name) / "never-launched"
        empty.mkdir()
        self.assertEqual(bridge_install.main(["--user-data-dir", str(empty)]), 1)

    def test_command_reports_relative_configuration_without_traceback(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = bridge_install.main(
                ["--user-data-dir", "relative/FL Studio"]
            )
        self.assertEqual(status, 1)
        self.assertIn("must be an absolute", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_print_source_names_the_packaged_bridge(self):
        self.assertEqual(bridge_install.main(["--print-source"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
