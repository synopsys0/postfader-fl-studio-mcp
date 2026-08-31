"""Bounded local Sound Selection history tests."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from fl_studio_mcp.sound_selection import history as history_module
from fl_studio_mcp.sound_selection.history import (
    MAX_HISTORY_COUNTER,
    MAX_HISTORY_SERIALIZED_BYTES,
    HistoryCorruptionError,
    HistoryWriteError,
    LocalSoundSelectionHistory,
    SoundHistoryRecord,
)
from fl_studio_mcp.sound_selection.models import preset_identity_digest


class SoundSelectionHistoryTests(unittest.TestCase):
    def test_atomic_persistence_pruning_and_explicit_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "sound-history.json"
            store = LocalSoundSelectionHistory(path, max_records=2)
            start = datetime(2025, 1, 1, tzinfo=timezone.utc)
            for index in range(3):
                digest = preset_identity_digest("synth", f"Preset {index}", index)
                self.assertTrue(
                    store.record_usage(
                        "synth",
                        digest,
                        "main_lead",
                        preset_name=f"Preset {index}",
                        now=start + timedelta(days=index),
                    )
                )
            self.assertEqual(store.status().record_count, 2)
            self.assertTrue(store.path.exists())
            digest = preset_identity_digest("synth", "Preset 2", 2)
            self.assertTrue(
                store.record_feedback(
                    palette_id="palette-test",
                    role_id="main_lead",
                    product_id="synth",
                    preset_identity_digest=digest,
                    verdict="accepted",
                    now=start + timedelta(days=4),
                )
            )
            self.assertEqual(store.lookup(product_id="synth", preset_identity_digest=digest, role_id="main_lead").accepted_count, 1)

    def test_no_persist_and_corruption_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "sound-history.json"
            store = LocalSoundSelectionHistory(path)
            digest = preset_identity_digest("synth", "Preset", 1)
            self.assertFalse(store.record_usage("synth", digest, "main_lead", persist=False))
            self.assertFalse(path.exists())
            path.write_text("{not-json", encoding="utf-8")
            corrupt = LocalSoundSelectionHistory(path)
            self.assertTrue(corrupt.status().corrupt)
            with self.assertRaises(HistoryCorruptionError):
                corrupt.record_usage("synth", digest, "main_lead")
            with self.assertRaises(HistoryCorruptionError):
                corrupt.record_feedback(
                    palette_id="palette-test",
                    verdict="neutral",
                )
            self.assertEqual(path.read_text(encoding="utf-8"), "{not-json")
            result = corrupt.reset()
            self.assertTrue(result.removed)
            self.assertFalse(path.exists())

    def test_case_only_usage_updates_cannot_create_duplicate_identity_rows(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "sound-history.json"
            store = LocalSoundSelectionHistory(path)
            digest = preset_identity_digest("synth", "Preset", 1)
            stamp = datetime(2025, 1, 1, tzinfo=timezone.utc)
            self.assertTrue(store.record_usage("Synth", digest, "main_lead", now=stamp))
            self.assertTrue(
                store.record_usage(
                    "synth",
                    digest,
                    "main_lead",
                    now=stamp + timedelta(days=1),
                )
            )
            records = store.records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].usage_count, 2)
            self.assertEqual(records[0].product_id, "synth")

    def test_oversized_input_is_isolated_without_loading_unbounded_data(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "sound-history.json"
            with path.open("wb") as handle:
                handle.seek(MAX_HISTORY_SERIALIZED_BYTES)
                handle.write(b"x")

            store = LocalSoundSelectionHistory(path)

            status = store.status()
            self.assertTrue(status.corrupt)
            self.assertFalse(status.healthy)
            self.assertIn("8 MiB", status.error or "")
            self.assertEqual(path.stat().st_size, MAX_HISTORY_SERIALIZED_BYTES + 1)

    def test_oversized_serialized_write_is_refused_before_replacing_source(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "sound-history.json"
            store = LocalSoundSelectionHistory(path)
            digest = preset_identity_digest("synth", "Preset", 1)
            store.record_usage("synth", digest, "main_lead")
            original = path.read_bytes()
            document = store.snapshot()

            with patch.object(history_module, "MAX_HISTORY_SERIALIZED_BYTES", 64):
                with self.assertRaises(HistoryWriteError):
                    with store._lock:
                        store._write_locked(document)

            self.assertEqual(path.read_bytes(), original)

    def test_history_counters_have_a_finite_bound_and_saturate(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "sound-history.json"
            store = LocalSoundSelectionHistory(path)
            digest = preset_identity_digest("synth", "Preset", 1)
            store.record_usage("synth", digest, "main_lead")
            current = store.records()[0]

            with self.assertRaises(ValidationError):
                SoundHistoryRecord(
                    **{
                        **current.model_dump(),
                        "usage_count": MAX_HISTORY_COUNTER + 1,
                    }
                )

            saturated = current.model_copy(
                update={
                    "usage_count": MAX_HISTORY_COUNTER,
                    "consecutive_use_count": MAX_HISTORY_COUNTER,
                    "accepted_count": MAX_HISTORY_COUNTER,
                    "rejected_count": MAX_HISTORY_COUNTER,
                }
            )
            document = store.snapshot().model_copy(update={"records": (saturated,)})
            with store._lock:
                store._write_locked(document)

            self.assertTrue(store.record_usage("synth", digest, "main_lead"))
            self.assertEqual(store.records()[0].usage_count, MAX_HISTORY_COUNTER)
            self.assertEqual(
                store.records()[0].consecutive_use_count, MAX_HISTORY_COUNTER
            )
            self.assertTrue(
                store.record_feedback(
                    palette_id="palette-test",
                    role_id="main_lead",
                    product_id="synth",
                    preset_identity_digest=digest,
                    verdict="accepted",
                )
            )
            self.assertTrue(
                store.record_feedback(
                    palette_id="palette-test-2",
                    role_id="main_lead",
                    product_id="synth",
                    preset_identity_digest=digest,
                    verdict="rejected",
                )
            )
            final = store.records()[0]
            self.assertEqual(final.accepted_count, MAX_HISTORY_COUNTER)
            self.assertEqual(final.rejected_count, MAX_HISTORY_COUNTER)


if __name__ == "__main__":
    unittest.main()
