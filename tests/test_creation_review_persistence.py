"""Focused persistence-redaction and transport contract tests."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import fl_studio_mcp.creation_review.persistence as review_persistence

from fl_studio_mcp.creation_review.models import (
    CreationEvaluationReport,
    EvaluationFinding,
    RecordFeedbackLockOperation,
    ReviewAudioAsset,
    ReviewGeneratedOutput,
    ReviewSectionMap,
    ReviewSession,
    ReviewSessionRequest,
    RevisionPlan,
    TempoChange,
)
from fl_studio_mcp.creation_review.persistence import LocalReviewSessionStore


class CreationReviewPersistenceTests(unittest.TestCase):
    @staticmethod
    def _session(session_id: str) -> ReviewSession:
        return ReviewSession(
            review_session_id=session_id,
            source_run_id="run-1",
            request=ReviewSessionRequest(
                source_run_id="run-1",
                brief=f"Persist {session_id}.",
                persist_session=True,
            ),
        )

    def test_persistence_redacts_sensitive_metadata_and_honors_asset_paths(self) -> None:
        asset = ReviewAudioAsset(
            asset_id="asset-1",
            asset_kind="candidate_full_mix",
            path="/private/bounce.wav",
            display_label="candidate",
        )
        generated = ReviewGeneratedOutput(
            output_id="output-1",
            output_kind="pattern",
            metadata={
                "prompt": "drop this prompt",
                "raw_transcript": "drop this transcript",
                "api_key": "sk-secret-value",
                "apiToken": "api-token-value",
                "clientSecret": "client-secret-value",
                "cloudThreadId": "cloud-thread-camel",
                "cloud_thread_id": "cloud-thread",
                "audio_bytes": "drop this media",
                "filename": "/private/palette.json",
                "input_file": "/private/project.flp",
                "location": "/private/other/location",
                "encoded": "UklGRgAAAAA=",
                "wav_blob": "UklGRgAAAAA=",
                "environment": {"SERVICE_TOKEN": "tok-secret-value"},
                "assets": ({"path": "/private/spoofed-asset.wav"},),
                "retained": "functional state",
            },
        )
        request = ReviewSessionRequest(
            source_run_id="run-1",
            brief="Keep the arrangement.",
            persist_session=True,
            persist_asset_paths=False,
        )
        session = ReviewSession(
            review_session_id="review-1",
            source_run_id="run-1",
            request=request,
            assets=(asset,),
            source_note_sequences=(generated,),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.json"
            LocalReviewSessionStore(path).save(session)
            encoded = path.read_text(encoding="utf-8")
            self.assertNotIn("/private/bounce.wav", encoded)
            for value in (
                "drop this prompt",
                "drop this transcript",
                "sk-secret-value",
                "api-token-value",
                "client-secret-value",
                "cloud-thread-camel",
                "cloud-thread",
                "drop this media",
                "/private/palette.json",
                "/private/project.flp",
                "/private/other/location",
                "UklGRgAAAAA=",
                "tok-secret-value",
                "/private/spoofed-asset.wav",
            ):
                self.assertNotIn(value, encoded)
            self.assertIn("functional state", encoded)

            path_with_assets = Path(directory) / "sessions-with-assets.json"
            persisted_request = request.model_copy(update={"persist_asset_paths": True})
            LocalReviewSessionStore(path_with_assets).save(
                session.model_copy(update={"request": persisted_request})
            )
            persisted = json.loads(path_with_assets.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted["sessions"][0]["assets"][0]["path"],
                "/private/bounce.wav",
            )
            persisted_text = path_with_assets.read_text(encoding="utf-8")
            self.assertNotIn("/private/palette.json", persisted_text)
            self.assertNotIn("/private/project.flp", persisted_text)
            self.assertNotIn("/private/other/location", persisted_text)
            self.assertNotIn("UklGRgAAAAA=", persisted_text)

    def test_section_map_scales_denominator_and_integrates_tempo_changes(self) -> None:
        section_map = ReviewSectionMap(
            tempo_bpm=120.0,
            time_signature_numerator=4,
            time_signature_denominator=8,
            bar_to_time_basis="tempo_change_map",
            tempo_changes=(TempoChange(start_bar=2.5, tempo_bpm=60.0),),
        )
        self.assertAlmostEqual(section_map.bar_to_seconds(2), 1.0)
        self.assertAlmostEqual(section_map.bar_to_seconds(3), 2.5)
        self.assertAlmostEqual(section_map.bar_to_seconds(3, 3.0), 4.0)
        with self.assertRaises(ValueError):
            section_map.bar_to_seconds(3, 4.0)
        with self.assertRaises(ValueError):
            section_map.bar_to_seconds(1, 5.0)
        with self.assertRaises(ValueError):
            ReviewSectionMap(
                tempo_bpm=120.0,
                tempo_changes=(
                    TempoChange(start_bar=3, tempo_bpm=60.0),
                    TempoChange(start_bar=2, tempo_bpm=90.0),
                ),
            )

    def test_finding_pruning_preserves_retained_plan_traceability(self) -> None:
        report = CreationEvaluationReport(
            evaluation_id="evaluation-1",
            review_session_id="review-1",
            source_run_id="run-1",
            asset_set_digest="a" * 64,
            section_map_digest="b" * 64,
            analyzer_version="test",
            findings=(
                EvaluationFinding(
                    finding_id="priority-first",
                    category="clipping_or_headroom",
                    evidence_source="decoded_audio_measurement",
                    severity="critical",
                    explanation="Critical first-ranked finding.",
                ),
                EvaluationFinding(
                    finding_id="revision-trace",
                    category="low_end",
                    evidence_source="explicit_user_feedback",
                    explanation="Finding retained by a recorded plan.",
                ),
            ),
            top_priorities=("priority-first", "revision-trace"),
        )
        plan = RevisionPlan(
            revision_plan_id="plan-1",
            review_session_id="review-1",
            source_evaluation_id="evaluation-1",
            source_run_id="run-1",
            targeted_findings=("revision-trace",),
            operations=(
                RecordFeedbackLockOperation(
                    operation_id="lock-1",
                    finding_ids=("revision-trace",),
                ),
            ),
        )
        session = ReviewSession(
            review_session_id="review-1",
            source_run_id="run-1",
            request=ReviewSessionRequest(
                source_run_id="run-1",
                brief="Persist bounded traceability.",
                persist_session=True,
            ),
            evaluations=(report,),
            revision_plans=(plan,),
        )
        with tempfile.TemporaryDirectory() as directory:
            stored = LocalReviewSessionStore(
                Path(directory) / "sessions.json",
                max_findings=1,
            ).save(session)
        self.assertEqual(
            tuple(item.finding_id for item in stored.evaluations[0].findings),
            ("revision-trace",),
        )
        self.assertEqual(stored.evaluations[0].top_priorities, ("revision-trace",))

    def test_two_store_instances_do_not_lose_concurrent_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.json"
            stores = (LocalReviewSessionStore(path), LocalReviewSessionStore(path))
            barrier = threading.Barrier(2)
            errors: list[BaseException] = []

            def save(index: int) -> None:
                try:
                    barrier.wait(timeout=2.0)
                    stores[index].save(self._session(f"review-{index + 1}"))
                except BaseException as exc:  # pragma: no cover - diagnostic path
                    errors.append(exc)

            threads = tuple(threading.Thread(target=save, args=(index,)) for index in range(2))
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(3.0)
            self.assertEqual(errors, [])
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(
                {item.review_session_id for item in LocalReviewSessionStore(path).snapshot()},
                {"review-1", "review-2"},
            )

    def test_directory_fsync_failure_does_not_report_committed_write_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.json"
            store = LocalReviewSessionStore(path)
            real_fsync = review_persistence.os.fsync
            calls = 0

            def fail_directory_fsync(descriptor: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected directory fsync failure")
                real_fsync(descriptor)

            with mock.patch.object(
                review_persistence.os,
                "fsync",
                side_effect=fail_directory_fsync,
            ):
                stored = store.save(self._session("review-committed"))
            self.assertEqual(stored.review_session_id, "review-committed")
            self.assertTrue(path.exists())
            self.assertIsNotNone(
                LocalReviewSessionStore(path).get("review-committed")
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
