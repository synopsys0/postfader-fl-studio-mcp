"""Focused contracts for Creation Review feedback, planning, and execution."""

from __future__ import annotations

import unittest

from fl_studio_mcp import production_runs as runs
from fl_studio_mcp.creation_review.feedback import (
    build_feedback_locks,
    lock_covers,
    release_feedback_locks,
)
from fl_studio_mcp.creation_review.models import (
    ChangeSoundAssignmentOperation,
    CreatePlaylistHandoffDeltaOperation,
    CreationFeedback,
    FeedbackDirective,
    PlaylistPlacement,
    RecordFeedbackLockOperation,
    RevisionPlan,
    RevisionRequest,
    RoleFeedback,
    TransformGeneratedSequenceOperation,
)
from fl_studio_mcp.creation_review.revision_executor import (
    RevisionExecutionContext,
    RevisionExecutor,
)
from fl_studio_mcp.creation_review.revision_planner import (
    compile_revision_plan,
    validate_revision_plan,
)
from fl_studio_mcp.creative import CreativeNote, make_sequence
from fl_studio_mcp.track_b_contracts import ChannelGeneratorTarget


class CreationReviewRevisionTests(unittest.TestCase):
    def _sequence(self):
        return make_sequence(
            name="Stored lead",
            generator="test",
            notes=(
                CreativeNote(pitch=60, start_beats=0.0, duration_beats=1.0),
                CreativeNote(pitch=64, start_beats=1.0, duration_beats=1.0),
            ),
        )

    def _request(self, *, authorized: bool = True) -> RevisionRequest:
        return RevisionRequest(
            source_evaluation_id="evaluation-1",
            source_run_id="run-1",
            requested_objective="Improve the lead without changing its motif.",
            allowed_changes=("transform_generated_sequence",),
            authorized_to_modify=authorized,
        )

    def _operation(self) -> TransformGeneratedSequenceOperation:
        return TransformGeneratedSequenceOperation(
            operation_id="transform-1",
            finding_ids=("finding-1",),
            role_id="main_lead",
            source_sequence_digest="a" * 64,
            parameters={
                "channel_index": 1,
                "pattern_number": 1,
                "transform": "transpose",
                "semitones": 2,
            },
        )

    def test_exact_feedback_shorthand_creates_independent_locks(self) -> None:
        feedback = CreationFeedback(
            feedback_id="feedback-1",
            review_session_id="session-1",
            preserve_directives=("keep chords",),
        )
        locks = build_feedback_locks(feedback)
        self.assertEqual({item.role_id for item in locks}, {"main_chords"})
        self.assertEqual({kind for item in locks for kind in item.lock_types}, {"sound_assignment", "note_content"})
        self.assertTrue(all(lock_covers(item, kind=item.lock_types[0], role_id="main_chords") for item in locks))

    def test_role_booleans_and_explicit_release_do_not_unlock_other_kinds(self) -> None:
        feedback = CreationFeedback(
            feedback_id="feedback-1",
            review_session_id="session-1",
            role_feedback=(RoleFeedback(feedback_id="role-1", role_id="main_lead", keep_sound=True, keep_notes=True),),
        )
        locks = build_feedback_locks(feedback)
        released = release_feedback_locks(locks, feedback_id="feedback-2", target="role", role_id="main_lead", kinds=("note_content",))
        self.assertEqual({kind for item in released for kind in item.lock_types if not item.released}, {"sound_assignment"})
        self.assertEqual(sum(item.released for item in released), 1)

    def test_nested_directive_cannot_elevate_non_user_feedback(self) -> None:
        feedback = CreationFeedback(
            feedback_id="feedback-ai",
            review_session_id="session-1",
            source="connected_ai_interpretation",
            preserve_directives=(
                FeedbackDirective(
                    directive_id="directive-ai",
                    text="Keep the lead notes.",
                    target="role",
                    role_id="main_lead",
                    lock_kinds=("note_content",),
                    preserve=True,
                    # A nested payload cannot self-authorize as user feedback.
                    source="user_explicit",
                ),
            ),
        )

        locks = build_feedback_locks(feedback)

        self.assertEqual(len(locks), 1)
        self.assertEqual(locks[0].feedback_id, "feedback-ai")
        self.assertEqual(locks[0].source, "connected_ai_interpretation")
        self.assertFalse(locks[0].explicit)

    def test_parent_user_feedback_owns_nested_directive_provenance(self) -> None:
        feedback = CreationFeedback(
            feedback_id="feedback-user",
            source="user_explicit",
            preserve_directives=(
                FeedbackDirective(
                    directive_id="directive-user",
                    text="Keep the lead notes.",
                    target="role",
                    role_id="main_lead",
                    lock_kinds=("note_content",),
                    preserve=True,
                    source="connected_ai_interpretation",
                ),
            ),
        )

        locks = build_feedback_locks(feedback)

        self.assertEqual(locks[0].feedback_id, "feedback-user")
        self.assertEqual(locks[0].source, "user_explicit")
        self.assertTrue(locks[0].explicit)

    def test_releasing_lock_retains_grant_and_release_provenance(self) -> None:
        feedback = CreationFeedback(
            feedback_id="feedback-grant",
            source="user_explicit",
            preserve_directives=(
                FeedbackDirective(
                    directive_id="directive-grant",
                    text="Keep the lead notes.",
                    target="role",
                    role_id="main_lead",
                    lock_kinds=("note_content",),
                    preserve=True,
                ),
            ),
        )
        locks = build_feedback_locks(feedback)

        released = release_feedback_locks(
            locks,
            feedback_id="feedback-release",
            source="user_explicit",
            target="role",
            role_id="main_lead",
            kinds=("note_content",),
        )

        self.assertEqual(released[0].feedback_id, "feedback-grant")
        self.assertEqual(released[0].source, "user_explicit")
        self.assertTrue(released[0].released)
        self.assertEqual(released[0].released_by_feedback_id, "feedback-release")
        self.assertEqual(released[0].released_by_source, "user_explicit")

    def test_non_user_release_cannot_unlock_an_accepted_lock(self) -> None:
        feedback = CreationFeedback(
            feedback_id="feedback-grant",
            source="user_explicit",
            preserve_directives=("keep chords",),
        )
        locks = build_feedback_locks(feedback)

        unchanged = release_feedback_locks(
            locks,
            feedback_id="feedback-ai-release",
            source="connected_ai_interpretation",
            target="role",
            role_id="main_chords",
            kinds=("note_content",),
        )

        self.assertTrue(unchanged)
        self.assertTrue(all(not item.released for item in unchanged))

    def test_broad_prose_does_not_create_a_lock(self) -> None:
        feedback = CreationFeedback(feedback_id="feedback-1", overall_note="The whole song is approximately right.")
        self.assertEqual(build_feedback_locks(feedback), ())

    def test_planner_uses_canonical_operation_and_digest(self) -> None:
        plan = compile_revision_plan(self._request(), (self._operation(),), review_session_id="session-1")
        self.assertIsInstance(plan, RevisionPlan)
        self.assertEqual(plan.operations[0].operation, "transform_generated_sequence")
        self.assertEqual(len(plan.digest), 64)
        self.assertTrue(validate_revision_plan(plan, request=self._request()).valid)

    def test_unauthorized_executor_never_calls_run_executor(self) -> None:
        request = self._request(authorized=False)
        plan = RevisionPlan(
            revision_plan_id="plan-1",
            review_session_id="session-1",
            source_evaluation_id=request.source_evaluation_id,
            source_run_id=request.source_run_id,
            operations=(self._operation(),),
        )
        context = RevisionExecutionContext(
            review_session_id="session-1",
            source_run_id="run-1",
            source_evaluation_id="evaluation-1",
            sequence_digests={"main_lead": "a" * 64},
            sequences={"main_lead": self._sequence()},
        )
        calls: list[object] = []
        result = RevisionExecutor(run_executor=lambda *_args: calls.append(True)).apply(plan, request, context)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(calls, [])
        self.assertEqual(result.authorization_count, 0)

    def test_executor_has_one_preflight_and_delegates_one_production_plan(self) -> None:
        request = self._request()
        plan = RevisionPlan(
            revision_plan_id="plan-1",
            review_session_id="session-1",
            source_evaluation_id=request.source_evaluation_id,
            source_run_id=request.source_run_id,
            operations=(self._operation(),),
        )
        context = RevisionExecutionContext(
            review_session_id="session-1",
            source_run_id="run-1",
            source_evaluation_id="evaluation-1",
            sequence_digests={"main_lead": "a" * 64},
            sequences={"main_lead": self._sequence()},
            session_fingerprint="c" * 32,
            project_state_digest="d" * 64,
        )
        calls: list[tuple[object, object]] = []

        def execute(production_request: object, production_plan: object):
            calls.append((production_request, production_plan))
            return {
                "status": "completed",
                "run_id": "b" * 32,
                "write_mode_enable_count": 1,
                "write_mode_disable_count": 1,
                "write_mode_shutdown_verified": True,
                "generated_outputs": (
                    runs.ProductionGeneratedOutput(
                        operation_id="transform-1",
                        output="note_sequence",
                        value=self._sequence(),
                    ),
                ),
            }

        executor = RevisionExecutor(
            readiness_checker=lambda *_args: {"state": "ready", "preflight_count": 1},
            session_capturer=lambda *_args: {
                "session_fingerprint": "c" * 32,
                "project_state_digest": "d" * 64,
            },
            run_executor=execute,
        )
        result = executor.apply(plan, request, context)
        self.assertEqual(result.status, "awaiting_rebounce")
        self.assertEqual(result.after_bounce_state, "awaiting")
        self.assertEqual(len(calls), 1)
        production_request, production_plan = calls[0]
        self.assertEqual(production_request.expected_session_fingerprint, "c" * 32)
        self.assertEqual(production_request.expected_project_state_digest, "d" * 64)
        write = production_plan.operations[0]
        self.assertEqual(write.operation, "write_note_sequence")
        self.assertEqual(write.mode, "replace")
        self.assertEqual([note.pitch for note in write.sequence.notes], [62, 66])
        self.assertEqual(result.authorization_count, 1)
        self.assertEqual(result.continuation_run_id, "b" * 32)
        self.assertEqual(result.generated_outputs[0].role_id, "main_lead")
        self.assertEqual(result.generated_outputs[0].digest, self._sequence().note_digest_sha256)
        self.assertEqual(
            result.generated_outputs[0].metadata["note_digest_sha256"],
            self._sequence().note_digest_sha256,
        )

    def test_missing_stored_sequence_blocks_before_run_execution(self) -> None:
        request = self._request()
        plan = RevisionPlan(
            revision_plan_id="plan-missing-sequence",
            review_session_id="session-1",
            source_evaluation_id=request.source_evaluation_id,
            source_run_id=request.source_run_id,
            operations=(self._operation(),),
        )
        calls: list[object] = []
        result = RevisionExecutor(
            run_executor=lambda *_args: calls.append(True),
        ).apply(
            plan,
            request,
            RevisionExecutionContext(
                review_session_id="session-1",
                source_run_id="run-1",
                source_evaluation_id="evaluation-1",
                sequence_digests={"main_lead": "a" * 64},
            ),
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(calls, [])
        self.assertTrue(any("stored PostFader NoteSequence" in item for item in result.blockers))

    def test_palette_adapter_uses_canonical_selected_preset_fields(self) -> None:
        request = RevisionRequest(
            source_evaluation_id="evaluation-1",
            source_run_id="run-1",
            requested_objective="Change only the lead sound.",
            allowed_changes=("change_sound_assignment",),
            authorized_to_modify=True,
        )
        operation = ChangeSoundAssignmentOperation(
            operation_id="sound-1",
            feedback_ids=("feedback-1",),
            role_id="main_lead",
        )
        plan = RevisionPlan(
            revision_plan_id="plan-sound",
            review_session_id="session-1",
            source_evaluation_id="evaluation-1",
            source_run_id="run-1",
            operations=(operation,),
        )
        adapted = RevisionExecutor().adapt_operations(
            plan,
            request,
            RevisionExecutionContext(
                review_session_id="session-1",
                source_run_id="run-1",
                source_evaluation_id="evaluation-1",
                palette_assignments={
                    "main_lead": {
                        "role_id": "main_lead",
                        "target": ChannelGeneratorTarget(channel_index=2),
                        "selected_preset": "Bright Lead",
                        "selected_preset_index": 7,
                    }
                },
            ),
        )
        self.assertEqual(adapted.blockers, ())
        self.assertEqual(adapted.operations[0].preset_name, "Bright Lead")
        self.assertEqual(adapted.operations[0].preset_index, 7)

    def test_local_feedback_lock_does_not_require_write_authorization_or_run(self) -> None:
        request = RevisionRequest(
            source_evaluation_id="evaluation-1",
            source_run_id="run-1",
            requested_objective="Retain the accepted lead lock.",
            allowed_changes=("record_feedback_lock",),
            authorized_to_modify=False,
        )
        plan = RevisionPlan(
            revision_plan_id="plan-lock",
            review_session_id="session-1",
            source_evaluation_id=request.source_evaluation_id,
            source_run_id=request.source_run_id,
            operations=(RecordFeedbackLockOperation(operation_id="lock-1"),),
        )
        calls: list[object] = []
        result = RevisionExecutor(
            run_executor=lambda *_args: calls.append(True),
        ).apply(plan, request, RevisionExecutionContext(
            review_session_id="session-1",
            source_run_id="run-1",
            source_evaluation_id="evaluation-1",
        ))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.authorization_count, 0)
        self.assertEqual(calls, [])
        self.assertEqual(result.operation_receipts[0].status, "verified")
        self.assertFalse(result.operation_receipts[0].mutating)

    def test_playlist_delta_is_a_typed_local_receipt_with_exact_rows(self) -> None:
        request = RevisionRequest(
            source_evaluation_id="evaluation-1",
            source_run_id="run-1",
            requested_objective="Add the counterlead placement handoff.",
            allowed_changes=("create_playlist_handoff_delta",),
            authorized_to_modify=False,
        )
        placement = PlaylistPlacement(
            pattern_number=7,
            pattern_name="PF Counterlead",
            section_id="drop-b",
            intended_playlist_track_number=5,
            intended_playlist_track_name="Counterlead",
            start_bar=25,
            end_bar=33,
            replacement_vs_addition="addition",
        )
        operation = CreatePlaylistHandoffDeltaOperation(
            operation_id="handoff-1",
            feedback_ids=("feedback-1",),
            section_id="drop-b",
            placements=(placement,),
        )
        plan = RevisionPlan(
            revision_plan_id="plan-handoff",
            review_session_id="session-1",
            source_evaluation_id="evaluation-1",
            source_run_id="run-1",
            operations=(operation,),
        )
        calls: list[object] = []
        result = RevisionExecutor(
            run_executor=lambda *_args: calls.append(True),
        ).apply(
            plan,
            request,
            RevisionExecutionContext(
                review_session_id="session-1",
                source_run_id="run-1",
                source_evaluation_id="evaluation-1",
            ),
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(calls, [])
        self.assertEqual(result.authorization_count, 0)
        self.assertEqual(result.generated_outputs[0].output_kind, "handoff")
        self.assertEqual(
            result.generated_outputs[0].metadata["placements"][0]["pattern_name"],
            "PF Counterlead",
        )
        self.assertIn("Playlist track 5", result.manual_handoffs[0].evidence[0])

    def test_planner_rejects_completed_operations_and_session_caps(self) -> None:
        request = self._request()
        with self.assertRaises(ValueError):
            compile_revision_plan(
                request,
                (self._operation(),),
                review_session_id="session-1",
                completed_operation_ids=("transform-1",),
            )
        with self.assertRaises(ValueError):
            compile_revision_plan(
                request,
                (self._operation(),),
                review_session_id="session-1",
                completed_revision_passes=1,
                maximum_revision_passes=1,
            )
        with self.assertRaises(ValueError):
            compile_revision_plan(
                request,
                (self._operation(),),
                review_session_id="session-1",
                completed_revision_operations=1,
                maximum_revision_operations=1,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
