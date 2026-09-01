"""Regression coverage for Production Run write-boundary ownership."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from threading import Event, Thread
from types import SimpleNamespace
from unittest import mock

from fl_studio_mcp import production_runs as runs
from fl_studio_mcp.track_b_contracts import VerifiedPatternSelectionWrite


SESSION = "a" * 32
OTHER_SESSION = "b" * 32
PROJECT_STATE = "c" * 64
WHEN = datetime(2026, 9, 1, tzinfo=timezone.utc)


def request() -> runs.ProductionRunRequest:
    return runs.ProductionRunRequest(
        brief="Apply the requested pattern change.",
        scope=runs.ProductionScope(
            kind="whole_project", description="The complete project."
        ),
        allowed_changes=("pattern_metadata",),
        completion_target="A complete playable draft.",
        interaction_policy="execute_once",
        authorized_to_modify=True,
    )


def pattern(
    operation_id: str = "select-pattern", number: int = 2
) -> runs.SelectPatternOperation:
    return runs.SelectPatternOperation(
        operation_id=operation_id,
        pattern_number=number,
    )


def production_plan(*operations: runs.ProductionOperation) -> runs.ProductionRunPlan:
    return runs.ProductionRunPlan(plan_id="write-boundary", operations=operations)


def verified_pattern(number: int = 2) -> VerifiedPatternSelectionWrite:
    return VerifiedPatternSelectionWrite(
        applied_at=WHEN,
        requested_pattern_number=number,
        before_pattern_number=1,
        after_pattern_number=number,
        verified=True,
        verification_summary="Pattern selection verified.",
        session_fingerprint=SESSION,
        session_precondition_applied=True,
    )


def mode_change(
    *,
    before_enabled: bool,
    after_enabled: bool,
    session_fingerprint: str = SESSION,
) -> SimpleNamespace:
    """Small typed-shape stand-in matching WriteModeChange's consumed fields."""

    return SimpleNamespace(
        before_enabled=before_enabled,
        after_enabled=after_enabled,
        requested_enabled=after_enabled,
        changed=before_enabled != after_enabled,
        bridge_mode="write_test" if after_enabled else "read_only",
        write_mode_origin="runtime_request" if after_enabled else "disabled",
        confirmation_required=after_enabled,
        confirmation_applied=after_enabled,
        session_fingerprint=session_fingerprint,
        session_precondition_applied=True,
        session_only=True,
        startup_default_enabled=False,
        project_saved=False,
        verified=True,
    )


class CreationWriteBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        checkpoint = mock.patch.object(
            runs,
            "_capture_project_state",
            return_value=(PROJECT_STATE, ""),
        )
        checkpoint.start()
        self.addCleanup(checkpoint.stop)

    def live_validation(self):
        return mock.patch.object(
            runs,
            "_live_validation",
            return_value=([], [], SESSION, PROJECT_STATE),
        )

    def current_session(self):
        return mock.patch.object(
            runs,
            "_current_session_matches",
            return_value=(True, ""),
        )

    def execute_with_mode(
        self, mode: mock.Mock
    ) -> tuple[runs.ProductionRunRegistry, runs.ProductionRunResult]:
        registry = runs.ProductionRunRegistry()
        with (
            self.live_validation(),
            self.current_session(),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(
                runs,
                "_dispatch_operation",
                return_value=verified_pattern(),
            ),
        ):
            result = registry.execute(request(), production_plan(pattern()))
        return registry, result

    def test_disabled_gate_is_enabled_once_and_shutdown_after_authorized_run(
        self,
    ) -> None:
        mode = mock.Mock()
        mode.set_write_mode.side_effect = [
            mode_change(before_enabled=False, after_enabled=True),
            mode_change(before_enabled=True, after_enabled=False),
        ]

        registry, result = self.execute_with_mode(mode)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.write_mode_enable_count, 1)
        self.assertEqual(result.write_mode_disable_count, 1)
        self.assertFalse(result.write_mode_active)
        self.assertTrue(result.write_mode_shutdown_verified)
        self.assertEqual(
            mode.set_write_mode.call_args_list,
            [
                mock.call(
                    enabled=True,
                    confirm_user_present=True,
                    session_fingerprint=SESSION,
                ),
                mock.call(
                    enabled=False,
                    confirm_user_present=False,
                    session_fingerprint=SESSION,
                ),
            ],
        )
        lookup = registry.get(result.run_id)
        self.assertIsNotNone(lookup.state)
        assert lookup.state is not None
        self.assertFalse(lookup.state.write_mode_preexisting)
        self.assertFalse(lookup.state.write_mode_owned_by_run)

    def test_preexisting_enabled_gate_is_preserved_and_not_owned_by_run(
        self,
    ) -> None:
        mode = mock.Mock()
        mode.set_write_mode.return_value = mode_change(
            before_enabled=True,
            after_enabled=True,
        )

        registry, result = self.execute_with_mode(mode)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.write_mode_enable_count, 0)
        self.assertEqual(result.write_mode_disable_count, 0)
        mode.set_write_mode.assert_called_once_with(
            enabled=True,
            confirm_user_present=True,
            session_fingerprint=SESSION,
        )
        lookup = registry.get(result.run_id)
        self.assertIsNotNone(lookup.state)
        assert lookup.state is not None
        self.assertTrue(lookup.state.write_mode_preexisting)
        self.assertFalse(lookup.state.write_mode_owned_by_run)

    def test_unexpected_exception_after_owned_enable_still_disables_in_finally(
        self,
    ) -> None:
        mode = mock.Mock()
        mode.set_write_mode.side_effect = [
            mode_change(before_enabled=False, after_enabled=True),
            mode_change(before_enabled=True, after_enabled=False),
        ]
        registry = runs.ProductionRunRegistry()

        def fail_after_enable(
            record: object,
            *,
            start_index: int,
            validation: runs.ProductionRunValidation,
        ) -> None:
            del start_index
            registry._enable_write_mode(
                record,
                validation=validation,
                start_index=0,
            )
            raise RuntimeError("unexpected execution failure")

        with (
            self.live_validation(),
            self.current_session(),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(
                runs.ProductionRunRegistry,
                "_execute_validated",
                side_effect=fail_after_enable,
            ),
        ):
            result = registry.execute(request(), production_plan(pattern()))

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.write_mode_enable_count, 1)
        self.assertEqual(result.write_mode_disable_count, 1)
        self.assertFalse(result.write_mode_active)
        self.assertTrue(result.write_mode_shutdown_verified)
        self.assertEqual(mode.set_write_mode.call_count, 2)
        self.assertEqual(result.completed_count, 0)

    def test_enable_exception_gets_best_effort_cleanup_without_verified_completion(
        self,
    ) -> None:
        mode = mock.Mock()
        mode.set_write_mode.side_effect = [
            RuntimeError("enable reply was lost"),
            mode_change(before_enabled=True, after_enabled=False),
        ]

        registry, result = self.execute_with_mode(mode)

        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.completed_count, 0)
        self.assertEqual(result.write_mode_disable_count, 1)
        self.assertFalse(result.write_mode_active)
        self.assertTrue(result.write_mode_shutdown_verified)
        self.assertEqual(mode.set_write_mode.call_count, 2)
        self.assertEqual(
            mode.set_write_mode.call_args_list[0],
            mock.call(
                enabled=True,
                confirm_user_present=True,
                session_fingerprint=SESSION,
            ),
        )
        self.assertEqual(
            mode.set_write_mode.call_args_list[1],
            mock.call(
                enabled=False,
                confirm_user_present=False,
                session_fingerprint=SESSION,
            ),
        )
        self.assertTrue(
            any(item.code == "write_mode_enable_failed" for item in result.blockers)
        )

    def test_continue_unexpected_exception_also_disables_owned_gate(self) -> None:
        mode = mock.Mock()
        mode.set_write_mode.side_effect = [
            mode_change(before_enabled=False, after_enabled=True),
            mode_change(before_enabled=True, after_enabled=False),
            mode_change(before_enabled=False, after_enabled=True),
            mode_change(before_enabled=True, after_enabled=False),
        ]
        registry = runs.ProductionRunRegistry()
        with (
            self.live_validation(),
            self.current_session(),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(
                runs,
                "_dispatch_operation",
                return_value=verified_pattern(),
            ),
        ):
            first = registry.execute(request(), production_plan(pattern("first")))
        self.assertEqual(first.status, "completed")

        def fail_after_enable(
            record: object,
            *,
            start_index: int,
            validation: runs.ProductionRunValidation,
        ) -> None:
            registry._enable_write_mode(
                record,
                validation=validation,
                start_index=start_index,
            )
            raise RuntimeError("unexpected continuation failure")

        with (
            self.live_validation(),
            self.current_session(),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(
                runs.ProductionRunRegistry,
                "_execute_validated",
                side_effect=fail_after_enable,
            ),
        ):
            continued = registry.continue_run(
                first.run_id,
                runs.ProductionRunDelta(
                    mode="append",
                    operations=(pattern("second", 3),),
                ),
            )

        self.assertEqual(continued.status, "blocked")
        self.assertEqual(continued.write_mode_enable_count, 2)
        self.assertEqual(continued.write_mode_disable_count, 2)
        self.assertFalse(continued.write_mode_active)
        self.assertTrue(continued.write_mode_shutdown_verified)
        self.assertEqual(mode.set_write_mode.call_count, 4)

    def test_shutdown_receipt_contradiction_or_session_mismatch_is_not_verified(
        self,
    ) -> None:
        for label, shutdown in (
            (
                "contradictory_after_enabled",
                mode_change(before_enabled=True, after_enabled=True),
            ),
            (
                "session_mismatch",
                mode_change(
                    before_enabled=True,
                    after_enabled=False,
                    session_fingerprint=OTHER_SESSION,
                ),
            ),
        ):
            with self.subTest(label=label):
                mode = mock.Mock()
                mode.set_write_mode.side_effect = [
                    mode_change(before_enabled=False, after_enabled=True),
                    shutdown,
                ]

                _registry, result = self.execute_with_mode(mode)

                self.assertEqual(result.status, "blocked")
                self.assertIsNot(result.write_mode_shutdown_verified, True)
                self.assertTrue(result.write_mode_active)
                self.assertEqual(result.write_mode_disable_count, 0)
                self.assertIn(
                    "write_mode_shutdown_failed",
                    {item.code for item in result.blockers},
                )

    def test_stop_waits_for_in_flight_enable_before_ordered_shutdown(self) -> None:
        enable_started = Event()
        release_enable = Event()
        enable_returned = Event()
        mode = mock.Mock()

        def transition(*, enabled: bool, **_: object) -> SimpleNamespace:
            if enabled:
                enable_started.set()
                self.assertTrue(release_enable.wait(timeout=2.0))
                enable_returned.set()
                return mode_change(before_enabled=False, after_enabled=True)
            self.assertTrue(enable_returned.is_set())
            return mode_change(before_enabled=True, after_enabled=False)

        mode.set_write_mode.side_effect = transition
        registry = runs.ProductionRunRegistry()
        result_box: list[runs.ProductionRunResult] = []

        def execute() -> None:
            result_box.append(registry.execute(request(), production_plan(pattern())))

        with (
            self.live_validation(),
            self.current_session(),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
            mock.patch.object(
                runs,
                "_dispatch_operation",
                return_value=verified_pattern(),
            ),
        ):
            worker = Thread(target=execute)
            worker.start()
            self.assertTrue(enable_started.wait(timeout=2.0))
            with registry._lock:
                run_id = next(iter(registry._runs))

            stopped = registry.stop(run_id)
            self.assertEqual(stopped.status, "stopped")
            self.assertEqual(mode.set_write_mode.call_count, 1)

            release_enable.set()
            worker.join(timeout=2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(result_box), 1)
        self.assertEqual(result_box[0].status, "stopped")
        self.assertEqual(mode.set_write_mode.call_count, 2)
        self.assertEqual(
            [call.kwargs["enabled"] for call in mode.set_write_mode.call_args_list],
            [True, False],
        )
        final = registry.get(run_id)
        assert final.state is not None
        self.assertFalse(final.state.write_mode_active)
        self.assertFalse(final.state.write_mode_owned_by_run)
        self.assertTrue(final.state.write_mode_shutdown_verified)


if __name__ == "__main__":
    unittest.main()
