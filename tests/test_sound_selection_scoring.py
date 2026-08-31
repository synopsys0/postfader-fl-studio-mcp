"""Focused deterministic scoring tests for Sound Selection."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from fl_studio_mcp.sound_selection import (
    SoundCandidate,
    SoundHistoryDocument,
    SoundHistoryFeedback,
    SoundHistoryRecord,
    SoundPaletteAssignment,
    SoundRoleRequest,
    SoundSelectionRequest,
    rank_candidates,
)
from fl_studio_mcp.track_b_contracts import ChannelGeneratorTarget


class SoundSelectionScoringTests(unittest.TestCase):
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)

    @staticmethod
    def _candidate(
        candidate_id: str,
        *,
        channel: int,
        product: str = "synth",
        preset: str = "Preset",
        descriptors: tuple[str, ...] = (),
        **updates: object,
    ) -> SoundCandidate:
        return SoundCandidate(
            candidate_id=candidate_id,
            target=ChannelGeneratorTarget(channel_index=channel),
            product_id=product,
            product_name=product,
            current_preset=preset,
            candidate_preset=preset,
            preset_identity_stable=True,
            preset_navigation_available=True,
            descriptors=descriptors,
            **updates,
        )

    def test_role_fit_beats_bounded_recent_use_penalty(self) -> None:
        role = SoundRoleRequest(
            role_id="main_lead",
            role_type="lead",
            desired_descriptors=("bright",),
        )
        request = SoundSelectionRequest(brief="bright lead", roles=(role,))
        strong = self._candidate("strong", channel=1, descriptors=("bright",))
        weak = self._candidate("weak", channel=2, product="other", preset="Other")
        history = SoundHistoryDocument(
            created_at=self.now,
            updated_at=self.now,
            records=(
                SoundHistoryRecord(
                    record_id="used",
                    product_id="synth",
                    preset_identity_digest=strong.identity_digest,
                    role_id="main_lead",
                    first_used_at=self.now,
                    last_used_at=self.now,
                    usage_count=12,
                    consecutive_use_count=12,
                ),
            ),
        )
        ranked = rank_candidates((weak, strong), role, request, history=history)
        self.assertEqual(ranked[0].candidate_id, "strong")

    def test_recent_exact_use_breaks_an_equal_quality_tie(self) -> None:
        role = SoundRoleRequest(role_id="texture", role_type="texture")
        request = SoundSelectionRequest(brief="texture", roles=(role,), seed=7)
        recent = self._candidate("recent", channel=1, preset="Recent")
        unused = self._candidate("unused", channel=2, product="other", preset="Unused")
        history = SoundHistoryDocument(
            created_at=self.now,
            updated_at=self.now,
            records=(
                SoundHistoryRecord(
                    record_id="used",
                    product_id="synth",
                    preset_identity_digest=recent.identity_digest,
                    role_id="texture",
                    first_used_at=self.now,
                    last_used_at=self.now,
                    usage_count=1,
                    consecutive_use_count=1,
                ),
            ),
        )
        ranked = rank_candidates((unused, recent), role, request, history=history)
        self.assertEqual(ranked[0].candidate_id, "unused")

    def test_locked_existing_assignment_remains_hard_with_preserve_false(self) -> None:
        role = SoundRoleRequest(role_id="main_lead", role_type="lead")
        request = SoundSelectionRequest(
            brief="replace nothing",
            roles=(role,),
            preserve_existing_roles=False,
        )
        current = self._candidate("current", channel=1)
        replacement = self._candidate("replacement", channel=2, product="other")
        existing = SoundPaletteAssignment(
            role_id="main_lead",
            target=current.target,
            product_id=current.product_id,
            product_name=current.product_name,
            selected_preset=current.selected_preset,
            preset_identity_digest=current.identity_digest,
            locked=True,
        )
        ranked = rank_candidates(
            (replacement, current),
            role,
            request,
            existing_assignments=(existing,),
        )
        self.assertEqual(ranked[0].candidate_id, "current")
        self.assertIn("preserved or locked role assignment", ranked[1].disqualification_reasons)

    def test_rejected_feedback_lowers_only_the_matching_identity(self) -> None:
        role = SoundRoleRequest(role_id="texture", role_type="texture")
        request = SoundSelectionRequest(brief="texture", roles=(role,))
        rejected = self._candidate("rejected", channel=1, preset="Rejected")
        good = self._candidate("good", channel=2, product="other", preset="Good")
        feedback = SoundHistoryFeedback(
            feedback_id="feedback-1",
            palette_id="palette-1",
            role_id="texture",
            product_id="synth",
            preset_identity_digest=rejected.identity_digest,
            verdict="rejected",
            recorded_at=self.now,
        )
        history = SoundHistoryDocument(
            created_at=self.now,
            updated_at=self.now,
            feedback=(feedback,),
        )
        ranked = rank_candidates((rejected, good), role, request, history=history)
        self.assertEqual(ranked[0].candidate_id, "good")
        self.assertLess(ranked[1].score_breakdown.feedback, 0.0)

    def test_complete_palette_descriptor_feedback_influences_future_ranking(self) -> None:
        role = SoundRoleRequest(role_id="texture", role_type="texture")
        request = SoundSelectionRequest(brief="choose a texture", roles=(role,))
        bright = self._candidate("bright", channel=1, descriptors=("bright",))
        dark = self._candidate("dark", channel=2, descriptors=("dark",))
        feedback = SoundHistoryFeedback(
            feedback_id="feedback-descriptor",
            palette_id="palette-1",
            verdict="accepted",
            descriptors=("bright",),
            recorded_at=self.now,
        )
        history = SoundHistoryDocument(
            created_at=self.now,
            updated_at=self.now,
            feedback=(feedback,),
        )
        ranked = rank_candidates((dark, bright), role, request, history=history)
        self.assertEqual(ranked[0].candidate_id, "bright")
        self.assertGreater(ranked[0].score_breakdown.feedback, 0.0)

    def test_descriptor_feedback_is_bounded_below_explicit_direction(self) -> None:
        role = SoundRoleRequest(
            role_id="texture",
            role_type="texture",
            desired_descriptors=("bright",),
        )
        request = SoundSelectionRequest(brief="bright texture", roles=(role,))
        bright = self._candidate("bright", channel=1, descriptors=("bright",))
        dark = self._candidate("dark", channel=2, descriptors=("dark",))
        feedback = SoundHistoryFeedback(
            feedback_id="feedback-rejected-descriptor",
            palette_id="palette-1",
            verdict="rejected",
            descriptors=("bright",),
            recorded_at=self.now,
        )
        history = SoundHistoryDocument(
            created_at=self.now,
            updated_at=self.now,
            feedback=(feedback,),
        )
        ranked = rank_candidates((dark, bright), role, request, history=history)
        self.assertEqual(ranked[0].candidate_id, "bright")
        self.assertGreaterEqual(ranked[0].score_breakdown.feedback, -0.35)

    def test_drum_candidates_require_the_default_minimum_map_roles(self) -> None:
        role = SoundRoleRequest(role_id="drums", role_type="drums")
        request = SoundSelectionRequest(brief="choose drums", roles=(role,))
        candidate = self._candidate(
            "kit",
            channel=1,
            pad_map_available=True,
            drum_missing_roles=("closed_hat",),
        )
        ranked = rank_candidates((candidate,), role, request)
        self.assertEqual(ranked[0].score, -100.0)
        self.assertIn(
            "required drum mapping cannot be established",
            ranked[0].disqualification_reasons,
        )

    def test_section_scope_unknown_on_live_candidate_does_not_disqualify(self) -> None:
        role = SoundRoleRequest(
            role_id="texture",
            role_type="texture",
            section_scope=("drop",),
        )
        request = SoundSelectionRequest(brief="drop texture", roles=(role,))
        candidate = self._candidate("texture", channel=1)
        ranked = rank_candidates((candidate,), role, request)
        self.assertEqual(ranked[0].disqualification_reasons, ())

    def test_reported_candidate_scope_still_rejects_out_of_scope_candidate(self) -> None:
        role = SoundRoleRequest(
            role_id="texture",
            role_type="texture",
            section_scope=("drop",),
        )
        request = SoundSelectionRequest(brief="drop texture", roles=(role,))
        candidate = self._candidate("texture", channel=1, section_scope=("verse",))
        ranked = rank_candidates((candidate,), role, request)
        self.assertIn("candidate is outside the role section scope", ranked[0].disqualification_reasons)

    def test_explicit_preference_overrides_rejection(self) -> None:
        role = SoundRoleRequest(
            role_id="main_lead",
            role_type="lead",
            preferred_presets=("Rejected",),
        )
        request = SoundSelectionRequest(brief="use Rejected", roles=(role,))
        rejected = self._candidate("rejected", channel=1, preset="Rejected")
        good = self._candidate("good", channel=2, product="other", preset="Good")
        feedback = SoundHistoryFeedback(
            feedback_id="feedback-2",
            palette_id="palette-1",
            role_id="main_lead",
            product_id="synth",
            preset_identity_digest=rejected.identity_digest,
            verdict="rejected",
            recorded_at=self.now,
        )
        history = SoundHistoryDocument(
            created_at=self.now,
            updated_at=self.now,
            feedback=(feedback,),
        )
        ranked = rank_candidates((good, rejected), role, request, history=history)
        self.assertEqual(ranked[0].candidate_id, "rejected")

    def test_unloaded_explicit_preference_is_a_blocker_not_a_silent_substitute(self) -> None:
        role = SoundRoleRequest(role_id="main_lead", role_type="lead")
        request = SoundSelectionRequest(
            brief="use an unavailable product",
            roles=(role,),
            product_preferences=("Missing Synth",),
        )
        result = rank_candidates(
            (self._candidate("fallback", channel=1),), role, request
        )[0]
        self.assertTrue(result.score_breakdown.totals_consistent)
        self.assertIn(
            "explicit product preference requires another loaded product",
            result.disqualification_reasons,
        )

    def test_exploratory_policy_opens_a_flexible_existing_role(self) -> None:
        role = SoundRoleRequest(role_id="texture", role_type="texture")
        request = SoundSelectionRequest(
            brief="explore a texture",
            roles=(role,),
            selection_policy={"mode": "exploratory", "novelty_weight": 1.0},
        )
        current = self._candidate("current", channel=1, product="old", preset="Old")
        alternate = self._candidate("alternate", channel=2, product="new", preset="New")
        existing = SoundPaletteAssignment(
            role_id="texture",
            target=current.target,
            product_id=current.product_id,
            product_name=current.product_name,
            selected_preset=current.selected_preset,
            selected_preset_index=current.preset_index,
            preset_identity_digest=current.identity_digest,
        )
        ranked = rank_candidates(
            (current, alternate),
            role,
            request,
            existing_assignments=(existing,),
        )
        self.assertEqual(ranked[0].candidate_id, "alternate")

    def test_same_loaded_target_cannot_hold_two_role_presets(self) -> None:
        role = SoundRoleRequest(role_id="texture", role_type="texture")
        request = SoundSelectionRequest(brief="add a texture", roles=(role,))
        selected = SoundPaletteAssignment(
            role_id="main_lead",
            target=ChannelGeneratorTarget(channel_index=1),
            product_id="synth",
            product_name="synth",
            selected_preset="Lead",
        )
        candidate = self._candidate(
            "same-target",
            channel=1,
            product="synth",
            preset="Texture",
        )

        ranked = rank_candidates(
            (candidate,),
            role,
            request,
            selected_candidates=(selected,),
        )

        self.assertIn(
            "loaded target is already assigned to another sound role",
            ranked[0].disqualification_reasons,
        )

    def test_exact_preset_reuse_needs_an_explicit_layering_reason(self) -> None:
        selected = SoundPaletteAssignment(
            role_id="main_lead",
            target=ChannelGeneratorTarget(channel_index=1),
            product_id="synth",
            product_name="synth",
            selected_preset="Shared",
        )
        candidate = self._candidate(
            "shared",
            channel=2,
            product="synth",
            preset="Shared",
        )
        ordinary = SoundRoleRequest(role_id="texture", role_type="texture")
        layered = ordinary.model_copy(update={"allow_layering": True})

        rejected = rank_candidates(
            (candidate,),
            ordinary,
            SoundSelectionRequest(brief="add texture", roles=(ordinary,)),
            selected_candidates=(selected,),
        )[0]
        accepted = rank_candidates(
            (candidate,),
            layered,
            SoundSelectionRequest(brief="layer the lead", roles=(layered,)),
            selected_candidates=(selected,),
        )[0]

        self.assertIn(
            "exact product and preset are already assigned without a layering reason",
            rejected.disqualification_reasons,
        )
        self.assertEqual(accepted.disqualification_reasons, ())


if __name__ == "__main__":
    unittest.main()
