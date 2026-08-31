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
from check_mcpb_bundle import REQUIRED as MCPB_REQUIRED  # noqa: E402
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
        self.assertEqual(
            config["env"],
            {
                "FL_BRIDGE_ENABLE_MIDI": "1",
                "FL_BRIDGE_MIDI_PORT": "${user_config.midi_port}",
                "FL_STUDIO_USER_DATA_DIR": (
                    "${user_config.fl_studio_user_data_dir}"
                ),
            },
        )

    def test_manifest_prompts_for_host_specific_paths_and_endpoint(self) -> None:
        config = self.manifest["user_config"]
        self.assertEqual(config["midi_port"]["type"], "string")
        self.assertTrue(config["midi_port"]["required"])
        self.assertIn("exact", config["midi_port"]["description"].lower())
        self.assertEqual(config["fl_studio_user_data_dir"]["type"], "directory")
        self.assertTrue(config["fl_studio_user_data_dir"]["required"])
        self.assertIn("${DOCUMENTS}", config["fl_studio_user_data_dir"]["default"])

    def test_manifest_explains_the_required_host_installation(self) -> None:
        guidance = self.manifest["long_description"].lower()
        self.assertIn("matching python package", guidance)
        self.assertIn("postfader setup", guidance)
        self.assertIn(".mcpb does not deploy", guidance)
        self.assertIn("virtual midi endpoint", guidance)

    def test_manifest_declares_macos_and_windows(self) -> None:
        self.assertEqual(
            self.manifest["compatibility"]["platforms"],
            ["darwin", "win32"],
        )
        self.assertEqual(
            self.manifest["compatibility"]["runtimes"]["python"],
            ">=3.10,<3.15",
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

    def test_manifest_tools_match_runtime_decorators(self) -> None:
        self.assertEqual(self.manifest["tools"], discover_tools())
        names = [tool["name"] for tool in self.manifest["tools"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertGreaterEqual(len(names), 75)
        self.assertEqual(len(names), 111)

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

    def test_bundle_checker_requires_the_complete_plugin_atlas_payload(self) -> None:
        self.assertTrue(
            {
                "fl_studio_mcp/plugin_atlas/__init__.py",
                "fl_studio_mcp/plugin_atlas_data/__init__.py",
                "fl_studio_mcp/plugin_atlas_mcp.py",
            }
            <= MCPB_REQUIRED
        )
        self.assertIn(
            "fl_studio_mcp/plugin_atlas_data/manifests/atlas.json",
            MCPB_REQUIRED,
        )

    def test_bundle_checker_requires_the_complete_sound_selection_payload(self) -> None:
        self.assertTrue(
            {
                "fl_studio_mcp/sound_selection/__init__.py",
                "fl_studio_mcp/sound_selection/models.py",
                "fl_studio_mcp/sound_selection/data/__init__.py",
                "fl_studio_mcp/sound_selection/data/descriptors-v1.json",
            }
            <= MCPB_REQUIRED
        )

    def test_bundle_entry_point_exists(self) -> None:
        self.assertTrue((ROOT / "mcpb_entry.py").is_file())

    def test_cli_is_pinned_to_an_exact_version(self) -> None:
        self.assertRegex(MCPB_NPM_PACKAGE, r"^@anthropic-ai/mcpb@\d+\.\d+\.\d+$")
        self.assertEqual(MCPB_NPM_PACKAGE, "@anthropic-ai/mcpb@2.1.2")

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

    def test_ignore_file_blocks_secrets_and_user_media(self) -> None:
        patterns = {
            line.strip()
            for line in (ROOT / ".mcpbignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        for required in (
            ".git/",
            ".github/",
            ".private/",
            ".mcp.json",
            ".env",
            "tests/",
            "scripts/",
            "*.flp",
            "*.wav",
            "*.mcpb",
        ):
            self.assertIn(required, patterns)

    def test_forged_bundle_with_private_member_is_rejected(self) -> None:
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
                archive.writestr(".private/host-report.md", "must not ship")
            failures = inspect_bundle(bundle)
        self.assertTrue(any(".private/host-report.md" in item for item in failures))

    def test_missing_bundle_fails_inspection(self) -> None:
        failures = inspect_bundle(ROOT / "does-not-exist.mcpb")
        self.assertTrue(failures)


if __name__ == "__main__":
    unittest.main()
