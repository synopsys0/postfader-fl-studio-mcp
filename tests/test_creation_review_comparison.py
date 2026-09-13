"""Focused deterministic tests for before/after Review comparisons."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import fl_studio_mcp.creation_review.comparison as review_comparison
from fl_studio_mcp.creation_review.assets import DecodedAudioCache, validate_audio_asset
from fl_studio_mcp.creation_review.comparison import (
    RevisionComparisonError,
    compare_revision_bounces,
)
from fl_studio_mcp.creation_review.models import (
    AcceptedElementLock,
    FrozenMap,
    ReviewGeneratedOutput,
    ReviewAssetSet,
    RevisionComparison,
)
from fl_studio_mcp.creation_review.sections import build_review_section_map


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "creation_review"


class CreationReviewComparisonTests(unittest.TestCase):
    def asset(self, name: str, kind: str, **kwargs: object):
        return validate_audio_asset(str(FIXTURES / name), asset_kind=kind, **kwargs)

    def section_map(self):
        return build_review_section_map(
            section_ranges=(
                {"section_id": "intro", "name": "Intro", "start_bar": 1, "end_bar": 2},
                {"section_id": "drop", "name": "Drop", "start_bar": 2, "end_bar": 3},
            ),
            tempo_bpm=120.0,
        )

    def test_aligned_comparison_has_global_section_and_objective_results(self) -> None:
        before = self.asset("clean_baseline.wav", "before_full_mix")
        after = self.asset(
            "improved_contrast_with_regression.wav", "after_full_mix"
        )
        result = compare_revision_bounces(
            before,
            after,
            section_map=self.section_map(),
            expected_objectives=(
                {"metric": "lufs_integrated", "direction": "increase"},
                {"metric": "not_measured", "direction": "increase"},
            ),
            max_seconds=4.0,
            cache=DecodedAudioCache(max_entries=8, max_bytes=64 * 1024 * 1024),
        )
        self.assertIsInstance(result, RevisionComparison)
        self.assertEqual(result.alignment_result.state, "aligned")
        self.assertTrue(result.global_deltas)
        self.assertEqual(len(result.section_deltas), 2)
        self.assertEqual(
            result.expected_objective_results[0].state, "moved_toward_target"
        )
        self.assertEqual(
            result.expected_objective_results[1].state, "insufficient_evidence"
        )
        # The fixture deliberately widens the stereo image; retain this as a
        # measurable regression even though the change is small in absolute dB.
        self.assertTrue(any("stereo_correlation" in item for item in result.regressions))
        self.assertEqual(result.technical_conclusion, "mixed")
        self.assertFalse(result.mutations_applied)
        self.assertTrue(result.zero_mutations)

    def test_duration_and_declared_offset_mismatches_withhold_deltas(self) -> None:
        before = self.asset("clean_baseline.wav", "before_full_mix")
        short_after = self.asset("duration_mismatch.wav", "after_full_mix")
        duration_result = compare_revision_bounces(
            before, short_after, max_seconds=4.0
        )
        self.assertEqual(duration_result.alignment_result.state, "duration_mismatch")
        self.assertFalse(duration_result.global_deltas)
        self.assertTrue(duration_result.unknown_metrics)

        offset_before = self.asset(
            "clean_baseline.wav", "before_full_mix", declared_offset_seconds=0.0
        )
        offset_after = self.asset(
            "improved_contrast_with_regression.wav",
            "after_full_mix",
            declared_offset_seconds=0.25,
        )
        offset_result = compare_revision_bounces(
            offset_before, offset_after, max_seconds=4.0
        )
        self.assertEqual(offset_result.alignment_result.state, "offset_mismatch")
        self.assertFalse(offset_result.global_deltas)

    def test_duration_mismatch_uses_only_an_explicit_common_section_window(self) -> None:
        before = self.asset("clean_baseline.wav", "before_full_mix")
        short_after = self.asset("duration_mismatch.wav", "after_full_mix")
        section_map = build_review_section_map(
            section_ranges=(
                {
                    "section_id": "intro",
                    "start_seconds": 0.0,
                    "end_seconds": 2.0,
                },
            ),
            tempo_bpm=120.0,
        )

        scalar_lengths: list[int] = []
        original_scalar_features = review_comparison._scalar_features

        def record_scalar_features(data, rate):
            scalar_lengths.append(len(data))
            return original_scalar_features(data, rate)

        with mock.patch.object(
            review_comparison,
            "_scalar_features",
            side_effect=record_scalar_features,
        ):
            result = compare_revision_bounces(
                before,
                short_after,
                section_map=section_map,
                max_seconds=4.0,
                cache=DecodedAudioCache(max_entries=8, max_bytes=64 * 1024 * 1024),
            )

        self.assertEqual(result.alignment_result.state, "aligned")
        self.assertTrue(result.global_deltas)
        self.assertEqual(scalar_lengths, [32_000, 32_000])
        self.assertTrue(
            any("explicit common section window" in item for item in result.warnings)
        )
        self.assertTrue(
            any("Unexamined audio outside that window" in item for item in result.warnings)
        )
        self.assertIn(
            "audio outside the explicit common section window was not examined",
            result.unknown_metrics,
        )

    def test_duration_mismatch_rejects_an_explicit_range_outside_both_bounces(self) -> None:
        before = self.asset("clean_baseline.wav", "before_full_mix")
        short_after = self.asset("duration_mismatch.wav", "after_full_mix")
        section_map = build_review_section_map(
            section_ranges=(
                {
                    "section_id": "full",
                    "start_seconds": 0.0,
                    "end_seconds": 4.0,
                },
            ),
            tempo_bpm=120.0,
        )

        result = compare_revision_bounces(
            before,
            short_after,
            section_map=section_map,
            max_seconds=4.0,
        )

        self.assertEqual(result.alignment_result.state, "duration_mismatch")
        self.assertFalse(result.global_deltas)
        self.assertIn(
            "global and section deltas withheld until bounces align",
            result.unknown_metrics,
        )

    def test_duration_mismatch_rejects_disjoint_declared_sections(self) -> None:
        before = self.asset("clean_baseline.wav", "before_full_mix")
        short_after = self.asset("duration_mismatch.wav", "after_full_mix")
        section_map = build_review_section_map(
            section_ranges=(
                {"section_id": "intro", "start_seconds": 0.0, "end_seconds": 1.0},
                {"section_id": "drop", "start_seconds": 2.0, "end_seconds": 3.0},
            ),
            tempo_bpm=120.0,
        )

        result = compare_revision_bounces(
            before,
            short_after,
            section_map=section_map,
            max_seconds=4.0,
        )

        self.assertEqual(result.alignment_result.state, "duration_mismatch")
        self.assertFalse(result.global_deltas)
        self.assertIn(
            "global and section deltas withheld until bounces align",
            result.unknown_metrics,
        )

    def test_conflicting_offset_and_expected_start_metadata_withholds_deltas(self) -> None:
        before = self.asset(
            "clean_baseline.wav",
            "before_full_mix",
            declared_offset_seconds=0.0,
            expected_start_seconds=0.5,
        )
        after = self.asset(
            "improved_contrast_with_regression.wav",
            "after_full_mix",
            declared_offset_seconds=0.0,
            expected_start_seconds=0.0,
        )
        result = compare_revision_bounces(before, after, max_seconds=4.0)

        self.assertEqual(result.alignment_result.state, "offset_mismatch")
        self.assertFalse(result.global_deltas)
        self.assertIn("Conflicting export-start metadata", result.alignment_result.explanation)
        self.assertIn("withheld", result.alignment_result.explanation)

    def test_comparison_requires_before_and_after_full_mix_asset_kinds(self) -> None:
        before = self.asset("clean_baseline.wav", "candidate_full_mix")
        after = self.asset(
            "improved_contrast_with_regression.wav", "after_full_mix"
        )

        with self.assertRaisesRegex(RevisionComparisonError, "before.*asset_kind"):
            compare_revision_bounces(before, after, max_seconds=4.0)

    def test_comparison_binds_after_asset_to_expected_revision_pass(self) -> None:
        before = self.asset("clean_baseline.wav", "before_full_mix")
        after = self.asset(
            "improved_contrast_with_regression.wav",
            "after_full_mix",
            revision_pass_id="pass-other",
        )

        with self.assertRaisesRegex(RevisionComparisonError, "does not match"):
            compare_revision_bounces(
                before,
                after,
                expected_revision_pass_id="pass-1",
                max_seconds=4.0,
            )

    def test_asset_set_slots_require_matching_full_mix_kinds(self) -> None:
        candidate = self.asset("clean_baseline.wav", "candidate_full_mix")

        with self.assertRaisesRegex(ValueError, "before_full_mix.*before_full_mix"):
            ReviewAssetSet(asset_set_id="assets-1", before_full_mix=candidate)

    def test_section_map_preserves_export_offset_for_records_and_bar_conversion(self) -> None:
        section_map = build_review_section_map(
            section_ranges=(
                {"section_id": "intro", "start_bar": 1, "end_bar": 3},
            ),
            tempo_bpm=120.0,
            export_offset_seconds=2.5,
        )

        self.assertEqual(section_map.export_offset_seconds, 2.5)
        self.assertEqual(section_map.sections[0].start_seconds, 2.5)
        self.assertEqual(section_map.sections[0].end_seconds, 6.5)
        self.assertEqual(section_map.bar_to_seconds(1), 2.5)
        self.assertEqual(section_map.bar_to_seconds(3), 6.5)
        self.assertEqual(section_map.digest, section_map.map_digest)

    def test_repeated_comparison_reuses_alignment_and_measurements(self) -> None:
        before = self.asset("clean_baseline.wav", "before_full_mix")
        after = self.asset("improved_contrast_with_regression.wav", "after_full_mix")
        cache = DecodedAudioCache(max_entries=8, max_bytes=64 * 1024 * 1024)

        with mock.patch.object(
            review_comparison.audio,
            "_estimate_alignment",
            wraps=review_comparison.audio._estimate_alignment,
        ) as alignment, mock.patch.object(
            review_comparison,
            "_scalar_features",
            wraps=review_comparison._scalar_features,
        ) as scalar_features:
            first = compare_revision_bounces(
                before,
                after,
                section_map=self.section_map(),
                max_seconds=4.0,
                cache=cache,
            )
            second = compare_revision_bounces(
                before,
                after,
                section_map=self.section_map(),
                max_seconds=4.0,
                cache=cache,
            )

        self.assertIsNot(first, second)
        self.assertEqual(alignment.call_count, 1)
        self.assertEqual(scalar_features.call_count, 2)
        self.assertEqual(cache.decode_count, 2)

    def test_accepted_identity_loss_uses_stored_sequence_and_palette_evidence(self) -> None:
        before = self.asset("clean_baseline.wav", "before_full_mix")
        after = self.asset("improved_contrast_with_regression.wav", "after_full_mix")
        lock = AcceptedElementLock(
            lock_id="keep-chords",
            scope="role",
            role_id="main_chords",
            lock_types=("note_content", "sound_assignment"),
            directive="Keep the accepted chord notes and sound.",
        )
        before_output = ReviewGeneratedOutput(
            output_id="chords-before",
            output_kind="note_sequence",
            role_id="main_chords",
            digest="a" * 64,
            metadata=FrozenMap({"note_digest_sha256": "a" * 64}),
        )
        after_output = ReviewGeneratedOutput(
            output_id="chords-after",
            output_kind="note_sequence",
            role_id="main_chords",
            digest="b" * 64,
            metadata=FrozenMap({"note_digest_sha256": "b" * 64}),
        )
        result = compare_revision_bounces(
            before,
            after,
            accepted_element_locks=(lock,),
            before_generated_outputs=(before_output,),
            after_generated_outputs=(after_output,),
            before_palette={
                "assignments": (
                    {
                        "role_id": "main_chords",
                        "product_id": "synth-a",
                        "selected_preset": "Bright Chords",
                    },
                )
            },
            after_palette={
                "assignments": (
                    {
                        "role_id": "main_chords",
                        "product_id": "synth-b",
                        "selected_preset": "Unrelated Chords",
                    },
                )
            },
            max_seconds=4.0,
            cache=DecodedAudioCache(max_entries=8, max_bytes=64 * 1024 * 1024),
        )
        self.assertTrue(
            any("stored generated-sequence digest evidence" in row for row in result.regressions)
        )
        self.assertTrue(
            any("stored Sound Palette assignment evidence" in row for row in result.regressions)
        )
        self.assertNotEqual(result.technical_conclusion, "improved")


if __name__ == "__main__":
    unittest.main(verbosity=2)
