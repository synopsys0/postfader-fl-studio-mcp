"""Deterministic tests for the v0.20 creative and arrangement pack."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE / "fakefl"))
sys.path.insert(0, str(ROOT / "fl_studio_mcp" / "_bridge"))
sys.path.insert(0, str(ROOT))

import _state  # noqa: E402
import device_UniversalBridge as bridge  # noqa: E402
from fl_studio_mcp.bridge_install import expected_bridge_deployment  # noqa: E402
from fl_studio_mcp.creative import (  # noqa: E402
    PIANO_ROLL,
    CreativeNote,
    HotkeyDispatch,
    MidiTrackSpec,
    PianoRollTargetReceipt,
    PianoRollTransform,
    SectionMarker,
    add_section_markers,
    compose_bassline,
    compose_chord_progression,
    compose_drums,
    compose_melody,
    export_type1_midi,
    piano_roll_scripts_directory,
    prepare_empty_pattern,
    record_automation_value,
    transform_piano_roll,
    write_piano_roll_notes,
)
from fl_studio_mcp.music_analysis import (  # noqa: E402
    analyze_tempo_and_key,
    transcribe_monophonic,
)


bridge.BRIDGE_SOURCE_SHA256 = expected_bridge_deployment()[1]


class DirectFakeClient:
    transport = "tcp"

    def ping(self):
        return bridge.cmd_ping({})

    def call(self, command: str, **arguments):
        result = bridge.HANDLERS[command](arguments)
        if isinstance(result, types.GeneratorType):
            while True:
                try:
                    next(result)
                except StopIteration as stopped:
                    return stopped.value
        return result


def target_receipt() -> PianoRollTargetReceipt:
    return PianoRollTargetReceipt(
        command="creative.prepare_piano_roll",
        channel_index=2,
        pattern_number=3,
        before_channel_indices=[0],
        after_channel_indices=[2],
        before_pattern_number=1,
        after_pattern_number=3,
        piano_roll_visible_before=False,
        piano_roll_visible_after=True,
        selected_target_verified=True,
        piano_roll_visibility_verified=True,
        session_fingerprint="a" * 32,
    )


class CreativeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._bridge_write_state = (
            bridge.LEAN_WRITES_ENABLED,
            bridge.WRITE_MODE_ORIGIN,
        )
        _state.reset()
        bridge.LEAN_WRITES_ENABLED = True
        bridge.WRITE_MODE_ORIGIN = "runtime_request"
        with PIANO_ROLL._lock:
            PIANO_ROLL._prepared = False
            PIANO_ROLL._armed = False
            PIANO_ROLL._last_request_id = None
            PIANO_ROLL._last_count = None
            PIANO_ROLL._last_digest = None
            PIANO_ROLL._last_operation = None

    def tearDown(self) -> None:
        bridge.LEAN_WRITES_ENABLED, bridge.WRITE_MODE_ORIGIN = (
            self._bridge_write_state
        )

    def test_composition_is_deterministic_and_scale_bounded(self) -> None:
        chords = compose_chord_progression(
            ["I", "vi", "IV", "V7"], root="C", collection="major"
        )
        self.assertEqual(chords.note_count, 13)
        self.assertEqual(chords.duration_beats, 16.0)
        self.assertTrue(all(note.pitch % 12 in {0, 2, 4, 5, 7, 9, 11} for note in chords.notes))

        first = compose_melody(
            root="D", collection="dorian", bars=8, seed=493, contour="arch"
        )
        second = compose_melody(
            root="D", collection="dorian", bars=8, seed=493, contour="arch"
        )
        different = compose_melody(
            root="D", collection="dorian", bars=8, seed=494, contour="arch"
        )
        self.assertEqual(first.note_digest_sha256, second.note_digest_sha256)
        self.assertNotEqual(first.note_digest_sha256, different.note_digest_sha256)
        self.assertTrue(first.notes)

        bass = compose_bassline(
            ["i", "VI", "III", "VII"],
            root="A",
            collection="natural_minor",
            style="walking",
            seed=11,
        )
        drums = compose_drums(style="dnb", bars=4, seed=11, swing=0.08)
        self.assertTrue(bass.notes)
        self.assertGreater(drums.note_count, 40)
        self.assertIn("General MIDI", drums.warnings[0])

    def test_type1_midi_export_is_reopened_and_event_verified(self) -> None:
        track = MidiTrackSpec(
            name="Lead",
            channel=2,
            notes=[
                CreativeNote(pitch=60, start_beats=0.0, duration_beats=1.0),
                CreativeNote(pitch=64, start_beats=1.0, duration_beats=0.5),
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "composition.mid")
            receipt = export_type1_midi(path, [track], tempo_bpm=123.0, ppq=960)
            self.assertTrue(receipt.verified)
            self.assertTrue(receipt.header_verified)
            self.assertTrue(receipt.note_events_verified)
            self.assertEqual(receipt.track_count, 2)
            self.assertEqual(receipt.note_count, 2)
            self.assertEqual(receipt.sha256, receipt.readback_sha256)
            with self.assertRaises(FileExistsError):
                export_type1_midi(path, [track])
            replaced = export_type1_midi(path, [track], overwrite=True)
            self.assertTrue(replaced.overwritten_existing_file)

    def test_type1_midi_create_only_export_wins_one_concurrent_race(self) -> None:
        tracks = [
            MidiTrackSpec(
                name="First",
                channel=1,
                notes=[CreativeNote(pitch=60, start_beats=0.0, duration_beats=1.0)],
            ),
            MidiTrackSpec(
                name="Second",
                channel=2,
                notes=[CreativeNote(pitch=67, start_beats=0.0, duration_beats=1.0)],
            ),
        ]
        barrier = threading.Barrier(2)
        from fl_studio_mcp import creative as creative_module

        real_midi_bytes = creative_module._midi_bytes

        def synchronized_midi_bytes(*args, **kwargs):
            barrier.wait(timeout=5)
            return real_midi_bytes(*args, **kwargs)

        def export(track: MidiTrackSpec, path: str):
            try:
                return "ok", export_type1_midi(
                    path,
                    [track],
                    tempo_bpm=111.0 if track.name == "First" else 137.0,
                    overwrite=False,
                )
            except Exception as exc:  # collect the race outcome in the caller
                return "error", exc

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "concurrent.mid")
            with mock.patch(
                "fl_studio_mcp.creative._midi_bytes",
                side_effect=synchronized_midi_bytes,
            ):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(export, track, path) for track in tracks]
                    outcomes = [future.result(timeout=10) for future in futures]

            successes = [value for kind, value in outcomes if kind == "ok"]
            failures = [value for kind, value in outcomes if kind == "error"]
            self.assertEqual(len(successes), 1, outcomes)
            self.assertEqual(len(failures), 1, outcomes)
            self.assertIsInstance(failures[0], FileExistsError)
            winner = successes[0]
            self.assertFalse(winner.overwritten_existing_file)
            self.assertEqual(
                hashlib.sha256(Path(path).read_bytes()).hexdigest(), winner.sha256
            )

    def test_type1_midi_create_only_reports_unsupported_filesystem(self) -> None:
        track = MidiTrackSpec(
            name="Lead",
            channel=1,
            notes=[CreativeNote(pitch=60, start_beats=0.0, duration_beats=1.0)],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unsupported.mid"
            with mock.patch(
                "fl_studio_mcp.creative.os.link",
                side_effect=OSError("hard links unavailable"),
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "filesystem must support same-directory hard links",
                ):
                    export_type1_midi(str(path), [track], overwrite=False)
            self.assertFalse(path.exists())

    def test_piano_roll_script_setup_and_manual_dispatch_are_honest(self) -> None:
        notes = [CreativeNote(pitch=60, start_beats=0, duration_beats=1)]
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {"POSTFADER_PIANO_ROLL_SCRIPTS_DIR": directory},
            clear=False,
        ):
            prepared = PIANO_ROLL.bridge_action("prepare")
            self.assertTrue(prepared.script_exists)
            self.assertFalse(prepared.armed_this_session)
            with self.assertRaises(ValueError):
                PIANO_ROLL.bridge_action("confirm")
            armed = PIANO_ROLL.bridge_action(
                "confirm", confirm_user_ran_script=True
            )
            self.assertTrue(armed.armed_this_session)

            manual = write_piano_roll_notes(
                notes,
                channel_index=2,
                pattern_number=3,
                mode="replace",
                auto_trigger=False,
            )
            source = Path(manual.script_path).read_text(encoding="ascii")
            self.assertEqual(manual.status, "prepared_for_manual_run")
            self.assertFalse(manual.application_verified)
            self.assertIn("score.clearNotes(True)", source)
            self.assertIn("score.addNote(note)", source)
            compile(source, "Postfader_Apply.pyscript", "exec")

            transform = transform_piano_roll(
                PianoRollTransform(
                    operation="humanize",
                    scope="selected",
                    timing_beats=0.03,
                    velocity_amount=0.08,
                    seed=91,
                ),
                channel_index=2,
                pattern_number=3,
                auto_trigger=False,
            )
            self.assertEqual(transform.status, "prepared_for_manual_run")
            transform_source = Path(transform.script_path).read_text("ascii")
            self.assertIn('operation == "humanize"', transform_source)
            compile(transform_source, "Postfader_Apply.pyscript", "exec")

    def test_piano_roll_directory_reuses_platform_aware_user_data_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            user_data = Path(directory) / "Redirected FL Data"
            explicit_scripts = Path(directory) / "Explicit scripts"
            with mock.patch.dict(
                os.environ,
                {
                    "FL_STUDIO_USER_DATA_DIR": os.fspath(user_data),
                    "POSTFADER_PIANO_ROLL_SCRIPTS_DIR": "",
                },
                clear=False,
            ):
                self.assertEqual(
                    piano_roll_scripts_directory(),
                    (
                        user_data
                        / "Settings"
                        / "Piano roll scripts"
                        / "Postfader"
                    ).resolve(),
                )
            with mock.patch.dict(
                os.environ,
                {
                    "FL_STUDIO_USER_DATA_DIR": os.fspath(user_data),
                    "POSTFADER_PIANO_ROLL_SCRIPTS_DIR": os.fspath(
                        explicit_scripts
                    ),
                },
                clear=False,
            ):
                self.assertEqual(
                    piano_roll_scripts_directory(), explicit_scripts.resolve()
                )
            with mock.patch.dict(
                os.environ,
                {"POSTFADER_PIANO_ROLL_SCRIPTS_DIR": "relative/scripts"},
                clear=False,
            ):
                with self.assertRaisesRegex(ValueError, "must be an absolute"):
                    piano_roll_scripts_directory()

    def test_piano_roll_auto_trigger_reports_dispatch_not_application(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {"POSTFADER_PIANO_ROLL_SCRIPTS_DIR": directory},
            clear=False,
        ):
            PIANO_ROLL.bridge_action("prepare")
            PIANO_ROLL.bridge_action("confirm", confirm_user_ran_script=True)
            with (
                mock.patch("fl_studio_mcp.creative._target_piano_roll", return_value=target_receipt()),
                mock.patch(
                    "fl_studio_mcp.creative._trigger_piano_roll_shortcut",
                    return_value=HotkeyDispatch(
                        platform="macos",
                        shortcut="Cmd+Opt+Y",
                        fl_window_found=True,
                        fl_window_focused=True,
                        hotkey_dispatched=True,
                    ),
                ),
            ):
                receipt = write_piano_roll_notes(
                    [CreativeNote(pitch=67, start_beats=0, duration_beats=2)],
                    channel_index=2,
                    pattern_number=3,
                    auto_trigger=True,
                )
        self.assertEqual(receipt.status, "hotkey_dispatched_unverified")
        self.assertTrue(receipt.target.selected_target_verified)
        self.assertTrue(receipt.trigger.hotkey_dispatched)
        self.assertFalse(receipt.application_verified)

    def _assert_piano_roll_live_sequence_is_serial(self, operation: str) -> None:
        events: list[tuple[str, str]] = []
        event_lock = threading.Lock()
        local = threading.local()
        active_steps = 0
        overlapped = False

        def step(name: str) -> None:
            nonlocal active_steps, overlapped
            owner = local.owner
            with event_lock:
                overlapped = overlapped or active_steps > 0
                active_steps += 1
                events.append((owner, name))
            try:
                # Give a competing request a deterministic opportunity to
                # enter each side-effect boundary when serialization is absent.
                time.sleep(0.01)
            finally:
                with event_lock:
                    active_steps -= 1

        def target(_channel_index: int, _pattern_number: int) -> PianoRollTargetReceipt:
            step("target")
            return target_receipt()

        def atomic(_path: Path, _content: str) -> str:
            step("script")
            return "b" * 64

        def record(**kwargs) -> None:
            step("record")
            original_record(**kwargs)

        def trigger() -> HotkeyDispatch:
            step("hotkey")
            return HotkeyDispatch(
                platform="macos",
                shortcut="Cmd+Opt+Y",
                fl_window_found=True,
                fl_window_focused=True,
                hotkey_dispatched=True,
            )

        original_record = PIANO_ROLL.record
        with PIANO_ROLL._lock:
            PIANO_ROLL._armed = True

        def invoke(owner: str, directory: str):
            local.owner = owner
            if operation == "notes":
                return write_piano_roll_notes(
                    [CreativeNote(pitch=60 if owner == "a" else 67, start_beats=0.0, duration_beats=1.0)],
                    channel_index=2,
                    pattern_number=3,
                    auto_trigger=True,
                )
            return transform_piano_roll(
                PianoRollTransform(
                    operation="humanize",
                    scope="selected",
                    timing_beats=0.03,
                    velocity_amount=0.08,
                    seed=91,
                ),
                channel_index=2,
                pattern_number=3,
                auto_trigger=True,
            )

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {"POSTFADER_PIANO_ROLL_SCRIPTS_DIR": directory},
            clear=False,
        ):
            with (
                mock.patch("fl_studio_mcp.creative._target_piano_roll", side_effect=target),
                mock.patch("fl_studio_mcp.creative._atomic_text", side_effect=atomic),
                mock.patch.object(PIANO_ROLL, "record", side_effect=record),
                mock.patch("fl_studio_mcp.creative._trigger_piano_roll_shortcut", side_effect=trigger),
            ):
                barrier = threading.Barrier(2)

                def run(owner: str):
                    local.owner = owner
                    barrier.wait(timeout=5)
                    return invoke(owner, directory)

                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(run, owner) for owner in ("a", "b")]
                    receipts = [future.result(timeout=10) for future in futures]

        self.assertEqual(len(receipts), 2)
        self.assertFalse(overlapped, events)
        self.assertEqual(active_steps, 0)
        by_owner = {
            owner: [name for event_owner, name in events if event_owner == owner]
            for owner in ("a", "b")
        }
        self.assertEqual(by_owner["a"], ["target", "script", "record", "hotkey"])
        self.assertEqual(by_owner["b"], ["target", "script", "record", "hotkey"])

    def test_piano_roll_note_target_script_record_hotkey_is_serial(self) -> None:
        self._assert_piano_roll_live_sequence_is_serial("notes")

    def test_piano_roll_transform_target_script_record_hotkey_is_serial(self) -> None:
        self._assert_piano_roll_live_sequence_is_serial("transform")

    def test_piano_roll_prepare_cannot_replace_an_in_flight_dispatch(self) -> None:
        from fl_studio_mcp import creative as creative_module

        trigger_entered = threading.Event()
        release_trigger = threading.Event()
        prepare_started = threading.Event()
        bootstrap_written = threading.Event()
        errors = []

        with PIANO_ROLL._lock:
            PIANO_ROLL._prepared = True
            PIANO_ROLL._armed = True

        def atomic(_path: Path, content: str) -> str:
            if content == creative_module._BOOTSTRAP_SCRIPT:
                bootstrap_written.set()
            return "c" * 64

        def trigger() -> HotkeyDispatch:
            trigger_entered.set()
            release_trigger.wait(timeout=2)
            return HotkeyDispatch(
                platform="macos",
                shortcut="Cmd+Opt+Y",
                fl_window_found=True,
                fl_window_focused=True,
                hotkey_dispatched=True,
            )

        def dispatch() -> None:
            try:
                write_piano_roll_notes(
                    [CreativeNote(pitch=60, start_beats=0.0, duration_beats=1.0)],
                    channel_index=2,
                    pattern_number=3,
                    auto_trigger=True,
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def prepare() -> None:
            prepare_started.set()
            try:
                PIANO_ROLL.bridge_action("prepare")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {"POSTFADER_PIANO_ROLL_SCRIPTS_DIR": directory},
            clear=False,
        ):
            with (
                mock.patch(
                    "fl_studio_mcp.creative._target_piano_roll",
                    return_value=target_receipt(),
                ),
                mock.patch(
                    "fl_studio_mcp.creative._atomic_text",
                    side_effect=atomic,
                ),
                mock.patch(
                    "fl_studio_mcp.creative._trigger_piano_roll_shortcut",
                    side_effect=trigger,
                ),
            ):
                dispatch_thread = threading.Thread(target=dispatch)
                dispatch_thread.start()
                self.assertTrue(trigger_entered.wait(timeout=2))

                prepare_thread = threading.Thread(target=prepare)
                prepare_thread.start()
                self.assertTrue(prepare_started.wait(timeout=2))
                self.assertFalse(bootstrap_written.wait(timeout=0.1))

                release_trigger.set()
                dispatch_thread.join(timeout=2)
                prepare_thread.join(timeout=2)

        self.assertFalse(dispatch_thread.is_alive())
        self.assertFalse(prepare_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(bootstrap_written.is_set())

    def test_native_pattern_marker_and_automation_workflows(self) -> None:
        client = DirectFakeClient()
        with mock.patch("fl_studio_mcp.creative.get_client", return_value=client):
            pattern = prepare_empty_pattern(name="Verse", length_beats=32)
            self.assertEqual(pattern.pattern_number, 3)
            self.assertEqual(pattern.outcome, "complete_verified")
            self.assertTrue(pattern.verified)
            self.assertEqual(_state.PATTERNS[3].name, "Verse")
            self.assertEqual(_state.PATTERNS[3].length, 32)

            markers = add_section_markers(
                [
                    SectionMarker(name="Intro", bar_number=1),
                    SectionMarker(name="Verse", bar_number=5, beat_offset=2),
                ]
            )
            self.assertTrue(markers.names_verified)
            self.assertFalse(markers.times_verified)
            self.assertFalse(markers.verified)
            self.assertEqual(_state.ARRANGEMENT_MARKERS[1][0], 18 * _state.REC_PPQ)

            _state.PLAYING = True
            _state.RECORDING = True
            automation = record_automation_value(
                target_kind="channel",
                target_index=1,
                property="pan",
                value_normalized=0.75,
                expected_before=0.5,
            )
            self.assertTrue(automation.control_value_verified)
            self.assertTrue(automation.capture_conditions_held)
            self.assertIsNone(automation.automation_event_recorded)
            self.assertFalse(automation.verified)
            self.assertAlmostEqual(_state.CHANNELS[1].pan, 0.5, places=3)

    def test_pattern_preparation_returns_ordered_partial_receipts(self) -> None:
        client = DirectFakeClient()
        with mock.patch("fl_studio_mcp.creative.get_client", return_value=client):
            complete = prepare_empty_pattern(name="Baseline", length_beats=8)

        _state.reset()
        failed_selection = complete.selection.model_copy(
            update={
                "verified": False,
                "verification_summary": "selection readback did not match",
            }
        )
        with (
            mock.patch("fl_studio_mcp.creative.get_client", return_value=client),
            mock.patch(
                "fl_studio_mcp.creative.TrackBController.select_pattern",
                return_value=failed_selection,
            ),
            mock.patch(
                "fl_studio_mcp.creative.TrackBController.set_pattern_identity"
            ) as identity_write,
        ):
            partial = prepare_empty_pattern(name="Verse", length_beats=32)
        self.assertEqual(partial.outcome, "selection_unverified")
        self.assertIsNone(partial.identity)
        self.assertIsNone(partial.length)
        self.assertFalse(partial.verified)
        identity_write.assert_not_called()

        _state.reset()
        failed_identity = complete.identity.model_copy(
            update={
                "verified": False,
                "name_verified": False,
                "verification_summary": "identity readback did not match",
            }
        )
        with (
            mock.patch("fl_studio_mcp.creative.get_client", return_value=client),
            mock.patch(
                "fl_studio_mcp.creative.TrackBController.select_pattern",
                return_value=complete.selection,
            ),
            mock.patch(
                "fl_studio_mcp.creative.TrackBController.set_pattern_identity",
                return_value=failed_identity,
            ),
            mock.patch(
                "fl_studio_mcp.creative.TrackBController.set_pattern_length"
            ) as length_write,
        ):
            partial = prepare_empty_pattern(name="Verse", length_beats=32)
        self.assertEqual(partial.outcome, "identity_unverified")
        self.assertIsNotNone(partial.identity)
        self.assertIsNone(partial.length)
        self.assertFalse(partial.verified)
        length_write.assert_not_called()

    def test_tempo_key_and_monophonic_transcription_use_decoded_audio(self) -> None:
        rate = 22050
        with tempfile.TemporaryDirectory() as directory:
            analysis_path = str(Path(directory) / "tempo-key.wav")
            duration = 8
            audio = np.zeros(rate * duration, dtype=np.float64)
            for onset in np.arange(0, duration, 0.5):
                start = int(onset * rate)
                audio[start:start + 600] += np.hanning(600)
            timeline = np.arange(len(audio)) / rate
            audio += 0.08 * np.sin(2 * np.pi * 261.625 * timeline)
            audio += 0.06 * np.sin(2 * np.pi * 329.628 * timeline)
            audio += 0.05 * np.sin(2 * np.pi * 391.995 * timeline)
            audio /= np.max(np.abs(audio))
            sf.write(analysis_path, audio, rate)
            report = analyze_tempo_and_key(analysis_path, max_seconds=8)
            self.assertTrue(report.tempo.available)
            self.assertLess(abs(report.tempo.bpm - 120.0), 7.0)
            self.assertEqual(report.key.key, "C major")
            self.assertIn(report.key.confidence, {"medium", "high"})

            melody_path = str(Path(directory) / "melody.wav")
            melody = np.zeros(rate * 2, dtype=np.float64)
            first_time = np.arange(int(rate * 0.8)) / rate
            melody[: len(first_time)] = 0.5 * np.sin(2 * np.pi * 440.0 * first_time)
            second_time = np.arange(int(rate * 0.8)) / rate
            melody[rate:rate + len(second_time)] = 0.5 * np.sin(
                2 * np.pi * 523.251 * second_time
            )
            sf.write(melody_path, melody, rate)
            transcription = transcribe_monophonic(
                melody_path,
                tempo_bpm=120.0,
                max_seconds=2.0,
                quantize_grid_beats=0.25,
            )
            self.assertEqual([note.pitch for note in transcription.sequence.notes], [69, 72])
            self.assertGreater(transcription.voiced_frame_share, 0.7)
            self.assertFalse(transcription.mutations_applied)


if __name__ == "__main__":
    unittest.main()
