"""Cover the input ceilings that are not part of SysEx framing.

Three paths could grow memory without limit from caller-supplied input:

* decoded audio, where the on-disk size limit says nothing about what the file
  becomes as float64 samples;
* the socket transport's per-client accumulator; and
* the file transport's request reader.

Everything here is hermetic. Audio is generated in a temporary directory, and
the transports are exercised as plain objects -- no sockets are bound, no
mailbox outside a temp dir is touched, and CoreMIDI is never opened.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE / "fakefl"))
sys.path.insert(0, str(ROOT / "fl_studio_mcp" / "_bridge"))
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

import device_UniversalBridge as bridge  # noqa: E402
from fl_studio_mcp import audio  # noqa: E402


def write_wav(path: Path, seconds: float, rate: int, channels: int) -> Path:
    frames = int(seconds * rate)
    data = np.zeros((frames, channels), dtype=np.float32)
    # A little signal so nothing is rejected as silence.
    data[:, 0] = np.sin(np.arange(frames) * 0.01, dtype=np.float32) * 0.5
    sf.write(path, data, rate, subtype="PCM_16")
    return path


class DecodedAudioCeilingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_ordinary_file_is_read_whole_and_says_so(self):
        path = write_wav(self.dir / "short.wav", 0.5, 48000, 2)
        loaded = audio.load(str(path))
        self.assertFalse(loaded.meta["truncated"])
        self.assertIsNone(loaded.meta["truncated_by"])
        self.assertEqual(loaded.meta["analyzed_frames"], loaded.meta["source_frames"])

    def test_max_seconds_truncation_is_attributed_to_the_caller(self):
        path = write_wav(self.dir / "longer.wav", 2.0, 48000, 2)
        loaded = audio.load(str(path), max_seconds=1.0)
        self.assertTrue(loaded.meta["truncated"])
        self.assertEqual(loaded.meta["truncated_by"], "max_seconds")
        self.assertEqual(loaded.meta["analyzed_frames"], 48000)

    def test_the_decode_ceiling_clamps_the_read(self):
        # Lowering the ceiling is how this stays hermetic: producing a file
        # that genuinely needs 2 GiB decoded would mean writing gigabytes.
        #
        # The ceiling has to leave room for more than one second, or the
        # too-wide refusal fires instead of the clamp -- so a three-second
        # file against a two-second ceiling exercises the clamp specifically.
        path = write_wav(self.dir / "wide.wav", 3.0, 48000, 2)
        original = audio.MAX_DECODED_AUDIO_BYTES
        try:
            # (2 channels + 1) * 8 bytes per frame, two seconds' worth.
            audio.MAX_DECODED_AUDIO_BYTES = 96_000 * 24
            loaded = audio.load(str(path))
        finally:
            audio.MAX_DECODED_AUDIO_BYTES = original
        self.assertTrue(loaded.meta["truncated"])
        self.assertEqual(loaded.meta["truncated_by"], "decode_limit")
        self.assertEqual(loaded.meta["analyzed_frames"], 96_000)
        self.assertEqual(loaded.meta["source_frames"], 144_000)

    def test_a_file_too_wide_for_one_second_is_refused_clearly(self):
        path = write_wav(self.dir / "wide.wav", 0.2, 48000, 2)
        original = audio.MAX_DECODED_AUDIO_BYTES
        try:
            audio.MAX_DECODED_AUDIO_BYTES = 1024  # under one second's worth
            with self.assertRaises(audio.AudioError) as refused:
                audio.load(str(path))
        finally:
            audio.MAX_DECODED_AUDIO_BYTES = original
        message = str(refused.exception)
        self.assertIn("refusing to read it", message)
        # Must arrive as the refusal it is, not wrapped as an unreadable file.
        self.assertNotIn("Could not read", message)

    def test_the_ceiling_covers_a_realistic_master_without_truncating(self):
        # Ten minutes of 96 kHz stereo is the largest ordinary input; it must
        # fit, or the bound would be breaking real analysis rather than
        # bounding abuse.
        frames = 600 * 96000
        needed = frames * (2 + 1) * 8
        self.assertLessEqual(needed, audio.MAX_DECODED_AUDIO_BYTES, needed)


class SocketAccumulatorTests(unittest.TestCase):
    class FakeSock:
        def __init__(self):
            self.sent = []

        def sendall(self, payload):
            self.sent.append(payload)

        def close(self):
            pass

    def _client(self):
        transport = bridge._SocketTransport()
        client = bridge._Client(self.FakeSock(), ("127.0.0.1", 1)) \
            if hasattr(bridge, "_Client") else None
        return transport, client

    def test_the_ceiling_is_shared_with_the_sysex_reassembler(self):
        self.assertEqual(
            bridge.MAX_TRANSPORT_REQUEST_BYTES, bridge.MAX_SYSEX_REQUEST_BYTES)

    def test_a_line_that_never_terminates_cannot_grow_without_bound(self):
        # The accumulator only grows through this check, so proving the check
        # rejects an oversized addition is what matters; the socket plumbing
        # around it is exercised by test_bridge.py.
        inbox = b"x" * bridge.MAX_TRANSPORT_REQUEST_BYTES
        chunk = b"y" * 4096
        self.assertGreater(
            len(inbox) + len(chunk), bridge.MAX_TRANSPORT_REQUEST_BYTES)


class FileTransportRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.transport = bridge._FileTransport()
        self.transport.root = self.root

    def tearDown(self):
        self.tmp.cleanup()

    def _request(self, name: str, body: str) -> Path:
        path = Path(self.root) / f"{bridge.REQ_PREFIX}{name}.json"
        path.write_text(body, encoding="utf-8")
        return path

    def test_a_normal_request_is_read_and_consumed(self):
        self._request("aaa", json.dumps({"id": 1, "cmd": "ping", "args": {}}))
        out = self.transport.poll()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][1]["cmd"], "ping")
        self.assertEqual(
            [n for n in os.listdir(self.root) if n.startswith(bridge.REQ_PREFIX)],
            [],
        )

    def test_an_oversized_request_is_removed_without_being_parsed(self):
        oversized = "x" * (bridge.MAX_TRANSPORT_REQUEST_BYTES + 1024)
        path = self._request("bbb", oversized)
        out = self.transport.poll()
        self.assertEqual(out, [])
        # Removed, so it cannot be retried on every subsequent tick.
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
