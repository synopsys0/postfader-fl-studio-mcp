"""Public Creation Review workflow and MCP registration tests."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fl_studio_mcp.creation_review.mcp as review_api
from fl_studio_mcp.creation_review.mcp import (
    ReviewAssetInput,
    ReviewAttachAssetsRequest,
    ReviewCompareRequest,
    ReviewEvaluateRequest,
)
from fl_studio_mcp.creation_review.models import (
    AcceptedElementLock,
    CreatePlaylistHandoffDeltaOperation,
    CreationEvaluationReport,
    CreationFeedback,
    FrozenMap,
    PlaylistPlacement,
    RecordFeedbackLockOperation,
    ReviewGeneratedOutput,
    ReviewReferenceSectionPair,
    ReviewReferenceSectionWindow,
    ReviewSectionRangeInput,
    ReviewSessionRequest,
    RevisionOperationReceipt,
    RevisionPass,
    RevisionPlan,
    RevisionRequest,
)
from fl_studio_mcp.creation_review.persistence import (
    LocalReviewSessionStore,
    ReviewSessionWriteError,
)
from fl_studio_mcp.creation_review.revision_executor import RevisionExecutor
from fl_studio_mcp.creation_review.revision_planner import revision_request_digest
from fl_studio_mcp.creation_review.sessions import ReviewSessionError, ReviewSessionRegistry
from fl_studio_mcp.mcp_server import mcp
from fl_studio_mcp.production_runs import ProductionRunSnapshot


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "creation_review"


def _source() -> dict[str, object]:
    return {
        "state": {
            "run_id": "run-1",
            "status": "completed",
            "session_fingerprint": "a" * 32,
            "request": {
                "brief": "Create a four-section playable draft.",
                "completion_target": "playable draft",
                "preserve": {"note_content": False},
            },
            "generated_outputs": (),
            "receipts": (),
        },
        "plan": {"operations": ()},
    }


def _evaluation(review_session_id: str) -> CreationEvaluationReport:
    return CreationEvaluationReport(
        evaluation_id="evaluation-1",
        review_session_id=review_session_id,
        source_run_id="run-1",
        asset_set_digest="b" * 64,
        section_map_digest="c" * 64,
        analyzer_version="test",
    )


class CreationReviewMCPTests(unittest.TestCase):
    def _registry(self) -> ReviewSessionRegistry:
        return ReviewSessionRegistry(source_runs={"run-1": _source()})

    def _evaluated_session(
        self,
        registry: ReviewSessionRegistry,
        *,
        review_session_id: str = "review-1",
        interaction_policy: str = "analyze_and_plan",
        authorized_to_modify: bool = False,
    ) -> object:
        registry.create(
            ReviewSessionRequest(
                source_run_id="run-1",
                brief="Review this draft.",
                interaction_policy=interaction_policy,  # type: ignore[arg-type]
                authorized_to_modify=authorized_to_modify,
            ),
            review_session_id=review_session_id,
        )
        registry.transition(review_session_id, "evaluating")
        registry.add_evaluation(review_session_id, _evaluation(review_session_id))
        return registry.get(review_session_id)

    @staticmethod
    def _apply_request(
        *,
        review_session_id: str,
        authorized_to_modify: bool,
        revision_authorized_to_modify: bool | None = None,
    ) -> review_api.ReviewApplyRevisionRequest:
        if revision_authorized_to_modify is None:
            revision_authorized_to_modify = authorized_to_modify
        return review_api.ReviewApplyRevisionRequest(
            review_session_id=review_session_id,
            revision_plan_id="revision-1",
            request=RevisionRequest(
                source_evaluation_id="evaluation-1",
                source_run_id="run-1",
                requested_objective="Apply the bounded revision.",
                allowed_changes=("record_feedback_lock",),
                authorized_to_modify=revision_authorized_to_modify,
            ),
            authorized_to_modify=authorized_to_modify,
        )

    def test_review_tools_are_registered_with_honest_annotations(self) -> None:
        tools = {item.name: item for item in asyncio.run(mcp.list_tools())}
        self.assertEqual(len(tools), 127)
        read_only = {
            "postfader_review_start",
            "postfader_review_attach_assets",
            "postfader_review_evaluate",
            "postfader_review_get",
            "postfader_review_compare",
            "postfader_review_plan_revision",
            "postfader_delivery_manifest",
            "postfader_review_export_handoff",
        }
        workflow = {
            "postfader_review_record_feedback",
            "postfader_review_stop",
        }
        destructive = {
            "postfader_review_apply_revision",
            "postfader_review_delete",
            "postfader_delivery_export_manifest",
        }
        self.assertTrue(read_only | workflow | destructive <= tools.keys())
        for name in read_only:
            annotation = tools[name].annotations
            self.assertTrue(annotation.read_only_hint, name)
            self.assertFalse(annotation.destructive_hint, name)
        for name in workflow:
            annotation = tools[name].annotations
            self.assertFalse(annotation.read_only_hint, name)
            self.assertFalse(annotation.destructive_hint, name)
        for name in destructive:
            annotation = tools[name].annotations
            self.assertFalse(annotation.read_only_hint, name)
            self.assertTrue(annotation.destructive_hint, name)
        # Existing v0.20 and Chunk 1 surfaces remain present.
        self.assertIn("postfader_execute_run", tools)
        self.assertIn("sound_selection_plan", tools)
        self.assertIn("plugins_atlas_search", tools)

    def test_start_attach_evaluate_is_read_only_and_retains_initial_feedback(self) -> None:
        registry = self._registry()
        reference_pair = ReviewReferenceSectionPair(
            reference=ReviewReferenceSectionWindow(
                section_id="reference-drop",
                start_seconds=8.0,
                end_seconds=12.0,
            ),
            candidate=ReviewReferenceSectionWindow(
                section_id="drop",
                start_seconds=4.0,
                end_seconds=8.0,
            ),
        )
        feedback = CreationFeedback(
            feedback_id="feedback-initial",
            overall_note="The bass should be clearer.",
        )
        request = ReviewSessionRequest(
            source_run_id="run-1",
            brief="Analyze this bounce only.",
            interaction_policy="analyze_only",
            user_feedback=(feedback,),
            reference_section_pairs=(reference_pair,),
        )
        with patch.object(review_api, "REVIEW_SESSIONS", registry):
            session = review_api.review_start(request)
            self.assertEqual(session.feedback, (feedback,))
            session = review_api.review_attach_assets(
                ReviewAttachAssetsRequest(
                    review_session_id=session.review_session_id,
                    assets=(
                        ReviewAssetInput(
                            path=str((FIXTURE_ROOT / "clean_baseline.wav").resolve()),
                            asset_kind="candidate_full_mix",
                            display_label="Synthetic clean baseline",
                        ),
                    ),
                )
            )
            with patch.object(
                review_api,
                "evaluate_creation",
                wraps=review_api.evaluate_creation,
            ) as evaluator:
                report = review_api.review_evaluate(
                    ReviewEvaluateRequest(
                        review_session_id=session.review_session_id,
                        section_ranges=(
                            ReviewSectionRangeInput(
                                section_id="intro",
                                name="Intro",
                                start_bar=1,
                                end_bar=3,
                            ),
                        ),
                    )
                )
            self.assertEqual(
                evaluator.call_args.kwargs["reference_section_pairs"],
                (reference_pair,),
            )
            current = registry.get(session.review_session_id)
        assert current is not None
        self.assertEqual(current.status, "evaluated")
        self.assertEqual(len(current.assets), 1)
        self.assertEqual(len(current.evaluations), 1)
        assert current.section_map is not None
        self.assertEqual(current.section_map.sections[0].start_seconds, 0.0)
        self.assertEqual(current.section_map.sections[0].end_seconds, 4.0)
        self.assertFalse(report.mutations_applied)
        self.assertTrue(report.zero_mutations)
        self.assertTrue(
            all(item.evidence_source in {
                "decoded_audio_measurement",
                "explicit_user_feedback",
                "production_run_receipt",
                "sound_palette_metadata",
                "synchronized_stem_measurement",
                "reference_comparison",
                "connected_ai_interpretation",
            } for item in report.findings)
        )

    def test_mcp_compare_requires_before_and_after_full_mix_kinds(self) -> None:
        registry = self._registry()
        with patch.object(review_api, "REVIEW_SESSIONS", registry):
            session = self._evaluated_session(registry)
            registry.add_revision_plan(
                session.review_session_id,
                RevisionPlan(
                    revision_plan_id="revision-1",
                    review_session_id=session.review_session_id,
                    source_evaluation_id="evaluation-1",
                    source_run_id="run-1",
                ),
            )
            registry.transition(session.review_session_id, "revising")
            registry.add_revision_pass(
                session.review_session_id,
                RevisionPass(
                    revision_pass_id="pass-1",
                    review_session_id=session.review_session_id,
                    source_evaluation_id="evaluation-1",
                    revision_plan_id="revision-1",
                    source_run_id="run-1",
                    status="awaiting_rebounce",
                    after_bounce_state="awaiting",
                ),
            )
            review_api.review_attach_assets(
                ReviewAttachAssetsRequest(
                    review_session_id=session.review_session_id,
                    assets=(
                        ReviewAssetInput(
                            path=str((FIXTURE_ROOT / "clean_baseline.wav").resolve()),
                            asset_id="candidate",
                            asset_kind="candidate_full_mix",
                        ),
                        ReviewAssetInput(
                            path=str(
                                (FIXTURE_ROOT / "improved_contrast_with_regression.wav").resolve()
                            ),
                            asset_id="after",
                            asset_kind="after_full_mix",
                            revision_pass_id="pass-1",
                        ),
                    ),
                )
            )
            with self.assertRaisesRegex(ValueError, "before_full_mix"):
                review_api.review_compare(
                    ReviewCompareRequest(
                        review_session_id=session.review_session_id,
                        before_asset_id="candidate",
                        after_asset_id="after",
                    )
                )

    def test_mcp_compare_binds_after_asset_to_selected_revision_pass(self) -> None:
        registry = self._registry()
        with patch.object(review_api, "REVIEW_SESSIONS", registry):
            session = self._evaluated_session(registry)
            registry.add_revision_plan(
                session.review_session_id,
                RevisionPlan(
                    revision_plan_id="revision-1",
                    review_session_id=session.review_session_id,
                    source_evaluation_id="evaluation-1",
                    source_run_id="run-1",
                    operations=(RecordFeedbackLockOperation(operation_id="lock-record"),),
                ),
            )
            registry.transition(session.review_session_id, "revising")
            registry.add_revision_pass(
                session.review_session_id,
                RevisionPass(
                    revision_pass_id="pass-1",
                    review_session_id=session.review_session_id,
                    source_evaluation_id="evaluation-1",
                    revision_plan_id="revision-1",
                    source_run_id="run-1",
                    status="awaiting_rebounce",
                ),
            )
            with self.assertRaisesRegex(ReviewSessionError, "unknown revision pass"):
                review_api.review_attach_assets(
                    ReviewAttachAssetsRequest(
                        review_session_id=session.review_session_id,
                        assets=(
                            ReviewAssetInput(
                                path=str((FIXTURE_ROOT / "clean_baseline.wav").resolve()),
                                asset_id="before",
                                asset_kind="before_full_mix",
                            ),
                            ReviewAssetInput(
                                path=str(
                                    (FIXTURE_ROOT / "improved_contrast_with_regression.wav").resolve()
                                ),
                                asset_id="after",
                                asset_kind="after_full_mix",
                                revision_pass_id="pass-other",
                            ),
                        ),
                    )
                )

    def test_export_handoff_uses_offset_section_timestamps(self) -> None:
        registry = self._registry()
        with patch.object(review_api, "REVIEW_SESSIONS", registry):
            session = review_api.review_start(
                ReviewSessionRequest(
                    source_run_id="run-1",
                    brief="Review an offset export.",
                )
            )
            review_api.review_attach_assets(
                ReviewAttachAssetsRequest(
                    review_session_id=session.review_session_id,
                    assets=(
                        ReviewAssetInput(
                            path=str((FIXTURE_ROOT / "clean_baseline.wav").resolve()),
                            asset_id="candidate",
                            asset_kind="candidate_full_mix",
                        ),
                    ),
                )
            )
            review_api.review_evaluate(
                ReviewEvaluateRequest(
                    review_session_id=session.review_session_id,
                    section_ranges=(
                        ReviewSectionRangeInput(
                            section_id="intro",
                            name="Intro",
                            start_bar=1,
                            end_bar=3,
                        ),
                    ),
                    export_offset_seconds=2.5,
                )
            )
            handoff = review_api.review_export_handoff(session.review_session_id)

        self.assertEqual(handoff.exact_start_seconds, 2.5)
        self.assertEqual(handoff.exact_end_seconds, 6.5)

    def test_explicit_section_input_rejects_incomplete_boundaries(self) -> None:
        with self.assertRaisesRegex(ValueError, "both start_bar and end_bar"):
            ReviewSectionRangeInput(section_id="incomplete", start_bar=1)

    def test_non_user_sources_cannot_grant_artistic_approval(self) -> None:
        registry = self._registry()
        with patch.object(review_api, "REVIEW_SESSIONS", registry):
            self._evaluated_session(registry)
            review_api.review_record_feedback(
                CreationFeedback(
                    feedback_id="ai-approval",
                    review_session_id="review-1",
                    source="connected_ai_interpretation",
                    overall_verdict="user_approved",
                    approval_level="final",
                )
            )
            review_api.review_record_feedback(
                CreationFeedback(
                    feedback_id="metric-approval",
                    review_session_id="review-1",
                    source="bounce_measurement",
                    overall_verdict="approved",
                    approval_level="final",
                )
            )
            current = registry.get("review-1")
            manifest = review_api.delivery_manifest("review-1")
        assert current is not None
        self.assertEqual(current.status, "evaluated")
        self.assertEqual(manifest.final_user_approval, "pending")

    def test_draft_confirmation_and_revision_request_are_not_final_approval(self) -> None:
        registry = self._registry()
        with patch.object(review_api, "REVIEW_SESSIONS", registry):
            self._evaluated_session(registry)
            review_api.review_record_feedback(
                CreationFeedback(
                    feedback_id="draft-confirmed",
                    review_session_id="review-1",
                    source="user_explicit",
                    overall_verdict="user_confirmed_draft",
                    approval_level="draft",
                )
            )
            draft_manifest = review_api.delivery_manifest("review-1")
            review_api.review_record_feedback(
                CreationFeedback(
                    feedback_id="needs-revision",
                    review_session_id="review-1",
                    source="user_explicit",
                    overall_verdict="needs_revision",
                )
            )
            revision_manifest = review_api.delivery_manifest("review-1")
        self.assertEqual(draft_manifest.final_user_approval, "user_confirmed_draft")
        self.assertEqual(revision_manifest.final_user_approval, "needs_revision")

    def test_read_only_review_policies_reject_apply_without_executor_calls(self) -> None:
        for policy in ("analyze_only", "analyze_and_plan"):
            registry = self._registry()
            with patch.object(review_api, "REVIEW_SESSIONS", registry):
                session = registry.create(
                    ReviewSessionRequest(
                        source_run_id="run-1",
                        brief=f"Read-only {policy} review.",
                        interaction_policy=policy,  # type: ignore[arg-type]
                    ),
                    review_session_id=f"review-{policy}",
                )
                request = self._apply_request(
                    review_session_id=session.review_session_id,
                    authorized_to_modify=False,
                )
                with patch.object(
                    RevisionExecutor,
                    "apply",
                    side_effect=AssertionError("read-only review dispatched an executor"),
                ) as executor:
                    with self.assertRaisesRegex(ValueError, "not authorized"):
                        review_api.review_apply_revision(request)
                executor.assert_not_called()

    def test_later_task_scoped_authorization_can_reuse_a_read_only_session(self) -> None:
        registry = self._registry()
        with patch.object(review_api, "REVIEW_SESSIONS", registry):
            self._evaluated_session(
                registry,
                review_session_id="review-authorization",
                interaction_policy="analyze_only",
                authorized_to_modify=False,
            )
            request = self._apply_request(
                review_session_id="review-authorization",
                authorized_to_modify=True,
                revision_authorized_to_modify=True,
            )
            registry.add_revision_plan(
                "review-authorization",
                RevisionPlan(
                    revision_plan_id="revision-1",
                    review_session_id="review-authorization",
                    source_evaluation_id="evaluation-1",
                    source_run_id="run-1",
                    revision_request_digest=revision_request_digest(request.request),
                    operations=(
                        RecordFeedbackLockOperation(operation_id="lock-record"),
                    ),
                ),
            )
            revision_pass = RevisionPass(
                revision_pass_id="pass-later-authorization",
                review_session_id="review-authorization",
                source_evaluation_id="evaluation-1",
                revision_plan_id="revision-1",
                source_run_id="run-1",
                status="awaiting_rebounce",
            )
            with patch.object(
                RevisionExecutor,
                "apply",
                return_value=revision_pass,
            ) as executor:
                result = review_api.review_apply_revision(request)
            executor.assert_called_once()
            self.assertEqual(result.revision_pass_id, "pass-later-authorization")

    def test_apply_rejects_request_that_differs_from_validated_plan(self) -> None:
        registry = self._registry()
        with patch.object(review_api, "REVIEW_SESSIONS", registry):
            self._evaluated_session(registry)
            planned_request = self._apply_request(
                review_session_id="review-1",
                authorized_to_modify=True,
            )
            registry.add_revision_plan(
                "review-1",
                RevisionPlan(
                    revision_plan_id="revision-1",
                    review_session_id="review-1",
                    source_evaluation_id="evaluation-1",
                    source_run_id="run-1",
                    revision_request_digest=revision_request_digest(
                        planned_request.request
                    ),
                    operations=(
                        RecordFeedbackLockOperation(operation_id="lock-record"),
                    ),
                ),
            )
            changed = planned_request.model_copy(
                update={
                    "request": planned_request.request.model_copy(
                        update={"requested_objective": "A different revision."}
                    )
                }
            )
            with patch.object(
                RevisionExecutor,
                "apply",
                side_effect=AssertionError("mismatched request reached mutation"),
            ) as executor, self.assertRaisesRegex(ValueError, "no longer matches"):
                review_api.review_apply_revision(changed)
            executor.assert_not_called()

    def test_revision_receipt_remains_visible_when_persistence_fails_after_execution(self) -> None:
        registry = self._registry()
        with patch.object(review_api, "REVIEW_SESSIONS", registry):
            self._evaluated_session(registry)
            apply_request = self._apply_request(
                review_session_id="review-1",
                authorized_to_modify=True,
            )
            registry.add_revision_plan(
                "review-1",
                RevisionPlan(
                    revision_plan_id="revision-1",
                    review_session_id="review-1",
                    source_evaluation_id="evaluation-1",
                    source_run_id="run-1",
                    revision_request_digest=revision_request_digest(
                        apply_request.request
                    ),
                    operations=(
                        RecordFeedbackLockOperation(operation_id="lock-record"),
                    ),
                ),
            )
            executed = RevisionPass(
                revision_pass_id="pass-store-failure",
                review_session_id="review-1",
                source_evaluation_id="evaluation-1",
                revision_plan_id="revision-1",
                source_run_id="run-1",
                status="completed",
                operation_receipts=(
                    RevisionOperationReceipt(
                        operation_id="lock-record",
                        operation="record_feedback_lock",
                        status="verified",
                        mutating=False,
                        outcome_known=True,
                        verified=True,
                    ),
                ),
            )
            with patch.object(
                RevisionExecutor,
                "apply",
                return_value=executed,
            ) as executor, patch.object(
                registry,
                "add_revision_pass",
                side_effect=ReviewSessionWriteError("injected store failure"),
            ):
                result = review_api.review_apply_revision(apply_request)
            executor.assert_called_once()
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.operation_receipts[0].status, "verified")
            self.assertFalse(result.automatic_replay_attempted)
            current = registry.get("review-1")
            assert current is not None
            self.assertEqual(current.status, "blocked")
            self.assertEqual(
                current.revision_passes[-1].revision_pass_id,
                "pass-store-failure",
            )

    def test_explicit_feedback_locks_cannot_be_omitted_or_conflicted(self) -> None:
        registry = self._registry()
        lock = AcceptedElementLock(
            lock_id="lock-lead",
            scope="role",
            role_id="main_lead",
            lock_types=("note_content",),
            directive="Keep the accepted lead notes.",
        )
        with patch.object(review_api, "REVIEW_SESSIONS", registry):
            self._evaluated_session(registry)
            review_api.review_record_feedback(
                CreationFeedback(
                    feedback_id="feedback-lock",
                    review_session_id="review-1",
                    source="user_explicit",
                    accepted_locks=(lock,),
                )
            )
            request = RevisionRequest(
                source_evaluation_id="evaluation-1",
                source_run_id="run-1",
                requested_objective="Keep the accepted lead notes.",
                allowed_changes=("record_feedback_lock",),
            )
            plan = review_api.review_plan_revision(
                review_api.ReviewPlanRevisionRequest(
                    review_session_id="review-1",
                    request=request,
                    operations=(RecordFeedbackLockOperation(operation_id="lock-record"),),
                    revision_plan_id="revision-1",
                )
            )
            self.assertEqual(
                tuple(item.lock_id for item in plan.protected_elements),
                ("lock-lead",),
            )
            conflicting = request.model_copy(
                update={
                    "accepted_element_locks": (
                        lock.model_copy(update={"directive": "Change the accepted lead notes."}),
                    )
                }
            )
            with self.assertRaisesRegex(ValueError, "conflicts with explicit"):
                review_api.review_plan_revision(
                    review_api.ReviewPlanRevisionRequest(
                        review_session_id="review-1",
                        request=conflicting,
                        operations=(
                            RecordFeedbackLockOperation(operation_id="lock-record-2"),
                        ),
                        revision_plan_id="revision-conflict",
                    )
                )

    def test_persisted_source_snapshot_supports_apply_after_source_run_eviction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review-sessions.json"
            store = LocalReviewSessionStore(path)
            seeded = ReviewSessionRegistry(
                store=store,
                source_runs={"run-1": _source()},
            )
            with patch.object(review_api, "REVIEW_SESSIONS", seeded):
                session = review_api.review_start(
                    ReviewSessionRequest(
                        source_run_id="run-1",
                        brief="Persist this revision session.",
                        interaction_policy="iterate_on_new_bounces",
                        authorized_to_modify=True,
                        persist_session=True,
                    )
                )
                seeded.transition(session.review_session_id, "evaluating")
                seeded.add_evaluation(
                    session.review_session_id,
                    _evaluation(session.review_session_id),
                )
                revision_request = RevisionRequest(
                    source_evaluation_id="evaluation-1",
                    source_run_id="run-1",
                    requested_objective="Record the persisted revision boundary.",
                    allowed_changes=("record_feedback_lock",),
                    authorized_to_modify=True,
                )
                seeded.add_revision_plan(
                    session.review_session_id,
                    RevisionPlan(
                        revision_plan_id="revision-1",
                        review_session_id=session.review_session_id,
                        source_evaluation_id="evaluation-1",
                        source_run_id="run-1",
                        revision_request_digest=revision_request_digest(
                            revision_request
                        ),
                        operations=(
                            RecordFeedbackLockOperation(operation_id="lock-record"),
                        ),
                    ),
                )

            restored = ReviewSessionRegistry(store=LocalReviewSessionStore(path))
            missing_source = ProductionRunSnapshot(
                found=False,
                message="source run was evicted from the process-local registry",
            )
            fake_pass = RevisionPass(
                revision_pass_id="pass-1",
                review_session_id=session.review_session_id,
                source_evaluation_id="evaluation-1",
                revision_plan_id="revision-1",
                source_run_id="run-1",
                status="awaiting_rebounce",
            )
            with patch.object(review_api, "REVIEW_SESSIONS", restored), patch.object(
                review_api, "PRODUCTION_RUNS"
            ) as production_runs, patch.object(
                RevisionExecutor, "apply", return_value=fake_pass
            ) as executor:
                production_runs.snapshot.return_value = missing_source
                result = review_api.review_apply_revision(
                    review_api.ReviewApplyRevisionRequest(
                        review_session_id=session.review_session_id,
                        revision_plan_id="revision-1",
                        request=revision_request,
                        authorized_to_modify=True,
                        expected_session_fingerprint="a" * 32,
                    )
                )
            self.assertEqual(result.revision_pass_id, "pass-1")
            executor.assert_called_once()
            context = executor.call_args.args[-1]
            self.assertEqual(context["session_fingerprint"], "a" * 32)
            self.assertEqual(context["source_run_id"], "run-1")

    def test_missing_session_result_explains_process_local_lifetime(self) -> None:
        registry = self._registry()
        with patch.object(review_api, "REVIEW_SESSIONS", registry):
            result = review_api.review_get("review-missing")
        self.assertFalse(result.found)
        self.assertIn("previous MCP process", result.message)

    def test_delivery_requires_explicit_artistic_approval(self) -> None:
        registry = self._registry()
        with patch.object(review_api, "REVIEW_SESSIONS", registry):
            session = review_api.review_start(
                ReviewSessionRequest(
                    source_run_id="run-1",
                    brief="Prepare delivery state.",
                )
            )
            registry.transition(session.review_session_id, "evaluating")
            registry.transition(session.review_session_id, "evaluated")
            registry.transition(session.review_session_id, "completed")
            manifest = review_api.delivery_manifest(session.review_session_id)
        self.assertEqual(manifest.final_user_approval, "pending")

    def test_delivery_export_cleans_new_artifacts_when_session_record_fails(self) -> None:
        registry = self._registry()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            review_api,
            "REVIEW_SESSIONS",
            registry,
        ):
            self._evaluated_session(registry)
            with patch.object(
                registry,
                "set_delivery_manifest",
                side_effect=ReviewSessionWriteError("injected delivery-store failure"),
            ), self.assertRaisesRegex(
                ReviewSessionWriteError,
                "injected delivery-store failure",
            ):
                review_api.delivery_export_manifest(
                    review_api.ReviewDeliveryExportRequest(
                        review_session_id="review-1",
                        output_directory=directory,
                    )
                )
            self.assertEqual(tuple(Path(directory).iterdir()), ())

        registry = self._registry()
        with patch.object(review_api, "REVIEW_SESSIONS", registry):
            session = review_api.review_start(
                ReviewSessionRequest(
                    source_run_id="run-1",
                    brief="Prepare approved delivery state.",
                )
            )
            registry.transition(session.review_session_id, "evaluating")
            registry.transition(session.review_session_id, "evaluated")
            review_api.review_record_feedback(
                CreationFeedback(
                    feedback_id="approval-1",
                    review_session_id=session.review_session_id,
                    source="user_explicit",
                    overall_verdict="user_approved",
                    approval_level="final",
                )
            )
            manifest = review_api.delivery_manifest(session.review_session_id)
        self.assertEqual(manifest.final_user_approval, "user_approved")

    def test_delivery_uses_latest_revision_outputs_and_run_checkpoint(self) -> None:
        registry = self._registry()
        with patch.object(review_api, "REVIEW_SESSIONS", registry):
            self._evaluated_session(registry)
            plan = RevisionPlan(
                revision_plan_id="revision-1",
                review_session_id="review-1",
                source_evaluation_id="evaluation-1",
                source_run_id="run-1",
                operations=(
                    RecordFeedbackLockOperation(operation_id="record-lock"),
                ),
            )
            registry.add_revision_plan("review-1", plan)
            registry.transition("review-1", "revising")
            revision_pass = RevisionPass(
                revision_pass_id="pass-1",
                review_session_id="review-1",
                source_evaluation_id="evaluation-1",
                revision_plan_id="revision-1",
                source_run_id="run-1",
                continuation_run_id="revision-run-1",
                status="awaiting_rebounce",
                generated_outputs=(
                    ReviewGeneratedOutput(
                        output_id="lead-notes-v2",
                        output_kind="note_sequence",
                        role_id="main_lead",
                        digest="d" * 64,
                    ),
                    ReviewGeneratedOutput(
                        output_id="lead-sound-v2",
                        output_kind="palette_assignment",
                        role_id="main_lead",
                        digest="e" * 64,
                        metadata=FrozenMap(
                            {
                                "role_id": "main_lead",
                                "product_name": "Synthetic Lead",
                                "preset_name": "Revision Bright",
                            }
                        ),
                    ),
                ),
            )
            registry.add_revision_pass("review-1", revision_pass)
            manifest = review_api.delivery_manifest("review-1")
        self.assertEqual(manifest.final_revision_pass_id, "pass-1")
        self.assertEqual(manifest.final_run_id, "revision-run-1")
        self.assertEqual(manifest.evaluations[0].evaluation_id, "evaluation-1")
        self.assertEqual(
            {item.output_id for item in manifest.accepted_generated_outputs},
            {"lead-notes-v2", "lead-sound-v2"},
        )
        lead = next(
            item
            for item in manifest.accepted_role_assignments
            if getattr(item, "role_id", None) == "main_lead"
        )
        self.assertEqual(getattr(lead, "preset_name", None), "Revision Bright")

    def test_delivery_includes_only_the_exact_revision_playlist_delta(self) -> None:
        registry = self._registry()
        placement = PlaylistPlacement(
            pattern_number=7,
            pattern_name="PF Counterlead",
            section_id="drop-b",
            intended_playlist_track_number=5,
            start_bar=25,
            end_bar=33,
            replacement_vs_addition="addition",
        )
        with patch.object(review_api, "REVIEW_SESSIONS", registry):
            self._evaluated_session(registry)
            operation = CreatePlaylistHandoffDeltaOperation(
                operation_id="handoff-1",
                feedback_ids=("feedback-1",),
                section_id="drop-b",
                placements=(placement,),
            )
            plan = RevisionPlan(
                revision_plan_id="revision-handoff",
                review_session_id="review-1",
                source_evaluation_id="evaluation-1",
                source_run_id="run-1",
                operations=(operation,),
            )
            registry.add_revision_plan("review-1", plan)
            registry.transition("review-1", "revising")
            revision_pass = RevisionPass(
                revision_pass_id="pass-handoff",
                review_session_id="review-1",
                source_evaluation_id="evaluation-1",
                revision_plan_id="revision-handoff",
                source_run_id="run-1",
                status="awaiting_rebounce",
                generated_outputs=(
                    ReviewGeneratedOutput(
                        output_id="handoff-1",
                        output_kind="handoff",
                        section_id="drop-b",
                        metadata=FrozenMap(
                            {
                                "placements": (
                                    placement.model_dump(
                                        mode="json", exclude_none=False
                                    ),
                                )
                            }
                        ),
                    ),
                ),
            )
            registry.add_revision_pass("review-1", revision_pass)
            manifest = review_api.delivery_manifest("review-1")
        assert manifest.playlist_handoff is not None
        self.assertEqual(len(manifest.playlist_handoff.delta_from_source), 1)
        self.assertEqual(
            manifest.playlist_handoff.delta_from_source[0].pattern_name,
            "PF Counterlead",
        )
        self.assertEqual(manifest.pattern_placements[0].start_bar, 25)

    def test_explicit_rejection_updates_session_without_changing_source(self) -> None:
        registry = self._registry()
        with patch.object(review_api, "REVIEW_SESSIONS", registry):
            session = review_api.review_start(
                ReviewSessionRequest(source_run_id="run-1", brief="Review it.")
            )
            registry.transition(session.review_session_id, "evaluating")
            registry.transition(session.review_session_id, "evaluated")
            rejected = review_api.review_record_feedback(
                CreationFeedback(
                    feedback_id="reject-1",
                    review_session_id=session.review_session_id,
                    source="user_explicit",
                    overall_verdict="user_rejected",
                    approval_level="rejected",
                )
            )
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(rejected.source_run_id, "run-1")
        self.assertEqual(rejected.source_snapshot.source_run_status, "completed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
