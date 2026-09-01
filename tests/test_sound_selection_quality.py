"""Focused deterministic quality contracts for Sound Selection."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from fl_studio_mcp.sound_selection import (
    DescriptorEvidence,
    PreferenceDirective,
    PresetCandidateDiscoveryPolicy,
    PresetCatalog,
    PresetRecord,
    SoundCandidate,
    SoundHistoryDocument,
    SoundHistoryFeedback,
    SoundPaletteAssignment,
    SoundRoleFeedback,
    SoundRoleRequest,
    SoundSelectionRequest,
    discover_preset_candidates,
    enrich_candidate_metadata,
    load_preset_metadata,
    migrate_palette_assignment,
    normalize_descriptor,
    plan_palette,
    rank_shortlist,
    score_candidates,
)
from fl_studio_mcp.sound_selection.executor import SoundSelectionService
from fl_studio_mcp.sound_selection.history import LocalSoundSelectionHistory
from fl_studio_mcp.sound_selection.preset_catalog import PresetPage
from fl_studio_mcp.track_b_contracts import (
    ChannelGeneratorTarget,
    PluginPresetPage,
    PluginPresetRecord,
    TargetedLoadedPluginInventory,
    TargetedPluginSummary,
)


class SoundSelectionQualityTests(unittest.TestCase):
    target = ChannelGeneratorTarget(channel_index=1)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def _candidate(self, name: str, *, channel: int = 1, **updates: object) -> SoundCandidate:
        values: dict[str, object] = {
            "candidate_id": name.casefold().replace(" ", "-"),
            "target": ChannelGeneratorTarget(channel_index=channel),
            "product_id": "synth",
            "product_name": "Synth",
            "current_preset": "Current",
            "candidate_preset": name,
            "preset_index": channel,
            "preset_identity_stable": True,
            "preset_navigation_available": True,
        }
        values.update(updates)
        return SoundCandidate(**values)

    def test_small_catalog_is_complete_and_large_catalog_is_truthfully_stratified(self) -> None:
        small = PresetCatalog(
            target=self.target,
            product_name="Synth",
            reported_preset_count=3,
            complete=True,
            presets=tuple(PresetRecord(index=i, name=f"P{i}") for i in range(3)),
        )
        complete = discover_preset_candidates(small)
        self.assertEqual(complete.coverage.coverage_mode, "complete")
        self.assertEqual([row.name for row in complete.presets], ["P0", "P1", "P2"])

        large = PresetCatalog(
            target=self.target,
            product_name="Synth",
            reported_preset_count=500,
            presets=tuple(PresetRecord(index=i, name=f"P{i}") for i in range(500)),
        )
        sampled = discover_preset_candidates(
            large,
            requested_presets=("P499",),
            policy=PresetCandidateDiscoveryPolicy(max_candidates=32, seed_page_count=1),
            seed=9,
        )
        self.assertEqual(sampled.coverage.coverage_mode, "stratified")
        self.assertIn("P499", sampled.coverage.exact_preference_matches)
        self.assertNotEqual(sampled.coverage.omitted_count, 0)

    def test_exact_lookup_callback_and_seed_are_deterministic(self) -> None:
        calls: list[tuple[int, int]] = []

        def page(*, start: int, limit: int) -> PresetPage:
            calls.append((start, limit))
            return PresetPage(
                target=self.target,
                product_name="Synth",
                reported_preset_count=1000,
                start=start,
                limit=limit,
                presets=tuple(PresetRecord(index=i, name=f"P{i}") for i in range(start, min(start + limit, 1000))),
            )

        def exact(*, name: str) -> PresetRecord:
            return PresetRecord(index=997, name=name)

        first = discover_preset_candidates(
            reported_preset_count=1000,
            page_loader=page,
            exact_lookup=exact,
            requested_presets=("Far Away",),
            seed=42,
        )
        calls.clear()
        second = discover_preset_candidates(
            reported_preset_count=1000,
            page_loader=page,
            exact_lookup=exact,
            requested_presets=("Far Away",),
            seed=42,
        )
        self.assertEqual(first.model_dump(mode="json"), second.model_dump(mode="json"))
        self.assertIn("Far Away", first.coverage.exact_preference_matches)
        self.assertEqual(first.coverage.coverage_mode, "stratified")

    def test_small_live_page_loader_is_collectively_complete(self) -> None:
        calls: list[int] = []

        def page(*, start: int, limit: int) -> PresetPage:
            calls.append(start)
            count = 100
            end = min(start + limit, count)
            return PresetPage(
                target=self.target,
                product_name="Synth",
                reported_preset_count=count,
                start=start,
                limit=limit,
                presets=tuple(
                    PresetRecord(index=index, name=f"P{index}")
                    for index in range(start, end)
                ),
            )

        discovered = discover_preset_candidates(
            reported_preset_count=100,
            page_loader=page,
        )
        self.assertEqual(calls, [0, 64])
        self.assertEqual(discovered.coverage.coverage_mode, "complete")
        self.assertEqual(len(discovered.presets), 100)
        self.assertEqual(discovered.coverage.omitted_count, 0)
        self.assertEqual(
            {item.index for item in discovered.presets},
            set(range(100)),
        )

    def test_user_metadata_overlay_is_isolated_and_enriches_candidate(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "overlay.json"
            path.write_text(
                json.dumps(
                    {
                        "metadata_version": "user-1",
                        "records": [
                            {
                                "product_id": "synth",
                                "preset_name": "Current",
                                "descriptors": ["bright"],
                                "provenance": "unknown",
                                "confidence": "medium",
                            }
                        ],
                        "families": [],
                    }
                ),
                encoding="utf-8",
            )
            bundled = load_preset_metadata()
            merged = load_preset_metadata(user_overlay=path)
            self.assertEqual(bundled.layer, "bundled")
            self.assertEqual(len(bundled.records), 0)
            self.assertEqual(merged.layer, "merged")
            self.assertEqual(merged.records[0].provenance, "user_local_reviewed")
            candidate = self._candidate("Current")
            enriched = enrich_candidate_metadata(candidate, merged)
            self.assertEqual(enriched.metadata_confidence, "medium")
            self.assertEqual(enriched.descriptors, ("bright",))

    def test_bundled_family_metadata_is_reviewed_but_not_exact_preset_claims(self) -> None:
        bundled = load_preset_metadata()
        self.assertEqual(len(bundled.records), 0)
        self.assertGreater(len(bundled.families), 0)
        candidate = self._candidate(
            "Unreviewed",
            product_id="image-line.flex",
            product_name="FLEX",
            descriptor_provenance=(
                DescriptorEvidence(
                    descriptor="synthetic",
                    provenance="preset_name_token",
                    confidence=0.35,
                    original_term="Synth",
                ),
            ),
        )
        enriched = enrich_candidate_metadata(candidate, bundled)
        self.assertEqual(enriched.metadata_provenance, "bundled_reviewed")
        self.assertEqual(enriched.metadata_confidence, "medium")
        evidence = next(
            item for item in enriched.descriptor_provenance if item.descriptor == "synthetic"
        )
        self.assertEqual(evidence.provenance, "bundled_reviewed")

    def test_normal_plan_uses_bounded_discovery_and_exposes_coverage(self) -> None:
        session = "a" * 32
        fingerprint = "b" * 64
        summary = TargetedPluginSummary(
            target=self.target,
            name="FLEX",
            target_fingerprint=fingerprint,
            reported_parameter_count=1,
        )

        class Inspector:
            def __init__(self) -> None:
                self.page_reads = 0

            def scan_loaded_plugins(self, *, only_used: bool = False):
                return TargetedLoadedPluginInventory(
                    observed_at=SoundSelectionQualityTests.now,
                    plugins=[summary],
                    warnings=[],
                )

            def list_plugin_presets(
                self,
                *,
                target,
                start,
                limit,
                include_current,
                include_empty_names,
            ):
                self.page_reads += 1
                total = 300
                end = min(start + limit, total)
                rows = [
                    PluginPresetRecord(
                        index=index,
                        name="Far Away" if index == 299 else f"P{index}",
                    )
                    for index in range(start, end)
                ]
                return PluginPresetPage(
                    observed_at=SoundSelectionQualityTests.now,
                    plugin=summary,
                    preset_count=total,
                    start=start,
                    limit=limit,
                    scanned_count=len(rows),
                    returned_count=len(rows),
                    has_more=end < total,
                    next_start=end if end < total else None,
                    presets=rows,
                    current_preset_name="P0",
                    current_preset_index=0,
                    current_preset_status="stable",
                    duplicate_names=[],
                    blank_name_indices=[],
                    session_fingerprint=session,
                )

            def get_plugin_current_preset(self, *, target, allow_master=False):
                return SimpleNamespace(
                    preset_count=300,
                    current_preset_name="P0",
                    current_preset_index=0,
                    current_preset_status="stable",
                    session_fingerprint=session,
                )

        with TemporaryDirectory() as directory:
            inspector = Inspector()
            service = SoundSelectionService(
                inspector=inspector,
                atlas_registry=SimpleNamespace(products=()),
                atlas_inspector=lambda *args, **kwargs: SimpleNamespace(
                    plugins=(), warnings=()
                ),
                history=LocalSoundSelectionHistory(Path(directory) / "history.json"),
                now=lambda: SoundSelectionQualityTests.now,
            )
            role = SoundRoleRequest(
                role_id="lead",
                role_type="lead",
                preferred_presets=("Far Away",),
            )
            plan = service.plan(
                SoundSelectionRequest(
                    brief="far away lead",
                    roles=(role,),
                    seed=7,
                )
            )
        self.assertGreater(inspector.page_reads, 1)
        self.assertEqual(
            plan.preset_discovery_coverage[0].coverage_mode,
            "stratified",
        )
        self.assertIn(
            "Far Away",
            plan.preset_discovery_coverage[0].exact_preference_matches,
        )
        self.assertEqual(plan.assignments[0].selected_preset, "Far Away")

    def test_descriptor_normalization_preserves_original_term(self) -> None:
        normalized = normalize_descriptor("hard attack")
        self.assertEqual(normalized.original_term, "hard attack")
        self.assertEqual(normalized.normalized_descriptor, "punchy")
        self.assertTrue(normalized.matched_synonym)

    def test_model_preference_is_soft_but_user_preference_is_hard(self) -> None:
        role = SoundRoleRequest(
            role_id="lead",
            role_type="lead",
            preference_directives=(
                PreferenceDirective(
                    value="Vital",
                    dimension="product",
                    origin="model_suggested",
                    strength="hard",
                ),
            ),
        )
        request = SoundSelectionRequest(brief="lead", roles=(role,))
        candidate = self._candidate("Current")
        soft = score_candidates((candidate,), role, request)[0]
        self.assertTrue(soft.eligible)
        self.assertIn("model_suggested", soft.preference_provenance)

        hard_role = role.model_copy(
            update={
                "preference_directives": (
                    PreferenceDirective(
                        value="Vital",
                        dimension="product",
                        origin="user_explicit",
                        strength="hard",
                    ),
                )
            }
        )
        hard = score_candidates((candidate,), hard_role, request.model_copy(update={"roles": (hard_role,)}))[0]
        self.assertFalse(hard.eligible)

    def test_hard_feedback_descriptor_direction_is_role_scoped(self) -> None:
        role = SoundRoleRequest(role_id="texture", role_type="texture")
        request = SoundSelectionRequest(brief="texture", roles=(role,))
        bright = self._candidate("bright", descriptors=("bright",), channel=1)
        dark = self._candidate("dark", descriptors=("dark",), channel=2)
        feedback = SoundHistoryFeedback(
            feedback_id="hard-descriptor",
            palette_id="palette-1",
            role_id="texture",
            verdict="accepted",
            desired_descriptors=("bright",),
            hard_preference=True,
            recorded_at=self.now,
        )
        history = SoundHistoryDocument(
            created_at=self.now,
            updated_at=self.now,
            feedback=(feedback,),
        )
        ranked = score_candidates((dark, bright), role, request, history=history)
        self.assertTrue(ranked[0].eligible)
        self.assertEqual(ranked[0].candidate.candidate_id, "bright")
        self.assertFalse(ranked[1].eligible)
        self.assertIn(
            "explicit hard feedback requires preferred descriptors",
            ranked[1].disqualification_reasons,
        )

    def test_shortlist_contains_margin_and_confidence_dimensions(self) -> None:
        role = SoundRoleRequest(role_id="lead", role_type="lead", desired_descriptors=("bright",))
        request = SoundSelectionRequest(brief="bright lead", roles=(role,))
        rows = (
            self._candidate("Bright", descriptors=("bright",), channel=1),
            self._candidate("Plain", descriptors=(), channel=2),
        )
        shortlist = rank_shortlist(rows, role, request)
        self.assertEqual(shortlist.winner_candidate_id, "bright")
        self.assertIsNotNone(shortlist.score_margin)
        self.assertEqual(len(shortlist.items), 2)
        self.assertIn(shortlist.items[0].metadata_confidence, {"low", "metadata_insufficient", "medium", "high"})

    def test_assignment_retains_selected_candidate_characteristics(self) -> None:
        candidate = self._candidate(
            "Reviewed Pluck",
            descriptors=("bright", "plucked"),
            registers=("high",),
            articulations=("pluck",),
            envelope_behavior=("fast_attack", "short_release"),
            mono_poly="mono",
            metadata_provenance="bundled_reviewed",
            metadata_confidence="medium",
            brightness=0.82,
            width=0.25,
            motion=0.40,
            aggression=0.55,
            softness=0.20,
            density=0.65,
            complexity=0.70,
            energy=0.75,
        )
        role = SoundRoleRequest(
            role_id="lead",
            role_type="lead",
            desired_descriptors=("bright",),
        )
        plan = plan_palette(
            SoundSelectionRequest(brief="reviewed lead", roles=(role,)),
            (candidate,),
        )
        assignment = plan.assignments[0]
        self.assertEqual(assignment.descriptors, candidate.descriptors)
        self.assertEqual(assignment.descriptor_provenance, candidate.descriptor_provenance)
        self.assertEqual(assignment.registers, ("high",))
        self.assertEqual(assignment.articulations, ("pluck",))
        self.assertEqual(assignment.envelope_behavior, ("fast_attack", "short_release"))
        self.assertEqual(assignment.mono_poly, "mono")
        self.assertEqual(assignment.brightness, 0.82)
        self.assertEqual(assignment.energy, 0.75)
        self.assertEqual(assignment.characteristic_provenance, "bundled_reviewed")
        self.assertEqual(assignment.metadata_provenance, "bundled_reviewed")

    def test_feedback_is_role_scoped_and_anchor_migration_is_not_locking(self) -> None:
        feedback = SoundRoleFeedback(
            palette_id="palette-1",
            role_id="lead",
            verdict="rejected",
            descriptors=("bright",),
            hard_exclusion=True,
        )
        self.assertEqual(feedback.role_id, "lead")
        migrated = migrate_palette_assignment(
            {
                "role_id": "lead",
                "product_name": "Synth",
                "selected_preset": "Placeholder",
                "anchor": True,
                "locked": False,
            }
        )
        self.assertTrue(migrated.anchor_after_selection)
        self.assertFalse(migrated.locked)

    def test_lock_existing_is_distinct_from_anchor_after_selection(self) -> None:
        current = self._candidate("Current", channel=1)
        existing = SoundPaletteAssignment(
            role_id="texture",
            target=current.target,
            product_id=current.product_id,
            product_name=current.product_name,
            selected_preset=current.selected_preset,
            selected_preset_index=current.preset_index,
            preset_identity_digest=current.identity_digest,
            locked=True,
        )
        locked_role = SoundRoleRequest(
            role_id="texture",
            role_type="texture",
            lock_existing=True,
        )
        locked_plan = plan_palette(
            SoundSelectionRequest(brief="keep texture", roles=(locked_role,)),
            (current,),
            existing=(existing,),
        )
        locked_assignment = locked_plan.assignments[0]
        self.assertTrue(locked_assignment.locked)
        self.assertFalse(locked_assignment.anchor)
        self.assertFalse(locked_assignment.anchor_after_selection)

        anchor_role = SoundRoleRequest(
            role_id="texture",
            role_type="texture",
            anchor_after_selection=True,
        )
        anchor_plan = plan_palette(
            SoundSelectionRequest(brief="anchor texture", roles=(anchor_role,)),
            (current,),
        )
        anchor_assignment = anchor_plan.assignments[0]
        self.assertTrue(anchor_assignment.anchor_after_selection)
        self.assertFalse(anchor_assignment.locked)


if __name__ == "__main__":
    unittest.main()
