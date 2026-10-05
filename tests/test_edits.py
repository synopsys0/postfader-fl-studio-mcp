"""Hermetic tests for the per-target setters: order, guards, and partial outcomes."""

from __future__ import annotations

import asyncio
import copy
import os
import sys
import types
import unittest
from typing import Any
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "fakefl"))
sys.path.insert(0, os.path.join(ROOT, "fl_studio_mcp", "_bridge"))
sys.path.insert(0, ROOT)

import _state  # noqa: E402
import device_UniversalBridge as bridge  # noqa: E402

from fl_studio_mcp import edits, mcp_server, production_runs  # noqa: E402
from fl_studio_mcp.bridge_client import BridgeError  # noqa: E402
from fl_studio_mcp.bridge_install import expected_bridge_deployment  # noqa: E402
from fl_studio_mcp.performance import TrackBInspector, TrackBReadGateway  # noqa: E402


SESSION = bridge.SESSION_FINGERPRINT
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
    "bridge_source_sha256": expected_bridge_deployment()[1],
    "session_fingerprint": SESSION,
}


class FakeBridgeClient:
    """The real bridge handlers over fake FL state, with a call log."""

    transport = "midi"

    def __init__(self, fail_on: str | None = None):
        self.ping_count = 0
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.fail_on = fail_on

    def ping(self) -> dict[str, Any]:
        self.ping_count += 1
        return dict(WRITE_ENABLED_PING)

    def call(self, cmd: str, **args: Any) -> Any:
        self.commands.append((cmd, copy.deepcopy(args)))
        if cmd == self.fail_on:
            raise BridgeError("injected link loss after dispatch")
        result = bridge.HANDLERS[cmd](args)
        if isinstance(result, types.GeneratorType):
            while True:
                try:
                    next(result)
                except StopIteration as stopped:
                    return stopped.value
        return result


def envelope(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "command": command,
        "session_fingerprint": SESSION,
        "session_precondition_applied": "session_fingerprint" in arguments,
        "expected_before_applied": "expected_before" in arguments,
        "undo_point_created": None,
        "warnings": [],
        "verified": True,
    }


def transport_reply(command: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Successful later-tick replies for every transport write."""

    reply = envelope(command, arguments)
    if command == "transport.stop":
        reply.update(
            before={"playing": True, "position": 0.4},
            after={"playing": False, "position": 0.0},
            verified_fields={"playing": True, "position": True},
        )
    elif command == "transport.set_playing":
        reply.update(before=not arguments["playing"], after=arguments["playing"])
    elif command == "transport.set_recording":
        reply.update(before=not arguments["recording"], after=arguments["recording"])
    elif command in {"transport.set_metronome", "transport.set_precount"}:
        reply.update(before=not arguments["enabled"], after=arguments["enabled"])
    elif command == "transport.set_tempo":
        reply.update(before=120.0, after=arguments["tempo_bpm"])
    elif command == "transport.set_loop_mode":
        reply.update(before="song", after=arguments["loop_mode"])
    elif command == "transport.set_song_position":
        reply.update(before=0.0, after=arguments["position"])
    elif command == "project.set_time_signature_numerator":
        numerator = arguments["numerator"]
        reply.update(
            before={"numerator": 4, "ppq": 96, "pulses_per_bar": 384, "denominator_available": False},
            after={
                "numerator": numerator,
                "ppq": 96,
                "pulses_per_bar": 96 * numerator,
                "denominator_available": False,
            },
        )
    else:  # pragma: no cover - a test setup error should be loud
        raise AssertionError(command)
    return reply


class ScriptedTransportClient(FakeBridgeClient):
    def call(self, cmd: str, **args: Any) -> Any:
        self.commands.append((cmd, copy.deepcopy(args)))
        return transport_reply(cmd, args)


def run_with(client: Any, function: Any, **arguments: Any) -> Any:
    with (
        mock.patch("fl_studio_mcp.workflows.get_client", return_value=client),
        mock.patch("fl_studio_mcp.performance.get_client", return_value=client),
    ):
        return function(**arguments)


class MixerTrackSetterTests(unittest.TestCase):
    def setUp(self) -> None:
        _state.reset()

    def test_every_field_applies_in_the_documented_order_with_one_handshake(self) -> None:
        client = FakeBridgeClient()
        result = run_with(
            client,
            edits.set_mixer_track,
            track_index=3,
            select=True,
            sends=(
                edits.MixerSendChange(
                    destination_track_index=5, enabled=True, level_normalized=0.5
                ),
            ),
            eq=(edits.MixerEqBandChange(band_index=1, gain_normalized=0.6),),
            armed=True,
            soloed=True,
            muted=True,
            stereo_separation=0.25,
            pan=-0.25,
            volume_db=-6.0,
            color=0x0055AA,
            name="Lead",
        )
        self.assertEqual(
            [command for command, _arguments in client.commands],
            [
                "mixer.set_name",
                "mixer.set_color",
                "mixer.set_volume_db",
                "mixer.set_pan",
                "mixer.set_stereo_separation",
                "mixer.set_mute",
                "mixer.set_solo",
                "mixer.set_arm",
                "mixer.set_eq",
                "mixer.set_send",
                "mixer.set_send_level",
                "mixer.select_track",
            ],
        )
        self.assertEqual(client.ping_count, 1)
        self.assertTrue(
            all(arguments["session_fingerprint"] == SESSION for _cmd, arguments in client.commands)
        )
        self.assertEqual(
            [item.step for item in result.results],
            [
                "name",
                "color",
                "volume",
                "pan",
                "stereo_separation",
                "muted",
                "soloed",
                "armed",
                "eq_band_1",
                "send_5",
                "send_5_level",
                "select",
            ],
        )
        self.assertTrue(result.verified and result.completed)
        self.assertEqual(result.requested_count, 12)
        self.assertEqual(result.skipped_steps, [])
        self.assertEqual(result.session_fingerprint, SESSION)
        track = _state.TRACKS[3]
        self.assertEqual(track.name, "Lead")
        self.assertTrue(track.muted)
        self.assertIn(5, track.routes)

    def test_guards_reach_only_the_writes_that_check_them(self) -> None:
        client = FakeBridgeClient()
        result = run_with(
            client,
            edits.set_mixer_track,
            track_index=3,
            name="Lead",
            pan=0.2,
            muted=True,
            expected_before=edits.ExpectedMixerTrackFields(pan=0.0, muted=False),
        )
        self.assertTrue(result.verified)
        sent = {command: arguments for command, arguments in client.commands}
        self.assertNotIn("expected_before", sent["mixer.set_name"])
        self.assertEqual(sent["mixer.set_pan"]["expected_before"], 0.0)
        self.assertEqual(sent["mixer.set_mute"]["expected_before"], False)

    def test_volume_db_guard_carries_both_forms_to_the_db_write(self) -> None:
        client = FakeBridgeClient()
        run_with(
            client,
            edits.set_mixer_track,
            track_index=3,
            volume_db=-6.0,
            expected_before=edits.ExpectedMixerTrackFields(volume_normalized=0.72),
        )
        self.assertEqual(
            client.commands[0][1]["expected_before"], {"volume_normalized": 0.72}
        )

    def test_invalid_calls_are_refused_before_any_contact(self) -> None:
        cases = (
            ({"pan": 0.1, "expected_before": edits.ExpectedMixerTrackFields(muted=False)}, "muted"),
            (
                {
                    "volume_normalized": 0.5,
                    "expected_before": edits.ExpectedMixerTrackFields(volume_db=-3.0),
                },
                "volume_db",
            ),
            ({"volume_normalized": 0.5, "volume_db": -6.0}, "not both"),
            ({"pan": 0.1, "tolerance_db": 0.2}, "tolerance_db"),
            ({}, "at least one field"),
            ({"track_index": 0, "pan": 0.1}, "allow_master"),
            (
                {"sends": (edits.MixerSendChange(destination_track_index=3, enabled=True),)},
                "itself",
            ),
            (
                {
                    "eq": (
                        edits.MixerEqBandChange(band_index=1, gain_normalized=0.4),
                        edits.MixerEqBandChange(band_index=1, frequency_normalized=0.4),
                    )
                },
                "more than once",
            ),
        )
        for arguments, message in cases:
            with self.subTest(message=message):
                client = FakeBridgeClient()
                call = {"track_index": 3, **arguments}
                with self.assertRaisesRegex(ValueError, message):
                    run_with(client, edits.set_mixer_track, **call)
                self.assertEqual(client.ping_count, 0)
                self.assertEqual(client.commands, [])

    def test_send_items_refuse_contradictions_and_guards_for_unset_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "removed"):
            edits.MixerSendChange(destination_track_index=5, enabled=False, level_normalized=0.4)
        with self.assertRaisesRegex(ValueError, "does not set a level"):
            edits.MixerSendChange(
                destination_track_index=5,
                enabled=True,
                expected_before=edits.ExpectedMixerSendState(level_normalized=0.8),
            )
        with self.assertRaisesRegex(ValueError, "needs enabled and/or level"):
            edits.MixerSendChange(destination_track_index=5)

    def test_an_unverified_write_stops_the_rest_unless_asked_to_continue(self) -> None:
        with mock.patch.object(bridge.mixer, "setTrackVolume", lambda *a, **k: None):
            client = FakeBridgeClient()
            stopped = run_with(
                client,
                edits.set_mixer_track,
                track_index=3,
                volume_normalized=0.65,
                pan=-0.4,
            )
            self.assertFalse(stopped.verified)
            self.assertFalse(stopped.completed)
            self.assertEqual(stopped.stopped_reason, "unverified_receipt")
            self.assertEqual([item.status for item in stopped.results], ["unverified"])
            self.assertEqual(stopped.skipped_steps, ["pan"])
            self.assertEqual(len(client.commands), 1)

            _state.reset()
            client = FakeBridgeClient()
            continued = run_with(
                client,
                edits.set_mixer_track,
                track_index=3,
                volume_normalized=0.65,
                pan=-0.4,
                stop_on_unverified=False,
            )
        self.assertFalse(continued.verified)
        self.assertTrue(continued.completed)
        self.assertEqual(
            [item.status for item in continued.results], ["unverified", "verified"]
        )

    def test_an_unknown_outcome_keeps_earlier_receipts_and_never_replays(self) -> None:
        client = FakeBridgeClient(fail_on="mixer.set_pan")
        result = run_with(
            client,
            edits.set_mixer_track,
            track_index=3,
            name="Lead",
            pan=-0.4,
            muted=True,
        )
        self.assertEqual(
            [(item.step, item.status) for item in result.results],
            [("name", "verified"), ("pan", "error_unknown")],
        )
        self.assertIn("injected link loss", result.results[1].error)
        self.assertEqual(result.stopped_reason, "unknown_outcome")
        self.assertEqual(result.skipped_steps, ["muted"])
        self.assertFalse(result.verified)
        self.assertFalse(result.automatic_replay_attempted)
        self.assertEqual(
            [command for command, _arguments in client.commands],
            ["mixer.set_name", "mixer.set_pan"],
        )
        self.assertEqual(_state.TRACKS[3].name, "Lead")
        self.assertFalse(_state.TRACKS[3].muted)


class ChannelPatternPlaylistSetterTests(unittest.TestCase):
    def setUp(self) -> None:
        _state.reset()

    def channel_fingerprint(self) -> str:
        inspector = TrackBInspector(TrackBReadGateway(FakeBridgeClient()))
        return inspector.list_channels().channels[0].channel_fingerprint

    def test_channel_fingerprint_guards_every_write_except_selection(self) -> None:
        fingerprint = self.channel_fingerprint()
        client = FakeBridgeClient()
        result = run_with(
            client,
            edits.set_channel,
            channel_index=0,
            name="Kick",
            volume_normalized=0.7,
            soloed=True,
            select=True,
            expected_before=edits.ExpectedChannelFields(channel_fingerprint=fingerprint),
        )
        self.assertTrue(result.verified)
        # Fingerprint-neutral writes run before the rename that changes it.
        self.assertEqual(
            [item.step for item in result.results], ["mix", "soloed", "identity", "select"]
        )
        sent = {command: arguments for command, arguments in client.commands}
        for command in ("channel.set_mix", "channel.set_solo", "channel.set_identity"):
            self.assertEqual(
                sent[command]["expected_before"]["channel_fingerprint"], fingerprint
            )
        self.assertNotIn("expected_before", sent["channel.select"])

    def test_the_fingerprint_stops_at_the_first_write_that_changes_it(self) -> None:
        fingerprint = self.channel_fingerprint()
        client = FakeBridgeClient()
        result = run_with(
            client,
            edits.set_channel,
            channel_index=0,
            name="Kick",
            mixer_destination=4,
            expected_before=edits.ExpectedChannelFields(channel_fingerprint=fingerprint),
        )
        self.assertTrue(result.verified)
        self.assertEqual([item.step for item in result.results], ["identity", "route"])
        sent = {command: arguments for command, arguments in client.commands}
        self.assertEqual(
            sent["channel.set_identity"]["expected_before"]["channel_fingerprint"],
            fingerprint,
        )
        # The rename changed the fingerprint; the session pin still holds.
        self.assertNotIn("expected_before", sent["channel.route_to_mixer"])
        self.assertEqual(sent["channel.route_to_mixer"]["session_fingerprint"], SESSION)

    def test_a_fingerprint_with_only_selection_is_refused_before_contact(self) -> None:
        client = FakeBridgeClient()
        with self.assertRaisesRegex(ValueError, "other than select"):
            run_with(
                client,
                edits.set_channel,
                channel_index=0,
                select=True,
                expected_before=edits.ExpectedChannelFields(channel_fingerprint="a" * 64),
            )
        self.assertEqual(client.ping_count, 0)

    def test_pattern_and_playlist_setters_verify_each_write(self) -> None:
        pattern = run_with(
            FakeBridgeClient(),
            edits.set_pattern,
            pattern_number=2,
            name="Chorus",
            length_beats=8,
            select=True,
        )
        self.assertTrue(pattern.verified)
        self.assertEqual(
            [item.step for item in pattern.results], ["identity", "length", "select"]
        )
        playlist = run_with(
            FakeBridgeClient(),
            edits.set_playlist_track,
            track_index=1,
            name="Vocals",
            muted=True,
        )
        self.assertTrue(playlist.verified)
        self.assertEqual([item.step for item in playlist.results], ["identity", "state"])


class TransportSetterTests(unittest.TestCase):
    def test_transport_releases_first_and_engages_last(self) -> None:
        client = ScriptedTransportClient()
        result = run_with(
            client,
            edits.set_transport,
            playing=True,
            recording=True,
            position_normalized=0.25,
            precount=True,
            metronome=True,
            loop_mode="pattern",
            time_signature_numerator=3,
            tempo_bpm=128.0,
        )
        self.assertTrue(result.verified)
        self.assertEqual(
            [command for command, _arguments in client.commands],
            [
                "transport.set_tempo",
                "project.set_time_signature_numerator",
                "transport.set_loop_mode",
                "transport.set_metronome",
                "transport.set_precount",
                "transport.set_song_position",
                "transport.set_recording",
                "transport.set_playing",
            ],
        )

        client = ScriptedTransportClient()
        result = run_with(
            client,
            edits.set_transport,
            tempo_bpm=100.0,
            recording=False,
            stop=True,
        )
        self.assertTrue(result.verified)
        self.assertEqual(
            [command for command, _arguments in client.commands],
            ["transport.stop", "transport.set_recording", "transport.set_tempo"],
        )

    def test_transport_guards_map_to_each_write(self) -> None:
        client = ScriptedTransportClient()
        run_with(
            client,
            edits.set_transport,
            tempo_bpm=128.0,
            metronome=True,
            expected_before=edits.ExpectedTransportFields(tempo_bpm=120.0, metronome=False),
        )
        sent = {command: arguments for command, arguments in client.commands}
        self.assertEqual(sent["transport.set_tempo"]["expected_before"], {"tempo_bpm": 120.0})
        self.assertEqual(sent["transport.set_metronome"]["expected_before"], {"enabled": False})

    def test_contradictory_transport_calls_are_refused_before_contact(self) -> None:
        cases = (
            ({"stop": True, "playing": True}, "stop already"),
            ({"stop": True, "position_normalized": 0.5}, "stop already"),
            ({"tempo_bpm": 120.0, "position_tolerance": 0.01}, "position_tolerance"),
            (
                {
                    "tempo_bpm": 120.0,
                    "expected_before": edits.ExpectedTransportFields(playing=False),
                },
                "playing",
            ),
        )
        for arguments, message in cases:
            with self.subTest(message=message):
                client = ScriptedTransportClient()
                with self.assertRaisesRegex(ValueError, message):
                    run_with(client, edits.set_transport, **arguments)
                self.assertEqual(client.ping_count, 0)


class ToolDispatchTests(unittest.TestCase):
    def test_project_step_history_dispatches_the_requested_direction(self) -> None:
        with mock.patch.object(
            mcp_server, "_performance_write", new_callable=mock.AsyncMock
        ) as write:
            for direction in ("undo", "redo"):
                asyncio.run(mcp_server.project_step_history(direction=direction))
                self.assertEqual(write.await_args.args, (direction,))

    def test_sound_plan_palette_routes_variations_by_base_palette_id(self) -> None:
        request = object()
        with (
            mock.patch.object(mcp_server, "plan_sound_selection", return_value="plan") as plan,
            mock.patch.object(
                mcp_server, "create_sound_selection_variation", return_value="variation"
            ) as variation,
        ):
            self.assertEqual(asyncio.run(mcp_server.sound_plan_palette(request)), "plan")
            self.assertEqual(
                asyncio.run(
                    mcp_server.sound_plan_palette(
                        request, base_palette_id="palette-1", section="chorus", replace_roles=("lead",)
                    )
                ),
                "variation",
            )
            with self.assertRaisesRegex(ValueError, "base_palette_id"):
                asyncio.run(mcp_server.sound_plan_palette(request, section="chorus"))
        plan.assert_called_once_with(request)
        variation.assert_called_once_with("palette-1", request, "chorus", ("lead",))

    def test_review_get_views_select_one_reader(self) -> None:
        with (
            mock.patch.object(mcp_server, "get_creation_review", return_value="session") as session,
            mock.patch.object(
                mcp_server, "build_review_export_handoff", return_value="export"
            ) as export,
            mock.patch.object(
                mcp_server, "build_review_delivery_manifest", return_value="delivery"
            ) as delivery,
        ):
            self.assertEqual(asyncio.run(mcp_server.review_get("review-1")), "session")
            self.assertEqual(
                asyncio.run(mcp_server.review_get("review-1", view="export_request")), "export"
            )
            self.assertEqual(
                asyncio.run(mcp_server.review_get("review-1", view="delivery_manifest")),
                "delivery",
            )
        for reader in (session, export, delivery):
            reader.assert_called_once_with("review-1")

    def test_run_validate_adds_readiness_only_on_request(self) -> None:
        validation = mock.Mock(name="validation")
        readiness = mock.Mock(name="readiness")
        with (
            mock.patch.object(
                production_runs, "validate_production_run", return_value=validation
            ) as validate,
            mock.patch.object(
                production_runs, "creation_readiness", return_value=readiness
            ) as ready,
            mock.patch.object(
                production_runs.ProductionRunCheck,
                "__init__",
                lambda self, **fields: self.__dict__.update(fields),
            ),
        ):
            plain = production_runs.check_production_run("request", "plan")
            self.assertIs(plain.validation, validation)
            self.assertIsNone(plain.readiness)
            ready.assert_not_called()
            full = production_runs.check_production_run(
                "request", "plan", include_readiness=True
            )
            self.assertIs(full.readiness, readiness)
        self.assertEqual(validate.call_count, 2)
        ready.assert_called_once_with("request", "plan")


if __name__ == "__main__":
    unittest.main(verbosity=2)
