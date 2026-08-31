#!/usr/bin/env python3
"""Strict, immutable contracts for the low-level preset bridge surface."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from fl_studio_mcp.track_b_contracts import (
    ExpectedPluginPresetState,
    LoopStarterRerollDispatch,
    MixerEffectTarget,
    PluginCurrentPreset,
    PluginPad,
    PluginPadMap,
    PluginPresetPage,
    PluginPresetRecord,
    PluginPresetState,
    TargetedPluginSummary,
    VerifiedPluginPresetSelection,
)


NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)
EFFECT = MixerEffectTarget(track_index=1, slot_index=0)
PLUGIN = TargetedPluginSummary(
    target=EFFECT,
    name="Synthetic Synth",
    target_fingerprint="a" * 64,
    reported_parameter_count=4,
    mix_level_normalized=0.5,
)


def page(**overrides: object) -> PluginPresetPage:
    values: dict[str, object] = {
        "observed_at": NOW,
        "plugin": PLUGIN,
        "preset_count": 3,
        "start": 0,
        "limit": 3,
        "scanned_count": 3,
        "returned_count": 3,
        "has_more": False,
        "next_start": None,
        "presets": [
            PluginPresetRecord(index=0, name="A", is_current=True),
            PluginPresetRecord(index=1, name="B"),
            PluginPresetRecord(index=2, name="C"),
        ],
        "current_preset_name": "A",
        "current_preset_index": 0,
        "current_preset_status": "stable",
        "duplicate_names": [],
        "blank_name_indices": [],
        "partial": False,
        "truncated": False,
        "truncated_by": None,
    }
    values.update(overrides)
    return PluginPresetPage(**values)


class PresetContractTests(unittest.TestCase):
    def test_large_catalog_can_preserve_name_without_fabricating_index(self) -> None:
        state = PluginPresetState(
            name="Authoritative Current", index=None, identity_status="stable"
        )
        self.assertEqual(state.name, "Authoritative Current")
        self.assertIsNone(state.index)

    def test_identity_statuses_are_not_coercible_or_cross_contaminated(self) -> None:
        with self.assertRaises(ValidationError):
            PluginPresetState(name="", identity_status="stable")
        with self.assertRaises(ValidationError):
            PluginPresetState(name="Still known", identity_status="unsupported")
        with self.assertRaises(ValidationError):
            PluginPresetState(name=None, index=2, identity_status="unresolved")
        unresolved = PluginPresetState(
            name="Reported but not indexed", index=None, identity_status="unresolved"
        )
        self.assertEqual(unresolved.name, "Reported but not indexed")

    def test_page_is_frozen_and_has_bounded_ordered_rows(self) -> None:
        observed = page()
        with self.assertRaises(ValidationError):
            observed.start = 1  # type: ignore[misc]
        with self.assertRaises(ValidationError):
            page(
                presets=[
                    PluginPresetRecord(index=0, name="A"),
                    PluginPresetRecord(index=0, name="B"),
                    PluginPresetRecord(index=2, name="C"),
                ]
            )
        with self.assertRaises(ValidationError):
            page(
                presets=[
                    PluginPresetRecord(index=0, name="A"),
                    PluginPresetRecord(index=1, name="B"),
                    PluginPresetRecord(index=4, name="C"),
                ]
            )
        with self.assertRaises(ValidationError):
            page(returned_count=3, scanned_count=2)
        with self.assertRaises(ValidationError):
            page(blank_name_indices=[3])

    def test_page_rejects_contradictory_duplicate_blank_and_current_annotations(self) -> None:
        with self.assertRaises(ValidationError):
            page(duplicate_names=["B"])
        with self.assertRaises(ValidationError):
            page(blank_name_indices=[0])
        with self.assertRaises(ValidationError):
            page(
                presets=[
                    PluginPresetRecord(index=0, name="A", is_current=True),
                    PluginPresetRecord(index=1, name="B"),
                    PluginPresetRecord(index=2, name="C"),
                ],
                current_preset_name="B",
                current_preset_index=1,
            )
        with self.assertRaises(ValidationError):
            page(
                presets=[
                    PluginPresetRecord(index=0, name="A", is_current=True),
                    PluginPresetRecord(index=1, name="B"),
                    PluginPresetRecord(index=2, name="C"),
                ],
                current_preset_status="unresolved",
                current_preset_index=None,
            )

    def test_page_allows_a_truthful_partial_page_without_complete_identity(self) -> None:
        observed = page(
            preset_count=6,
            has_more=True,
            next_start=3,
            partial=True,
            truncated=True,
        )
        self.assertTrue(observed.partial)
        self.assertTrue(observed.has_more)
        self.assertEqual(observed.next_start, 3)

    def test_current_and_pad_contracts_preserve_unsupported_state(self) -> None:
        current = PluginCurrentPreset(
            observed_at=NOW,
            plugin=PLUGIN,
            preset_count=12,
            current_preset_name="Known Name",
            current_preset_index=None,
            current_preset_status="stable",
        )
        self.assertEqual(current.current.name, "Known Name")
        unsupported = PluginCurrentPreset(
            observed_at=NOW,
            plugin=PLUGIN,
            preset_count=12,
            current_preset_name=None,
            current_preset_index=None,
            current_preset_status="unsupported",
        )
        self.assertEqual(unsupported.current.identity_status, "unsupported")
        unresolved = PluginCurrentPreset(
            observed_at=NOW,
            plugin=PLUGIN,
            preset_count=12,
            current_preset_name="Reported but not indexed",
            current_preset_index=None,
            current_preset_status="unresolved",
        )
        self.assertEqual(unresolved.current.name, "Reported but not indexed")
        pads = PluginPadMap(
            observed_at=NOW,
            plugin=PLUGIN,
            pad_count=2,
            pads=[
                PluginPad(pad_index=0, semitone=36, color=0x010203, empty=False, muted=False),
                PluginPad(pad_index=1, semitone=38, color=0x040506, empty=True, muted=False),
            ],
            complete=True,
        )
        self.assertEqual(len(pads.pads), 2)
        with self.assertRaises(ValidationError):
            PluginPadMap(
                observed_at=NOW,
                plugin=PLUGIN,
                pad_count=2,
                pads=[PluginPad(pad_index=1, semitone=36)],
                complete=False,
            )

    def test_selection_proof_cannot_claim_unverified_or_wrong_identity(self) -> None:
        common = {
            "applied_at": NOW,
            "verified": True,
            "verification_summary": "verified",
            "target": EFFECT,
            "plugin": PLUGIN,
            "requested_preset_name": "B",
            "requested_preset_index": 1,
            "before": PluginPresetState(name="A", index=0, identity_status="stable"),
            "after": PluginPresetState(name="B", index=1, identity_status="stable"),
            "outcome": "verified",
            "navigation_direction": "next",
            "navigation_steps": 1,
            "max_navigation_steps": 4,
            "settle_tick_limit": 1,
            "target_fingerprint": "a" * 64,
        }
        receipt = VerifiedPluginPresetSelection(**common)
        self.assertTrue(receipt.verified)
        with self.assertRaises(ValidationError):
            VerifiedPluginPresetSelection(
                **{**common, "after": PluginPresetState(name="C", index=2, identity_status="stable")}
            )
        with self.assertRaises(ValidationError):
            VerifiedPluginPresetSelection(**{**common, "outcome": "unknown"})

    def test_loop_starter_is_always_dispatch_only(self) -> None:
        receipt = LoopStarterRerollDispatch(
            observed_at=NOW,
            channel_index=2,
            dispatched=True,
            before_channel_fingerprint="b" * 64,
            after_channel_fingerprint="b" * 64,
        )
        self.assertFalse(receipt.verified)
        with self.assertRaises(ValidationError):
            LoopStarterRerollDispatch(
                observed_at=NOW,
                channel_index=2,
                dispatched=True,
                verified=True,  # type: ignore[arg-type]
            )

    def test_expected_current_requires_a_guard_field(self) -> None:
        with self.assertRaises(ValidationError):
            ExpectedPluginPresetState()
        self.assertEqual(ExpectedPluginPresetState(index=3).index, 3)


if __name__ == "__main__":
    unittest.main()
