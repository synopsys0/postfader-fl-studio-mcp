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
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "fakefl"))
sys.path.insert(0, os.path.join(ROOT, "fl_studio_mcp", "_bridge"))
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

    def test_shared_dispatch_rejects_non_object_request_and_args(self):
        for request in (
            [],
            {"id": 8, "cmd": "ping", "args": []},
        ):
            with self.subTest(request=request):
                response = bridge._dispatch(request)
                self.assertFalse(response["ok"])
                self.assertIn("JSON object", response["error"])

        healthy = bridge._dispatch({"id": 9, "cmd": "ping", "args": {}})
        self.assertTrue(healthy["ok"])
        self.assertTrue(healthy["result"]["pong"])

    def test_bridge_advertises_a_separate_midi_wire_protocol(self):
        hello = json.loads(self.transport._hello())
        ping = bridge.cmd_ping({})

        self.assertEqual(hello["protocol"], bridge.PROTOCOL_VERSION)
        self.assertEqual(ping["protocol"], bridge.PROTOCOL_VERSION)
        self.assertEqual(
            hello[bridge.MIDI_WIRE_PROTOCOL_FIELD],
            bridge.MIDI_WIRE_PROTOCOL_VERSION,
        )
        self.assertEqual(
            ping[bridge.MIDI_WIRE_PROTOCOL_FIELD],
            bridge.MIDI_WIRE_PROTOCOL_VERSION,
        )
        self.assertEqual(bridge.PROTOCOL_VERSION, 2)

    def test_legacy_client_request_frames_are_accepted_by_current_bridge(self):
        request = {
            "id": 11,
            "cmd": "ping",
            "args": {"legacy_padding": "X" * 1100},
        }
        body = json.dumps(request).encode("ascii")
        legacy_chunk = bridge.MIDI_WIRE_SYSEX_CHUNKS[
            bridge.LEGACY_MIDI_WIRE_PROTOCOL_VERSION
        ]
        chunks = [
            body[index:index + legacy_chunk]
            for index in range(0, len(body), legacy_chunk)
        ]
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len(chunks[0]), legacy_chunk)

        for index, chunk in enumerate(chunks):
            self.transport.feed(
                frame(
                    bridge.TAG_REQUEST,
                    request["id"],
                    index,
                    len(chunks),
                    chunk,
                )
            )

        self.assertEqual(self.transport.ready, [(request["id"], request)])
        response = bridge._dispatch(request)
        self.assertTrue(response["ok"])
        self.assertNotIn("client_session", response)
        self.assertNotIn("request_token", response)

    def test_oversized_and_invalid_sequence_frames_are_dropped(self):
        invalid = (
            frame(
                bridge.TAG_REQUEST,
                1,
                0,
                2,
                b"A" * (bridge.MAX_SYSEX_INPUT_CHUNK + 1),
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
        session = "a" * 32
        token = session + "-1"
        transport = OneRequestTransport()
        transport.poll = lambda: [(
            99,
            {
                "id": 99,
                "cmd": "mixer.list",
                "args": {},
                "client_session": session,
                "request_token": token,
            },
        )]
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
        self.assertEqual(transport.responses[0][1]["client_session"], session)
        self.assertEqual(transport.responses[0][1]["request_token"], token)

    def test_correlated_oversized_response_becomes_an_accepted_small_error(self):
        original = bridge.MAX_SYSEX_RESPONSE_WIRE_BYTES
        bridge.MAX_SYSEX_RESPONSE_WIRE_BYTES = 32
        session = "a" * 32
        token = session + "-1"
        try:
            self.transport.respond(
                9,
                {
                    "id": 9,
                    "client_session": session,
                    "request_token": token,
                    "ok": True,
                    "result": "X" * 64,
                },
            )
        finally:
            bridge.MAX_SYSEX_RESPONSE_WIRE_BYTES = original

        self.assertEqual(len(self.transport.outbox), 1)
        payload = self.transport.outbox[0][9:-1].decode("ascii")
        response = json.loads(payload)
        self.assertEqual(response["id"], 9)
        self.assertFalse(response["ok"])
        self.assertIn("size limit", response["error"])
        self.assertEqual(response["client_session"], session)
        self.assertEqual(response["request_token"], token)

        receiver = bridge_client._MidiTransport("unused", timeout=0)
        receiver._active_request_id = 9
        receiver._active_client_session = session
        receiver.midi_in = QueueMidiIn(*self.transport.outbox)
        receiver._drain()
        self.assertEqual(receiver.replies, {9: response})

    def test_v012_receiver_ceiling_gets_a_legacy_readable_error(self):
        self.assertEqual(bridge.LEGACY_MIDI_RESPONSE_PART_CEILING, 4096)
        self.assertEqual(
            bridge.MAX_SYSEX_RESPONSE_PARTS,
            bridge.LEGACY_MIDI_RESPONSE_PART_CEILING,
        )
        response = {"id": 12, "ok": True, "result": {"blob": ""}}
        empty_size = len(json.dumps(response))
        response["result"]["blob"] = "X" * (
            bridge.MAX_SYSEX_RESPONSE_WIRE_BYTES - empty_size
        )
        self.assertEqual(
            len(json.dumps(response)), bridge.MAX_SYSEX_RESPONSE_WIRE_BYTES
        )

        self.transport.respond(12, response)

        self.assertEqual(
            len(self.transport.outbox),
            bridge.LEGACY_MIDI_RESPONSE_PART_CEILING,
        )
        self.assertTrue(all(
            len(message)
            <= bridge.MIDI_WIRE_FRAME_BYTES[
                bridge.LEGACY_MIDI_WIRE_PROTOCOL_VERSION
            ]
            for message in self.transport.outbox
        ))
        self.assertTrue(all(
            (((message[7] << 7) | message[8])
             == bridge.LEGACY_MIDI_RESPONSE_PART_CEILING)
            for message in self.transport.outbox
        ))
        reassembled = b"".join(
            message[9:-1] for message in self.transport.outbox
        ).decode("ascii")
        self.assertEqual(json.loads(reassembled), response)

        # One byte beyond the shared part ceiling becomes a one-frame error
        # that a v0.12 client can still reassemble. Legacy requests have no
        # correlation fields, so the fallback must remain uncorrelated too.
        response["result"]["blob"] += "X"
        self.transport.outbox = []
        self.transport.respond(12, response)
        self.assertEqual(len(self.transport.outbox), 1)
        fallback = json.loads(
            self.transport.outbox[0][9:-1].decode("ascii")
        )
        self.assertEqual(fallback["id"], 12)
        self.assertFalse(fallback["ok"])
        self.assertIn("v0.12 receivers", fallback["error"])
        self.assertNotIn("client_session", fallback)
        self.assertNotIn("request_token", fallback)

    def test_midi_ready_queue_full_error_retains_correlation(self):
        session = "b" * 32
        token = session + "-1"
        self.transport.ready = [
            (index + 1, {"id": index + 1})
            for index in range(bridge.MAX_SYSEX_READY_MESSAGES)
        ]
        request = {
            "id": 13,
            "cmd": "ping",
            "args": {},
            "client_session": session,
            "request_token": token,
        }
        body = json.dumps(request).encode("ascii")
        self.feed(13, 0, 1, body)

        self.assertEqual(len(self.transport.outbox), 1)
        receiver = bridge_client._MidiTransport("unused", timeout=0)
        receiver._active_request_id = 13
        receiver._active_client_session = session
        receiver.midi_in = QueueMidiIn(*self.transport.outbox)
        receiver._drain()
        response = receiver.replies[13]
        self.assertFalse(response["ok"])
        self.assertIn("queue is full", response["error"])
        self.assertEqual(response["client_session"], session)
        self.assertEqual(response["request_token"], token)

    def test_new_correlated_midi_request_cancels_abandoned_job_and_frames(self):
        closed = []

        def abandoned_work():
            try:
                yield
            finally:
                closed.append(True)

        generator = abandoned_work()
        next(generator)
        old_transport = bridge._transport
        old_jobs = list(bridge._jobs)
        transport = bridge._MidiTransport()
        transport.outbox = [b"abandoned-response"]
        session = "a" * 32
        transport.ready = [
            (
                7,
                {
                    "id": 7,
                    "cmd": "ping",
                    "args": {},
                    "client_session": session,
                    "request_token": session + "-1",
                },
            )
        ]
        transport.flush = lambda: None
        bridge._transport = transport
        bridge._jobs[:] = [
            bridge._Job(
                7,
                7,
                generator,
                "mixer.list",
                "b" * 32,
                "b" * 32 + "-1",
            )
        ]
        try:
            bridge._pump()
        finally:
            bridge._transport = old_transport
            bridge._jobs[:] = old_jobs

        self.assertEqual(closed, [True])
        self.assertNotIn(b"abandoned-response", transport.outbox)
        self.assertEqual(len(transport.outbox), 1)
        response = json.loads(transport.outbox[0][9:-1].decode("ascii"))
        self.assertTrue(response["ok"])
        self.assertEqual(response["client_session"], session)
        self.assertEqual(response["request_token"], session + "-1")

    def test_windows_pacing_sends_one_sysex_frame_per_flush(self):
        sent = []
        old_limit = bridge.MAX_SYSEX_PER_TICK
        old_sender = bridge.device.midiOutSysex
        bridge.MAX_SYSEX_PER_TICK = 1
        bridge.device.midiOutSysex = sent.append
        self.transport.outbox = [b"one", b"two", b"three"]
        try:
            self.transport.flush()
        finally:
            bridge.MAX_SYSEX_PER_TICK = old_limit
            bridge.device.midiOutSysex = old_sender

        self.assertEqual(sent, [b"one"])
        self.assertEqual(self.transport.outbox, [b"two", b"three"])


class ClientFramingTests(unittest.TestCase):
    def setUp(self):
        self.transport = bridge_client._MidiTransport("unused", timeout=0)

    def drain(self, *messages: bytes) -> None:
        self.transport.midi_in = QueueMidiIn(*messages)
        self.transport._drain()

    def legacy_mismatch(self, family):
        transport = bridge_client._MidiTransport("unused", timeout=0)
        sink = SinkMidiOut()
        legacy_hello = json.dumps(
            {"hello": True, "protocol": 2, "transport": "midi"}
        ).encode("ascii")
        transport.midi_in = QueueMidiIn(
            frame(bridge.TAG_RESPONSE, 0, 0, 1, legacy_hello)
        )
        transport.midi_out = sink
        transport._open = lambda: True
        client = bridge_client.BridgeClient(
            port=1,
            mailbox="/nonexistent",
            midi_port="unused",
        )
        client._transports = [transport]

        with (
            mock.patch.object(
                bridge_client, "platform_family", return_value=family
            ),
            self.assertRaises(
                bridge_client.MidiWireProtocolMismatchError
            ) as raised,
        ):
            client.call("ping")
        return raised.exception, sink, transport

    def test_client_and_bridge_wire_limits_match(self):
        self.assertEqual(
            bridge_client.MIDI_WIRE_PROTOCOL_VERSION,
            bridge.MIDI_WIRE_PROTOCOL_VERSION,
        )
        self.assertEqual(
            bridge_client.MIDI_WIRE_SYSEX_CHUNKS,
            bridge.MIDI_WIRE_SYSEX_CHUNKS,
        )
        self.assertEqual(
            bridge_client.MIDI_WIRE_FRAME_BYTES,
            bridge.MIDI_WIRE_FRAME_BYTES,
        )
        self.assertEqual(bridge_client.SYSEX_CHUNK, bridge.SYSEX_CHUNK)
        self.assertEqual(
            bridge_client.WINMM_SYSEX_BUFFER_BYTES,
            bridge.WINMM_SYSEX_BUFFER_BYTES,
        )
        self.assertEqual(
            bridge_client.SYSEX_FRAME_OVERHEAD_BYTES,
            bridge.SYSEX_FRAME_OVERHEAD_BYTES,
        )
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
            bridge_client.LEGACY_MIDI_RESPONSE_PART_CEILING,
            bridge.LEGACY_MIDI_RESPONSE_PART_CEILING,
        )
        self.assertEqual(
            bridge_client.MAX_SYSEX_RESPONSE_WIRE_BYTES,
            bridge.MAX_SYSEX_RESPONSE_WIRE_BYTES,
        )
        self.assertLessEqual(
            bridge_client.MAX_SYSEX_RESPONSE_PARTS,
            bridge_client.LEGACY_MIDI_RESPONSE_PART_CEILING,
        )
        self.assertEqual(
            bridge_client.MAX_SYSEX_FRAME_BYTES,
            bridge.MAX_SYSEX_FRAME_BYTES,
        )
        self.assertLessEqual(
            bridge_client.MAX_SYSEX_FRAME_BYTES,
            bridge_client.WINMM_SYSEX_BUFFER_BYTES,
        )
        self.assertEqual(
            bridge.MAX_SYSEX_INPUT_CHUNK,
            bridge.MIDI_WIRE_SYSEX_CHUNKS[
                bridge.LEGACY_MIDI_WIRE_PROTOCOL_VERSION
            ],
        )
        self.assertEqual(
            bridge.MAX_SYSEX_INPUT_FRAME_BYTES,
            bridge.MIDI_WIRE_FRAME_BYTES[
                bridge.LEGACY_MIDI_WIRE_PROTOCOL_VERSION
            ],
        )

    def test_current_heartbeat_negotiates_before_current_requests(self):
        sink = SinkMidiOut()
        hello = bridge._MidiTransport()._hello().encode("ascii")
        self.transport.midi_in = QueueMidiIn(
            frame(bridge.TAG_RESPONSE, 0, 0, 1, hello)
        )
        self.transport.midi_out = sink
        self.transport._open = lambda: True

        self.assertTrue(self.transport.available())
        self.assertEqual(
            self.transport.midi_wire_protocol_version,
            bridge_client.MIDI_WIRE_PROTOCOL_VERSION,
        )
        self.assertEqual(
            self.transport.bridge_protocol_version,
            bridge.PROTOCOL_VERSION,
        )
        self.assertEqual(sink.messages, [])

    def test_v012_heartbeat_fails_before_any_request_with_upgrade_path(self):
        error, sink, transport = self.legacy_mismatch("windows")
        self.assertEqual(error.bridge_protocol_version, 2)
        self.assertEqual(
            error.detected_wire_protocol_version,
            bridge_client.LEGACY_MIDI_WIRE_PROTOCOL_VERSION,
        )
        self.assertEqual(
            error.expected_wire_protocol_version,
            bridge_client.MIDI_WIRE_PROTOCOL_VERSION,
        )
        self.assertTrue(
            error.bridge_target_path.endswith(
                os.path.join(
                    "Settings",
                    "Hardware",
                    "Universal Bridge",
                    "device_UniversalBridge.py",
                )
            )
        )
        message = str(error)
        self.assertIn("No command was sent", message)
        self.assertIn("response correlation", message)
        self.assertIn("1,034-byte SysEx", message)
        self.assertIn("WinMM", message)
        self.assertNotIn("CoreMIDI", message)
        self.assertIn("postfader-install-bridge", message)
        self.assertIn("Expected bridge target:", message)
        self.assertIn("View > Script output", message)
        self.assertIn("Reload script", message)
        self.assertEqual(sink.messages, [])
        self.assertIsNone(transport.midi_in)
        self.assertIsNone(transport.midi_out)

    def test_v012_macos_mismatch_names_correlation_not_winmm(self):
        error, sink, transport = self.legacy_mismatch("macos")
        message = str(error)

        self.assertIn("No command was sent", message)
        self.assertIn("response correlation", message)
        self.assertIn("CoreMIDI", message)
        self.assertIn("automatic downgrade unsafe", message)
        self.assertNotIn("WinMM", message)
        self.assertIn("postfader-install-bridge", message)
        self.assertIn("Reload script", message)
        self.assertEqual(sink.messages, [])
        self.assertIsNone(transport.midi_in)
        self.assertIsNone(transport.midi_out)

    def test_current_client_accepts_current_correlated_bridge_response(self):
        client = bridge_client.BridgeClient(
            port=1,
            mailbox="/nonexistent",
            midi_port="unused",
        )
        transport = bridge_client._MidiTransport("unused", timeout=1)
        observed = {}
        hello = bridge._MidiTransport()._hello().encode("ascii")
        transport.midi_in = QueueMidiIn(
            frame(bridge.TAG_RESPONSE, 0, 0, 1, hello)
        )
        transport.midi_out = SinkMidiOut()
        transport._open = lambda: True

        def current_response(rid, payload):
            observed.update(payload)
            return {
                "id": rid,
                "client_session": payload["client_session"],
                "request_token": payload["request_token"],
                "ok": True,
                "result": {
                    "pong": True,
                    bridge_client.MIDI_WIRE_PROTOCOL_FIELD:
                        bridge_client.MIDI_WIRE_PROTOCOL_VERSION,
                },
            }

        transport.request = current_response
        client._transports = [transport]

        result = client.call("ping")

        self.assertTrue(result["pong"])
        self.assertEqual(
            transport.midi_wire_protocol_version,
            bridge_client.MIDI_WIRE_PROTOCOL_VERSION,
        )
        self.assertEqual(
            result[bridge_client.MIDI_WIRE_PROTOCOL_FIELD],
            bridge_client.MIDI_WIRE_PROTOCOL_VERSION,
        )
        self.assertEqual(len(observed["client_session"]), 32)
        self.assertTrue(
            observed["request_token"].startswith(
                observed["client_session"] + "-"
            )
        )

    def test_multiframe_response_survives_winmm_default_buffer(self):
        session = "a" * 32
        response = {
            "id": 7,
            "client_session": session,
            "request_token": session + "-1",
            "ok": True,
            "result": {"blob": "X" * (bridge.SYSEX_CHUNK * 2)},
        }
        sender = bridge._MidiTransport()
        sender.respond(7, response)

        self.assertGreater(len(sender.outbox), 1)
        self.assertTrue(
            all(
                len(message) <= bridge.WINMM_SYSEX_BUFFER_BYTES
                for message in sender.outbox
            )
        )

        # Model WinMM's fixed input buffers: an oversized message disappears
        # before Python can drain it. Every corrected frame must survive.
        received = [
            message
            for message in sender.outbox
            if len(message) <= bridge.WINMM_SYSEX_BUFFER_BYTES
        ]
        self.transport._active_request_id = 7
        self.transport._active_client_session = session
        self.drain(*received)
        self.assertEqual(self.transport.replies, {7: response})

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

    def test_same_wire_id_from_abandoned_process_is_rejected_by_session(self):
        self.transport._active_request_id = 7
        self.transport._active_client_session = "b" * 32
        stale = json.dumps(
            {
                "id": 7,
                "client_session": "a" * 32,
                "request_token": "a" * 32 + "-1",
                "ok": True,
                "result": {"plugins": [{}]},
            }
        ).encode("ascii")
        wanted = json.dumps(
            {
                "id": 7,
                "client_session": "b" * 32,
                "request_token": "b" * 32 + "-1",
                "ok": True,
                "result": {"plugins": [{"slot": 2}]},
            }
        ).encode("ascii")

        self.drain(
            frame(bridge.TAG_RESPONSE, 7, 0, 1, stale),
            frame(bridge.TAG_RESPONSE, 7, 0, 1, wanted),
        )

        self.assertEqual(self.transport.replies, {7: json.loads(wanted)})

    def test_midi_input_queue_covers_the_bounded_response(self):
        self.assertGreaterEqual(
            bridge_client.MIDI_INPUT_QUEUE_SIZE,
            bridge_client.MAX_SYSEX_RESPONSE_PARTS
            + bridge_client.MAX_SYSEX_HELLO_PARTS,
        )

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
        self.transport.midi_in = QueueMidiIn()
        self.transport.midi_out = sink
        self.transport._open = lambda: True

        with self.assertRaises(TimeoutError):
            self.transport.request(7, {"id": 7, "cmd": "ping", "args": {}})

        self.assertEqual(len(sink.messages), 1)
        self.assertIsNone(self.transport._active_request_id)
        self.assertEqual(self.transport.partial, {})
        self.assertEqual(self.transport.replies, {})
        self.assertEqual(self.transport.partial_bytes, 0)

    @staticmethod
    def _scheduled_response_transport(timeout, schedule):
        clock = {"now": 0.0}

        class ScheduledMidiIn:
            def __init__(self, values):
                self.values = list(values)

            def get_message(self):
                if self.values and self.values[0][0] <= clock["now"]:
                    _when, message = self.values.pop(0)
                    return list(message), 0.0
                return None

        transport = bridge_client._MidiTransport("unused", timeout=timeout)
        transport.midi_in = ScheduledMidiIn(schedule)
        transport.midi_out = SinkMidiOut()
        transport._open = lambda: True
        return transport, clock

    @staticmethod
    def _advance_fake_clock(clock):
        def advance(_seconds):
            clock["now"] += 0.1

        return advance

    def test_valid_frame_progress_extends_idle_timeout(self):
        session = "b" * 32
        request = {
            "id": 7,
            "cmd": "mixer.list",
            "args": {},
            "client_session": session,
            "request_token": session + "-1",
        }
        response = {
            "id": 7,
            "client_session": session,
            "request_token": session + "-1",
            "ok": True,
            "result": {"blob": "Y" * (bridge_client.SYSEX_CHUNK * 3)},
        }
        body = json.dumps(response).encode("ascii")
        chunks = [
            body[index:index + bridge_client.SYSEX_CHUNK]
            for index in range(0, len(body), bridge_client.SYSEX_CHUNK)
        ]
        schedule = [
            (
                0.8 * (index + 1),
                frame(
                    bridge.TAG_RESPONSE,
                    7,
                    index,
                    len(chunks),
                    chunk,
                ),
            )
            for index, chunk in enumerate(chunks)
        ]
        transport, clock = self._scheduled_response_transport(1.0, schedule)

        with (
            mock.patch.object(
                bridge_client.time, "monotonic", side_effect=lambda: clock["now"]
            ),
            mock.patch.object(
                bridge_client.time,
                "sleep",
                side_effect=self._advance_fake_clock(clock),
            ),
        ):
            result = transport.request(7, request)

        self.assertGreater(clock["now"], transport.timeout)
        self.assertEqual(result, response)

    def test_progress_then_stall_uses_idle_timeout_and_cleans_partial(self):
        first = frame(bridge.TAG_RESPONSE, 7, 0, 2, b'{"id":7,')
        transport, clock = self._scheduled_response_transport(
            1.0, [(0.8, first)]
        )

        with (
            mock.patch.object(
                bridge_client.time, "monotonic", side_effect=lambda: clock["now"]
            ),
            mock.patch.object(
                bridge_client.time,
                "sleep",
                side_effect=self._advance_fake_clock(clock),
            ),
            self.assertRaisesRegex(TimeoutError, "no MIDI reply progress"),
        ):
            transport.request(7, {"id": 7, "cmd": "mixer.list", "args": {}})

        self.assertGreaterEqual(clock["now"], 1.8)
        self.assertLess(clock["now"], 5.0)
        self.assertEqual(transport.partial, {})
        self.assertEqual(transport.partial_bytes, 0)

    def test_continuous_progress_still_has_a_hard_deadline(self):
        schedule = [
            (
                0.8 * (index + 1),
                frame(bridge.TAG_RESPONSE, 7, index, 10, b"x"),
            )
            for index in range(9)
        ]
        transport, clock = self._scheduled_response_transport(1.0, schedule)

        with (
            mock.patch.object(
                bridge_client.time, "monotonic", side_effect=lambda: clock["now"]
            ),
            mock.patch.object(
                bridge_client.time,
                "sleep",
                side_effect=self._advance_fake_clock(clock),
            ),
            self.assertRaisesRegex(TimeoutError, "hard deadline"),
        ):
            transport.request(7, {"id": 7, "cmd": "mixer.list", "args": {}})

        self.assertGreaterEqual(clock["now"], 5.0)
        self.assertEqual(transport.partial, {})
        self.assertEqual(transport.partial_bytes, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
