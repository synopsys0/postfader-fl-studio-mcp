#!/usr/bin/env python3
"""Small packaging/version checks for the local pre-release."""

from __future__ import annotations

import importlib.metadata
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

        # Scope the metadata check to this checkout.  A developer may still
        # have an older editable install in the virtualenv when the offline
        # build backend is unavailable; that must not override the metadata
        # generated in the repository itself.
        repository_versions = {
            distribution.version
            for distribution in importlib.metadata.distributions(path=[str(ROOT)])
            if distribution.metadata.get("Name", "").lower().replace("_", "-")
            == "postfader-fl-studio-mcp"
        }
        self.assertEqual(repository_versions, {expected})

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

    def test_entry_points_are_exactly_the_supported_three(self) -> None:
        # Pinned as a set, not merely checked for presence: a stray console
        # script is a public surface, and this file is where that gets caught.
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(
            project["project"]["scripts"],
            {
                "fl-studio-mcp": "fl_studio_mcp.mcp_server:main",
                "postfader-install-bridge": "fl_studio_mcp.bridge_install:main",
                "postfader-doctor": "fl_studio_mcp.diagnostics:main",
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

    def test_the_packaged_bridge_is_data_rather_than_a_module(self) -> None:
        # It calls FL Studio's embedded API at import time, so it must not be
        # reachable by `import fl_studio_mcp._bridge...`.
        self.assertFalse((ROOT / "fl_studio_mcp" / "_bridge" / "__init__.py").exists())

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
