"""Execute generated mutations twice to verify unknown outcomes cannot replay."""

from __future__ import annotations

import builtins
import json
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from fl_studio_mcp import creative


@dataclass
class Note:
    number: int = 60
    time: int = 0
    length: int = 96
    velocity: float = 0.75
    pan: float = 0.5
    release: float = 0.5
    color: int = 0
    pitchofs: int = 0
    slide: bool = False
    porta: bool = False
    muted: bool = False
    selected: bool = True


class Score:
    PPQ = 96

    def __init__(self, notes=(), *, fail_after_add=False):
        self.notes = list(notes)
        self.add_calls = 0
        self.clear_calls = 0
        self.fail_after_add = fail_after_add

    @property
    def noteCount(self):
        return len(self.notes)

    def getNote(self, index):
        return self.notes[index]

    def addNote(self, note):
        self.add_calls += 1
        self.notes.append(note)
        if self.fail_after_add:
            raise RuntimeError("failure after the note was added")

    def clearNotes(self, *_arguments):
        self.clear_calls += 1
        self.notes.clear()

    def deleteNote(self, index):
        del self.notes[index]


class FailedReceiptWrite:
    """Keep exclusive file creation real, fail the completion write afterward."""

    def __init__(self, handle):
        self.handle = handle

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *arguments):
        return self.handle.__exit__(*arguments)

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def write(self, _data):
        raise OSError("completion receipt write failed")


class PianoRollReplayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="piano-roll-replay-")
        self.addCleanup(self.directory.cleanup)
        self.receipt_path = Path(self.directory.name) / "apply.json"
        self.notes = [creative.CreativeNote(pitch=64, start_beats=0, duration_beats=1)]
        self.request_id = "a" * 32
        self.secret = "b" * 64

    def _write_script(self, mode="append"):
        return creative._notes_script(
            self.notes, mode, self.request_id,
            requested_note_digest=creative.note_digest(self.notes),
            receipt_path=self.receipt_path, receipt_secret=self.secret,
        )

    def _transform_script(self, **arguments):
        return creative._transform_script(
            creative.PianoRollTransform(scope="all", **arguments), self.request_id,
            receipt_path=self.receipt_path,
        )

    @staticmethod
    def _execute(source, score, **scope):
        module = types.ModuleType("flpianoroll")
        module.Note = Note
        module.score = score
        with mock.patch.dict(sys.modules, {"flpianoroll": module}):
            exec(compile(source, "Postfader_Apply.pyscript", "exec"), dict(scope))

    def test_append_replay_preserves_one_mutation_and_authenticated_receipt(self):
        score = Score()
        source = self._write_script()
        self._execute(source, score)
        first_receipt = self.receipt_path.read_bytes()
        self._execute(source, score)
        self.assertEqual([note.number for note in score.notes], [64])
        self.assertEqual(score.add_calls, 1)
        self.assertEqual(self.receipt_path.read_bytes(), first_receipt)
        evidence, error = creative._read_piano_roll_receipt(
            self.receipt_path, receipt_secret=self.secret, request_id=self.request_id,
            requested_note_digest=creative.note_digest(self.notes), notes=self.notes,
            mode="append",
        )
        self.assertIsNone(error)
        self.assertTrue(evidence.script_completed and evidence.postcondition_verified)

    def test_replace_replay_does_not_clear_intervening_user_edits(self):
        score = Score((Note(number=60),))
        source = self._write_script("replace")
        self._execute(source, score)
        score.notes.append(Note(number=72))
        self._execute(source, score)
        self.assertEqual([note.number for note in score.notes], [64, 72])
        self.assertEqual(score.clear_calls, 1)

    def test_partial_mutation_failure_emits_failure_and_blocks_replay(self):
        score = Score(fail_after_add=True)
        source = self._write_script()
        with self.assertRaisesRegex(RuntimeError, "after the note was added"):
            self._execute(source, score)
        payload = json.loads(self.receipt_path.read_text())["payload"]
        self.assertFalse(payload["script_completed"])
        self.assertFalse(payload["postcondition_verified"])
        self._execute(source, score)
        self.assertEqual(score.add_calls, 1)
        self.assertEqual(score.noteCount, 1)

    def test_completion_receipt_failure_keeps_claim_and_blocks_replay(self):
        score = Score()
        source = self._write_script()

        def failed_open(path, mode):
            return FailedReceiptWrite(builtins.open(path, mode))

        with self.assertRaisesRegex(OSError, "completion receipt write failed"):
            self._execute(source, score, open=failed_open)
        self.assertEqual(self.receipt_path.read_bytes(), b"")
        self._execute(source, score)
        self.assertEqual(score.add_calls, 1)

    def test_existing_incomplete_claim_blocks_score_access(self):
        class InaccessibleScore:
            @property
            def noteCount(self):
                raise AssertionError("a claimed request accessed the score")

        self.receipt_path.write_bytes(b"")
        self._execute(self._write_script(), InaccessibleScore())
        self._execute(
            self._transform_script(operation="transpose", semitones=12),
            InaccessibleScore(),
        )
        self.assertEqual(self.receipt_path.read_bytes(), b"")

    def test_concurrent_executions_reserve_only_one_mutation(self):
        source = self._write_script()
        score = Score()
        module = types.ModuleType("flpianoroll")
        module.Note = Note
        module.score = score
        with mock.patch.dict(sys.modules, {"flpianoroll": module}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(exec, compile(source, "replay", "exec"), {}) for _ in range(2)]
                for future in futures:
                    future.result()
        self.assertEqual(score.add_calls, 1)
        self.assertEqual(score.noteCount, 1)

    def test_transform_replay_does_not_transpose_or_duplicate_twice(self):
        for arguments in (
            {"operation": "transpose", "semitones": 12},
            {"operation": "duplicate", "offset_beats": 1},
        ):
            with self.subTest(arguments=arguments):
                self.receipt_path.unlink(missing_ok=True)
                source = self._transform_script(**arguments)
                score = Score((Note(),))
                self._execute(source, score)
                observed = [(note.number, note.time) for note in score.notes]
                self._execute(source, score)
                self.assertEqual([(note.number, note.time) for note in score.notes], observed)
                expected = [(72, 0)] if arguments["operation"] == "transpose" else [(60, 0), (60, 96)]
                self.assertEqual(observed, expected)

    def test_transform_partial_failure_keeps_claim_and_blocks_replay(self):
        source = self._transform_script(operation="duplicate", offset_beats=1)
        score = Score((Note(),), fail_after_add=True)
        with self.assertRaisesRegex(RuntimeError, "after the note was added"):
            self._execute(source, score)
        self._execute(source, score)
        self.assertEqual(score.add_calls, 1)
        self.assertEqual(score.noteCount, 2)

    def test_claim_creation_failure_prevents_mutation(self):
        for source in (
            self._write_script(),
            self._transform_script(operation="transpose", semitones=12),
        ):
            with self.subTest(source=source.splitlines()[2]):
                score = Score((Note(),))
                with self.assertRaisesRegex(PermissionError, "no receipt access"):
                    self._execute(source, score, open=mock.Mock(side_effect=PermissionError("no receipt access")))
                self.assertEqual([(note.number, note.time) for note in score.notes], [(60, 0)])
                self.assertEqual(score.add_calls, 0)

    def test_claim_sync_failure_prevents_mutation_and_blocks_replay(self):
        source = self._write_script()
        score = Score()
        with mock.patch("os.fsync", side_effect=OSError("claim was not durable")):
            with self.assertRaisesRegex(OSError, "claim was not durable"):
                self._execute(source, score)
        self._execute(source, score)
        self.assertEqual(score.add_calls, 0)


if __name__ == "__main__":
    unittest.main()
