#!/usr/bin/env python3
"""Small packaging/version checks for the local pre-release."""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT))

import fl_studio_mcp  # noqa: E402


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
        self.assertIn("release-bundles/", {line.strip() for line in gitignore})
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

    def test_local_conversations_and_run_journals_cannot_enter_public_tree(self) -> None:
        scanner = self.load_public_tree_scanner()
        candidates = (
            ".codex/config.toml", "conversation.jsonl", "session.log",
            "production-runs-v1.sqlite3", "runs.sqlite3-wal", "runs.db-shm",
        )
        with tempfile.TemporaryDirectory(prefix="postfader-public-tree-") as raw:
            root = Path(raw)
            for name in candidates:
                with self.subTest(name=name):
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("synthetic private record", encoding="utf-8")
                    with mock.patch.object(scanner, "ROOT", root):
                        self.assertTrue(scanner.check_file(Path(name)))
                    ignored = subprocess.run(
                        ["git", "check-ignore", "--no-index", name],
                        cwd=ROOT, capture_output=True, check=False,
                    )
                    self.assertEqual(ignored.returncode, 0)

    def test_github_credential_families_are_rejected_without_echoing_secrets(self) -> None:
        scanner = self.load_public_tree_scanner()
        with tempfile.TemporaryDirectory(prefix="postfader-public-tree-") as raw:
            root = Path(raw)
            for family in "pousr":
                with self.subTest(family=family):
                    (root / "candidate.txt").write_text(
                        "gh" + family + "_" + "a" * 24, encoding="utf-8",
                    )
                    with mock.patch.object(scanner, "ROOT", root):
                        self.assertIn("GitHub token", scanner.check_file(Path("candidate.txt")))

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

    def test_reviewed_maintainability_plan_is_the_only_plan_name_exception(self) -> None:
        scanner = self.load_public_tree_scanner()
        with tempfile.TemporaryDirectory(prefix="postfader-public-tree-") as raw:
            root = Path(raw)
            reviewed = root / "docs" / "maintainability-plan.md"
            reviewed.parent.mkdir(parents=True)
            reviewed.write_text("reviewed public guidance", encoding="utf-8")
            with mock.patch.object(scanner, "ROOT", root):
                self.assertEqual(
                    scanner.check_file(Path("docs/maintainability-plan.md")), []
                )

                lookalike = root / "docs" / "archive" / "maintainability-plan.md"
                lookalike.parent.mkdir(parents=True)
                lookalike.write_text("internal working material", encoding="utf-8")
                failures = scanner.check_file(
                    Path("docs/archive/maintainability-plan.md")
                )
            self.assertIn("internal working-document name: plan", failures)
            self.assertIn("unreviewed public documentation path", failures)

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

    def test_every_offline_test_is_included_in_the_required_suite(self) -> None:
        runner = load_safe_runner()
        offline = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "tests").glob("test_*.py")
        } - {"tests/test_midi_transport.py"}
        self.assertEqual(set(runner.SAFE_TESTS), offline)
        self.assertEqual(len(runner.SAFE_TESTS), len(offline))

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
        journal = Path(kwargs["env"]["POSTFADER_PRODUCTION_RUN_PATH"])
        self.assertEqual(journal.name, "production-runs.sqlite3")
        self.assertFalse(journal.parent.exists(), "test journal directory must be temporary")

    def test_safe_runner_can_instrument_children_for_coverage(self) -> None:
        runner = load_safe_runner()
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Ran 1 test\n", stderr=""
        )
        path = ROOT / "tests" / "test_bridge_stamp.py"
        with tempfile.TemporaryDirectory(prefix="postfader-coverage-") as raw:
            with (
                mock.patch.dict(
                    os.environ,
                    {runner.COVERAGE_DIRECTORY_ENV: raw},
                    clear=False,
                ),
                mock.patch.object(
                    runner.subprocess, "run", return_value=completed
                ) as run,
            ):
                self.assertIs(runner.run_safe_test(path), completed)

            command = run.call_args.args[0]
            self.assertEqual(
                command[:5],
                [sys.executable, "-m", "coverage", "run", "--parallel-mode"],
            )
            self.assertEqual(command[5], os.fspath(path))
            self.assertEqual(
                run.call_args.kwargs["env"]["COVERAGE_FILE"],
                os.fspath(Path(raw) / ".coverage"),
            )

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

        modules = list(package.glob("*.py"))
        modules.extend((package / "plugin_atlas").rglob("*.py"))
        modules.extend((package / "plugin_atlas_data").rglob("*.py"))
        modules.extend((package / "sound_selection").rglob("*.py"))
        modules.extend((package / "creation_pipeline").rglob("*.py"))
        modules.extend((package / "creation_review").rglob("*.py"))
        for module in modules:
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

    def test_workflow_actions_are_pinned_by_commit(self) -> None:
        unpinned = re.compile(r"uses:\s+[^\s]+@(v\d+|release/v\d+)\s*$")
        for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            with self.subTest(workflow=workflow.name):
                text = workflow.read_text(encoding="utf-8")
                self.assertNotIn("test_midi_transport.py", text)
                self.assertEqual(
                    [line for line in text.splitlines() if unpinned.search(line)],
                    [],
                )

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

    def test_sdist_verification_requires_every_runtime_module(self) -> None:
        verifier = load_distribution_verifier()
        required = (
            verifier.RUNTIME_MODULES
            | verifier.ATLAS_DATA_FILES
            | verifier.SOUND_SELECTION_DATA_FILES
            | {name.lstrip("/") for name in verifier.SDIST_REQUIRED_SUFFIXES}
        )
        missing_module = "fl_studio_mcp/plugin_loading.py"
        self.assertIn(missing_module, required)
        with tempfile.TemporaryDirectory(prefix="postfader-sdist-check-") as raw:
            archive_path = Path(raw) / "source.tar.gz"
            for omit in (None, missing_module):
                with tarfile.open(archive_path, "w:gz") as archive:
                    for name in sorted(required - {omit}):
                        content = (
                            verifier.MCP_OWNERSHIP_MARKER.encode("utf-8")
                            if name == "README.md" else b"fixture"
                        )
                        member = tarfile.TarInfo("package/" + name)
                        member.size = len(content)
                        archive.addfile(member, io.BytesIO(content))
                failures = verifier.inspect_sdist(archive_path)
                if omit is None:
                    self.assertEqual(failures, [])
                else:
                    self.assertEqual(failures, ["sdist is missing " + missing_module])


if __name__ == "__main__":
    unittest.main(verbosity=2)
