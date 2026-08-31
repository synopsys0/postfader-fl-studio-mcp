"""Tests for the public generic and Codex setup bundles."""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_builder():
    path = ROOT / "scripts" / "build_release_bundles.py"
    spec = importlib.util.spec_from_file_location("postfader_release_bundles", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load release bundle builder")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.builder = load_builder()
        cls.temporary = tempfile.TemporaryDirectory(prefix="postfader-release-bundles-")
        cls.output = Path(cls.temporary.name) / "first"
        cls.bundles = cls.builder.build_bundles(cls.output)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_builds_all_canonical_platform_and_codex_names(self) -> None:
        version = self.builder.project_version()
        self.assertEqual(
            [path.name for path in self.bundles],
            [
                f"PostFader-v{version}-Windows.zip",
                f"PostFader-v{version}-macOS.zip",
                f"PostFader-v{version}-Codex-Windows.zip",
                f"PostFader-v{version}-Codex-macOS.zip",
            ],
        )

    def test_bundles_pass_their_centralized_inspector(self) -> None:
        version = self.builder.project_version()
        for (platform, codex), bundle in zip(
            self.builder.BUNDLE_SPECS,
            self.bundles,
        ):
            with self.subTest(platform=platform, codex=codex):
                self.assertEqual(
                    self.builder.inspect_bundle(
                        bundle,
                        platform=platform,
                        version=version,
                        codex=codex,
                    ),
                    [],
                )

    def test_bundles_exclude_tests_private_state_and_build_outputs(self) -> None:
        for bundle in self.bundles:
            with zipfile.ZipFile(bundle) as archive:
                members = archive.namelist()
            for forbidden in ("/.private/", "/.github/", "/tests/", "/dist/"):
                with self.subTest(bundle=bundle.name, forbidden=forbidden):
                    self.assertFalse(any(forbidden in name for name in members))

    def test_source_inventory_uses_git_tracked_files_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="postfader-bundle-source-") as raw:
            root = Path(raw)
            subprocess.run(
                ["git", "init", "--quiet", str(root)],
                check=True,
                capture_output=True,
            )
            for relative_text in self.builder.ROOT_FILES:
                path = root / relative_text
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("placeholder\n", encoding="utf-8")
            for directory_text in self.builder.SOURCE_DIRECTORIES:
                (root / directory_text).mkdir(parents=True, exist_ok=True)
            tracked = root / "docs" / "tracked.md"
            ignored = root / "docs" / "local-secret.env"
            tracked.write_text("public\n", encoding="utf-8")
            ignored.write_text("must not ship\n", encoding="utf-8")
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "add",
                    "--",
                    *self.builder.ROOT_FILES,
                    "docs/tracked.md",
                ],
                check=True,
                capture_output=True,
            )

            inventory = self.builder._source_files(root)

        self.assertIn(Path("docs/tracked.md"), inventory)
        self.assertNotIn(Path("docs/local-secret.env"), inventory)

    def test_inspector_rejects_any_unexpected_archive_member(self) -> None:
        version = self.builder.project_version()
        source = next(path for path in self.bundles if path.name.endswith("-macOS.zip"))
        forged = Path(self.temporary.name) / "forged.zip"
        forged.write_bytes(source.read_bytes())
        root = f"PostFader-v{version}-macOS"
        with zipfile.ZipFile(forged, "a") as archive:
            archive.writestr(f"{root}/docs/unreviewed-local-note.txt", "private")

        failures = self.builder.inspect_bundle(
            forged,
            platform="macOS",
            version=version,
        )

        self.assertTrue(
            any("unexpected member is present" in failure for failure in failures),
            failures,
        )

    def test_installers_and_start_guides_are_obvious_top_level_entries(self) -> None:
        version = self.builder.project_version()
        expected = {
            "Windows": ("Install PostFader.cmd", "START HERE - Windows.md"),
            "macOS": ("Install PostFader.command", "START HERE - macOS.md"),
        }
        for platform, bundle in zip(("Windows", "macOS"), self.bundles):
            root = f"PostFader-v{version}-{platform}"
            with zipfile.ZipFile(bundle) as archive:
                names = set(archive.namelist())
                guide = archive.read(f"{root}/{expected[platform][1]}").decode("utf-8")
            self.assertIn(f"{root}/{expected[platform][0]}", names)
            self.assertIn(f"{root}/fl_studio_mcp/cli.py", names)
            self.assertIn(f"{root}/fl_studio_mcp/setup_wizard.py", names)
            self.assertIn(f"{root}/fl_studio_mcp/plugin_atlas/__init__.py", names)
            self.assertIn(f"{root}/fl_studio_mcp/plugin_atlas_mcp.py", names)
            self.assertIn(
                f"{root}/fl_studio_mcp/plugin_atlas_data/manifests/atlas.json",
                names,
            )
            self.assertIn("starts read-only", guide)
            self.assertIn("postfader-doctor", guide)
            self.assertIn("postfader", guide)
            self.assertIn(" setup", guide)
            self.assertIn("The one FL Studio action you must complete", guide)
            self.assertIn("PostFader remains installed", guide)
            self.assertIn("does not install or configure a virtual MIDI", guide)
            self.assertIn("Setup never enables write mode", guide)
            self.assertIn("PowerShell", guide)

    def test_codex_bundles_have_targeted_launchers_and_registration_guides(self) -> None:
        version = self.builder.project_version()
        expected = {
            "Windows": "Install PostFader for Codex.cmd",
            "macOS": "Install PostFader for Codex.command",
        }
        for platform, bundle in zip(("Windows", "macOS"), self.bundles[2:]):
            root = f"PostFader-v{version}-Codex-{platform}"
            guide_name = f"START HERE - {platform}.md"
            with zipfile.ZipFile(bundle) as archive:
                names = set(archive.namelist())
                guide = archive.read(f"{root}/{guide_name}").decode("utf-8")
                launcher = archive.read(f"{root}/{expected[platform]}").decode("utf-8")
            self.assertIn(f"{root}/{expected[platform]}", names)
            self.assertIn("--client codex-toml --register-codex", launcher)
            self.assertIn("codex mcp add", guide)
            self.assertIn("separate confirmation", guide)
            self.assertIn("refuses to replace a different entry", guide)
            self.assertIn("manual fallback", guide)

    def test_launchers_start_guided_setup_without_rolling_back_bootstrap(self) -> None:
        windows = self.builder._windows_launcher("test").decode("utf-8")
        macos = self.builder._macos_launcher("test").decode("utf-8")

        windows_setup = '"%~dp0.venv\\Scripts\\postfader.exe" setup --interactive'
        self.assertIn(windows_setup, windows)
        self.assertGreater(
            windows.index(windows_setup),
            windows.rindex('scripts\\install.ps1" -SkipBridgeDeployment'),
        )
        self.assertIn(
            "PostFader is installed, but guided setup is not complete yet.",
            windows,
        )
        self.assertIn("goto installed", windows)
        self.assertIn(":installed\r\n", windows)
        self.assertIn("exit /b 0", windows[windows.index(":installed\r\n") :])

        macos_setup = '"$POSTFADER_RELEASE_DIR/.venv/bin/postfader" setup --interactive'
        self.assertIn(macos_setup, macos)
        self.assertGreater(
            macos.index(macos_setup),
            macos.index('bash "$POSTFADER_RELEASE_DIR/scripts/install.sh"'),
        )
        self.assertIn(
            "PostFader is installed, but guided setup is not complete yet.",
            macos,
        )
        setup_branch = macos[macos.index(macos_setup) :]
        self.assertIn("POSTFADER_STATUS=0", setup_branch)

        codex_windows = self.builder._windows_launcher("test", codex=True).decode(
            "utf-8"
        )
        codex_macos = self.builder._macos_launcher("test", codex=True).decode("utf-8")
        codex_args = "setup --interactive --client codex-toml --register-codex"
        self.assertIn(codex_args, codex_windows)
        self.assertEqual(codex_windows.count(codex_args), 2)
        self.assertIn(codex_args, codex_macos)
        self.assertEqual(codex_macos.count(codex_args), 2)

    def test_macos_launcher_and_shell_installer_are_executable(self) -> None:
        version = self.builder.project_version()
        bundle = next(path for path in self.bundles if path.name.endswith("-macOS.zip"))
        root = f"PostFader-v{version}-macOS"
        with zipfile.ZipFile(bundle) as archive:
            for relative in ("Install PostFader.command", "scripts/install.sh"):
                mode = archive.getinfo(f"{root}/{relative}").external_attr >> 16
                self.assertNotEqual(mode & 0o111, 0, relative)

        codex_bundle = next(
            path for path in self.bundles if path.name.endswith("-Codex-macOS.zip")
        )
        codex_root = f"PostFader-v{version}-Codex-macOS"
        with zipfile.ZipFile(codex_bundle) as archive:
            for relative in (
                "Install PostFader for Codex.command",
                "scripts/install.sh",
            ):
                mode = archive.getinfo(f"{codex_root}/{relative}").external_attr >> 16
                self.assertNotEqual(mode & 0o111, 0, relative)

    def test_generated_launchers_have_non_mutating_ci_entry_points(self) -> None:
        windows = self.builder._windows_launcher("test").decode("utf-8")
        macos = self.builder._macos_launcher("test").decode("utf-8")
        self.assertIn("POSTFADER_BUNDLE_DRY_RUN", windows)
        self.assertIn(
            'scripts\\install.ps1" -DryRun -SkipBridgeDeployment', windows
        )
        self.assertIn(
            'scripts\\install.ps1" -SkipBridgeDeployment', windows
        )
        self.assertIn("POSTFADER_BUNDLE_DRY_RUN", macos)
        self.assertIn("bash -n", macos)
        self.assertNotIn("command -v python3", macos)
        self.assertIn(
            'bash "$POSTFADER_RELEASE_DIR/scripts/install.sh" '
            "--skip-bridge-deployment",
            macos,
        )
        self.assertLess(
            windows.index('if "%POSTFADER_BUNDLE_DRY_RUN%"=="1" exit /b 0'),
            windows.index('"%~dp0.venv\\Scripts\\postfader.exe" setup --interactive'),
        )
        self.assertLess(
            macos.index('if [ "${POSTFADER_BUNDLE_DRY_RUN:-0}" = "1" ]'),
            macos.index('"$POSTFADER_RELEASE_DIR/.venv/bin/postfader" setup --interactive'),
        )
        for launcher in (
            self.builder._windows_launcher("test", codex=True).decode("utf-8"),
            self.builder._macos_launcher("test", codex=True).decode("utf-8"),
        ):
            self.assertIn("POSTFADER_BUNDLE_DRY_RUN", launcher)
            self.assertLess(
                launcher.index("POSTFADER_BUNDLE_DRY_RUN"),
                launcher.index("--register-codex"),
            )

    def test_repeated_builds_are_byte_for_byte_reproducible(self) -> None:
        second = Path(self.temporary.name) / "second"
        rebuilt = self.builder.build_bundles(second)
        first_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in self.bundles]
        second_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in rebuilt]
        self.assertEqual(first_hashes, second_hashes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
