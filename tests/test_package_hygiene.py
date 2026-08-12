#!/usr/bin/env python3
"""Small packaging/version checks for the local pre-release."""

from __future__ import annotations

import ast
import json
import unittest
from importlib.resources import files
from pathlib import Path

import fl_studio_mcp

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


class PackageHygieneTests(unittest.TestCase):
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
            "_bridge/device_UniversalBridge.py",
            declared["tool"]["setuptools"]["package-data"]["fl_studio_mcp"],
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
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]
        self.assertEqual(project["name"], "postfader-fl-studio-mcp")
        self.assertEqual(project["readme"], "README.md")
        self.assertEqual(project["requires-python"], ">=3.10")
        self.assertIn("Programming Language :: Python :: 3.14", project["classifiers"])
        self.assertTrue(any(item.startswith("anyio") for item in project["dependencies"]))
        self.assertTrue(
            any(item.startswith("tomli") for item in project["optional-dependencies"]["test"])
        )
        self.assertEqual(
            project["urls"]["Repository"],
            "https://github.com/synopsys0/postfader-fl-studio-mcp",
        )

    def test_offline_prototype_is_not_in_the_public_package(self) -> None:
        package = ROOT / "fl_studio_mcp"
        for name in ("server.py", "models.py", "project_file.py"):
            with self.subTest(name=name):
                self.assertFalse((package / name).exists())

        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertFalse(any(item.startswith("pyflp") for item in project["project"]["dependencies"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
