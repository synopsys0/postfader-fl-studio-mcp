"""Existing-score inspection through generated scripts and the real fake-FL dispatcher."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from test_creative import DirectFakeClient, _state, bridge

from fl_studio_mcp import creative, piano_roll
from fl_studio_mcp.creative import PIANO_ROLL, PIANO_ROLL_SCRIPT_NAME, HotkeyDispatch


@dataclass(frozen=True)
class ObservedNote:
    number: int = 60
    time: int = 0
    length: int = 960
    velocity: float = 0.8
    pan: float = 0.5
    release: float = 0.6
    color: int = 0
    pitchofs: int = 0
    slide: bool = False
    porta: bool = False
    muted: bool = False
    selected: bool = False


class ReadOnlyScore:
    """Implements only reading; frozen notes make accidental edits fail."""

    def __init__(self, notes: tuple[ObservedNote, ...], ppq: int = 960) -> None:
        self.notes = notes
        self.PPQ = ppq
        self.read_indices: list[int] = []

    @property
    def noteCount(self) -> int:
        return len(self.notes)

    def getNote(self, index: int) -> ObservedNote:
        self.read_indices.append(index)
        return self.notes[index]


class DispatchClient(DirectFakeClient):
    """Use bridge command-policy enforcement as well as the fake FL API."""

    def __init__(self, observation_update: dict[str, object] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.ping_count = 0
        self.observation_update = observation_update

    def ping(self):
        self.ping_count += 1
        return super().ping()

    def call(self, command: str, **arguments):
        self.calls.append((command, arguments))
        response = bridge._dispatch({"id": len(self.calls), "cmd": command, "args": arguments})
        if isinstance(response, bridge._Job):
            while True:
                try:
                    next(response.gen)
                except StopIteration as stopped:
                    result = stopped.value
                    break
        else:
            if not response["ok"]:
                raise RuntimeError(response["error"])
            result = response["result"]
        if command == "creative.piano_roll_target" and self.observation_update:
            result = {**result, **self.observation_update}
        return result


def dispatch_receipt(*, dispatched: bool = True) -> HotkeyDispatch:
    return HotkeyDispatch(
        platform="macos", shortcut="Command+Option+Y", fl_window_found=True,
        fl_window_focused=True, hotkey_dispatched=dispatched,
        error=None if dispatched else "shortcut was not dispatched",
    )


class PianoRollTests(unittest.TestCase):
    def setUp(self) -> None:
        _state.reset()
        self.bridge_state = bridge.LEAN_WRITES_ENABLED, bridge.WRITE_MODE_ORIGIN
        bridge.LEAN_WRITES_ENABLED = False
        bridge.WRITE_MODE_ORIGIN = "test_read_only"
        with PIANO_ROLL._lock:
            self.piano_roll_state = {
                key: value for key, value in vars(PIANO_ROLL).items() if key != "_lock"
            }
            PIANO_ROLL._armed = True
        self.directory = tempfile.TemporaryDirectory(prefix="piano-roll-inspection-")
        self.addCleanup(self.directory.cleanup)
        self.script_directory = Path(self.directory.name)
        self.client = DispatchClient()
        self.addCleanup(mock.patch.stopall)
        mock.patch("fl_studio_mcp.creative.get_client", return_value=self.client).start()
        mock.patch.object(piano_roll, "get_client", return_value=self.client).start()
        # Even validation cases that unexpectedly reach dispatch must remain
        # entirely inside the fake runtime.
        mock.patch.object(
            piano_roll, "_trigger_piano_roll_shortcut",
            return_value=dispatch_receipt(dispatched=False),
        ).start()
        mock.patch(
            "fl_studio_mcp.creative.subprocess.run",
            side_effect=AssertionError("real subprocesses are prohibited in Piano Roll tests"),
        ).start()
        mock.patch.object(
            piano_roll, "piano_roll_scripts_directory", return_value=self.script_directory
        ).start()
        mock.patch.object(piano_roll, "PIANO_ROLL_RECEIPT_WAIT_SECONDS", 0.0).start()

    def tearDown(self) -> None:
        bridge.LEAN_WRITES_ENABLED, bridge.WRITE_MODE_ORIGIN = self.bridge_state
        with PIANO_ROLL._lock:
            vars(PIANO_ROLL).update(self.piano_roll_state)

    def _read(self, score: ReadOnlyScore, *, edit_receipt=None, dispatched=True, **arguments):
        fake_module = types.ModuleType("flpianoroll")
        fake_module.score = score

        def trigger():
            if dispatched:
                scope: dict[str, object] = {}
                script = self.script_directory / PIANO_ROLL_SCRIPT_NAME
                exec(compile(script.read_text(encoding="ascii"), str(script), "exec"), scope)
                if edit_receipt is not None:
                    edit_receipt(scope)
            return dispatch_receipt(dispatched=dispatched)

        with (
            mock.patch.dict(sys.modules, {"flpianoroll": fake_module}),
            mock.patch.object(piano_roll, "_trigger_piano_roll_shortcut", side_effect=trigger),
        ):
            return piano_roll.read_piano_roll_notes(
                channel_index=2, pattern_number=3, **arguments
            )

    def test_reads_existing_notes_without_enabling_project_writes(self) -> None:
        notes = (
            ObservedNote(number=0, time=-240, length=240),
            ObservedNote(
                number=131, time=480, length=1440, pitchofs=120,
                color=15, velocity=1.0, pan=0.0, release=0.0,
                slide=True, porta=True, muted=True, selected=True,
            ),
        )
        score = ReadOnlyScore(notes)
        result = self._read(score)
        self.assertEqual(result.status, "observed")
        self.assertEqual(result.total_note_count, 2)
        self.assertEqual(result.ppq, 960)
        self.assertEqual(result.notes[0].start_ticks, -240)
        self.assertEqual(result.notes[0].start_beats, -0.25)
        self.assertEqual(result.notes[1].start_beats, 0.5)
        self.assertEqual(result.notes[1].duration_beats, 1.5)
        self.assertEqual(result.notes[1].pitch, 131)
        self.assertEqual(result.notes[1].pitch_offset_tenths, 120)
        self.assertTrue(result.notes[1].slide and result.notes[1].portamento)
        self.assertTrue(result.notes[1].muted and result.notes[1].selected)
        self.assertEqual(result.notes[1].release, 0.0)
        self.assertEqual(score.notes, notes)
        self.assertEqual(score.read_indices, [0, 1])
        self.assertFalse(result.notes_modified)
        self.assertFalse(result.project_saved)
        self.assertFalse(bridge.LEAN_WRITES_ENABLED)
        self.assertEqual(
            [command for command, _ in self.client.calls],
            ["creative.prepare_piano_roll", "creative.piano_roll_target"],
        )
        self.assertEqual(self.client.calls[0][1]["index_scope"], "global")
        self.assertEqual(result.target.after_channel_indices, [2])
        self.assertEqual(result.target.after_pattern_number, 3)
        self.assertIn("start_beats", result.model_dump()["notes"][0])

    def test_selected_filter_keeps_raw_page_offsets_and_can_return_empty_pages(self) -> None:
        score = ReadOnlyScore(tuple(ObservedNote(selected=index == 4) for index in range(6)), ppq=192)
        first = self._read(score, offset=1, limit=2, selected_only=True)
        self.assertEqual(first.notes, ())
        self.assertEqual(first.next_offset, 3)
        self.assertEqual(first.total_note_count, 6)
        self.assertEqual(score.read_indices, [1, 2])
        second = self._read(score, offset=3, limit=2, selected_only=True)
        self.assertEqual(tuple(note.note_index for note in second.notes), (4,))
        self.assertEqual(second.next_offset, 5)
        self.assertEqual(second.ppq, 192)
        self.assertEqual(score.read_indices, [1, 2, 3, 4])
        last = self._read(score, offset=5, limit=2, selected_only=True)
        self.assertEqual(last.notes, ())
        self.assertIsNone(last.next_offset)

    def test_empty_or_exhausted_score_returns_an_observed_empty_page(self) -> None:
        for notes, offset in (((), 0), ((ObservedNote(),), 50)):
            with self.subTest(count=len(notes), offset=offset):
                result = self._read(ReadOnlyScore(notes), offset=offset)
                self.assertEqual(result.status, "observed")
                self.assertEqual(result.total_note_count, len(notes))
                self.assertEqual(result.notes, ())
                self.assertIsNone(result.next_offset)

    def test_target_drift_suppresses_all_note_attribution(self) -> None:
        changes = (
            {"channel_indices": [0]}, {"channel_indices": [0, 2]},
            {"pattern_number": 1}, {"piano_roll_visible": False},
            {"session_fingerprint": "f" * 32}, {"channel_indices": "malformed"},
        )
        for change in changes:
            with self.subTest(change=change):
                self.client.observation_update = change
                result = self._read(ReadOnlyScore((ObservedNote(),)))
                self.assertEqual(result.status, "target_changed")
                self.assertEqual(result.notes, ())
                self.assertIsNone(result.total_note_count)
                self.assertIsNone(result.ppq)
                self.assertIsNone(result.next_offset)

    def test_failed_target_readback_suppresses_notes(self) -> None:
        original = self.client.call

        def call(command, **arguments):
            if command == "creative.piano_roll_target":
                raise RuntimeError("bridge disconnected")
            return original(command, **arguments)

        with mock.patch.object(self.client, "call", side_effect=call):
            result = self._read(ReadOnlyScore((ObservedNote(),)))
        self.assertEqual(result.status, "target_changed")
        self.assertEqual(result.notes, ())
        self.assertIn("bridge disconnected", result.warnings[0])

    def test_undispatched_shortcut_returns_without_waiting_or_readback(self) -> None:
        with mock.patch.object(piano_roll, "_read_snapshot_receipt") as receipt_reader:
            result = self._read(ReadOnlyScore((ObservedNote(),)), dispatched=False)
        self.assertEqual(result.status, "not_dispatched")
        self.assertEqual(result.notes, ())
        receipt_reader.assert_not_called()
        self.assertEqual([command for command, _ in self.client.calls], ["creative.prepare_piano_roll"])

    def test_missing_or_malformed_receipt_is_reported_without_notes(self) -> None:
        for contents in (None, b"not-json", b'{"payload":{}}', b"\xff"):
            with self.subTest(contents=contents):
                def edit(scope):
                    path = Path(scope["RECEIPT_PATH"])
                    if contents is None:
                        path.unlink()
                    else:
                        path.write_bytes(contents)
                result = self._read(ReadOnlyScore((ObservedNote(),)), edit_receipt=edit)
                self.assertEqual(result.status, "receipt_unavailable")
                self.assertEqual(result.notes, ())
                self.assertIsNone(result.total_note_count)

    def test_malformed_signature_is_reported_without_raising_type_error(self) -> None:
        def edit(scope):
            path = Path(scope["RECEIPT_PATH"])
            value = json.loads(path.read_text(encoding="ascii"))
            value["hmac_sha256"] = "\u2603" * 64
            path.write_text(json.dumps(value), encoding="ascii")
        result = self._read(ReadOnlyScore((ObservedNote(),)), edit_receipt=edit)
        self.assertEqual(result.status, "receipt_unavailable")
        self.assertEqual(result.notes, ())

    def test_script_failure_reports_runtime_error_without_fake_empty_score(self) -> None:
        result = self._read(ReadOnlyScore((ObservedNote(),), ppq=0))
        self.assertEqual(result.status, "receipt_unavailable")
        self.assertIsNone(result.total_note_count)
        self.assertIsNone(result.ppq)
        self.assertIn("unsupported PPQ", result.warnings[0])

    def test_invalid_arguments_are_rejected_before_bridge_or_file_effects(self) -> None:
        invalid = (
            {"channel_index": -1}, {"channel_index": True}, {"channel_index": 1.5},
            {"pattern_number": 0}, {"pattern_number": 1000}, {"pattern_number": True},
            {"offset": -1}, {"offset": True}, {"offset": piano_roll.MAX_SCORE_NOTES + 1},
            {"limit": 0}, {"limit": True}, {"limit": piano_roll.MAX_NOTE_PAGE + 1},
            {"selected_only": 1}, {"session_fingerprint": "not-a-session"},
        )
        with mock.patch.object(piano_roll, "_trigger_piano_roll_shortcut") as trigger:
            for value in invalid:
                with self.subTest(arguments=value), self.assertRaises(ValueError):
                    piano_roll.read_piano_roll_notes(**{
                        "channel_index": 2, "pattern_number": 3, **value,
                    })
        trigger.assert_not_called()
        self.assertEqual(self.client.ping_count, 0)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(list(self.script_directory.iterdir()), [])

    def test_live_range_and_stale_session_reject_before_navigation(self) -> None:
        with mock.patch.object(bridge.patterns, "patternMax", return_value=3):
            for arguments in ({"channel_index": 99}, {"pattern_number": 4}, {"session_fingerprint": "b" * 32}):
                with self.subTest(arguments=arguments), self.assertRaises((ValueError, RuntimeError)):
                    piano_roll.read_piano_roll_notes(**{
                        "channel_index": 2, "pattern_number": 3, **arguments,
                    })
                self.assertEqual(_state.CURRENT_PATTERN, 1)
                self.assertEqual([i for i, channel in enumerate(_state.CHANNELS) if channel.selected], [0])
        self.assertEqual(list(self.script_directory.iterdir()), [])

    def test_unarmed_bridge_rejects_before_navigation(self) -> None:
        PIANO_ROLL._armed = False
        with self.assertRaisesRegex(ValueError, "not armed"):
            piano_roll.read_piano_roll_notes(channel_index=2, pattern_number=3)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(list(self.script_directory.iterdir()), [])

    def test_real_shortcut_helper_respects_offline_sandbox_before_subprocess(self) -> None:
        with (
            mock.patch.dict(os.environ, {"FL_BRIDGE_SANDBOXED": "1"}),
            mock.patch.object(
                creative.subprocess, "run",
                side_effect=AssertionError("offline tests cannot invoke OS automation"),
            ) as subprocess_run,
        ):
            result = creative._trigger_piano_roll_shortcut()
        subprocess_run.assert_not_called()
        self.assertFalse(result.hotkey_dispatched)
        self.assertIn("offline", (result.error or "").lower())


class PianoRollReceiptTests(unittest.TestCase):
    request_id = "a" * 32
    secret = "b" * 64

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="piano-roll-receipt-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "receipt.json"
        note = dict(
            note_index=0, pitch=60, start_ticks=0, length_ticks=96, ppq=96,
            velocity=0.8, pan=0.5, release=0.5, color=0, pitch_offset_tenths=0,
            slide=False, portamento=False, muted=False, selected=False,
        )
        self.payload = dict(
            request_id=self.request_id, operation="read_notes", offset=0, limit=2,
            selected_only=False, total_note_count=2, ppq=96,
            notes=[note, {**note, "note_index": 1}], script_completed=True, error=None,
        )

    def _write(self, payload, *, signed=True):
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
        signature = hmac.new(bytes.fromhex(self.secret), canonical, hashlib.sha256).hexdigest()
        self.path.write_text(json.dumps({
            "payload": payload, "hmac_sha256": signature if signed else "0" * 64,
        }), encoding="ascii")

    def _read(self, *, selected_only=False):
        return piano_roll._read_snapshot_receipt(
            self.path, request_id=self.request_id, receipt_secret=self.secret,
            offset=0, limit=2, selected_only=selected_only,
        )

    def test_signed_valid_page_parses_raw_note_values(self) -> None:
        self._write(self.payload)
        result = self._read()
        self.assertEqual(tuple(note.note_index for note in result.notes), (0, 1))
        self.assertEqual(result.notes[0].duration_beats, 1.0)

    def test_receipt_rejects_invalid_identity_page_and_note_metadata(self) -> None:
        changes = (
            {"request_id": "c" * 32}, {"operation": "write_notes"}, {"offset": 1},
            {"limit": 1}, {"selected_only": True}, {"total_note_count": 2.0},
            {"script_completed": False, "error": "could not read score"},
            {"notes": self.payload["notes"][:1]},
            {"notes": list(reversed(self.payload["notes"]))},
            {"notes": [self.payload["notes"][0]] * 2},
            {"notes": [{**self.payload["notes"][0], "ppq": 192}, self.payload["notes"][1]]},
            {"notes": [self.payload["notes"][0], {**self.payload["notes"][1], "note_index": 2}]},
            {"notes": [self.payload["notes"][0], {**self.payload["notes"][1], "pitch": 132}]},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self._write({**copy.deepcopy(self.payload), **change})
                self._read()

    def test_selected_page_rejects_unselected_notes(self) -> None:
        self._write({**self.payload, "selected_only": True})
        with self.assertRaisesRegex(ValueError, "metadata is inconsistent"):
            self._read(selected_only=True)

    def test_signature_mismatch_and_oversized_receipt_are_rejected(self) -> None:
        self._write(self.payload, signed=False)
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self._read()
        self.path.write_bytes(b" " * (piano_roll.MAX_SNAPSHOT_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "response limit"):
            self._read()


if __name__ == "__main__":
    unittest.main(verbosity=2)
