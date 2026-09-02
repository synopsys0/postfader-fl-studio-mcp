"""Closed Production Run adapters for Creation Review."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import fl_studio_mcp.creation_review.mcp as review_api
from fl_studio_mcp import production_runs as runs
from fl_studio_mcp.creation_review.models import (
    AcceptedElementLock,
    CreationFeedback,
    DeliveryManifest,
    PlaylistHandoff,
    PlaylistPlacement,
    RecordFeedbackLockOperation,
    ReviewAudioAsset,
    ReviewReferenceSectionPair,
    ReviewReferenceSectionWindow,
    ReviewSectionRangeInput,
    ReviewSessionRequest,
    RevisionPass,
    RevisionPlan,
    RevisionRequest,
)
from fl_studio_mcp.creation_review.sessions import ReviewSessionRegistry


FIXTURE = (
    Path(__file__).parent / "fixtures" / "creation_review" / "clean_baseline.wav"
).resolve()


def _source() -> dict[str, object]:
    return {
        "state": {
            "run_id": "source-run",
            "status": "completed",
            "request": {
                "brief": "Create the draft.",
                "completion_target": "playable draft",
            },
            "sections": (
                {
                    "section_id": "drop-a",
                    "name": "Drop A",
                    "start_bar": 1,
                    "end_bar": 2,
                    "start_seconds": 0.0,
                    "end_seconds": 2.0,
                    "source": "production_run",
                },
            ),
            "generated_outputs": (),
            "receipts": (),
        },
        "plan": {"operations": ()},
    }


def _request(*, policy: runs.InteractionPolicy = "execute_once") -> runs.ProductionRunRequest:
    return runs.ProductionRunRequest(
        brief="Review the supplied bounce.",
        scope=runs.ProductionScope(
            kind="whole_project", description="Creation Review metadata only."
        ),
        allowed_changes=("review",),
        completion_target="A structured review and bounded revision plan.",
        interaction_policy=policy,
        max_operations=16,
        max_iterations=1,
        authorized_to_modify=True,
    )


class CreationReviewProductionRunTests(unittest.TestCase):
    def test_playlist_handoff_operation_returns_current_revision_delta(self) -> None:
        registry = ReviewSessionRegistry(source_runs={"source-run": _source()})
        session = registry.create(
            ReviewSessionRequest(
                source_run_id="source-run",
                brief="Review the current handoff.",
            ),
            review_session_id="review-1",
        )
        placement = PlaylistPlacement(
            pattern_number=7,
            pattern_name="PF Counterlead",
            section_id="drop-b",
            intended_playlist_track_number=5,
            start_bar=25,
            end_bar=33,
            replacement_vs_addition="addition",
        )
        delta = PlaylistHandoff(
            handoff_id="playlist-review-1",
            placements=(placement,),
            delta_from_source=(placement,),
            status="one_action_required",
        )
        manifest = DeliveryManifest(
            delivery_id="delivery-review-1",
            source_run_id="source-run",
            review_session_id="review-1",
            playlist_handoff=delta,
            next_action="Place the counterlead pattern.",
        )
        operation = runs.CreatePlaylistHandoffOperation(
            operation_id="handoff",
            review_session=session.review_session_id,
        )
        with mock.patch.object(
            review_api,
            "REVIEW_SESSIONS",
            registry,
        ), mock.patch.object(
            review_api,
            "delivery_manifest",
            return_value=manifest,
        ):
            result = runs._dispatch_operation(
                operation,
                session_fingerprint=None,
                outputs={},
            )
        self.assertIs(result, delta)
        self.assertEqual(result.delta_from_source, (placement,))

    def test_evaluation_operation_retains_explicit_ranges_and_reference_pairs(self) -> None:
        pair = ReviewReferenceSectionPair(
            reference=ReviewReferenceSectionWindow(
                section_id="ref-drop",
                start_seconds=12.0,
                end_seconds=16.0,
            ),
            candidate=ReviewReferenceSectionWindow(
                section_id="drop",
                start_seconds=8.0,
                end_seconds=12.0,
            ),
        )
        operation = runs.EvaluateCreationOperation(
            operation_id="evaluate",
            review_session="review-1",
            section_ranges=(
                ReviewSectionRangeInput(
                    section_id="drop",
                    start_bar=5,
                    end_bar=9,
                ),
            ),
            reference_section_pairs=(pair,),
        )
        self.assertEqual(operation.section_ranges[0].start_bar, 5)
        self.assertEqual(operation.reference_section_pairs, (pair,))

    def test_closed_review_plan_uses_backward_typed_references(self) -> None:
        lock = AcceptedElementLock(
            lock_id="lock-chords",
            scope="role",
            role_id="main_chords",
            lock_types=("sound_assignment", "note_content"),
            directive="Keep the accepted chord sound and notes.",
        )
        feedback = CreationFeedback(
            feedback_id="feedback-1",
            source="user_explicit",
            accepted_locks=(lock,),
        )
        revision_request = RevisionRequest(
            source_evaluation_id="resolved-from-reference",
            source_run_id="source-run",
            requested_objective="Retain the accepted chords.",
            section_scope=("intro",),
            allowed_changes=("record_feedback_lock",),
            authorized_to_modify=True,
        )
        plan = runs.ProductionRunPlan(
            plan_id="review-plan",
            operations=(
                runs.StartReviewSessionOperation(
                    operation_id="start",
                    request=ReviewSessionRequest(
                        source_run_id="source-run",
                        brief="Review the bounce.",
                        interaction_policy="analyze_and_plan",
                    ),
                ),
                runs.AttachReviewAssetsOperation(
                    operation_id="attach",
                    review_session=runs.OperationOutputReference(
                        operation_id="start", output="review_session"
                    ),
                    assets=(
                        ReviewAudioAsset(
                            asset_id="candidate",
                            asset_kind="candidate_full_mix",
                            path=str(FIXTURE),
                            display_label="Synthetic candidate",
                        ),
                    ),
                ),
                runs.EvaluateCreationOperation(
                    operation_id="evaluate",
                    review_session=runs.OperationOutputReference(
                        operation_id="attach", output="review_session"
                    ),
                ),
                runs.RecordCreationFeedbackOperation(
                    operation_id="feedback",
                    review_session=runs.OperationOutputReference(
                        operation_id="attach", output="review_session"
                    ),
                    feedback=feedback,
                ),
                runs.PlanCreationRevisionOperation(
                    operation_id="plan-revision",
                    review_session=runs.OperationOutputReference(
                        operation_id="feedback", output="review_session"
                    ),
                    request=revision_request,
                    revision_operations=(
                        RecordFeedbackLockOperation(
                            operation_id="retain-chords",
                            section_id="drop-a",
                        ),
                    ),
                    source_evaluation=runs.OperationOutputReference(
                        operation_id="evaluate", output="evaluation_report"
                    ),
                    accepted_lock_refs=(
                        runs.OperationOutputReference(
                            operation_id="feedback",
                            output="feedback_lock",
                            item_id="lock-chords",
                        ),
                    ),
                    section_refs=(
                        runs.OperationOutputReference(
                            operation_id="start",
                            output="section_definition",
                            item_id="drop-a",
                        ),
                    ),
                ),
            ),
        )
        validation = runs.validate_production_run(
            _request(), plan, inspect_live=False
        )
        self.assertTrue(validation.valid, validation.blockers)
        self.assertEqual(
            validation.resolved_operation_order,
            ("start", "attach", "evaluate", "feedback", "plan-revision"),
        )
        self.assertEqual(validation.expected_mutation_categories, ("review",))

        review_registry = ReviewSessionRegistry(
            source_runs={"source-run": _source()}
        )
        production_registry = runs.ProductionRunRegistry(max_runs=4)
        with mock.patch.object(
            review_api, "REVIEW_SESSIONS", review_registry
        ), mock.patch.object(
            runs.WriteModeManager,
            "set_write_mode",
            side_effect=AssertionError("outer review plan must not enable FL writes"),
        ):
            result = production_registry.execute(_request(), plan)
        self.assertEqual(result.status, "completed")
        lookup = production_registry.get(result.run_id)
        assert lookup.state is not None
        self.assertEqual(lookup.state.write_mode_enable_count, 0)
        output_types = {item.output for item in lookup.state.generated_outputs}
        self.assertTrue(
            {
                "review_session",
                "evaluation_report",
                "finding",
                "feedback_lock",
                "section_definition",
                "revision_plan",
            }
            <= output_types
        )
        revision_plan = next(
            item.value
            for item in lookup.state.generated_outputs
            if item.output == "revision_plan"
        )
        self.assertIsInstance(revision_plan, RevisionPlan)
        self.assertEqual(
            revision_plan.source_evaluation_id,
            next(
                item.value.evaluation_id
                for item in lookup.state.generated_outputs
                if item.output == "evaluation_report"
            ),
        )
        self.assertEqual(
            {item.lock_id for item in revision_plan.protected_elements},
            {"lock-chords"},
        )

    def test_review_reference_type_mismatch_fails_before_dispatch(self) -> None:
        plan = runs.ProductionRunPlan(
            plan_id="bad-review-ref",
            operations=(
                runs.GenerateMelodyOperation(operation_id="melody"),
                runs.EvaluateCreationOperation(
                    operation_id="evaluate",
                    review_session=runs.OperationOutputReference(
                        operation_id="melody", output="review_session"
                    ),
                ),
            ),
        )
        validation = runs.validate_production_run(
            runs.ProductionRunRequest(
                brief="Invalid review plan.",
                scope=runs.ProductionScope(
                    kind="whole_project", description="Invalid test scope."
                ),
                allowed_changes=("composition", "review"),
                completion_target="No mutation.",
                authorized_to_modify=True,
            ),
            plan,
            inspect_live=False,
        )
        self.assertFalse(validation.valid)
        self.assertIn(
            "incompatible_output_reference",
            {item.code for item in validation.blockers},
        )

    def test_review_output_bound_is_preflighted(self) -> None:
        session_reference = "review-existing"
        plan = runs.ProductionRunPlan(
            plan_id="too-many-evaluations",
            operations=(
                runs.EvaluateCreationOperation(
                    operation_id="evaluate-1",
                    review_session=session_reference,
                ),
                runs.EvaluateCreationOperation(
                    operation_id="evaluate-2",
                    review_session=session_reference,
                ),
            ),
        )
        validation = runs.validate_production_run(
            _request(), plan, inspect_live=False
        )
        self.assertFalse(validation.valid)
        self.assertIn(
            "generated_output_limit_exceeded",
            {item.code for item in validation.blockers},
        )

    def test_plan_only_review_never_dispatches_or_enables_writes(self) -> None:
        plan = runs.ProductionRunPlan(
            plan_id="plan-only-review",
            operations=(
                runs.RecordCreationFeedbackOperation(
                    operation_id="feedback",
                    review_session="review-existing",
                    feedback=CreationFeedback(
                        feedback_id="feedback-1",
                        review_session_id="review-existing",
                    ),
                ),
            ),
        )
        registry = runs.ProductionRunRegistry(max_runs=2)
        with mock.patch.object(
            runs, "_dispatch_operation", side_effect=AssertionError("dispatched")
        ), mock.patch.object(
            runs.WriteModeManager,
            "set_write_mode",
            side_effect=AssertionError("write mode changed"),
        ):
            result = registry.execute(_request(policy="plan_only"), plan)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.attempted_count, 0)
        self.assertEqual(result.write_mode_enable_count, 0)

    def test_outer_apply_adapter_leaves_write_boundary_to_revision_executor(self) -> None:
        revision_request = RevisionRequest(
            source_evaluation_id="evaluation-1",
            source_run_id="source-run",
            requested_objective="Apply one verified change.",
            allowed_changes=("record_feedback_lock",),
            authorized_to_modify=True,
        )
        revision_plan = RevisionPlan(
            revision_plan_id="revision-1",
            review_session_id="review-1",
            source_evaluation_id="evaluation-1",
            source_run_id="source-run",
            operations=(RecordFeedbackLockOperation(operation_id="lock"),),
        )
        operation = runs.ApplyCreationRevisionOperation(
            operation_id="apply",
            review_session="review-1",
            plan=revision_plan,
            request=revision_request,
            authorized_to_modify=True,
        )
        plan = runs.ProductionRunPlan(
            plan_id="outer-apply", operations=(operation,)
        )
        fake_pass = RevisionPass(
            revision_pass_id="pass-1",
            review_session_id="review-1",
            source_evaluation_id="evaluation-1",
            revision_plan_id="revision-1",
            source_run_id="source-run",
            status="awaiting_rebounce",
            authorization_count=1,
            write_mode_enable_count=1,
            write_mode_disable_count=1,
            shutdown_verified=True,
        )
        registry = runs.ProductionRunRegistry(max_runs=2)
        with mock.patch.object(
            runs, "_dispatch_operation", return_value=fake_pass
        ), mock.patch.object(
            runs.WriteModeManager,
            "set_write_mode",
            side_effect=AssertionError("outer run enabled write mode"),
        ):
            result = registry.execute(_request(), plan)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.write_mode_enable_count, 0)
        self.assertEqual(result.write_mode_disable_count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
