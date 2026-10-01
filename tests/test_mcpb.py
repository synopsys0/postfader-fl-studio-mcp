#!/usr/bin/env python3
"""Hermetic checks for the Claude Desktop MCPB packaging surface."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_mcpb  # noqa: E402
from build_mcpb import MCPB_NPM_PACKAGE  # noqa: E402
from check_mcpb_bundle import inspect_bundle  # noqa: E402
from sync_mcpb_manifest import discover_tools  # noqa: E402


class MCPBPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(
            (ROOT / "manifest.json").read_text(encoding="utf-8")
        )

    def test_manifest_version_matches_python_package(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        package_spec = importlib.util.spec_from_file_location(
            "postfader_version_only", ROOT / "fl_studio_mcp" / "__init__.py"
        )
        self.assertIsNotNone(package_spec)
        module = importlib.util.module_from_spec(package_spec)
        assert package_spec is not None and package_spec.loader is not None
        package_spec.loader.exec_module(module)
        self.assertEqual(self.manifest["version"], project["project"]["version"])
        self.assertEqual(self.manifest["version"], module.__version__)
        self.assertEqual(
            self.manifest["compatibility"]["runtimes"]["python"],
            project["project"]["requires-python"],
        )

    def test_manifest_tools_match_runtime_decorators(self) -> None:
        self.assertEqual(self.manifest["tools"], discover_tools())
        names = [tool["name"] for tool in self.manifest["tools"]]
        self.assertEqual(len(names), len(set(names)))

    def test_every_runtime_tool_has_protocol_annotations(self) -> None:
        server = ROOT / "fl_studio_mcp" / "mcp_server.py"
        tree = ast.parse(server.read_text(encoding="utf-8"), filename=str(server))
        annotated: list[str] = []
        unannotated: list[str] = []
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                function = decorator.func
                if not (
                    isinstance(function, ast.Attribute) and function.attr == "tool"
                ):
                    continue
                name = next(
                    (
                        keyword.value.value
                        for keyword in decorator.keywords
                        if keyword.arg == "name"
                        and isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)
                    ),
                    node.name,
                )
                if any(keyword.arg == "annotations" for keyword in decorator.keywords):
                    annotated.append(name)
                else:
                    unannotated.append(name)
        self.assertTrue(annotated)
        self.assertEqual(unannotated, [])
        self.assertEqual(len(annotated), len(self.manifest["tools"]))

    def test_manifest_does_not_enable_fl_write_mode(self) -> None:
        encoded = json.dumps(self.manifest)
        self.assertNotIn("FL_BRIDGE_ENABLE_WRITES", encoded)

    def test_windows_cmd_path_found_by_preflight_is_executed_exactly(self) -> None:
        resolved = r"C:\bundled node\npx.cmd"
        with mock.patch.object(build_mcpb.shutil, "which", return_value=resolved):
            version, command_prefix = build_mcpb._preflight()
        self.assertEqual(version, self.manifest["version"])
        self.assertEqual(
            command_prefix,
            (
                resolved,
                "--yes",
                f"--package={MCPB_NPM_PACKAGE}",
                "mcpb",
            ),
        )

        with mock.patch.object(build_mcpb.subprocess, "run") as run:
            build_mcpb._run_cli(command_prefix, "validate", "manifest.json")
        command = run.call_args.args[0]
        self.assertEqual(command[0], resolved)
        self.assertEqual(command[-2:], ["validate", "manifest.json"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_pnpm_is_a_shell_free_fallback_with_the_same_exact_pin(self) -> None:
        resolved = r"C:\bundled node\pnpm.cmd"

        def find(name: str):
            return resolved if name == "pnpm.cmd" else None

        with mock.patch.object(build_mcpb.shutil, "which", side_effect=find):
            command_prefix = build_mcpb._resolve_cli()
        self.assertEqual(
            command_prefix,
            (resolved, "dlx", MCPB_NPM_PACKAGE),
        )

        with mock.patch.object(build_mcpb.subprocess, "run") as run:
            build_mcpb._run_cli(command_prefix, "pack", ".", r"C:\out dir\x.mcpb")
        self.assertEqual(
            run.call_args.args[0],
            [
                resolved,
                "dlx",
                MCPB_NPM_PACKAGE,
                "pack",
                ".",
                r"C:\out dir\x.mcpb",
            ],
        )
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_preflight_fails_when_no_supported_package_runner_exists(self) -> None:
        with mock.patch.object(build_mcpb.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "npx or pnpm"):
                build_mcpb._resolve_cli()

    def test_forged_bundle_with_private_member_is_rejected(self) -> None:
        private_members = (
            ".private/host-report.md", ".codex/config.toml",
            ".claude/settings.json", "conversation.jsonl", "session.log",
            "production.sqlite3", "production.sqlite3-wal", "run.db-shm",
        )
        with tempfile.TemporaryDirectory(prefix="postfader-forged-mcpb-") as raw:
            bundle = Path(raw) / "forged.mcpb"
            with zipfile.ZipFile(bundle, "w") as archive:
                for required in sorted(
                    {
                        "mcpb_entry.py",
                        "pyproject.toml",
                        "fl_studio_mcp/mcp_server.py",
                        "fl_studio_mcp/_bridge/device_UniversalBridge.py",
                    }
                ):
                    archive.writestr(required, "fixture")
                archive.writestr(
                    "manifest.json",
                    (ROOT / "manifest.json").read_text(encoding="utf-8"),
                )
                for member in private_members:
                    archive.writestr(member, "must not ship")
            failures = inspect_bundle(bundle)
        for member in private_members:
            with self.subTest(member=member):
                self.assertTrue(any(member in item for item in failures))


if __name__ == "__main__":
    unittest.main()
