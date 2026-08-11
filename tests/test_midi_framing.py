#!/usr/bin/env python3
"""Hermetic abuse-case tests for the SysEx framing protocol.

This file never imports or opens CoreMIDI. It feeds byte strings directly to
the FL-side reassembler and uses an in-memory stand-in for the client's MIDI
input, so it is safe on a machine where FL Studio and an IAC bus are live.
"""

from __future__ import annotations

import json
import os
import sys
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "fakefl"))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
sys.path.insert(0, ROOT)

os.environ.pop("FL_BRIDGE_ENABLE_WRITES", None)

import device_UniversalBridge as bridge  # noqa: E402
from fl_studio_mcp import bridge_client  # noqa: E402


def frame(tag: int, mid: int, seq: int, total: int, payload: bytes,
          *, terminated: bool = True) -> bytes:
    header = bytes([
        0xF0,
        bridge.SYSEX_ID,
        tag,
        (mid >> 7) & 0x7F,
        mid & 0x7F,
        (seq >> 7) & 0x7F,
        seq & 0x7F,
        (total >> 7) & 0x7F,
        total & 0x7F,
    ])
    return header + payload + (b"\xF7" if terminated else b"")


class QueueMidiIn:
    def __init__(self, *messages: bytes):
        self.messages = [(list(message), 0.0) for message in messages]

    def get_message(self):
        return self.messages.pop(0) if self.messages else None


class SinkMidiOut:
    def __init__(self):
        self.messages = []

    def send_message(self, message):
        self.messages.append(list(message))


class BridgeFramingTests(unittest.TestCase):
    def setUp(self):
        self.transport = bridge._MidiTransport()

    def feed(self, mid: int, seq: int, total: int, payload: bytes,
             *, terminated: bool = True) -> None:
        self.transport.feed(
            frame(
                bridge.TAG_REQUEST,
                mid,
                seq,
                total,
                payload,
                terminated=terminated,
            )
        )

    def test_valid_contiguous_request_is_reassembled(self):
        body = json.dumps({"id": 7, "cmd": "ping", "args": {}}).encode("ascii")
        cut = len(body) // 2
        self.feed(7, 0, 2, body[:cut])
        self.feed(7, 1, 2, body[cut:])

        self.assertEqual(
            self.transport.ready,
            [(7, {"id": 7, "cmd": "ping", "args": {}})],
        )
        self.assertEqual(self.transport.partial, {})
        self.assertEqual(self.transport.partial_bytes, 0)

    def test_oversized_and_invalid_sequence_frames_are_dropped(self):
        invalid = (
            frame(
                bridge.TAG_REQUEST,
                1,
                0,
                2,
                b"A" * (bridge.SYSEX_CHUNK + 1),
            ),
            frame(bridge.TAG_REQUEST, 1, 0, 0, b"A"),
            frame(bridge.TAG_REQUEST, 1, 2, 2, b"A"),
            frame(
                bridge.TAG_REQUEST,
                1,
                0,
                bridge.MAX_SYSEX_REQUEST_PARTS + 1,
                b"A",
            ),
        )
        for message in invalid:
            with self.subTest(length=len(message)):
                self.transport.feed(message)
                self.assertEqual(self.transport.partial, {})
                self.assertEqual(self.transport.ready, [])

    def test_unterminated_frame_remains_compatible_but_is_still_bounded(self):
        body = json.dumps({"id": 2, "cmd": "ping", "args": {}}).encode("ascii")
        self.feed(2, 0, 1, body, terminated=False)
        self.assertEqual(self.transport.ready[0][1]["cmd"], "ping")

    def test_inconsistent_total_and_conflicting_duplicate_clear_the_message(self):
        self.feed(3, 0, 2, b'{"id":3,')
        self.feed(3, 1, 3, b'"cmd":"ping"}')
        self.assertEqual(self.transport.partial, {})
        self.assertEqual(self.transport.partial_bytes, 0)

        self.feed(4, 0, 2, b'{"id":4,')
        self.feed(4, 0, 2, b'{"id":5,')
        self.assertEqual(self.transport.partial, {})
        self.assertEqual(self.transport.partial_bytes, 0)

    def test_partial_message_count_and_aggregate_bytes_are_bounded(self):
        for mid in range(1, bridge.MAX_SYSEX_PARTIAL_MESSAGES + 5):
            self.feed(mid, 0, 2, b"A" * bridge.SYSEX_CHUNK)
        self.assertLessEqual(
            len(self.transport.partial), bridge.MAX_SYSEX_PARTIAL_MESSAGES
        )
        self.assertLessEqual(
            self.transport.partial_bytes, bridge.MAX_SYSEX_PARTIAL_BYTES
        )

        filled = bridge._MidiTransport()
        total = bridge.MAX_SYSEX_REQUEST_PARTS
        for seq in range(total - 1):
            filled.feed(
                frame(
                    bridge.TAG_REQUEST,
                    1,
                    seq,
                    total,
                    b"B" * bridge.SYSEX_CHUNK,
                )
            )
        filled.feed(
            frame(
                bridge.TAG_REQUEST,
                2,
                0,
                2,
                b"C" * bridge.SYSEX_CHUNK,
            )
        )
        filled.feed(
            frame(
                bridge.TAG_REQUEST,
                3,
                0,
                2,
                b"D" * bridge.SYSEX_CHUNK,
            )
        )
        self.assertLessEqual(filled.partial_bytes, bridge.MAX_SYSEX_PARTIAL_BYTES)
        self.assertNotIn(3, filled.partial)

    def test_incomplete_requests_expire_by_idle_tick(self):
        self.feed(5, 0, 2, b'{"id":5,')
        self.transport.ticks = bridge.SYSEX_PARTIAL_TTL_TICKS + 1
        self.transport._expire_partial()

        self.assertEqual(self.transport.partial, {})
        self.assertEqual(self.transport.partial_bytes, 0)

    def test_ready_and_outbox_queues_are_bounded(self):
        for mid in range(1, bridge.MAX_SYSEX_READY_MESSAGES + 5):
            body = json.dumps({"id": mid, "cmd": "ping", "args": {}}).encode(
                "ascii"
            )
            self.feed(mid, 0, 1, body)

        self.assertEqual(
            len(self.transport.ready), bridge.MAX_SYSEX_READY_MESSAGES
        )
        self.assertLessEqual(
            len(self.transport.outbox), bridge.MAX_SYSEX_OUTBOX_FRAMES
        )

        self.transport.outbox = [b"occupied"] * (
            bridge.MAX_SYSEX_OUTBOX_FRAMES - 1
        )
        before = len(self.transport.outbox)
        self.assertFalse(
            self.transport._send(bridge.TAG_RESPONSE, 1, ["one", "two"])
        )
        self.assertEqual(len(self.transport.outbox), before)

    def test_long_running_job_queue_is_bounded(self):
        class OneRequestTransport:
            def __init__(self):
                self.responses = []

            def poll(self):
                return [(99, {"id": 99, "cmd": "mixer.list", "args": {}})]

            def respond(self, handle, response):
                self.responses.append((handle, response))

            def flush(self):
                pass

        old_transport = bridge._transport
        old_jobs = list(bridge._jobs)
        transport = OneRequestTransport()
        bridge._transport = transport
        bridge._jobs[:] = [object()] * bridge.MAX_PENDING_JOBS
        try:
            bridge._pump()
        finally:
            bridge._transport = old_transport
            bridge._jobs[:] = old_jobs

        self.assertEqual(len(transport.responses), 1)
        self.assertFalse(transport.responses[0][1]["ok"])
        self.assertIn("queue is full", transport.responses[0][1]["error"])

    def test_oversized_response_becomes_a_small_error(self):
        original = bridge.MAX_SYSEX_RESPONSE_BYTES
        bridge.MAX_SYSEX_RESPONSE_BYTES = 32
        try:
            self.transport.respond(
                9, {"id": 9, "ok": True, "result": "X" * 64}
            )
        finally:
            bridge.MAX_SYSEX_RESPONSE_BYTES = original

        self.assertEqual(len(self.transport.outbox), 1)
        payload = self.transport.outbox[0][9:-1].decode("ascii")
        response = json.loads(payload)
        self.assertEqual(response["id"], 9)
        self.assertFalse(response["ok"])
        self.assertIn("size limit", response["error"])


class ClientFramingTests(unittest.TestCase):
    def setUp(self):
        self.transport = bridge_client._MidiTransport("unused", timeout=0)

    def drain(self, *messages: bytes) -> None:
        self.transport.midi_in = QueueMidiIn(*messages)
        self.transport._drain()

    def test_client_and_bridge_wire_limits_match(self):
        self.assertEqual(bridge_client.SYSEX_CHUNK, bridge.SYSEX_CHUNK)
        self.assertEqual(
            bridge_client.MAX_SYSEX_REQUEST_BYTES,
            bridge.MAX_SYSEX_REQUEST_BYTES,
        )
        self.assertEqual(
            bridge_client.MAX_SYSEX_REQUEST_PARTS,
            bridge.MAX_SYSEX_REQUEST_PARTS,
        )
        self.assertEqual(
            bridge_client.MAX_SYSEX_RESPONSE_BYTES,
            bridge.MAX_SYSEX_RESPONSE_BYTES,
        )
        self.assertEqual(
            bridge_client.MAX_SYSEX_RESPONSE_PARTS,
            bridge.MAX_SYSEX_RESPONSE_PARTS,
        )
        self.assertEqual(
            bridge_client.MAX_SYSEX_FRAME_BYTES,
            bridge.MAX_SYSEX_FRAME_BYTES,
        )

    def test_only_the_active_request_id_is_reassembled(self):
        self.transport._active_request_id = 7
        wanted = json.dumps({"id": 7, "ok": True, "result": {}}).encode("ascii")
        unwanted = json.dumps({"id": 8, "ok": True, "result": {}}).encode("ascii")
        cut = len(wanted) // 2

        self.drain(
            frame(bridge.TAG_RESPONSE, 8, 0, 1, unwanted),
            frame(bridge.TAG_RESPONSE, 7, 0, 2, wanted[:cut]),
            frame(bridge.TAG_RESPONSE, 7, 1, 2, wanted[cut:]),
        )

        self.assertEqual(self.transport.replies, {7: json.loads(wanted)})
        self.assertNotIn(8, self.transport.partial)
        self.assertEqual(self.transport.partial_bytes, 0)

    def test_client_rejects_oversized_invalid_and_inconsistent_frames(self):
        self.transport._active_request_id = 7
        invalid = (
            frame(
                bridge.TAG_RESPONSE,
                7,
                0,
                2,
                b"A" * (bridge_client.SYSEX_CHUNK + 1),
            ),
            frame(bridge.TAG_RESPONSE, 7, 0, 0, b"A"),
            frame(bridge.TAG_RESPONSE, 7, 2, 2, b"A"),
            frame(
                bridge.TAG_RESPONSE,
                7,
                0,
                bridge_client.MAX_SYSEX_RESPONSE_PARTS + 1,
                b"A",
            ),
        )
        self.drain(*invalid)
        self.assertEqual(self.transport.partial, {})
        self.assertEqual(self.transport.replies, {})

        self.drain(
            frame(bridge.TAG_RESPONSE, 7, 0, 2, b'{"id":7,'),
            frame(bridge.TAG_RESPONSE, 7, 1, 3, b'"ok":true}'),
        )
        self.assertEqual(self.transport.partial, {})
        self.assertEqual(self.transport.partial_bytes, 0)

    def test_client_partial_response_expires(self):
        self.transport._active_request_id = 7
        self.drain(frame(bridge.TAG_RESPONSE, 7, 0, 2, b'{"id":7,'))
        self.transport.partial[7]["updated"] -= (
            bridge_client.SYSEX_PARTIAL_TTL_SECONDS + 1
        )
        self.drain()

        self.assertEqual(self.transport.partial, {})
        self.assertEqual(self.transport.partial_bytes, 0)

    def test_only_a_small_valid_heartbeat_is_accepted_without_a_request(self):
        not_hello = json.dumps({"id": 0, "ok": True}).encode("ascii")
        hello = json.dumps({"hello": True, "protocol": 2}).encode("ascii")
        self.drain(
            frame(bridge.TAG_RESPONSE, 1, 0, 1, not_hello),
            frame(bridge.TAG_RESPONSE, 0, 0, 1, not_hello),
            frame(bridge.TAG_RESPONSE, 0, 0, 1, hello),
        )

        self.assertTrue(self.transport.hello_seen)
        self.assertEqual(self.transport.replies, {})
        self.assertEqual(self.transport.partial, {})

    def test_outgoing_request_size_is_bounded_before_any_frame_is_sent(self):
        sink = SinkMidiOut()
        self.transport.midi_out = sink
        self.transport._open = lambda: True

        with self.assertRaisesRegex(ValueError, "size limit"):
            self.transport.request(
                7, {"id": 7, "payload": "X" * bridge_client.MAX_SYSEX_REQUEST_BYTES}
            )
        self.assertEqual(sink.messages, [])
        self.assertIsNone(self.transport._active_request_id)

    def test_timeout_clears_the_expected_id_and_partial_state(self):
        sink = SinkMidiOut()
        self.transport.midi_out = sink
        self.transport._open = lambda: True

        with self.assertRaises(TimeoutError):
            self.transport.request(7, {"id": 7, "cmd": "ping", "args": {}})

        self.assertEqual(len(sink.messages), 1)
        self.assertIsNone(self.transport._active_request_id)
        self.assertEqual(self.transport.partial, {})
        self.assertEqual(self.transport.replies, {})
        self.assertEqual(self.transport.partial_bytes, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
