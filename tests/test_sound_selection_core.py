"""Deterministic pure Sound Selection core tests."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from fl_studio_mcp.sound_selection import (
    PaletteApplyReceipt,
    SoundCandidate,
    SoundInventory,
    SoundPaletteAssignment,
    SoundPaletteState,
    SoundPaletteStateRegistry,
    SoundRoleRequest,
    SoundSelectionRequest,
    SoundTargetInventory,
    classify_preset_name,
    create_palette_variation,
    load_descriptor_vocabulary,
    plan_palette,
    rank_candidates,
)
from fl_studio_mcp.track_b_contracts import ChannelGeneratorTarget


class SoundSelectionCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vital = ChannelGeneratorTarget(channel_index=1)
        self.stock = ChannelGeneratorTarget(channel_index=2)

    def test_descriptor_data_is_versioned_and_name_evidence_is_weak(self) -> None:
        catalog = load_descriptor_vocabulary()
        self.assertEqual(catalog.schema_version, "1.0")
        evidence = classify_preset_name("Bright Pluck")
        self.assertIn("bright", {item.descriptor for item in evidence})
        self.assertTrue(all(item.confidence < 0.5 for item in evidence))

    def test_drum_roles_default_to_the_minimum_safe_pattern_map(self) -> None:
        direct = SoundRoleRequest(role_id="drums", role_type="drums")
        parsed = SoundRoleRequest.model_validate(
            {"role_id": "drums", "role_type": "drums"}
        )
        expected = ("kick", "snare", "closed_hat")
        self.assertEqual(direct.required_drum_roles, expected)
        self.assertEqual(parsed.required_drum_roles, expected)

    def test_tuple_text_elements_are_bounded_individually(self) -> None:
        with self.assertRaises(ValueError):
            SoundRoleRequest(
                role_id="texture",
                technique_ids=("x" * 4097,),
            )

    def test_section_scope_is_carried_by_the_assignment_when_inventory_is_unknown(self) -> None:
        role = SoundRoleRequest(
            role_id="texture",
            role_type="texture",
            section_scope=("drop",),
        )
        request = SoundSelectionRequest(brief="drop texture", roles=(role,))
        candidate = SoundCandidate(
            candidate_id="texture",
            target=self.vital,
            product_id="synth",
            product_name="Synth",
            current_preset="Texture",
            candidate_preset="Texture",
            preset_identity_stable=True,
            preset_navigation_available=True,
        )
        plan = plan_palette(request, (candidate,))
        self.assertEqual(plan.blockers, ())
        self.assertEqual(plan.assignments[0].section_scope, ("drop",))

    def test_new_role_cannot_reuse_a_target_from_an_omitted_existing_role(self) -> None:
        role = SoundRoleRequest(role_id="texture", role_type="texture")
        request = SoundSelectionRequest(brief="add a texture", roles=(role,))
        candidate = SoundCandidate(
            candidate_id="texture",
            target=self.vital,
            product_id="synth",
            product_name="Synth",
            current_preset="Texture",
            candidate_preset="Texture",
            preset_identity_stable=True,
            preset_navigation_available=True,
        )
        existing = (
            # This role is intentionally omitted from the new request but
            # remains physically assigned to the only available target.
            SoundPaletteAssignment(
                role_id="main_lead",
                target=self.vital,
                product_id="synth",
                product_name="Synth",
                selected_preset="Lead",
            ),
        )
        plan = plan_palette(request, (candidate,), existing=existing)
        self.assertEqual(plan.assignments, ())
        self.assertIn("no loaded candidate satisfies required role 'texture'", plan.blockers)

    def test_explicit_product_preference_and_exclusion_are_hard(self) -> None:
        role = SoundRoleRequest(role_id="main_lead", role_type="lead")
        request = SoundSelectionRequest(
            brief="use StockSynth for the lead",
            roles=(role,),
            product_preferences=("StockSynth",),
            product_exclusions=("Vital",),
        )
        rows = (
            SoundCandidate(
                candidate_id="vital-lead",
                target=self.vital,
                product_id="vital",
                product_name="Vital",
                candidate_preset="Bright Pluck",
                current_preset="Bright Pluck",
                product_origin="third_party",
                preset_identity_stable=True,
            ),
            SoundCandidate(
                candidate_id="stock-lead",
                target=self.stock,
                product_id="stock",
                product_name="StockSynth",
                candidate_preset="Lead One",
                current_preset="Lead One",
                product_origin="stock",
                preset_identity_stable=True,
            ),
        )
        ranked = rank_candidates(rows, role, request)
        self.assertEqual(ranked[0].product_id, "stock")
        self.assertIn("explicit product exclusion", ranked[1].disqualification_reasons)

    def test_same_state_and_seed_are_order_independent(self) -> None:
        role = SoundRoleRequest(role_id="texture", role_type="texture", desired_descriptors=("bright",))
        request = SoundSelectionRequest(brief="bright texture", roles=(role,), seed=19)
        rows = tuple(
            SoundCandidate(
                candidate_id=f"texture-{index}",
                target=ChannelGeneratorTarget(channel_index=index),
                product_id=f"p{index}",
                product_name=f"Synth {index}",
                candidate_preset="Texture",
                current_preset="Texture",
                descriptors=("bright",),
                preset_identity_stable=True,
            )
            for index in (3, 4, 5)
        )
        first = rank_candidates(rows, role, request)
        second = rank_candidates(tuple(reversed(rows)), role, request)
        self.assertEqual(
            [item.candidate_id for item in first],
            [item.candidate_id for item in second],
        )
        self.assertTrue(all(item.score_breakdown.totals_consistent for item in first))

    def test_inventory_keeps_large_reported_count_but_bounded_page(self) -> None:
        observed = SoundTargetInventory(
            target=self.vital,
            product_name="Observed Synth",
            preset_count=1_000_000,
            preset_names=("Bright Pluck", "Dark Pad"),
            preset_indices=(10, 999_999),
            current_preset="Bright Pluck",
            preset_navigation_available=True,
        )
        self.assertEqual(observed.preset_count, 1_000_000)
        self.assertEqual(len(observed.candidates()), 2)
        self.assertEqual(observed.candidates()[0].preset_index, 10)

    def test_blank_preset_rows_are_not_executable_candidates(self) -> None:
        observed = SoundTargetInventory(
            target=self.vital,
            product_name="Observed Synth",
            preset_names=("", "  ", "Bright Pluck"),
            preset_indices=(0, 1, 2),
            preset_navigation_available=True,
        )

        candidates = observed.candidates()

        self.assertEqual(
            [(item.candidate_preset, item.preset_index) for item in candidates],
            [("Bright Pluck", 2)],
        )

    def test_blank_only_preset_page_without_current_has_no_candidates(self) -> None:
        observed = SoundTargetInventory(
            target=self.vital,
            product_name="Observed Synth",
            preset_names=("", "  "),
            preset_indices=(0, 1),
            preset_navigation_available=True,
        )

        self.assertEqual(observed.candidates(), ())

    def test_plan_is_read_only_and_variation_defaults_to_loaded_roles(self) -> None:
        role = SoundRoleRequest(
            role_id="main_lead",
            role_type="lead",
            desired_descriptors=("bright",),
        )
        request = SoundSelectionRequest(brief="bright lead", roles=(role,))
        target = SoundTargetInventory(
            target=self.vital,
            product_id="vital",
            product_name="Vital",
            current_preset="Bright Pluck",
            preset_names=("Bright Pluck",),
            preset_indices=(1,),
            preset_navigation_available=True,
            preset_identity_stable=True,
        )
        inventory = SoundInventory(
            session_fingerprint="a" * 64,
            loaded_generators=(target,),
        )
        before = inventory.model_dump(mode="json")
        plan = plan_palette(request, inventory)
        self.assertEqual(plan.assignments[0].selection_action, "keep_current")
        self.assertFalse(plan.mutations_applied)
        self.assertEqual(inventory.model_dump(mode="json"), before)

    def test_palette_identity_changes_when_inventory_changes_in_same_session(self) -> None:
        role = SoundRoleRequest(role_id="texture", role_type="texture")
        request = SoundSelectionRequest(brief="choose a texture", roles=(role,))
        first = SoundInventory(
            session_fingerprint="a" * 32,
            loaded_generators=(
                SoundTargetInventory(
                    target=self.vital,
                    product_id="first",
                    product_name="First Synth",
                    current_preset="One",
                    preset_names=("One",),
                    preset_indices=(0,),
                    preset_identity_stable=True,
                ),
            ),
        )
        second = SoundInventory(
            session_fingerprint="a" * 32,
            loaded_generators=(
                SoundTargetInventory(
                    target=self.vital,
                    product_id="second",
                    product_name="Second Synth",
                    current_preset="Two",
                    preset_names=("Two",),
                    preset_indices=(0,),
                    preset_identity_stable=True,
                ),
            ),
        )

        self.assertNotEqual(
            plan_palette(request, first).palette_id,
            plan_palette(request, second).palette_id,
        )

    def test_palette_identity_is_independent_of_inventory_order_but_tracks_state_flags(self) -> None:
        role = SoundRoleRequest(role_id="texture", role_type="texture")
        request = SoundSelectionRequest(brief="choose a texture", roles=(role,))
        first_target = SoundTargetInventory(
            target=self.vital,
            product_id="first",
            product_name="First Synth",
            current_preset="One",
            preset_names=("One",),
            preset_indices=(0,),
            preset_navigation_available=True,
            preset_identity_stable=True,
        )
        second_target = SoundTargetInventory(
            target=self.stock,
            product_id="second",
            product_name="Second Synth",
            current_preset="Two",
            preset_names=("Two",),
            preset_indices=(0,),
            preset_navigation_available=True,
            preset_identity_stable=True,
        )
        first = SoundInventory(
            session_fingerprint="a" * 32,
            loaded_generators=(first_target, second_target),
        )
        reordered = SoundInventory(
            session_fingerprint="a" * 32,
            loaded_generators=(second_target, first_target),
        )
        self.assertEqual(
            plan_palette(request, first).palette_id,
            plan_palette(request, reordered).palette_id,
        )

        changed_target = first_target.model_copy(
            update={"current_preset": "Other", "preset_navigation_available": False}
        )
        changed = SoundInventory(
            session_fingerprint="a" * 32,
            loaded_generators=(changed_target, second_target),
        )
        self.assertNotEqual(
            plan_palette(request, first).palette_id,
            plan_palette(request, changed).palette_id,
        )

    def test_registry_put_cannot_erase_completed_receipts(self) -> None:
        role = SoundRoleRequest(role_id="texture", role_type="texture")
        request = SoundSelectionRequest(brief="choose a texture", roles=(role,))
        candidate = SoundCandidate(
            candidate_id="texture",
            target=self.vital,
            product_id="synth",
            product_name="Synth",
            current_preset="Texture",
            candidate_preset="Texture",
            preset_identity_stable=True,
            preset_navigation_available=True,
        )
        plan = plan_palette(request, (candidate,))
        registry = SoundPaletteStateRegistry()
        state = registry.register_plan(plan)
        receipt = PaletteApplyReceipt(
            assignment_id=plan.assignments[0].assignment_id,
            role_id="texture",
            verified=True,
            verification_summary="verified",
            selected_preset="Texture",
        )
        applied = registry.record_receipts(plan.palette_id, (receipt,))
        with self.assertRaisesRegex(ValueError, "append-only"):
            registry.put(applied.model_copy(update={"apply_receipts": ()}))
        self.assertEqual(registry.require(plan.palette_id).apply_receipts, (receipt,))
        self.assertEqual(state.palette_id, applied.palette_id)

    def test_palette_receipts_are_bounded(self) -> None:
        stamp = datetime(2025, 1, 1, tzinfo=timezone.utc)
        receipts = tuple(
            PaletteApplyReceipt(
                assignment_id=f"assignment-{index}",
                role_id="texture",
                verified=False,
                verification_summary="unknown",
            )
            for index in range(129)
        )
        with self.assertRaises(ValueError):
            SoundPaletteState(
                palette_id="palette-bounded",
                created_at=stamp,
                updated_at=stamp,
                apply_receipts=receipts,
            )

        request = SoundSelectionRequest(brief="bounded receipts", roles=())
        inventory = SoundInventory(session_fingerprint="a" * 32)
        plan = plan_palette(request, inventory)
        registry = SoundPaletteStateRegistry()
        registry.register_plan(plan)
        registry.record_receipts(plan.palette_id, receipts[:128], status="failed")
        with self.assertRaisesRegex(ValueError, "receipt bound"):
            registry.record_receipts(
                plan.palette_id,
                (
                    PaletteApplyReceipt(
                        assignment_id="assignment-overflow",
                        role_id="texture",
                        verified=False,
                        verification_summary="unknown",
                    ),
                ),
                status="failed",
            )

    def test_applied_palette_can_record_a_failed_section_delta(self) -> None:
        stamp = datetime(2025, 1, 1, tzinfo=timezone.utc)
        request = SoundSelectionRequest(brief="section delta", roles=())
        plan = plan_palette(
            request,
            SoundInventory(session_fingerprint="a" * 32),
        )
        registry = SoundPaletteStateRegistry()
        registry.register_plan(plan, now=stamp)
        base_receipt = PaletteApplyReceipt(
            assignment_id="assignment-base",
            role_id="texture",
            verified=True,
            verification_summary="base verified",
        )
        registry.record_receipts(
            plan.palette_id,
            (base_receipt,),
            status="applied",
            now=stamp,
        )
        delta_receipt = PaletteApplyReceipt(
            assignment_id="assignment-delta",
            role_id="texture",
            verified=False,
            verification_summary="delta outcome unknown",
        )
        updated = registry.record_receipts(
            plan.palette_id,
            (delta_receipt,),
            status="partially_applied",
            now=stamp,
        )
        self.assertEqual(updated.apply_receipts, (base_receipt, delta_receipt))
        self.assertEqual(updated.status, "partially_applied")

    def test_section_variation_preserves_anchor_and_changes_flexible_role(self) -> None:
        lead = SoundRoleRequest(role_id="main_lead", role_type="lead")
        texture = SoundRoleRequest(role_id="texture", role_type="texture")
        request = SoundSelectionRequest(brief="lead and texture", roles=(lead, texture))
        target_a = SoundTargetInventory(
            target=self.vital,
            product_id="a",
            product_name="Synth A",
            current_preset="A",
            preset_names=("A",),
            preset_indices=(0,),
            preset_navigation_available=True,
            preset_identity_stable=True,
        )
        target_b = SoundTargetInventory(
            target=self.stock,
            product_id="b",
            product_name="Synth B",
            current_preset="B",
            preset_names=("B",),
            preset_indices=(0,),
            preset_navigation_available=True,
            preset_identity_stable=True,
        )
        target_c = SoundTargetInventory(
            target=ChannelGeneratorTarget(channel_index=3),
            product_id="c",
            product_name="Synth C",
            current_preset="C",
            preset_names=("C",),
            preset_indices=(0,),
            preset_navigation_available=True,
            preset_identity_stable=True,
        )
        inventory = SoundInventory(
            session_fingerprint="b" * 32,
            loaded_generators=(target_a, target_b, target_c),
        )
        base = plan_palette(request, inventory)
        variation = create_palette_variation(base, request, inventory, section="drop")
        self.assertIn("main_lead", variation.unchanged_role_ids)
        self.assertTrue(
            any(item.role_id == "texture" for item in variation.assignments)
        )

    def test_explicit_variation_replacement_walks_past_continuity_winner(self) -> None:
        role = SoundRoleRequest(role_id="main_lead", role_type="lead")
        base_request = SoundSelectionRequest(
            brief="keep A as the lead",
            roles=(role,),
            preset_preferences=("A",),
        )
        variation_request = SoundSelectionRequest(
            brief="replace the lead with B",
            roles=(role,),
            preset_preferences=("B",),
        )
        candidates = (
            SoundCandidate(
                candidate_id="a",
                target=self.vital,
                product_id="synth-a",
                product_name="Synth A",
                current_preset="A",
                candidate_preset="A",
                preset_index=0,
                preset_navigation_available=True,
                preset_identity_stable=True,
            ),
            SoundCandidate(
                candidate_id="b",
                target=self.stock,
                product_id="synth-b",
                product_name="Synth B",
                current_preset="B",
                candidate_preset="B",
                preset_index=0,
                preset_navigation_available=True,
                preset_identity_stable=True,
            ),
        )
        base = plan_palette(base_request, candidates)
        variation = create_palette_variation(
            base,
            variation_request,
            candidates,
            section="drop",
            replace_roles=("main_lead",),
        )
        self.assertEqual(variation.assignments[0].product_id, "synth-b")


if __name__ == "__main__":
    unittest.main()
