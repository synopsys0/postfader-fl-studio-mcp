#!/usr/bin/env python3
"""Synthetic FL checks for bounded preset and Loop Starter bridge commands."""

from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, os.fspath(HERE / "fakefl"))

import _state  # noqa: E402


BRIDGE_PATH = ROOT / "fl_studio_mcp" / "_bridge" / "device_UniversalBridge.py"
_spec = importlib.util.spec_from_file_location("preset_bridge_test", BRIDGE_PATH)
assert _spec is not None and _spec.loader is not None
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


def exhaust(generator: types.GeneratorType) -> tuple[dict, int]:
    """Drive a bridge generator as FL would, returning result and idle ticks."""

    yielded = 0
    while True:
        try:
            next(generator)
        except StopIteration as stopped:
            return stopped.value, yielded
        yielded += 1
        bridge._idle_tick += 1


def plugin(
    presets: list[str] | None = None,
    pads: list[dict[str, object]] | None = None,
) -> object:
    return _state.Plugin(
        "Synthetic Preset Plug",
        [("Gain", 0.5)],
        presets=presets,
        pads=pads,
    )


EFFECT = {
    "target_kind": "mixer_effect",
    "track": 3,
    "slot": 0,
    "use_global_index": False,
}
GENERATOR = {
    "target_kind": "channel_generator",
    "channel": 0,
    "slot": -1,
    "use_global_index": True,
    "index_scope": "global",
}


class PresetBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        _state.reset()
        bridge.MAX_PRESET_ENUMERATION = 4096
        bridge.LEAN_WRITES_ENABLED = True
        bridge._idle_tick = 0
        bridge._jobs[:] = []
        self.session = bridge.SESSION_FINGERPRINT

    def install_effect(
        self,
        presets: list[str] | None = None,
        pads: list[dict[str, object]] | None = None,
    ) -> object:
        selected = plugin(presets, pads)
        _state.TRACKS[3].slots[0] = selected
        return selected

    def install_generator(self, presets: list[str]) -> object:
        selected = plugin(presets)
        _state.CHANNELS[0].generator_plugin = selected
        return selected

    def test_reads_effect_and_global_generator_with_explicit_scopes(self) -> None:
        self.install_effect(["A", "B", "C"])
        result, ticks = exhaust(
            bridge.cmd_plugin_presets({**EFFECT, "start": 0, "limit": 3})
        )
        self.assertEqual(ticks, 0)
        self.assertEqual(result["track"], 3)
        self.assertFalse(result["use_global_index"])
        self.assertEqual([row["name"] for row in result["presets"]], ["A", "B", "C"])
        self.assertEqual(result["current_preset_name"], "A")
        self.assertEqual(result["current_preset_index"], 0)
        self.assertEqual(len(result["target_fingerprint"]), 64)

        self.install_generator(["Pad", "Lead"])
        result, _ = exhaust(
            bridge.cmd_plugin_current_preset(dict(GENERATOR))
        )
        self.assertEqual(result["channel"], 0)
        self.assertEqual(result["slot"], -1)
        self.assertTrue(result["use_global_index"])
        self.assertEqual(result["index_scope"], "global")
        self.assertEqual(result["current_preset_name"], "Pad")
        self.assertEqual(len(result["target_fingerprint"]), 64)

    def test_pagination_blank_duplicate_and_bounds_are_truthful(self) -> None:
        self.install_effect(["A", "", "B", "A"])
        result, _ = exhaust(
            bridge.cmd_plugin_presets(
                {**EFFECT, "start": 0, "limit": 4, "include_empty_names": False}
            )
        )
        self.assertEqual([row["index"] for row in result["presets"]], [0, 2, 3])
        self.assertEqual(result["blank_name_indices"], [1])
        self.assertEqual(result["duplicate_names"], ["A"])
        self.assertTrue(result["partial"] is False)
        with self.assertRaises(ValueError):
            exhaust(bridge.cmd_plugin_presets({**EFFECT, "limit": 0}))
        with self.assertRaises(ValueError):
            exhaust(bridge.cmd_plugin_presets({**EFFECT, "start": 5}))

    def test_large_catalog_preserves_current_name_but_not_unresolved_index(self) -> None:
        self.install_effect(["Current", "P1", "P2", "P3", "P4"])
        bridge.MAX_PRESET_ENUMERATION = 3
        result, _ = exhaust(
            bridge.cmd_plugin_presets({**EFFECT, "start": 0, "limit": 2})
        )
        self.assertEqual(result["current_preset_name"], "Current")
        self.assertIsNone(result["current_preset_index"])
        self.assertEqual(result["current_preset_status"], "stable")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncated_by"], "max_enumeration")

    def test_pad_map_is_generic_bounded_and_normalizes_values(self) -> None:
        self.install_effect(
            ["A"],
            [
                {"semitone": 36, "color": -1, "empty": False, "muted": True},
                {"semitone": 42, "color": 0x010203, "empty": True, "muted": False},
            ],
        )
        result, _ = exhaust(bridge.cmd_plugin_pad_map(dict(EFFECT)))
        self.assertTrue(result["complete"])
        self.assertEqual(result["pad_count"], 2)
        self.assertEqual(result["pads"][0]["color"], 0xFFFFFFFF)
        self.assertEqual(result["pads"][0]["semitone"], 36)
        self.assertEqual(result["pads"][0]["semitone_name"], "Note 36")
        self.assertTrue(result["pads"][1]["empty"])

    def test_exact_name_and_index_use_shortest_navigation_and_later_ticks(self) -> None:
        selected = self.install_effect(["A", "B", "C", "D"])
        with mock.patch.object(
            bridge.plugins, "nextPreset", wraps=bridge.plugins.nextPreset
        ) as next_preset:
            result, ticks = exhaust(
                bridge.cmd_plugin_select_preset(
                    {**EFFECT, "preset_name": "C", "max_navigation_steps": 4}
                )
            )
        self.assertEqual(result["outcome"], "verified")
        self.assertTrue(result["verified"])
        self.assertEqual(result["navigation_direction"], "next")
        self.assertEqual(result["navigation_steps"], 2)
        self.assertEqual(ticks, 2)
        self.assertEqual(next_preset.call_count, 2)
        self.assertEqual(result["after"]["name"], "C")
        self.assertEqual(len(_state.UNDO), 1)

        _state.reset()
        selected = self.install_effect(["A", "B", "C", "D"])
        with mock.patch.object(
            bridge.plugins, "prevPreset", wraps=bridge.plugins.prevPreset
        ) as prev_preset:
            result, ticks = exhaust(
                bridge.cmd_plugin_select_preset(
                    {**EFFECT, "preset_index": 3, "max_navigation_steps": 4}
                )
            )
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(result["navigation_direction"], "prev")
        self.assertEqual(result["navigation_steps"], 1)
        self.assertEqual(ticks, 1)
        self.assertEqual(prev_preset.call_count, 1)
        self.assertEqual(result["requested_preset_name"], "D")
        self.assertIsNotNone(selected)

    def test_indexed_selection_proves_duplicate_occurrence_from_navigation_path(self) -> None:
        self.install_effect(["A", "Duplicate", "Duplicate"])
        result, ticks = exhaust(
            bridge.cmd_plugin_select_preset(
                {**EFFECT, "preset_index": 2, "max_navigation_steps": 2}
            )
        )
        self.assertEqual(result["outcome"], "verified")
        self.assertTrue(result["verified"])
        self.assertEqual(result["requested_preset_name"], "Duplicate")
        self.assertEqual(result["requested_preset_index"], 2)
        self.assertEqual(result["after"]["name"], "Duplicate")
        self.assertEqual(result["after"]["index"], 2)
        self.assertEqual(result["navigation_direction"], "prev")
        self.assertEqual(result["navigation_steps"], 1)
        self.assertEqual(ticks, 1)

    def test_ambiguous_name_is_refused_before_dispatch(self) -> None:
        self.install_effect(["A", "Duplicate", "B", "Duplicate"])
        with self.assertRaises(ValueError):
            exhaust(
                bridge.cmd_plugin_select_preset(
                    {**EFFECT, "preset_name": "Duplicate"}
                )
            )
        self.assertEqual(_state.UNDO, [])

    def test_bounded_fallback_proves_one_name_cycle_when_indexed_reads_fail(self) -> None:
        self.install_effect(["A", "B", "C", "D"])
        original = bridge.plugins.getName

        def no_indexed_names(
            index: int,
            slot: int = -1,
            flag: int = 0,
            paramIndex: int = 0,
            useGlobalIndex: bool = False,
        ) -> str:
            if flag == bridge.midi.FPN_Preset and paramIndex != bridge.midi.GPN_GetCurrentPreset:
                raise RuntimeError("indexed names unsupported")
            return original(index, slot, flag, paramIndex, useGlobalIndex)

        with mock.patch.object(bridge.plugins, "getName", side_effect=no_indexed_names):
            result, ticks = exhaust(
                bridge.cmd_plugin_select_preset(
                    {**EFFECT, "preset_name": "B", "max_navigation_steps": 10}
                )
            )
        self.assertEqual(result["navigation_direction"], "fallback_next")
        self.assertEqual(result["outcome"], "verified")
        self.assertTrue(result["verified"])
        self.assertEqual(result["navigation_steps"], 5)
        self.assertEqual(ticks, 5)

        self.install_effect(["A", "B", "C", "B"])
        with mock.patch.object(bridge.plugins, "getName", side_effect=no_indexed_names):
            result, ticks = exhaust(
                bridge.cmd_plugin_select_preset(
                    {**EFFECT, "preset_name": "B", "max_navigation_steps": 10}
                )
            )
        self.assertEqual(result["outcome"], "unknown")
        self.assertFalse(result["verified"])
        self.assertLessEqual(ticks, 10)
        self.assertTrue(any("ambiguous" in item for item in result["warnings"]))

    def test_unstable_readback_and_dispatch_errors_never_claim_success_or_retry(self) -> None:
        self.install_effect(["A", "B"])
        with mock.patch.object(bridge.plugins, "getName", side_effect=RuntimeError("unsupported")):
            with self.assertRaises(ValueError):
                exhaust(
                    bridge.cmd_plugin_select_preset(
                        {**EFFECT, "preset_name": "B"}
                    )
                )
        self.assertEqual(_state.UNDO, [])

        self.install_effect(["A", "B"])
        dispatches = 0

        def fail_dispatch(*args: object, **kwargs: object) -> None:
            nonlocal dispatches
            dispatches += 1
            raise RuntimeError("ambiguous transport failure")

        with mock.patch.object(bridge.plugins, "nextPreset", side_effect=fail_dispatch):
            result, _ = exhaust(
                bridge.cmd_plugin_select_preset(
                    {**EFFECT, "preset_name": "B"}
                )
            )
        self.assertEqual(result["outcome"], "unknown")
        self.assertFalse(result["verified"])
        self.assertEqual(dispatches, 1)

    def test_session_and_target_changes_stop_before_further_dispatch(self) -> None:
        self.install_effect(["A", "B", "C"])
        original_next = bridge.plugins.nextPreset
        old_session = bridge.SESSION_FINGERPRINT

        def change_session(*args: object, **kwargs: object) -> None:
            original_next(*args, **kwargs)
            bridge.SESSION_FINGERPRINT = "f" * 32

        try:
            with mock.patch.object(bridge.plugins, "nextPreset", side_effect=change_session):
                result, _ = exhaust(
                    bridge.cmd_plugin_select_preset(
                        {
                            **EFFECT,
                            "preset_name": "B",
                            "session_fingerprint": old_session,
                        }
                    )
                )
        finally:
            bridge.SESSION_FINGERPRINT = old_session
        self.assertEqual(result["outcome"], "unknown")
        self.assertFalse(result["verified"])
        self.assertTrue(any("session changed" in item for item in result["warnings"]))

        self.install_effect(["A", "B"])
        original_next = bridge.plugins.nextPreset

        def change_target(*args: object, **kwargs: object) -> None:
            original_next(*args, **kwargs)
            _state.TRACKS[3].slots[0].name = "Replaced Target"

        with mock.patch.object(bridge.plugins, "nextPreset", side_effect=change_target):
            result, _ = exhaust(
                bridge.cmd_plugin_select_preset({**EFFECT, "preset_name": "B"})
            )
        self.assertEqual(result["outcome"], "unknown")
        self.assertFalse(result["verified"])
        self.assertTrue(any("target identity changed" in item for item in result["warnings"]))

    def test_loop_starter_is_explicit_global_single_dispatch_and_unverified(self) -> None:
        channel = 1
        before = bridge._channel_summary(channel)
        with mock.patch.object(
            bridge.channels,
            "rerollLoopStarterLoop",
            wraps=bridge.channels.rerollLoopStarterLoop,
        ) as reroll:
            result = bridge.cmd_channels_reroll_loop_starter_loop(
                {
                    "channel": channel,
                    "index_scope": "global",
                    "session_fingerprint": self.session,
                    "expected_before": {
                        "channel_fingerprint": before["channel_fingerprint"]
                    },
                }
            )
        self.assertEqual(reroll.call_count, 1)
        self.assertTrue(result["dispatched"])
        self.assertFalse(result["identity_verified"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["index_scope"], "global")
        self.assertEqual(
            getattr(_state.CHANNELS[channel], "loop_starter_rerolls", 0), 1
        )

        with self.assertRaises(ValueError):
            bridge.cmd_channels_reroll_loop_starter_loop(
                {
                    "channel": channel,
                    "index_scope": "global",
                    "expected_before": {"channel_fingerprint": "0" * 64},
                }
            )
        self.assertEqual(getattr(_state.CHANNELS[channel], "loop_starter_rerolls", 0), 1)


if __name__ == "__main__":
    unittest.main()
