"""Fake-only coverage for the supervised live acceptance harnesses."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, os.fspath(ROOT))

from fl_studio_mcp.acceptance import (  # noqa: E402
    AcceptanceConfigurationError,
    IsolatedReadToolSupervisor,
    ReadToolInvocationError,
    ReadToolInvocationResult,
    ReadToolTimeoutError,
    authoritative_tool_surface,
    read_acceptance_arguments,
    resolve_evidence_reference,
    run_read_acceptance,
    run_write_acceptance,
    validate_write_scenario_plan,
)
from fl_studio_mcp.evidence import (  # noqa: E402
    EvidenceOutputError,
    configure_acceptance_transport,
    reserve_evidence_output,
)


def _successful_isolated_read(_name, _arguments):
    from fl_studio_mcp import bridge_client

    class FakeClient:
        def close(self):
            return None

    bridge_client._client = FakeClient()
    return {"isolated": "success"}


def _failing_isolated_read(_name, _arguments):
    from fl_studio_mcp import bridge_client

    class FakeClient:
        def close(self):
            return None

    bridge_client._client = FakeClient()
    raise RuntimeError("isolated fake failure")


def _never_returning_isolated_read(_name, _arguments):
    from fl_studio_mcp import bridge_client

    class FakeClient:
        def close(self):
            return None

    bridge_client._client = FakeClient()
    time.sleep(60)


class ReadAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.surface = asyncio.run(authoritative_tool_surface())

    def arguments(self):
        return read_acceptance_arguments(
            mixer_track_index=3,
            plugin_track_index=3,
            plugin_slot_index=1,
            pattern_number=1,
            channel_index=0,
            fixture_root=ROOT / "tests" / "fixtures" / "audio",
        )

    def test_authoritative_read_surface_is_exercised_without_stale_count(self):
        calls = []

        async def fake(name, arguments):
            calls.append((name, arguments))
            if name == "fl_get_project_summary":
                return {
                    "connection": {
                        "program_title": "Fake FL Studio",
                        "fl_app_version": "26.1.3 [build 5336]",
                        "fl_build": 5336,
                        "midi_scripting_api_version": 44,
                        "bridge_protocol_version": 2,
                        "bridge_source_sha256": "a" * 64,
                        "session_fingerprint": "b" * 32,
                        "bridge_transport": "files",
                        "bridge_mode": "read_only",
                    }
                }
            return {"tool": name, "fake": True}

        report = asyncio.run(
            run_read_acceptance(
                self.arguments(), caller=fake, surface=self.surface, environ={}
            )
        )
        self.assertEqual(report["overall"], "pass")
        self.assertEqual(
            {name for name, _arguments in calls}, set(self.surface.read_tools)
        )
        self.assertEqual(len(calls), len(self.surface.read_tools))
        self.assertEqual(report["connection"]["selected_transport"], "files")
        self.assertEqual(report["connection"]["bridge_protocol"], 2)

    def test_one_read_failure_is_recorded_while_remaining_reads_continue(self):
        calls = []
        checkpoints = []
        failed = self.surface.read_tools[len(self.surface.read_tools) // 2]

        async def fake(name, _arguments):
            calls.append(name)
            if name == failed:
                raise RuntimeError("fake read failure")
            return {"ok": True}

        report = asyncio.run(
            run_read_acceptance(
                self.arguments(),
                caller=fake,
                surface=self.surface,
                environ={},
                checkpoint=lambda report: checkpoints.append(
                    dict(report["last_checkpoint"])
                ),
            )
        )
        self.assertEqual(report["overall"], "fail")
        self.assertEqual(len(calls), len(self.surface.read_tools))
        self.assertEqual(
            report["failures"],
            [
                {
                    "stage": "read_execution",
                    "tool": failed,
                    "error": "fake read failure",
                }
            ],
        )
        self.assertEqual(
            [
                item["status"]
                for item in checkpoints
                if item["tool"] == failed
            ],
            ["in_flight", "failed"],
        )

    def test_large_response_arguments_are_explicitly_bounded(self):
        arguments = self.arguments()
        self.assertIsNone(arguments["fl_list_mixer_tracks"]["max_tracks"])
        self.assertEqual(arguments["plugins_inspect_parameter_map"]["limit"], 128)
        self.assertEqual(arguments["plugins_scan_parameters"]["max_indices"], 8192)
        self.assertEqual(arguments["plugins_scan_parameters"]["max_results"], 2048)
        self.assertIn("fl_list_channels", arguments)
        self.assertIn("fl_get_step_sequence", arguments)

    def test_read_checkpoints_bracket_every_invocation_in_order(self):
        checkpoints = []

        async def fake(name, _arguments):
            return {"tool": name}

        def checkpoint(report):
            checkpoints.append(dict(report["last_checkpoint"]))

        report = asyncio.run(
            run_read_acceptance(
                self.arguments(),
                caller=fake,
                surface=self.surface,
                environ={},
                checkpoint=checkpoint,
            )
        )
        self.assertEqual(report["overall"], "pass")
        self.assertEqual(
            [(item["tool"], item["status"]) for item in checkpoints],
            [
                pair
                for name in self.surface.read_tools
                for pair in ((name, "in_flight"), (name, "passed"))
            ],
        )
        for index, (before, after) in enumerate(
            zip(checkpoints[::2], checkpoints[1::2]), start=1
        ):
            self.assertEqual(before["tool_index"], index)
            self.assertEqual(after["tool_index"], index)
            self.assertEqual(before["tool_count"], len(self.surface.read_tools))
            self.assertEqual(before["arguments"], after["arguments"])
            self.assertIn("monotonic_elapsed_seconds", before)
            self.assertIn("response_json_bytes", after)
            self.assertRegex(after["response_sha256"], r"^[0-9a-f]{64}$")

    def test_timeout_stops_and_marks_every_later_read_skipped(self):
        calls = []
        timed_out = self.surface.read_tools[1]

        async def bounded(name, _arguments, timeout_seconds):
            calls.append(name)
            cleanup = {
                "worker_reaped": True,
                "client_cleanup": {"status": "process_terminated"},
            }
            if name == timed_out:
                raise ReadToolTimeoutError(
                    "injected bounded timeout",
                    timeout_seconds=timeout_seconds,
                    cleanup=cleanup,
                )
            return ReadToolInvocationResult({"tool": name}, cleanup)

        report = asyncio.run(
            run_read_acceptance(
                self.arguments(),
                surface=self.surface,
                environ={},
                bounded_caller=bounded,
                per_tool_timeout_seconds=1.0,
                overall_timeout_seconds=10.0,
            )
        )
        self.assertEqual(calls, list(self.surface.read_tools[:2]))
        self.assertEqual(report["tools"][1]["status"], "timed_out")
        self.assertTrue(
            all(item["status"] == "skipped" for item in report["tools"][2:])
        )
        self.assertEqual(report["overall"], "fail")

    def test_never_returning_worker_leaves_exact_durable_checkpoint_and_is_reaped(self):
        supervisor = IsolatedReadToolSupervisor(
            handler=_never_returning_isolated_read
        )
        with tempfile.TemporaryDirectory(prefix="postfader-read-timeout-") as temp:
            path = Path(temp) / "timeout.json"
            destination = reserve_evidence_output(path, required=True)
            assert destination is not None
            try:
                report = asyncio.run(
                    run_read_acceptance(
                        self.arguments(),
                        surface=self.surface,
                        environ={},
                        checkpoint=destination.write,
                        bounded_caller=supervisor.invoke,
                        per_tool_timeout_seconds=0.25,
                        overall_timeout_seconds=5.0,
                    )
                )
            finally:
                supervisor.close()
                destination.close()
            evidence = json.loads(path.read_text(encoding="utf-8"))

        first = self.surface.read_tools[0]
        self.assertEqual(report["tools"][0]["tool"], first)
        self.assertEqual(report["tools"][0]["status"], "timed_out")
        cleanup = report["tools"][0]["worker_cleanup"]
        self.assertTrue(cleanup["worker_reaped"])
        self.assertTrue(cleanup["termination_requested"])
        self.assertEqual(
            cleanup["client_cleanup"]["status"], "process_terminated"
        )
        self.assertFalse(supervisor.has_active_worker)
        self.assertEqual(evidence["last_checkpoint"]["tool"], first)
        self.assertEqual(evidence["last_checkpoint"]["status"], "timed_out")
        self.assertTrue(
            evidence["last_checkpoint"]["worker_cleanup"]["worker_reaped"]
        )

    def test_isolated_worker_closes_client_on_success_and_exception(self):
        for handler, expected_error in (
            (_successful_isolated_read, None),
            (_failing_isolated_read, "isolated fake failure"),
        ):
            with self.subTest(handler=handler.__name__):
                supervisor = IsolatedReadToolSupervisor(handler=handler)
                try:
                    if expected_error is None:
                        result = asyncio.run(
                            supervisor.invoke("fake_read", {}, 10.0)
                        )
                        cleanup = result.cleanup
                    else:
                        with self.assertRaisesRegex(
                            ReadToolInvocationError, expected_error
                        ) as raised:
                            asyncio.run(
                                supervisor.invoke("fake_read", {}, 10.0)
                            )
                        cleanup = raised.exception.cleanup
                finally:
                    supervisor.close()
                self.assertEqual(
                    cleanup["client_cleanup"]["status"], "closed"
                )
                self.assertTrue(cleanup["worker_reaped"])
                self.assertFalse(supervisor.has_active_worker)

    def test_production_worker_serializes_local_read_and_reaps_without_client(self):
        supervisor = IsolatedReadToolSupervisor()
        try:
            result = asyncio.run(
                supervisor.invoke(
                    "audio_analyze_file",
                    {
                        "path": os.fspath(
                            ROOT / "tests" / "fixtures" / "audio" / "reference_mix.wav"
                        ),
                        "max_seconds": 30.0,
                    },
                    30.0,
                )
            )
        finally:
            supervisor.close()
        self.assertIsInstance(result.value, dict)
        self.assertTrue(result.cleanup["worker_reaped"])
        self.assertEqual(
            result.cleanup["client_cleanup"]["status"], "not_created"
        )
        self.assertFalse(supervisor.has_active_worker)

    def test_postread_evidence_failure_stops_after_reaped_worker(self):
        supervisor = IsolatedReadToolSupervisor(
            handler=_successful_isolated_read
        )
        checkpoint_statuses = []

        def checkpoint(report):
            status = report["last_checkpoint"]["status"]
            checkpoint_statuses.append(status)
            if status == "passed":
                raise EvidenceOutputError("injected read checkpoint failure")

        try:
            report = asyncio.run(
                run_read_acceptance(
                    self.arguments(),
                    surface=self.surface,
                    environ={},
                    checkpoint=checkpoint,
                    bounded_caller=supervisor.invoke,
                    per_tool_timeout_seconds=10.0,
                    overall_timeout_seconds=30.0,
                )
            )
        finally:
            supervisor.close()
        self.assertEqual(checkpoint_statuses, ["in_flight", "passed"])
        self.assertEqual(report["overall"], "fail")
        self.assertEqual(report["tools"][0]["status"], "passed")
        self.assertTrue(report["tools"][0]["worker_cleanup"]["worker_reaped"])
        self.assertTrue(
            all(item["status"] == "skipped" for item in report["tools"][1:])
        )
        self.assertFalse(supervisor.has_active_worker)

    def test_read_deadlines_require_positive_bounds_and_isolated_caller(self):
        async def fake(_name, _arguments):
            return {"ok": True}

        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(AcceptanceConfigurationError):
                    asyncio.run(
                        run_read_acceptance(
                            self.arguments(),
                            caller=fake,
                            surface=self.surface,
                            environ={},
                            per_tool_timeout_seconds=timeout,
                        )
                    )
        with self.assertRaisesRegex(
            AcceptanceConfigurationError, "isolated bounded caller"
        ):
            asyncio.run(
                run_read_acceptance(
                    self.arguments(),
                    caller=fake,
                    surface=self.surface,
                    environ={},
                    per_tool_timeout_seconds=1.0,
                )
            )


class WriteAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.surface = asyncio.run(authoritative_tool_surface())

    def scenario(self):
        arguments = {
            "fl_redo": {},
            "fl_route_channel_to_mixer": {
                "channel_index": 0,
                "mixer_destination": 2,
            },
            "fl_select_channel": {"channel_index": 0},
            "fl_select_mixer_track": {"track_index": 1},
            "fl_select_pattern": {"pattern_number": 1},
            "fl_set_channel_identity": {"channel_index": 0, "name": "Fixture"},
            "fl_set_channel_mix": {
                "channel_index": 0,
                "volume_normalized": 0.5,
            },
            "fl_set_channel_pitch": {
                "channel_index": 0,
                "pitch_normalized": 0.55,
            },
            "fl_set_channel_solo": {"channel_index": 0, "soloed": True},
            "fl_set_loop_mode": {"loop_mode": "song"},
            "fl_set_metronome": {"enabled": True},
            "fl_set_mixer_arm": {"track_index": 1, "armed": True},
            "fl_set_mixer_color": {"track_index": 1, "color": 0x0055AA},
            "fl_set_mixer_mute": {"track_index": 1, "muted": False},
            "fl_set_mixer_name": {"track_index": 1, "name": "Fixture"},
            "fl_set_mixer_pan": {"track_index": 1, "pan": 0.0},
            "fl_set_mixer_send": {
                "track_index": 1,
                "destination_track_index": 2,
                "enabled": True,
            },
            "fl_set_mixer_send_level": {
                "track_index": 1,
                "destination_track_index": 2,
                "level_normalized": 0.5,
            },
            "fl_set_mixer_solo": {"track_index": 1, "soloed": True},
            "fl_set_mixer_stereo_separation": {
                "track_index": 1,
                "stereo_separation": 0.25,
            },
            "fl_set_mixer_volume": {
                "track_index": 1,
                "volume_normalized": 0.8,
            },
            "fl_set_mixer_volume_db": {"track_index": 1, "volume_db": -6.0},
            "fl_set_pattern_identity": {"pattern_number": 1, "name": "Fixture"},
            "fl_set_pattern_length": {"pattern_number": 1, "length_beats": 16},
            "fl_set_playing": {"playing": False},
            "fl_set_playlist_track_identity": {"track_index": 1, "name": "Fixture"},
            "fl_set_playlist_track_state": {"track_index": 1, "muted": True},
            "fl_set_plugin_param": {
                "parameter_index": 0,
                "normalized_value": 0.5,
                "track_index": 1,
                "slot_index": 0,
            },
            "fl_set_plugin_param_display": {
                "parameter": 0,
                "target_value": 0.0,
                "track_index": 1,
                "slot_index": 0,
            },
            "fl_set_plugin_param_option": {
                "parameter": 0,
                "option": "Fixture option",
                "track_index": 1,
                "slot_index": 0,
            },
            "fl_set_song_position": {"position_normalized": 0.0},
            "fl_set_step_sequence": {
                "pattern_number": 1,
                "channel_index": 0,
                "expected_digest": "a" * 64,
                "updates": [{"step_index": 0, "enabled": True}],
            },
            "fl_set_tempo": {"tempo_bpm": 120.0},
            "fl_set_precount": {"enabled": True},
            "fl_set_recording": {"recording": True},
            "fl_set_time_signature_numerator": {"numerator": 4},
            "fl_set_track_eq": {
                "track_index": 1,
                "band_index": 0,
                "gain_normalized": 0.5,
            },
            "fl_stop": {},
            "fl_undo": {},
        }
        return {
            "scenario_version": 1,
            "fixture_status": "REVIEWED_FOR_THIS_DISPOSABLE_PROJECT",
            "safe_to_edit": True,
            "operations": [
                {
                    "tool": name,
                    "before": {"tool": "fl_get_transport_state", "arguments": {}},
                    "mutation_arguments": arguments[name],
                    "restore": [{"tool": name, "arguments": arguments[name]}],
                    "verify_paths": ["playing", "recording"],
                }
                for name in self.surface.persistent_write_tools
            ],
        }

    @staticmethod
    def project_summary():
        return {
            "connection": {
                "bridge_provenance_verified": True,
                "verified_writes_enabled": True,
                "session_fingerprint": "c" * 32,
                "bridge_transport": "midi",
                "bridge_protocol_version": 2,
                "bridge_source_sha256": "d" * 64,
            }
        }

    def passing_caller(self, calls):
        async def fake(name, arguments):
            calls.append((name, dict(arguments)))
            if name == "fl_get_project_summary":
                return self.project_summary()
            if name == "fl_get_transport_state":
                return {"playing": False, "recording": False}
            return {"verified": True, "session_fingerprint": "c" * 32}

        return fake

    def run_acceptance(self, scenario, caller, **overrides):
        options = {
            "confirm_user_present": True,
            "confirm_disposable_project": True,
            "confirm_safe_to_edit": True,
            "caller": caller,
            "surface": self.surface,
        }
        options.update(overrides)
        return asyncio.run(run_write_acceptance(scenario, **options))

    def test_every_persistent_write_is_mutated_once_restored_and_reread(self):
        calls = []
        report = self.run_acceptance(self.scenario(), self.passing_caller(calls))
        self.assertEqual(report["overall"], "pass")
        self.assertEqual(
            {item["tool"] for item in report["operations"]},
            set(self.surface.persistent_write_tools),
        )
        write_counts = Counter(
            name for name, _arguments in calls if name in self.surface.persistent_write_tools
        )
        self.assertEqual(
            write_counts,
            Counter({name: 2 for name in self.surface.persistent_write_tools}),
        )
        self.assertTrue(all(item["mutation_attempts"] == 1 for item in report["operations"]))
        self.assertFalse(report["project_saved"])
        self.assertNotIn("fl_trigger_note", self.surface.persistent_write_tools)
        self.assertIn("fl_trigger_note", self.surface.ephemeral_tools)
        self.assertNotIn("fl_set_write_mode", self.surface.persistent_write_tools)
        self.assertEqual(
            self.surface.session_control_tools,
            ("fl_set_write_mode",),
        )

    def test_required_confirmations_refuse_before_preflight_or_writes(self):
        calls = []
        with self.assertRaisesRegex(AcceptanceConfigurationError, "confirm-user-present"):
            self.run_acceptance(
                self.scenario(),
                self.passing_caller(calls),
                confirm_user_present=False,
            )
        self.assertEqual(calls, [])

    def test_missing_review_marker_refuses_before_all_contact(self):
        scenario = self.scenario()
        del scenario["fixture_status"]
        calls = []
        with self.assertRaisesRegex(
            AcceptanceConfigurationError, "REVIEWED_FOR_THIS_DISPOSABLE_PROJECT"
        ):
            self.run_acceptance(scenario, self.passing_caller(calls))
        self.assertEqual(calls, [])

    def test_entire_scenario_shape_is_refused_before_preflight(self):
        scenario = self.scenario()
        scenario["operations"][-1]["restore"] = []
        calls = []
        with self.assertRaisesRegex(AcceptanceConfigurationError, "restore action"):
            self.run_acceptance(scenario, self.passing_caller(calls))
        self.assertEqual(calls, [])

    def test_playing_or_recording_refuses_before_any_write(self):
        calls = []

        async def fake(name, arguments):
            calls.append((name, arguments))
            if name == "fl_get_project_summary":
                return self.project_summary()
            return {"playing": True, "recording": False}

        report = self.run_acceptance(self.scenario(), fake)
        self.assertEqual(report["overall"], "fail")
        self.assertIn("playing or recording", report["failures"][0]["reason"])
        self.assertFalse(
            any(name in self.surface.persistent_write_tools for name, _args in calls)
        )
    def test_ambiguous_mutation_failure_is_never_replayed(self):
        calls = []
        first = self.surface.persistent_write_tools[0]

        async def fake(name, arguments):
            calls.append((name, arguments))
            if name == "fl_get_project_summary":
                return self.project_summary()
            if name == "fl_get_transport_state":
                return {"playing": False, "recording": False}
            if name == first:
                raise TimeoutError("ambiguous transport loss")
            return {"verified": True}

        report = self.run_acceptance(self.scenario(), fake)
        self.assertEqual(report["overall"], "fail")
        self.assertEqual(report["operations"][0]["status"], "ambiguous_transport_failure")
        self.assertFalse(report["operations"][0]["automatic_replay"])
        self.assertEqual(sum(name == first for name, _args in calls), 1)

    def test_premutation_checkpoint_failure_aborts_with_zero_writes(self):
        calls = []

        def checkpoint(report):
            if report["last_checkpoint"]["phase"] == "mutation_attempt":
                raise EvidenceOutputError("injected pre-mutation checkpoint loss")

        report = self.run_acceptance(
            self.scenario(),
            self.passing_caller(calls),
            checkpoint=checkpoint,
        )
        self.assertEqual(report["overall"], "fail")
        self.assertEqual(report["phase"], "evidence_output_failure")
        self.assertEqual(report["operations"][0]["mutation_attempts"], 0)
        self.assertEqual(
            report["operations"][0]["status"],
            "evidence_output_failure_before_mutation",
        )
        self.assertFalse(
            any(name in self.surface.persistent_write_tools for name, _args in calls)
        )

    def test_postmutation_checkpoint_failure_cannot_suppress_restoration(self):
        calls = []
        first = self.surface.persistent_write_tools[0]
        failure_active = False

        def checkpoint(report):
            nonlocal failure_active
            latest = report["last_checkpoint"]
            if (
                not failure_active
                and latest["phase"] == "mutation_result"
                and latest.get("status") == "completed"
            ):
                failure_active = True
            if failure_active:
                raise EvidenceOutputError("injected post-mutation checkpoint loss")

        report = self.run_acceptance(
            self.scenario(),
            self.passing_caller(calls),
            checkpoint=checkpoint,
        )
        first_calls = [name for name, _arguments in calls if name == first]
        self.assertTrue(failure_active)
        self.assertEqual(first_calls, [first, first])
        self.assertEqual(report["overall"], "fail")
        self.assertEqual(report["operations"][0]["mutation_attempts"], 1)
        self.assertEqual(
            report["operations"][0]["status"], "evidence_output_failure"
        )
        self.assertEqual(
            report["operations"][0]["restoration_status"], "verified"
        )
        self.assertIn("after_restore", report["operations"][0])
        self.assertEqual(
            report["evidence_output_failures"][0]["checkpoint_phase"],
            "mutation_result",
        )

    def test_checkpoint_failure_between_restores_runs_both_and_rereads(self):
        scenario = self.scenario()
        first = self.surface.persistent_write_tools[0]
        scenario["operations"][0]["restore"].append(
            dict(scenario["operations"][0]["restore"][0])
        )
        calls = []
        injected = False

        def checkpoint(report):
            nonlocal injected
            latest = report["last_checkpoint"]
            if (
                not injected
                and latest["phase"] == "restoration_result"
                and latest.get("restore_index") == 0
                and latest.get("status") == "completed"
            ):
                injected = True
                raise EvidenceOutputError("injected between restore actions")

        report = self.run_acceptance(
            scenario,
            self.passing_caller(calls),
            checkpoint=checkpoint,
        )
        self.assertTrue(injected)
        self.assertEqual(
            [name for name, _arguments in calls if name == first],
            [first, first, first],
        )
        operation = report["operations"][0]
        self.assertEqual(len(operation["restore"]), 2)
        self.assertEqual(
            [item["status"] for item in operation["restore"]],
            ["completed", "completed"],
        )
        self.assertIn("after_restore", operation)
        self.assertEqual(operation["restoration_status"], "verified")
        self.assertEqual(operation["status"], "evidence_output_failure")
        self.assertEqual(report["overall"], "fail")

    def test_durable_evidence_identifies_ambiguous_last_mutation(self):
        calls = []
        first = self.surface.persistent_write_tools[0]

        async def fake(name, arguments):
            calls.append((name, arguments))
            if name == "fl_get_project_summary":
                return self.project_summary()
            if name == "fl_get_transport_state":
                return {"playing": False, "recording": False}
            if name == first:
                raise TimeoutError("injected ambiguous transport loss")
            return {"verified": True}

        with tempfile.TemporaryDirectory(prefix="postfader-checkpoints-") as temp:
            path = Path(temp) / "ambiguous.json"
            destination = reserve_evidence_output(path, required=True)
            assert destination is not None
            try:
                report = self.run_acceptance(
                    self.scenario(), fake, checkpoint=destination.write
                )
            finally:
                destination.close()
            evidence = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(report["overall"], "fail")
        self.assertEqual(evidence["last_checkpoint"]["phase"], "mutation_result")
        self.assertEqual(evidence["last_checkpoint"]["tool"], first)
        self.assertTrue(evidence["last_checkpoint"]["ambiguous"])
        self.assertFalse(evidence["last_checkpoint"]["automatic_replay"])
        states = [item["state"] for item in evidence["evidence_journal"]]
        self.assertTrue(
            any(
                state.get("last_checkpoint", {}).get("phase")
                == "mutation_attempt"
                and state["last_checkpoint"].get("tool") == first
                for state in states
            )
        )

    def test_durable_evidence_records_all_restores_and_uncertain_reread(self):
        scenario = self.scenario()
        first = self.surface.persistent_write_tools[0]
        first_operation = scenario["operations"][0]
        first_operation["restore"].append(dict(first_operation["restore"][0]))
        transport_reads = 0

        async def fake(name, _arguments):
            nonlocal transport_reads
            if name == "fl_get_project_summary":
                return self.project_summary()
            if name == "fl_get_transport_state":
                transport_reads += 1
                if transport_reads > 1 + len(scenario["operations"]):
                    raise TimeoutError("injected independent reread loss")
                return {"playing": False, "recording": False}
            return {"verified": True}

        with tempfile.TemporaryDirectory(prefix="postfader-checkpoints-") as temp:
            path = Path(temp) / "reread.json"
            destination = reserve_evidence_output(path, required=True)
            assert destination is not None
            try:
                report = self.run_acceptance(
                    scenario, fake, checkpoint=destination.write
                )
            finally:
                destination.close()
            evidence = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(report["overall"], "fail")
        states = [item["state"] for item in evidence["evidence_journal"]]
        restore_attempts = [
            state["last_checkpoint"]
            for state in states
            if state.get("last_checkpoint", {}).get("phase")
            == "restoration_attempt"
        ]
        self.assertEqual(
            [(item["tool"], item["restore_index"]) for item in restore_attempts],
            [(first, 0), (first, 1)],
        )
        self.assertEqual(
            evidence["last_checkpoint"]["phase"],
            "independent_restoration_reread_result",
        )
        self.assertEqual(evidence["last_checkpoint"]["status"], "uncertain")
        self.assertEqual(
            evidence["operations"][0]["restoration_status"], "uncertain"
        )

    def test_unverified_restore_stops_immediately_and_loudly(self):
        calls = []
        first = self.surface.persistent_write_tools[0]
        write_calls = 0

        async def fake(name, arguments):
            nonlocal write_calls
            calls.append((name, arguments))
            if name == "fl_get_project_summary":
                return self.project_summary()
            if name == "fl_get_transport_state":
                return {"playing": False, "recording": False}
            if name == first:
                write_calls += 1
                return {"verified": write_calls == 1}
            return {"verified": True}

        report = self.run_acceptance(self.scenario(), fake)
        self.assertEqual(report["overall"], "fail")
        self.assertEqual(report["operations"][0]["status"], "restore_unverified")
        self.assertIn("RESTORE UNVERIFIED", report["failures"][0]["reason"])
        self.assertEqual(write_calls, 2)
        self.assertEqual(len(report["operations"]), 1)

    def test_master_requires_per_tool_acknowledgement(self):
        scenario = self.scenario()
        first = "fl_set_mixer_volume"
        operation = next(item for item in scenario["operations"] if item["tool"] == first)
        operation["mutation_arguments"]["track_index"] = 0
        operation["mutation_arguments"]["allow_master"] = True
        operation["restore"][0]["arguments"]["track_index"] = 0
        operation["restore"][0]["arguments"]["allow_master"] = True
        calls = []
        report = self.run_acceptance(scenario, self.passing_caller(calls))
        self.assertEqual(report["overall"], "fail")
        self.assertIn("Master target", report["failures"][0]["reason"])

        calls.clear()
        report = self.run_acceptance(
            scenario,
            self.passing_caller(calls),
            acknowledged_master_tools=[first],
        )
        self.assertEqual(report["overall"], "pass")

    def test_nested_plugin_target_master_requires_acknowledgement(self):
        scenario = self.scenario()
        first = "fl_set_plugin_param"
        operation = next(item for item in scenario["operations"] if item["tool"] == first)
        operation["mutation_arguments"] = {
            "parameter_index": 0,
            "normalized_value": 0.5,
            "target": {"kind": "mixer_effect", "track_index": 0, "slot_index": 1}
        }
        operation["restore"][0]["arguments"] = dict(operation["mutation_arguments"])
        calls = []
        report = self.run_acceptance(scenario, self.passing_caller(calls))
        self.assertEqual(report["overall"], "fail")
        self.assertIn("Master target", report["failures"][0]["reason"])
        self.assertTrue(calls)
        self.assertFalse(
            any(name in self.surface.persistent_write_tools for name, _args in calls)
        )

    def test_schema_and_role_errors_are_refused_before_preflight(self):
        cases = []
        empty = self.scenario()
        next(item for item in empty["operations"] if item["tool"] == "fl_set_tempo")[
            "mutation_arguments"
        ] = {}
        cases.append((empty, "required property"))

        wrong_type = self.scenario()
        next(
            item for item in wrong_type["operations"] if item["tool"] == "fl_set_tempo"
        )["mutation_arguments"]["tempo_bpm"] = "fast"
        cases.append((wrong_type, "not of type"))

        write_before = self.scenario()
        write_before["operations"][0]["before"]["tool"] = "fl_set_tempo"
        cases.append((write_before, "not authoritatively read-only"))

        unknown_restore = self.scenario()
        unknown_restore["operations"][0]["restore"][0]["tool"] = "not_a_tool"
        cases.append((unknown_restore, "not an authoritative persistent-write tool"))

        malformed_restore = self.scenario()
        next(
            item
            for item in malformed_restore["operations"]
            if item["tool"] == "fl_set_tempo"
        )["restore"][0]["arguments"] = {"tempo_bpm": "slow"}
        cases.append((malformed_restore, "not of type"))

        for scenario, message in cases:
            with self.subTest(message=message):
                calls = []
                with self.assertRaisesRegex(AcceptanceConfigurationError, message):
                    self.run_acceptance(scenario, self.passing_caller(calls))
                self.assertEqual(calls, [])

    def test_late_resolved_schema_error_prevents_all_writes(self):
        scenario = self.scenario()
        tempo = next(
            item for item in scenario["operations"] if item["tool"] == "fl_set_tempo"
        )
        tempo["mutation_arguments"] = {"tempo_bpm": {"$before": "playing"}}
        calls = []
        report = self.run_acceptance(scenario, self.passing_caller(calls))
        self.assertEqual(report["overall"], "fail")
        self.assertEqual(report["phase"], "scenario_preparation")
        self.assertFalse(
            any(name in self.surface.persistent_write_tools for name, _args in calls)
        )

    def test_before_template_resolving_to_master_refuses_all_writes(self):
        scenario = self.scenario()
        operation = next(
            item for item in scenario["operations"] if item["tool"] == "fl_set_mixer_volume"
        )
        operation["mutation_arguments"] = {
            "track_index": {"$before": "track_index"},
            "volume_normalized": 0.7,
            "allow_master": True,
        }
        operation["restore"][0]["arguments"] = {
            "track_index": {"$before": "track_index"},
            "volume_normalized": 0.8,
            "allow_master": True,
        }
        operation["verify_paths"] = ["track_index"]
        calls = []

        async def fake(name, arguments):
            calls.append((name, dict(arguments)))
            if name == "fl_get_project_summary":
                return self.project_summary()
            if name == "fl_get_transport_state":
                return {"playing": False, "recording": False, "track_index": 0}
            return {"verified": True}

        report = self.run_acceptance(scenario, fake)
        self.assertEqual(report["overall"], "fail")
        self.assertIn("Master target", report["failures"][0]["reason"])
        self.assertFalse(
            any(name in self.surface.persistent_write_tools for name, _args in calls)
        )

    def test_versioned_fixture_fully_resolves_all_direct_writes_without_io(self):
        scenario = json.loads(
            (ROOT / "tests" / "fixtures" / "write-scenario-v1.json").read_text(
                encoding="utf-8"
            )
        )
        prepared = validate_write_scenario_plan(self.surface, scenario)
        self.assertEqual(len(prepared), len(self.surface.persistent_write_tools))
        step = next(item for item in prepared if item.tool == "fl_set_step_sequence")
        self.assertEqual(
            step.restore_actions[0][1]["expected_digest"],
            "e729573f357c04daf32a05078da93e10ac32019fc638c2a422f11dea58a9589a",
        )

        identity = next(
            item for item in prepared if item.tool == "fl_set_channel_identity"
        )
        self.assertEqual(identity.mutation_arguments["color"], 0xFF1480FF)

        position = next(
            item for item in prepared if item.tool == "fl_set_song_position"
        )
        self.assertEqual(position.mutation_arguments["position_normalized"], 0.5)
        self.assertEqual(position.mutation_arguments["tolerance"], 0.001)

    def test_mixer_route_restoration_uses_destination_identity_not_position(self):
        scenario = json.loads(
            (ROOT / "tests" / "fixtures" / "write-scenario-v1.json").read_text(
                encoding="utf-8"
            )
        )
        prepared = validate_write_scenario_plan(self.surface, scenario)
        send = next(item for item in prepared if item.tool == "fl_set_mixer_send")
        level = next(
            item for item in prepared if item.tool == "fl_set_mixer_send_level"
        )
        self.assertTrue(send.restore_actions[0][1]["enabled"])
        self.assertEqual(send.restore_actions[1][1]["level_normalized"], 0.8)
        self.assertEqual(level.restore_actions[0][1]["level_normalized"], 0.8)

        target_level = level.verify_specs[-1]
        wrong_destination_only = {
            "routes": [
                {"destination_track_index": 0, "level_normalized": 0.8},
                {"destination_track_index": 2, "level_normalized": 0.5},
            ]
        }
        self.assertEqual(
            resolve_evidence_reference(target_level, level.before), 0.8
        )
        self.assertEqual(
            resolve_evidence_reference(target_level, wrong_destination_only), 0.5
        )
        self.assertNotEqual(
            resolve_evidence_reference(target_level, level.before),
            resolve_evidence_reference(target_level, wrong_destination_only),
        )

    def test_missing_or_ambiguous_route_refuses_before_every_mutation(self):
        cases = (
            ([], 0),
            (
                [
                    {"destination_track_index": 2, "level_normalized": 0.8},
                    {"destination_track_index": 2, "level_normalized": 0.7},
                ],
                2,
            ),
        )
        for routes, match_count in cases:
            with self.subTest(match_count=match_count):
                scenario = self.scenario()
                route = next(
                    item
                    for item in scenario["operations"]
                    if item["tool"] == "fl_set_mixer_send_level"
                )
                selector = {
                    "$select": {
                        "path": "routes",
                        "where": {"destination_track_index": 2},
                        "value": "level_normalized",
                    }
                }
                route["restore"][0]["arguments"]["level_normalized"] = selector
                route["verify_paths"] = [selector]
                calls = []

                async def fake(name, arguments):
                    calls.append((name, dict(arguments)))
                    if name == "fl_get_project_summary":
                        return self.project_summary()
                    if name == "fl_get_transport_state":
                        return {
                            "playing": False,
                            "recording": False,
                            "routes": routes,
                        }
                    return {"verified": True}

                report = self.run_acceptance(scenario, fake)
                self.assertEqual(report["overall"], "fail")
                self.assertIn(
                    "matched %d entries" % match_count,
                    report["failures"][0]["reason"],
                )
                self.assertFalse(
                    any(
                        name in self.surface.persistent_write_tools
                        for name, _args in calls
                    )
                )

    def test_playback_position_is_restored_before_a_later_failure(self):
        scenario = self.scenario()
        playback = next(
            item for item in scenario["operations"] if item["tool"] == "fl_set_playing"
        )
        playback["mutation_arguments"] = {"playing": True}
        playback["restore"] = [
            {"arguments": {"playing": False}},
            {
                "tool": "fl_set_song_position",
                "arguments": {
                    "position_normalized": {"$before": "song_position_normalized"}
                },
            },
        ]
        playback["verify_paths"] = ["playing", "song_position_normalized"]
        state = {
            "playing": False,
            "recording": False,
            "song_position_normalized": 0.25,
        }

        async def fake(name, arguments):
            if name == "fl_get_project_summary":
                return self.project_summary()
            if name == "fl_get_transport_state":
                return dict(state)
            if name == "fl_set_playing":
                state["playing"] = arguments["playing"]
                if arguments["playing"]:
                    state["song_position_normalized"] = 0.35
                return {"verified": True}
            if name == "fl_set_song_position":
                state["song_position_normalized"] = arguments[
                    "position_normalized"
                ]
                return {"verified": True}
            if name == "fl_set_plugin_param":
                raise TimeoutError("injected later failure")
            return {"verified": True}

        report = self.run_acceptance(scenario, fake)
        self.assertEqual(report["overall"], "fail")
        playback_result = next(
            item for item in report["operations"] if item["tool"] == "fl_set_playing"
        )
        self.assertEqual(playback_result["status"], "passed")
        self.assertFalse(state["playing"])
        self.assertEqual(state["song_position_normalized"], 0.25)

    def test_playback_restore_cannot_pass_at_the_advanced_position(self):
        scenario = self.scenario()
        playback = next(
            item for item in scenario["operations"] if item["tool"] == "fl_set_playing"
        )
        playback["mutation_arguments"] = {"playing": True}
        playback["restore"] = [
            {"arguments": {"playing": False}},
            {
                "tool": "fl_set_song_position",
                "arguments": {
                    "position_normalized": {"$before": "song_position_normalized"}
                },
            },
        ]
        playback["verify_paths"] = ["playing", "song_position_normalized"]
        state = {
            "playing": False,
            "recording": False,
            "song_position_normalized": 0.25,
        }

        async def fake(name, arguments):
            if name == "fl_get_project_summary":
                return self.project_summary()
            if name == "fl_get_transport_state":
                return dict(state)
            if name == "fl_set_playing":
                state["playing"] = arguments["playing"]
                if arguments["playing"]:
                    state["song_position_normalized"] = 0.35
                return {"verified": True}
            if name == "fl_set_song_position":
                # Inject a lying transport result: independent evidence must win.
                return {"verified": True}
            return {"verified": True}

        report = self.run_acceptance(scenario, fake)
        playback_result = next(
            item for item in report["operations"] if item["tool"] == "fl_set_playing"
        )
        self.assertEqual(report["overall"], "fail")
        self.assertEqual(playback_result["status"], "failed")
        self.assertIn(
            "song_position_normalized",
            playback_result["restoration_mismatches"],
        )
        self.assertEqual(state["song_position_normalized"], 0.35)

    def test_public_fixture_template_is_not_live_eligible(self):
        scenario = json.loads(
            (ROOT / "tests" / "fixtures" / "write-scenario-v1.json").read_text(
                encoding="utf-8"
            )
        )
        calls = []
        with self.assertRaisesRegex(
            AcceptanceConfigurationError, "REVIEWED_FOR_THIS_DISPOSABLE_PROJECT"
        ):
            self.run_acceptance(scenario, self.passing_caller(calls))
        self.assertEqual(calls, [])


def _load_script(name):
    path = ROOT / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location("test_" + name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class LiveScriptSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scripts = {
            name: _load_script(name)
            for name in (
                "live_read_acceptance",
                "live_write_acceptance",
                "live_note_acceptance",
            )
        }

    def test_existing_output_refuses_before_any_live_async_call(self):
        with tempfile.TemporaryDirectory(prefix="postfader-evidence-") as temp:
            existing = Path(temp) / "existing.json"
            existing.write_text("DO NOT REPLACE", encoding="utf-8")
            arguments = {
                "live_read_acceptance": ["--output", os.fspath(existing)],
                "live_write_acceptance": [
                    "--scenario",
                    os.fspath(Path(temp) / "not-read.json"),
                    "--output",
                    os.fspath(existing),
                ],
                "live_note_acceptance": [
                    "--channel",
                    "0",
                    "--output",
                    os.fspath(existing),
                ],
            }
            for name, module in self.scripts.items():
                with self.subTest(script=name):
                    async_main = mock.AsyncMock(
                        side_effect=AssertionError("live async call reached")
                    )
                    with (
                        mock.patch.object(module, "async_main", async_main),
                        mock.patch.object(module.sys, "stderr", io.StringIO()),
                    ):
                        status = module.main(arguments[name])
                    self.assertEqual(status, 2)
                    async_main.assert_not_called()
                    self.assertEqual(
                        existing.read_text(encoding="utf-8"), "DO NOT REPLACE"
                    )

    def test_missing_fixture_status_refuses_before_live_setup_or_async_calls(self):
        module = self.scripts["live_write_acceptance"]
        scenario = json.loads(
            (ROOT / "tests" / "fixtures" / "write-scenario-v1.json").read_text(
                encoding="utf-8"
            )
        )
        del scenario["fixture_status"]
        with tempfile.TemporaryDirectory(prefix="postfader-evidence-") as temp:
            scenario_path = Path(temp) / "missing-marker.json"
            output_path = Path(temp) / "evidence.json"
            scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
            async_main = mock.AsyncMock(
                side_effect=AssertionError("live async call reached")
            )
            with (
                mock.patch.object(module, "async_main", async_main),
                mock.patch.object(
                    module,
                    "configure_acceptance_transport",
                    side_effect=AssertionError("live transport setup reached"),
                ) as configure,
                mock.patch.object(module.sys, "stderr", io.StringIO()),
            ):
                status = module.main(
                    [
                        "--scenario",
                        os.fspath(scenario_path),
                        "--output",
                        os.fspath(output_path),
                    ]
                )
            self.assertEqual(status, 2)
            async_main.assert_not_called()
            configure.assert_not_called()
            evidence = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertFalse(evidence["contact_started"])
            self.assertEqual(evidence["phase"], "scenario_eligibility")

    def test_uncreatable_output_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="postfader-evidence-") as temp:
            impossible = Path(temp) / "missing-parent" / "evidence.json"
            with self.assertRaises(EvidenceOutputError):
                reserve_evidence_output(impossible, required=True)

    def test_failed_atomic_checkpoint_preserves_last_durable_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="postfader-evidence-") as temp:
            path = Path(temp) / "evidence.json"
            destination = reserve_evidence_output(path, required=True)
            assert destination is not None
            destination.write(
                {
                    "schema_version": 1,
                    "phase": "last_durable_phase",
                    "project_saved": False,
                }
            )
            before = path.read_bytes()
            with mock.patch(
                "fl_studio_mcp.evidence.os.replace",
                side_effect=OSError("injected replacement failure"),
            ):
                with self.assertRaises(EvidenceOutputError):
                    destination.write(
                        {
                            "schema_version": 1,
                            "phase": "must_not_replace_last_state",
                            "project_saved": False,
                        }
                    )
            destination.close()
            self.assertEqual(path.read_bytes(), before)
            evidence = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(evidence["phase"], "last_durable_phase")
            self.assertFalse(evidence["project_saved"])

    def test_atomic_checkpoint_retries_a_transient_permission_error(self):
        with tempfile.TemporaryDirectory(prefix="postfader-evidence-") as temp:
            path = Path(temp) / "evidence.json"
            destination = reserve_evidence_output(path, required=True)
            assert destination is not None
            real_replace = os.replace
            attempts = []

            def transient_replace(source, target):
                attempts.append((source, target))
                if len(attempts) == 1:
                    raise PermissionError("injected transient file lock")
                return real_replace(source, target)

            with (
                mock.patch(
                    "fl_studio_mcp.evidence.os.replace",
                    side_effect=transient_replace,
                ),
                mock.patch("fl_studio_mcp.evidence.time.sleep") as sleep,
            ):
                destination.write(
                    {
                        "schema_version": 1,
                        "phase": "retry_succeeded",
                        "project_saved": False,
                    }
                )
            destination.close()

            self.assertEqual(len(attempts), 2)
            sleep.assert_called_once()
            evidence = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(evidence["phase"], "retry_succeeded")

    def test_atomic_checkpoint_journal_does_not_duplicate_full_snapshots(self):
        with tempfile.TemporaryDirectory(prefix="postfader-evidence-") as temp:
            path = Path(temp) / "evidence.json"
            destination = reserve_evidence_output(path, required=True)
            assert destination is not None
            payload = "x" * (64 * 1024)
            for index in range(20):
                destination.write(
                    {
                        "schema_version": 1,
                        "phase": "checkpoint_%d" % index,
                        "last_checkpoint": {
                            "phase": "mutation_result",
                            "operation_index": index,
                        },
                        "large_live_state": payload,
                        "project_saved": False,
                    }
                )
            destination.close()
            evidence = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                evidence["evidence_format"], "atomic_snapshot_compact_journal_v2"
            )
            self.assertEqual(evidence["phase"], "checkpoint_19")
            self.assertEqual(len(evidence["evidence_journal"]), 21)
            self.assertLess(path.stat().st_size, 256 * 1024)

    def test_read_contact_checkpoint_failure_refuses_before_async_execution(self):
        module = self.scripts["live_read_acceptance"]

        class FailingContactWrite:
            path = Path("fake-read-contact-evidence.json")

            def __init__(self):
                self.closed = False

            def write(self, _value):
                raise EvidenceOutputError("injected contact checkpoint failure")

            def close(self):
                self.closed = True

        destination = FailingContactWrite()
        async_main = mock.AsyncMock(
            side_effect=AssertionError("live async execution reached")
        )
        stderr = io.StringIO()
        with (
            mock.patch.object(
                module, "reserve_evidence_output", return_value=destination
            ),
            mock.patch.object(module, "configure_acceptance_transport"),
            mock.patch.object(module, "async_main", async_main),
            mock.patch.object(module.sys, "stderr", stderr),
        ):
            status = module.main(["--output", "unused.json"])
        self.assertEqual(status, 2)
        async_main.assert_not_called()
        self.assertTrue(destination.closed)
        self.assertIn("contact_started_checkpoint", stderr.getvalue())

    def test_final_read_evidence_write_failure_is_clean_and_nonzero(self):
        module = self.scripts["live_read_acceptance"]

        class FailingFinalWrite:
            path = Path("fake-final-read-evidence.json")

            def __init__(self):
                self.write_count = 0
                self.closed = False

            def write(self, _value):
                self.write_count += 1
                if self.write_count == 2:
                    raise EvidenceOutputError("injected final read write failure")

            def close(self):
                self.closed = True

        destination = FailingFinalWrite()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                module, "reserve_evidence_output", return_value=destination
            ),
            mock.patch.object(module, "configure_acceptance_transport"),
            mock.patch.object(
                module,
                "async_main",
                mock.AsyncMock(
                    return_value={"overall": "pass", "project_saved": False}
                ),
            ),
            mock.patch.object(module.sys, "stderr", stderr),
        ):
            status = module.main(["--output", "unused.json"])
        self.assertEqual(status, 1)
        self.assertTrue(destination.closed)
        self.assertIn("final_evidence_output", stderr.getvalue())
        self.assertIn("final_evidence_write", stderr.getvalue())
        self.assertIn("injected final read write failure", stderr.getvalue())

    def test_read_cli_rejects_nonpositive_or_nonfinite_deadlines(self):
        module = self.scripts["live_read_acceptance"]
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(value=value):
                with (
                    mock.patch.object(module.sys, "stderr", io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    module.parse_args(
                        ["--plan", "--per-tool-timeout-seconds", value]
                    )
                self.assertEqual(raised.exception.code, 2)

    def test_final_read_evidence_close_failure_is_clean_and_nonzero(self):
        module = self.scripts["live_read_acceptance"]

        class FailingClose:
            path = Path("fake-final-read-evidence.json")

            def write(self, _value):
                return None

            def close(self):
                raise EvidenceOutputError("injected final read close failure")

        stderr = io.StringIO()
        with mock.patch.object(module.sys, "stderr", stderr):
            status = module._finish(
                FailingClose(),
                {"overall": "pass", "project_saved": False},
                contact_started=True,
            )
        self.assertEqual(status, 1)
        self.assertIn("final_evidence_close", stderr.getvalue())
        self.assertIn("injected final read close failure", stderr.getvalue())

    def test_final_evidence_write_failure_is_clean_and_nonzero(self):
        module = self.scripts["live_write_acceptance"]

        class FailingFinalWrite:
            path = Path("fake-final-evidence.json")

            def __init__(self):
                self.write_count = 0
                self.closed = False

            def write(self, _value):
                self.write_count += 1
                if self.write_count == 2:
                    raise EvidenceOutputError("injected final write failure")

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory(prefix="postfader-evidence-") as temp:
            scenario_path = Path(temp) / "reviewed.json"
            scenario_path.write_text(
                json.dumps(
                    {
                        "scenario_version": 1,
                        "fixture_status": "REVIEWED_FOR_THIS_DISPOSABLE_PROJECT",
                        "safe_to_edit": True,
                    }
                ),
                encoding="utf-8",
            )
            destination = FailingFinalWrite()
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    module, "reserve_evidence_output", return_value=destination
                ),
                mock.patch.object(module, "configure_acceptance_transport"),
                mock.patch.object(
                    module,
                    "async_main",
                    mock.AsyncMock(
                        return_value={
                            "overall": "pass",
                            "project_saved": False,
                        }
                    ),
                ),
                mock.patch.object(module.sys, "stderr", stderr),
            ):
                status = module.main(
                    [
                        "--scenario",
                        os.fspath(scenario_path),
                        "--output",
                        os.fspath(Path(temp) / "unused.json"),
                    ]
                )
        self.assertEqual(status, 1)
        self.assertTrue(destination.closed)
        self.assertIn("final_evidence_output", stderr.getvalue())
        self.assertIn("final_evidence_write", stderr.getvalue())
        self.assertIn("injected final write failure", stderr.getvalue())

    def test_final_evidence_close_failure_is_clean_and_nonzero(self):
        module = self.scripts["live_write_acceptance"]

        class FailingClose:
            path = Path("fake-final-evidence.json")

            def write(self, _value):
                return None

            def close(self):
                raise EvidenceOutputError("injected final close failure")

        destination = FailingClose()
        stderr = io.StringIO()
        with mock.patch.object(module.sys, "stderr", stderr):
            status = module._finish(
                destination,
                {"overall": "pass", "project_saved": False},
                contact_started=True,
            )
        self.assertEqual(status, 1)
        self.assertIn("final_evidence_close", stderr.getvalue())
        self.assertIn("injected final close failure", stderr.getvalue())

    def test_plan_transport_is_disabled_even_with_explicit_query(self):
        with mock.patch.dict(
            os.environ,
            {"FL_BRIDGE_ENABLE_MIDI": "1", "FL_BRIDGE_MIDI_PORT": "Ambient"},
            clear=False,
        ):
            query = configure_acceptance_transport("Reviewed Port", live=False)
            self.assertEqual(query, "Reviewed Port")
            self.assertEqual(os.environ["FL_BRIDGE_ENABLE_MIDI"], "0")
            self.assertNotIn("FL_BRIDGE_MIDI_PORT", os.environ)

    def test_explicit_live_transport_is_configured_exactly(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            query = configure_acceptance_transport("  Exact Endpoint  ", live=True)
            self.assertEqual(query, "Exact Endpoint")
            self.assertEqual(os.environ["FL_BRIDGE_ENABLE_MIDI"], "1")
            self.assertEqual(os.environ["FL_BRIDGE_MIDI_PORT"], "Exact Endpoint")


if __name__ == "__main__":
    unittest.main(verbosity=2)
