"""Focused deterministic tests for Creation Review audio evaluation."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from builtins import open as builtin_open
from pathlib import Path
from unittest import mock

import fl_studio_mcp.creation_review.analysis as review_analysis
import fl_studio_mcp.creation_review.assets as review_assets
import fl_studio_mcp.creation_review.contrast as review_contrast
from fl_studio_mcp.creation_review.analysis import evaluate_creation
from fl_studio_mcp.creation_review.assets import (
    DecodedAudioCache,
    ReviewAssetError,
    build_review_asset_set,
    sha256_file,
    validate_audio_asset,
)
from fl_studio_mcp.creation_review.findings import build_evaluation_findings
from fl_studio_mcp.creation_review.models import (
    CreationEvaluationReport,
    CreationFeedback,
    FrozenMap,
    ReviewGeneratedOutput,
    ReviewSourceSnapshot,
    RoleFeedback,
)
from fl_studio_mcp.creation_review.sections import build_review_section_map


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "creation_review"


class CreationReviewAnalysisTests(unittest.TestCase):
    def asset(self, name: str, kind: str, **kwargs: object):
        return validate_audio_asset(
            str(FIXTURES / name), asset_kind=kind, **kwargs
        )

    def section_map(self):
        # One bar is two seconds at 120 BPM.  The final section intentionally
        # extends past the four-second fixture to exercise bounded coverage.
        return build_review_section_map(
            section_ranges=(
                {"section_id": "intro", "name": "Intro", "start_bar": 1, "end_bar": 2},
                {"section_id": "build", "name": "Build", "start_bar": 2, "end_bar": 3},
                {"section_id": "drop", "name": "Drop", "start_bar": 3, "end_bar": 4},
                {"section_id": "tail", "name": "Tail", "start_bar": 4, "end_bar": 5},
            ),
            tempo_bpm=120.0,
        )

    def test_asset_validation_and_decode_cache_are_bounded(self) -> None:
        asset = self.asset("clean_baseline.wav", "candidate_full_mix")
        cache = DecodedAudioCache(max_entries=2, max_bytes=32 * 1024 * 1024)
        first = cache.get_or_decode(asset, max_seconds=4.0)
        second = cache.get_or_decode(asset, max_seconds=4.0)
        self.assertIs(first, second)
        self.assertEqual(cache.decode_count, 1)
        self.assertEqual(cache.stats().entries, 1)

        with self.assertRaises(ReviewAssetError):
            validate_audio_asset(str(FIXTURES), asset_kind="candidate_full_mix")
        with tempfile.NamedTemporaryFile(suffix=".txt") as unsupported:
            unsupported.write(b"not audio")
            unsupported.flush()
            with self.assertRaises(ReviewAssetError):
                validate_audio_asset(unsupported.name, asset_kind="candidate_full_mix")

    def test_hashing_enforces_byte_cap_and_detects_growth(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            path = Path(handle.name)
            handle.write(b"a" * (1024 * 1024 + 32))
        self.addCleanup(path.unlink, missing_ok=True)
        with self.assertRaisesRegex(ReviewAssetError, "byte cap"):
            sha256_file(path, max_bytes=1024)

        class GrowingReader:
            def __init__(self, wrapped: object) -> None:
                self.wrapped = wrapped
                self.grown = False

            def __enter__(self) -> "GrowingReader":
                self.wrapped.__enter__()  # type: ignore[attr-defined]
                return self

            def __exit__(self, *args: object) -> object:
                return self.wrapped.__exit__(*args)  # type: ignore[attr-defined]

            def fileno(self) -> int:
                return self.wrapped.fileno()  # type: ignore[attr-defined]

            def read(self, size: int = -1) -> bytes:
                block = self.wrapped.read(size)  # type: ignore[attr-defined]
                if not self.grown:
                    self.grown = True
                    with builtin_open(path, "ab") as writer:
                        writer.write(b"growth")
                return block

        def growing_open(candidate: object, mode: str = "r", *args: object, **kwargs: object):
            wrapped = builtin_open(candidate, mode, *args, **kwargs)
            if Path(candidate) == path and mode == "rb":
                return GrowingReader(wrapped)
            return wrapped

        with mock.patch("builtins.open", side_effect=growing_open), self.assertRaisesRegex(
            ReviewAssetError,
            "changed while it was being hashed",
        ):
            sha256_file(path, max_bytes=2 * 1024 * 1024)

    def test_concurrent_cache_requests_decode_and_compute_once(self) -> None:
        asset = self.asset("clean_baseline.wav", "candidate_full_mix")
        cache = DecodedAudioCache(max_entries=2, max_bytes=32 * 1024 * 1024)
        original_load = review_assets.audio.load
        decode_calls = 0
        decode_lock = threading.Lock()

        def delayed_load(*args: object, **kwargs: object):
            nonlocal decode_calls
            with decode_lock:
                decode_calls += 1
            time.sleep(0.02)
            return original_load(*args, **kwargs)

        decoded: list[object] = []
        with mock.patch.object(review_assets.audio, "load", side_effect=delayed_load):
            threads = [
                threading.Thread(
                    target=lambda: decoded.append(
                        cache.get_or_decode(asset, max_seconds=4.0)
                    )
                )
                for _ in range(4)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2.0)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(decode_calls, 1)
        self.assertEqual(cache.decode_count, 1)
        self.assertEqual(len({id(item) for item in decoded}), 1)

        feature_calls = 0
        feature_lock = threading.Lock()
        features: list[object] = []

        def compute(_loaded: object) -> object:
            nonlocal feature_calls
            with feature_lock:
                feature_calls += 1
            time.sleep(0.02)
            return {"stable": True}

        threads = [
            threading.Thread(
                target=lambda: features.append(
                    cache.get_or_compute_features(
                        asset,
                        analyzer_version="test-1",
                        section_map_digest="a" * 64,
                        analysis_policy_digest="b" * 64,
                        compute=compute,
                        max_seconds=4.0,
                    )
                )
            )
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2.0)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(feature_calls, 1)
        self.assertEqual(features, [{"stable": True}] * 4)

    def test_repeated_evaluation_reuses_global_and_section_features(self) -> None:
        candidate = self.asset("clean_baseline.wav", "candidate_full_mix")
        asset_set = build_review_asset_set((candidate,))
        section_map = self.section_map()
        cache = DecodedAudioCache(max_entries=8, max_bytes=64 * 1024 * 1024)

        with mock.patch.object(
            review_analysis,
            "_global_measurement",
            wraps=review_analysis._global_measurement,
        ) as global_measurement, mock.patch.object(
            review_analysis,
            "_section_measurement",
            wraps=review_analysis._section_measurement,
        ) as section_measurement:
            first = evaluate_creation(
                asset_set,
                section_map=section_map,
                max_seconds=4.0,
                cache=cache,
            )
            second = evaluate_creation(
                asset_set,
                section_map=section_map,
                max_seconds=4.0,
                cache=cache,
            )

        self.assertIsNot(first, second)
        self.assertEqual(global_measurement.call_count, 1)
        self.assertEqual(section_measurement.call_count, 2)
        self.assertEqual(cache.decode_count, 1)
        self.assertEqual(cache.stats().feature_entries, 2)

    def test_repeated_reference_comparison_reuses_alignment_and_features(self) -> None:
        candidate = self.asset("clean_baseline.wav", "candidate_full_mix")
        reference = self.asset("improved_contrast_with_regression.wav", "reference_full_mix")
        cache = DecodedAudioCache(max_entries=8, max_bytes=64 * 1024 * 1024)
        candidate_loaded = cache.get_or_decode(candidate, max_seconds=4.0)
        reference_loaded = cache.get_or_decode(reference, max_seconds=4.0)

        with mock.patch.object(
            review_contrast.audio,
            "_estimate_alignment",
            wraps=review_contrast.audio._estimate_alignment,
        ) as alignment, mock.patch.object(
            review_contrast,
            "_loaded_features",
            wraps=review_contrast._loaded_features,
        ) as loaded_features:
            first = review_contrast.compare_reference_features(
                reference_loaded,
                candidate_loaded,
                reference_asset_id=reference.asset_id,
                candidate_asset_id=candidate.asset_id,
                requested_dimensions=("tonal_balance", "loudness"),
                reference_asset=reference,
                candidate_asset=candidate,
                section_map_digest="section-map-digest",
                analysis_policy_digest="policy-digest",
                max_seconds=4.0,
                cache=cache,
            )
            second = review_contrast.compare_reference_features(
                reference_loaded,
                candidate_loaded,
                reference_asset_id=reference.asset_id,
                candidate_asset_id=candidate.asset_id,
                requested_dimensions=("tonal_balance", "loudness"),
                reference_asset=reference,
                candidate_asset=candidate,
                section_map_digest="section-map-digest",
                analysis_policy_digest="policy-digest",
                max_seconds=4.0,
                cache=cache,
            )

        self.assertIs(first, second)
        self.assertEqual(alignment.call_count, 1)
        self.assertEqual(loaded_features.call_count, 2)

    def test_asset_set_records_synchronized_stems_and_digest(self) -> None:
        candidate = self.asset(
            "clean_baseline.wav",
            "candidate_full_mix",
            declared_offset_seconds=0.0,
        )
        vocal = self.asset(
            "masking_vocal_stem.wav",
            "vocal_stem",
            role_id="lead_vocal",
            declared_offset_seconds=0.0,
        )
        instrumental = self.asset(
            "masking_instrumental_stem.wav",
            "instrumental_stem",
            role_id="music",
            declared_offset_seconds=0.0,
        )
        asset_set = build_review_asset_set((candidate, vocal, instrumental))
        self.assertEqual(asset_set.candidate_full_mix.asset_id, candidate.asset_id)
        self.assertEqual(len(asset_set.synchronized_stems), 2)
        self.assertEqual(asset_set.alignment_state, "aligned")
        self.assertTrue(asset_set.asset_set_digest)
        self.assertEqual(asset_set.digest, asset_set.asset_set_digest)

        before = self.asset("clean_baseline.wav", "before_full_mix")
        short_after = self.asset("duration_mismatch.wav", "after_full_mix")
        mismatched = build_review_asset_set((before, short_after))
        self.assertFalse(mismatched.duration_compatible)
        self.assertEqual(mismatched.alignment_state, "unsynchronized")

    def test_matching_duration_stems_without_common_start_withhold_attribution(self) -> None:
        candidate = self.asset("clean_baseline.wav", "candidate_full_mix")
        vocal = self.asset(
            "masking_vocal_stem.wav", "vocal_stem", role_id="lead_vocal"
        )
        instrumental = self.asset(
            "masking_instrumental_stem.wav",
            "instrumental_stem",
            role_id="music",
        )
        asset_set = build_review_asset_set((candidate, vocal, instrumental))
        self.assertEqual(asset_set.alignment_state, "unknown")
        self.assertTrue(
            any("common export start" in item for item in asset_set.limitations)
        )

        report = evaluate_creation(
            asset_set,
            section_map=self.section_map(),
            max_seconds=4.0,
            cache=DecodedAudioCache(max_entries=8, max_bytes=64 * 1024 * 1024),
        )
        self.assertFalse(report.masking_analysis["context_ready"])
        self.assertEqual(report.masking_analysis["attribution_confidence"], "unknown")
        self.assertTrue(
            any(
                "remains unknown until synchronized" in item
                for item in report.masking_analysis["limitations"]
            )
        )
        self.assertTrue(report.stem_measurements)
        self.assertTrue(
            all(item.attribution_confidence < 0.5 for item in report.stem_measurements)
        )
        self.assertTrue(
            all(
                any("masking attribution" in limitation for limitation in item.limitations)
                for item in report.stem_measurements
            )
        )
        self.assertTrue(
            any("common-start alignment" in item for item in report.unavailable_analyses)
        )

    def test_section_precedence_and_timeline_issues_are_explicit(self) -> None:
        section_map = build_review_section_map(
            section_ranges=(
                {"section_id": "user", "start_seconds": 0.0, "end_seconds": 1.0},
            ),
            detected_suggestions=(
                {"section_id": "detected", "start_bar": 1, "end_bar": 2},
            ),
            tempo_bpm=120.0,
        )
        self.assertEqual(section_map.sections[0].source, "user_supplied")
        self.assertEqual(section_map.sections[0].start_seconds, 0.0)

        issues = build_review_section_map(
            section_ranges=(
                {"section_id": "a", "start_bar": 1, "end_bar": 2},
                {"section_id": "b", "start_bar": 3, "end_bar": 4},
                {"section_id": "c", "start_bar": 3.5, "end_bar": 5},
            ),
            tempo_bpm=120.0,
        )
        self.assertEqual(len(issues.gaps), 1)
        self.assertEqual(len(issues.overlaps), 1)
        self.assertEqual(issues.gaps[0].kind, "gap")
        self.assertEqual(issues.overlaps[0].kind, "overlap")

    def test_evaluation_returns_canonical_report_with_stems_and_generated_notes(self) -> None:
        candidate = self.asset(
            "clipped_candidate.wav",
            "candidate_full_mix",
            declared_offset_seconds=0.0,
        )
        vocal = self.asset(
            "masking_vocal_stem.wav",
            "vocal_stem",
            role_id="lead_vocal",
            declared_offset_seconds=0.0,
        )
        instrumental = self.asset(
            "masking_instrumental_stem.wav",
            "instrumental_stem",
            role_id="music",
            declared_offset_seconds=0.0,
        )
        asset_set = build_review_asset_set((candidate, vocal, instrumental))
        section_map = self.section_map()
        source = ReviewSourceSnapshot(
            source_run_id="run-1",
            original_brief="Create a four-section draft with an impactful drop.",
            completion_target="playable_draft",
            sections=section_map.sections,
            generated_note_sequences=(
                ReviewGeneratedOutput(
                    output_id="lead-sequence",
                    output_kind="note_sequence",
                    role_id="lead",
                    metadata=FrozenMap(
                        {
                            "name": "Drop Lead",
                            "generator": "deterministic-test",
                            "notes": (
                                {
                                    "pitch": 64,
                                    "start_beats": 0.0,
                                    "duration_beats": 1.0,
                                    "velocity": 0.8,
                                },
                                {
                                    "pitch": 67,
                                    "start_beats": 1.0,
                                    "duration_beats": 1.0,
                                    "velocity": 0.7,
                                },
                            ),
                            "duration_beats": 2.0,
                        }
                    ),
                ),
            ),
        )
        feedback = CreationFeedback(
            feedback_id="feedback-1",
            overall_note="The bass is too soft; keep the chords.",
        )
        report = evaluate_creation(
            asset_set,
            section_map=section_map,
            source_run=source,
            review_session_id="review-1",
            source_run_id="run-1",
            user_feedback=(feedback,),
            max_seconds=4.0,
            max_findings=16,
            cache=DecodedAudioCache(max_entries=8, max_bytes=64 * 1024 * 1024),
        )
        self.assertIsInstance(report, CreationEvaluationReport)
        self.assertEqual(report.review_session_id, "review-1")
        self.assertEqual(report.source_run_id, "run-1")
        self.assertIsInstance(report.global_measurements, FrozenMap)
        self.assertEqual(len(report.per_section_measurements), 2)
        self.assertEqual(len(report.stem_measurements), 2)
        self.assertTrue(report.masking_analysis["context_ready"])
        self.assertEqual(report.masking_analysis["attribution_confidence"], "high")
        self.assertTrue(
            all(item.attribution_confidence > 0.8 for item in report.stem_measurements)
        )
        self.assertIn("lead", report.generated_content_analysis)
        self.assertFalse(report.mutations_applied)
        self.assertTrue(report.zero_mutations)
        self.assertTrue(report.findings)
        self.assertEqual(report.findings[0].evidence_source, "explicit_user_feedback")
        self.assertTrue(set(report.top_priorities).issubset({item.finding_id for item in report.findings}))

    def test_canonical_feedback_note_is_scoped_and_ranked(self) -> None:
        findings = build_evaluation_findings(
            global_measurements={
                "loudness": {"clipped_samples": 10, "true_peak_dbtp": 0.2},
            },
            user_feedback=(
                CreationFeedback(
                    feedback_id="feedback-2",
                    role_feedback=(
                        RoleFeedback(
                            feedback_id="role-feedback-2",
                            role_id="lead",
                            note="The lead is too loud.",
                        ),
                    ),
                ),
            ),
            max_findings=8,
        )
        self.assertTrue(findings)
        self.assertEqual(findings[0].evidence_source, "explicit_user_feedback")
        self.assertEqual(findings[0].role_id, "lead")

    def test_goal_classification_and_processing_receipts_are_exposed(self) -> None:
        candidate = self.asset("clean_baseline.wav", "candidate_full_mix")
        asset_set = build_review_asset_set((candidate,))
        section_map = self.section_map()
        source = ReviewSourceSnapshot(
            source_run_id="run-goals",
            original_brief="Make the mix loud and give the drop an emotional lift.",
            completion_target="playable_draft",
            sections=section_map.sections,
            processing_receipts=(
                ReviewGeneratedOutput(
                    output_id="processing-plan",
                    output_kind="processing_plan",
                    metadata=FrozenMap(
                        {
                            "plan_id": "plan-1",
                            "actions": ({"action_id": "eq-1"},),
                        }
                    ),
                ),
            ),
        )
        report = evaluate_creation(
            asset_set,
            section_map=section_map,
            source_run=source,
            requested_focus=("stereo width", "drop development"),
            max_seconds=4.0,
            cache=DecodedAudioCache(max_entries=8, max_bytes=64 * 1024 * 1024),
        )
        goals = tuple(report.global_measurements["goal_evaluations"])
        states = {str(item["state"]) for item in goals}
        self.assertIn("technically_evaluable", states)
        self.assertIn("proxy_evaluable", states)
        self.assertIn("requires_user_judgment", states)
        self.assertEqual(report.processing_review_state.state, "proxy")
        self.assertEqual(report.processing_review_state.confidence, 0.45)
        self.assertEqual(report.status, "partial")
        if hasattr(report, "goal_evaluations"):
            self.assertEqual(len(report.goal_evaluations), len(goals))

    def test_reference_comparison_reports_requested_paired_section_metrics(self) -> None:
        candidate = self.asset("clean_baseline.wav", "candidate_full_mix")
        reference = self.asset("improved_contrast_with_regression.wav", "reference_full_mix")
        cache = DecodedAudioCache(max_entries=8, max_bytes=64 * 1024 * 1024)
        candidate_loaded = cache.get_or_decode(candidate, max_seconds=4.0)
        reference_loaded = cache.get_or_decode(reference, max_seconds=4.0)
        reference_data = review_contrast.audio._channel_data(reference_loaded)
        candidate_data = review_contrast.audio._channel_data(candidate_loaded)
        frame_count = min(len(reference_data), len(candidate_data))
        alignment_payload = {
            "alignment": {
                "confidence": {"level": "high"},
                "common_coverage": 1.0,
                "reference_common_start_sample": 0,
                "target_common_start_sample_at_reference_rate": 0,
            },
            "loudness_matching": {"applied": True},
            "rate_conversion": None,
        }
        with mock.patch.object(
            review_contrast,
            "_aligned_reference_payload",
            return_value=(alignment_payload, reference_data[:frame_count], candidate_data[:frame_count]),
        ):
            comparison = review_contrast.compare_reference_features(
                reference_loaded,
                candidate_loaded,
                requested_dimensions=("loudness",),
                section_pairs=(
                    {
                        "reference": {"section_id": "reference-drop", "start_seconds": 0.0, "end_seconds": 1.0},
                        "candidate": {"section_id": "candidate-drop", "start_seconds": 0.0, "end_seconds": 1.0},
                    },
                ),
            )
        self.assertEqual(comparison.alignment_state, "aligned")
        self.assertEqual(tuple(comparison.requested_dimensions), ("loudness",))
        self.assertEqual(tuple(comparison.measurements["comparisons"]), ("loudness",))
        self.assertEqual(len(comparison.measurements["paired_section_metrics"]), 1)
        self.assertIn("loudness", comparison.measurements["paired_section_metrics"][0]["metrics"])
        self.assertTrue(comparison.measurements["directional_only"])
        self.assertTrue(comparison.measurements["reference_not_defect"])

    def test_reference_alignment_failure_withholds_all_metrics(self) -> None:
        candidate = self.asset("clean_baseline.wav", "candidate_full_mix")
        reference = self.asset("improved_contrast_with_regression.wav", "reference_full_mix")
        cache = DecodedAudioCache(max_entries=8, max_bytes=64 * 1024 * 1024)
        candidate_loaded = cache.get_or_decode(candidate, max_seconds=4.0)
        reference_loaded = cache.get_or_decode(reference, max_seconds=4.0)
        with mock.patch.object(
            review_contrast,
            "_aligned_reference_payload",
            side_effect=RuntimeError("ambiguous timeline"),
        ):
            comparison = review_contrast.compare_reference_features(
                reference_loaded,
                candidate_loaded,
                requested_dimensions=("loudness",),
                section_pairs=(("reference-drop", "candidate-drop"),),
            )
        self.assertEqual(comparison.alignment_state, "failed")
        self.assertFalse(comparison.measurements["comparison_ready"])
        self.assertEqual(dict(comparison.measurements["comparisons"]), {})
        self.assertEqual(tuple(comparison.paired_section_ids), ())
        self.assertTrue(comparison.measurements["directional_only"])
        self.assertTrue(comparison.measurements["reference_not_defect"])

    def test_generated_note_metrics_are_bounded_and_truthful(self) -> None:
        def sequence_output(
            output_id: str,
            role_id: str,
            section_id: str,
            name: str,
            notes: tuple[dict[str, float], ...],
        ) -> ReviewGeneratedOutput:
            return ReviewGeneratedOutput(
                output_id=output_id,
                output_kind="note_sequence",
                role_id=role_id,
                section_id=section_id,
                metadata=FrozenMap(
                    {
                        "name": name,
                        "generator": "deterministic-test",
                        "notes": notes,
                        "duration_beats": 4.0,
                    }
                ),
            )

        source = ReviewSourceSnapshot(
            source_run_id="run-notes",
            original_brief="Create a lead motif over chord roots.",
            completion_target="playable_draft",
            generated_note_sequences=(
                sequence_output(
                    "chords-a",
                    "chords",
                    "drop-a",
                    "Drop Chords",
                    (
                        {"pitch": 60.0, "start_beats": 0.0, "duration_beats": 2.0, "velocity": 0.8},
                        {"pitch": 64.0, "start_beats": 0.0, "duration_beats": 2.0, "velocity": 0.8},
                        {"pitch": 67.0, "start_beats": 0.0, "duration_beats": 2.0, "velocity": 0.8},
                    ),
                ),
                sequence_output(
                    "lead-a",
                    "lead",
                    "drop-a",
                    "Drop Lead",
                    ({"pitch": 64.0, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 0.8},),
                ),
                sequence_output(
                    "lead-b",
                    "lead",
                    "drop-b",
                    "Drop Lead",
                    ({"pitch": 65.0, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 0.8},),
                ),
                sequence_output(
                    "bass-a",
                    "bass",
                    "drop-a",
                    "Drop Bass",
                    ({"pitch": 60.0, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 0.8},),
                ),
                sequence_output(
                    "sub-a",
                    "sub",
                    "drop-a",
                    "Drop Sub",
                    ({"pitch": 60.0, "start_beats": 0.0, "duration_beats": 1.0, "velocity": 0.8},),
                ),
            ),
        )
        analysis, limitations = review_analysis._generated_note_analysis(source)
        self.assertTrue(analysis["motif_overlap"]["available"])
        self.assertTrue(analysis["harmonic_agreement"]["available"])
        self.assertIn("chord_root_to_bass", analysis["harmonic_agreement"]["metrics"])
        self.assertTrue(analysis["section_development"]["available"])
        self.assertTrue(any("persisted" in value for value in limitations))


if __name__ == "__main__":
    unittest.main(verbosity=2)
