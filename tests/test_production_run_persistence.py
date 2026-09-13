"""Restart and crash-boundary tests; no test contacts a live FL bridge."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


# The safe runner executes each file directly in a fresh child process.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fl_studio_mcp import production_runs as runs
from fl_studio_mcp.production_run_persistence import (
    LocalProductionRunStore,
    ProductionRunConflictError,
    ProductionRunStoreError,
)
from tests.test_production_runs import (
    OTHER_SESSION,
    PROJECT_STATE,
    SESSION,
    melody,
    plan,
    request,
    select_pattern,
    verified_selection,
)


class ProductionRunPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state" / "production.sqlite3"

    def registry(self, **kwargs: object) -> runs.ProductionRunRegistry:
        return runs.ProductionRunRegistry(store_path=self.path, **kwargs)

    def child(self, source: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", textwrap.dedent(source)],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "POSTFADER_PRODUCTION_RUN_PATH": str(self.path)},
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    def mock_live(self, *, session: str = SESSION) -> None:
        mode = mock.Mock()
        mode.set_write_mode.side_effect = lambda **kwargs: SimpleNamespace(
            before_enabled=False,
            after_enabled=kwargs["enabled"],
            session_fingerprint=session,
        )
        for patch in (
            mock.patch.object(runs, "_creation_plan_needs_readiness", return_value=False),
            mock.patch.object(runs, "_live_validation", return_value=(
                [], [], session, PROJECT_STATE,
            )),
            mock.patch.object(runs, "_runtime_preflight_blocker", return_value=None),
            mock.patch.object(runs, "_current_session_matches", return_value=(True, "")),
            mock.patch.object(runs, "_capture_project_state", return_value=(PROJECT_STATE, "")),
            mock.patch.object(runs, "WriteModeManager", return_value=mode),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_import_and_missing_reads_do_not_create_user_files(self) -> None:
        result = self.child("""
            from fl_studio_mcp.production_runs import PRODUCTION_RUNS, list_production_runs
            assert not PRODUCTION_RUNS.get('a' * 32).found
            assert list_production_runs() == ()
        """)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.path.parent.exists())

    def test_in_memory_registry_remains_ephemeral(self) -> None:
        with mock.patch.dict(os.environ, {"POSTFADER_PRODUCTION_RUN_PATH": str(self.path)}):
            result = runs.ProductionRunRegistry().execute(
                request(allowed_changes=("composition",)), plan(melody()),
            )
        self.assertEqual(result.status, "completed")
        self.assertFalse(self.path.exists())

    def test_completed_run_survives_two_real_processes(self) -> None:
        first = self.child("""
            from fl_studio_mcp.production_runs import PRODUCTION_RUNS
            from tests.test_production_runs import request, plan, melody
            result = PRODUCTION_RUNS.execute(request(allowed_changes=('composition',)), plan(melody()))
            assert result.status == 'completed', result
            print(result.run_id)
        """)
        self.assertEqual(first.returncode, 0, first.stderr)
        run_id = first.stdout.strip()
        second = self.child(f"""
            from fl_studio_mcp.production_runs import PRODUCTION_RUNS, list_production_runs
            from fl_studio_mcp.creative import NoteSequence
            state = PRODUCTION_RUNS.get({run_id!r}).state
            assert state.status == 'completed'
            assert state.completed_operations == ('melody',)
            assert isinstance(state.generated_outputs[0].value, NoteSequence)
            assert state.recovered_at is not None and not state.process_local
            assert list_production_runs()[0].run_id == {run_id!r}
            print(state.model_dump_json())
        """)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["receipts"][0]["status"], "generated")

    def test_recovered_generated_output_is_used_by_saved_remainder(self) -> None:
        registry = self.registry()
        adaptation = runs.AdaptNoteSequenceOperation(
            operation_id="adapt",
            sequence=runs.OperationOutputReference(operation_id="melody", output="note_sequence"),
            characteristics=runs.SelectedSoundCharacteristics(role_id="lead"),
            connected_ai_register=(60, 72),
        )
        pause = runs._blocker("setup_or_session", "test_pause", "Pause after the generated melody.")
        with mock.patch.object(runs, "_read_only_result_blocker", return_value=pause):
            first = registry.execute(
                request(allowed_changes=("composition",)), plan(melody(), adaptation),
            )
        self.assertEqual(first.status, "blocked")
        self.assertEqual(first.completed_count, 1)
        recovered = self.registry()
        with mock.patch.object(runs, "_dispatch_operation", wraps=runs._dispatch_operation) as dispatch:
            final = recovered.continue_run(first.run_id, runs.ProductionRunDelta(mode="resume"))
        self.assertEqual(final.status, "completed", final)
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(dispatch.call_args.args[0].operation_id, "adapt")
        self.assertEqual(final.receipts[0], first.receipts[0])
        self.assertEqual(final.generated_outputs[0], first.generated_outputs[0])
        adapted = next(item.value for item in final.generated_outputs if item.operation_id == "adapt" and item.output == "note_sequence")
        self.assertTrue(all(60 <= note.pitch <= 72 for note in adapted.notes))

    def test_partial_verified_run_resumes_only_unattempted_operation(self) -> None:
        self.mock_live()
        registry = self.registry()
        pause = runs._blocker("setup_or_session", "test_pause", "Pause before the second write.")
        with (
            mock.patch.object(runs, "_runtime_preflight_blocker", side_effect=[None, pause]),
            mock.patch.object(runs, "_dispatch_operation", return_value=verified_selection()),
        ):
            first = registry.execute(request(), plan(select_pattern("first"), select_pattern("second", 3)))
        self.assertEqual(first.completed_count, 1)
        recovered = self.registry()
        with (
            mock.patch.object(runs, "_live_validation", return_value=([], [], SESSION, PROJECT_STATE)) as validate,
            mock.patch.object(runs, "_runtime_preflight_blocker", return_value=None) as target_check,
            mock.patch.object(runs, "_dispatch_operation", return_value=verified_selection(pattern_number=3)) as dispatch,
        ):
            final = recovered.continue_run(first.run_id, runs.ProductionRunDelta(mode="resume"))
        self.assertEqual(final.status, "completed", final)
        self.assertEqual(final.receipts[0], first.receipts[0])
        self.assertEqual(validate.call_args.kwargs["start_index"], 1)
        self.assertEqual(target_check.call_count, 1)
        self.assertEqual(dispatch.call_args.args[0].operation_id, "second")
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(self.registry().get(first.run_id).state.completed_operations, ("first", "second"))

    def test_cached_partial_checkpoint_does_not_mistake_own_writes_for_project_drift(self) -> None:
        self.mock_live()
        registry = self.registry()
        record = registry._create_record(
            request().model_copy(update={"expected_project_state_digest": PROJECT_STATE}),
            plan(select_pattern("first"), select_pattern("second", 3)),
        )
        record.state = record.state.model_copy(update={
            "session_fingerprint": SESSION, "project_state_digest": PROJECT_STATE,
        })
        record.readiness = SimpleNamespace(sound_inventory=None, loaded_processing_observations=())
        validation = runs.validate_production_run(record.state.request, record.plan, inspect_live=False).model_copy(update={
            "session_fingerprint": SESSION, "project_state_digest": PROJECT_STATE,
        })
        with (
            mock.patch.object(runs, "_runtime_preflight_blocker", side_effect=[None, SystemExit(23)]),
            mock.patch.object(runs, "_dispatch_operation", return_value=verified_selection()),
            self.assertRaises(SystemExit),
        ):
            registry._execute_validated(record, start_index=0, validation=validation)
        recovered = self.registry()
        state = recovered.get(record.state.run_id).state
        self.assertEqual(state.completed_operations, ("first",))
        self.assertIsNone(state.project_state_digest)
        with (
            mock.patch.object(runs.ReadOnlyInspector, "connection_info", return_value=SimpleNamespace(
                connected=True, compatible=True, session_fingerprint=SESSION, project_load_epoch=False,
            )),
            mock.patch.object(runs, "_dispatch_operation") as legacy_dispatch,
            mock.patch.object(runs, "WriteModeManager") as legacy_mode,
        ):
            blocked = recovered.continue_run(state.run_id, runs.ProductionRunDelta(mode="resume"))
        self.assertEqual(blocked.status, "blocked")
        self.assertIn("recovered_project_identity_unavailable", {item.code for item in blocked.blockers})
        legacy_dispatch.assert_not_called()
        legacy_mode.assert_not_called()
        after_own_write = "d" * 64
        with (
            mock.patch.object(runs.ReadOnlyInspector, "connection_info", return_value=SimpleNamespace(
                connected=True, compatible=True, session_fingerprint=SESSION, project_load_epoch=True,
            )),
            mock.patch.object(runs, "_live_validation", return_value=([], [], SESSION, after_own_write)),
            mock.patch.object(runs, "_capture_project_state", return_value=(after_own_write, "")),
            mock.patch.object(runs, "_dispatch_operation", return_value=verified_selection(pattern_number=3)) as dispatch,
        ):
            result = recovered.continue_run(state.run_id, runs.ProductionRunDelta(mode="resume"))
        self.assertEqual(result.status, "completed", result)
        self.assertEqual(result.receipts[0], state.receipts[0])
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(dispatch.call_args.args[0].operation_id, "second")

    def test_legacy_bridge_cannot_resume_without_a_post_write_checkpoint(self) -> None:
        registry = self.registry()
        record = registry._create_record(request(), plan(select_pattern("first"), select_pattern("next", 3)))
        record.state = record.state.model_copy(update={
            "session_fingerprint": SESSION,
            "project_state_digest": None,
            "recovered_at": runs._now(),
        })
        for supports_epoch, session, code in (
            (False, SESSION, "recovered_project_identity_unavailable"),
            (True, OTHER_SESSION, "continued_session_changed"),
            (True, SESSION, None),
        ):
            with mock.patch.object(runs.ReadOnlyInspector, "connection_info", return_value=SimpleNamespace(
                connected=True, compatible=True, session_fingerprint=session,
                project_load_epoch=supports_epoch,
            )):
                blocker = registry._recovery_identity_blocker(record, record.plan, 1)
            self.assertEqual(None if blocker is None else blocker.code, code)

    def test_real_crash_keeps_start_marker_and_records_unknown_without_replay(self) -> None:
        child = self.child("""
            import os
            from types import SimpleNamespace
            from unittest import mock
            from fl_studio_mcp import production_runs as runs
            from tests.test_production_runs import request, plan, melody, select_pattern, SESSION, PROJECT_STATE
            real_dispatch = runs._dispatch_operation
            def dispatch(operation, **kwargs):
                if operation.operation_id == 'write':
                    os._exit(23)
                return real_dispatch(operation, **kwargs)
            mode = mock.Mock()
            mode.set_write_mode.return_value = SimpleNamespace(before_enabled=False, after_enabled=True, session_fingerprint=SESSION)
            with (
                mock.patch.object(runs, '_creation_plan_needs_readiness', return_value=False),
                mock.patch.object(runs, '_live_validation', return_value=([], [], SESSION, PROJECT_STATE)),
                mock.patch.object(runs, '_runtime_preflight_blocker', return_value=None),
                mock.patch.object(runs, '_current_session_matches', return_value=(True, '')),
                mock.patch.object(runs, 'WriteModeManager', return_value=mode),
                mock.patch.object(runs, '_dispatch_operation', side_effect=dispatch),
            ):
                runs.PRODUCTION_RUNS.execute(request(), plan(melody(), select_pattern('write'), select_pattern('later', 3)))
        """)
        self.assertEqual(child.returncode, 23, child.stderr)
        store = LocalProductionRunStore(self.path)
        run_id = store.list_ids()[0]
        durable = json.loads(store.load(run_id)[1])
        self.assertEqual(durable["in_flight_operation_id"], "write")
        self.assertFalse(durable["state"]["write_mode_active"])
        self.assertFalse(durable["state"]["write_mode_owned_by_run"])
        with mock.patch.object(runs, "_dispatch_operation") as dispatch:
            registry = self.registry()
            state = registry.get(run_id).state
            self.assertEqual(state.status, "blocked")
            self.assertEqual(state.completed_operations, ("melody",))
            self.assertEqual(state.receipts[-1].operation_id, "write")
            self.assertEqual(state.receipts[-1].status, "error_unknown")
            self.assertFalse(state.receipts[-1].outcome_known)
            self.assertEqual(state.current_operation_index, 2)
            self.assertFalse(state.write_mode_active)
            self.assertIsNone(registry._write_mode_owner_run_id)
            with self.assertRaisesRegex(ValueError, "inspect the project"):
                registry.continue_run(run_id, runs.ProductionRunDelta(mode="resume"))
        dispatch.assert_not_called()
        self.assertEqual(len(self.registry().get(run_id).state.receipts), 2)

    def test_changed_session_blocks_recovered_continuation_before_dispatch(self) -> None:
        self.mock_live()
        with mock.patch.object(runs, "_dispatch_operation", return_value=verified_selection()):
            first = self.registry().execute(request(), plan(select_pattern("first")))
        with (
            mock.patch.object(runs, "_live_validation", return_value=([], [], OTHER_SESSION, PROJECT_STATE)),
            mock.patch.object(runs, "_dispatch_operation") as dispatch,
            mock.patch.object(runs, "WriteModeManager") as mode,
        ):
            result = self.registry().continue_run(first.run_id, runs.ProductionRunDelta(
                mode="append", operations=(select_pattern("next", 3),),
            ))
        self.assertEqual(result.status, "blocked")
        self.assertIn("continued_session_changed", {item.code for item in result.blockers})
        self.assertEqual(result.receipts, first.receipts)
        dispatch.assert_not_called()
        mode.assert_not_called()

    def test_completed_operation_cannot_be_replaced_after_restart(self) -> None:
        first = self.registry().execute(request(allowed_changes=("composition",)), plan(melody()))
        result = self.registry().continue_run(first.run_id, runs.ProductionRunDelta(
            mode="append", operations=(melody(),),
        ))
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.receipts, first.receipts)

    def test_eviction_only_drops_memory_and_keeps_durable_ids_discoverable(self) -> None:
        registry = self.registry(max_runs=1)
        one = registry.execute(request(allowed_changes=("composition",)), plan(melody("one")))
        two = registry.execute(request(allowed_changes=("composition",)), plan(melody("two")))
        self.assertEqual({row.run_id for row in registry.list_runs()}, {one.run_id, two.run_id})
        self.assertTrue(registry.get(one.run_id).found)
        self.assertLessEqual(len(registry._runs), 1)

    def test_stopped_remainder_and_plan_are_recoverable(self) -> None:
        registry = self.registry()
        first = registry.execute(request(policy="plan_only"), plan(select_pattern()))
        registry.continue_run(first.run_id, runs.ProductionRunDelta())
        state = self.registry().get(first.run_id).state
        self.assertEqual(state.status, "stopped")
        self.assertEqual(state.total_operations, 0)

    def test_stale_registry_cannot_overwrite_a_new_checkpoint(self) -> None:
        first = self.registry().execute(request(allowed_changes=("composition",)), plan(melody()))
        stale = self.registry()
        stale.get(first.run_id)
        self.registry().continue_run(first.run_id, runs.ProductionRunDelta(
            mode="append", operations=(melody("second"),),
        ))
        with (
            mock.patch.object(runs, "_dispatch_operation") as dispatch,
            self.assertRaises(ProductionRunConflictError),
        ):
            stale.continue_run(first.run_id, runs.ProductionRunDelta(
                mode="append", operations=(melody("stale"),),
            ))
        dispatch.assert_not_called()
        self.assertEqual(self.registry().get(first.run_id).state.completed_operations, ("melody", "second"))
        self.assertEqual(stale.get(first.run_id).state.completed_operations, ("melody", "second"))
        next_result = stale.continue_run(first.run_id, runs.ProductionRunDelta(
            mode="append", operations=(melody("third"),),
        ))
        self.assertEqual(next_result.status, "completed")
        self.assertEqual(next_result.completed_count, 3)

    def test_failed_start_marker_commit_prevents_dispatch(self) -> None:
        store = LocalProductionRunStore(self.path)
        save = store.save
        def fail_marker(run_id: str, payload: str, **kwargs: object) -> int:
            if json.loads(payload)["in_flight_operation_id"]:
                raise ProductionRunStoreError("Disk full")
            return save(run_id, payload, **kwargs)
        with (
            mock.patch.object(store, "save", side_effect=fail_marker),
            mock.patch.object(runs, "_dispatch_operation") as dispatch,
            self.assertRaisesRegex(ProductionRunStoreError, "Disk full"),
        ):
            runs.ProductionRunRegistry(store=store).execute(
                request(allowed_changes=("composition",)), plan(melody()),
            )
        dispatch.assert_not_called()

    def test_failed_receipt_commit_recovers_unknown_without_inventing_success(self) -> None:
        self.mock_live()
        store = LocalProductionRunStore(self.path)
        save = store.save
        def fail_receipt(run_id: str, payload: str, **kwargs: object) -> int:
            if json.loads(payload)["state"]["receipts"]:
                raise ProductionRunStoreError("Disk full after dispatch")
            return save(run_id, payload, **kwargs)
        with (
            mock.patch.object(store, "save", side_effect=fail_receipt),
            mock.patch.object(runs, "_dispatch_operation", return_value=verified_selection()) as dispatch,
            self.assertRaisesRegex(ProductionRunStoreError, "Disk full after dispatch"),
        ):
            runs.ProductionRunRegistry(store=store).execute(request(), plan(select_pattern()))
        self.assertEqual(dispatch.call_count, 1)
        recovered = self.registry().get(store.list_ids()[0]).state
        self.assertEqual(recovered.receipts[0].status, "error_unknown")
        self.assertEqual(recovered.completed_operations, ())

    def test_corrupt_journal_is_reported_without_overwriting_it(self) -> None:
        self.path.parent.mkdir()
        damaged = b"not a SQLite database"
        self.path.write_bytes(damaged)
        with self.assertRaises(ProductionRunStoreError):
            self.registry().get("a" * 32)
        self.assertEqual(self.path.read_bytes(), damaged)


if __name__ == "__main__":
    unittest.main()
