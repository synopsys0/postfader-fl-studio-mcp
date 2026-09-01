"""Focused tests for the read-only creation-pipeline foundation."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from fl_studio_mcp.contracts import ConnectionInfo
from fl_studio_mcp.creation_pipeline.context import (
    ContextTargetIdentity,
    CreationRunContextSnapshot,
    build_context_snapshot,
)
from fl_studio_mcp.creation_pipeline.models import (
    ConnectionReadiness,
    CreationReadinessInput,
    CreationReadinessReport,
    DrumCoverage,
    EffectCoverageReport,
    InstrumentPoolCoverage,
    InstrumentTargetCoverage,
    PatternCoverage,
    PianoRollReadiness,
)
from fl_studio_mcp.creation_pipeline.outcomes import (
    ArrangementDeliveryOutcome,
    AudibleQualityOutcome,
    CreationOutcome,
    ManualHandoffItem,
    ManualHandoffOutcome,
    ProcessingOutcome,
    TechnicalExecutionOutcome,
    build_creation_outcome,
)
from fl_studio_mcp.creation_pipeline.readiness import CreationReadinessService
from fl_studio_mcp.creation_pipeline.timing import RunTimingCollector
from fl_studio_mcp.track_b_contracts import ChannelGeneratorTarget


SESSION = "a" * 32
TARGET = "b" * 64
WHEN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def connection(**updates: object) -> ConnectionInfo:
    values: dict[str, object] = {
        "connected": True,
        "compatible": True,
        "compatibility_reason": "ok",
        "runtime_write_mode_control": True,
        "bridge_provenance": "matching",
        "bridge_provenance_verified": True,
        "session_fingerprint": SESSION,
    }
    values.update(updates)
    return ConnectionInfo(**values)


def piano_roll() -> PianoRollReadiness:
    return PianoRollReadiness(
        apply_script_present=True,
        armed_this_process=True,
        authenticated_arming_receipt=True,
        arming_receipt_id="arm-1",
        target_selection_supported=True,
        persistence_receipt_supported=True,
    )


def ready_facts(**updates: object) -> CreationReadinessInput:
    values: dict[str, object] = {
        "observed_at": WHEN,
        "connection": ConnectionReadiness(
            connection=connection(),
            mcp_process_identity="process-1",
            package_source_revision="rev-1",
            deployed_bridge_revision="rev-1",
            running_bridge_revision="rev-1",
            midi_endpoint="endpoint-1",
            midi_input_available=True,
            midi_output_available=True,
            queue_healthy=True,
            supported_fl_build=True,
        ),
        "piano_roll": piano_roll(),
        "effects": EffectCoverageReport(dry_by_design=True),
    }
    values.update(updates)
    return CreationReadinessInput(**values)


class CreationPipelineFoundationTests(unittest.TestCase):
    def test_fully_ready_report_is_immutable_and_has_context(self) -> None:
        report = CreationReadinessService().evaluate(ready_facts())

        self.assertEqual(report.overall_state, "ready")
        self.assertEqual(report.score, 100.0)
        self.assertFalse(report.blockers)
        self.assertTrue(report.zero_mutations)
        self.assertFalse(report.mutations_performed)
        self.assertIsInstance(report.context_snapshot, CreationRunContextSnapshot)
        round_trip = CreationReadinessReport.model_validate(
            report.model_dump(mode="python")
        )
        self.assertIsInstance(round_trip.context_snapshot, CreationRunContextSnapshot)
        self.assertEqual(
            round_trip.context_snapshot.context_digest,
            report.context_snapshot.context_digest,
        )
        with self.assertRaises((TypeError, ValueError)):
            report.overall_state = "blocked"  # type: ignore[misc]

    def test_non_blocking_playlist_handoff_is_aggregated(self) -> None:
        facts = ready_facts(
            patterns=PatternCoverage(
                manual_playlist_placement_required=True,
                expected_manual_playlist_actions=("Place Drop A at bar 17.",),
            )
        )
        report = CreationReadinessService().evaluate(facts)

        self.assertEqual(report.overall_state, "ready_with_limitations")
        self.assertFalse(report.blockers)
        self.assertEqual(len(report.manual_actions), 1)
        self.assertEqual(report.manual_actions[0].action_id, "playlist-placement-1")
        self.assertIn("manual_playlist_placement", {item.code for item in report.limitations})

    def test_independent_blockers_are_returned_together(self) -> None:
        facts = ready_facts(
            connection=ConnectionReadiness(
                connection=connection(
                    bridge_source_sha256="c" * 64,
                    expected_bridge_source_sha256="d" * 64,
                ),
                mcp_process_identity="process-1",
                midi_input_available=False,
                midi_output_available=False,
            ),
            piano_roll=PianoRollReadiness(),
            drum_coverage=DrumCoverage(required=True, required_roles=("kick", "snare")),
            patterns=PatternCoverage(required_empty_patterns=(7, 8)),
            effects=EffectCoverageReport(
                completion_target="polished_mix_ready",
                processing_required_for_completion=True,
                missing_capabilities=(
                    {
                        "category": "reverb",
                        "reason": "no compatible loaded effect",
                        "required_for_completion": True,
                    },
                ),
            ),
        )
        report = CreationReadinessService().evaluate(facts)
        codes = {item.code for item in report.blockers}

        self.assertEqual(report.overall_state, "blocked")
        self.assertGreaterEqual(len(report.blockers), 8)
        for expected in {
            "bridge_source_revision_mismatch",
            "midi_input_unavailable",
            "midi_output_unavailable",
            "piano_roll_not_armed",
            "drum_generator_missing",
            "empty_pattern_missing",
            "required_processing_missing",
        }:
            self.assertIn(expected, codes)

    def test_missing_optional_effect_is_limitation_for_draft(self) -> None:
        facts = ready_facts(
            effects=EffectCoverageReport(
                requested_categories=("reverb",),
                missing_capabilities=(
                    {"category": "reverb", "reason": "not loaded"},
                ),
            )
        )
        report = CreationReadinessService().evaluate(facts)

        self.assertEqual(report.overall_state, "ready_with_limitations")
        self.assertFalse(report.blockers)
        self.assertIn("missing_requested_effect", {item.code for item in report.limitations})

    def test_readiness_does_not_mutate_observations(self) -> None:
        target = InstrumentTargetCoverage(
            target=ChannelGeneratorTarget(channel_index=2),
            target_fingerprint=TARGET,
            product_name="Synthetic Generator",
            requested_roles=("lead",),
            usable_preset_candidate=True,
            preset_identity_verified=True,
        )
        facts = ready_facts(
            requested_roles=("lead",),
            instrument_pool=InstrumentPoolCoverage(
                loaded_generators=(target,),
                requested_roles=("lead",),
                covered_roles=("lead",),
                preset_navigation_supported=True,
                usable_preset_discovery=True,
            ),
        )
        before = facts.model_dump(mode="json")
        report = CreationReadinessService().evaluate(facts)

        self.assertEqual(before, facts.model_dump(mode="json"))
        self.assertEqual(facts.instrument_pool.loaded_generators[0], target)
        self.assertIsNotNone(report.context_snapshot)
        self.assertEqual(report.context_snapshot.relevant_target_fingerprints, (TARGET,))

    def test_context_snapshot_reuse_and_refresh_preserve_old_digest(self) -> None:
        snapshot = build_context_snapshot(
            session_fingerprint=SESSION,
            target_fingerprints=(
                ContextTargetIdentity(
                    target_id="lead",
                    kind="channel_generator",
                    fingerprint=TARGET,
                ),
            ),
            captured_at=WHEN,
        )
        original_digest = snapshot.context_digest
        reused = snapshot.model_copy()
        refreshed = snapshot.with_target_refresh(
            ContextTargetIdentity(
                target_id="lead",
                kind="channel_generator",
                fingerprint="c" * 64,
            )
        )

        self.assertEqual(reused.context_digest, original_digest)
        self.assertEqual(snapshot.target_fingerprint_for("lead"), TARGET)
        self.assertNotEqual(refreshed.context_digest, original_digest)
        self.assertEqual(snapshot.target_fingerprint_for("lead"), TARGET)

    def test_timing_collector_tracks_phases_and_soft_warnings(self) -> None:
        ticks = iter(
            (
                WHEN,
                WHEN + timedelta(seconds=2),
                WHEN + timedelta(seconds=2),
            )
        )
        collector = RunTimingCollector(clock=lambda: next(ticks), phase_soft_target_ms=1000)
        collector.start_phase("preflight")
        collector.record_full_inventory_scan()
        collector.record_piano_roll_preparation()
        collector.end_phase()
        collector.skip_phase("palette", "no palette requested")
        report = collector.report(generated_at=WHEN)

        self.assertEqual(report.phase_timings[0].duration_ms, 2000.0)
        self.assertTrue(report.phase_timings[1].skipped)
        self.assertEqual(report.operation_summary.full_inventory_scan_count, 1)
        self.assertTrue(any(item.code == "phase_soft_target_exceeded" for item in report.warnings))
        self.assertTrue(report.local_only)
        self.assertFalse(report.telemetry_uploaded)

    def test_timing_collector_merges_resumed_phase_without_duplicate_row(self) -> None:
        collector = RunTimingCollector()
        collector.start_phase("processing", started_at=WHEN)
        first = collector.end_phase(
            ended_at=WHEN + timedelta(milliseconds=125)
        )
        initial_report = collector.report(generated_at=WHEN)
        continuation = RunTimingCollector.from_report(
            initial_report,
            clock=lambda: WHEN + timedelta(seconds=1, milliseconds=225),
        )
        continuation.resume_phase(
            "processing", started_at=WHEN + timedelta(seconds=1)
        )
        continuation.record_operation(2)
        resumed = continuation.end_phase(
            ended_at=WHEN + timedelta(seconds=1, milliseconds=225)
        )
        report = continuation.report(generated_at=WHEN)

        self.assertEqual(first.operation_count, 0)
        self.assertEqual(resumed.operation_count, 2)
        self.assertEqual(len(report.phase_timings), 1)
        self.assertEqual(report.phase_timings[0].duration_ms, 350.0)
        self.assertEqual(report.operation_summary.operation_count, 2)

    def test_outcome_keeps_audible_quality_separate(self) -> None:
        technical = TechnicalExecutionOutcome(
            status="verified",
            expected_operations=2,
            attempted_operations=2,
            completed_operations=2,
            verified_receipts=2,
        )
        outcome = build_creation_outcome(
            technical=technical,
            arrangement=ArrangementDeliveryOutcome(status="playable", pattern_count=4),
            processing=ProcessingOutcome(status="dry_missing_effects", missing_effects=("reverb",)),
        )

        self.assertIsInstance(outcome, CreationOutcome)
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.audible_quality.status, "not_evaluated")
        self.assertEqual(outcome.processing.status, "dry_missing_effects")
        self.assertFalse(outcome.audible_quality.status == "user_approved")

    def test_partial_and_manual_handoff_outcomes_are_truthful(self) -> None:
        item = ManualHandoffItem(
            action_id="playlist-1",
            dimension="arrangement",
            instruction="Place the generated pattern in the Playlist.",
        )
        outcome = build_creation_outcome(
            technical=TechnicalExecutionOutcome(status="partial"),
            arrangement=ArrangementDeliveryOutcome(
                status="patterns_created_not_placed",
                manual_playlist_actions=(item,),
            ),
            processing=ProcessingOutcome(status="not_requested"),
            audible_quality=AudibleQualityOutcome(status="user_rejected", evidence_source="user_statement"),
            manual_handoff=ManualHandoffOutcome(status="outstanding", actions=(item,)),
        )

        self.assertEqual(outcome.status, "partial")
        self.assertEqual(outcome.manual_handoff.status, "outstanding")
        self.assertEqual(outcome.audible_quality.status, "user_rejected")


if __name__ == "__main__":
    unittest.main()
