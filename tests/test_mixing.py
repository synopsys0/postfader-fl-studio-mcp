"""Deterministic tests for production-copilot workflows."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fl_studio_mcp.bridge_install import expected_bridge_deployment
from fl_studio_mcp.mixing import (
    PeakFrame,
    PeakFrameTrack,
    PeakTrackAggregate,
    PeakWatchRegistry,
    PeakWatchReport,
    create_gain_stage_plan,
    list_plugin_profiles,
    masking_recommendations,
    reference_recommendations,
    resolve_processing_intent,
    run_mix_doctor,
)
from fl_studio_mcp.track_b_contracts import (
    MixerEffectTarget,
    TargetedLoadedPluginInventory,
    TargetedPluginSummary,
)


ROOT = Path(__file__).resolve().parents[1]
AUDIO = ROOT / "tests" / "fixtures" / "audio"
SESSION = "a" * 32


class CompatibleClient:
    transport = "tcp"

    def ping(self):
        return {
            "pong": True,
            "protocol": 2,
            "program_title": "FL Studio 2026",
            "fl_version": "Producer Edition v26.1.3 [build 5336]",
            "midi_scripting_api_version": 44,
            "bridge_mode": "read_only",
            "verified_writes_enabled": False,
            "runtime_write_mode_control": True,
            "write_mode_origin": "disabled",
            "startup_write_mode_enabled": False,
            "bridge_source_sha256": expected_bridge_deployment()[1],
            "session_fingerprint": SESSION,
        }


class FakePeakReader:
    def read(self, *, only_used: bool, max_tracks: int) -> PeakFrame:
        return PeakFrame(
            observed_at=datetime.now(timezone.utc),
            session_fingerprint=SESSION,
            observed_idle_tick=1,
            playing=True,
            song_position_normalized=0.25,
            total_track_count=126,
            scanned_track_count=max_tracks,
            partial=max_tracks < 126,
            tracks=[
                PeakFrameTrack(
                    track_index=3,
                    name="Lead Vox",
                    fader_normalized=0.8,
                    fader_db=0.0,
                    muted=False,
                    peak_left=0.25,
                    peak_right=0.2,
                    peak_max=0.25,
                    peak_dbfs=-12.041,
                )
            ],
        )


class MixingWorkflowTests(unittest.TestCase):
    def test_mix_doctor_uses_decoded_bounce_measurements(self) -> None:
        report = run_mix_doctor(
            str(AUDIO / "candidate_delayed_minus6db.wav"),
            target="balanced",
            reference_path=str(AUDIO / "reference_mix.wav"),
        )
        self.assertEqual(report.policy_version, "postfader-mix-policy-1")
        self.assertEqual(
            report.analysis.file.sha256,
            "c04cd63c2d9cec098e5e4ed28296539d7a3899ebc9fb19cb5f0210ef8b6c8785",
        )
        self.assertTrue(report.issues)
        self.assertFalse(report.mutations_applied)
        self.assertFalse(report.reference_comparison.comparison_ready)

    def test_reference_withholds_when_alignment_is_not_ready(self) -> None:
        report = reference_recommendations(
            str(AUDIO / "reference_mix.wav"),
            str(AUDIO / "candidate_delayed_minus6db.wav"),
        )
        self.assertFalse(report.actionable)
        self.assertEqual(report.adjustments, [])
        self.assertIn("withheld", report.warnings[0])

    def test_masking_recommendations_require_synchronous_context(self) -> None:
        report = masking_recommendations(
            str(AUDIO / "reference_mix.wav"),
            str(AUDIO / "candidate_delayed_minus6db.wav"),
        )
        self.assertTrue(report.actionable)
        self.assertTrue(report.remediations)
        self.assertTrue(all(item.preferred_method == "dynamic_eq_or_automation" for item in report.remediations))

    def test_peak_watch_persists_and_stops_without_mutating_fl(self) -> None:
        registry = PeakWatchRegistry()
        with mock.patch("fl_studio_mcp.mixing._PeakReader", return_value=FakePeakReader()):
            started = registry.start(
                duration_seconds=1.0,
                interval_ms=500,
                only_used=True,
                max_tracks=16,
            )
            stopped = registry.stop(started.watch_id)
        self.assertGreaterEqual(stopped.frame_count, 1)
        self.assertEqual(stopped.status, "stopped")
        self.assertEqual(stopped.tracks[0].max_peak_dbfs, -12.041)

    def test_gain_stage_plan_uses_db_fader_receipts_not_curve_guessing(self) -> None:
        watch = PeakWatchReport(
            watch_id="b" * 32,
            status="completed",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            requested_duration_seconds=60.0,
            interval_ms=500,
            only_used=True,
            max_tracks=126,
            frame_count=120,
            session_fingerprint=SESSION,
            tracks=[
                PeakTrackAggregate(
                    track_index=3,
                    name="Lead Vox",
                    sample_count=120,
                    max_peak_linear=0.5,
                    max_peak_dbfs=-6.021,
                    clipping_frame_count=0,
                    last_fader_normalized=0.8,
                    last_fader_db=0.0,
                    last_muted=False,
                )
            ],
        )
        with (
            mock.patch("fl_studio_mcp.mixing.PEAK_WATCHES.get", return_value=watch),
            mock.patch("fl_studio_mcp.mixing.get_client", return_value=CompatibleClient()),
        ):
            result = create_gain_stage_plan("b" * 32, target_peak_dbfs=-12.0)
        self.assertIsNotNone(result.plan)
        operation = result.plan.operations[0]
        self.assertEqual(operation.operation, "mixer_volume_db")
        self.assertAlmostEqual(operation.volume_db, -5.98, places=2)
        self.assertEqual(operation.expected_before.volume_db, 0.0)

    def test_profiles_and_intents_match_loaded_stock_plugin(self) -> None:
        catalog = list_plugin_profiles("compressor")
        self.assertEqual(catalog.profile_count, 1)
        inventory = TargetedLoadedPluginInventory(
            observed_at=datetime.now(timezone.utc),
            plugins=[
                TargetedPluginSummary(
                    target=MixerEffectTarget(
                        track_index=3, slot_index=1, allow_master=False
                    ),
                    name="Fruity Compressor",
                    reported_parameter_count=6,
                    mix_level_normalized=1.0,
                )
            ],
        )
        inspector = mock.Mock()
        inspector.scan_loaded_plugins.return_value = inventory
        with mock.patch("fl_studio_mcp.mixing.TrackBInspector", return_value=inspector):
            resolution = resolve_processing_intent(
                "control_dynamics", track_index=3, strength=0.6
            )
        self.assertTrue(resolution.ready)
        self.assertEqual(resolution.steps[0].compatible_profile_ids, ("fl-fruity-compressor",))
        self.assertTrue(resolution.steps[0].loaded_targets)
        self.assertFalse(resolution.mutations_applied)


if __name__ == "__main__":
    unittest.main()
