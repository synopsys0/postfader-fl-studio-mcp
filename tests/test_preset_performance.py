#!/usr/bin/env python3
"""Typed gateway/controller seams for the low-level preset bridge surface."""

from __future__ import annotations

import copy
import unittest
from typing import Any

from fl_studio_mcp.bridge_client import IDEMPOTENT_READ_COMMANDS
from fl_studio_mcp.bridge_install import expected_bridge_deployment
from fl_studio_mcp.performance import (
    TRACK_B_PRESET_MUTATION_COMMANDS,
    TRACK_B_PRESET_READ_COMMANDS,
    TrackBController,
    TrackBInspector,
    TrackBMutationGateway,
    TrackBReadGateway,
)
from fl_studio_mcp.track_b_contracts import (
    ExpectedPluginPresetState,
    MixerEffectTarget,
)


SESSION = "1" * 32
TARGET_FINGERPRINT = "a" * 64
CHANNEL_FINGERPRINT = "b" * 64


def ping(*, writable: bool = False) -> dict[str, Any]:
    digest = expected_bridge_deployment()[1]
    return {
        "pong": True,
        "protocol": 2,
        "program_title": "FL Studio 2026",
        "fl_version": "Producer Edition v26.1.3 [build 5336]",
        "midi_scripting_api_version": 44,
        "bridge_mode": "write_test" if writable else "read_only",
        "verified_writes_enabled": writable,
        "runtime_write_mode_control": True,
        "write_mode_origin": "startup_environment" if writable else "disabled",
        "startup_write_mode_enabled": writable,
        "bridge_source_sha256": digest,
        "session_fingerprint": SESSION,
    }


class Client:
    transport = "midi"

    def __init__(self, responses: dict[str, dict[str, Any]], *, writable: bool) -> None:
        self.responses = responses
        self.ping_response = ping(writable=writable)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def ping(self) -> dict[str, Any]:
        return copy.deepcopy(self.ping_response)

    def call(self, command: str, **arguments: Any) -> dict[str, Any]:
        self.calls.append((command, copy.deepcopy(arguments)))
        response = self.responses[command]
        return copy.deepcopy(response)


def effect_fields() -> dict[str, Any]:
    return {
        "target_kind": "mixer_effect",
        "track": 3,
        "slot": 0,
        "use_global_index": False,
        "plugin": "Synthetic FX",
        "plugin_user_name": "Synthetic FX",
        "param_count": 4,
        "mix_level": 0.5,
        "target_fingerprint": TARGET_FINGERPRINT,
    }


def read_page() -> dict[str, Any]:
    return {
        "command": "plugin.presets",
        **effect_fields(),
        "preset_count": 3,
        "start": 0,
        "limit": 3,
        "scanned_count": 3,
        "returned_count": 3,
        "has_more": False,
        "next_start": None,
        "presets": [
            {"index": 0, "name": "A", "is_current": True},
            {"index": 1, "name": "B", "is_current": False},
            {"index": 2, "name": "C", "is_current": False},
        ],
        "current_preset_name": "A",
        "current_preset_index": 0,
        "current_preset_status": "stable",
        "duplicate_names": [],
        "blank_name_indices": [],
        "partial": False,
        "truncated": False,
        "truncated_by": None,
        "session_fingerprint": SESSION,
        "unsaved_changes": 0,
        "warnings": [],
    }


class PresetPerformanceTests(unittest.TestCase):
    def test_read_gateway_parses_pages_current_and_pad_maps(self) -> None:
        page = read_page()
        current = {
            "command": "plugin.current_preset",
            **effect_fields(),
            "preset_count": 3,
            "current_preset_name": "A",
            "current_preset_index": 0,
            "current_preset_status": "stable",
            "session_fingerprint": SESSION,
            "unsaved_changes": 0,
            "warnings": [],
        }
        pad_map = {
            "command": "plugin.pad_map",
            **effect_fields(),
            "pad_count": 1,
            "pads": [
                {
                    "pad_index": 0,
                    "semitone": 36,
                    "color": 0x010203,
                    "empty": False,
                    "muted": False,
                    "semitone_name": "C2",
                }
            ],
            "complete": True,
            "session_fingerprint": SESSION,
            "unsaved_changes": 0,
            "warnings": [],
        }
        client = Client(
            {
                "plugin.presets": page,
                "plugin.current_preset": current,
                "plugin.pad_map": pad_map,
            },
            writable=False,
        )
        inspector = TrackBInspector(TrackBReadGateway(client))
        observed = inspector.list_plugin_presets(track_index=3, slot_index=0, limit=3)
        self.assertEqual(observed.current_preset_index, 0)
        self.assertEqual([row.name for row in observed.presets], ["A", "B", "C"])
        self.assertEqual(inspector.get_plugin_current_preset(track_index=3, slot_index=0).current_preset_name, "A")
        self.assertEqual(inspector.inspect_plugin_pad_map(track_index=3, slot_index=0).pads[0].semitone, 36)
        self.assertEqual(client.calls[0][0], "plugin.presets")
        self.assertEqual(client.calls[0][1]["target_kind"], "mixer_effect")
        self.assertFalse(client.calls[0][1]["use_global_index"])

    def test_inventory_preserves_live_target_fingerprints_for_apply_preflight(self) -> None:
        client = Client(
            {
                "project.info": {
                    "unsaved_changes": 0,
                    "undo_history_position": 1,
                    "undo_history_count": 1,
                },
                "mixer.list": {
                    "tracks": [
                        {
                            "index": 3,
                            "plugins": [
                                {
                                    "slot": 0,
                                    "name": "Synthetic FX",
                                    "user_name": "Synthetic FX",
                                    "param_count": 4,
                                    "mix_level": 0.5,
                                    "target_fingerprint": TARGET_FINGERPRINT,
                                }
                            ],
                        }
                    ]
                },
                "channels.list": {
                    "channels": [
                        {
                            "index": 7,
                            "index_scope": "global",
                            "plugin": "Synthetic Synth",
                            "plugin_user_name": "Synthetic Synth",
                            "reported_parameter_count": 2,
                            "target_fingerprint": TARGET_FINGERPRINT,
                        }
                    ]
                },
            },
            writable=False,
        )
        inspector = TrackBInspector(TrackBReadGateway(client))
        inventory = inspector.scan_loaded_plugins(only_used=False)
        self.assertEqual(
            [item.target_fingerprint for item in inventory.plugins],
            [TARGET_FINGERPRINT, TARGET_FINGERPRINT],
        )

    def test_controller_requires_verified_selection_receipt_and_preserves_target(self) -> None:
        selection = {
            "command": "plugin.select_preset",
            **effect_fields(),
            "preset_count": 3,
            "requested_preset_name": "B",
            "requested_preset_index": 1,
            "before": {"name": "A", "index": 0, "identity_status": "stable"},
            "after": {"name": "B", "index": 1, "identity_status": "stable"},
            "outcome": "verified",
            "verified": True,
            "navigation_direction": "next",
            "navigation_steps": 1,
            "max_navigation_steps": 4,
            "settle_tick_limit": 1,
            "undo_point_created": False,
            "session_fingerprint": SESSION,
            "session_precondition_applied": True,
            "expected_before_applied": True,
            "project_saved": False,
            "warnings": [],
        }
        client = Client({"plugin.select_preset": selection}, writable=True)
        controller = TrackBController(TrackBMutationGateway(client))
        receipt = controller.select_plugin_preset(
            target=MixerEffectTarget(track_index=3, slot_index=0),
            preset_name="B",
            expected_current=ExpectedPluginPresetState(name="A", index=0),
            session_fingerprint=SESSION,
            target_fingerprint=TARGET_FINGERPRINT,
            max_navigation_steps=4,
        )
        self.assertTrue(receipt.verified)
        self.assertEqual(receipt.after.name, "B")
        command, arguments = client.calls[0]
        self.assertEqual(command, "plugin.select_preset")
        self.assertEqual(arguments["expected_before"], {"name": "A", "index": 0})
        self.assertEqual(arguments["target_kind"], "mixer_effect")
        self.assertEqual(arguments["target_fingerprint"], TARGET_FINGERPRINT)

    def test_loop_starter_controller_keeps_dispatch_separate_from_preset_proof(self) -> None:
        response = {
            "command": "channels.rerollLoopStarterLoop",
            "channel": 2,
            "index_scope": "global",
            "before_channel_fingerprint": CHANNEL_FINGERPRINT,
            "after_channel_fingerprint": CHANNEL_FINGERPRINT,
            "dispatched": True,
            "identity_verified": False,
            "verified": False,
            "verification_basis": "dispatch_only_no_identity_readback",
            "undo_point_created": False,
            "session_fingerprint": SESSION,
            "session_precondition_applied": True,
            "expected_before_applied": True,
            "project_saved": False,
            "warnings": [],
        }
        client = Client({"channels.rerollLoopStarterLoop": response}, writable=True)
        controller = TrackBController(TrackBMutationGateway(client))
        receipt = controller.reroll_loop_starter_loop(
            channel_index=2,
            channel_fingerprint=CHANNEL_FINGERPRINT,
            session_fingerprint=SESSION,
        )
        self.assertTrue(receipt.dispatched)
        self.assertFalse(receipt.verified)
        command, arguments = client.calls[0]
        self.assertEqual(command, "channels.rerollLoopStarterLoop")
        self.assertEqual(arguments, {
            "channel": 2,
            "index_scope": "global",
            "session_fingerprint": SESSION,
            "expected_before": {"channel_fingerprint": CHANNEL_FINGERPRINT},
        })

    def test_command_allowlists_and_client_replay_boundary_are_explicit(self) -> None:
        self.assertIn("plugin.presets", TRACK_B_PRESET_READ_COMMANDS)
        self.assertIn("plugin.current_preset", TRACK_B_PRESET_READ_COMMANDS)
        self.assertIn("plugin.pad_map", TRACK_B_PRESET_READ_COMMANDS)
        self.assertIn("plugin.select_preset", TRACK_B_PRESET_MUTATION_COMMANDS)
        self.assertIn("channels.rerollLoopStarterLoop", TRACK_B_PRESET_MUTATION_COMMANDS)
        self.assertIn("plugin.presets", IDEMPOTENT_READ_COMMANDS)
        self.assertIn("plugin.current_preset", IDEMPOTENT_READ_COMMANDS)
        self.assertIn("plugin.pad_map", IDEMPOTENT_READ_COMMANDS)
        self.assertNotIn("plugin.select_preset", IDEMPOTENT_READ_COMMANDS)
        self.assertNotIn("channels.rerollLoopStarterLoop", IDEMPOTENT_READ_COMMANDS)

    def test_controller_rejects_a_mismatched_returned_target_fingerprint(self) -> None:
        selection = {
            "command": "plugin.select_preset",
            **effect_fields(),
            "target_fingerprint": "c" * 64,
            "preset_count": 3,
            "requested_preset_name": "B",
            "requested_preset_index": 1,
            "before": {"name": "A", "index": 0, "identity_status": "stable"},
            "after": {"name": "B", "index": 1, "identity_status": "stable"},
            "outcome": "verified",
            "verified": True,
            "navigation_direction": "next",
            "navigation_steps": 1,
            "max_navigation_steps": 4,
            "settle_tick_limit": 1,
            "undo_point_created": False,
            "session_fingerprint": SESSION,
            "session_precondition_applied": False,
            "expected_before_applied": False,
            "project_saved": False,
            "warnings": [],
        }
        client = Client({"plugin.select_preset": selection}, writable=True)
        controller = TrackBController(TrackBMutationGateway(client))
        with self.assertRaises(ValueError):
            controller.select_plugin_preset(
                target=MixerEffectTarget(track_index=3, slot_index=0),
                preset_name="B",
                target_fingerprint=TARGET_FINGERPRINT,
                max_navigation_steps=4,
            )

    def test_controller_rejects_a_mismatched_echoed_request(self) -> None:
        selection = {
            "command": "plugin.select_preset",
            **effect_fields(),
            "preset_count": 3,
            "requested_preset_name": "B",
            "requested_preset_index": 1,
            "before": {"name": "A", "index": 0, "identity_status": "stable"},
            "after": {"name": "B", "index": 1, "identity_status": "stable"},
            "outcome": "verified",
            "verified": True,
            "navigation_direction": "next",
            "navigation_steps": 1,
            "max_navigation_steps": 4,
            "settle_tick_limit": 1,
            "undo_point_created": False,
            "session_fingerprint": SESSION,
            "session_precondition_applied": False,
            "expected_before_applied": False,
            "project_saved": False,
            "warnings": [],
        }
        client = Client({"plugin.select_preset": selection}, writable=True)
        controller = TrackBController(TrackBMutationGateway(client))
        with self.assertRaises(ValueError):
            controller.select_plugin_preset(
                target=MixerEffectTarget(track_index=3, slot_index=0),
                preset_name="C",
                max_navigation_steps=4,
            )

    def test_controller_rejects_a_before_state_that_violates_expected_current(self) -> None:
        selection = {
            "command": "plugin.select_preset",
            **effect_fields(),
            "preset_count": 3,
            "requested_preset_name": "B",
            "requested_preset_index": 1,
            "before": {"name": "C", "index": 2, "identity_status": "stable"},
            "after": {"name": "B", "index": 1, "identity_status": "stable"},
            "outcome": "verified",
            "verified": True,
            "navigation_direction": "next",
            "navigation_steps": 1,
            "max_navigation_steps": 4,
            "settle_tick_limit": 1,
            "undo_point_created": False,
            "session_fingerprint": SESSION,
            "session_precondition_applied": True,
            "expected_before_applied": True,
            "project_saved": False,
            "warnings": [],
        }
        client = Client({"plugin.select_preset": selection}, writable=True)
        controller = TrackBController(TrackBMutationGateway(client))
        with self.assertRaises(ValueError):
            controller.select_plugin_preset(
                target=MixerEffectTarget(track_index=3, slot_index=0),
                preset_name="B",
                expected_current=ExpectedPluginPresetState(name="A", index=0),
                session_fingerprint=SESSION,
                max_navigation_steps=4,
            )


if __name__ == "__main__":
    unittest.main()
