"""Hermetic tests for task-scoped Production Runs.

The tests keep the live bridge behind small patches for execution-boundary
cases.  Structural validation is exercised directly so malformed plans can be
proven to perform zero project mutations.
"""

from __future__ import annotations

import asyncio
import threading
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from fl_studio_mcp import production_runs as runs
from fl_studio_mcp.contracts import ConnectionInfo, ProjectSummary, TransportState
from fl_studio_mcp.creative import (
    CreativeNote,
    NoteSequence,
    PianoRollDispatch,
    PianoRollScriptRuntimeEvidence,
    compose_melody,
    make_sequence,
    note_digest,
)
from fl_studio_mcp.track_b_contracts import VerifiedPatternSelectionWrite


SESSION = "a" * 32
OTHER_SESSION = "b" * 32
PROJECT_STATE = "c" * 64
ALL_CHANGES = (
    "composition",
    "notes",
    "pattern_metadata",
    "arrangement",
    "automation",
    "mixer",
    "channels",
    "playlist_metadata",
    "tempo",
    "routing",
    "plugin_parameters",
)


def request(
    *,
    policy: runs.InteractionPolicy = "execute_once",
    allowed_changes: tuple[str, ...] = ALL_CHANGES,
    scope: runs.ProductionScope | None = None,
    preserve: runs.ProductionPreservation | None = None,
    authorized: bool = True,
    max_operations: int = 32,
    max_iterations: int = 4,
) -> runs.ProductionRunRequest:
    return runs.ProductionRunRequest(
        brief="Complete the requested production task.",
        scope=scope
        or runs.ProductionScope(
            kind="whole_project",
            description="The requested production scope.",
        ),
        preserve=preserve or runs.ProductionPreservation(),
        allowed_changes=allowed_changes,
        completion_target="A complete playable draft.",
        interaction_policy=policy,
        max_operations=max_operations,
        max_iterations=max_iterations,
        authorized_to_modify=authorized,
    )


def plan(
    *operations: runs.ProductionOperation, plan_id: str = "plan-1"
) -> runs.ProductionRunPlan:
    return runs.ProductionRunPlan(plan_id=plan_id, operations=operations)


def melody(operation_id: str = "melody") -> runs.GenerateMelodyOperation:
    return runs.GenerateMelodyOperation(operation_id=operation_id)


def chords(operation_id: str = "chords") -> runs.GenerateChordProgressionOperation:
    return runs.GenerateChordProgressionOperation(
        operation_id=operation_id,
        progression=("I", "V", "vi", "IV"),
    )


def select_pattern(
    operation_id: str = "select", pattern_number: int = 2
) -> runs.SelectPatternOperation:
    return runs.SelectPatternOperation(
        operation_id=operation_id,
        pattern_number=pattern_number,
    )


def sequence() -> NoteSequence:
    return compose_melody(root="C", collection="major", bars=1, seed=7)


def verified_selection(
    *, verified: bool = True, pattern_number: int = 2
) -> VerifiedPatternSelectionWrite:
    return VerifiedPatternSelectionWrite(
        applied_at=datetime.now(timezone.utc),
        requested_pattern_number=pattern_number,
        before_pattern_number=1,
        after_pattern_number=pattern_number if verified else 1,
        verified=verified,
        verification_summary=(
            "Pattern selection verified."
            if verified
            else "Pattern selection was not verified."
        ),
        session_fingerprint=SESSION,
        session_precondition_applied=True,
    )


class ProductionRunTests(unittest.TestCase):
    def setUp(self) -> None:
        checkpoint = mock.patch.object(
            runs,
            "_capture_project_state",
            return_value=(PROJECT_STATE, ""),
        )
        checkpoint.start()
        self.addCleanup(checkpoint.stop)

    def test_valid_multi_operation_plan_has_order_and_capabilities(self) -> None:
        production_plan = plan(
            chords("drop_chords"),
            runs.WriteNoteSequenceOperation(
                operation_id="write_drop_chords",
                sequence=runs.OperationOutputReference(
                    operation_id="drop_chords", output="note_sequence"
                ),
                channel_index=2,
                pattern_number=4,
            ),
        )

        validation = runs.validate_production_run(
            request(allowed_changes=("composition", "notes")),
            production_plan,
            inspect_live=False,
        )

        self.assertTrue(validation.valid)
        self.assertEqual(
            validation.resolved_operation_order,
            ("drop_chords", "write_drop_chords"),
        )
        self.assertEqual(
            validation.plan_digest, runs.production_plan_digest(production_plan)
        )
        self.assertIn("host_side_creative_generation", validation.required_capabilities)
        self.assertIn(
            "armed_piano_roll_script_bridge", validation.required_capabilities
        )
        self.assertEqual(validation.blockers, ())

    def test_duplicate_operation_ids_are_rejected_structurally(self) -> None:
        validation = runs.validate_production_run(
            request(allowed_changes=("composition",)),
            plan(melody("same"), chords("same")),
            inspect_live=False,
        )

        self.assertFalse(validation.valid)
        self.assertIn(
            "duplicate_operation_id", {item.code for item in validation.blockers}
        )

    def test_missing_operation_reference_is_rejected(self) -> None:
        validation = runs.validate_production_run(
            request(allowed_changes=("notes",)),
            plan(
                runs.WriteNoteSequenceOperation(
                    operation_id="write",
                    sequence=runs.OperationOutputReference(
                        operation_id="missing", output="note_sequence"
                    ),
                    channel_index=1,
                    pattern_number=2,
                )
            ),
            inspect_live=False,
        )

        self.assertFalse(validation.valid)
        self.assertIn(
            "missing_operation_reference", {item.code for item in validation.blockers}
        )

    def test_future_operation_reference_is_rejected(self) -> None:
        validation = runs.validate_production_run(
            request(allowed_changes=("notes", "composition")),
            plan(
                runs.WriteNoteSequenceOperation(
                    operation_id="write",
                    sequence=runs.OperationOutputReference(
                        operation_id="future", output="note_sequence"
                    ),
                    channel_index=1,
                    pattern_number=2,
                ),
                melody("future"),
            ),
            inspect_live=False,
        )

        self.assertFalse(validation.valid)
        self.assertIn(
            "future_operation_reference", {item.code for item in validation.blockers}
        )

    def test_incompatible_output_reference_is_rejected(self) -> None:
        validation = runs.validate_production_run(
            request(allowed_changes=("notes", "pattern_metadata")),
            plan(
                runs.PreparePatternOperation(
                    operation_id="pattern",
                    pattern_number=2,
                    name="Drop",
                ),
                runs.WriteNoteSequenceOperation(
                    operation_id="write",
                    sequence=runs.OperationOutputReference(
                        operation_id="pattern", output="note_sequence"
                    ),
                    channel_index=1,
                    pattern_number=2,
                ),
            ),
            inspect_live=False,
        )

        self.assertFalse(validation.valid)
        self.assertIn(
            "incompatible_output_reference",
            {item.code for item in validation.blockers},
        )

    def test_known_unavailable_operations_return_structured_capability_blockers(
        self,
    ) -> None:
        for operation_name, category in (
            ("create_playlist_clip", "unsupported_by_fl_studio"),
            ("render_project", "unsupported_by_fl_studio"),
            ("insert_plugin", "unsupported_by_fl_studio"),
            ("save_project", "unsupported_by_postfader"),
        ):
            with self.subTest(operation=operation_name):
                operation = runs.UnavailableProductionOperation(
                    operation_id=f"unsupported-{operation_name}",
                    operation=operation_name,
                )
                validation = runs.validate_production_run(
                    request(),
                    plan(operation),
                    inspect_live=False,
                )

                self.assertFalse(validation.valid)
                self.assertEqual(
                    validation.unsupported_operations,
                    (f"unsupported-{operation_name}:{operation_name}",),
                )
                self.assertEqual(validation.blockers[0].category, category)
                self.assertEqual(
                    validation.blockers[0].code,
                    "unsupported_production_operation",
                )

        registry = runs.ProductionRunRegistry()
        with (
            mock.patch.object(runs, "WriteModeManager") as mode,
            mock.patch.object(runs, "_dispatch_operation") as dispatch,
        ):
            result = registry.execute(
                request(),
                plan(
                    runs.UnavailableProductionOperation(
                        operation_id="unsupported-render",
                        operation="render_project",
                    )
                ),
            )
        self.assertEqual(result.status, "blocked")
        mode.assert_not_called()
        dispatch.assert_not_called()

    def test_generator_semantics_and_piano_roll_note_limit_are_preflighted(
        self,
    ) -> None:
        invalid_generator = runs.validate_production_run(
            request(allowed_changes=("composition",)),
            plan(
                runs.GenerateChordProgressionOperation(
                    operation_id="invalid-chords",
                    progression=("not-a-chord",),
                )
            ),
            inspect_live=False,
        )
        too_many_notes = make_sequence(
            name="Too many",
            generator="test",
            notes=[CreativeNote(pitch=60, start_beats=0, duration_beats=1)]
            * (runs.MAX_PIANO_ROLL_NOTES + 1),
        )
        oversized_write = runs.validate_production_run(
            request(allowed_changes=("notes",)),
            plan(
                runs.WriteNoteSequenceOperation(
                    operation_id="oversized-write",
                    sequence=too_many_notes,
                    channel_index=1,
                    pattern_number=2,
                )
            ),
            inspect_live=False,
        )

        self.assertIn(
            "invalid_generator_operation",
            {item.code for item in invalid_generator.blockers},
        )
        self.assertIn(
            "piano_roll_note_limit_exceeded",
            {item.code for item in oversized_write.blockers},
        )

    def test_request_operation_limit_is_enforced(self) -> None:
        validation = runs.validate_production_run(
            request(allowed_changes=("composition",), max_operations=1),
            plan(melody("one"), melody("two")),
            inspect_live=False,
        )

        self.assertFalse(validation.valid)
        self.assertIn(
            "operation_limit_exceeded", {item.code for item in validation.blockers}
        )

    def test_validation_diagnostics_are_bounded_with_an_explicit_omission(self) -> None:
        dependencies = tuple(f"missing-{index}" for index in range(32))
        production_plan = plan(
            *(
                runs.GenerateMelodyOperation(
                    operation_id=f"melody-{index}",
                    after=dependencies,
                )
                for index in range(runs.MAX_PRODUCTION_OPERATIONS)
            )
        )

        validation = runs.validate_production_run(
            request(
                allowed_changes=("composition",),
                max_operations=runs.MAX_PRODUCTION_OPERATIONS,
            ),
            production_plan,
            inspect_live=False,
        )

        self.assertFalse(validation.valid)
        self.assertEqual(len(validation.blockers), runs.MAX_VALIDATION_BLOCKERS)
        self.assertEqual(
            validation.blockers[-1].code,
            "additional_blockers_omitted",
        )

    def test_registry_is_bounded_and_evicts_oldest_released_run(self) -> None:
        registry = runs.ProductionRunRegistry(max_runs=2)
        records = [
            registry._create_record(
                request(allowed_changes=("composition",)), plan(melody(name))
            )
            for name in ("one", "two")
        ]
        for record in records:
            registry._release(record)

        newest = registry._create_record(
            request(allowed_changes=("composition",)), plan(melody("three"))
        )
        registry._release(newest)

        self.assertEqual(len(registry._runs), 2)
        self.assertFalse(registry.get(records[0].state.run_id).found)
        self.assertTrue(registry.get(records[1].state.run_id).found)
        self.assertTrue(registry.get(newest.state.run_id).found)

    def test_plan_only_mutating_plan_never_dispatches_or_enables_writes(self) -> None:
        registry = runs.ProductionRunRegistry()
        production_plan = plan(select_pattern())
        with (
            mock.patch.object(runs, "WriteModeManager") as mode,
            mock.patch.object(runs, "_dispatch_operation") as dispatch,
        ):
            result = registry.execute(
                request(policy="plan_only", allowed_changes=("pattern_metadata",)),
                production_plan,
            )

        self.assertEqual(result.status, "blocked")
        self.assertIn("plan_only_mutation", {item.code for item in result.blockers})
        mode.assert_not_called()
        dispatch.assert_not_called()

    def test_authorized_run_enables_write_mode_once(self) -> None:
        registry = runs.ProductionRunRegistry()
        production_plan = plan(select_pattern("first", 2), select_pattern("second", 3))
        mode = mock.Mock()
        mode.set_write_mode.return_value = SimpleNamespace(
            session_fingerprint=SESSION,
            after_enabled=True,
        )
        dispatch = mock.Mock(
            side_effect=[verified_selection(), verified_selection(pattern_number=3)]
        )
        with (
            mock.patch.object(
                runs,
                "_live_validation",
                return_value=([], [], SESSION, PROJECT_STATE),
            ),
            mock.patch.object(
                runs, "_current_session_matches", return_value=(True, "")
            ),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(runs, "_dispatch_operation", dispatch),
        ):
            result = registry.execute(
                request(allowed_changes=("pattern_metadata",)),
                production_plan,
            )

        self.assertEqual(result.status, "completed")
        mode.set_write_mode.assert_called_once_with(
            enabled=True,
            confirm_user_present=True,
            session_fingerprint=SESSION,
        )
        self.assertEqual(dispatch.call_count, 2)

    def test_mutating_continuation_reuses_the_runs_single_write_transition(
        self,
    ) -> None:
        registry = runs.ProductionRunRegistry()
        mode = mock.Mock()
        mode.set_write_mode.return_value = SimpleNamespace(
            session_fingerprint=SESSION,
            after_enabled=True,
        )
        active_connection = SimpleNamespace(
            connected=True,
            compatible=True,
            session_fingerprint=SESSION,
            verified_writes_enabled=True,
            bridge_provenance_verified=True,
        )
        inspector = mock.Mock()
        inspector.connection_info.return_value = active_connection
        dispatch = mock.Mock(
            side_effect=[verified_selection(), verified_selection(pattern_number=3)]
        )
        with (
            mock.patch.object(
                runs,
                "_live_validation",
                return_value=([], [], SESSION, PROJECT_STATE),
            ),
            mock.patch.object(
                runs, "_current_session_matches", return_value=(True, "")
            ),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(runs, "ReadOnlyInspector", return_value=inspector),
            mock.patch.object(runs, "_dispatch_operation", dispatch),
        ):
            first = registry.execute(
                request(allowed_changes=("pattern_metadata",)),
                plan(select_pattern("first", 2)),
            )
            continued = registry.continue_run(
                first.run_id,
                runs.ProductionRunDelta(
                    mode="append",
                    operations=(select_pattern("second", 3),),
                ),
            )

        self.assertEqual(first.status, "completed")
        self.assertEqual(continued.status, "completed")
        self.assertEqual(mode.set_write_mode.call_count, 1)
        self.assertEqual(dispatch.call_count, 2)

    def test_validation_and_plan_only_do_not_enable_write_mode(self) -> None:
        mode = mock.Mock()
        production_plan = plan(melody())
        with mock.patch.object(runs, "WriteModeManager", return_value=mode):
            validation = runs.validate_production_run(
                request(
                    policy="plan_only",
                    allowed_changes=("composition",),
                    authorized=False,
                ),
                production_plan,
                inspect_live=False,
            )
            registry = runs.ProductionRunRegistry()
            result = registry.execute(
                request(
                    policy="plan_only",
                    allowed_changes=("composition",),
                    authorized=False,
                ),
                production_plan,
            )

        self.assertTrue(validation.valid)
        self.assertEqual(result.status, "completed")
        mode.assert_not_called()

    def test_session_fingerprint_change_stops_before_next_operation(self) -> None:
        registry = runs.ProductionRunRegistry()
        production_plan = plan(select_pattern("first", 2), select_pattern("second", 3))
        mode = mock.Mock()
        mode.set_write_mode.return_value = SimpleNamespace(
            session_fingerprint=SESSION,
            after_enabled=True,
        )
        dispatch = mock.Mock(return_value=verified_selection())
        with (
            mock.patch.object(
                runs,
                "_live_validation",
                return_value=([], [], SESSION, PROJECT_STATE),
            ),
            mock.patch.object(
                runs,
                "_current_session_matches",
                side_effect=[(True, ""), (False, "FL Studio reloaded the bridge.")],
            ),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(runs, "_dispatch_operation", dispatch),
        ):
            result = registry.execute(
                request(allowed_changes=("pattern_metadata",)),
                production_plan,
            )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.completed_count, 1)
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(result.blockers[-1].code, "session_precondition_failed")

    def test_unverified_mutation_stops_future_operations(self) -> None:
        registry = runs.ProductionRunRegistry()
        production_plan = plan(select_pattern("first", 2), select_pattern("second", 3))
        dispatch = mock.Mock(
            side_effect=[
                verified_selection(verified=False),
                AssertionError("must stop"),
            ]
        )
        mode = mock.Mock()
        mode.set_write_mode.return_value = SimpleNamespace(
            session_fingerprint=SESSION,
            after_enabled=True,
        )
        with (
            mock.patch.object(
                runs,
                "_live_validation",
                return_value=([], [], SESSION, PROJECT_STATE),
            ),
            mock.patch.object(
                runs, "_current_session_matches", return_value=(True, "")
            ),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(runs, "_dispatch_operation", dispatch),
        ):
            result = registry.execute(
                request(allowed_changes=("pattern_metadata",)),
                production_plan,
            )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.blockers[-1].category, "unverified_mutation")
        self.assertEqual(result.attempted_count, 1)
        self.assertEqual(dispatch.call_count, 1)

    def test_authenticated_piano_roll_runtime_receipt_can_continue_run(self) -> None:
        notes = [CreativeNote(pitch=60, start_beats=0, duration_beats=1)]
        digest = note_digest(notes)
        evidence = PianoRollScriptRuntimeEvidence(
            request_id="d" * 32,
            operation="write_notes",
            mode="replace",
            requested_note_digest=digest,
            expected_added_note_count=1,
            ppq=96,
            before_note_count=None,
            score_note_count=1,
            added_note_digest_sha256="e" * 64,
            score_digest_sha256="f" * 64,
            script_completed=True,
            postcondition_verified=True,
            receipt_path="/tmp/synthetic-piano-roll-receipt.json",
            receipt_sha256="0" * 64,
            persistence_check_completed=True,
            persistence_check_verified=True,
            verification_receipt_path=(
                "/tmp/synthetic-piano-roll-persistence.json"
            ),
            verification_receipt_sha256="2" * 64,
        )
        receipt = PianoRollDispatch(
            requested_at=datetime.now(timezone.utc),
            request_id=evidence.request_id,
            operation="write_notes",
            mode="replace",
            script_path="/tmp/Postfader_Apply.pyscript",
            script_sha256="1" * 64,
            requested_note_count=1,
            requested_note_digest=digest,
            status="script_runtime_verified",
            application_verified=True,
            script_runtime_evidence=evidence,
        )

        self.assertEqual(runs._classify_mutation_result(receipt), (True, True, ""))

    def test_unknown_mutation_outcome_stops_without_retrying_or_dispatching_later(
        self,
    ) -> None:
        registry = runs.ProductionRunRegistry()
        production_plan = plan(
            select_pattern("first", 2),
            select_pattern("ambiguous", 3),
            select_pattern("later", 4),
        )
        dispatch = mock.Mock(
            side_effect=[
                verified_selection(),
                RuntimeError("reply lost after dispatch"),
                AssertionError("must stop"),
            ]
        )
        mode = mock.Mock()
        mode.set_write_mode.return_value = SimpleNamespace(
            session_fingerprint=SESSION,
            after_enabled=True,
        )
        with (
            mock.patch.object(
                runs,
                "_live_validation",
                return_value=([], [], SESSION, PROJECT_STATE),
            ),
            mock.patch.object(
                runs, "_current_session_matches", return_value=(True, "")
            ),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(runs, "_dispatch_operation", dispatch),
        ):
            result = registry.execute(
                request(allowed_changes=("pattern_metadata",)),
                production_plan,
            )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.blockers[-1].category, "unknown_outcome")
        self.assertEqual(result.attempted_count, 2)
        self.assertEqual(dispatch.call_count, 2)
        lookup = registry.get(result.run_id)
        self.assertTrue(lookup.found)
        assert lookup.state is not None
        self.assertEqual(lookup.state.receipts[-1].status, "error_unknown")
        self.assertFalse(result.rollback_attempted)

    def test_long_operation_error_is_bounded_and_returns_a_blocked_run(self) -> None:
        registry = runs.ProductionRunRegistry()
        mode = mock.Mock()
        mode.set_write_mode.return_value = SimpleNamespace(
            session_fingerprint=SESSION,
            after_enabled=True,
        )
        with (
            mock.patch.object(
                runs,
                "_live_validation",
                return_value=([], [], SESSION, PROJECT_STATE),
            ),
            mock.patch.object(
                runs, "_current_session_matches", return_value=(True, "")
            ),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(
                runs,
                "_dispatch_operation",
                side_effect=RuntimeError("x" * 4000),
            ),
        ):
            result = registry.execute(
                request(allowed_changes=("pattern_metadata",)),
                plan(select_pattern()),
            )

        self.assertEqual(result.status, "blocked")
        lookup = registry.get(result.run_id)
        assert lookup.state is not None
        self.assertEqual(lookup.state.status, "blocked")
        self.assertLessEqual(len(lookup.state.receipts[0].error or ""), 2048)
        self.assertLessEqual(len(lookup.state.blockers[-1].evidence[0]), 512)

    def test_stop_during_a_failing_mutation_remains_stopped(self) -> None:
        registry = runs.ProductionRunRegistry()
        entered = threading.Event()
        release = threading.Event()
        outcome: list[runs.ProductionRunResult] = []
        mode = mock.Mock()
        mode.set_write_mode.return_value = SimpleNamespace(
            session_fingerprint=SESSION,
            after_enabled=True,
        )

        def fail_after_stop(*_args: object, **_kwargs: object) -> object:
            entered.set()
            self.assertTrue(release.wait(5))
            raise RuntimeError("mutation reply was lost")

        def execute() -> None:
            outcome.append(
                registry.execute(
                    request(allowed_changes=("pattern_metadata",)),
                    plan(select_pattern()),
                )
            )

        with (
            mock.patch.object(
                runs,
                "_live_validation",
                return_value=([], [], SESSION, PROJECT_STATE),
            ),
            mock.patch.object(
                runs, "_current_session_matches", return_value=(True, "")
            ),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(runs, "_dispatch_operation", side_effect=fail_after_stop),
        ):
            worker = threading.Thread(target=execute)
            worker.start()
            self.assertTrue(entered.wait(5))
            with registry._lock:
                run_id = next(iter(registry._runs))
            stopped = registry.stop(run_id)
            release.set()
            worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(stopped.status, "stopped")
        self.assertEqual(outcome[0].status, "stopped")
        lookup = registry.get(run_id)
        assert lookup.state is not None
        self.assertEqual(lookup.state.status, "stopped")
        self.assertIn(
            "nothing was undone", (lookup.state.final_summary or "").casefold()
        )

    def test_stop_during_the_last_successful_mutation_is_not_overwritten(self) -> None:
        registry = runs.ProductionRunRegistry()

        def stop_before_return(*_args: object, **_kwargs: object) -> object:
            with registry._lock:
                run_id = next(iter(registry._runs))
            registry.stop(run_id)
            return verified_selection()

        mode = mock.Mock()
        mode.set_write_mode.return_value = SimpleNamespace(
            session_fingerprint=SESSION,
            after_enabled=True,
        )
        with (
            mock.patch.object(
                runs,
                "_live_validation",
                return_value=([], [], SESSION, PROJECT_STATE),
            ),
            mock.patch.object(
                runs, "_current_session_matches", return_value=(True, "")
            ),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(
                runs, "_dispatch_operation", side_effect=stop_before_return
            ),
        ):
            result = registry.execute(
                request(allowed_changes=("pattern_metadata",)),
                plan(select_pattern()),
            )

        self.assertEqual(result.status, "stopped")
        lookup = registry.get(result.run_id)
        assert lookup.state is not None
        self.assertEqual(lookup.state.status, "stopped")

    def test_stop_during_session_check_prevents_dispatch(self) -> None:
        registry = runs.ProductionRunRegistry()
        entered = threading.Event()
        release = threading.Event()
        outcome: list[runs.ProductionRunResult] = []

        def delayed_match(*_args: object, **_kwargs: object) -> tuple[bool, str]:
            entered.set()
            self.assertTrue(release.wait(5))
            return True, ""

        def execute() -> None:
            outcome.append(
                registry.execute(
                    request(allowed_changes=("pattern_metadata",)),
                    plan(select_pattern()),
                )
            )

        mode = mock.Mock()
        mode.set_write_mode.return_value = SimpleNamespace(
            session_fingerprint=SESSION,
            after_enabled=True,
        )
        dispatch = mock.Mock(return_value=verified_selection())
        with (
            mock.patch.object(
                runs,
                "_live_validation",
                return_value=([], [], SESSION, PROJECT_STATE),
            ),
            mock.patch.object(
                runs, "_current_session_matches", side_effect=delayed_match
            ),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(runs, "_dispatch_operation", dispatch),
        ):
            worker = threading.Thread(target=execute)
            worker.start()
            self.assertTrue(entered.wait(5))
            with registry._lock:
                run_id = next(iter(registry._runs))
            registry.stop(run_id)
            release.set()
            worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome[0].status, "stopped")
        dispatch.assert_not_called()

    def test_later_failure_keeps_earlier_receipt_and_never_claims_rollback(
        self,
    ) -> None:
        registry = runs.ProductionRunRegistry()
        production_plan = plan(select_pattern("first", 2), select_pattern("second", 3))
        dispatch = mock.Mock(
            side_effect=[
                verified_selection(),
                RuntimeError("reply lost after dispatch"),
            ]
        )
        mode = mock.Mock()
        mode.set_write_mode.return_value = SimpleNamespace(
            session_fingerprint=SESSION,
            after_enabled=True,
        )
        with (
            mock.patch.object(
                runs,
                "_live_validation",
                return_value=([], [], SESSION, PROJECT_STATE),
            ),
            mock.patch.object(
                runs, "_current_session_matches", return_value=(True, "")
            ),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(runs, "_dispatch_operation", dispatch),
        ):
            result = registry.execute(
                request(allowed_changes=("pattern_metadata",)),
                production_plan,
            )

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.completed_count, 1)
        self.assertEqual(result.attempted_count, 2)
        lookup = registry.get(result.run_id)
        self.assertTrue(lookup.found)
        assert lookup.state is not None
        self.assertEqual(
            [receipt.status for receipt in lookup.state.receipts],
            ["verified", "error_unknown"],
        )
        self.assertFalse(result.rollback_attempted)
        self.assertFalse(result.project_saved)
        self.assertFalse(lookup.state.rollback_attempted)
        self.assertFalse(lookup.state.project_saved)

    def test_continue_append_preserves_completed_receipts_and_executes_only_new_work(
        self,
    ) -> None:
        registry = runs.ProductionRunRegistry()
        original = plan(melody("completed"))
        generated = sequence()
        with mock.patch.object(runs, "_dispatch_operation", return_value=generated):
            first = registry.execute(
                request(allowed_changes=("composition",)),
                original,
            )
        before = registry.get(first.run_id)
        self.assertTrue(before.found)
        assert before.state is not None
        original_receipt = before.state.receipts[0]

        with mock.patch.object(
            runs, "_dispatch_operation", return_value=generated
        ) as dispatch:
            continued = registry.continue_run(
                first.run_id,
                runs.ProductionRunDelta(
                    mode="append",
                    operations=(melody("new"),),
                ),
            )

        after = registry.get(first.run_id)
        self.assertEqual(continued.status, "completed")
        self.assertEqual(dispatch.call_count, 1)
        self.assertTrue(after.found)
        assert after.state is not None
        self.assertEqual(after.state.receipts[0], original_receipt)
        self.assertEqual(
            [receipt.operation_id for receipt in after.state.receipts],
            ["completed", "new"],
        )

    def test_scope_restrictions_reject_disallowed_operations_before_execution(
        self,
    ) -> None:
        scoped = runs.ProductionScope(
            kind="mix_only",
            description="Only mixer and routing changes are allowed.",
        )
        production_plan = plan(select_pattern())
        validation = runs.validate_production_run(
            request(
                scope=scoped,
                allowed_changes=("mixer",),
            ),
            production_plan,
            inspect_live=False,
        )

        self.assertFalse(validation.valid)
        codes = {item.code for item in validation.blockers}
        self.assertIn("change_not_allowed", codes)
        self.assertIn("outside_run_scope", codes)

        registry = runs.ProductionRunRegistry()
        with mock.patch.object(runs, "_dispatch_operation") as dispatch:
            result = registry.execute(
                request(scope=scoped, allowed_changes=("mixer",)),
                production_plan,
            )
        self.assertEqual(result.status, "blocked")
        dispatch.assert_not_called()

    def test_mix_only_rejects_channel_pitch_as_note_content(self) -> None:
        pitch_change = runs.ApplyVerifiedBatchOperation(
            operation_id="pitch",
            operations=(
                {
                    "operation_id": "pitch-channel",
                    "operation": "channel_pitch",
                    "channel_index": 2,
                    "pitch_normalized": 0.2,
                },
            ),
        )
        validation = runs.validate_production_run(
            request(
                scope=runs.ProductionScope(
                    kind="mix_only",
                    description="Mix without changing musical content.",
                ),
                preserve=runs.ProductionPreservation(note_content=True),
                allowed_changes=("channels",),
            ),
            plan(pitch_change),
            inspect_live=False,
        )

        codes = {item.code for item in validation.blockers}
        self.assertIn("change_not_allowed", codes)
        self.assertIn("outside_run_scope", codes)
        self.assertIn("preserved_category", codes)

    def test_selected_targets_require_explicit_opt_in_for_global_changes(self) -> None:
        tempo_change = runs.ApplyVerifiedBatchOperation(
            operation_id="tempo",
            operations=(
                {
                    "operation_id": "set-tempo",
                    "operation": "tempo",
                    "tempo_bpm": 174.0,
                },
            ),
        )
        selected = runs.ProductionScope(
            kind="selected_targets",
            description="Only the selected vocal channel.",
            targets=(runs.ProductionTarget(kind="channel", index=2),),
        )

        rejected = runs.validate_production_run(
            request(scope=selected, allowed_changes=("tempo",)),
            plan(tempo_change),
            inspect_live=False,
        )
        explicitly_allowed = runs.validate_production_run(
            request(
                scope=selected.model_copy(
                    update={"additional_allowed_changes": ("tempo",)}
                ),
                allowed_changes=("tempo",),
            ),
            plan(tempo_change),
            inspect_live=False,
        )

        self.assertIn("outside_run_scope", {item.code for item in rejected.blockers})
        self.assertTrue(explicitly_allowed.valid)

    def test_continue_cannot_rewrite_a_completed_receipt(self) -> None:
        registry = runs.ProductionRunRegistry()
        original = plan(melody("completed"))
        generated = sequence()
        with mock.patch.object(runs, "_dispatch_operation", return_value=generated):
            first = registry.execute(
                request(allowed_changes=("composition",)),
                original,
            )
        before = registry.get(first.run_id)
        self.assertTrue(before.found)
        assert before.state is not None
        original_receipt = before.state.receipts[0]

        delta = runs.ProductionRunDelta(
            mode="replace_remaining",
            operations=(melody("completed"),),
        )
        continued = registry.continue_run(first.run_id, delta)
        after = registry.get(first.run_id)

        self.assertEqual(continued.status, "blocked")
        self.assertEqual(continued.blockers[-1].code, "duplicate_operation_id")
        self.assertTrue(after.found)
        assert after.state is not None
        self.assertEqual(after.state.receipts[0], original_receipt)

    def test_empty_replacement_stops_and_preserves_completed_receipts(self) -> None:
        registry = runs.ProductionRunRegistry()
        with mock.patch.object(runs, "_dispatch_operation", return_value=sequence()):
            first = registry.execute(
                request(allowed_changes=("composition",)),
                plan(melody("completed")),
            )
        before = registry.get(first.run_id)
        assert before.state is not None

        stopped = registry.continue_run(
            first.run_id,
            runs.ProductionRunDelta(mode="replace_remaining", operations=()),
        )
        after = registry.get(first.run_id)

        self.assertEqual(stopped.status, "stopped")
        self.assertEqual(stopped.blockers[-1].code, "remainder_removed")
        assert after.state is not None
        self.assertEqual(after.state.receipts, before.state.receipts)
        self.assertEqual(after.state.completed_operations, ("completed",))

    def test_continue_stops_when_the_live_project_checkpoint_changed(self) -> None:
        registry = runs.ProductionRunRegistry()
        mode = mock.Mock()
        mode.set_write_mode.return_value = SimpleNamespace(
            session_fingerprint=SESSION,
            after_enabled=True,
        )
        dispatch = mock.Mock(return_value=verified_selection())
        with (
            mock.patch.object(
                runs,
                "_live_validation",
                side_effect=[
                    ([], [], SESSION, PROJECT_STATE),
                    ([], [], SESSION, "d" * 64),
                ],
            ),
            mock.patch.object(
                runs, "_current_session_matches", return_value=(True, "")
            ),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(runs, "_dispatch_operation", dispatch),
        ):
            first = registry.execute(
                request(allowed_changes=("pattern_metadata",)),
                plan(select_pattern("first", 2)),
            )
            continued = registry.continue_run(
                first.run_id,
                runs.ProductionRunDelta(
                    mode="append",
                    operations=(select_pattern("second", 3),),
                ),
            )

        self.assertEqual(first.status, "completed")
        self.assertEqual(continued.status, "blocked")
        self.assertEqual(
            continued.blockers[-1].code,
            "continued_project_state_changed",
        )
        self.assertEqual(dispatch.call_count, 1)

    def test_continue_rejects_a_dependency_that_did_not_complete(self) -> None:
        registry = runs.ProductionRunRegistry()
        mode = mock.Mock()
        mode.set_write_mode.return_value = SimpleNamespace(
            session_fingerprint=SESSION,
            after_enabled=True,
        )
        dispatch = mock.Mock(return_value=verified_selection(verified=False))
        with (
            mock.patch.object(
                runs,
                "_live_validation",
                return_value=([], [], SESSION, PROJECT_STATE),
            ),
            mock.patch.object(
                runs, "_current_session_matches", return_value=(True, "")
            ),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(runs, "_dispatch_operation", dispatch),
        ):
            first = registry.execute(
                request(allowed_changes=("pattern_metadata", "composition")),
                plan(select_pattern("failed", 2)),
            )
            continued = registry.continue_run(
                first.run_id,
                runs.ProductionRunDelta(
                    mode="append",
                    operations=(
                        runs.GenerateMelodyOperation(
                            operation_id="dependent",
                            after=("failed",),
                        ),
                    ),
                ),
            )

        self.assertEqual(first.status, "blocked")
        self.assertEqual(continued.status, "blocked")
        self.assertIn(
            "dependency_not_completed",
            {item.code for item in continued.blockers},
        )
        self.assertEqual(dispatch.call_count, 1)

    def test_stop_during_continuation_validation_prevents_write_enablement(
        self,
    ) -> None:
        registry = runs.ProductionRunRegistry()
        generated = sequence()
        with mock.patch.object(runs, "_dispatch_operation", return_value=generated):
            first = registry.execute(
                request(
                    allowed_changes=("composition", "pattern_metadata"),
                    policy="execute_until_blocked",
                ),
                plan(melody("generated")),
            )

        entered = threading.Event()
        release = threading.Event()
        outcome: list[runs.ProductionRunResult] = []

        def validate_live(
            *_args: object, **_kwargs: object
        ) -> tuple[list[object], list[str], str, str]:
            entered.set()
            self.assertTrue(release.wait(5))
            return [], [], SESSION, PROJECT_STATE

        def continue_work() -> None:
            outcome.append(
                registry.continue_run(
                    first.run_id,
                    runs.ProductionRunDelta(
                        mode="append",
                        operations=(select_pattern("future", 2),),
                    ),
                )
            )

        mode = mock.Mock()
        with (
            mock.patch.object(runs, "_live_validation", side_effect=validate_live),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(runs, "_dispatch_operation") as dispatch,
        ):
            worker = threading.Thread(target=continue_work)
            worker.start()
            self.assertTrue(entered.wait(5))
            registry.stop(first.run_id)
            release.set()
            worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome[0].status, "stopped")
        mode.assert_not_called()
        dispatch.assert_not_called()

    def test_stop_prevents_continuation_and_does_not_undo(self) -> None:
        registry = runs.ProductionRunRegistry()
        production_plan = plan(select_pattern())
        mode = mock.Mock()
        mode.set_write_mode.return_value = SimpleNamespace(
            session_fingerprint=SESSION,
            after_enabled=True,
        )
        with (
            mock.patch.object(
                runs,
                "_live_validation",
                return_value=([], [], SESSION, PROJECT_STATE),
            ),
            mock.patch.object(
                runs, "_current_session_matches", return_value=(True, "")
            ),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(
                runs, "_dispatch_operation", return_value=verified_selection()
            ),
        ):
            first = registry.execute(
                request(allowed_changes=("pattern_metadata",)),
                production_plan,
            )

        stopped = registry.stop(first.run_id)
        self.assertEqual(stopped.status, "stopped")
        self.assertFalse(stopped.rollback_attempted)
        self.assertIn("nothing was undone", stopped.summary)
        with self.assertRaisesRegex(ValueError, "was stopped"):
            registry.continue_run(
                first.run_id,
                runs.ProductionRunDelta(
                    mode="append",
                    operations=(melody("future"),),
                ),
            )

    def test_stopping_twice_is_idempotent_and_does_not_grow_history(self) -> None:
        registry = runs.ProductionRunRegistry()
        with mock.patch.object(runs, "_dispatch_operation", return_value=sequence()):
            first = registry.execute(
                request(allowed_changes=("composition",)),
                plan(melody()),
            )

        stopped_once = registry.stop(first.run_id)
        state_once = registry.get(first.run_id).state
        stopped_twice = registry.stop(first.run_id)
        state_twice = registry.get(first.run_id).state

        self.assertEqual(stopped_twice, stopped_once)
        self.assertIsNotNone(state_once)
        self.assertEqual(state_twice, state_once)

    def test_missing_process_local_run_is_explicit(self) -> None:
        registry = runs.ProductionRunRegistry()
        missing_id = "f" * 32

        lookup = registry.get(missing_id)

        self.assertFalse(lookup.found)
        self.assertIn("previous MCP process", lookup.message)
        with self.assertRaisesRegex(ValueError, "previous MCP process"):
            registry.continue_run(
                missing_id, runs.ProductionRunDelta(operations=(melody(),))
            )
        with self.assertRaisesRegex(ValueError, "previous MCP process"):
            registry.stop(missing_id)

    def test_existing_operation_adapters_delegate_to_postfader_implementations(
        self,
    ) -> None:
        generated = sequence()
        generate = melody("generate")
        with mock.patch.object(
            runs, "compose_melody", return_value=generated
        ) as composer:
            result = runs._dispatch_operation(
                generate,
                session_fingerprint=None,
                outputs={},
            )
        self.assertIs(result, generated)
        composer.assert_called_once()

        prepare = runs.PreparePatternOperation(
            operation_id="prepare",
            pattern_number=7,
            name="Drop",
        )
        with mock.patch.object(
            runs, "prepare_empty_pattern", return_value=object()
        ) as preparer:
            runs._dispatch_operation(
                prepare,
                session_fingerprint=SESSION,
                outputs={},
            )
        preparer.assert_called_once_with(
            name="Drop",
            length_beats=16,
            color=None,
            start_pattern_number=7,
            expected_pattern_number=7,
            session_fingerprint=SESSION,
        )

        reference_outputs = {
            "generate": runs.ProductionGeneratedOutput(
                operation_id="generate",
                value=generated,
            )
        }
        write = runs.WriteNoteSequenceOperation(
            operation_id="write",
            sequence=runs.OperationOutputReference(
                operation_id="generate", output="note_sequence"
            ),
            channel_index=3,
            pattern_number=5,
            mode="replace",
        )
        with mock.patch.object(
            runs, "write_piano_roll_notes", return_value=object()
        ) as writer:
            write_result = runs._dispatch_operation(
                write,
                session_fingerprint=SESSION,
                outputs=reference_outputs,
            )
        self.assertIsNotNone(write_result)
        writer.assert_called_once_with(
            generated.notes,
            channel_index=3,
            pattern_number=5,
            mode="replace",
            auto_trigger=True,
            session_fingerprint=SESSION,
        )

        batch = runs.ApplyVerifiedBatchOperation(
            operation_id="batch",
            operations=(
                {
                    "operation_id": "select",
                    "operation": "pattern_identity",
                    "pattern_number": 2,
                    "name": "Drop",
                },
            ),
        )
        batch_result = object()
        executor = mock.Mock()
        executor.apply.return_value = batch_result
        with mock.patch.object(runs, "VerifiedBatchExecutor", return_value=executor):
            self.assertIs(
                runs._dispatch_operation(
                    batch,
                    session_fingerprint=SESSION,
                    outputs={},
                ),
                batch_result,
            )
        executor.apply.assert_called_once_with(
            operations=list(batch.operations),
            stop_on_unverified=True,
            session_fingerprint=SESSION,
        )

    def test_unarmed_piano_roll_operations_share_one_concise_setup_blocker(
        self,
    ) -> None:
        production_plan = plan(
            runs.WriteNoteSequenceOperation(
                operation_id="write_one",
                sequence=sequence(),
                channel_index=1,
                pattern_number=2,
            ),
            runs.WriteNoteSequenceOperation(
                operation_id="write_two",
                sequence=sequence(),
                channel_index=2,
                pattern_number=3,
            ),
        )
        connection = ConnectionInfo(
            connected=True,
            compatible=True,
            compatibility_reason="ok",
            runtime_write_mode_control=True,
            bridge_provenance_verified=True,
            session_fingerprint=SESSION,
        )
        project = ProjectSummary(
            observed_at=datetime.now(timezone.utc),
            connection=connection,
            mixer_track_count=8,
            channel_count=8,
            pattern_count=8,
            playlist_track_count=8,
            transport=TransportState(playing=False, recording=False),
        )
        inspector = mock.Mock()
        inspector.connection_info.side_effect = [connection, connection]
        inspector.project_summary.return_value = project
        piano_status = SimpleNamespace(
            armed_this_session=False,
            setup_instruction="Prepare and confirm the Piano Roll script once.",
        )
        pattern_inventory = SimpleNamespace(maximum_pattern_number=8, patterns=[])
        track_inspector = mock.Mock()
        track_inspector.list_patterns.return_value = pattern_inventory
        with (
            mock.patch.object(runs, "ReadOnlyInspector", return_value=inspector),
            mock.patch.object(runs, "TrackBInspector", return_value=track_inspector),
            mock.patch.object(runs.PIANO_ROLL, "status", return_value=piano_status),
        ):
            validation = runs.validate_production_run(
                request(allowed_changes=("notes",)),
                production_plan,
                inspect_live=True,
            )

        piano_blockers = [
            item
            for item in validation.blockers
            if item.code == "piano_roll_bridge_not_armed"
        ]
        self.assertEqual(len(piano_blockers), 1)
        self.assertIn("one setup step", piano_blockers[0].message)

    def test_live_pattern_validation_uses_pattern_maximum_not_used_count(
        self,
    ) -> None:
        connection = ConnectionInfo(
            connected=True,
            compatible=True,
            compatibility_reason="ok",
            runtime_write_mode_control=True,
            bridge_provenance_verified=True,
            session_fingerprint=SESSION,
        )
        project = ProjectSummary(
            observed_at=datetime.now(timezone.utc),
            connection=connection,
            mixer_track_count=8,
            channel_count=8,
            pattern_count=1,
            playlist_track_count=8,
            transport=TransportState(playing=False, recording=False),
        )
        inspector = mock.Mock()
        inspector.connection_info.return_value = connection
        inspector.project_summary.return_value = project
        track_inspector = mock.Mock()
        track_inspector.list_patterns.return_value = SimpleNamespace(
            maximum_pattern_number=4,
            patterns=[],
        )
        with (
            mock.patch.object(runs, "ReadOnlyInspector", return_value=inspector),
            mock.patch.object(runs, "TrackBInspector", return_value=track_inspector),
        ):
            validation = runs.validate_production_run(
                request(allowed_changes=("pattern_metadata",)),
                plan(select_pattern(pattern_number=2)),
                inspect_live=True,
            )

        self.assertTrue(validation.valid)
        self.assertNotIn(
            "target_index_unavailable", {item.code for item in validation.blockers}
        )

    def test_generator_scalar_payloads_are_bounded(self) -> None:
        with self.assertRaises(ValueError):
            runs.GenerateMelodyOperation(operation_id="root", root="C" * 1000)
        with self.assertRaises(ValueError):
            runs.GenerateChordProgressionOperation(
                operation_id="chord",
                progression=("I" * 1000,),
            )
        with self.assertRaises(ValueError):
            runs.GenerateMelodyOperation(operation_id="seed", seed=2**80)

    def test_registry_lookups_isolate_nested_generated_output_lists(self) -> None:
        registry = runs.ProductionRunRegistry()
        with mock.patch.object(runs, "_dispatch_operation", return_value=sequence()):
            result = registry.execute(
                request(allowed_changes=("composition",)),
                plan(melody()),
            )

        first = registry.get(result.run_id)
        assert first.state is not None
        original_count = len(first.state.generated_outputs[0].value.notes)
        first.state.generated_outputs[0].value.notes.append(
            CreativeNote(pitch=72, start_beats=0, duration_beats=1)
        )
        second = registry.get(result.run_id)
        assert second.state is not None
        self.assertEqual(
            len(second.state.generated_outputs[0].value.notes),
            original_count,
        )

    def test_generated_sequence_can_feed_a_later_write_operation(self) -> None:
        generated = runs._dispatch_operation(
            chords("harmony"),
            session_fingerprint=None,
            outputs={},
        )
        self.assertIsInstance(generated, NoteSequence)
        outputs = {
            "harmony": runs.ProductionGeneratedOutput(
                operation_id="harmony",
                value=generated,
            )
        }
        write = runs.WriteNoteSequenceOperation(
            operation_id="write_harmony",
            sequence=runs.OperationOutputReference(
                operation_id="harmony", output="note_sequence"
            ),
            channel_index=2,
            pattern_number=4,
        )
        with mock.patch.object(
            runs, "write_piano_roll_notes", return_value=object()
        ) as writer:
            runs._dispatch_operation(
                write,
                session_fingerprint=SESSION,
                outputs=outputs,
            )
        self.assertEqual(writer.call_args.args[0], generated.notes)

    def test_invalid_plan_performs_zero_project_mutations(self) -> None:
        registry = runs.ProductionRunRegistry()
        invalid = plan(melody("duplicate"), chords("duplicate"))
        with (
            mock.patch.object(runs, "_live_validation") as live,
            mock.patch.object(runs, "_dispatch_operation") as dispatch,
            mock.patch.object(runs, "WriteModeManager") as mode,
        ):
            result = registry.execute(
                request(allowed_changes=("composition",)),
                invalid,
            )

        self.assertEqual(result.status, "blocked")
        live.assert_not_called()
        dispatch.assert_not_called()
        mode.assert_not_called()
        self.assertEqual(result.attempted_count, 0)

    def test_production_run_tools_remain_registered_with_honest_annotations(
        self,
    ) -> None:
        from fl_studio_mcp import mcp_server

        tools = {tool.name: tool for tool in asyncio.run(mcp_server.mcp.list_tools())}
        expected = {
            "postfader_validate_run",
            "postfader_execute_run",
            "postfader_get_run",
            "postfader_continue_run",
            "postfader_stop_run",
        }
        self.assertTrue(expected <= tools.keys())
        self.assertTrue(
            {
                "fl_apply_verified_batch",
                "compose_melody",
                "mix_apply_plan",
                "piano_roll_write_notes",
            }
            <= tools.keys()
        )
        for name in ("postfader_validate_run", "postfader_get_run"):
            annotations = tools[name].annotations
            self.assertIsNotNone(annotations)
            self.assertTrue(annotations.read_only_hint)
            self.assertFalse(annotations.destructive_hint)
        for name in ("postfader_execute_run", "postfader_continue_run"):
            annotations = tools[name].annotations
            self.assertIsNotNone(annotations)
            self.assertFalse(annotations.read_only_hint)
            self.assertTrue(annotations.destructive_hint)
            self.assertFalse(annotations.idempotent_hint)
        stop_annotations = tools["postfader_stop_run"].annotations
        self.assertIsNotNone(stop_annotations)
        self.assertFalse(stop_annotations.read_only_hint)
        self.assertFalse(stop_annotations.destructive_hint)


if __name__ == "__main__":
    unittest.main(verbosity=2)
