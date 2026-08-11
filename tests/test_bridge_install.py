"""Cover the packaged bridge deployment used by postfader-install-bridge.

Everything here runs against temporary directories. Nothing touches a real FL
Studio user-data folder, CoreMIDI, or the IAC bus.

The point of these checks is that a package install and a clone install put the
*same bytes* in the same place. `scripts/install.sh` delegates to this module
precisely so the two cannot drift, and that guarantee is only worth anything if
something asserts it.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from fl_studio_mcp import bridge_install  # noqa: E402
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

    def test_the_packaged_bridge_is_not_importable(self):
        # It calls FL Studio's embedded API at import time. Reaching it with a
        # plain import from anywhere else would fail confusingly, so it must
        # stay package data rather than a submodule.
        self.assertFalse((bridge_install.bridge_source_path().parent / "__init__.py").exists())

    def test_the_packaged_bridge_is_ascii(self):
        # FL Studio loads MIDI scripts through an ASCII code path.
        raw = bridge_install.bridge_source_path().read_bytes()
        self.assertTrue(all(byte < 128 for byte in raw))


class UserDataResolutionTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("FL_STUDIO_USER_DATA_DIR")
        os.environ.pop("FL_STUDIO_USER_DATA_DIR", None)

    def tearDown(self):
        os.environ.pop("FL_STUDIO_USER_DATA_DIR", None)
        if self._saved is not None:
            os.environ["FL_STUDIO_USER_DATA_DIR"] = self._saved

    def test_default_location(self):
        self.assertEqual(
            bridge_install.user_data_dir(),
            Path("~/Documents/Image-Line/FL Studio").expanduser(),
        )

    def test_environment_override(self):
        os.environ["FL_STUDIO_USER_DATA_DIR"] = "/tmp/some-fl-folder"
        self.assertEqual(bridge_install.user_data_dir(), Path("/tmp/some-fl-folder"))

    def test_explicit_argument_beats_the_environment(self):
        os.environ["FL_STUDIO_USER_DATA_DIR"] = "/tmp/from-env"
        self.assertEqual(
            bridge_install.user_data_dir("/tmp/from-argument"),
            Path("/tmp/from-argument"),
        )

    def test_target_sits_in_fl_studios_controller_folder(self):
        target = bridge_install.target_path("/tmp/fl")
        self.assertEqual(
            target,
            Path("/tmp/fl/Settings/Hardware/Universal Bridge/device_UniversalBridge.py"),
        )


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

    def test_print_source_names_the_packaged_bridge(self):
        self.assertEqual(bridge_install.main(["--print-source"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
