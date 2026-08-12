#!/usr/bin/env python3
"""Hermetic checks for the Claude Desktop MCPB packaging surface."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from build_mcpb import MCPB_NPM_PACKAGE  # noqa: E402
from check_mcpb_bundle import inspect_bundle  # noqa: E402
from sync_mcpb_manifest import discover_tools  # noqa: E402


class MCPBPackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(
            (ROOT / "manifest.json").read_text(encoding="utf-8")
        )

    def test_manifest_uses_supported_uv_schema(self) -> None:
        self.assertEqual(self.manifest["manifest_version"], "0.4")
        self.assertEqual(self.manifest["server"]["type"], "uv")
        self.assertEqual(self.manifest["server"]["entry_point"], "mcpb_entry.py")
        config = self.manifest["server"]["mcp_config"]
        self.assertEqual(config["command"], "uv")
        self.assertEqual(
            config["args"],
            ["run", "--directory", "${__dirname}", "mcpb_entry.py"],
        )
        self.assertEqual(config["env"], {"FL_BRIDGE_ENABLE_MIDI": "1"})

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

    def test_manifest_tools_match_runtime_decorators(self) -> None:
        self.assertEqual(self.manifest["tools"], discover_tools())
        names = [tool["name"] for tool in self.manifest["tools"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertLess(len(names), 40)

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

    def test_bundle_entry_point_exists(self) -> None:
        self.assertTrue((ROOT / "mcpb_entry.py").is_file())

    def test_cli_is_pinned_to_an_exact_version(self) -> None:
        self.assertRegex(MCPB_NPM_PACKAGE, r"^@anthropic-ai/mcpb@\d+\.\d+\.\d+$")
        self.assertEqual(MCPB_NPM_PACKAGE, "@anthropic-ai/mcpb@2.1.2")

    def test_ignore_file_blocks_secrets_and_user_media(self) -> None:
        patterns = {
            line.strip()
            for line in (ROOT / ".mcpbignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        for required in (
            ".git/",
            ".github/",
            ".mcp.json",
            ".env",
            "tests/",
            "scripts/",
            "*.flp",
            "*.wav",
            "*.mcpb",
        ):
            self.assertIn(required, patterns)

    def test_missing_bundle_fails_inspection(self) -> None:
        failures = inspect_bundle(ROOT / "does-not-exist.mcpb")
        self.assertTrue(failures)


if __name__ == "__main__":
    unittest.main()
