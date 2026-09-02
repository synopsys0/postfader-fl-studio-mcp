"""Focused contracts and local-persistence tests for Creation Review."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fl_studio_mcp.creation_review.models import (
    AlignmentResult,
    CreationEvaluationReport,
    CreationFeedback,
    DeliveryManifest,
    EvaluationFinding,
    PlaylistHandoff,
    PlaylistPlacement,
    ReviewAssetSet,
    ReviewAudioAsset,
    ReviewSession,
    ReviewSessionRequest,
    RevisionComparison,
    RevisionPass,
)
from fl_studio_mcp.creation_review.persistence import (
    LocalReviewSessionStore,
    ReviewSessionCorruptionError,
    ReviewSessionWriteError,
)
from fl_studio_mcp.creation_review.sections import build_review_section_map
from fl_studio_mcp.creation_review.sessions import (
    InvalidReviewSessionTransition,
    ReviewAudioAssetError,
    ReviewSessionError,
    ReviewSessionLimitError,
    ReviewSessionRegistry,
    UnknownSourceRunError,
    snapshot_source_run,
)


def _request(source_run_id: str = "run-1", **updates: object) -> ReviewSessionRequest:
    payload: dict[str, object] = {
        "source_run_id": source_run_id,
        "brief": "Review the playable draft.",
        "persist_session": True,
        "persist_asset_paths": False,
        "max_revision_passes": 3,
    }
    payload.update(updates)
    return ReviewSessionRequest(**payload)


def _source(source_run_id: str = "run-1") -> dict[str, object]:
    return {
        "state": {
            "run_id": source_run_id,
            "status": "completed",
            "request": {
                "brief": "Make a playable draft.",
                "completion_target": "playable_draft",
                "preserve": {"note_content": True},
            },
            "generated_outputs": [],
            "receipts": [],
        }
    }


def _asset(asset_id: str, kind: str, digest_character: str, path: str) -> ReviewAudioAsset:
    return ReviewAudioAsset(
        asset_id=asset_id,
        asset_kind=kind,  # type: ignore[arg-type]
        path=path,
        display_label=asset_id,
        sha256=digest_character * 64,
        sample_rate_hz=48_000,
        channels=2,
        duration_seconds=10.0,
        file_size_bytes=128,
        validation_state="valid",
    )


class CreationReviewSessionTests(unittest.TestCase):
    def test_source_snapshot_retains_sections_patterns_and_processing_receipts(self) -> None:
        source = _source()
        source["plan"] = {
            "operations": [
                {
                    "operation": "add_section_markers",
                    "operation_id": "markers",
                    "markers": [
                        {"name": "Intro", "bar_number": 1},
                        {"name": "Drop", "bar_number": 9},
                    ],
                },
                {
                    "operation": "prepare_pattern",
                    "operation_id": "pattern",
                    "pattern_number": 2,
                    "name": "PF Drop",
                    "length_beats": 32,
                },
            ]
        }
        state = source["state"]
        assert isinstance(state, dict)
        state["receipts"] = [
            {
                "operation_id": "processing",
                "operation": "apply_processing_plan",
                "result": {"plan_id": "plan-1", "verified": True},
            }
        ]
        snapshot = snapshot_source_run("run-1", source)
        self.assertEqual([item.name for item in snapshot.sections], ["Intro", "Drop"])
        self.assertEqual(snapshot.pattern_plan[0].pattern_number, 2)
        self.assertEqual(snapshot.processing_receipts[0].output_id, "processing")

    def test_source_snapshot_retains_generated_output_section_identity(self) -> None:
        source = _source()
        source["plan"] = {
            "operations": (
                {
                    "operation_id": "planned-sequence",
                    "operation": "generate_melody",
                    "section_id": "drop",
                },
            )
        }
        state = source["state"]
        assert isinstance(state, dict)
        state["generated_outputs"] = (
            {
                "output": "note_sequence",
                "operation_id": "reported-sequence",
                "section_id": "chorus",
                "value": {"notes": ()},
            },
            {
                "output": "note_sequence",
                "operation_id": "planned-sequence",
                "value": {"notes": ()},
            },
        )

        snapshot = snapshot_source_run("run-1", source)

        self.assertEqual(
            [item.section_id for item in snapshot.generated_note_sequences],
            ["chorus", "drop"],
        )

    def test_source_transport_checkpoint_is_retained_for_sections_and_tempo_map(self) -> None:
        source = _source()
        state = source["state"]
        assert isinstance(state, dict)
        state["run_context"] = {
            "project_checkpoint": {
                "transport": {
                    "tempo_bpm": 150.0,
                    "time_signature_numerator": 3,
                    "time_signature_denominator": 8,
                    "tempo_changes": ({"start_bar": 5.5, "tempo_bpm": 100.0},),
                }
            }
        }
        state["sections"] = (
            {"section_id": "bars", "name": "Bars", "start_bar": 1, "end_bar": 6},
        )

        snapshot = snapshot_source_run("run-1", source)
        self.assertEqual(snapshot.tempo_bpm, 150.0)
        self.assertEqual(snapshot.time_signature_numerator, 3)
        self.assertEqual(snapshot.time_signature_denominator, 8)
        self.assertEqual(snapshot.tempo_changes[-1].start_bar, 5.5)
        self.assertEqual(snapshot.sections[0].start_seconds, 0.0)
        self.assertEqual(snapshot.sections[0].end_seconds, 3.15)

        section_map = build_review_section_map(source)
        self.assertEqual(section_map.tempo_bpm, 150.0)
        self.assertEqual(section_map.time_signature_numerator, 3)
        self.assertEqual(section_map.time_signature_denominator, 8)
        self.assertEqual(section_map.tempo_changes[0].start_bar, 5.5)
        self.assertAlmostEqual(section_map.bar_to_seconds(6), 3.15)

    def test_bar_only_source_sections_never_get_zero_to_one_placeholder(self) -> None:
        source = _source()
        state = source["state"]
        assert isinstance(state, dict)
        state["sections"] = (
            {"section_id": "bar-only", "name": "Bar Only", "start_bar": 3, "end_bar": 5},
            {"section_id": "incomplete", "start_bar": 7},
        )
        snapshot = snapshot_source_run("run-1", source)
        self.assertEqual(len(snapshot.sections), 1)
        section = snapshot.sections[0]
        self.assertEqual(section.start_seconds, 4.0)
        self.assertEqual(section.end_seconds, 8.0)
        self.assertNotEqual((section.start_seconds, section.end_seconds), (0.0, 1.0))

    def test_unknown_source_is_rejected_when_lookup_is_configured(self) -> None:
        registry = ReviewSessionRegistry(source_lookup=lambda _source_id: None)
        with self.assertRaises(UnknownSourceRunError):
            registry.create(_request(), review_session_id="review-1")

    def test_process_local_request_does_not_write_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.json"
            store = LocalReviewSessionStore(path)
            registry = ReviewSessionRegistry(store=store, source_runs={})
            request = _request(persist_session=False)
            session = registry.create(request, source_run=_source(), review_session_id="review-1")
            self.assertEqual(session.review_session_id, "review-1")
            self.assertFalse(path.exists())
            self.assertIsNone(store.get("review-1"))

    def test_persisted_session_survives_recreation_without_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.json"
            store = LocalReviewSessionStore(path)
            registry = ReviewSessionRegistry(store=store, source_runs={"run-1": _source()})
            session = registry.create(_request(), review_session_id="review-1")
            asset = _asset("asset-1", "candidate_full_mix", "a", "/private/audio/bounce.wav")
            registry.attach_asset("review-1", asset, validate=False)
            persisted = json.loads(path.read_text("utf-8"))
            encoded = json.dumps(persisted)
            self.assertNotIn("/private/audio/bounce.wav", encoded)
            self.assertNotIn("asset-bytes", encoded)
            restored = LocalReviewSessionStore(path).get(session.review_session_id)
            assert restored is not None
            self.assertEqual(restored.assets[0].sha256, asset.sha256)
            self.assertIsNone(restored.assets[0].path)

    def test_store_bounds_findings_feedback_assets_and_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.json"
            store = LocalReviewSessionStore(
                path,
                max_assets=1,
                max_findings=1,
                max_feedback=1,
                max_revision_passes=1,
            )
            request = _request()
            first = _asset("asset-1", "candidate_full_mix", "a", "/private/one.wav")
            second = _asset("asset-2", "reference_full_mix", "b", "/private/two.wav")
            finding_one = EvaluationFinding(
                finding_id="finding-1",
                category="low_end",
                evidence_source="decoded_audio_measurement",
                explanation="One.",
            )
            finding_two = EvaluationFinding(
                finding_id="finding-2",
                category="stereo",
                evidence_source="decoded_audio_measurement",
                explanation="Two.",
            )
            report = CreationEvaluationReport(
                evaluation_id="evaluation-1",
                review_session_id="review-1",
                source_run_id="run-1",
                asset_set_digest="c" * 64,
                section_map_digest="d" * 64,
                analyzer_version="test",
                findings=(finding_one, finding_two),
                top_priorities=("finding-1", "finding-2"),
            )
            feedback_one = CreationFeedback(feedback_id="feedback-1")
            feedback_two = CreationFeedback(feedback_id="feedback-2")
            plan_pass_one = RevisionPass(
                revision_pass_id="pass-1",
                review_session_id="review-1",
                source_evaluation_id="evaluation-1",
                revision_plan_id="plan-1",
                source_run_id="run-1",
                status="completed",
            )
            plan_pass_two = plan_pass_one.model_copy(update={"revision_pass_id": "pass-2"})
            asset_set = ReviewAssetSet(
                asset_set_id="asset-set-1",
                candidate_full_mix=first,
            )
            comparison = RevisionComparison(
                comparison_id="comparison-1",
                before_asset=first,
                after_asset=second,
                alignment_result=AlignmentResult(state="aligned"),
            )
            session = ReviewSession(
                review_session_id="review-1",
                source_run_id="run-1",
                request=request,
                assets=(first, second),
                asset_sets=(asset_set,),
                evaluations=(report,),
                feedback=(feedback_one, feedback_two),
                revision_passes=(plan_pass_one, plan_pass_two),
                comparisons=(comparison,),
            )
            stored = store.save(session)
            self.assertEqual(len(stored.assets), 1)
            self.assertEqual(len(stored.evaluations[0].findings), 1)
            self.assertEqual(stored.evaluations[0].findings[0].finding_id, "finding-1")
            self.assertEqual(len(stored.feedback), 1)
            self.assertEqual(len(stored.revision_passes), 1)
            self.assertEqual(stored.asset_sets, ())
            self.assertEqual(stored.comparisons, ())

    def test_frozen_measurements_are_immutable_below_the_model(self) -> None:
        report = CreationEvaluationReport(
            evaluation_id="evaluation-1",
            review_session_id="review-1",
            source_run_id="run-1",
            asset_set_digest="c" * 64,
            section_map_digest="d" * 64,
            analyzer_version="test",
            global_measurements={"nested": {"values": [1, 2]}},
        )
        nested = report.global_measurements["nested"]
        assert hasattr(nested, "__getitem__")
        with self.assertRaises(TypeError):
            nested["values"] = ()  # type: ignore[index]
        with self.assertRaises(TypeError):
            report.global_measurements._values = ()  # type: ignore[misc]

    def test_revision_pass_replacement_respects_bound_without_rejecting_update(self) -> None:
        registry = ReviewSessionRegistry(max_sessions=2, source_runs={"run-1": _source()})
        request = _request(max_revision_passes=1, persist_session=False)
        registry.create(request, review_session_id="review-1")
        registry.transition("review-1", "evaluating")
        finding = EvaluationFinding(
            finding_id="finding-1",
            category="low_end",
            evidence_source="decoded_audio_measurement",
            explanation="One.",
        )
        registry.add_evaluation(
            "review-1",
            CreationEvaluationReport(
                evaluation_id="evaluation-1",
                review_session_id="review-1",
                source_run_id="run-1",
                asset_set_digest="c" * 64,
                section_map_digest="d" * 64,
                analyzer_version="test",
                findings=(finding,),
            ),
        )
        from fl_studio_mcp.creation_review.models import RevisionPlan

        plan = RevisionPlan(
            revision_plan_id="plan-1",
            review_session_id="review-1",
            source_evaluation_id="evaluation-1",
            source_run_id="run-1",
        )
        registry.add_revision_plan("review-1", plan)
        registry.transition("review-1", "revising")
        revision_pass = RevisionPass(
            revision_pass_id="pass-1",
            review_session_id="review-1",
            source_evaluation_id="evaluation-1",
            revision_plan_id="plan-1",
            source_run_id="run-1",
            status="completed",
        )
        registry.add_revision_pass("review-1", revision_pass)
        replaced = revision_pass.model_copy(update={"status": "blocked"})
        registry.add_revision_pass("review-1", replaced)
        assert registry.get("review-1") is not None
        assert registry.get("review-1").revision_passes[0].status == "blocked"  # type: ignore[union-attr]

    def test_nested_asset_paths_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.json"
            store = LocalReviewSessionStore(path)
            request = _request()
            before = _asset("before", "before_full_mix", "a", "/private/before.wav")
            after = _asset("after", "after_full_mix", "b", "/private/after.wav")
            candidate = _asset("candidate", "candidate_full_mix", "c", "/private/candidate.wav")
            asset_set = ReviewAssetSet(
                asset_set_id="asset-set-1",
                candidate_full_mix=candidate,
                before_full_mix=before,
                after_full_mix=after,
            )
            comparison = RevisionComparison(
                comparison_id="comparison-1",
                before_asset=before,
                after_asset=after,
                alignment_result=AlignmentResult(state="aligned"),
            )
            manifest = DeliveryManifest(
                delivery_id="delivery-1",
                source_run_id="run-1",
                review_session_id="review-1",
                review_assets=(candidate,),
                next_action="Listen and approve.",
            )
            session = ReviewSession(
                review_session_id="review-1",
                source_run_id="run-1",
                request=request,
                asset_sets=(asset_set,),
                assets=(before, after, candidate),
                comparisons=(comparison,),
                delivery_manifests=(manifest,),
            )
            store.save(session)
            encoded = path.read_text("utf-8")
            for private_path in ("/private/before.wav", "/private/after.wav", "/private/candidate.wav"):
                self.assertNotIn(private_path, encoded)

    def test_corrupt_store_is_isolated_until_explicit_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.json"
            path.write_text("{not-json", encoding="utf-8")
            store = LocalReviewSessionStore(path)
            status = store.status()
            self.assertTrue(status.corrupt)
            with self.assertRaises(ReviewSessionCorruptionError):
                store.save(
                    ReviewSession(
                        review_session_id="review-1",
                        source_run_id="run-1",
                        request=_request(),
                    )
                )
            self.assertEqual(path.read_text("utf-8"), "{not-json")
            result = store.reset(explicit=True)
            self.assertTrue(result.removed)
            self.assertFalse(path.exists())

    def test_registry_reset_clears_memory_and_allows_id_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.json"
            store = LocalReviewSessionStore(path)
            registry = ReviewSessionRegistry(
                store=store,
                source_runs={"run-1": _source()},
            )
            registry.create(_request(), review_session_id="review-reset")
            self.assertIsNotNone(registry.get("review-reset"))

            result = registry.reset_store(explicit=True)

            self.assertTrue(result.removed)
            self.assertIsNone(registry.get("review-reset"))
            self.assertEqual(registry.snapshot(), ())
            self.assertIsNone(LocalReviewSessionStore(path).get("review-reset"))
            recreated = registry.create(_request(), review_session_id="review-reset")
            self.assertEqual(recreated.review_session_id, "review-reset")

    def test_concurrent_feedback_updates_are_read_modify_write_atomic(self) -> None:
        registry = ReviewSessionRegistry(source_runs={"run-1": _source()})
        registry.create(
            _request(persist_session=False),
            review_session_id="review-concurrent",
        )
        worker_count = 24
        ready = threading.Barrier(worker_count)
        errors: list[BaseException] = []

        def record(index: int) -> None:
            try:
                ready.wait()
                registry.add_feedback(
                    "review-concurrent",
                    CreationFeedback(feedback_id=f"feedback-{index}"),
                )
            except BaseException as exc:  # pragma: no cover - assertion below reports it
                errors.append(exc)

        workers = [threading.Thread(target=record, args=(index,)) for index in range(worker_count)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        self.assertEqual(errors, [])
        session = registry.get("review-concurrent")
        assert session is not None
        self.assertEqual(
            {item.feedback_id for item in session.feedback},
            {f"feedback-{index}" for index in range(worker_count)},
        )

    def test_duplicate_id_is_rejected_before_capacity_eviction(self) -> None:
        registry = ReviewSessionRegistry(
            max_sessions=2,
            source_runs={"run-1": _source()},
        )
        registry.create(_request(persist_session=False), review_session_id="review-a")
        registry.create(_request(persist_session=False), review_session_id="review-b")
        registry.stop("review-a")

        with self.assertRaises(ReviewSessionError):
            registry.create(
                _request(persist_session=False),
                review_session_id="review-a",
            )

        self.assertIsNotNone(registry.get("review-a"))
        self.assertIsNotNone(registry.get("review-b"))
        self.assertEqual(
            {item.review_session_id for item in registry.snapshot()},
            {"review-a", "review-b"},
        )

    def test_process_local_capacity_does_not_evict_persisted_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.json"
            store = LocalReviewSessionStore(path)
            registry = ReviewSessionRegistry(
                store=store,
                max_sessions=1,
                source_runs={"run-1": _source()},
            )
            registry.create(_request(), review_session_id="review-persisted")

            with self.assertRaises(ReviewSessionLimitError):
                registry.create(
                    _request(persist_session=False),
                    review_session_id="review-local",
                )

            self.assertIsNotNone(registry.get("review-persisted"))
            self.assertIsNotNone(store.get("review-persisted"))
            self.assertIsNone(registry.get("review-local"))

    def test_registry_evicts_oldest_terminal_session_deterministically(self) -> None:
        registry = ReviewSessionRegistry(max_sessions=2, source_runs={"run-1": _source()})
        registry.create(_request(persist_session=False), review_session_id="review-a")
        registry.create(_request(persist_session=False), review_session_id="review-b")
        registry.stop("review-a")
        registry.create(_request(persist_session=False), review_session_id="review-c")
        self.assertIsNone(registry.get("review-a"))
        self.assertEqual(
            [item.review_session_id for item in registry.snapshot()],
            ["review-b", "review-c"],
        )

    def test_invalid_lifecycle_transition_is_rejected(self) -> None:
        registry = ReviewSessionRegistry(source_runs={"run-1": _source()})
        registry.create(_request(persist_session=False), review_session_id="review-1")
        with self.assertRaises(InvalidReviewSessionTransition):
            registry.transition("review-1", "completed")

    def test_update_methods_cannot_skip_lifecycle_states(self) -> None:
        registry = ReviewSessionRegistry(source_runs={"run-1": _source()})
        registry.create(_request(persist_session=False), review_session_id="review-1")
        finding = EvaluationFinding(
            finding_id="finding-1",
            category="low_end",
            evidence_source="decoded_audio_measurement",
            explanation="One.",
        )
        report = CreationEvaluationReport(
            evaluation_id="evaluation-1",
            review_session_id="review-1",
            source_run_id="run-1",
            asset_set_digest="c" * 64,
            section_map_digest="d" * 64,
            analyzer_version="test",
            findings=(finding,),
        )
        with self.assertRaises(InvalidReviewSessionTransition):
            registry.add_evaluation("review-1", report)

    def test_asset_attachment_binds_source_run_and_requires_valid_state(self) -> None:
        registry = ReviewSessionRegistry(source_runs={"run-1": _source()})
        registry.create(_request(persist_session=False), review_session_id="review-1")
        attached = registry.attach_asset(
            "review-1",
            _asset("asset-1", "candidate_full_mix", "a", "/private/one.wav"),
            validate=False,
        )
        self.assertEqual(attached.source_run_id, "run-1")
        with self.assertRaises(ReviewAudioAssetError):
            registry.attach_asset(
                "review-1",
                attached.model_copy(update={"validation_state": "unvalidated", "asset_id": "asset-2"}),
                validate=False,
            )
        with self.assertRaises(ReviewSessionError):
            registry.attach_asset(
                "review-1",
                attached.model_copy(update={"source_run_id": "run-2", "asset_id": "asset-3"}),
                validate=False,
            )

    def test_comparison_requires_known_attached_assets_even_when_session_is_empty(self) -> None:
        registry = ReviewSessionRegistry(source_runs={"run-1": _source()})
        registry.create(_request(persist_session=False), review_session_id="review-1")
        comparison = RevisionComparison(
            comparison_id="comparison-1",
            before_asset=_asset("before", "before_full_mix", "a", "/private/before.wav"),
            after_asset=_asset("after", "after_full_mix", "b", "/private/after.wav"),
            alignment_result=AlignmentResult(state="aligned"),
        )
        with self.assertRaises(ReviewSessionError):
            registry.add_comparison("review-1", comparison)

    def test_initial_feedback_changes_state_only_for_explicit_user_decisions(self) -> None:
        explicit = ReviewSessionRegistry(source_runs={"run-1": _source()}).create(
            _request(
                persist_session=False,
                user_feedback=(
                    CreationFeedback(
                        feedback_id="approval",
                        overall_verdict="user_approved",
                        approval_level="final",
                    ),
                ),
            ),
            review_session_id="review-explicit",
        )
        self.assertEqual(explicit.status, "accepted")
        self.assertIn("accepted", explicit.current_next_action.casefold())

        non_user = ReviewSessionRegistry(source_runs={"run-1": _source()}).create(
            _request(
                persist_session=False,
                user_feedback=(
                    CreationFeedback(
                        feedback_id="ai-approval",
                        source="connected_ai_interpretation",
                        overall_verdict="user_approved",
                        approval_level="final",
                    ),
                ),
            ),
            review_session_id="review-ai",
        )
        self.assertEqual(non_user.status, "created")
        self.assertIn("attach", non_user.current_next_action.casefold())

    def test_explicit_approval_and_rejection_feedback_work_from_opening_states(self) -> None:
        cases = (
            ("created", "user_approved", "final", "accepted"),
            ("awaiting_assets", "user_approved", "final", "accepted"),
            ("created", "user_rejected", "rejected", "rejected"),
            ("awaiting_assets", "user_rejected", "rejected", "rejected"),
        )
        for index, (opening, verdict, approval_level, expected) in enumerate(cases):
            with self.subTest(opening=opening, expected=expected):
                registry = ReviewSessionRegistry(source_runs={"run-1": _source()})
                session_id = f"review-feedback-{index}"
                registry.create(
                    _request(persist_session=False),
                    review_session_id=session_id,
                )
                if opening == "awaiting_assets":
                    registry.transition(session_id, "awaiting_assets")

                updated = registry.add_feedback(
                    session_id,
                    CreationFeedback(
                        feedback_id=f"feedback-{index}",
                        overall_verdict=verdict,  # type: ignore[arg-type]
                        approval_level=approval_level,  # type: ignore[arg-type]
                    ),
                )

                self.assertEqual(updated.status, expected)
                self.assertEqual(len(updated.feedback), 1)

    def test_revision_pass_receipt_can_be_recorded_locally_after_store_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.json"
            store = LocalReviewSessionStore(path)
            registry = ReviewSessionRegistry(
                store=store,
                source_runs={"run-1": _source()},
            )
            registry.create(_request(), review_session_id="review-recovery")
            finding = EvaluationFinding(
                finding_id="finding-recovery",
                category="low_end",
                evidence_source="decoded_audio_measurement",
                explanation="One.",
            )
            registry.transition("review-recovery", "evaluating")
            registry.add_evaluation(
                "review-recovery",
                CreationEvaluationReport(
                    evaluation_id="evaluation-recovery",
                    review_session_id="review-recovery",
                    source_run_id="run-1",
                    asset_set_digest="c" * 64,
                    section_map_digest="d" * 64,
                    analyzer_version="test",
                    findings=(finding,),
                ),
            )
            from fl_studio_mcp.creation_review.models import RevisionPlan

            registry.add_revision_plan(
                "review-recovery",
                RevisionPlan(
                    revision_plan_id="plan-recovery",
                    review_session_id="review-recovery",
                    source_evaluation_id="evaluation-recovery",
                    source_run_id="run-1",
                ),
            )
            registry.transition("review-recovery", "revising")
            revision_pass = RevisionPass(
                revision_pass_id="pass-recovery",
                review_session_id="review-recovery",
                source_evaluation_id="evaluation-recovery",
                revision_plan_id="plan-recovery",
                source_run_id="run-1",
                status="completed",
            )

            with patch.object(
                registry,
                "_persist",
                side_effect=ReviewSessionWriteError("simulated write failure"),
            ):
                with self.assertRaises(ReviewSessionWriteError):
                    registry.add_revision_pass("review-recovery", revision_pass)

            durable = store.get("review-recovery")
            assert durable is not None
            self.assertEqual(durable.revision_passes, ())
            with patch.object(registry, "_persist", side_effect=AssertionError("no retry")):
                recovered = registry.record_revision_pass_after_persistence_failure(
                    "review-recovery",
                    revision_pass,
                    persistence_error=ReviewSessionWriteError("simulated write failure"),
                )

            self.assertEqual(recovered.status, "blocked")
            self.assertEqual(recovered.revision_passes, (revision_pass,))
            self.assertFalse(recovered.revision_passes[0].automatic_replay_attempted)
            self.assertTrue(any("no replay" in item for item in recovered.blockers))
            self.assertEqual(registry.get("review-recovery"), recovered)

    def test_playlist_handoff_contract_exposes_exact_row_and_status(self) -> None:
        placement = PlaylistPlacement(
            handoff_item_id="item-1",
            pattern_number=4,
            pattern_name="PF Counterlead",
            source_operation_id="op-1",
            intended_playlist_track_number=5,
            intended_playlist_track_name="Counterlead",
            start_bar=25,
            end_bar=33,
            length_bars=8,
            layer_order=2,
            repeat_count=1,
            expected_mute_state=False,
            dependency="Drop A content",
            replacement_vs_addition="addition",
            completed_state=False,
            user_confirmed_state=False,
            notes="Add without moving existing clips.",
        )
        handoff = PlaylistHandoff(
            handoff_id="handoff-1",
            placements=(placement,),
            status="one_action_required",
        )
        self.assertEqual(placement.playlist_track, 5)
        self.assertEqual(placement.completed_state, "not_started")
        self.assertEqual(handoff.status, "one_action_required")


if __name__ == "__main__":
    unittest.main(verbosity=2)
