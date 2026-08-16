#!/usr/bin/env python3
"""Hermetic security and contract tests for the 0.12 performance surface."""

from __future__ import annotations

import asyncio
import copy
import math
import unittest
from datetime import datetime, timezone
from typing import Any, get_args
from unittest import mock

from pydantic import ValidationError

from fl_studio_mcp import mcp_server
from fl_studio_mcp.bridge_client import BridgeError
from fl_studio_mcp.bridge_install import expected_bridge_deployment
from fl_studio_mcp.performance import (
    TARGET_AWARE_EXISTING_PLUGIN_TOOLS,
    TEMPO_READBACK_TOLERANCE,
    TRACK_B_MUTATION_COMMANDS,
    TRACK_B_MCP_TOOL_NAMES,
    TRACK_B_READ_COMMANDS,
    TrackBBoundaryViolation,
    TrackBController,
    TrackBInspector,
    TrackBMutationGateway,
    TrackBMutationsUnavailable,
    TrackBReadGateway,
    normalize_plugin_target,
)
from fl_studio_mcp.readonly_inspector import IncompatibleFLStudio
from fl_studio_mcp.track_b_contracts import (
    PLAYBACK_SPEED_OMISSION_REASON,
    ChannelGeneratorTarget,
    ChannelIdentitySnapshot,
    ChannelMixSnapshot,
    ChannelPitchSnapshot,
    ChannelRouteSnapshot,
    ChannelSoloSnapshot,
    ChannelSummary,
    ExpectedChannelIdentityState,
    ExpectedChannelMixState,
    ExpectedChannelPitchState,
    ExpectedChannelRouteState,
    ExpectedChannelSelectionState,
    ExpectedChannelSoloState,
    ExpectedChannelTargetState,
    ExpectedLoopModeState,
    ExpectedPlayingState,
    ExpectedSongPositionState,
    ExpectedStopState,
    ExpectedTempoState,
    LiveNoteDispatch,
    MixerEffectTarget,
    StepCellUpdate,
    StepCellVerification,
    StepSequenceSnapshot,
    TargetedLoadedPluginInventory,
    TargetedPluginParameterPage,
    TargetedPluginParameterScan,
    TrackBResult,
    VerifiedChannelIdentityWrite,
    VerifiedChannelMixWrite,
    VerifiedChannelPitchWrite,
    VerifiedChannelRouteWrite,
    VerifiedChannelSelectionWrite,
    VerifiedChannelSoloWrite,
    VerifiedLoopModeWrite,
    VerifiedPlayingWrite,
    VerifiedSongPositionWrite,
    VerifiedStepSequenceWrite,
    VerifiedStopWrite,
    VerifiedTargetedPluginDisplayWrite,
    VerifiedTargetedPluginOptionWrite,
    VerifiedTargetedPluginParameterWrite,
    VerifiedTempoWrite,
    compute_channel_fingerprint,
    compute_step_sequence_digest,
    normalize_fl_color,
)


SESSION = "a" * 32
OTHER_SESSION = "b" * 32
CHANNEL_FINGERPRINT = "c" * 64


def compatible_ping(
    *, writable: bool = True, session: str | None = SESSION, **overrides: Any
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "pong": True,
        "protocol": 2,
        "program_title": "FL Studio 2026",
        "fl_version": "Producer Edition v26.1.3 [build 5336]",
        "midi_scripting_api_version": 44,
        "bridge_mode": "write_test" if writable else "read_only",
        "verified_writes_enabled": writable,
        "runtime_write_mode_control": True,
        "write_mode_origin": "startup_environment" if writable else "disabled",
        "startup_write_mode_enabled": writable,
        "bridge_source_sha256": expected_bridge_deployment()[1],
        "session_fingerprint": session,
    }
    response.update(overrides)
    return response


class ScriptedClient:
    """Small bridge double with separate handshake and command tripwires."""

    transport = "midi"

    def __init__(
        self,
        handler: Any = None,
        *,
        ping: dict[str, Any] | Exception | None = None,
    ) -> None:
        self.handler = handler
        self.ping_response = compatible_ping() if ping is None else ping
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.ping_count = 0

    def ping(self) -> dict[str, Any]:
        self.ping_count += 1
        if isinstance(self.ping_response, Exception):
            raise self.ping_response
        return copy.deepcopy(self.ping_response)

    def call(self, command: str, **arguments: Any) -> dict[str, Any]:
        self.calls.append((command, copy.deepcopy(arguments)))
        if isinstance(self.handler, Exception):
            raise self.handler
        if callable(self.handler):
            value = self.handler(command, arguments)
        elif isinstance(self.handler, dict) and command in self.handler:
            value = self.handler[command]
        else:
            value = self.handler
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def controller_for(
    handler: Any, *, ping: dict[str, Any] | Exception | None = None
) -> tuple[TrackBController, ScriptedClient]:
    client = ScriptedClient(handler, ping=ping)
    return TrackBController(TrackBMutationGateway(client)), client


def inspector_for(
    handler: Any, *, ping: dict[str, Any] | Exception | None = None
) -> tuple[TrackBInspector, ScriptedClient]:
    client = ScriptedClient(handler, ping=ping)
    return TrackBInspector(TrackBReadGateway(client)), client


def mutation_envelope(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "command": command,
        "session_fingerprint": SESSION,
        "session_precondition_applied": "session_fingerprint" in arguments,
        "expected_before_applied": "expected_before" in arguments,
        "undo_point_created": False,
        "warnings": [],
    }


def transport_handler(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = mutation_envelope(command, arguments)
    if command == "transport.set_playing":
        response.update(before=False, after=arguments["playing"], verified=True)
    elif command == "transport.stop":
        response.update(
            before={"playing": True, "position": 0.375},
            after={"playing": False, "position": 0.0},
            verified_fields={"playing": True, "position": True},
            verified=True,
        )
    elif command == "transport.set_song_position":
        response.update(before=0.1, after=arguments["position"], verified=True)
    elif command == "transport.set_loop_mode":
        response.update(before="song", after=arguments["loop_mode"], verified=True)
    elif command == "transport.set_tempo":
        response.update(before=120.0, after=arguments["tempo_bpm"], verified=True)
    else:  # pragma: no cover - a test setup error should be loud
        raise AssertionError(command)
    return response


def channel_handler(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = mutation_envelope(command, arguments)
    response["index_scope"] = "global"
    if command == "channel.set_mix":
        requested = {
            key: arguments[key]
            for key in ("volume", "pan", "muted")
            if key in arguments
        }
        response.update(
            channel=arguments["channel"],
            before={
                "volume": 0.5,
                "pan": 0.0,
                "muted": False,
                "channel_fingerprint": CHANNEL_FINGERPRINT,
            },
            after={
                "volume": requested.get("volume", 0.5),
                "pan": requested.get("pan", 0.0),
                "muted": requested.get("muted", False),
                "channel_fingerprint": CHANNEL_FINGERPRINT,
            },
            verified_fields={key: True for key in requested},
            verified=True,
        )
    elif command == "channel.set_identity":
        requested = {
            key: arguments[key] for key in ("name", "color") if key in arguments
        }
        response.update(
            channel=arguments["channel"],
            before={
                "name": "Before",
                "color": 1,
                "channel_fingerprint": CHANNEL_FINGERPRINT,
            },
            after={
                "name": requested.get("name", "Before"),
                "color": requested.get("color", 1),
                "channel_fingerprint": "d" * 64,
            },
            verified_fields={key: True for key in requested},
            verified=True,
        )
    elif command == "channel.set_solo":
        response.update(
            channel=arguments["channel"],
            before={
                "soloed": False,
                "channel_fingerprint": CHANNEL_FINGERPRINT,
            },
            after={
                "soloed": arguments["soloed"],
                "channel_fingerprint": CHANNEL_FINGERPRINT,
            },
            verified=True,
        )
    elif command == "channel.set_pitch":
        response.update(
            channel=arguments["channel"],
            before={
                "pitch": 0.0,
                "pitch_semitones": 0.0,
                "pitch_range": 2.0,
                "channel_fingerprint": CHANNEL_FINGERPRINT,
            },
            after={
                "pitch": arguments["pitch"],
                "pitch_semitones": arguments["pitch"] * 2.0,
                "pitch_range": 2.0,
                "channel_fingerprint": CHANNEL_FINGERPRINT,
            },
            verified=True,
        )
    elif command == "channel.select":
        response.update(
            channel=arguments["channel"],
            exclusive=True,
            before=[0],
            after=[arguments["channel"]],
            verified=True,
            undo_point_created=None,
        )
    elif command == "channel.route_to_mixer":
        response.update(
            channel=arguments["channel"],
            before={
                "mixer_destination": 2,
                "channel_fingerprint": CHANNEL_FINGERPRINT,
            },
            after={
                "mixer_destination": arguments["destination"],
                "channel_fingerprint": "e" * 64,
            },
            verified=True,
        )
    else:  # pragma: no cover
        raise AssertionError(command)
    return response


def step_snapshot(
    *, pattern: int = 2, channel: int = 3, cells: list[bool] | None = None
) -> dict[str, Any]:
    values = [True, False, False, True] if cells is None else list(cells)
    return {
        "pattern": pattern,
        "current_pattern": pattern,
        "channel": channel,
        "index_scope": "global",
        "step_count": len(values),
        "grid_resolution": "sixteenth_note",
        "cells": values,
        "digest_algorithm": "sha256-canonical-json-v1",
        "digest": compute_step_sequence_digest(
            pattern_number=pattern,
            channel_index=channel,
            step_count=len(values),
            cells=values,
        ),
    }


def step_write_handler(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if command != "sequencer.set":  # pragma: no cover
        raise AssertionError(command)
    before = step_snapshot(pattern=arguments["pattern"], channel=arguments["channel"])
    if before["digest"] != arguments["expected_digest"]:
        raise AssertionError("test passed a digest for a different before-grid")
    after_cells = list(before["cells"])
    proofs = []
    for update in arguments["updates"]:
        after_cells[update["step_index"]] = update["enabled"]
        proofs.append(
            {
                "step_index": update["step_index"],
                "requested_enabled": update["enabled"],
                "after_enabled": update["enabled"],
                "verified": True,
            }
        )
    response = mutation_envelope(command, arguments)
    response.update(
        pattern=arguments["pattern"],
        channel=arguments["channel"],
        index_scope="global",
        expected_digest=arguments["expected_digest"],
        expected_before_applied=True,
        before=before,
        after=step_snapshot(
            pattern=arguments["pattern"],
            channel=arguments["channel"],
            cells=after_cells,
        ),
        verified_cells=proofs,
        verified=True,
    )
    return response


class GatewayBoundaryTests(unittest.TestCase):
    def test_allowlists_are_exact_and_disjoint_by_mutability(self) -> None:
        self.assertEqual(
            TRACK_B_READ_COMMANDS,
            {
                "project.info",
                "project.history",
                "channels.list",
                "mixer.list",
                "plugin.params",
                "plugin.scan_params",
                "plugin.preset_count",
                "sequencer.get",
                "patterns.list",
                "patterns.find_empty",
                "playlist.list",
            },
        )
        self.assertEqual(
            TRACK_B_MUTATION_COMMANDS,
            {
                "transport.set_playing",
                "transport.stop",
                "transport.set_song_position",
                "transport.set_loop_mode",
                "transport.set_tempo",
                "transport.set_recording",
                "transport.set_metronome",
                "transport.set_precount",
                "project.set_time_signature_numerator",
                "project.undo",
                "project.redo",
                "channel.set_mix",
                "channel.set_solo",
                "channel.set_pitch",
                "channel.select",
                "channel.set_identity",
                "channel.route_to_mixer",
                "pattern.select",
                "pattern.set_identity",
                "pattern.set_length",
                "playlist.set_identity",
                "playlist.set_state",
                "plugin.set_param",
                "plugin.set_param_display",
                "plugin.set_param_option",
                "sequencer.set",
                "channel.trigger_note",
            },
        )
        self.assertTrue(TRACK_B_READ_COMMANDS.isdisjoint(TRACK_B_MUTATION_COMMANDS))

    def test_gateways_reject_cross_boundary_and_arbitrary_commands(self) -> None:
        client = ScriptedClient(lambda command, arguments: {})
        read = TrackBReadGateway(client)
        write = TrackBMutationGateway(client)
        for command in (*TRACK_B_MUTATION_COMMANDS, "call", "project.save"):
            with self.subTest(gateway="read", command=command):
                with self.assertRaises(TrackBBoundaryViolation):
                    read.call(command)
        for command in (*TRACK_B_READ_COMMANDS, "call", "project.save"):
            with self.subTest(gateway="write", command=command):
                with self.assertRaises(TrackBBoundaryViolation):
                    write.call(command)
        self.assertEqual(client.calls, [])

    def test_gateways_reject_non_mapping_bridge_replies(self) -> None:
        for gateway, command in (
            (TrackBReadGateway(ScriptedClient([])), "channels.list"),
            (TrackBMutationGateway(ScriptedClient([])), "transport.stop"),
        ):
            with self.subTest(command=command):
                with self.assertRaisesRegex(ValueError, "malformed reply"):
                    gateway.call(command)

    def test_ambiguous_mutation_transport_failure_is_never_replayed(self) -> None:
        controller, client = controller_for(BridgeError("reply lost"))
        with self.assertRaisesRegex(BridgeError, "reply lost"):
            controller.set_playing(playing=True)
        self.assertEqual([name for name, _ in client.calls], ["transport.set_playing"])

    def test_unknown_transport_name_is_normalized(self) -> None:
        client = ScriptedClient({})
        client.transport = "surprise"
        self.assertEqual(TrackBReadGateway(client).transport, "unknown")
        self.assertEqual(TrackBMutationGateway(client).transport, "unknown")


class MutationGateTests(unittest.TestCase):
    def test_read_only_bridge_refuses_before_dispatch(self) -> None:
        controller, client = controller_for(
            transport_handler, ping=compatible_ping(writable=False)
        )
        with self.assertRaisesRegex(TrackBMutationsUnavailable, "fl_set_write_mode"):
            controller.set_playing(playing=True)
        self.assertEqual(client.calls, [])

    def test_mismatched_bridge_provenance_refuses_before_dispatch(self) -> None:
        controller, client = controller_for(
            transport_handler,
            ping=compatible_ping(bridge_source_sha256="0" * 64),
        )
        with self.assertRaisesRegex(TrackBMutationsUnavailable, "source SHA-256"):
            controller.set_playing(playing=True)
        self.assertEqual(client.calls, [])

    def test_incompatible_handshake_refuses_reads_before_dispatch(self) -> None:
        inspector, client = inspector_for(
            {}, ping=compatible_ping(pong=False)
        )
        with self.assertRaisesRegex(IncompatibleFLStudio, "pong=true"):
            inspector.list_channels()
        self.assertEqual(client.calls, [])

    def test_malformed_requested_session_refuses_before_dispatch(self) -> None:
        controller, client = controller_for(transport_handler)
        for session in ("short", "A" * 32, "g" * 32):
            with self.subTest(session=session):
                with self.assertRaisesRegex(ValueError, "32 lowercase hex"):
                    controller.set_playing(
                        playing=True, session_fingerprint=session
                    )
        self.assertEqual(client.calls, [])

    def test_stale_or_malformed_handshake_session_refuses_before_dispatch(self) -> None:
        for reported in (OTHER_SESSION, "not-a-session"):
            controller, client = controller_for(
                transport_handler, ping=compatible_ping(session=reported)
            )
            with self.subTest(reported=reported):
                with self.assertRaisesRegex(
                    TrackBMutationsUnavailable, "session precondition failed"
                ):
                    controller.set_playing(
                        playing=True, session_fingerprint=SESSION
                    )
            self.assertEqual(client.calls, [])

    def test_reply_from_a_changed_session_is_rejected(self) -> None:
        def changed(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            response = transport_handler(command, arguments)
            response["session_fingerprint"] = OTHER_SESSION
            return response

        controller, _client = controller_for(changed)
        with self.assertRaisesRegex(ValueError, "session changed"):
            controller.set_playing(playing=True)

    def test_reply_must_truthfully_report_each_precondition(self) -> None:
        def contradictory(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            response = transport_handler(command, arguments)
            response["session_precondition_applied"] = False
            return response

        controller, _client = controller_for(contradictory)
        with self.assertRaisesRegex(ValueError, "session-precondition metadata"):
            controller.set_playing(playing=True, session_fingerprint=SESSION)


class TransportControllerTests(unittest.TestCase):
    def test_one_based_project_history_accepts_independent_last_cursor(self) -> None:
        inspector, client = inspector_for(
            {
                "project.history": {
                    "command": "project.history",
                    "position": 1,
                    "count": 1,
                    "last_position": 0,
                    "level_hint": "1 / 1",
                    "project_dirty_flag": 0,
                    "can_undo": False,
                    "can_redo": False,
                }
            }
        )

        observed = inspector.project_history()

        self.assertEqual(observed.history.position, 1)
        self.assertEqual(observed.history.count, 1)
        self.assertEqual(observed.history.last_position, 0)
        self.assertFalse(observed.history.can_undo)
        self.assertFalse(observed.history.can_redo)
        self.assertEqual(client.calls, [("project.history", {})])

    def test_set_playing_returns_typed_absolute_result(self) -> None:
        controller, client = controller_for(transport_handler)
        result = controller.set_playing(
            playing=True,
            session_fingerprint=SESSION,
            expected_before=ExpectedPlayingState(playing=False),
        )
        self.assertIsInstance(result, VerifiedPlayingWrite)
        self.assertTrue(result.verified)
        self.assertEqual((result.before_playing, result.after_playing), (False, True))
        self.assertFalse(result.project_saved)
        self.assertEqual(result.verification_basis, "readback_on_a_later_fl_idle_tick")
        command, arguments = client.calls[-1]
        self.assertEqual(command, "transport.set_playing")
        self.assertEqual(arguments["playing"], True)
        self.assertEqual(arguments["expected_before"], {"playing": False})

    def test_stop_requires_both_state_fields_to_verify(self) -> None:
        controller, _client = controller_for(transport_handler)
        result = controller.stop(
            session_fingerprint=SESSION,
            expected_before=ExpectedStopState(playing=True),
        )
        self.assertIsInstance(result, VerifiedStopWrite)
        self.assertTrue(result.playing_verified)
        self.assertTrue(result.position_verified)
        self.assertTrue(result.verified)
        self.assertEqual(result.after_song_position_normalized, 0.0)

    def test_set_song_position_returns_requested_value_and_tolerance(self) -> None:
        controller, _client = controller_for(transport_handler)
        result = controller.set_song_position(
            position_normalized=0.625,
            tolerance=0.001,
            expected_before=ExpectedSongPositionState(song_position_normalized=0.1),
        )
        self.assertIsInstance(result, VerifiedSongPositionWrite)
        self.assertEqual(result.requested_song_position_normalized, 0.625)
        self.assertEqual(result.after_song_position_normalized, 0.625)
        self.assertEqual(result.tolerance, 0.001)

    def test_set_loop_mode_is_an_absolute_enum(self) -> None:
        controller, _client = controller_for(transport_handler)
        result = controller.set_loop_mode(
            loop_mode="pattern",
            expected_before=ExpectedLoopModeState(loop_mode="song"),
        )
        self.assertIsInstance(result, VerifiedLoopModeWrite)
        self.assertEqual(result.requested_loop_mode, "pattern")
        self.assertEqual(result.after_loop_mode, "pattern")

    def test_set_tempo_returns_typed_bpm_result(self) -> None:
        controller, _client = controller_for(transport_handler)
        result = controller.set_tempo(
            tempo_bpm=137.5,
            expected_before=ExpectedTempoState(tempo_bpm=120.0),
        )
        self.assertIsInstance(result, VerifiedTempoWrite)
        self.assertEqual(result.requested_tempo_bpm, 137.5)
        self.assertEqual(result.after_tempo_bpm, 137.5)

    def test_tempo_uses_the_bridge_readback_tolerance(self) -> None:
        self.assertEqual(TEMPO_READBACK_TOLERANCE, 1e-3)

        def within_tolerance(
            command: str, arguments: dict[str, Any]
        ) -> dict[str, Any]:
            response = transport_handler(command, arguments)
            response["after"] = arguments["tempo_bpm"] + 5e-4
            return response

        controller, _client = controller_for(within_tolerance)
        result = controller.set_tempo(tempo_bpm=137.5)
        self.assertTrue(result.verified)

        def outside_tolerance(
            command: str, arguments: dict[str, Any]
        ) -> dict[str, Any]:
            response = transport_handler(command, arguments)
            response["after"] = arguments["tempo_bpm"] + 1.5e-3
            return response

        controller, _client = controller_for(outside_tolerance)
        with self.assertRaisesRegex(ValueError, "tempo.*verified|verified.*tempo"):
            controller.set_tempo(tempo_bpm=137.5)

    def test_transport_inputs_are_strict_and_bounded_before_dispatch(self) -> None:
        cases = (
            ("playing", lambda c: c.set_playing(playing=1)),
            ("position", lambda c: c.set_song_position(position_normalized=1.1)),
            ("position nan", lambda c: c.set_song_position(position_normalized=math.nan)),
            ("tolerance", lambda c: c.set_song_position(position_normalized=0.2, tolerance=0.1)),
            ("loop", lambda c: c.set_loop_mode(loop_mode="toggle")),
            ("tempo", lambda c: c.set_tempo(tempo_bpm=9.9)),
        )
        for label, invoke in cases:
            controller, client = controller_for(transport_handler)
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    invoke(controller)
            self.assertEqual(client.calls, [])

    def test_stop_contract_rejects_aggregate_or_field_contradictions(self) -> None:
        controller, _client = controller_for(transport_handler)

        def one_field_failed(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            response = transport_handler(command, arguments)
            response["verified_fields"]["position"] = False
            response["verified"] = True
            return response

        controller, _client = controller_for(one_field_failed)
        with self.assertRaisesRegex(ValidationError, "AND"):
            controller.stop()

    def test_verified_transport_results_require_matching_later_tick_readback(self) -> None:
        cases = (
            (
                "playing",
                lambda response: response.update(after=False),
                lambda controller: controller.set_playing(playing=True),
            ),
            (
                "stop",
                lambda response: response["after"].update(
                    playing=True, position=0.25
                ),
                lambda controller: controller.stop(),
            ),
            (
                "song position",
                lambda response: response.update(after=0.1),
                lambda controller: controller.set_song_position(
                    position_normalized=0.625
                ),
            ),
            (
                "loop mode",
                lambda response: response.update(after="song"),
                lambda controller: controller.set_loop_mode(loop_mode="pattern"),
            ),
            (
                "tempo",
                lambda response: response.update(after=120.0),
                lambda controller: controller.set_tempo(tempo_bpm=137.5),
            ),
        )
        for label, corrupt, invoke in cases:
            def contradictory(
                command: str,
                arguments: dict[str, Any],
                corrupt=corrupt,
            ) -> dict[str, Any]:
                response = transport_handler(command, arguments)
                corrupt(response)
                return response

            controller, _client = controller_for(contradictory)
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    (ValueError, ValidationError), "verified|readback|requested|AND"
                ):
                    invoke(controller)


class ChannelReadAndFingerprintTests(unittest.TestCase):
    @staticmethod
    def channel_reply(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if command != "channels.list":  # pragma: no cover
            raise AssertionError(command)
        return {
            "command": "channels.list",
            "channel_count": 1,
            "partial": False,
            "unsaved_changes": 0,
            "channels": [
                {
                    "index": 7,
                    "index_scope": "global",
                    "name": "Demo Channel",
                    "type": 2,
                    "type_name": "generator_plugin",
                    "color": 123,
                    "volume": 0.7,
                    "pan": -0.2,
                    "muted": False,
                    "solo": False,
                    "selected": True,
                    "mixer_track": 4,
                    "plugin": "MiniSynth",
                    "reported_parameter_count": 16,
                }
            ],
        }

    def test_channel_fingerprint_is_deterministic_and_identity_sensitive(self) -> None:
        arguments = dict(
            channel_index=7,
            name="Demo Channel",
            channel_type_code=2,
            color=123,
            mixer_destination=4,
            generator_name="MiniSynth",
        )
        first = compute_channel_fingerprint(**arguments)
        self.assertEqual(first, compute_channel_fingerprint(**arguments))
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        for field, changed in (
            ("channel_index", 8),
            ("name", "Other"),
            ("channel_type_code", 3),
            ("color", 124),
            ("mixer_destination", 5),
            ("generator_name", "OtherSynth"),
        ):
            with self.subTest(field=field):
                altered = dict(arguments, **{field: changed})
                self.assertNotEqual(first, compute_channel_fingerprint(**altered))

    def test_list_channels_computes_global_observation_scoped_identity(self) -> None:
        inspector, client = inspector_for(self.channel_reply)
        result = inspector.list_channels()
        self.assertEqual(result.total_channel_count, 1)
        channel = result.channels[0]
        self.assertEqual(channel.channel_index, 7)
        self.assertEqual(channel.index_scope, "global")
        self.assertEqual(channel.identity_scope, "observation_scoped_not_durable")
        self.assertEqual(channel.generator.name, "MiniSynth")
        self.assertEqual(
            channel.channel_fingerprint,
            compute_channel_fingerprint(
                channel_index=7,
                name="Demo Channel",
                channel_type_code=2,
                color=123,
                mixer_destination=4,
                generator_name="MiniSynth",
            ),
        )
        self.assertEqual(client.calls, [("channels.list", {"global_count": True})])

    def test_signed_fl_color_is_published_and_fingerprinted_as_unsigned(self) -> None:
        signed = -13880523
        unsigned = 0xFF2C3335

        def signed_reply(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            response = self.channel_reply(command, arguments)
            row = response["channels"][0]
            row["color"] = signed
            row["channel_fingerprint"] = compute_channel_fingerprint(
                channel_index=7,
                name="Demo Channel",
                channel_type_code=2,
                color=signed,
                mixer_destination=4,
                generator_name="MiniSynth",
            )
            return response

        inspector, _client = inspector_for(signed_reply)
        channel = inspector.list_channels().channels[0]
        self.assertEqual(channel.color, unsigned)
        self.assertEqual(
            channel.channel_fingerprint,
            compute_channel_fingerprint(
                channel_index=7,
                name="Demo Channel",
                channel_type_code=2,
                color=unsigned,
                mixer_destination=4,
                generator_name="MiniSynth",
            ),
        )

    def test_color_normalizer_preserves_exact_32_bit_word(self) -> None:
        self.assertEqual(normalize_fl_color(-13880523), 0xFF2C3335)
        self.assertEqual(normalize_fl_color(0xFF2C3335), 0xFF2C3335)
        self.assertEqual(normalize_fl_color(-(1 << 31)), 1 << 31)
        self.assertEqual(normalize_fl_color(0xFFFFFFFF), 0xFFFFFFFF)
        fingerprint_arguments = dict(
            channel_index=7,
            name="Demo Channel",
            channel_type_code=2,
            mixer_destination=4,
            generator_name="MiniSynth",
        )
        self.assertEqual(
            compute_channel_fingerprint(
                **fingerprint_arguments, color=-13880523
            ),
            compute_channel_fingerprint(
                **fingerprint_arguments, color=0xFF2C3335
            ),
        )
        for malformed in (True, -((1 << 31) + 1), 1 << 32, 1.5, "1"):
            with self.subTest(value=malformed):
                with self.assertRaises(ValueError):
                    normalize_fl_color(malformed)  # type: ignore[arg-type]

    def test_list_channels_rejects_contradictory_bridge_fingerprint(self) -> None:
        def contradictory(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            response = self.channel_reply(command, arguments)
            response["channels"][0]["channel_fingerprint"] = "0" * 64
            return response

        inspector, _client = inspector_for(contradictory)
        with self.assertRaisesRegex(ValueError, "contradictory fingerprint"):
            inspector.list_channels()

    def test_channel_contract_independently_recomputes_fingerprint(self) -> None:
        with self.assertRaisesRegex(ValidationError, "does not match"):
            ChannelSummary(
                channel_index=1,
                name="Kick",
                channel_fingerprint="0" * 64,
            )

    def test_channel_list_rejects_non_global_scope_and_malformed_rows(self) -> None:
        bad_values = ("group", 3, None)
        for value in bad_values:
            def bad(command: str, arguments: dict[str, Any], value=value) -> dict[str, Any]:
                response = self.channel_reply(command, arguments)
                if value is None:
                    response["channels"] = ["not a row"]
                else:
                    response["channels"][0]["index_scope"] = value
                return response

            inspector, _client = inspector_for(bad)
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    inspector.list_channels()

        def missing_scope(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            response = self.channel_reply(command, arguments)
            response["channels"][0].pop("index_scope")
            return response

        inspector, _client = inspector_for(missing_scope)
        with self.assertRaisesRegex(ValueError, "explicitly.*global"):
            inspector.list_channels()


def verified_contract_fields(*, verified: bool = True) -> dict[str, Any]:
    return {
        "applied_at": datetime.now(timezone.utc),
        "verified": verified,
        "verification_summary": "test proof",
    }


class ChannelMutationTests(unittest.TestCase):
    def test_all_channel_mutation_contracts_are_public_results(self) -> None:
        mutation_types = {
            value
            for value in get_args(TrackBResult)
            if isinstance(value, type) and value.__name__.startswith("VerifiedChannel")
        }
        self.assertEqual(
            mutation_types,
            {
                VerifiedChannelMixWrite,
                VerifiedChannelIdentityWrite,
                VerifiedChannelRouteWrite,
                VerifiedChannelSoloWrite,
                VerifiedChannelPitchWrite,
                VerifiedChannelSelectionWrite,
            },
        )

    def test_mix_result_has_nullable_flags_only_for_requested_fields(self) -> None:
        controller, client = controller_for(channel_handler)
        result = controller.set_channel_mix(
            channel_index=3,
            volume_normalized=0.75,
            muted=True,
            expected_before=ExpectedChannelMixState(
                channel_fingerprint=CHANNEL_FINGERPRINT,
                volume_normalized=0.5,
                muted=False,
            ),
        )
        self.assertIsInstance(result, VerifiedChannelMixWrite)
        self.assertTrue(result.volume_verified)
        self.assertIsNone(result.pan_verified)
        self.assertTrue(result.mute_verified)
        self.assertTrue(result.verified)
        command, arguments = client.calls[-1]
        self.assertEqual(command, "channel.set_mix")
        self.assertEqual(arguments["index_scope"], "global")
        self.assertNotIn("pan", arguments)

    def test_solo_pitch_and_exclusive_selection_are_typed_and_verified(self) -> None:
        controller, client = controller_for(channel_handler)
        solo = controller.set_channel_solo(
            channel_index=3,
            soloed=True,
            expected_before=ExpectedChannelSoloState(soloed=False),
        )
        pitch = controller.set_channel_pitch(
            channel_index=3,
            pitch_normalized=0.5,
            expected_before=ExpectedChannelPitchState(pitch_normalized=0.0),
        )
        selection = controller.select_channel(
            channel_index=3,
            expected_before=ExpectedChannelSelectionState(
                selected_channel_indices=[0]
            ),
        )
        self.assertIsInstance(solo, VerifiedChannelSoloWrite)
        self.assertIsInstance(solo.before, ChannelSoloSnapshot)
        self.assertTrue(solo.after.soloed)
        self.assertIsInstance(pitch, VerifiedChannelPitchWrite)
        self.assertIsInstance(pitch.after, ChannelPitchSnapshot)
        self.assertEqual(pitch.after.pitch_semitones, 1.0)
        self.assertIsInstance(selection, VerifiedChannelSelectionWrite)
        self.assertEqual(selection.after_selected_channel_indices, [3])
        self.assertIsNone(selection.undo_point_created)
        self.assertEqual(
            [command for command, _arguments in client.calls],
            ["channel.set_solo", "channel.set_pitch", "channel.select"],
        )

    def test_identity_result_has_nullable_flags_and_aggregate_and(self) -> None:
        controller, _client = controller_for(channel_handler)
        result = controller.set_channel_identity(channel_index=3, name="Bass")
        self.assertIsInstance(result, VerifiedChannelIdentityWrite)
        self.assertTrue(result.name_verified)
        self.assertIsNone(result.color_verified)
        self.assertTrue(result.verified)

    def test_high_bit_color_write_and_restore_normalize_signed_bridge_readback(self) -> None:
        baseline = 0xFF2C3335
        alternate = 0xFE102030
        state = [baseline - (1 << 32)]

        def signed_handler(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self.assertEqual(command, "channel.set_identity")
            before = state[0]
            requested = arguments["color"]
            state[0] = requested - (1 << 32) if requested >= (1 << 31) else requested
            response = mutation_envelope(command, arguments)
            response.update(
                channel=arguments["channel"],
                index_scope="global",
                before={
                    "name": "Before",
                    "color": before,
                    "channel_fingerprint": CHANNEL_FINGERPRINT,
                },
                after={
                    "name": "Before",
                    "color": state[0],
                    "channel_fingerprint": "d" * 64,
                },
                verified_fields={"color": True},
                verified=True,
            )
            return response

        controller, client = controller_for(signed_handler)
        changed = controller.set_channel_identity(
            channel_index=3,
            color=alternate,
            expected_before=ExpectedChannelIdentityState(color=baseline),
        )
        restored = controller.set_channel_identity(
            channel_index=3,
            color=baseline,
            expected_before=ExpectedChannelIdentityState(color=alternate),
        )

        self.assertEqual(changed.before.color, baseline)
        self.assertEqual(changed.after.color, alternate)
        self.assertTrue(changed.color_verified)
        self.assertTrue(changed.verified)
        self.assertEqual(restored.before.color, alternate)
        self.assertEqual(restored.after.color, baseline)
        self.assertTrue(restored.verified)
        self.assertEqual(state[0], baseline - (1 << 32))
        self.assertEqual(client.calls[0][1]["color"], alternate)
        self.assertEqual(client.calls[0][1]["expected_before"]["color"], baseline)
        self.assertEqual(client.calls[1][1]["color"], baseline)
        self.assertEqual(client.calls[1][1]["expected_before"]["color"], alternate)

    def test_color_write_accepts_fl_owned_high_byte_in_later_readback(self) -> None:
        baseline = 0xFF2C3335
        requested = 0x0055AA

        def opaque_high_byte_handler(
            command: str, arguments: dict[str, Any]
        ) -> dict[str, Any]:
            response = mutation_envelope(command, arguments)
            response.update(
                channel=arguments["channel"],
                index_scope="global",
                before={
                    "name": "Before",
                    "color": baseline,
                    "channel_fingerprint": CHANNEL_FINGERPRINT,
                },
                after={
                    "name": "Before",
                    "color": 0xFF000000 | arguments["color"],
                    "channel_fingerprint": "d" * 64,
                },
                verified_fields={"color": True},
                verified=True,
            )
            return response

        controller, client = controller_for(opaque_high_byte_handler)
        result = controller.set_channel_identity(
            channel_index=3,
            color=requested,
            expected_before=ExpectedChannelIdentityState(color=baseline & 0xFFFFFF),
        )

        self.assertTrue(result.verified)
        self.assertTrue(result.color_verified)
        self.assertEqual(result.requested_color, requested)
        self.assertEqual(result.after.color, 0xFF0055AA)
        self.assertEqual(
            client.calls[0][1]["expected_before"]["color"], baseline & 0xFFFFFF
        )

    def test_color_write_rejects_contradictory_low_24_bit_readback(self) -> None:
        def contradictory(
            command: str, arguments: dict[str, Any]
        ) -> dict[str, Any]:
            response = mutation_envelope(command, arguments)
            response.update(
                channel=arguments["channel"],
                index_scope="global",
                before={
                    "name": "Before",
                    "color": 0xFF2C3335,
                    "channel_fingerprint": CHANNEL_FINGERPRINT,
                },
                after={
                    "name": "Before",
                    "color": 0xFF0055AB,
                    "channel_fingerprint": "d" * 64,
                },
                verified_fields={"color": True},
                verified=True,
            )
            return response

        controller, _client = controller_for(contradictory)
        with self.assertRaisesRegex(ValueError, "channel color"):
            controller.set_channel_identity(channel_index=3, color=0x0055AA)

    def test_route_result_is_the_third_absolute_channel_mutation(self) -> None:
        controller, client = controller_for(channel_handler)
        result = controller.route_channel_to_mixer(
            channel_index=3,
            mixer_destination=-1,
            expected_before=ExpectedChannelRouteState(mixer_destination=2),
        )
        self.assertIsInstance(result, VerifiedChannelRouteWrite)
        self.assertEqual(result.requested_mixer_destination, -1)
        self.assertEqual(result.after.mixer_destination, -1)
        self.assertEqual(client.calls[-1][1]["destination"], -1)

    def test_channel_mutations_require_explicit_global_scope_echo(self) -> None:
        cases = (
            (
                "mix",
                lambda controller: controller.set_channel_mix(
                    channel_index=3, muted=True
                ),
            ),
            (
                "identity",
                lambda controller: controller.set_channel_identity(
                    channel_index=3, name="Bass"
                ),
            ),
            (
                "route",
                lambda controller: controller.route_channel_to_mixer(
                    channel_index=3, mixer_destination=2
                ),
            ),
        )
        for label, invoke in cases:
            for scope in (None, "grouped"):
                def bad_scope(
                    command: str,
                    arguments: dict[str, Any],
                    scope=scope,
                ) -> dict[str, Any]:
                    response = channel_handler(command, arguments)
                    if scope is None:
                        response.pop("index_scope")
                    else:
                        response["index_scope"] = scope
                    return response

                controller, _client = controller_for(bad_scope)
                with self.subTest(label=label, scope=scope):
                    with self.assertRaisesRegex(ValueError, "global channel"):
                        invoke(controller)

    def test_grouped_contracts_reject_unrequested_proof_flags(self) -> None:
        with self.assertRaisesRegex(ValidationError, "only requested"):
            VerifiedChannelMixWrite(
                **verified_contract_fields(),
                channel_index=1,
                requested_volume_normalized=0.5,
                before=ChannelMixSnapshot(),
                after=ChannelMixSnapshot(),
                volume_verified=True,
                pan_verified=True,
            )
        with self.assertRaisesRegex(ValidationError, "only requested"):
            VerifiedChannelIdentityWrite(
                **verified_contract_fields(),
                channel_index=1,
                requested_name="Lead",
                before=ChannelIdentitySnapshot(),
                after=ChannelIdentitySnapshot(),
                name_verified=True,
                color_verified=True,
            )

    def test_grouped_contracts_reject_aggregate_or_per_field_disagreement(self) -> None:
        with self.assertRaisesRegex(ValidationError, "AND"):
            VerifiedChannelMixWrite(
                **verified_contract_fields(verified=True),
                channel_index=1,
                requested_volume_normalized=0.5,
                requested_pan=0.2,
                before=ChannelMixSnapshot(),
                after=ChannelMixSnapshot(),
                volume_verified=True,
                pan_verified=False,
            )
        with self.assertRaisesRegex(ValidationError, "AND"):
            VerifiedChannelIdentityWrite(
                **verified_contract_fields(verified=False),
                channel_index=1,
                requested_name="Lead",
                before=ChannelIdentitySnapshot(),
                after=ChannelIdentitySnapshot(),
                name_verified=True,
            )

    def test_channel_inputs_fail_before_dispatch(self) -> None:
        cases = (
            lambda c: c.set_channel_mix(channel_index=1),
            lambda c: c.set_channel_mix(channel_index=True, muted=False),
            lambda c: c.set_channel_mix(channel_index=1, volume_normalized=1.1),
            lambda c: c.set_channel_mix(channel_index=1, muted=1),
            lambda c: c.set_channel_identity(channel_index=1),
            lambda c: c.set_channel_identity(channel_index=1, name="x" * 65),
            lambda c: c.route_channel_to_mixer(channel_index=1, mixer_destination=-2),
        )
        for invoke in cases:
            controller, client = controller_for(channel_handler)
            with self.assertRaises(ValueError):
                invoke(controller)
            self.assertEqual(client.calls, [])

    def test_missing_or_malformed_per_field_proof_is_rejected(self) -> None:
        def malformed(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            response = channel_handler(command, arguments)
            response["verified_fields"] = {"volume": "yes"}
            return response

        controller, _client = controller_for(malformed)
        with self.assertRaisesRegex(ValueError, "requested field"):
            controller.set_channel_mix(channel_index=1, volume_normalized=0.5)

    def test_verified_channel_results_require_matching_after_snapshots(self) -> None:
        cases = (
            (
                "mix",
                "channel.set_mix",
                lambda response: response["after"].pop("volume"),
                lambda controller: controller.set_channel_mix(
                    channel_index=1, volume_normalized=0.75
                ),
            ),
            (
                "identity",
                "channel.set_identity",
                lambda response: response["after"].update(name="Wrong"),
                lambda controller: controller.set_channel_identity(
                    channel_index=1, name="Right"
                ),
            ),
            (
                "route",
                "channel.route_to_mixer",
                lambda response: response["after"].update(mixer_destination=2),
                lambda controller: controller.route_channel_to_mixer(
                    channel_index=1, mixer_destination=5
                ),
            ),
        )
        for label, expected_command, corrupt, invoke in cases:
            def contradictory(
                command: str,
                arguments: dict[str, Any],
                expected_command=expected_command,
                corrupt=corrupt,
            ) -> dict[str, Any]:
                self.assertEqual(command, expected_command)
                response = channel_handler(command, arguments)
                corrupt(response)
                return response

            controller, _client = controller_for(contradictory)
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    (ValueError, ValidationError), "verified|readback|requested|after"
                ):
                    invoke(controller)


class SequencerTests(unittest.TestCase):
    def test_digest_is_deterministic_and_binds_pattern_channel_and_cells(self) -> None:
        base = dict(pattern_number=2, channel_index=3, step_count=4)
        digest = compute_step_sequence_digest(**base, cells=[True, False, False, True])
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(
            digest,
            compute_step_sequence_digest(**base, cells=[True, False, False, True]),
        )
        self.assertNotEqual(
            digest,
            compute_step_sequence_digest(**dict(base, pattern_number=3), cells=[True, False, False, True]),
        )
        self.assertNotEqual(
            digest,
            compute_step_sequence_digest(**base, cells=[False, False, False, True]),
        )

    def test_read_parses_absolute_grid_and_verifies_digest(self) -> None:
        expected = step_snapshot()

        def handler(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            self.assertEqual(command, "sequencer.get")
            self.assertEqual(
                arguments, {"pattern": 2, "channel": 3, "index_scope": "global"}
            )
            return dict(expected, command="sequencer.get", unsaved_changes=0)

        inspector, _client = inspector_for(handler)
        result = inspector.get_step_sequence(pattern_number=2, channel_index=3)
        self.assertEqual(result.cells, [True, False, False, True])
        self.assertEqual(result.digest, expected["digest"])
        self.assertEqual(result.current_pattern_number, 2)
        self.assertFalse(result.observation_atomic)

    def test_read_refuses_digest_current_pattern_and_cell_shape_errors(self) -> None:
        mutations = (
            lambda raw: raw.update(digest="0" * 64),
            lambda raw: raw.update(current_pattern=1),
            lambda raw: raw.update(cells=[True, 0, False, True]),
            lambda raw: raw.update(step_count=5),
            lambda raw: raw.update(grid_resolution="quarter_note"),
            lambda raw: raw.update(index_scope="grouped"),
            lambda raw: raw.pop("index_scope"),
        )
        for mutate in mutations:
            def handler(command: str, arguments: dict[str, Any], mutate=mutate) -> dict[str, Any]:
                raw = dict(step_snapshot(), command="sequencer.get")
                mutate(raw)
                return raw

            inspector, _client = inspector_for(handler)
            with self.subTest(mutate=mutate):
                with self.assertRaises(ValueError):
                    inspector.get_step_sequence(pattern_number=2, channel_index=3)

    def test_set_parses_each_cell_proof_and_absolute_after_grid(self) -> None:
        before = step_snapshot()
        controller, client = controller_for(step_write_handler)
        result = controller.set_step_sequence(
            pattern_number=2,
            channel_index=3,
            expected_digest=before["digest"],
            updates=[
                StepCellUpdate(step_index=0, enabled=False),
                {"step_index": 2, "enabled": True},
            ],
            session_fingerprint=SESSION,
        )
        self.assertIsInstance(result, VerifiedStepSequenceWrite)
        self.assertEqual(result.after.cells, [False, False, True, True])
        self.assertTrue(all(proof.verified for proof in result.cells_verified))
        self.assertTrue(result.verified)
        command, arguments = client.calls[-1]
        self.assertEqual(command, "sequencer.set")
        self.assertEqual(arguments["expected_digest"], before["digest"])
        self.assertNotIn("expected_before", arguments)

    def test_set_refuses_duplicate_updates_or_bad_digest_before_dispatch(self) -> None:
        before = step_snapshot()
        cases = (
            dict(expected_digest="short", updates=[{"step_index": 0, "enabled": True}]),
            dict(expected_digest=before["digest"], updates=[]),
            dict(
                expected_digest=before["digest"],
                updates=[
                    {"step_index": 0, "enabled": True},
                    {"step_index": 0, "enabled": False},
                ],
            ),
            dict(
                expected_digest=before["digest"],
                updates=[{"step_index": 0, "enabled": 1}],
            ),
            dict(
                expected_digest=before["digest"],
                updates=[
                    {"step_index": index, "enabled": True}
                    for index in range(257)
                ],
            ),
        )
        for values in cases:
            controller, client = controller_for(step_write_handler)
            with self.subTest(values=values):
                with self.assertRaises((ValueError, ValidationError)):
                    controller.set_step_sequence(
                        pattern_number=2, channel_index=3, **values
                    )
            self.assertEqual(client.calls, [])

    def test_set_refuses_reordered_or_aggregate_cell_proof(self) -> None:
        before = step_snapshot()

        def reordered(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            response = step_write_handler(command, arguments)
            response["verified_cells"].reverse()
            return response

        controller, _client = controller_for(reordered)
        with self.assertRaisesRegex(ValueError, "out of order"):
            controller.set_step_sequence(
                pattern_number=2,
                channel_index=3,
                expected_digest=before["digest"],
                updates=[
                    {"step_index": 0, "enabled": False},
                    {"step_index": 2, "enabled": True},
                ],
            )

        def aggregate(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            response = step_write_handler(command, arguments)
            response["verified_cells"][0]["verified"] = False
            response["verified"] = True
            return response

        controller, _client = controller_for(aggregate)
        with self.assertRaisesRegex(ValidationError, "AND"):
            controller.set_step_sequence(
                pattern_number=2,
                channel_index=3,
                expected_digest=before["digest"],
                updates=[{"step_index": 0, "enabled": False}],
            )

    def test_set_rejects_cell_proof_that_contradicts_after_grid(self) -> None:
        before = step_snapshot()

        def contradictory(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            response = step_write_handler(command, arguments)
            response["verified_cells"][0]["after_enabled"] = True
            return response

        controller, _client = controller_for(contradictory)
        with self.assertRaisesRegex(
            (ValueError, ValidationError), "cell|after|requested|proof"
        ):
            controller.set_step_sequence(
                pattern_number=2,
                channel_index=3,
                expected_digest=before["digest"],
                updates=[{"step_index": 0, "enabled": False}],
            )

    def test_set_requires_global_scope_on_envelope_and_both_snapshots(self) -> None:
        before = step_snapshot()
        mutations = (
            lambda raw: raw.pop("index_scope"),
            lambda raw: raw.update(index_scope="grouped"),
            lambda raw: raw["before"].pop("index_scope"),
            lambda raw: raw["after"].update(index_scope="grouped"),
        )
        for mutate in mutations:
            def bad_scope(
                command: str,
                arguments: dict[str, Any],
                mutate=mutate,
            ) -> dict[str, Any]:
                response = step_write_handler(command, arguments)
                mutate(response)
                return response

            controller, _client = controller_for(bad_scope)
            with self.subTest(mutate=mutate):
                with self.assertRaisesRegex(ValueError, "global channel"):
                    controller.set_step_sequence(
                        pattern_number=2,
                        channel_index=3,
                        expected_digest=before["digest"],
                        updates=[{"step_index": 0, "enabled": False}],
                    )

    def test_snapshot_and_write_contracts_independently_enforce_digest_proof(self) -> None:
        raw = step_snapshot()
        with self.assertRaisesRegex(ValidationError, "digest"):
            StepSequenceSnapshot(
                pattern_number=2,
                current_pattern_number=2,
                channel_index=3,
                step_count=4,
                cells=[True, False, False, True],
                digest="0" * 64,
            )
        before = StepSequenceSnapshot(
            pattern_number=2,
            current_pattern_number=2,
            channel_index=3,
            step_count=4,
            cells=raw["cells"],
            digest=raw["digest"],
        )
        after_raw = step_snapshot(cells=[False, False, False, True])
        after = StepSequenceSnapshot(
            pattern_number=2,
            current_pattern_number=2,
            channel_index=3,
            step_count=4,
            cells=after_raw["cells"],
            digest=after_raw["digest"],
        )
        with self.assertRaisesRegex(ValidationError, "AND"):
            VerifiedStepSequenceWrite(
                **verified_contract_fields(verified=True),
                pattern_number=2,
                channel_index=3,
                expected_digest=before.digest,
                requested_updates=[StepCellUpdate(step_index=0, enabled=False)],
                before=before,
                after=after,
                cells_verified=[
                    StepCellVerification(
                        step_index=0,
                        requested_enabled=False,
                        after_enabled=False,
                        verified=False,
                    )
                ],
            )


class LiveNoteTests(unittest.TestCase):
    @staticmethod
    def note_handler(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = mutation_envelope(command, arguments)
        response.update(
            channel=arguments["channel"],
            index_scope="global",
            note=arguments["note"],
            velocity=arguments["velocity"],
            duration_ms=arguments["duration_ms"],
            midi_channel=arguments["midi_channel"],
            dispatched=True,
            note_off_sent=True,
        )
        return response

    def test_note_receipt_is_explicitly_dispatch_only(self) -> None:
        controller, client = controller_for(self.note_handler)
        result = controller.trigger_note(
            channel_index=3,
            note=60,
            velocity=100,
            duration_ms=300,
            midi_channel=-1,
            expected_before=ExpectedChannelTargetState(
                channel_fingerprint=CHANNEL_FINGERPRINT
            ),
        )
        self.assertIsInstance(result, LiveNoteDispatch)
        self.assertTrue(result.dispatched)
        self.assertTrue(result.note_off_sent)
        self.assertEqual(result.verification_basis, "dispatch_only_no_state_readback")
        self.assertFalse(result.project_saved)
        self.assertIsNone(result.undo_point_created)
        self.assertNotIn("verified", type(result).model_fields)
        self.assertEqual(client.calls[-1][1]["index_scope"], "global")

    def test_note_off_failure_is_reported_without_a_verification_claim(self) -> None:
        def missing_off(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            response = self.note_handler(command, arguments)
            response["note_off_sent"] = False
            return response

        controller, _client = controller_for(missing_off)
        result = controller.trigger_note(channel_index=0, note=0, velocity=1)
        self.assertFalse(result.note_off_sent)
        self.assertTrue(any("did not confirm note-off" in item for item in result.warnings))

    def test_note_receipt_requires_global_scope_and_causal_dispatch(self) -> None:
        mutations = (
            lambda raw: raw.pop("index_scope"),
            lambda raw: raw.update(index_scope="grouped"),
            lambda raw: raw.update(dispatched=False, note_off_sent=True),
        )
        for mutate in mutations:
            def contradictory(
                command: str,
                arguments: dict[str, Any],
                mutate=mutate,
            ) -> dict[str, Any]:
                response = self.note_handler(command, arguments)
                mutate(response)
                return response

            controller, _client = controller_for(contradictory)
            with self.subTest(mutate=mutate):
                with self.assertRaises(ValueError):
                    controller.trigger_note(channel_index=0, note=60, velocity=100)

    def test_note_inputs_and_malformed_receipts_are_rejected(self) -> None:
        cases = (
            dict(channel_index=0, note=128, velocity=100),
            dict(channel_index=0, note=60, velocity=0),
            dict(channel_index=0, note=60, velocity=100, duration_ms=19),
            dict(channel_index=0, note=60, velocity=100, midi_channel=16),
        )
        for values in cases:
            controller, client = controller_for(self.note_handler)
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    controller.trigger_note(**values)
            self.assertEqual(client.calls, [])

        def malformed(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            response = self.note_handler(command, arguments)
            response["note_off_sent"] = 1
            return response

        controller, _client = controller_for(malformed)
        with self.assertRaisesRegex(ValueError, "boolean"):
            controller.trigger_note(channel_index=0, note=60, velocity=100)


class PluginTargetNormalizationTests(unittest.TestCase):
    def test_legacy_effect_pair_normalizes_without_weakening_slot_bounds(self) -> None:
        result = normalize_plugin_target(track_index=4, slot_index=2)
        self.assertEqual(result.kind, "mixer_effect")
        self.assertEqual(result.bridge_index, 4)
        self.assertEqual(result.slot_index, 2)
        self.assertFalse(result.use_global_index)
        with self.assertRaises(ValueError):
            normalize_plugin_target(track_index=4, slot_index=-1)

    def test_generator_target_normalizes_to_global_channel_and_slot_minus_one(self) -> None:
        result = normalize_plugin_target(
            target={"kind": "channel_generator", "channel_index": 7}
        )
        self.assertEqual(result.kind, "channel_generator")
        self.assertEqual(result.bridge_index, 7)
        self.assertEqual(result.channel_index, 7)
        self.assertEqual(result.slot_index, -1)
        self.assertTrue(result.use_global_index)
        self.assertFalse(result.allow_master)

    def test_explicit_mixer_target_carries_its_own_master_acknowledgement(self) -> None:
        result = normalize_plugin_target(
            target={
                "kind": "mixer_effect",
                "track_index": 0,
                "slot_index": 1,
                "allow_master": True,
            }
        )
        self.assertEqual(result.track_index, 0)
        self.assertTrue(result.allow_master)
        with self.assertRaisesRegex((ValueError, ValidationError), "Master"):
            normalize_plugin_target(
                target={
                    "kind": "mixer_effect",
                    "track_index": 0,
                    "slot_index": 1,
                }
            )

    def test_target_and_legacy_arguments_are_an_exclusive_or(self) -> None:
        target = ChannelGeneratorTarget(channel_index=3)
        bad_calls = (
            lambda: normalize_plugin_target(),
            lambda: normalize_plugin_target(track_index=2),
            lambda: normalize_plugin_target(slot_index=2),
            lambda: normalize_plugin_target(
                target=target, track_index=2, slot_index=2
            ),
            lambda: normalize_plugin_target(target=target, allow_master=True),
        )
        for invoke in bad_calls:
            with self.subTest(invoke=invoke):
                with self.assertRaises(ValueError):
                    invoke()

    def test_target_validation_is_strict_and_forbids_unknown_fields(self) -> None:
        cases = (
            {"kind": "channel_generator", "channel_index": True},
            {"kind": "channel_generator", "channel_index": 1, "slot_index": -1},
            {"kind": "mixer_effect", "track_index": 1, "slot_index": 1.0},
            {"kind": "unknown", "channel_index": 1},
        )
        for target in cases:
            with self.subTest(target=target):
                with self.assertRaises((ValueError, ValidationError)):
                    normalize_plugin_target(target=target)


def project_info() -> dict[str, Any]:
    return {
        "unsaved_changes": 0,
        "undo_history_position": 1,
        "undo_history_count": 1,
    }


def generator_target_echo(channel: int) -> dict[str, Any]:
    return {
        "target_kind": "channel_generator",
        "channel": channel,
        "slot": -1,
        "use_global_index": True,
        "index_scope": "global",
    }


def effect_target_echo(track: int, slot: int) -> dict[str, Any]:
    return {
        "target_kind": "mixer_effect",
        "track": track,
        "slot": slot,
        "use_global_index": False,
    }


class TargetAwarePluginTests(unittest.TestCase):
    def test_existing_six_plugin_tools_are_the_only_target_aware_names(self) -> None:
        self.assertEqual(
            TARGET_AWARE_EXISTING_PLUGIN_TOOLS,
            {
                "plugins_scan_loaded_plugins",
                "plugins_inspect_parameter_map",
                "plugins_scan_parameters",
                "fl_set_plugin_param",
                "fl_set_plugin_param_display",
                "fl_set_plugin_param_option",
            },
        )
        self.assertEqual(len(TRACK_B_MCP_TOOL_NAMES), 31)
        self.assertTrue(TRACK_B_MCP_TOOL_NAMES.isdisjoint(TARGET_AWARE_EXISTING_PLUGIN_TOOLS))

    def test_inventory_combines_effects_and_global_generators(self) -> None:
        def handler(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if command == "project.info":
                return project_info()
            if command == "mixer.list":
                return {
                    "tracks": [
                        {
                            "index": 0,
                            "plugins": [
                                {
                                    "slot": 2,
                                    "name": "Master FX",
                                    "param_count": 8,
                                    "mix_level": 0.6,
                                }
                            ],
                        }
                    ]
                }
            if command == "channels.list":
                return {
                    "channels": [
                        {
                            "index": 7,
                            "index_scope": "global",
                            "plugin": "MiniSynth",
                            "reported_parameter_count": 16,
                        }
                    ]
                }
            raise AssertionError(command)

        inspector, client = inspector_for(handler)
        result = inspector.scan_loaded_plugins()
        self.assertIsInstance(result, TargetedLoadedPluginInventory)
        self.assertEqual(len(result.plugins), 2)
        self.assertIsInstance(result.plugins[0].target, MixerEffectTarget)
        self.assertTrue(result.plugins[0].target.allow_master)
        self.assertEqual(result.plugins[0].mix_level_normalized, 0.6)
        self.assertIsInstance(result.plugins[1].target, ChannelGeneratorTarget)
        self.assertEqual(result.plugins[1].target.channel_index, 7)
        self.assertIsNone(result.plugins[1].mix_level_normalized)
        self.assertEqual(
            [command for command, _ in client.calls],
            ["project.info", "mixer.list", "channels.list", "project.info"],
        )

    def test_inventory_requires_global_scope_even_for_non_plugin_rows(self) -> None:
        def handler(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if command == "project.info":
                return project_info()
            if command == "mixer.list":
                return {"tracks": []}
            if command == "channels.list":
                return {"channels": [{"index": 0, "plugin": None}]}
            raise AssertionError(command)

        inspector, _client = inspector_for(handler)
        with self.assertRaisesRegex(ValueError, "explicitly.*global"):
            inspector.scan_loaded_plugins()

    def test_generator_parameter_page_uses_slot_minus_one_and_global_scope(self) -> None:
        def handler(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if command == "project.info":
                return project_info()
            if command == "plugin.params":
                self.assertEqual(arguments["channel"], 7)
                self.assertEqual(arguments["slot"], -1)
                self.assertTrue(arguments["use_global_index"])
                self.assertEqual(arguments["index_scope"], "global")
                return {
                    **generator_target_echo(7),
                    "command": "plugin.params",
                    "plugin": "MiniSynth",
                    "param_count": 2,
                    "params": [
                        {"index": 0, "name": "Cutoff", "value": 0.5, "display": "50 %"},
                        {"index": 1, "name": "", "value": 0.0, "display": "0"},
                    ],
                }
            raise AssertionError(command)

        inspector, _client = inspector_for(handler)
        result = inspector.plugin_parameters(
            target=ChannelGeneratorTarget(channel_index=7), limit=2
        )
        self.assertIsInstance(result, TargetedPluginParameterPage)
        self.assertIsInstance(result.plugin.target, ChannelGeneratorTarget)
        self.assertEqual(result.returned_count, 2)
        self.assertEqual(result.parameters[1].classification, "padding_candidate")
        self.assertTrue(all(not value.safe_to_modify for value in result.parameters))

    def test_generator_parameter_scan_preserves_bounded_scan_provenance(self) -> None:
        def handler(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if command == "project.info":
                return project_info()
            if command == "plugin.scan_params":
                return {
                    **generator_target_echo(7),
                    "command": "plugin.scan_params",
                    "plugin": "MiniSynth",
                    "reported_count": 10,
                    "scan_start": 2,
                    "scan_end": 6,
                    "examined": 4,
                    "highest_index_examined": 5,
                    "padding_skipped": 3,
                    "truncated": True,
                    "truncated_by": "max_indices",
                    "params": [
                        {"index": 3, "name": "Drive", "value": 0.25, "display": "25 %"}
                    ],
                }
            raise AssertionError(command)

        inspector, _client = inspector_for(handler)
        result = inspector.scan_plugin_parameters(
            target={"kind": "channel_generator", "channel_index": 7},
            start=2,
            max_indices=4,
        )
        self.assertIsInstance(result, TargetedPluginParameterScan)
        self.assertTrue(result.truncated)
        self.assertEqual(result.truncated_by, "max_indices")
        self.assertEqual((result.examined_count, result.real_count), (4, 1))

    def test_generator_parameter_write_echoes_unambiguous_target(self) -> None:
        def handler(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            response = mutation_envelope(command, arguments)
            response.update(
                **generator_target_echo(7),
                index=3,
                plugin="MiniSynth",
                name="Drive",
                before={"value": 0.2, "display": "20 %"},
                after={"value": 0.8, "display": "80 %"},
                display_changed=True,
                reads_at_value=True,
                verification_basis="value_readback",
                verified=True,
            )
            return response

        controller, client = controller_for(handler)
        result = controller.set_plugin_parameter(
            target={"kind": "channel_generator", "channel_index": 7},
            parameter_index=3,
            normalized_value=0.8,
            session_fingerprint=SESSION,
        )
        self.assertIsInstance(result, VerifiedTargetedPluginParameterWrite)
        self.assertIsInstance(result.target, ChannelGeneratorTarget)
        self.assertTrue(result.reads_at_requested_value)
        self.assertEqual(result.verification_basis_detail, "value_readback")
        arguments = client.calls[-1][1]
        self.assertEqual(arguments["slot"], -1)
        self.assertEqual(arguments["channel"], 7)
        self.assertTrue(arguments["use_global_index"])

    def test_legacy_effect_display_and_option_writes_remain_compatible(self) -> None:
        def handler(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            response = mutation_envelope(command, arguments)
            common = {
                **effect_target_echo(4, 2),
                "index": 1,
                "plugin": "Compressor",
                "name": "Mode",
                "matched_on": "index",
                "matched_text": "Mode",
                "normalised": 0.5,
                "before": {"value": 0.0, "display": "Off"},
                "after": {"value": 0.5, "display": "On"},
                "verified": True,
            }
            if command == "plugin.set_param_display":
                common.update(
                    landed_on=6.0,
                    tolerance=0.1,
                    after={"value": 0.5, "display": "6.0 dB"},
                )
            elif command == "plugin.set_param_option":
                common.update(selected="On", steps=8, options=["Off", "On"])
            else:  # pragma: no cover
                raise AssertionError(command)
            response.update(common)
            return response

        controller, _client = controller_for(handler)
        display = controller.set_plugin_parameter_display(
            track_index=4,
            slot_index=2,
            parameter=1,
            target_value=6.0,
            tolerance=0.1,
        )
        option = controller.set_plugin_parameter_option(
            track_index=4,
            slot_index=2,
            parameter=1,
            option="On",
            sweep_steps=8,
        )
        self.assertIsInstance(display, VerifiedTargetedPluginDisplayWrite)
        self.assertIsInstance(option, VerifiedTargetedPluginOptionWrite)
        self.assertTrue(display.verified)
        self.assertEqual(option.selected_option, "On")
        self.assertTrue(all(isinstance(value.target, MixerEffectTarget) for value in (display, option)))

    def test_normalized_write_recomputes_proof_for_generators_and_effects(self) -> None:
        addresses = (
            {"target": ChannelGeneratorTarget(channel_index=7)},
            {"track_index": 4, "slot_index": 2},
        )
        corruptions = (
            lambda raw: raw.update(
                after={"value": 0.2, "display": "80 %"},
                reads_at_value=True,
            ),
            lambda raw: raw.update(
                after={"value": 0.2, "display": "20 %"},
                display_changed=True,
                reads_at_value=False,
                verification_basis="display_change_only",
            ),
            lambda raw: raw.update(verified=False),
        )
        for address in addresses:
            for corrupt in corruptions:
                def contradictory(
                    command: str,
                    arguments: dict[str, Any],
                    corrupt=corrupt,
                ) -> dict[str, Any]:
                    response = mutation_envelope(command, arguments)
                    target_echo = (
                        generator_target_echo(arguments["channel"])
                        if arguments["target_kind"] == "channel_generator"
                        else effect_target_echo(arguments["track"], arguments["slot"])
                    )
                    response.update(
                        **target_echo,
                        index=3,
                        plugin="Synth",
                        name="Drive",
                        before={"value": 0.2, "display": "20 %"},
                        after={"value": 0.8, "display": "80 %"},
                        display_changed=True,
                        reads_at_value=True,
                        verification_basis="value_readback",
                        verified=True,
                    )
                    corrupt(response)
                    return response

                controller, _client = controller_for(contradictory)
                with self.subTest(address=address, corrupt=corrupt):
                    with self.assertRaisesRegex(ValueError, "plug-in"):
                        controller.set_plugin_parameter(
                            **address,
                            parameter_index=3,
                            normalized_value=0.8,
                        )

    def test_display_write_recomputes_landed_and_later_tick_proof(self) -> None:
        corruptions = (
            lambda raw: raw.update(landed_on=7.0),
            lambda raw: raw.update(after={"value": 0.5, "display": "7.0 dB"}),
            lambda raw: raw.update(tolerance=0.5),
            lambda raw: raw.update(verified=False),
        )
        for corrupt in corruptions:
            def contradictory(
                command: str,
                arguments: dict[str, Any],
                corrupt=corrupt,
            ) -> dict[str, Any]:
                response = mutation_envelope(command, arguments)
                response.update(
                    **generator_target_echo(7),
                    index=1,
                    plugin="MiniSynth",
                    name="Drive",
                    matched_on="index",
                    matched_text="Drive",
                    normalised=0.5,
                    before={"value": 0.0, "display": "0.0 dB"},
                    after={"value": 0.5, "display": "6.0 dB"},
                    landed_on=6.0,
                    tolerance=0.1,
                    verified=True,
                )
                corrupt(response)
                return response

            controller, _client = controller_for(contradictory)
            with self.subTest(corrupt=corrupt):
                with self.assertRaisesRegex(ValueError, "plug-in"):
                    controller.set_plugin_parameter_display(
                        target=ChannelGeneratorTarget(channel_index=7),
                        parameter=1,
                        target_value=6.0,
                        tolerance=0.1,
                    )

    def test_option_write_requires_exact_selection_and_later_tick_proof(self) -> None:
        def reply(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            response = mutation_envelope(command, arguments)
            response.update(
                **generator_target_echo(7),
                index=1,
                plugin="MiniSynth",
                name="Mode",
                matched_on="index",
                matched_text="Mode",
                normalised=0.5,
                before={"value": 0.0, "display": "Off"},
                after={"value": 0.5, "display": "wide on"},
                selected="Wide On",
                steps=8,
                options=["Off", "Wide On"],
                verified=True,
            )
            return response

        controller, _client = controller_for(reply)
        valid = controller.set_plugin_parameter_option(
            target=ChannelGeneratorTarget(channel_index=7),
            parameter=1,
            option="wide on",
            sweep_steps=8,
        )
        self.assertTrue(valid.verified)
        self.assertEqual(valid.selected_option, "Wide On")

        with self.assertRaisesRegex(ValueError, "exactly match"):
            controller.set_plugin_parameter_option(
                target=ChannelGeneratorTarget(channel_index=7),
                parameter=1,
                option="On",
                sweep_steps=8,
            )

        corruptions = (
            lambda raw: raw.update(
                selected="Off", after={"value": 0.0, "display": "Off"}
            ),
            lambda raw: raw.update(options=["Off"]),
            lambda raw: raw.update(after={"value": 0.0, "display": "Off"}),
            lambda raw: raw.update(verified=False),
        )
        for corrupt in corruptions:
            def contradictory(
                command: str,
                arguments: dict[str, Any],
                corrupt=corrupt,
            ) -> dict[str, Any]:
                response = reply(command, arguments)
                corrupt(response)
                return response

            controller, _client = controller_for(contradictory)
            with self.subTest(corrupt=corrupt):
                with self.assertRaisesRegex(ValueError, "option"):
                    controller.set_plugin_parameter_option(
                        target=ChannelGeneratorTarget(channel_index=7),
                        parameter=1,
                        option="wide on",
                        sweep_steps=8,
                    )

    def test_generator_reply_must_echo_target_kind_slot_and_global_scope(self) -> None:
        mutations = (
            lambda raw: raw.update(target_kind="mixer_effect"),
            lambda raw: raw.update(slot=0),
            lambda raw: raw.update(use_global_index=False),
            lambda raw: raw.pop("use_global_index"),
        )
        for mutate in mutations:
            def handler(command: str, arguments: dict[str, Any], mutate=mutate) -> dict[str, Any]:
                response = mutation_envelope(command, arguments)
                response.update(
                    **generator_target_echo(7),
                    index=0,
                    plugin="MiniSynth",
                    name="Cutoff",
                    before={"value": 0.2, "display": "20 %"},
                    after={"value": 0.8, "display": "80 %"},
                    display_changed=True,
                    reads_at_value=True,
                    verified=True,
                )
                mutate(response)
                return response

            controller, _client = controller_for(handler)
            with self.subTest(mutate=mutate):
                with self.assertRaises(ValueError):
                    controller.set_plugin_parameter(
                        target={"kind": "channel_generator", "channel_index": 7},
                        parameter_index=0,
                        normalized_value=0.8,
                    )


class TargetAwareMCPBoundaryTests(unittest.TestCase):
    TARGETED_ADDRESS_TOOLS = {
        "plugins_inspect_parameter_map",
        "plugins_scan_parameters",
        "fl_set_plugin_param",
        "fl_set_plugin_param_display",
        "fl_set_plugin_param_option",
    }

    def test_schemas_keep_legacy_pair_and_add_optional_discriminated_target(self) -> None:
        tools = {
            tool.name: tool
            for tool in asyncio.run(mcp_server.mcp.list_tools())
        }
        for name in self.TARGETED_ADDRESS_TOOLS:
            with self.subTest(tool=name):
                schema = tools[name].input_schema
                properties = schema["properties"]
                required = set(schema.get("required", []))
                self.assertTrue(
                    {"track_index", "slot_index", "target"} <= set(properties)
                )
                self.assertTrue(
                    {"track_index", "slot_index", "target"}.isdisjoint(required)
                )
                self.assertIsNone(properties["track_index"]["default"])
                self.assertIsNone(properties["slot_index"]["default"])
                self.assertIsNone(properties["target"]["default"])

                target_union = next(
                    branch
                    for branch in properties["target"]["anyOf"]
                    if "discriminator" in branch
                )
                discriminator = target_union["discriminator"]
                self.assertEqual(discriminator["propertyName"], "kind")
                self.assertEqual(
                    discriminator["mapping"],
                    {
                        "mixer_effect": "#/$defs/MixerEffectTarget",
                        "channel_generator": "#/$defs/ChannelGeneratorTarget",
                    },
                )
                self.assertEqual(
                    {variant["$ref"] for variant in target_union["oneOf"]},
                    {
                        "#/$defs/MixerEffectTarget",
                        "#/$defs/ChannelGeneratorTarget",
                    },
                )
                self.assertEqual(
                    set(schema["$defs"]["MixerEffectTarget"]["required"]),
                    {"track_index", "slot_index"},
                )
                self.assertEqual(
                    set(schema["$defs"]["ChannelGeneratorTarget"]["required"]),
                    {"channel_index"},
                )
                self.assertEqual(
                    schema["$defs"]["MixerEffectTarget"]["properties"]["kind"][
                        "const"
                    ],
                    "mixer_effect",
                )
                self.assertEqual(
                    schema["$defs"]["ChannelGeneratorTarget"]["properties"][
                        "kind"
                    ]["const"],
                    "channel_generator",
                )

    def test_inventory_schema_preserves_legacy_response_by_default(self) -> None:
        tools = {
            tool.name: tool
            for tool in asyncio.run(mcp_server.mcp.list_tools())
        }
        schema = tools["plugins_scan_loaded_plugins"].input_schema
        properties = schema["properties"]
        self.assertEqual(
            set(properties), {"only_used", "include_channel_generators"}
        )
        self.assertFalse(properties["include_channel_generators"]["default"])
        self.assertNotIn(
            "include_channel_generators", set(schema.get("required", []))
        )
        self.assertIn(
            "False preserves the 0.11 mixer-effect-only response contract",
            properties["include_channel_generators"]["description"],
        )

    def test_generator_targets_route_through_performance_methods(self) -> None:
        target = ChannelGeneratorTarget(channel_index=7)
        read_results = [object(), object(), object()]
        write_results = [object(), object(), object()]
        with (
            mock.patch.object(
                mcp_server,
                "_performance_read",
                new=mock.AsyncMock(side_effect=read_results),
            ) as performance_read,
            mock.patch.object(
                mcp_server,
                "_performance_write",
                new=mock.AsyncMock(side_effect=write_results),
            ) as performance_write,
            mock.patch.object(
                mcp_server,
                "_run",
                new=mock.AsyncMock(side_effect=AssertionError("legacy read used")),
            ) as legacy_read,
            mock.patch.object(
                mcp_server,
                "_write",
                new=mock.AsyncMock(side_effect=AssertionError("legacy write used")),
            ) as legacy_write,
        ):
            actual_reads = [
                asyncio.run(
                    mcp_server.plugins_scan_loaded_plugins(
                        only_used=True, include_channel_generators=True
                    )
                ),
                asyncio.run(
                    mcp_server.plugins_inspect_parameter_map(
                        target=target,
                        limit=7,
                        offset=2,
                        name_filter="cut",
                    )
                ),
                asyncio.run(
                    mcp_server.plugins_scan_parameters(
                        target=target,
                        start=1,
                        end=9,
                        max_indices=8,
                        max_results=3,
                    )
                ),
            ]
            actual_writes = [
                asyncio.run(
                    mcp_server.fl_set_plugin_param(
                        parameter_index=4,
                        normalized_value=0.75,
                        target=target,
                        session_fingerprint=SESSION,
                    )
                ),
                asyncio.run(
                    mcp_server.fl_set_plugin_param_display(
                        parameter="Drive",
                        target_value=6.0,
                        target=target,
                        tolerance=0.1,
                        session_fingerprint=SESSION,
                    )
                ),
                asyncio.run(
                    mcp_server.fl_set_plugin_param_option(
                        parameter=2,
                        option="Wide",
                        target=target,
                        sweep_steps=16,
                        session_fingerprint=SESSION,
                    )
                ),
            ]

        self.assertEqual(actual_reads, read_results)
        self.assertEqual(actual_writes, write_results)
        self.assertEqual(
            performance_read.await_args_list,
            [
                mock.call("scan_loaded_plugins", only_used=True),
                mock.call(
                    "plugin_parameters",
                    target=target,
                    track_index=None,
                    slot_index=None,
                    limit=7,
                    offset=2,
                    name_filter="cut",
                ),
                mock.call(
                    "scan_plugin_parameters",
                    target=target,
                    track_index=None,
                    slot_index=None,
                    start=1,
                    end=9,
                    max_indices=8,
                    max_results=3,
                ),
            ],
        )
        self.assertEqual(
            performance_write.await_args_list,
            [
                mock.call(
                    "set_plugin_parameter",
                    target=target,
                    track_index=None,
                    slot_index=None,
                    parameter_index=4,
                    normalized_value=0.75,
                    allow_master=False,
                    session_fingerprint=SESSION,
                    expected_before=None,
                ),
                mock.call(
                    "set_plugin_parameter_display",
                    target=target,
                    track_index=None,
                    slot_index=None,
                    parameter="Drive",
                    target_value=6.0,
                    tolerance=0.1,
                    allow_master=False,
                    session_fingerprint=SESSION,
                    expected_before=None,
                ),
                mock.call(
                    "set_plugin_parameter_option",
                    target=target,
                    track_index=None,
                    slot_index=None,
                    parameter=2,
                    option="Wide",
                    sweep_steps=16,
                    allow_master=False,
                    session_fingerprint=SESSION,
                    expected_before=None,
                ),
            ],
        )
        legacy_read.assert_not_awaited()
        legacy_write.assert_not_awaited()

    def test_legacy_calls_keep_the_original_inspector_and_writer_paths(self) -> None:
        read_results = [object(), object(), object()]
        write_results = [object(), object(), object()]
        with (
            mock.patch.object(
                mcp_server,
                "_run",
                new=mock.AsyncMock(side_effect=read_results),
            ) as legacy_read,
            mock.patch.object(
                mcp_server,
                "_write",
                new=mock.AsyncMock(side_effect=write_results),
            ) as legacy_write,
            mock.patch.object(
                mcp_server,
                "_performance_read",
                new=mock.AsyncMock(
                    side_effect=AssertionError("performance read used")
                ),
            ) as performance_read,
            mock.patch.object(
                mcp_server,
                "_performance_write",
                new=mock.AsyncMock(
                    side_effect=AssertionError("performance write used")
                ),
            ) as performance_write,
        ):
            actual_reads = [
                asyncio.run(
                    mcp_server.plugins_scan_loaded_plugins(only_used=True)
                ),
                asyncio.run(
                    mcp_server.plugins_inspect_parameter_map(
                        track_index=2,
                        slot_index=3,
                        limit=7,
                        offset=2,
                        name_filter="cut",
                    )
                ),
                asyncio.run(
                    mcp_server.plugins_scan_parameters(
                        track_index=2,
                        slot_index=3,
                        start=1,
                        end=9,
                        max_indices=8,
                        max_results=3,
                    )
                ),
            ]
            actual_writes = [
                asyncio.run(
                    mcp_server.fl_set_plugin_param(
                        parameter_index=4,
                        normalized_value=0.75,
                        track_index=2,
                        slot_index=3,
                        session_fingerprint=SESSION,
                    )
                ),
                asyncio.run(
                    mcp_server.fl_set_plugin_param_display(
                        parameter="Drive",
                        target_value=6.0,
                        track_index=2,
                        slot_index=3,
                        tolerance=0.1,
                        session_fingerprint=SESSION,
                    )
                ),
                asyncio.run(
                    mcp_server.fl_set_plugin_param_option(
                        parameter=2,
                        option="Wide",
                        track_index=2,
                        slot_index=3,
                        sweep_steps=16,
                        session_fingerprint=SESSION,
                    )
                ),
            ]

        self.assertEqual(actual_reads, read_results)
        self.assertEqual(actual_writes, write_results)
        self.assertEqual(
            legacy_read.await_args_list,
            [
                mock.call("scan_loaded_plugins", only_used=True),
                mock.call(
                    "plugin_parameters",
                    track_index=2,
                    slot_index=3,
                    limit=7,
                    offset=2,
                    name_filter="cut",
                ),
                mock.call(
                    "scan_plugin_parameters",
                    track_index=2,
                    slot_index=3,
                    start=1,
                    end=9,
                    max_indices=8,
                    max_results=3,
                ),
            ],
        )
        self.assertEqual(
            legacy_write.await_args_list,
            [
                mock.call(
                    "set_plugin_parameter",
                    track_index=2,
                    slot_index=3,
                    parameter_index=4,
                    normalized_value=0.75,
                    allow_master=False,
                    session_fingerprint=SESSION,
                    expected_before=None,
                ),
                mock.call(
                    "set_plugin_parameter_display",
                    track_index=2,
                    slot_index=3,
                    parameter="Drive",
                    target_value=6.0,
                    tolerance=0.1,
                    allow_master=False,
                    session_fingerprint=SESSION,
                    expected_before=None,
                ),
                mock.call(
                    "set_plugin_parameter_option",
                    track_index=2,
                    slot_index=3,
                    parameter=2,
                    option="Wide",
                    sweep_steps=16,
                    allow_master=False,
                    session_fingerprint=SESSION,
                    expected_before=None,
                ),
            ],
        )
        performance_read.assert_not_awaited()
        performance_write.assert_not_awaited()

    def test_target_plus_legacy_pair_is_rejected_before_bridge_dispatch(self) -> None:
        def unreachable(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError(f"bridge dispatched {command}: {arguments}")

        inspector, read_client = inspector_for(unreachable)
        controller, write_client = controller_for(unreachable)
        target = ChannelGeneratorTarget(channel_index=7)
        cases = (
            (
                "inspect parameter map",
                mcp_server.plugins_inspect_parameter_map,
                {},
            ),
            ("scan parameters", mcp_server.plugins_scan_parameters, {}),
            (
                "set normalized parameter",
                mcp_server.fl_set_plugin_param,
                {"parameter_index": 4, "normalized_value": 0.75},
            ),
            (
                "set displayed parameter",
                mcp_server.fl_set_plugin_param_display,
                {"parameter": "Drive", "target_value": 6.0},
            ),
            (
                "set option parameter",
                mcp_server.fl_set_plugin_param_option,
                {"parameter": 2, "option": "Wide"},
            ),
        )
        with (
            mock.patch.object(
                mcp_server, "TrackBInspector", return_value=inspector
            ),
            mock.patch.object(
                mcp_server, "TrackBController", return_value=controller
            ),
        ):
            for label, function, arguments in cases:
                with self.subTest(tool=label):
                    with self.assertRaisesRegex(ValueError, "not both"):
                        asyncio.run(
                            function(
                                target=target,
                                track_index=2,
                                slot_index=3,
                                **arguments,
                            )
                        )

        self.assertEqual(read_client.ping_count, 0)
        self.assertEqual(write_client.ping_count, 0)
        self.assertEqual(read_client.calls, [])
        self.assertEqual(write_client.calls, [])

    def test_incomplete_legacy_pairs_are_rejected_before_any_route(self) -> None:
        cases = (
            (
                "inspect parameter map",
                mcp_server.plugins_inspect_parameter_map,
                {},
            ),
            ("scan parameters", mcp_server.plugins_scan_parameters, {}),
            (
                "set normalized parameter",
                mcp_server.fl_set_plugin_param,
                {"parameter_index": 4, "normalized_value": 0.75},
            ),
            (
                "set displayed parameter",
                mcp_server.fl_set_plugin_param_display,
                {"parameter": "Drive", "target_value": 6.0},
            ),
            (
                "set option parameter",
                mcp_server.fl_set_plugin_param_option,
                {"parameter": 2, "option": "Wide"},
            ),
        )
        incomplete_addresses = (
            {},
            {"track_index": 2},
            {"slot_index": 3},
        )
        with (
            mock.patch.object(
                mcp_server,
                "_run",
                new=mock.AsyncMock(side_effect=AssertionError("legacy read used")),
            ) as legacy_read,
            mock.patch.object(
                mcp_server,
                "_write",
                new=mock.AsyncMock(side_effect=AssertionError("legacy write used")),
            ) as legacy_write,
            mock.patch.object(
                mcp_server,
                "_performance_read",
                new=mock.AsyncMock(
                    side_effect=AssertionError("performance read used")
                ),
            ) as performance_read,
            mock.patch.object(
                mcp_server,
                "_performance_write",
                new=mock.AsyncMock(
                    side_effect=AssertionError("performance write used")
                ),
            ) as performance_write,
        ):
            for label, function, base_arguments in cases:
                for address in incomplete_addresses:
                    with self.subTest(tool=label, address=address):
                        with self.assertRaisesRegex(
                            ValueError, "target or both legacy"
                        ):
                            asyncio.run(function(**base_arguments, **address))

        legacy_read.assert_not_awaited()
        legacy_write.assert_not_awaited()
        performance_read.assert_not_awaited()
        performance_write.assert_not_awaited()


class ContractAndMalformedReplyTests(unittest.TestCase):
    def test_contracts_are_strict_frozen_and_forbid_extra_fields(self) -> None:
        value = ExpectedPlayingState(playing=True)
        with self.assertRaises(ValidationError):
            ExpectedPlayingState(playing=1)
        with self.assertRaises(ValidationError):
            ExpectedPlayingState(playing=True, surprise=True)
        with self.assertRaises(ValidationError):
            value.playing = False

    def test_empty_expected_before_contracts_are_rejected(self) -> None:
        for contract in (
            ExpectedStopState,
            ExpectedChannelMixState,
            ExpectedChannelIdentityState,
            ExpectedChannelRouteState,
        ):
            with self.subTest(contract=contract.__name__):
                with self.assertRaises(ValidationError):
                    contract()

    def test_wrong_command_missing_boolean_and_bad_snapshot_are_rejected(self) -> None:
        def wrong_command(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            response = transport_handler(command, arguments)
            response["command"] = "transport.stop"
            return response

        controller, _client = controller_for(wrong_command)
        with self.assertRaisesRegex(ValueError, "replied"):
            controller.set_playing(playing=True)

        def missing_verified(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            response = transport_handler(command, arguments)
            response.pop("verified")
            return response

        controller, _client = controller_for(missing_verified)
        with self.assertRaisesRegex(ValueError, "boolean"):
            controller.set_playing(playing=True)

        def bad_snapshot(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
            response = channel_handler(command, arguments)
            response["before"] = []
            return response

        controller, _client = controller_for(bad_snapshot)
        with self.assertRaisesRegex(ValueError, "snapshot"):
            controller.set_channel_mix(channel_index=1, muted=True)

    def test_nonfinite_and_boolean_numbers_are_rejected_before_dispatch(self) -> None:
        cases = (
            lambda c: c.set_tempo(tempo_bpm=True),
            lambda c: c.set_tempo(tempo_bpm=math.inf),
            lambda c: c.set_channel_mix(channel_index=0, pan=math.nan),
            lambda c: c.route_channel_to_mixer(
                channel_index=False, mixer_destination=1
            ),
        )
        for invoke in cases:
            controller, client = controller_for(transport_handler)
            with self.subTest(invoke=invoke):
                with self.assertRaises(ValueError):
                    invoke(controller)
            self.assertEqual(client.calls, [])

    def test_playback_speed_is_absent_with_an_explicit_backend_reason(self) -> None:
        self.assertNotIn("transport.set_playback_speed", TRACK_B_MUTATION_COMMANDS)
        self.assertNotIn("transport.setPlaybackSpeed", TRACK_B_MUTATION_COMMANDS)
        self.assertFalse(hasattr(TrackBController, "set_playback_speed"))
        self.assertNotIn("PlaybackSpeed", {value.__name__ for value in get_args(TrackBResult)})
        self.assertIn("no authoritative playback speed getter", PLAYBACK_SPEED_OMISSION_REASON)
        self.assertIn("later-idle-tick readback", PLAYBACK_SPEED_OMISSION_REASON)


if __name__ == "__main__":
    unittest.main(verbosity=2)
