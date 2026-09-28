"""Hermetic MCP and acceptance seams for the Sound Selection workflow."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import get_args, get_type_hints


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT))

from fl_studio_mcp.acceptance import (  # noqa: E402
    authoritative_tool_surface,
    read_acceptance_arguments,
)
from fl_studio_mcp.mcp_server import mcp  # noqa: E402
from fl_studio_mcp.sound_selection.executor import SoundPaletteLookup  # noqa: E402
from fl_studio_mcp.sound_selection.mcp import sound_selection_apply  # noqa: E402
from fl_studio_mcp.sound_selection.models import SoundPaletteVariationPlan  # noqa: E402


SOUND_SELECTION_TOOLS = (
    "plugins_list_presets",
    "plugins_get_current_preset",
    "plugins_inspect_pad_map",
    "fl_select_plugin_preset",
    "sound_selection_inventory",
    "sound_selection_plan",
    "sound_selection_get",
    "sound_selection_create_variation",
    "sound_selection_apply",
    "sound_selection_record_feedback",
    "sound_selection_history_status",
    "sound_selection_history_reset",
)


def _tool_map():
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


class SoundSelectionMcpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.surface = asyncio.run(authoritative_tool_surface())

    def test_live_read_arguments_cover_preset_and_sound_selection_reads(self) -> None:
        arguments = read_acceptance_arguments(
            mixer_track_index=3,
            plugin_track_index=3,
            plugin_slot_index=1,
            pattern_number=1,
            channel_index=0,
            sound_selection_channel_index=2,
            fixture_root=ROOT / "tests" / "fixtures" / "audio",
        )
        self.assertEqual(
            set(self.surface.read_tools),
            set(arguments),
        )
        self.assertEqual(
            arguments["plugins_inspect_pad_map"]["target"],
            {"kind": "channel_generator", "channel_index": 2},
        )

    def test_sound_selection_inputs_and_outputs_are_strict(self) -> None:
        tools = _tool_map()
        for name in SOUND_SELECTION_TOOLS:
            with self.subTest(tool=name):
                tool = tools[name]
                schema = tool.input_schema
                self.assertEqual(schema.get("type"), "object")
                self.assertIs(schema.get("additionalProperties"), False)
                self.assertTrue(tool.output_schema)
                self.assertIs(
                    tool.output_schema.get("additionalProperties"),
                    False,
                )
                for definition in schema.get("$defs", {}).values():
                    if definition.get("type") == "object":
                        self.assertIs(
                            definition.get("additionalProperties"),
                            False,
                            definition,
                        )
                for definition in tool.output_schema.get("$defs", {}).values():
                    if definition.get("type") == "object":
                        self.assertIs(
                            definition.get("additionalProperties"),
                            False,
                            definition,
                        )

    def test_apply_accepts_variation_plan_in_wrapper_and_tool_schema(self) -> None:
        annotation = get_type_hints(sound_selection_apply)["plan_or_id"]
        self.assertIn(SoundPaletteVariationPlan, get_args(annotation))

        tool = _tool_map()["sound_selection_apply"]
        palette_schema = tool.input_schema["properties"]["palette"]
        variation_definition = tool.input_schema["$defs"]["SoundPaletteVariationPlan"]
        self.assertTrue(
            any(
                branch.get("$ref", "").endswith("SoundPaletteVariationPlan")
                for branch in palette_schema.get("anyOf", ())
            )
        )
        self.assertIs(variation_definition.get("additionalProperties"), False)

    def test_sound_selection_apply_requires_session_fingerprint_before_service_call(self) -> None:
        tool = _tool_map()["sound_selection_apply"]
        self.assertIn("session_fingerprint", tool.input_schema["required"])
        with self.assertRaisesRegex(
            Exception,
            r"(?s)session_fingerprint.*Field required",
        ):
            asyncio.run(
                mcp.call_tool(
                    "sound_selection_apply",
                    {"palette": "missing", "authorized_to_modify": True},
                )
            )

    def test_missing_palette_lookup_is_a_strict_process_local_result(self) -> None:
        result = asyncio.run(
            mcp.call_tool(
                "sound_selection_get",
                {"palette_id": "missing-mcp-palette"},
            )
        )
        self.assertFalse(getattr(result, "is_error", False))
        body = result.structured_content
        self.assertIsInstance(body, dict)
        lookup = SoundPaletteLookup.model_validate(body, strict=True)
        self.assertIs(lookup.found, False)
        self.assertIs(lookup.process_local, True)
        self.assertIs(body["found"], False)
        self.assertIs(body["process_local"], True)
        self.assertIsNone(body["state"])
        self.assertIn("process-local", body["message"])
        self.assertRegex(body["message"], "expired|another process")

    def test_live_sound_selection_plan_is_non_contacting_and_bounded(self) -> None:
        script = ROOT / "scripts" / "live_sound_selection_acceptance.py"
        environment = os.environ.copy()
        environment.update(
            {
                "FL_BRIDGE_ENABLE_MIDI": "1",
                "FL_BRIDGE_ENABLE_WRITES": "1",
                "FL_BRIDGE_SANDBOXED": "0",
            }
        )
        completed = subprocess.run(
            [sys.executable, "-B", os.fspath(script), "--plan", "--channel", "2"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["kind"], "postfader_sound_selection_acceptance")
        self.assertFalse(report["contact_started"])
        self.assertFalse(report["physical_io_performed"])
        self.assertEqual(report["arguments"]["inventory"]["preset_limit"], 64)

    def test_live_sound_selection_help_is_available_without_contact(self) -> None:
        script = ROOT / "scripts" / "live_sound_selection_acceptance.py"
        environment = os.environ.copy()
        environment.update(
            {
                "FL_BRIDGE_ENABLE_MIDI": "1",
                "FL_BRIDGE_ENABLE_WRITES": "1",
                "FL_BRIDGE_SANDBOXED": "0",
            }
        )
        completed = subprocess.run(
            [sys.executable, "-B", os.fspath(script), "--help"],
            cwd=ROOT.parent,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("--plan", completed.stdout)
        self.assertIn("--preset-name", completed.stdout)

    @staticmethod
    def _live_script():
        path = ROOT / "scripts" / "live_sound_selection_acceptance.py"
        spec = importlib.util.spec_from_file_location(
            "live_sound_selection_acceptance", path
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_live_sound_selection_plan_skips_authoritative_surface_and_calls(self) -> None:
        module = self._live_script()

        async def unexpected_surface():
            raise AssertionError("--plan must not inspect the MCP surface")

        async def unexpected_call(_name, _arguments, _timeout):
            raise AssertionError("--plan must not call an MCP tool")

        module.authoritative_tool_surface = unexpected_surface
        module._call = unexpected_call
        report = asyncio.run(module.async_main(module.parse_args(["--plan"])))
        self.assertEqual(report["phase"], "plan_only")
        self.assertFalse(report["contact_started"])
        self.assertFalse(report["automatic_replay_attempted"])

    @staticmethod
    def _fake_live_surface():
        return SimpleNamespace(
            all_tools={
                "sound_selection_inventory",
                "plugins_list_presets",
                "plugins_get_current_preset",
                "plugins_inspect_pad_map",
                "sound_selection_plan",
                "fl_select_plugin_preset",
                "sound_selection_apply",
                "postfader_validate_run",
                "postfader_execute_run",
                "postfader_get_run",
                "postfader_continue_run",
            }
        )

    def _fake_live_calls(self, module, calls, *, unknown_selection=False):
        session = "c" * 32
        target_fingerprint = "b" * 64
        run_id = "a" * 32
        assignment = {
            "role_id": module.ACCEPTANCE_ROLE,
            "target": {"kind": "channel_generator", "channel_index": 0},
            "selected_preset": "Acceptance Lead",
            "selected_preset_index": 1,
            "preset_identity_digest": "d" * 64,
        }
        initial_outputs = [
            {
                "operation_id": module.REFERENCE_OPERATION,
                "output": "sound_palette",
                "value": {"assignments": [assignment]},
            },
            {
                "operation_id": module.REFERENCE_OPERATION,
                "output": "palette_assignment",
                "role_id": module.ACCEPTANCE_ROLE,
                "value": assignment,
            },
            {
                "operation_id": module.APPLY_OPERATION,
                "output": "sound_palette",
                "value": {"status": "applied", "assignments": [assignment]},
            },
        ]
        final_outputs = [
            *initial_outputs,
            {
                "operation_id": module.VARIATION_OPERATION,
                "output": "section_variation",
                "value": {
                    "preserve_anchor_roles": True,
                    "unchanged_role_ids": [module.ACCEPTANCE_ROLE],
                    "assignments": [],
                },
            },
        ]
        state = {"preset": "Initial", "continued": False}

        async def fake_call(name, arguments, _timeout):
            calls.append((name, arguments))
            if name == "sound_selection_inventory":
                return {
                    "session_fingerprint": session,
                    "loaded_generators": [
                        {
                            "target": {
                                "kind": "channel_generator",
                                "channel_index": 0,
                                "index_scope": "global",
                            },
                            "preset_names": ["Initial", "Acceptance Lead"],
                            "preset_indices": [0, 1],
                        }
                    ],
                }
            if name == "plugins_list_presets":
                return {
                    "session_fingerprint": session,
                    "target_fingerprint": target_fingerprint,
                    "presets": [
                        {"name": "Initial", "index": 0},
                        {"name": "Acceptance Lead", "index": 1},
                    ],
                }
            if name == "plugins_get_current_preset":
                return {
                    "session_fingerprint": session,
                    "current_preset_name": state["preset"],
                    "current_preset_index": 0
                    if state["preset"] == "Initial"
                    else 1,
                    "current_preset_status": "stable",
                }
            if name == "plugins_inspect_pad_map":
                return {
                    "session_fingerprint": session,
                    "pad_count": 1,
                    "complete": True,
                    "pads": [{"pad_index": 0}],
                }
            if name == "sound_selection_plan":
                return {
                    "palette_id": "palette-acceptance",
                    "assignments": [assignment],
                }
            if name == "fl_select_plugin_preset":
                if unknown_selection:
                    return {
                        "verified": False,
                        "outcome": "unknown",
                        "after": {
                            "name": "Initial",
                            "index": 0,
                            "identity_status": "stable",
                        },
                    }
                state["preset"] = "Acceptance Lead"
                return {
                    "verified": True,
                    "outcome": "verified",
                    "after": {
                        "name": "Acceptance Lead",
                        "index": 1,
                        "identity_status": "stable",
                    },
                }
            if name == "sound_selection_apply":
                return {"status": "applied", "verified_count": 1, "blockers": []}
            if name == "postfader_validate_run":
                return {"valid": True, "blockers": []}
            if name == "postfader_execute_run":
                return {"status": "completed", "run_id": run_id}
            if name == "postfader_get_run":
                outputs = final_outputs if state["continued"] else initial_outputs
                return {"state": {"generated_outputs": outputs}}
            if name == "postfader_continue_run":
                state["continued"] = True
                return {"status": "completed", "run_id": run_id}
            raise AssertionError(name)

        return fake_call

    def test_live_sound_selection_full_flow_proves_reference_and_anchor_continuation(self) -> None:
        module = self._live_script()
        calls = []

        async def surface():
            return self._fake_live_surface()

        module.authoritative_tool_surface = surface
        module._call = self._fake_live_calls(module, calls)
        args = module.parse_args(
            [
                "--apply",
                "--confirm-user-present",
                "--confirm-disposable-project",
                "--confirm-safe-to-edit",
            ]
        )
        report = asyncio.run(module.async_main(args))
        self.assertEqual(report["overall"], "pass")
        self.assertEqual(report["phase"], "complete")
        self.assertEqual(
            [name for name, _arguments in calls],
            [
                "sound_selection_inventory",
                "plugins_list_presets",
                "plugins_get_current_preset",
                "plugins_inspect_pad_map",
                "sound_selection_plan",
                "fl_select_plugin_preset",
                "plugins_get_current_preset",
                "sound_selection_apply",
                "postfader_validate_run",
                "postfader_execute_run",
                "postfader_get_run",
                "postfader_continue_run",
                "postfader_get_run",
            ],
        )
        self.assertEqual(
            report["checks"]["later_tick_preset_readback"]["status"],
            "passed",
        )
        self.assertEqual(
            report["checks"]["production_run_reference"]["status"],
            "passed",
        )
        self.assertEqual(
            report["checks"]["continuation_preserves_anchor"]["status"],
            "passed",
        )
        self.assertFalse(report["project_saved"])
        self.assertFalse(report["automatic_replay_attempted"])
        continuation = next(
            arguments
            for name, arguments in calls
            if name == "postfader_continue_run"
        )
        self.assertEqual(
            continuation["delta"]["operations"][0]["palette"]["operation_id"],
            module.REFERENCE_OPERATION,
        )

    def test_live_sound_selection_unknown_preset_outcome_stops_without_replay(self) -> None:
        module = self._live_script()
        calls = []

        async def surface():
            return self._fake_live_surface()

        module.authoritative_tool_surface = surface
        module._call = self._fake_live_calls(module, calls, unknown_selection=True)
        args = module.parse_args(
            [
                "--apply",
                "--confirm-user-present",
                "--confirm-disposable-project",
                "--confirm-safe-to-edit",
            ]
        )
        report = asyncio.run(module.async_main(args))
        self.assertEqual(report["overall"], "fail")
        self.assertEqual(report["phase"], "unknown_outcome")
        self.assertFalse(report["automatic_replay"])
        self.assertFalse(report["automatic_replay_attempted"])
        self.assertEqual(
            [name for name, _arguments in calls],
            [
                "sound_selection_inventory",
                "plugins_list_presets",
                "plugins_get_current_preset",
                "plugins_inspect_pad_map",
                "sound_selection_plan",
                "fl_select_plugin_preset",
            ],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
