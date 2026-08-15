#!/usr/bin/env python3
"""Small packaging/version checks for the local pre-release."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from importlib.resources import files
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT))

import fl_studio_mcp

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


def load_safe_runner():
    path = ROOT / "scripts" / "run_safe_tests.py"
    spec = importlib.util.spec_from_file_location("postfader_safe_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_distribution_verifier():
    path = ROOT / "scripts" / "verify_distribution.py"
    spec = importlib.util.spec_from_file_location(
        "postfader_distribution_verifier", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PackageHygieneTests(unittest.TestCase):
    @staticmethod
    def load_public_tree_scanner():
        scanner_path = ROOT / "scripts" / "check_public_tree.py"
        spec = importlib.util.spec_from_file_location(
            "postfader_public_tree", scanner_path
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load public-tree scanner")
        scanner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scanner)
        return scanner

    def test_private_working_directory_is_ignored_and_scanner_forbidden(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".private/", {line.strip() for line in gitignore})
        scanner = self.load_public_tree_scanner()
        with tempfile.TemporaryDirectory(prefix="postfader-public-tree-") as raw:
            root = Path(raw)
            private_file = root / ".private" / "host-report.md"
            private_file.parent.mkdir()
            private_file.write_text("local only", encoding="utf-8")
            with mock.patch.object(scanner, "ROOT", root):
                failures = scanner.check_file(Path(".private/host-report.md"))
        self.assertIn("forbidden directory: .private", failures)

    def test_windows_home_path_is_rejected_by_public_tree_scanner(self) -> None:
        scanner = self.load_public_tree_scanner()
        for separator in ("\\", "/"):
            with self.subTest(separator=separator):
                private_path = separator.join(
                    ("C:", "Users", "fixture-user", "Documents")
                )
                with tempfile.TemporaryDirectory(
                    prefix="postfader-public-tree-"
                ) as raw:
                    root = Path(raw)
                    candidate = root / "candidate.txt"
                    candidate.write_text(private_path, encoding="utf-8")
                    with mock.patch.object(scanner, "ROOT", root):
                        failures = scanner.check_file(Path("candidate.txt"))
                self.assertIn("absolute Windows home path", failures)

    def test_internal_working_documents_are_rejected_by_public_tree_scanner(
        self,
    ) -> None:
        scanner = self.load_public_tree_scanner()
        candidates = {
            "docs/demo-script.md": "demo",
            "docs/release-plan.md": "plan",
            "notes/project-handoff.txt": "handoff",
            "release-checklist.rst": "checklist",
        }
        with tempfile.TemporaryDirectory(prefix="postfader-public-tree-") as raw:
            root = Path(raw)
            for relative_text, token in candidates.items():
                with self.subTest(relative=relative_text):
                    relative = Path(relative_text)
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("internal working material", encoding="utf-8")
                    with mock.patch.object(scanner, "ROOT", root):
                        failures = scanner.check_file(relative)
                    self.assertIn(
                        "internal working-document name: %s" % token,
                        failures,
                    )

    def test_public_documentation_paths_require_an_explicit_allowlist(self) -> None:
        scanner = self.load_public_tree_scanner()
        with tempfile.TemporaryDirectory(prefix="postfader-public-tree-") as raw:
            root = Path(raw)
            candidate = root / "docs" / "new-guide.md"
            candidate.parent.mkdir(parents=True)
            candidate.write_text("unreviewed guide", encoding="utf-8")
            with mock.patch.object(scanner, "ROOT", root):
                failures = scanner.check_file(Path("docs/new-guide.md"))
        self.assertIn("unreviewed public documentation path", failures)

    def test_current_public_documentation_is_exactly_allowlisted(self) -> None:
        scanner = self.load_public_tree_scanner()
        current = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "docs").iterdir()
            if path.is_file()
        }
        self.assertEqual(current, scanner.PUBLIC_DOCUMENT_PATHS)

    def test_checkout_scripts_resolve_from_a_space_path_and_external_cwd(self) -> None:
        runner = load_safe_runner()
        scripts = (
            "scripts/inspect_readonly.py",
            "scripts/validate_selection_readonly.py",
            "scripts/validate_writes.py",
        )
        with tempfile.TemporaryDirectory(prefix="flmcp external cwd ") as cwd:
            for relative in scripts:
                with self.subTest(script=relative):
                    completed = subprocess.run(
                        [sys.executable, "-B", str(ROOT / relative), "--help"],
                        cwd=cwd,
                        env=runner.safe_child_environment(),
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    self.assertEqual(
                        completed.returncode,
                        0,
                        completed.stdout + completed.stderr,
                    )
                    self.assertIn("--midi-port", completed.stdout)

    def test_safe_runner_overrides_ambient_midi_opt_in(self) -> None:
        runner = load_safe_runner()
        with mock.patch.dict(
            os.environ,
            {
                "FL_BRIDGE_ENABLE_MIDI": "1",
                "FL_BRIDGE_ENABLE_WRITES": "1",
                "FL_BRIDGE_SANDBOXED": "0",
                "FL_BRIDGE_MIDI_PORT": "Must Not Be Enumerated",
            },
            clear=False,
        ):
            child = runner.safe_child_environment()
            self.assertEqual(child["FL_BRIDGE_ENABLE_MIDI"], "0")
            self.assertEqual(child["FL_BRIDGE_ENABLE_WRITES"], "0")
            self.assertEqual(child["FL_BRIDGE_SANDBOXED"], "1")
            self.assertEqual(
                child["FL_BRIDGE_MIDI_PORT"], "SAFE_TEST_MIDI_DISABLED"
            )
            self.assertEqual(os.environ["FL_BRIDGE_ENABLE_MIDI"], "1")
            self.assertEqual(os.environ["FL_BRIDGE_ENABLE_WRITES"], "1")
            self.assertEqual(os.environ["FL_BRIDGE_SANDBOXED"], "0")
            self.assertEqual(
                os.environ["FL_BRIDGE_MIDI_PORT"], "Must Not Be Enumerated"
            )

    def test_safe_runner_passes_isolation_and_timeout_to_every_child(self) -> None:
        runner = load_safe_runner()
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Ran 1 test\n", stderr=""
        )
        path = ROOT / "tests" / "test_bridge_stamp.py"
        with (
            mock.patch.dict(
                os.environ,
                {
                    "FL_BRIDGE_ENABLE_MIDI": "1",
                    "FL_BRIDGE_ENABLE_WRITES": "1",
                    "FL_BRIDGE_SANDBOXED": "0",
                },
                clear=False,
            ),
            mock.patch.object(runner.subprocess, "run", return_value=completed) as run,
        ):
            self.assertIs(runner.run_safe_test(path), completed)

        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["env"]["FL_BRIDGE_ENABLE_MIDI"], "0")
        self.assertEqual(kwargs["env"]["FL_BRIDGE_ENABLE_WRITES"], "0")
        self.assertEqual(kwargs["env"]["FL_BRIDGE_SANDBOXED"], "1")
        self.assertEqual(kwargs["timeout"], runner.SAFE_TEST_TIMEOUT_SECONDS)
        self.assertGreater(kwargs["timeout"], 0)

    def test_safe_runner_environment_blocks_native_midi_probe_subprocess(self) -> None:
        runner = load_safe_runner()
        source = """
from unittest import mock
from fl_studio_mcp import bridge_client
assert bridge_client.MIDI_ENABLED is False
with mock.patch.object(
    bridge_client.subprocess,
    'run',
    side_effect=AssertionError('native MIDI probe spawned'),
):
    assert bridge_client._midi_preflight('must-not-be-enumerated') is False
"""
        probe = subprocess.run(
            [sys.executable, "-B", "-c", source],
            cwd=ROOT,
            env=runner.safe_child_environment(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)

    def test_declared_runtime_and_repository_metadata_versions_match(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        expected = project["project"]["version"]
        self.assertEqual(expected, fl_studio_mcp.__version__)

        # server.json is committed release metadata, so both its server version
        # and every package version must agree with the source declaration.
        # Do not consult generated *.egg-info here: ignored editable-install
        # metadata may legitimately lag a source version bump. CI separately
        # checks the freshly built wheel's METADATA.
        manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
        self.assertEqual(expected, manifest["version"])
        self.assertEqual(
            {expected},
            {package["version"] for package in manifest["packages"]},
        )

    def test_registry_manifest_respects_published_description_limit(self) -> None:
        manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
        description = manifest["description"]
        self.assertIsInstance(description, str)
        self.assertGreater(len(description), 0)
        self.assertLessEqual(
            len(description),
            100,
            "MCP Registry rejects server descriptions longer than 100 characters",
        )

    def test_no_author_host_records_are_shipped(self) -> None:
        # An installed copy must describe the user's own FL Studio, never the
        # machine this package was built on. A dated validation record used to
        # ship here and was surfaced through fl_get_capabilities, which meant
        # every install answered with the author's host instead of its own.
        package = files("fl_studio_mcp")
        for name in ("validation_manifest.json",
                     "selection_validation_manifest.json"):
            with self.subTest(name=name):
                self.assertFalse((package / name).is_file())

    def test_entry_points_are_exactly_the_supported_four(self) -> None:
        # Pinned as a set, not merely checked for presence: a stray console
        # script is a public surface, and this file is where that gets caught.
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(
            project["project"]["scripts"],
            {
                "fl-studio-mcp": "fl_studio_mcp.mcp_server:main",
                "postfader-install-bridge": "fl_studio_mcp.bridge_install:main",
                "postfader-doctor": "fl_studio_mcp.diagnostics:main",
                "postfader-plugin-report": "fl_studio_mcp.plugin_report:main",
            },
        )

    def test_an_install_can_deploy_the_bridge_without_the_repository(self) -> None:
        # The whole reason the bridge is package data. If it stops shipping,
        # `pip install` silently produces a server that can never reach FL
        # Studio, and the failure only shows up on a user's machine.
        package = files("fl_studio_mcp")
        self.assertTrue((package / "_bridge" / "device_UniversalBridge.py").is_file())

        declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn(
            "fl_studio_mcp._bridge",
            declared["tool"]["setuptools"]["packages"],
        )

    def test_runtime_modules_do_not_import_the_fl_controller_body(self) -> None:
        # `_bridge` has no __init__.py, but its directory may still be found as
        # a PEP 420 namespace package. That is not the safety boundary. The
        # controller body imports FL-only modules at top level and therefore
        # must be executed only by FL Studio; ordinary package modules must
        # never import it. Inspect their syntax without importing the body.
        package = ROOT / "fl_studio_mcp"
        controller = package / "_bridge" / "device_UniversalBridge.py"
        self.assertTrue(controller.is_file())
        self.assertFalse((controller.parent / "__init__.py").exists())

        for module in package.glob("*.py"):
            tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    base = "." * node.level + (node.module or "")
                    imports.extend(f"{base}.{alias.name}" for alias in node.names)
            with self.subTest(module=module.name):
                self.assertFalse(
                    any("_bridge" in imported.split(".") for imported in imports),
                    f"{module.name} imports the FL-only controller body",
                )

    def test_public_metadata_and_direct_test_dependencies_are_declared(self) -> None:
        metadata = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = metadata["project"]
        self.assertEqual(project["name"], "postfader-fl-studio-mcp")
        self.assertEqual(project["readme"], "README.md")
        self.assertEqual(project["requires-python"], ">=3.10")
        self.assertIn("Programming Language :: Python :: 3.14", project["classifiers"])
        self.assertIn(
            "Operating System :: Microsoft :: Windows :: Windows 11",
            project["classifiers"],
        )
        self.assertIn("Operating System :: MacOS :: MacOS X", project["classifiers"])
        self.assertTrue(any(item.startswith("anyio") for item in project["dependencies"]))
        self.assertTrue(
            any(item.startswith("tomli") for item in project["optional-dependencies"]["test"])
        )
        self.assertEqual(
            project["urls"]["Repository"],
            "https://github.com/synopsys0/postfader-fl-studio-mcp",
        )
        self.assertEqual(
            metadata["tool"]["setuptools"]["packages"],
            ["fl_studio_mcp", "fl_studio_mcp._bridge"],
        )
        self.assertNotIn("package-data", metadata["tool"]["setuptools"])

    def test_offline_prototype_is_not_in_the_public_package(self) -> None:
        package = ROOT / "fl_studio_mcp"
        for name in ("server.py", "models.py", "project_file.py"):
            with self.subTest(name=name):
                self.assertFalse((package / name).exists())

        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertFalse(any(item.startswith("pyflp") for item in project["project"]["dependencies"]))

    def test_ci_targets_windows_and_macos_with_transport_disabled(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("windows-latest", workflow)
        self.assertIn("macos-latest", workflow)
        self.assertIn('FL_BRIDGE_ENABLE_MIDI: "0"', workflow)
        self.assertIn('FL_BRIDGE_SANDBOXED: "1"', workflow)
        self.assertNotIn("test_midi_transport.py", workflow)
        self.assertIn("scripts/verify_distribution.py", workflow)
        self.assertIn("scripts/clean_wheel_smoke.py", workflow)

    def test_release_publish_waits_for_both_native_platform_checks(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        platform = workflow.split("\n  platform-verify:\n", 1)[1].split(
            "\n  build:\n", 1
        )[0]
        build = workflow.split("\n  build:\n", 1)[1].split(
            "\n  publish:\n", 1
        )[0]
        publish = workflow.split("\n  publish:\n", 1)[1].split(
            "\n  github-release:\n", 1
        )[0]

        self.assertIn("os: [windows-latest, macos-latest]", platform)
        self.assertIn('python-version: ["3.10", "3.14"]', platform)
        self.assertIn("python-version: ${{ matrix.python-version }}", platform)
        self.assertIn("scripts/run_safe_tests.py", platform)
        self.assertIn("scripts/verify_distribution.py", platform)
        self.assertIn("scripts/clean_wheel_smoke.py", platform)
        self.assertIn("if: runner.os == 'Windows'", platform)
        self.assertIn("scripts/install.ps1", platform)
        self.assertIn("scripts/launch_fl_studio.ps1", platform)
        self.assertIn("scripts/verify_distribution.py", build)
        self.assertIn("scripts/clean_wheel_smoke.py", build)
        self.assertIn("needs: [build, platform-verify]", publish)

    def test_distribution_verifier_blocks_a_missing_ownership_marker(self) -> None:
        verifier = load_distribution_verifier()
        base_metadata = (
            "Metadata-Version: 2.4\n"
            "Name: postfader-fl-studio-mcp\n"
            "Version: 0.13.0\n"
            "License-Expression: Apache-2.0\n\n"
        )
        with tempfile.TemporaryDirectory(prefix="postfader-marker-wheel-") as raw:
            wheel = Path(raw) / "fixture.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(
                    "postfader_fl_studio_mcp-0.13.0.dist-info/METADATA",
                    base_metadata + "No ownership claim here.\n",
                )
            missing = verifier.inspect_wheel(wheel, "0.13.0")

            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(
                    "postfader_fl_studio_mcp-0.13.0.dist-info/METADATA",
                    base_metadata + verifier.MCP_OWNERSHIP_MARKER + "\n",
                )
            present = verifier.inspect_wheel(wheel, "0.13.0")

        self.assertTrue(any("ownership marker" in item for item in missing))
        self.assertFalse(any("ownership marker" in item for item in present))

    def test_distribution_verifier_pins_new_v013_runtime_modules(self) -> None:
        verifier = load_distribution_verifier()
        expected = {
            "fl_studio_mcp/acceptance.py",
            "fl_studio_mcp/client_config.py",
            "fl_studio_mcp/evidence.py",
            "fl_studio_mcp/host_config.py",
        }
        self.assertEqual(verifier.V013_REQUIRED_RUNTIME_MODULES, expected)
        self.assertLessEqual(expected, verifier.RUNTIME_MODULES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
