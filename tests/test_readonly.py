"""Security and contract tests for the inspector, the writer, and the MCP surface."""

from __future__ import annotations

import asyncio
import copy
import math
import os
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "fakefl"))
sys.path.insert(0, os.path.join(ROOT, "fl_studio_mcp", "_bridge"))
sys.path.insert(0, ROOT)

import _state  # noqa: E402
import device_UniversalBridge as bridge  # noqa: E402

from fl_studio_mcp.bridge_client import BridgeClient, BridgeError, MAX_WIRE_ID  # noqa: E402
from fl_studio_mcp.bridge_install import (  # noqa: E402
    BridgeInstallError,
    expected_bridge_deployment,
)
from fl_studio_mcp.contracts import (  # noqa: E402
    CapabilityStatus,
    ConnectionInfo,
    ExpectedEqBandState,
    ExpectedPluginParameterState,
    MixerTrackSummary,
    PluginParameter,
    SelectedRangeObservation,
    VerifiedMixerEqWrite,
    VerifiedMixerMuteWrite,
    VerifiedMixerNameWrite,
    VerifiedMixerPanWrite,
    VerifiedMixerSendLevelWrite,
    VerifiedMixerSendWrite,
    VerifiedMixerVolumeWrite,
    VerifiedPluginDisplayWrite,
    VerifiedPluginOptionWrite,
    VerifiedPluginParameterWrite,
    WriteModeChange,
)
from fl_studio_mcp.readonly_inspector import (  # noqa: E402
    IncompatibleFLStudio,
    ReadOnlyGateway,
    ReadOnlyInspector,
    ReadOnlyViolation,
)
from fl_studio_mcp.mcp_server import mcp  # noqa: E402
from fl_studio_mcp.verified_writer import (  # noqa: E402
    VerifiedWriter,
    VerifiedWritesUnavailable,
    WriteBoundaryViolation,
    WriteGateway,
    WriteModeBoundaryViolation,
    WriteModeConfirmationRequired,
    WriteModeGateway,
    WriteModeManager,
    WriteModeUnavailable,
)
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from pydantic import ValidationError  # noqa: E402


class DirectFakeClient:
    transport = "midi"

    def ping(self):
        return bridge.cmd_ping({})

    def call(self, cmd, **args):
        handler = bridge.HANDLERS[cmd]
        result = handler(args)
        if isinstance(result, types.GeneratorType):
            while True:
                try:
                    next(result)
                except StopIteration as stopped:
                    return stopped.value
        return result


class FL2025Client(DirectFakeClient):
    def ping(self):
        return {
            "pong": True,
            "protocol": 1,
            "program_title": "FL Studio 2025",
            "fl_version": "Producer Edition v25.2.5 [build 5055]",
            "midi_scripting_api_version": 37,
        }


class ConfigurablePingClient(DirectFakeClient):
    def __init__(self, response):
        self.response = response

    def ping(self):
        return dict(self.response)


class RefuseDispatchClient(ConfigurablePingClient):
    """A configurable handshake whose command path is a test tripwire."""

    def __init__(self, response):
        super().__init__(response)
        self.commands = []

    def call(self, cmd, **args):
        self.commands.append((cmd, dict(args)))
        raise AssertionError("a rejected handshake reached %r" % cmd)


class DownClient(DirectFakeClient):
    def ping(self):
        raise BridgeError("fixture disconnected")


# The handshakes a bridge produces with its bounded write surface enabled and
# disabled. Only the bridge decides the actual state; the host must verify it.
EXPECTED_BRIDGE_DIGEST = expected_bridge_deployment()[1]
SESSION_FINGERPRINT = bridge.SESSION_FINGERPRINT
WRITE_ENABLED_PING = {
    "pong": True,
    "protocol": 2,
    "program_title": "FL Studio 2026",
    "fl_version": "Producer Edition v26.1.3 [build 5336]",
    "midi_scripting_api_version": 44,
    "bridge_mode": "write_test",
    "verified_writes_enabled": True,
    "runtime_write_mode_control": True,
    "write_mode_origin": "startup_environment",
    "startup_write_mode_enabled": True,
    "bridge_source_sha256": EXPECTED_BRIDGE_DIGEST,
    "session_fingerprint": SESSION_FINGERPRINT,
}
WRITES_DISABLED_PING = dict(
    WRITE_ENABLED_PING,
    bridge_mode="read_only",
    verified_writes_enabled=False,
    write_mode_origin="disabled",
    startup_write_mode_enabled=False,
)


class WriteEnabledFakeClient(DirectFakeClient):
    """The fake bridge as it looks with the write flag set, plus a call log."""

    def __init__(self):
        self.commands = []

    def ping(self):
        return dict(WRITE_ENABLED_PING)

    def call(self, cmd, **args):
        self.commands.append((cmd, dict(args)))
        return super().call(cmd, **args)


class PreBasisWriteEnabledClient(WriteEnabledFakeClient):
    """The first protocol-2 bridge, before it added verification_basis."""

    def call(self, cmd, **args):
        result = super().call(cmd, **args)
        if cmd == "plugin.set_param":
            result.pop("verification_basis", None)
        return result


class WritesDisabledClient(DirectFakeClient):
    """A bridge FL started without the write flag; any dispatch is a bug here."""

    def ping(self):
        return dict(WRITES_DISABLED_PING)

    def call(self, cmd, **args):
        raise AssertionError(
            "the writer dispatched %r to a bridge that cannot write" % cmd
        )


class RuntimeModeClient:
    """Stateful protocol double for one runtime write-mode bridge session."""

    transport = "midi"

    def __init__(
        self,
        *,
        enabled=False,
        startup_default=False,
        ping_overrides=None,
        reply_overrides=None,
    ):
        self.enabled = enabled
        self.startup_default = startup_default
        self.origin = (
            "startup_environment"
            if enabled and startup_default
            else "runtime_request"
            if enabled
            else "disabled"
        )
        self.ping_overrides = dict(ping_overrides or {})
        self.reply_overrides = dict(reply_overrides or {})
        self.commands = []
        self.ping_count = 0

    def ping(self):
        self.ping_count += 1
        response = dict(
            WRITE_ENABLED_PING,
            bridge_mode="write_test" if self.enabled else "read_only",
            verified_writes_enabled=self.enabled,
            write_mode_origin=self.origin,
            startup_write_mode_enabled=self.startup_default,
        )
        response.update(self.ping_overrides)
        return response

    def call(self, cmd, **args):
        self.commands.append((cmd, dict(args)))
        if cmd != "session.set_write_mode":
            raise AssertionError("unexpected command %r" % cmd)
        before = self.enabled
        self.enabled = args["enabled"]
        self.origin = "runtime_request" if self.enabled else "disabled"
        response = {
            "command": cmd,
            "requested_enabled": self.enabled,
            "before_enabled": before,
            "after_enabled": self.enabled,
            "changed": before != self.enabled,
            "bridge_mode": "write_test" if self.enabled else "read_only",
            "write_mode_origin": self.origin,
            "runtime_write_mode_control": True,
            "confirmation_required": self.enabled,
            "confirmation_applied": self.enabled,
            "session_fingerprint": SESSION_FINGERPRINT,
            "session_precondition_applied": True,
            "session_only": True,
            "startup_default_enabled": self.startup_default,
            "project_saved": False,
        }
        response.update(self.reply_overrides)
        return response


def state_fingerprint():
    return copy.deepcopy(
        {
            "tracks": [
                {
                    "name": track.name,
                    "volume": track.volume,
                    "pan": track.pan,
                    "stereo_sep": track.stereo_sep,
                    "muted": track.muted,
                    "solo": track.solo,
                    "armed": track.armed,
                    "selected": track.selected,
                    "enabled": track.enabled,
                    "slots_enabled": track.slots_enabled,
                    "polarity_reversed": track.rev_polarity,
                    "channels_swapped": track.swap_channels,
                    "colour": track.color,
                    "eq": track.eq,
                    "routes": track.routes,
                    "slots": {
                        slot: {
                            "name": plugin.name,
                            "names": plugin.param_names,
                            "values": plugin.values,
                        }
                        for slot, plugin in track.slots.items()
                    },
                    "slot_mix": track.slot_mix,
                }
                for track in _state.TRACKS
            ],
            "channels": [vars(channel) for channel in _state.CHANNELS],
            "undo": _state.UNDO,
            "playing": _state.PLAYING,
            "recording": _state.RECORDING,
            "song_pos": _state.SONG_POS,
            "song_pos_ticks": _state.SONG_POS_TICKS,
            "selection": (_state.SELECTION_START, _state.SELECTION_END),
            "loop_mode": _state.LOOP_MODE,
            "ppq": _state.REC_PPQ,
            "tempo": _state.TEMPO,
        }
    )


class ReadOnlyInspectorTests(unittest.TestCase):
    def test_sandboxed_midi_preflight_does_not_spawn_crash_probe(self):
        from fl_studio_mcp import bridge_client

        bridge_client._MIDI_PREFLIGHT.clear()
        with (
            mock.patch.object(bridge_client, "MIDI_ENABLED", True),
            mock.patch.dict(
                os.environ,
                {"FL_BRIDGE_SANDBOXED": "1"},
                clear=False,
            ),
            mock.patch.object(bridge_client.subprocess, "run") as run,
        ):
            self.assertFalse(bridge_client._midi_preflight("IAC Driver"))
        run.assert_not_called()

    def setUp(self):
        _state.reset()
        self.gateway = ReadOnlyGateway(DirectFakeClient())
        self.inspector = ReadOnlyInspector(self.gateway)

    def test_gateway_rejects_every_non_allowlisted_operation(self):
        for command in (
            "mixer.set_volume",
            "mixer.set_pan",
            "mixer.set_mute",
            "mixer.set_eq",
            "mixer.set_name",
            "mixer.set_send",
            "mixer.set_send_level",
            "plugin.set_param",
            "plugin.set_param_display",
            "plugin.set_param_option",
            "call",
        ):
            with self.subTest(command=command):
                with self.assertRaises(ReadOnlyViolation):
                    self.gateway.call(command)

    def test_bridge_dispatcher_is_independently_locked_read_only(self):
        before = state_fingerprint()
        response = bridge._dispatch(
            {"id": 9, "cmd": "mixer.set_volume",
             "args": {"track": 3, "value": 0.1}}
        )
        self.assertFalse(response["ok"])
        self.assertIn("locked read-only", response["error"])
        self.assertNotIn("mixer.set_volume", response["available"])
        self.assertEqual(before, state_fingerprint())

    def test_complete_capture_does_not_mutate_fake_fl_state(self):
        before = state_fingerprint()
        report = self.inspector.capture(parameter_limit=8, max_plugins=8)
        after = state_fingerprint()
        self.assertEqual(before, after)
        self.assertEqual(report.mode, "read_only")
        self.assertEqual(report.project.connection.program_title, "FL Studio 2026")
        self.assertEqual(report.project.project_title, "Synthetic Test Project")
        self.assertTrue(any(t.name == "Lead Vox" for t in report.mixer.tracks))
        self.assertGreaterEqual(len(report.parameter_previews), 1)
        self.assertTrue(
            all(
                not parameter.safe_to_modify
                for page in report.parameter_previews
                for parameter in page.parameters
            )
        )

    def test_dated_4_4_shapes_stay_raw_without_runtime_fixture_identity(self):
        cases = (
            (96, -1, -1, 0),
            (96, 2688, -1, 1),
            (96, 384, 1152, 1),
            (96, 0, 768, 1),
            (96, 1728, 1920, 1),
            (192, 5568, -1, 1),
            (192, 576, 1344, 1),
            (192, 768, 2304, 1),
            (192, 3456, 3840, 1),
        )
        for ppq, start, end, loop_mode in cases:
            with self.subTest(ppq=ppq, start=start, end=end):
                _state.REC_PPQ = ppq
                _state.SELECTION_START = start
                _state.SELECTION_END = end
                _state.LOOP_MODE = loop_mode
                before = state_fingerprint()
                result = self.inspector.selected_range()
                self.assertEqual(before, state_fingerprint())
                self.assertEqual(
                    (result.raw_start_time, result.raw_end_time), (start, end)
                )
                self.assertEqual(result.selection_state, "unknown")
                self.assertEqual(result.selection_presence, "unknown")
                self.assertEqual(result.interpretation_status, "unvalidated")
                self.assertIsNone(result.semantic_scope)
                self.assertEqual(result.timebase_ppq, ppq)
                self.assertEqual(result.raw_time_unit, "unknown")
                self.assertIsNone(result.start_ticks)
                self.assertIsNone(result.end_ticks)
                self.assertIsNone(result.duration_ticks)
                self.assertIsNone(result.range_order)
                self.assertEqual(result.render_endpoint_inclusivity, "unknown")
                self.assertFalse(result.safe_for_rendering)
                self.assertTrue(
                    any("getRecPPB" in warning for warning in result.warnings)
                )
                self.assertTrue(
                    any(
                        "meter-independent" in warning
                        for warning in result.warnings
                    )
                )
                if start < 0:
                    self.assertIsNone(result.raw_start_display_hint)
                if end < 0:
                    self.assertIsNone(result.raw_end_display_hint)

    def test_uninterpreted_endpoint_shapes_are_preserved_raw(self):
        _state.LOOP_MODE = 1
        for ppq in (96, 192):
            for start, end in (
                (-1, 384),
                (384, -2),
                (-2, -1),
                (384, 384),
                (900, 300),
            ):
                with self.subTest(ppq=ppq, start=start, end=end):
                    _state.REC_PPQ = ppq
                    _state.SELECTION_START = start
                    _state.SELECTION_END = end
                    result = self.inspector.selected_range()
                    self.assertEqual(
                        (result.raw_start_time, result.raw_end_time), (start, end)
                    )
                    self.assertEqual(result.interpretation_status, "unvalidated")
                    self.assertEqual(result.selection_state, "unknown")
                    self.assertIsNone(result.start_ticks)

    def test_unvalidated_scope_keeps_raw_selection_without_normalized_ticks(self):
        base_ping = {
            "pong": True,
            "protocol": 2,
            "program_title": "FL Studio 2026",
            "fl_version": "Producer Edition v26.1.3 [build 5336]",
            "midi_scripting_api_version": 44,
            "bridge_mode": "read_only",
        }
        _state.SELECTION_START = 384
        _state.SELECTION_END = 768
        _state.LOOP_MODE = 1
        responses = []
        for change in (
            {"fl_version": "Producer Edition v26.1.4 [build 5337]"},
            {"midi_scripting_api_version": 45},
            {"protocol": 1, "bridge_mode": "legacy_unknown"},
        ):
            response = dict(base_ping)
            response.update(change)
            responses.append(response)
        for response in responses:
            with self.subTest(response=response):
                result = ReadOnlyInspector(
                    ReadOnlyGateway(ConfigurablePingClient(response))
                ).selected_range()
                self.assertEqual(result.interpretation_status, "unvalidated")
                self.assertEqual(result.selection_state, "unknown")
                self.assertEqual(result.selection_presence, "unknown")
                self.assertEqual(result.raw_time_unit, "unknown")
                self.assertIsNone(result.semantic_scope)
                self.assertIsNone(result.start_ticks)

        class UnsupportedPPQClient(DirectFakeClient):
            def call(self, cmd, **args):
                if cmd == "arrangement.selection":
                    return {
                        "first_raw_start": 1536,
                        "first_raw_end": 3072,
                        "first_ppq": 384,
                        "second_raw_start": 1536,
                        "second_raw_end": 3072,
                        "second_ppq": 384,
                        "start_hint": "2:01:00",
                        "end_hint": "3:01:00",
                    }
                return super().call(cmd, **args)

        result = ReadOnlyInspector(
            ReadOnlyGateway(UnsupportedPPQClient())
        ).selected_range()
        self.assertEqual(result.interpretation_status, "unvalidated")
        self.assertEqual(result.raw_time_unit, "unknown")
        self.assertIsNone(result.start_ticks)

        class TornPPQClient(DirectFakeClient):
            def call(self, cmd, **args):
                if cmd == "arrangement.selection":
                    return {
                        "first_raw_start": 576,
                        "first_raw_end": 1344,
                        "first_ppq": 192,
                        "second_raw_start": 576,
                        "second_raw_end": 1344,
                        "second_ppq": 192,
                        "start_hint": "1:13:00",
                        "end_hint": "2:13:00",
                    }
                return super().call(cmd, **args)

        _state.REC_PPQ = 96
        result = ReadOnlyInspector(ReadOnlyGateway(TornPPQClient())).selected_range()
        self.assertEqual(result.interpretation_status, "unvalidated")
        self.assertEqual(result.raw_time_unit, "unknown")
        self.assertIsNone(result.semantic_scope)

    def test_ppq192_unset_state_remains_raw_and_unvalidated(self):
        _state.REC_PPQ = 192
        _state.SELECTION_START = -1
        _state.SELECTION_END = -1
        _state.LOOP_MODE = 0
        result = self.inspector.selected_range()
        self.assertEqual(result.interpretation_status, "unvalidated")
        self.assertEqual(result.selection_state, "unknown")
        self.assertEqual(result.selection_presence, "unknown")
        self.assertEqual(result.raw_time_unit, "unknown")
        self.assertIsNone(result.semantic_scope)
        self.assertIsNone(result.start_ticks)

    def test_playing_recording_or_untested_mode_keeps_selection_unvalidated(self):
        _state.SELECTION_START = 384
        _state.SELECTION_END = 768
        cases = (
            (True, False, 1),
            (False, True, 1),
            (False, False, 0),
        )
        for playing, recording, loop_mode in cases:
            with self.subTest(playing=playing, recording=recording, mode=loop_mode):
                _state.PLAYING = playing
                _state.RECORDING = recording
                _state.LOOP_MODE = loop_mode
                result = self.inspector.selected_range()
                self.assertEqual(result.interpretation_status, "unvalidated")
                self.assertEqual(result.selection_presence, "unknown")
                self.assertIsNone(result.start_ticks)
        _state.PLAYING = False
        _state.RECORDING = False

    def test_integer_zero_transport_flags_do_not_bypass_meter_gate(self):
        class IntegerZeroTransportClient(DirectFakeClient):
            def call(self, cmd, **args):
                result = super().call(cmd, **args)
                if cmd == "project.info":
                    result = dict(result)
                    result["playing"] = 0
                    result["recording"] = 0
                return result

        _state.SELECTION_START = 384
        _state.SELECTION_END = 768
        _state.LOOP_MODE = 1
        result = ReadOnlyInspector(
            ReadOnlyGateway(IntegerZeroTransportClient())
        ).selected_range()
        self.assertEqual(result.interpretation_status, "unvalidated")
        self.assertEqual(result.selection_presence, "unknown")
        self.assertIsNone(result.start_ticks)
        self.assertIsNone(result.semantic_scope)

    def test_malformed_transport_flags_keep_selection_unvalidated(self):
        class TransportFlagClient(DirectFakeClient):
            def __init__(self, field, value):
                self.field = field
                self.value = value

            def call(self, cmd, **args):
                result = super().call(cmd, **args)
                if cmd == "project.info":
                    result = dict(result)
                    result[self.field] = self.value
                return result

        _state.SELECTION_START = 384
        _state.SELECTION_END = 768
        _state.LOOP_MODE = 1
        for field in ("playing", "recording"):
            for value in (0.0, "0", None, True, 1):
                with self.subTest(field=field, value=value):
                    result = ReadOnlyInspector(
                        ReadOnlyGateway(TransportFlagClient(field, value))
                    ).selected_range()
                    self.assertEqual(result.interpretation_status, "unvalidated")
                    self.assertEqual(result.selection_state, "unknown")
                    self.assertEqual(result.selection_presence, "unknown")
                    self.assertIsNone(result.semantic_scope)
                    self.assertIsNone(result.start_ticks)

    def test_timeline_capability_stays_partial_for_raw_getter_and_evidence(self):
        report = self.inspector.capabilities()
        capability = next(
            item
            for item in report.capabilities
            if item.capability == "timeline_selection"
        )
        self.assertEqual(capability.status, CapabilityStatus.PARTIAL)
        self.assertTrue(
            any(
                "Current raw selection read probe succeeded" in evidence.detail
                for evidence in capability.evidence
            )
        )
        self.assertTrue(
            any(
                "getRecPPB" in limitation
                for limitation in capability.limitations
            )
        )
        self.assertTrue(
            any("inclusivity" in limitation for limitation in capability.limitations)
        )
        self.assertTrue(
            any("inactive selection" in limitation for limitation in capability.limitations)
        )

    def test_midi_capability_names_the_native_host_transport(self):
        with mock.patch(
            "fl_studio_mcp.readonly_inspector.platform_family",
            return_value="windows",
        ):
            windows = self.inspector.capabilities()
        capability = next(
            item
            for item in windows.capabilities
            if item.capability == "midi_sysex_bridge"
        )
        self.assertIn("configured virtual MIDI endpoint", capability.access_path)
        self.assertIn("WinMM", capability.access_path)
        self.assertNotIn("CoreMIDI", capability.access_path)

    def test_unstable_or_noninteger_selection_payload_fails_closed(self):
        class PayloadClient(DirectFakeClient):
            def __init__(self, payload):
                self.payload = payload

            def call(self, cmd, **args):
                if cmd == "arrangement.selection":
                    return dict(self.payload)
                return super().call(cmd, **args)

        good = {
            "first_raw_start": 0,
            "first_raw_end": 384,
            "first_ppq": 96,
            "second_raw_start": 0,
            "second_raw_end": 384,
            "second_ppq": 96,
            "start_hint": "1:01:000",
            "end_hint": "2:01:000",
        }
        cases = []
        changed = dict(good)
        changed["second_raw_end"] = 385
        cases.append(changed)
        for field, value in (
            ("first_raw_start", True),
            ("first_raw_start", 1.5),
            ("first_raw_start", "1"),
            ("first_raw_start", 9_007_199_254_740_992),
            ("first_ppq", 0),
        ):
            payload = dict(good)
            payload[field] = value
            cases.append(payload)
        for payload in cases:
            with self.subTest(payload=payload):
                inspector = ReadOnlyInspector(ReadOnlyGateway(PayloadClient(payload)))
                with self.assertRaises(ValueError):
                    inspector.selected_range()

    def test_selection_safety_literals_cannot_be_overridden(self):
        _state.SELECTION_START = 384
        _state.SELECTION_END = 768
        _state.LOOP_MODE = 1
        result = self.inspector.selected_range().model_dump()
        result["safe_for_rendering"] = True
        with self.assertRaises(ValidationError):
            SelectedRangeObservation.model_validate(result)
        result = self.inspector.selected_range().model_dump()
        result["selection_presence"] = "range"
        with self.assertRaises(ValidationError):
            SelectedRangeObservation.model_validate(result)
        result = self.inspector.selected_range().model_dump()
        result["raw_time_unit"] = "project_timeline_ticks"
        with self.assertRaises(ValidationError):
            SelectedRangeObservation.model_validate(result)
        result = self.inspector.selected_range().model_dump()
        result["start_ticks"] = 384
        with self.assertRaises(ValidationError):
            SelectedRangeObservation.model_validate(result)
        result = self.inspector.selected_range().model_dump()
        result["render_endpoint_inclusivity"] = "exclusive"
        with self.assertRaises(ValidationError):
            SelectedRangeObservation.model_validate(result)
        result = self.inspector.selected_range().model_dump()
        result["interpretation_status"] = "validated_for_scope"
        with self.assertRaises(ValidationError):
            SelectedRangeObservation.model_validate(result)
        result = self.inspector.selected_range().model_dump()
        result["semantic_scope"] = {"timebase_ppq": 96, "loop_mode": 1}
        with self.assertRaises(ValidationError):
            SelectedRangeObservation.model_validate(result)
        result = self.inspector.selected_range().model_dump()
        result["duration_ticks"] = 384
        with self.assertRaises(ValidationError):
            SelectedRangeObservation.model_validate(result)
        result = self.inspector.selected_range().model_dump()
        result["range_order"] = "ascending"
        with self.assertRaises(ValidationError):
            SelectedRangeObservation.model_validate(result)

    def test_selected_range_json_schema_is_structurally_raw_only(self):
        properties = SelectedRangeObservation.model_json_schema()["properties"]
        for field, value in (
            ("raw_time_unit", "unknown"),
            ("selection_state", "unknown"),
            ("selection_presence", "unknown"),
            ("interpretation_status", "unvalidated"),
            ("render_endpoint_inclusivity", "unknown"),
            ("safe_for_rendering", False),
        ):
            with self.subTest(field=field):
                self.assertEqual(properties[field]["const"], value)
        for field in (
            "semantic_scope",
            "start_ticks",
            "end_ticks",
            "duration_ticks",
            "range_order",
        ):
            with self.subTest(field=field):
                self.assertEqual(properties[field]["type"], "null")

    def test_parameter_paging_preserves_padding_as_classified_rows(self):
        page = self.inspector.plugin_parameters(
            track_index=5, slot_index=0, limit=6, offset=0
        )
        self.assertEqual(page.reported_parameter_count, 240)
        self.assertEqual(page.scanned_count, 6)
        self.assertTrue(page.has_more)
        self.assertEqual(page.next_offset, 6)
        self.assertEqual(page.parameters[0].classification, "reported")
        self.assertEqual(page.parameters[4].classification, "padding_candidate")

    def test_selected_default_named_track_survives_used_filter(self):
        _state.TRACKS[20].selected = True
        result = self.inspector.list_mixer_tracks(only_used=True)
        self.assertIn(20, {track.index for track in result.tracks})
        self.assertTrue(any("heuristic" in warning for warning in result.warnings))

    def test_processing_state_is_not_confused_with_plugin_presence(self):
        _state.TRACKS[3].slots_enabled = False
        result = self.inspector.inspect_mixer_track(3)
        self.assertFalse(result.track.effect_slots_enabled)
        self.assertTrue(result.track.track_enabled)
        self.assertEqual(result.track.plugins[0].mix_level_normalized, 1.0)
        self.assertNotIn("enabled", result.track.plugins[0].model_dump())

    def test_track_indices_are_validated_against_live_count(self):
        for index in (-1, len(_state.TRACKS), 999999):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    self.inspector.inspect_mixer_track(index)

    def test_handshake_rejects_malformed_or_unknown_semantics(self):
        base = {
            "pong": True,
            "protocol": 2,
            "program_title": "FL Studio 2026",
            "fl_version": "Producer Edition v26.1.3 [build 5336]",
            "midi_scripting_api_version": 44,
            "bridge_mode": "read_only",
        }
        cases = []
        for change in (
            {"pong": False},
            {"protocol": 999},
            {"midi_scripting_api_version": 43},
            {"fl_version": "Producer Edition v26.1.3"},
            {"bridge_mode": "legacy_unknown"},
        ):
            value = dict(base)
            value.update(change)
            cases.append(value)
        for response in cases:
            with self.subTest(response=response):
                info = ReadOnlyInspector(
                    ReadOnlyGateway(ConfigurablePingClient(response))
                ).connection_info()
                self.assertFalse(info.compatible)

    def test_write_mode_is_compatible_and_only_the_bridge_enables_writes(self):
        # A write-enabled bridge still reads correctly, so it is compatible.
        # Whether writes may be dispatched is a separate field, and it is true
        # only when the bridge itself states both halves of the handshake.
        enabled = ReadOnlyInspector(
            ReadOnlyGateway(ConfigurablePingClient(WRITE_ENABLED_PING))
        ).connection_info()
        self.assertTrue(enabled.compatible)
        self.assertEqual(enabled.bridge_mode, "write_test")
        self.assertTrue(enabled.verified_writes_enabled)
        self.assertFalse(enabled.bridge_read_only_enforced)

        for change in (
            {"verified_writes_enabled": False},
            {"verified_writes_enabled": "yes"},
            {"verified_writes_enabled": 1},
            {"bridge_mode": "read_only"},
        ):
            response = dict(WRITE_ENABLED_PING)
            response.update(change)
            with self.subTest(response=response):
                info = ReadOnlyInspector(
                    ReadOnlyGateway(ConfigurablePingClient(response))
                ).connection_info()
                self.assertTrue(info.compatible)
                self.assertFalse(info.verified_writes_enabled)

    def test_connection_retains_matching_bridge_provenance_and_session(self):
        info = ReadOnlyInspector(
            ReadOnlyGateway(ConfigurablePingClient(WRITE_ENABLED_PING))
        ).connection_info()

        self.assertTrue(info.compatible)
        self.assertEqual(info.bridge_source_sha256, EXPECTED_BRIDGE_DIGEST)
        self.assertEqual(info.expected_bridge_source_sha256, EXPECTED_BRIDGE_DIGEST)
        self.assertEqual(info.bridge_provenance, "matching")
        self.assertIs(info.bridge_provenance_verified, True)
        self.assertEqual(info.session_fingerprint, SESSION_FINGERPRINT)
        self.assertEqual(info.warnings, [])

    def test_reads_warn_but_continue_for_untrusted_bridge_provenance(self):
        cases = {
            "missing": None,
            "malformed": "not-a-sha256",
            "mismatched": "0" * 64,
        }
        for provenance, digest in cases.items():
            response = dict(WRITE_ENABLED_PING)
            if digest is None:
                response.pop("bridge_source_sha256")
            else:
                response["bridge_source_sha256"] = digest
            with self.subTest(provenance=provenance):
                inspector = ReadOnlyInspector(
                    ReadOnlyGateway(ConfigurablePingClient(response))
                )
                info = inspector.connection_info()
                self.assertTrue(info.connected)
                self.assertTrue(info.compatible)
                self.assertEqual(info.bridge_provenance, provenance)
                self.assertIs(info.bridge_provenance_verified, False)
                self.assertTrue(info.warnings)
                # The warning must reach ordinary observations, not only the
                # connection model returned by a capabilities request.
                tracks = inspector.list_mixer_tracks(max_tracks=1)
                self.assertTrue(
                    any("write" in warning.lower() for warning in tracks.warnings),
                    tracks.warnings,
                )

    def test_malformed_session_fingerprint_does_not_break_reads(self):
        response = dict(WRITE_ENABLED_PING, session_fingerprint="not-a-session")
        inspector = ReadOnlyInspector(ReadOnlyGateway(ConfigurablePingClient(response)))
        info = inspector.connection_info()

        self.assertTrue(info.compatible)
        self.assertIsNone(info.session_fingerprint)
        self.assertTrue(any("session fingerprint" in w for w in info.warnings))
        self.assertEqual(inspector.project_summary().project_title, "Synthetic Test Project")

    def test_reads_continue_when_packaged_bridge_digest_is_unavailable(self):
        with mock.patch(
            "fl_studio_mcp.readonly_inspector.expected_bridge_deployment",
            side_effect=BridgeInstallError("fixture source missing"),
        ):
            inspector = ReadOnlyInspector(
                ReadOnlyGateway(ConfigurablePingClient(WRITE_ENABLED_PING))
            )
            info = inspector.connection_info()
            self.assertTrue(info.compatible)
            self.assertEqual(info.bridge_provenance, "unavailable")
            self.assertIs(info.bridge_provenance_verified, False)
            self.assertTrue(any("could not be checked" in w for w in info.warnings))
            self.assertEqual(
                inspector.project_summary().project_title, "Synthetic Test Project"
            )

    def test_offline_capabilities_do_not_claim_current_reads(self):
        report = ReadOnlyInspector(ReadOnlyGateway(DownClient())).capabilities()
        by_name = {item.capability: item.status for item in report.capabilities}
        self.assertEqual(
            by_name["project_transport_and_counts"], CapabilityStatus.UNVALIDATED
        )
        self.assertEqual(
            by_name["mixer_tracks_routing_and_loaded_effects"],
            CapabilityStatus.UNVALIDATED,
        )

    def test_nonfinite_and_contradictory_safety_models_are_rejected(self):
        with self.assertRaises(ValidationError):
            MixerTrackSummary(index=0, name="Master", volume_db=math.nan)
        with self.assertRaises(ValidationError):
            PluginParameter(
                index=0,
                reported_name="Unknown",
                display_text_available=False,
                safe_to_modify=True,
            )

    def test_structured_expected_before_models_require_a_real_guard(self):
        for model in (ExpectedEqBandState, ExpectedPluginParameterState):
            with self.subTest(model=model.__name__):
                with self.assertRaises(ValidationError):
                    model()
        with self.assertRaises(ValidationError):
            ExpectedEqBandState(gain_normalized=0.5, surprise=0.5)
        with self.assertRaises(ValidationError):
            ExpectedPluginParameterState(normalized_value=1.5)

    def test_observations_explicitly_admit_non_atomicity(self):
        report = self.inspector.capture(parameter_limit=4, max_plugins=2)
        self.assertFalse(report.observation_atomic)
        self.assertFalse(report.mixer.observation_atomic)
        self.assertTrue(
            any("non-atomic" in warning for warning in report.mixer.warnings)
        )

    def test_bridge_client_wraps_fourteen_bit_wire_ids(self):
        class DummyTransport:
            name = "tcp"

            def request(self, rid, payload):
                return {"id": rid, "ok": True, "result": {"rid": rid}}

            def close(self):
                pass

        client = BridgeClient(port=1, mailbox="/nonexistent", midi_port="none")
        client._active = DummyTransport()
        client._id = MAX_WIRE_ID - 1
        self.assertEqual(client.call("ping")["rid"], MAX_WIRE_ID)
        self.assertEqual(client.call("ping")["rid"], 1)

    def test_version_gate_rejects_fl_studio_2025(self):
        inspector = ReadOnlyInspector(ReadOnlyGateway(FL2025Client()))
        connection = inspector.connection_info()
        self.assertFalse(connection.compatible)
        with self.assertRaises(IncompatibleFLStudio):
            inspector.project_summary()

    def test_agent_contracts_forbid_unknown_fields(self):
        with self.assertRaises(ValidationError):
            ConnectionInfo(
                connected=True,
                compatible=True,
                compatibility_reason="test",
                surprise="not allowed",
            )

    def test_mcp_surface_is_exactly_the_published_tool_set(self):
        # This used to be a blanket ban on any tool whose name contained
        # "set_", which encoded a product that could only read. The narrowly
        # scoped mutations are now part of the surface, so the guard is an
        # exact set instead: an unintended tool appearing still fails here,
        # and so does one silently disappearing.
        tools = asyncio.run(mcp.list_tools())
        names = {tool.name for tool in tools}
        read_tools = {
            "fl_get_capabilities",
            "fl_get_project_summary",
            "fl_get_transport_state",
            "fl_get_selected_range",
            "fl_list_mixer_tracks",
            "fl_inspect_mixer_track",
            "plugins_scan_loaded_plugins",
            "plugins_inspect_parameter_map",
            "plugins_scan_parameters",
            "copilot_capture_readonly_inspection",
            "fl_list_channels",
            "fl_get_step_sequence",
        }
        write_tools = {
            "fl_set_mixer_volume",
            "fl_set_mixer_pan",
            "fl_set_mixer_mute",
            "fl_set_track_eq",
            "fl_set_mixer_name",
            "fl_set_mixer_send",
            "fl_set_mixer_send_level",
            "fl_set_plugin_param",
            "fl_set_plugin_param_display",
            "fl_set_plugin_param_option",
            "fl_set_playing",
            "fl_stop",
            "fl_set_song_position",
            "fl_set_loop_mode",
            "fl_set_tempo",
            "fl_set_channel_mix",
            "fl_set_channel_identity",
            "fl_route_channel_to_mixer",
            "fl_set_step_sequence",
        }
        audition_tools = {"fl_trigger_note"}
        mode_tools = {"fl_set_write_mode"}
        audio_tools = {
            # File measurement, not FL control: these read a rendered
            # bounce from disk and never reach the bridge.
            "audio_analyze_file",
            "audio_compare_files",
            "audio_analyze_masking",
            "audio_find_recent_bounces",
        }
        self.assertEqual(
            names,
            read_tools | write_tools | audition_tools | mode_tools | audio_tools,
        )
        # Still no undo, render, approval ceremony or reflective escape hatch,
        # whatever it might be called.
        prohibited_fragments = (
            "apply",
            "rollback",
            "undo",
            "render",
            "api_call",
            "save",
            "exec",
            "eval",
        )
        self.assertFalse(
            [name for name in names if any(fragment in name for fragment in prohibited_fragments)]
        )
        by_name = {tool.name: tool for tool in tools}
        for name in read_tools | audio_tools:
            with self.subTest(tool=name):
                annotations = by_name[name].annotations
                self.assertTrue(annotations and annotations.read_only_hint)
                self.assertIs(annotations.destructive_hint, False)
        for name in write_tools:
            with self.subTest(tool=name):
                annotations = by_name[name].annotations
                self.assertIsNotNone(annotations)
                # Honest: it overwrites live project state. An observed undo
                # point is useful recovery evidence, not a non-destructive hint.
                self.assertIs(annotations.read_only_hint, False)
                self.assertIs(annotations.destructive_hint, True)
                # Absolute targets still must not invite client retries: a
                # repeat can add undo history or repeat a parameter sweep.
                self.assertIs(annotations.idempotent_hint, False)
                properties = by_name[name].input_schema["properties"]
                required = set(by_name[name].input_schema.get("required", []))
                self.assertIn("session_fingerprint", properties)
                self.assertNotIn("session_fingerprint", required)
                if name == "fl_set_step_sequence":
                    self.assertNotIn("expected_before", properties)
                    self.assertIn("expected_digest", properties)
                    self.assertIn("expected_digest", required)
                else:
                    self.assertIn("expected_before", properties)
                    self.assertNotIn("expected_before", required)
        annotations = by_name["fl_trigger_note"].annotations
        self.assertIsNotNone(annotations)
        self.assertIs(annotations.read_only_hint, False)
        self.assertIs(annotations.destructive_hint, False)
        self.assertIs(annotations.idempotent_hint, False)
        mode_annotations = by_name["fl_set_write_mode"].annotations
        self.assertIsNotNone(mode_annotations)
        self.assertIs(mode_annotations.read_only_hint, False)
        self.assertIs(mode_annotations.destructive_hint, True)
        self.assertIs(mode_annotations.idempotent_hint, True)
        self.assertEqual(
            set(by_name["fl_set_write_mode"].input_schema["properties"]),
            {"enabled", "confirm_user_present"},
        )
        self.assertTrue(all(tool.output_schema for tool in tools))
        selection_schema = next(
            tool.output_schema
            for tool in tools
            if tool.name == "fl_get_selected_range"
        )["properties"]
        self.assertEqual(selection_schema["interpretation_status"]["const"], "unvalidated")
        self.assertEqual(selection_schema["selection_state"]["const"], "unknown")
        self.assertEqual(selection_schema["selection_presence"]["const"], "unknown")
        self.assertEqual(selection_schema["raw_time_unit"]["const"], "unknown")
        for field in (
            "semantic_scope",
            "start_ticks",
            "end_ticks",
            "duration_ticks",
            "range_order",
        ):
            self.assertEqual(selection_schema[field]["type"], "null")
        self.assertTrue(
            all(tool.input_schema.get("additionalProperties") is False for tool in tools)
        )


class WriteModeTests(unittest.TestCase):
    def setUp(self):
        _state.reset()

    def test_gateway_exposes_only_the_session_mode_command(self):
        client = RuntimeModeClient()
        gateway = WriteModeGateway(client)
        self.assertEqual(
            gateway.ALLOWED_COMMANDS,
            {"session.set_write_mode"},
        )
        for command in (
            "ping",
            "project.info",
            "mixer.set_volume",
            "project.save",
            "call",
        ):
            with self.subTest(command=command):
                with self.assertRaises(WriteModeBoundaryViolation):
                    gateway.call(command)
        self.assertEqual(client.commands, [])

    def test_enabling_requires_confirmation_before_contacting_the_bridge(self):
        client = RuntimeModeClient()
        manager = WriteModeManager(WriteModeGateway(client))
        before = state_fingerprint()
        with self.assertRaisesRegex(
            WriteModeConfirmationRequired, "confirm_user_present=true"
        ):
            manager.set_write_mode(enabled=True)
        self.assertEqual(client.ping_count, 0)
        self.assertEqual(client.commands, [])
        self.assertEqual(state_fingerprint(), before)

    def test_runtime_enable_is_typed_and_verified_by_a_second_handshake(self):
        client = RuntimeModeClient()
        manager = WriteModeManager(WriteModeGateway(client))
        before = state_fingerprint()
        result = manager.set_write_mode(
            enabled=True,
            confirm_user_present=True,
        )

        self.assertIsInstance(result, WriteModeChange)
        self.assertTrue(result.verified)
        self.assertTrue(result.after_enabled)
        self.assertTrue(result.changed)
        self.assertEqual(result.bridge_mode, "write_test")
        self.assertEqual(result.write_mode_origin, "runtime_request")
        self.assertEqual(
            result.verification_basis,
            "post_transition_bridge_handshake",
        )
        self.assertTrue(result.session_only)
        self.assertFalse(result.project_saved)
        self.assertEqual(client.ping_count, 2)
        self.assertEqual(
            client.commands,
            [
                (
                    "session.set_write_mode",
                    {
                        "enabled": True,
                        "confirm_user_present": True,
                        "session_fingerprint": SESSION_FINGERPRINT,
                    },
                )
            ],
        )
        self.assertEqual(state_fingerprint(), before)
        with self.assertRaises(ValidationError):
            result.after_enabled = False

    def test_disabling_needs_no_positive_confirmation_and_is_idempotent(self):
        client = RuntimeModeClient(enabled=True)
        manager = WriteModeManager(WriteModeGateway(client))
        first = manager.set_write_mode(enabled=False)
        second = manager.set_write_mode(enabled=False)

        self.assertFalse(first.after_enabled)
        self.assertTrue(first.changed)
        self.assertFalse(second.after_enabled)
        self.assertFalse(second.changed)
        self.assertFalse(second.confirmation_required)
        self.assertFalse(second.confirmation_applied)
        self.assertEqual(second.write_mode_origin, "disabled")

    def test_enable_refuses_stale_or_untrusted_bridge_before_dispatch(self):
        cases = {
            "mismatched": {"bridge_source_sha256": "0" * 64},
            "missing control": {"runtime_write_mode_control": False},
            "missing session": {"session_fingerprint": None},
            "unknown origin": {"write_mode_origin": "mystery"},
        }
        for label, overrides in cases.items():
            client = RuntimeModeClient(ping_overrides=overrides)
            manager = WriteModeManager(WriteModeGateway(client))
            with self.subTest(label=label):
                with self.assertRaises(WriteModeUnavailable):
                    manager.set_write_mode(
                        enabled=True,
                        confirm_user_present=True,
                    )
                self.assertEqual(client.commands, [])

    def test_contradictory_command_metadata_never_becomes_success(self):
        client = RuntimeModeClient(reply_overrides={"project_saved": True})
        manager = WriteModeManager(WriteModeGateway(client))
        with self.assertRaisesRegex(
            WriteModeUnavailable, "write mode is enabled.*project_saved"
        ):
            manager.set_write_mode(
                enabled=True,
                confirm_user_present=True,
            )
        self.assertTrue(client.enabled)
        self.assertEqual(client.ping_count, 2)

    def test_lost_mode_reply_is_not_replayed_and_post_handshake_proves_state(self):
        class LostReplyClient(RuntimeModeClient):
            def call(self, cmd, **args):
                super().call(cmd, **args)
                raise BridgeError("fixture reply lost after dispatch")

        client = LostReplyClient()
        manager = WriteModeManager(WriteModeGateway(client))
        result = manager.set_write_mode(
            enabled=True,
            confirm_user_present=True,
        )

        self.assertTrue(result.verified)
        self.assertTrue(result.after_enabled)
        self.assertTrue(result.warnings)
        self.assertIn("no command was replayed", result.warnings[0])
        self.assertEqual(len(client.commands), 1)
        self.assertEqual(client.ping_count, 2)

    def test_bridge_reload_during_transition_is_refused(self):
        class ReloadingClient(RuntimeModeClient):
            def ping(self):
                response = super().ping()
                if self.ping_count > 1:
                    response["session_fingerprint"] = "b" * 32
                return response

        client = ReloadingClient()
        manager = WriteModeManager(WriteModeGateway(client))
        with self.assertRaisesRegex(WriteModeUnavailable, "reloaded"):
            manager.set_write_mode(
                enabled=True,
                confirm_user_present=True,
            )

    def test_literal_booleans_are_required(self):
        manager = WriteModeManager(WriteModeGateway(RuntimeModeClient()))
        for enabled, confirmed in ((1, True), (True, 1), ("true", True)):
            with self.subTest(enabled=enabled, confirmed=confirmed):
                with self.assertRaisesRegex(ValueError, "true or false"):
                    manager.set_write_mode(
                        enabled=enabled,
                        confirm_user_present=confirmed,
                    )


class VerifiedWriteTests(unittest.TestCase):
    """The verified writes, driven against the deterministic fake bridge.

    There is no live path here and none is invented. Every write goes through
    the real bridge handlers -- including their idle-tick yield, which the fake
    client drives to completion -- against the fake FL API.
    """

    def setUp(self):
        _state.reset()
        self.client = WriteEnabledFakeClient()
        self.writer = VerifiedWriter(WriteGateway(self.client))

    def dispatched(self):
        return [command for command, _ in self.client.commands]

    def unwritable(self):
        return VerifiedWriter(WriteGateway(WritesDisabledClient()))

    # -- shared shape ----------------------------------------------------

    def assert_write_report(self, result, command, track_index, master=False):
        self.assertEqual(result.schema_version, "1.0")
        self.assertEqual(result.bridge_command, command)
        self.assertEqual(result.track_index, track_index)
        self.assertIs(result.targeted_master, master)
        self.assertEqual(
            result.verification_basis, "readback_on_a_later_fl_idle_tick"
        )
        # Observed, not asserted: the fake takes a real undo point, so this
        # must read True rather than merely being declared True.
        self.assertIs(result.undo_point_created, True)
        self.assertIs(result.project_saved, False)
        self.assertTrue(result.verification_summary)
        # A write report records something that already happened to the user's
        # project, so nothing downstream may edit it.
        with self.assertRaises(ValidationError):
            result.verified = not result.verified

    def assert_unverified(self, result):
        self.assertIs(result.verified, False)
        self.assertTrue(result.warnings, "an unverified write must warn")
        self.assertTrue(result.warnings[0].startswith("UNVERIFIED:"))
        # The summary has to say what FL read back instead, not just "failed".
        self.assertIn("accepted the write but read", result.verification_summary)

    # -- gateway ---------------------------------------------------------

    def test_write_gateway_dispatches_nothing_but_the_ten_writes(self):
        self.assertEqual(
            WriteGateway.ALLOWED_COMMANDS,
            {
                "mixer.set_volume", "mixer.set_pan", "mixer.set_mute",
                "mixer.set_eq", "mixer.set_name", "mixer.set_send",
                "mixer.set_send_level", "plugin.set_param",
                "plugin.set_param_display", "plugin.set_param_option",
            },
        )
        for command in (
            "ping",
            "project.info",
            "arrangement.selection",
            "mixer.list",
            "mixer.track",
            "plugin.params",
            "plugin.scan_params",
            "channels.list",
            "no.such.command",
        ):
            with self.subTest(command=command):
                with self.assertRaises(WriteBoundaryViolation):
                    WriteGateway(self.client).call(command)
        self.assertEqual(self.dispatched(), [])

    def test_allow_master_requires_a_literal_boolean_on_all_ten_writes(self):
        calls = (
            (
                "set_mixer_volume",
                {"track_index": 0, "volume_normalized": 0.5},
            ),
            ("set_mixer_pan", {"track_index": 0, "pan": 0.25}),
            ("set_mixer_mute", {"track_index": 0, "muted": True}),
            (
                "set_mixer_eq",
                {"track_index": 0, "band_index": 1, "gain_normalized": 0.6},
            ),
            ("set_mixer_name", {"track_index": 0, "name": "Master Test"}),
            (
                "set_mixer_send",
                {
                    "track_index": 0,
                    "destination_track_index": 3,
                    "enabled": True,
                },
            ),
            (
                "set_mixer_send_level",
                {
                    "track_index": 0,
                    "destination_track_index": 3,
                    "level_normalized": 0.5,
                },
            ),
            (
                "set_plugin_parameter",
                {
                    "track_index": 0,
                    "slot_index": 0,
                    "parameter_index": 0,
                    "normalized_value": 0.4,
                },
            ),
            (
                "set_plugin_parameter_display",
                {
                    "track_index": 0,
                    "slot_index": 0,
                    "parameter": 0,
                    "target_value": 20.0,
                },
            ),
            (
                "set_plugin_parameter_option",
                {
                    "track_index": 0,
                    "slot_index": 0,
                    "parameter": 0,
                    "option": "On",
                },
            ),
        )
        before = state_fingerprint()
        for method, arguments in calls:
            with self.subTest(method=method):
                with self.assertRaises(ValueError) as caught:
                    getattr(self.writer, method)(
                        **arguments,
                        allow_master="false",
                    )
                self.assertIn("allow_master", str(caught.exception))
        self.assertEqual(self.dispatched(), [])
        self.assertEqual(_state.UNDO, [])
        self.assertEqual(state_fingerprint(), before)

    def test_every_write_refuses_when_the_bridge_cannot_write(self):
        writer = self.unwritable()
        calls = (
            ("set_mixer_volume", {"track_index": 3, "volume_normalized": 0.5}),
            ("set_mixer_pan", {"track_index": 3, "pan": -0.25}),
            ("set_mixer_mute", {"track_index": 3, "muted": True}),
            ("set_mixer_eq", {"track_index": 3, "band_index": 1, "gain_normalized": 0.7}),
            (
                "set_plugin_parameter",
                {
                    "track_index": 3,
                    "slot_index": 1,
                    "parameter_index": 0,
                    "normalized_value": 0.3,
                },
            ),
        )
        before = state_fingerprint()
        for method, arguments in calls:
            with self.subTest(method=method):
                # WritesDisabledClient raises AssertionError if anything is
                # dispatched, so this also proves the refusal is local and no
                # raw bridge rejection is being dressed up as one.
                with self.assertRaises(VerifiedWritesUnavailable) as caught:
                    getattr(writer, method)(**arguments)
                message = str(caught.exception)
                self.assertIn("fl_set_write_mode", message)
                self.assertIn("confirm_user_present=true", message)
                self.assertIn("bridge_mode='read_only'", message)
        self.assertEqual(before, state_fingerprint())

    def test_writes_fail_closed_for_every_untrusted_provenance_state(self):
        cases = {
            "missing": None,
            "malformed": "not-a-sha256",
            "mismatched": "0" * 64,
        }
        for provenance, digest in cases.items():
            response = dict(WRITE_ENABLED_PING)
            if digest is None:
                response.pop("bridge_source_sha256")
            else:
                response["bridge_source_sha256"] = digest
            client = RefuseDispatchClient(response)
            writer = VerifiedWriter(WriteGateway(client))
            with self.subTest(provenance=provenance):
                before = state_fingerprint()
                with self.assertRaises(RuntimeError) as caught:
                    writer.set_mixer_volume(
                        track_index=3, volume_normalized=0.5
                    )
                self.assertIn(provenance, str(caught.exception).lower())
                self.assertEqual(client.commands, [])
                self.assertEqual(state_fingerprint(), before)

    def test_writes_fail_closed_when_expected_provenance_is_unavailable(self):
        client = RefuseDispatchClient(WRITE_ENABLED_PING)
        writer = VerifiedWriter(WriteGateway(client))
        before = state_fingerprint()
        with mock.patch(
            "fl_studio_mcp.readonly_inspector.expected_bridge_deployment",
            side_effect=BridgeInstallError("fixture source missing"),
        ):
            with self.assertRaises(RuntimeError) as caught:
                writer.set_mixer_volume(track_index=3, volume_normalized=0.5)
        self.assertIn("unavailable", str(caught.exception).lower())
        self.assertEqual(client.commands, [])
        self.assertEqual(state_fingerprint(), before)

    # -- mixer volume ----------------------------------------------------

    def test_volume_write_returns_the_verified_typed_shape(self):
        result = self.writer.set_mixer_volume(track_index=3, volume_normalized=0.65)
        self.assertIsInstance(result, VerifiedMixerVolumeWrite)
        self.assert_write_report(result, "mixer.set_volume", 3)
        self.assertIs(result.verified, True)
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.requested_volume_normalized, 0.65)
        self.assertEqual(result.before_volume_normalized, 0.72)
        self.assertEqual(result.after_volume_normalized, 0.65)
        self.assertIsNotNone(result.before_volume_db)
        self.assertIsNotNone(result.after_volume_db)
        self.assertEqual(_state.TRACKS[3].volume, 0.65)
        self.assertEqual(self.dispatched(), ["mixer.set_volume"])
        # One undo point, and the project was never saved.
        self.assertEqual(len(_state.UNDO), 1)

    def test_volume_write_reports_an_ignored_write_instead_of_raising(self):
        with mock.patch.object(bridge.mixer, "setTrackVolume", lambda *a, **k: None):
            result = self.writer.set_mixer_volume(track_index=3, volume_normalized=0.65)
        self.assert_unverified(result)
        self.assertEqual(result.after_volume_normalized, 0.72)
        self.assertEqual(_state.TRACKS[3].volume, 0.72)

    def test_volume_write_refuses_master_unless_asked_for_by_name(self):
        with self.assertRaises(ValueError) as caught:
            self.writer.set_mixer_volume(track_index=0, volume_normalized=0.5)
        self.assertIn("allow_master", str(caught.exception))
        self.assertEqual(self.dispatched(), [])
        self.assertEqual(_state.TRACKS[0].volume, 0.8)

        result = self.writer.set_mixer_volume(
            track_index=0, volume_normalized=0.5, allow_master=True
        )
        self.assert_write_report(result, "mixer.set_volume", 0, master=True)
        self.assertIs(result.verified, True)
        self.assertEqual(_state.TRACKS[0].volume, 0.5)

    def test_volume_write_rejects_out_of_range_before_the_bridge(self):
        for value in (-0.01, 1.01, 42.0, float("nan"), float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.writer.set_mixer_volume(track_index=3, volume_normalized=value)
        with self.assertRaises(ValueError):
            self.writer.set_mixer_volume(track_index=-1, volume_normalized=0.5)
        self.assertEqual(self.dispatched(), [])
        self.assertEqual(_state.TRACKS[3].volume, 0.72)

    # -- mixer pan -------------------------------------------------------

    def test_pan_write_returns_the_verified_typed_shape(self):
        result = self.writer.set_mixer_pan(track_index=3, pan=-0.4)
        self.assertIsInstance(result, VerifiedMixerPanWrite)
        self.assert_write_report(result, "mixer.set_pan", 3)
        self.assertIs(result.verified, True)
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.requested_pan, -0.4)
        self.assertEqual(result.before_pan, 0.0)
        self.assertEqual(result.after_pan, -0.4)
        self.assertEqual(_state.TRACKS[3].pan, -0.4)
        self.assertEqual(self.dispatched(), ["mixer.set_pan"])

    def test_pan_write_reports_an_ignored_write_instead_of_raising(self):
        with mock.patch.object(bridge.mixer, "setTrackPan", lambda *a, **k: None):
            result = self.writer.set_mixer_pan(track_index=3, pan=-0.4)
        self.assert_unverified(result)
        self.assertEqual(result.after_pan, 0.0)

    def test_pan_write_refuses_master_unless_asked_for_by_name(self):
        with self.assertRaises(ValueError) as caught:
            self.writer.set_mixer_pan(track_index=0, pan=0.3)
        self.assertIn("allow_master", str(caught.exception))
        self.assertEqual(self.dispatched(), [])

        result = self.writer.set_mixer_pan(track_index=0, pan=0.3, allow_master=True)
        self.assert_write_report(result, "mixer.set_pan", 0, master=True)
        self.assertIs(result.verified, True)

    def test_pan_write_rejects_out_of_range_before_the_bridge(self):
        for value in (-1.01, 1.01, float("nan"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.writer.set_mixer_pan(track_index=3, pan=value)
        self.assertEqual(self.dispatched(), [])
        self.assertEqual(_state.TRACKS[3].pan, 0.0)

    # -- track name ------------------------------------------------------

    def test_name_write_returns_the_verified_typed_shape(self):
        result = self.writer.set_mixer_name(track_index=3, name="Lead Verb")
        self.assertIsInstance(result, VerifiedMixerNameWrite)
        self.assert_write_report(result, "mixer.set_name", 3)
        self.assertIs(result.verified, True)
        self.assertIs(result.restored_default, False)
        self.assertEqual(result.after_name, "Lead Verb")
        self.assertEqual(_state.TRACKS[3].name, "Lead Verb")

    def test_an_empty_name_restores_the_default_rather_than_blanking_it(self):
        self.writer.set_mixer_name(track_index=3, name="Lead Verb")
        result = self.writer.set_mixer_name(track_index=3, name="")
        self.assertIs(result.verified, True)
        self.assertIs(result.restored_default, True)
        # FL answers an empty request with its own default, so the readback
        # deliberately does not echo what was asked for.
        self.assertNotEqual(result.after_name, "")
        self.assertTrue(result.after_name)

    def test_name_write_rejects_a_runaway_string_before_the_bridge(self):
        with self.assertRaises(ValueError):
            self.writer.set_mixer_name(track_index=3, name="x" * 65)
        self.assertEqual(self.dispatched(), [])

    # -- sends -----------------------------------------------------------

    def test_send_write_creates_and_tears_down_a_route(self):
        result = self.writer.set_mixer_send(
            track_index=3, destination_track_index=5, enabled=True
        )
        self.assertIsInstance(result, VerifiedMixerSendWrite)
        self.assert_write_report(result, "mixer.set_send", 3)
        self.assertIs(result.verified, True)
        self.assertIs(result.after_enabled, True)
        self.assertEqual(result.destination_track_index, 5)
        self.assertIn(5, _state.TRACKS[3].routes)

        result = self.writer.set_mixer_send(
            track_index=3, destination_track_index=5, enabled=False
        )
        self.assertIs(result.verified, True)
        self.assertIs(result.after_enabled, False)
        # FL raises rather than reporting a level for an inactive route, so
        # there is no honest zero to put here.
        self.assertIsNone(result.level_normalized)
        self.assertNotIn(5, _state.TRACKS[3].routes)

    def test_sending_to_master_needs_no_flag_but_sending_from_it_does(self):
        result = self.writer.set_mixer_send(
            track_index=3, destination_track_index=0, enabled=True
        )
        self.assertIs(result.verified, True)
        with self.assertRaises(ValueError) as caught:
            self.writer.set_mixer_send(
                track_index=0, destination_track_index=3, enabled=True
            )
        self.assertIn("allow_master", str(caught.exception))

    def test_a_track_may_not_send_to_itself(self):
        with self.assertRaises(ValueError) as caught:
            self.writer.set_mixer_send(
                track_index=3, destination_track_index=3, enabled=True
            )
        self.assertIn("itself", str(caught.exception))
        self.assertEqual(self.dispatched(), [])

    def test_send_level_lands_on_an_existing_route(self):
        self.writer.set_mixer_send(
            track_index=3, destination_track_index=5, enabled=True
        )
        result = self.writer.set_mixer_send_level(
            track_index=3, destination_track_index=5, level_normalized=0.42
        )
        self.assertIsInstance(result, VerifiedMixerSendLevelWrite)
        self.assertIs(result.verified, True)
        self.assertIs(result.send_active, True)
        self.assertAlmostEqual(result.after_level_normalized, 0.42)

    def test_a_send_level_without_a_send_is_refused_not_reported_unverified(self):
        # The device trap. FL raises "Index out of range" reading the level of
        # an inactive route, so writing one could never be read back; the
        # refusal has to name the command that creates the route.
        with self.assertRaises(Exception) as caught:
            self.writer.set_mixer_send_level(
                track_index=3, destination_track_index=7, level_normalized=0.5
            )
        self.assertIn("mixer.set_send", str(caught.exception))

    # -- plug-in parameters in their own units ---------------------------

    def test_display_write_lands_on_the_number_the_plugin_shows(self):
        result = self.writer.set_plugin_parameter_display(
            track_index=3, slot_index=1, parameter="Threshold", target_value=40.0
        )
        self.assertIsInstance(result, VerifiedPluginDisplayWrite)
        self.assert_write_report(result, "plugin.set_param_display", 3)
        self.assertIs(result.verified, True)
        self.assertEqual(result.matched_on, "name")
        self.assertEqual(result.parameter_index, 0)
        self.assertLessEqual(abs(result.landed_value - 40.0), result.tolerance)
        self.assertTrue(result.after.display_text)

    def test_display_write_resolves_by_display_when_there_is_no_name(self):
        # Real third-party controls routinely have an empty name and carry
        # identity in the display string, so this is an ordinary path, not a
        # fallback. These displays are also static, which is exactly the
        # "FL accepted it and nothing moved" case: resolution must still be
        # right and the outcome must still be reported honestly.
        plugin = _state.TRACKS[3].slots[1]
        plugin.param_names[2] = ""
        plugin.displays = ["Auto mode ", "1 ms", "50 ms", "2 ms", "3 ms", "4 ms"]
        result = self.writer.set_plugin_parameter_display(
            track_index=3, slot_index=1, parameter="50 ms", target_value=30.0
        )
        self.assertEqual(result.matched_on, "display")
        self.assertEqual(result.parameter_index, 2)
        self.assertIs(result.verified, False)
        self.assertTrue(any("UNVERIFIED" in w for w in result.warnings))
        self.assertIn("could not land", result.verification_summary)
        self.assertEqual(result.after.display_text, "50 ms")

    def test_ambiguous_parameter_name_is_refused_and_index_disambiguates(self):
        plugin = _state.TRACKS[3].slots[1]
        plugin.param_names[2] = "Attack Time"
        plugin.param_names[3] = "Attack Curve"
        before = state_fingerprint()

        with self.assertRaises(Exception) as caught:
            self.writer.set_plugin_parameter_display(
                track_index=3, slot_index=1, parameter="Attack", target_value=30.0
            )
        message = str(caught.exception).lower()
        self.assertIn("ambiguous", message)
        self.assertIn("attack time", message)
        self.assertIn("attack curve", message)
        self.assertIn("index", message)
        self.assertEqual(state_fingerprint(), before)
        self.assertEqual(_state.UNDO, [])

        result = self.writer.set_plugin_parameter_display(
            track_index=3, slot_index=1, parameter=2, target_value=30.0
        )
        self.assertEqual(result.parameter_index, 2)
        self.assertEqual(result.matched_on, "index")

    def test_display_write_refuses_a_control_with_no_number_to_search(self):
        plugin = _state.TRACKS[3].slots[1]
        plugin.displays = ["Chromatic ", "a", "b", "c", "d", "e"]
        with self.assertRaises(Exception) as caught:
            self.writer.set_plugin_parameter_display(
                track_index=3, slot_index=1, parameter=0, target_value=1.0
            )
        self.assertIn("set_param", str(caught.exception))

    def test_display_write_rejects_a_nonsense_parameter_before_the_bridge(self):
        for bad in (None, 1.5, True, "   "):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.writer.set_plugin_parameter_display(
                        track_index=3, slot_index=1, parameter=bad, target_value=1.0
                    )
        self.assertEqual(self.dispatched(), [])

    # -- enumerated parameters -------------------------------------------

    def test_option_write_lands_on_a_named_option(self):
        result = self.writer.set_plugin_parameter_option(
            track_index=9, slot_index=0, parameter="Key", option="A"
        )
        self.assertIsInstance(result, VerifiedPluginOptionWrite)
        self.assert_write_report(result, "plugin.set_param_option", 9)
        self.assertIs(result.verified, True)
        self.assertEqual(result.selected_option, "A")
        self.assertEqual(result.after.display_text, "A")

    def test_option_write_reports_the_whole_enumeration_it_found(self):
        # FL cannot be asked what a control accepts, so discovering it is the
        # point of the sweep, not a side effect.
        result = self.writer.set_plugin_parameter_option(
            track_index=9, slot_index=0, parameter="Scale", option="Minor"
        )
        self.assertEqual(result.options, ["Chromatic", "Major", "Minor"])
        self.assertIs(result.verified, True)

    def test_option_write_rejects_a_substring_and_restores(self):
        before = _state.TRACKS[9].slots[0].values[0]
        with self.assertRaisesRegex(Exception, "no option matching"):
            self.writer.set_plugin_parameter_option(
                track_index=9, slot_index=0, parameter="Key", option="#"
            )
        self.assertEqual(_state.TRACKS[9].slots[0].values[0], before)

    def test_option_write_rejects_a_non_exact_bridge_receipt(self):
        real_call = self.client.call

        def substring_receipt(cmd, **args):
            result = real_call(cmd, **args)
            if cmd == "plugin.set_param_option":
                result["selected"] = "Wide A"
                result["after"]["display"] = "Wide A"
                result["options"].append("Wide A")
            return result

        with mock.patch.object(self.client, "call", side_effect=substring_receipt):
            with self.assertRaisesRegex(ValueError, "exactly match"):
                self.writer.set_plugin_parameter_option(
                    track_index=9, slot_index=0, parameter="Key", option="A"
                )

    def test_a_missing_option_restores_the_control_it_moved(self):
        before = _state.TRACKS[9].slots[0].values[0]
        with self.assertRaises(Exception) as caught:
            self.writer.set_plugin_parameter_option(
                track_index=9, slot_index=0, parameter="Key", option="H"
            )
        # The sweep necessarily moved the control while looking; a failed
        # search must not leave it somewhere the caller never asked for.
        self.assertEqual(_state.TRACKS[9].slots[0].values[0], before)
        message = str(caught.exception)
        self.assertIn("'A'", message)
        self.assertIn("'C#'", message)

    def test_option_write_rejects_an_empty_option_before_the_bridge(self):
        with self.assertRaises(ValueError):
            self.writer.set_plugin_parameter_option(
                track_index=9, slot_index=0, parameter="Key", option="   "
            )
        self.assertEqual(self.dispatched(), [])

    # -- mixer mute ------------------------------------------------------

    def test_mute_write_returns_the_verified_typed_shape(self):
        result = self.writer.set_mixer_mute(track_index=3, muted=True)
        self.assertIsInstance(result, VerifiedMixerMuteWrite)
        self.assert_write_report(result, "mixer.set_mute", 3)
        self.assertIs(result.verified, True)
        self.assertIs(result.requested_muted, True)
        self.assertIs(result.before_muted, False)
        self.assertIs(result.after_muted, True)
        self.assertTrue(_state.TRACKS[3].muted)

        # A state, never a toggle: repeating it leaves the track muted.
        again = self.writer.set_mixer_mute(track_index=3, muted=True)
        self.assertIs(again.verified, True)
        self.assertIs(again.after_muted, True)
        self.assertTrue(_state.TRACKS[3].muted)

    def test_mute_write_reports_an_ignored_write_instead_of_raising(self):
        with mock.patch.object(bridge.mixer, "muteTrack", lambda *a, **k: None):
            result = self.writer.set_mixer_mute(track_index=3, muted=True)
        self.assert_unverified(result)
        self.assertIs(result.after_muted, False)
        self.assertFalse(_state.TRACKS[3].muted)

    def test_mute_write_refuses_master_unless_asked_for_by_name(self):
        with self.assertRaises(ValueError) as caught:
            self.writer.set_mixer_mute(track_index=0, muted=True)
        self.assertIn("allow_master", str(caught.exception))
        self.assertEqual(self.dispatched(), [])
        self.assertFalse(_state.TRACKS[0].muted)

        result = self.writer.set_mixer_mute(track_index=0, muted=True, allow_master=True)
        self.assert_write_report(result, "mixer.set_mute", 0, master=True)
        self.assertIs(result.verified, True)
        self.assertTrue(_state.TRACKS[0].muted)

    def test_mute_write_rejects_a_non_boolean_state_before_the_bridge(self):
        for value in ("true", 1, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.writer.set_mixer_mute(track_index=3, muted=value)
        with self.assertRaises(ValueError):
            self.writer.set_mixer_mute(track_index=-3, muted=True)
        self.assertEqual(self.dispatched(), [])

    # -- built-in EQ -----------------------------------------------------

    def test_eq_write_returns_the_verified_typed_shape(self):
        result = self.writer.set_mixer_eq(
            track_index=3, band_index=1, gain_normalized=0.7
        )
        self.assertIsInstance(result, VerifiedMixerEqWrite)
        self.assert_write_report(result, "mixer.set_eq", 3)
        self.assertIs(result.verified, True)
        self.assertEqual(result.band_index, 1)
        self.assertEqual(result.requested_gain_normalized, 0.7)
        self.assertIsNone(result.requested_frequency_normalized)
        self.assertEqual(result.before.gain_normalized, 0.5)
        self.assertEqual(result.after.gain_normalized, 0.7)
        self.assertIsNotNone(result.after.gain_db)
        self.assertIsNotNone(result.after.frequency_hz)
        self.assertIs(result.gain_verified, True)
        # Null, not false: frequency was not part of this request.
        self.assertIsNone(result.frequency_verified)
        self.assertEqual(_state.TRACKS[3].eq[1]["gain"], 0.7)
        self.assertEqual(self.dispatched(), ["mixer.set_eq"])

    def test_eq_write_reports_an_ignored_write_instead_of_raising(self):
        with mock.patch.object(bridge.mixer, "setEqFrequency", lambda *a, **k: None):
            result = self.writer.set_mixer_eq(
                track_index=3, band_index=1, gain_normalized=0.7,
                frequency_normalized=0.25,
            )
        self.assert_unverified(result)
        # The half that landed still says so; the whole write does not.
        self.assertIs(result.gain_verified, True)
        self.assertIs(result.frequency_verified, False)
        self.assertEqual(_state.TRACKS[3].eq[1]["freq"], 0.5)

    def test_eq_write_refuses_master_unless_asked_for_by_name(self):
        with self.assertRaises(ValueError) as caught:
            self.writer.set_mixer_eq(track_index=0, band_index=0, gain_normalized=0.6)
        self.assertIn("allow_master", str(caught.exception))
        self.assertEqual(self.dispatched(), [])

        result = self.writer.set_mixer_eq(
            track_index=0, band_index=0, gain_normalized=0.6, allow_master=True
        )
        self.assert_write_report(result, "mixer.set_eq", 0, master=True)
        self.assertIs(result.verified, True)

    def test_eq_write_rejects_out_of_range_before_the_bridge(self):
        for arguments in (
            {"band_index": 3, "gain_normalized": 0.6},
            {"band_index": -1, "gain_normalized": 0.6},
            {"band_index": 1, "gain_normalized": 1.5},
            {"band_index": 1, "frequency_normalized": -0.2},
            {"band_index": 1, "gain_normalized": float("nan")},
            # Neither field given: nothing to do, and never a silent no-op.
            {"band_index": 1},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    self.writer.set_mixer_eq(track_index=3, **arguments)
        self.assertEqual(self.dispatched(), [])
        self.assertEqual(_state.TRACKS[3].eq[1]["gain"], 0.5)

    # -- plug-in parameter -----------------------------------------------

    def test_plugin_parameter_write_returns_the_verified_typed_shape(self):
        result = self.writer.set_plugin_parameter(
            track_index=3, slot_index=1, parameter_index=0, normalized_value=0.3
        )
        self.assertIsInstance(result, VerifiedPluginParameterWrite)
        self.assert_write_report(result, "plugin.set_param", 3)
        self.assertIs(result.verified, True)
        self.assertEqual(result.slot_index, 1)
        self.assertEqual(result.parameter_index, 0)
        self.assertEqual(result.plugin_name, "Fruity Compressor")
        self.assertEqual(result.parameter_name, "Threshold")
        self.assertEqual(result.requested_normalized_value, 0.3)
        self.assertIs(result.display_changed, True)
        self.assertEqual(result.verification_basis_detail, "display_change_only")
        self.assertTrue(result.after.display_text_available)
        self.assertNotEqual(result.after.display_text, result.before.display_text)
        self.assertEqual(_state.TRACKS[3].slots[1].values[0], 0.3)
        self.assertEqual(self.dispatched(), ["plugin.set_param"])
        without_basis = result.model_dump()
        without_basis.pop("verification_basis_detail")
        with self.assertRaises(ValidationError):
            VerifiedPluginParameterWrite.model_validate(without_basis)

    def test_plugin_parameter_write_reports_an_ignored_write_instead_of_raising(self):
        with mock.patch.object(bridge.plugins, "setParamValue", lambda *a, **k: 1):
            result = self.writer.set_plugin_parameter(
                track_index=3, slot_index=1, parameter_index=0, normalized_value=0.3
            )
        self.assertIs(result.verified, False)
        self.assertTrue(result.warnings[0].startswith("UNVERIFIED:"))
        self.assertIs(result.display_changed, False)
        self.assertIs(result.reads_at_requested_value, False)
        self.assertEqual(result.verification_basis_detail, "none")
        self.assertEqual(_state.TRACKS[3].slots[1].values[0], 0.6)

    def test_plugin_parameter_write_reports_value_readback_when_already_there(self):
        result = self.writer.set_plugin_parameter(
            track_index=3, slot_index=1, parameter_index=0, normalized_value=0.6
        )
        self.assertIs(result.verified, True)
        self.assertIs(result.reads_at_requested_value, True)
        self.assertEqual(result.verification_basis_detail, "value_readback")

    def test_plugin_parameter_write_derives_basis_for_an_older_bridge(self):
        writer = VerifiedWriter(WriteGateway(PreBasisWriteEnabledClient()))
        result = writer.set_plugin_parameter(
            track_index=3, slot_index=1, parameter_index=0, normalized_value=0.3
        )
        self.assertIs(result.verified, True)
        self.assertEqual(result.verification_basis_detail, "display_change_only")

    def test_plugin_parameter_write_refuses_master_unless_asked_for_by_name(self):
        with self.assertRaises(ValueError) as caught:
            self.writer.set_plugin_parameter(
                track_index=0, slot_index=0, parameter_index=0, normalized_value=0.4
            )
        self.assertIn("allow_master", str(caught.exception))
        self.assertEqual(self.dispatched(), [])

        result = self.writer.set_plugin_parameter(
            track_index=0,
            slot_index=0,
            parameter_index=0,
            normalized_value=0.4,
            allow_master=True,
        )
        self.assert_write_report(result, "plugin.set_param", 0, master=True)
        self.assertIs(result.verified, True)
        self.assertEqual(result.plugin_name, "Fruity Limiter")

    def test_plugin_parameter_write_rejects_out_of_range_before_the_bridge(self):
        for arguments in (
            {"slot_index": 10, "parameter_index": 0, "normalized_value": 0.4},
            {"slot_index": -1, "parameter_index": 0, "normalized_value": 0.4},
            {"slot_index": 1, "parameter_index": -1, "normalized_value": 0.4},
            {"slot_index": 1, "parameter_index": 0, "normalized_value": 1.4},
            {"slot_index": 1, "parameter_index": 0, "normalized_value": -0.1},
            {"slot_index": 1, "parameter_index": 0, "normalized_value": float("inf")},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    self.writer.set_plugin_parameter(track_index=3, **arguments)
        self.assertEqual(self.dispatched(), [])
        self.assertEqual(_state.TRACKS[3].slots[1].values[0], 0.6)


class VerifiedWriteToolTests(unittest.TestCase):
    """All ten writes as the agent reaches them: through the MCP tools."""

    TOOLS = {
        "fl_set_mixer_volume": {"track_index": 3, "volume_normalized": 0.65},
        "fl_set_mixer_pan": {"track_index": 3, "pan": -0.4},
        "fl_set_mixer_mute": {"track_index": 3, "muted": True},
        "fl_set_track_eq": {"track_index": 3, "band_index": 1, "gain_normalized": 0.7},
        "fl_set_plugin_param": {
            "track_index": 3,
            "slot_index": 1,
            "parameter_index": 0,
            "normalized_value": 0.3,
        },
        "fl_set_mixer_name": {"track_index": 3, "name": "Lead Verb"},
        "fl_set_mixer_send": {
            "track_index": 3,
            "destination_track_index": 5,
            "enabled": True,
        },
        "fl_set_mixer_send_level": {
            "track_index": 3,
            "destination_track_index": 0,
            "level_normalized": 0.5,
        },
        "fl_set_plugin_param_display": {
            "track_index": 3,
            "slot_index": 1,
            "parameter": "Threshold",
            "target_value": 40.0,
        },
        "fl_set_plugin_param_option": {
            "track_index": 9,
            "slot_index": 0,
            "parameter": "Key",
            "option": "A",
        },
    }
    EXPECTED_BEFORE = {
        "fl_set_mixer_volume": 0.72,
        "fl_set_mixer_pan": 0.0,
        "fl_set_mixer_mute": False,
        "fl_set_track_eq": {"gain_normalized": 0.5},
        "fl_set_mixer_name": "Lead Vox",
        "fl_set_mixer_send": False,
        "fl_set_mixer_send_level": 0.8,
        "fl_set_plugin_param": {
            "normalized_value": 0.6,
            "display_text": "60.0 %",
        },
        "fl_set_plugin_param_display": {
            "normalized_value": 0.6,
            "display_text": "60.0 %",
        },
        "fl_set_plugin_param_option": {
            "normalized_value": 0.0,
            "display_text": "C",
        },
    }
    STALE_EXPECTED_BEFORE = {
        "fl_set_mixer_volume": 0.1,
        "fl_set_mixer_pan": 0.25,
        "fl_set_mixer_mute": True,
        "fl_set_track_eq": {"gain_normalized": 0.1},
        "fl_set_mixer_name": "A Different Track",
        "fl_set_mixer_send": True,
        "fl_set_mixer_send_level": 0.1,
        "fl_set_plugin_param": {"normalized_value": 0.1},
        "fl_set_plugin_param_display": {"display_text": "not the live display"},
        "fl_set_plugin_param_option": {"display_text": "not the live option"},
    }
    OUT_OF_RANGE = {
        "fl_set_mixer_volume": {"track_index": 3, "volume_normalized": 1.5},
        "fl_set_mixer_pan": {"track_index": 3, "pan": -4.0},
        "fl_set_mixer_mute": {"track_index": -2, "muted": True},
        "fl_set_track_eq": {"track_index": 3, "band_index": 7, "gain_normalized": 0.7},
        "fl_set_plugin_param": {
            "track_index": 3,
            "slot_index": 44,
            "parameter_index": 0,
            "normalized_value": 0.3,
        },
        "fl_set_mixer_name": {"track_index": 3, "name": "x" * 200},
        "fl_set_mixer_send": {
            "track_index": 3,
            "destination_track_index": 3,      # a track cannot send to itself
            "enabled": True,
        },
        "fl_set_mixer_send_level": {
            "track_index": 3,
            "destination_track_index": 0,
            "level_normalized": 9.0,
        },
        "fl_set_plugin_param_display": {
            "track_index": 3,
            "slot_index": 44,
            "parameter": "Threshold",
            "target_value": 40.0,
        },
        "fl_set_plugin_param_option": {
            "track_index": 9,
            "slot_index": 0,
            "parameter": "Key",
            "option": "   ",
        },
    }

    def setUp(self):
        _state.reset()
        self.client = WriteEnabledFakeClient()

    def call(self, name, arguments, client=None):
        from fl_studio_mcp import mcp_server

        target = self.client if client is None else client
        with mock.patch.object(
            mcp_server,
            "VerifiedWriter",
            lambda: VerifiedWriter(WriteGateway(target)),
        ):
            return asyncio.run(mcp.call_tool(name, arguments))

    def structured(self, result):
        self.assertFalse(getattr(result, "is_error", False), result)
        body = result.structured_content
        return body.get("result", body)

    def test_each_write_tool_returns_its_verified_typed_result(self):
        for name, arguments in self.TOOLS.items():
            with self.subTest(tool=name):
                _state.reset()
                self.client = WriteEnabledFakeClient()
                body = self.structured(self.call(name, arguments))
                self.assertIs(body["verified"], True)
                self.assertEqual(body["schema_version"], "1.0")
                self.assertEqual(
                    body["verification_basis"], "readback_on_a_later_fl_idle_tick"
                )
                self.assertIs(body["undo_point_created"], True)
                self.assertIs(body["project_saved"], False)
                self.assertEqual(body["session_fingerprint"], SESSION_FINGERPRINT)
                self.assertIs(body["session_precondition_applied"], False)
                self.assertIs(body["expected_before_applied"], False)
                self.assertEqual(body["track_index"], arguments["track_index"])
                self.assertIs(body["targeted_master"], False)
                self.assertEqual(body["warnings"], [])
                self.assertTrue(body["verification_summary"])
                self.assertEqual(len(self.client.commands), 1)

    def test_all_ten_tools_accept_and_report_matching_optional_preconditions(self):
        for name, arguments in self.TOOLS.items():
            with self.subTest(tool=name):
                _state.reset()
                self.client = WriteEnabledFakeClient()
                guarded = dict(
                    arguments,
                    session_fingerprint=SESSION_FINGERPRINT,
                    expected_before=self.EXPECTED_BEFORE[name],
                )
                body = self.structured(self.call(name, guarded))
                self.assertIs(body["verified"], True)
                self.assertEqual(body["session_fingerprint"], SESSION_FINGERPRINT)
                self.assertIs(body["session_precondition_applied"], True)
                self.assertIs(body["expected_before_applied"], True)
                self.assertEqual(len(self.client.commands), 1)
                sent = self.client.commands[0][1]
                self.assertEqual(sent["session_fingerprint"], SESSION_FINGERPRINT)
                self.assertEqual(sent["expected_before"], self.EXPECTED_BEFORE[name])

    def test_all_ten_tools_refuse_stale_expected_before_without_mutating_or_undo(self):
        for name, arguments in self.TOOLS.items():
            with self.subTest(tool=name):
                _state.reset()
                self.client = WriteEnabledFakeClient()
                before = state_fingerprint()
                stale = dict(
                    arguments,
                    session_fingerprint=SESSION_FINGERPRINT,
                    expected_before=self.STALE_EXPECTED_BEFORE[name],
                )
                with self.assertRaises(ToolError) as caught:
                    self.call(name, stale)
                self.assertIn("expected_before", str(caught.exception))
                self.assertIn("nothing was changed", str(caught.exception))
                self.assertEqual(state_fingerprint(), before)
                self.assertEqual(_state.UNDO, [])
                self.assertEqual(len(self.client.commands), 1)

    def test_all_ten_tools_refuse_a_stale_session_without_mutating_or_undo(self):
        stale_session = "f" * 32
        if stale_session == SESSION_FINGERPRINT:
            stale_session = "e" * 32
        for name, arguments in self.TOOLS.items():
            with self.subTest(tool=name):
                _state.reset()
                self.client = WriteEnabledFakeClient()
                before = state_fingerprint()
                stale = dict(arguments, session_fingerprint=stale_session)
                with self.assertRaises(ToolError) as caught:
                    self.call(name, stale)
                self.assertIn("session", str(caught.exception).lower())
                self.assertEqual(state_fingerprint(), before)
                self.assertEqual(_state.UNDO, [])
                # The fresh handshake catches this before the command is sent;
                # the bridge repeats the check to close the ping/command race.
                self.assertEqual(self.client.commands, [])

    def test_plugin_write_exposes_proof_strength_through_mcp(self):
        body = self.structured(
            self.call("fl_set_plugin_param", self.TOOLS["fl_set_plugin_param"])
        )
        self.assertIs(body["verified"], True)
        self.assertIs(body["display_changed"], True)
        self.assertIs(body["reads_at_requested_value"], False)
        self.assertEqual(body["verification_basis_detail"], "display_change_only")

        _state.reset()
        self.client = WriteEnabledFakeClient()
        already_there = dict(
            self.TOOLS["fl_set_plugin_param"], normalized_value=0.6
        )
        body = self.structured(self.call("fl_set_plugin_param", already_there))
        self.assertIs(body["reads_at_requested_value"], True)
        self.assertEqual(body["verification_basis_detail"], "value_readback")

    def test_unverified_write_reaches_the_agent_as_a_result_not_an_error(self):
        with mock.patch.object(bridge.mixer, "setTrackVolume", lambda *a, **k: None):
            body = self.structured(
                self.call("fl_set_mixer_volume", self.TOOLS["fl_set_mixer_volume"])
            )
        self.assertIs(body["verified"], False)
        self.assertTrue(body["warnings"][0].startswith("UNVERIFIED:"))
        self.assertEqual(body["after_volume_normalized"], 0.72)

    def test_write_tools_name_the_missing_flag_when_the_bridge_cannot_write(self):
        for name, arguments in self.TOOLS.items():
            with self.subTest(tool=name):
                with self.assertRaises(ToolError) as caught:
                    self.call(name, arguments, client=WritesDisabledClient())
                self.assertIn("fl_set_write_mode", str(caught.exception))
                self.assertIn(
                    "confirm_user_present=true",
                    str(caught.exception),
                )

    def test_write_tools_refuse_master_by_default_and_allow_it_explicitly(self):
        for name, arguments in self.TOOLS.items():
            with self.subTest(tool=name):
                _state.reset()
                self.client = WriteEnabledFakeClient()
                # Slot 1 is empty on Master; slot 0 holds the Fruity Limiter.
                master = dict(arguments, track_index=0)
                # Master's slot 0 holds the Fruity Limiter; slot 1 is empty.
                if name.startswith("fl_set_plugin_param"):
                    master["slot_index"] = 0
                if name == "fl_set_plugin_param_display":
                    master["parameter"] = "GAIN"
                    master["target_value"] = 40.0
                if name == "fl_set_plugin_param_option":
                    # That limiter has no text enumeration, so drive the option
                    # matcher on a display string instead -- the same path a
                    # nameless third-party control takes.
                    master["parameter"] = "SAT"
                    master["option"] = "0.0 %"
                if name.startswith("fl_set_mixer_send"):
                    # Master may not send to itself, and a level needs a route.
                    master["destination_track_index"] = 5
                    _state.TRACKS[0].routes[5] = 0.8
                with self.assertRaises(ToolError) as caught:
                    self.call(name, master)
                self.assertIn("allow_master", str(caught.exception))
                self.assertEqual(self.client.commands, [])

                body = self.structured(self.call(name, dict(master, allow_master=True)))
                self.assertIs(body["targeted_master"], True)
                self.assertIs(body["verified"], True)

    def test_write_tools_reject_out_of_range_input_before_the_bridge(self):
        for name, arguments in self.OUT_OF_RANGE.items():
            with self.subTest(tool=name):
                self.client = WriteEnabledFakeClient()
                with self.assertRaises(ToolError):
                    self.call(name, arguments)
                self.assertEqual(self.client.commands, [])

    def test_write_tools_reject_an_unknown_argument(self):
        for name, arguments in self.TOOLS.items():
            with self.subTest(tool=name):
                self.client = WriteEnabledFakeClient()
                with self.assertRaises(ToolError):
                    self.call(name, dict(arguments, nudge_by=0.1))
                self.assertEqual(self.client.commands, [])

    def test_mcp_schema_rejects_malformed_preconditions_before_dispatch(self):
        cases = (
            (
                "fl_set_mixer_volume",
                dict(self.TOOLS["fl_set_mixer_volume"], session_fingerprint="short"),
            ),
            (
                "fl_set_track_eq",
                dict(self.TOOLS["fl_set_track_eq"], expected_before={}),
            ),
            (
                "fl_set_plugin_param",
                dict(self.TOOLS["fl_set_plugin_param"], expected_before={}),
            ),
            (
                "fl_set_plugin_param_option",
                dict(
                    self.TOOLS["fl_set_plugin_param_option"],
                    expected_before={"normalized_value": 2.0},
                ),
            ),
        )
        for name, arguments in cases:
            with self.subTest(tool=name):
                self.client = WriteEnabledFakeClient()
                with self.assertRaises(ToolError):
                    self.call(name, arguments)
                self.assertEqual(self.client.commands, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
