"""Deterministic ownership and recovery tests for the local bridge client."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fl_studio_mcp import bridge_client  # noqa: E402
from fl_studio_mcp.bridge_client import (  # noqa: E402
    BridgeClient,
    BridgeError,
    MidiTransportOwnedError,
    _MidiPortOwnership,
    _MidiTransport,
)


class ScriptedTransport:
    """Small transport double with explicit availability/request scripts."""

    def __init__(self, name="midi", available=None, requests=None):
        self.name = name
        self.available_script = list(available or [True])
        self.request_script = list(requests or [])
        self.available_calls = 0
        self.request_calls = []
        self.close_calls = 0

    def available(self):
        self.available_calls += 1
        if self.available_script:
            return self.available_script.pop(0)
        return True

    def request(self, rid, payload):
        self.request_calls.append((rid, payload))
        if not self.request_script:
            return {"id": rid, "ok": True, "result": {"rid": rid}}
        outcome = self.request_script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome(rid, payload) if callable(outcome) else outcome

    def close(self):
        self.close_calls += 1


def ok_result(**result):
    def response(rid, _payload):
        return {"id": rid, "ok": True, "result": dict(result)}

    return response


def client_with(*transports):
    client = BridgeClient(port=1, mailbox="/nonexistent", midi_port="none")
    client._transports = list(transports)
    client._active = None
    return client


class BridgeClientRecoveryTests(unittest.TestCase):
    def test_midi_transport_locks_resolved_ports_before_open_and_releases(self):
        events = []

        class FakePort:
            def __init__(self, direction):
                self.direction = direction

            def get_ports(self):
                return ["Other", "IAC Driver Bus 1"]

            def open_port(self, index):
                events.append(("open", self.direction, index))

            def close_port(self):
                events.append(("close", self.direction))

            def ignore_types(self, **_kwargs):
                pass

        class FakeOwnership:
            def __init__(self, identity):
                self.identity = identity
                events.append(("ownership", identity))

            def acquire(self):
                events.append(("acquire", self.identity))

            def release(self):
                events.append(("release", self.identity))

        fake_rtmidi = types.SimpleNamespace(
            MidiIn=lambda: FakePort("in"),
            MidiOut=lambda: FakePort("out"),
        )
        with (
            mock.patch.dict(sys.modules, {"rtmidi": fake_rtmidi}),
            mock.patch(
                "fl_studio_mcp.bridge_client._midi_preflight",
                return_value=True,
            ),
            mock.patch(
                "fl_studio_mcp.bridge_client._MidiPortOwnership",
                FakeOwnership,
            ),
        ):
            transport = _MidiTransport("IAC Driver", timeout=1)
            self.assertTrue(transport._open())
            transport.partial[7] = {0: "stale"}
            transport.replies[7] = {"stale": True}
            transport.close()

        identity = (
            "input=IAC Driver Bus 1; output=IAC Driver Bus 1"
        )
        self.assertLess(
            events.index(("acquire", identity)),
            events.index(("open", "in", 1)),
        )
        self.assertIn(("release", identity), events)
        self.assertEqual(transport.partial, {})
        self.assertEqual(transport.replies, {})

    def test_physical_iac_ownership_is_exclusive_across_processes(self):
        identity = "input=IAC Driver Bus Test; output=IAC Driver Bus Test"
        with tempfile.TemporaryDirectory(prefix="flmcp-lock-test-") as lock_dir:
            owner = _MidiPortOwnership(identity, lock_dir=lock_dir)
            owner.acquire()
            source = (
                "import sys; "
                "from fl_studio_mcp.bridge_client import _MidiPortOwnership; "
                "lock=_MidiPortOwnership(sys.argv[1], lock_dir=sys.argv[2]); "
                "lock.acquire()"
            )
            probe = subprocess.run(
                [sys.executable, "-c", source, identity, lock_dir],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertNotEqual(probe.returncode, 0)
            self.assertIn("already owned by another live", probe.stderr)
            self.assertIn("owner pid %d" % os.getpid(), probe.stderr)

            owner.release()
            successor = _MidiPortOwnership(identity, lock_dir=lock_dir)
            successor.acquire()
            successor.release()

    def test_restart_reselects_a_different_transport_for_read(self):
        stopped = ScriptedTransport(
            name="tcp",
            available=[False],
            requests=[OSError("old FL socket closed")],
        )
        restarted = ScriptedTransport(
            name="midi",
            available=[True],
            requests=[ok_result(project="restarted")],
        )
        client = client_with(stopped, restarted)
        client._active = stopped
        with mock.patch(
            "fl_studio_mcp.bridge_client.RECONNECT_DELAY_SECONDS", 0
        ):
            result = client.call("project.info")

        self.assertEqual(result, {"project": "restarted"})
        self.assertEqual(stopped.close_calls, 1)
        self.assertEqual(len(stopped.request_calls), 1)
        self.assertEqual(restarted.available_calls, 1)
        self.assertEqual(len(restarted.request_calls), 1)
        self.assertEqual(client.transport, "midi")

    def test_cold_restart_gets_one_bounded_reselection_window(self):
        restarting = ScriptedTransport(
            available=[False, True],
            requests=[ok_result(state="back")],
        )
        client = client_with(restarting)
        with mock.patch(
            "fl_studio_mcp.bridge_client.RECONNECT_DELAY_SECONDS", 0
        ):
            result = client.call("ping")

        self.assertEqual(result, {"state": "back"})
        self.assertEqual(restarting.available_calls, 2)
        self.assertEqual(len(restarting.request_calls), 1)

    def test_script_reload_reopens_same_transport_and_uses_a_new_id(self):
        reloaded = ScriptedTransport(
            requests=[
                TimeoutError("script reloading"),
                ok_result(state="ready"),
            ],
        )
        client = client_with(reloaded)
        client._active = reloaded
        with mock.patch(
            "fl_studio_mcp.bridge_client.RECONNECT_DELAY_SECONDS", 0
        ):
            result = client.call("mixer.track", track=4)

        self.assertEqual(result, {"state": "ready"})
        self.assertEqual(reloaded.close_calls, 1)
        self.assertEqual(reloaded.available_calls, 1)
        self.assertEqual(
            [request_id for request_id, _ in reloaded.request_calls], [1, 2]
        )
        self.assertTrue(
            all(
                payload["cmd"] == "mixer.track"
                and payload["args"] == {"track": 4}
                for _, payload in reloaded.request_calls
            )
        )

    def test_persistent_read_timeout_has_exactly_one_bounded_replay(self):
        transport = ScriptedTransport(
            requests=[TimeoutError("first"), TimeoutError("second")]
        )
        client = client_with(transport)
        client._active = transport
        with (
            mock.patch(
                "fl_studio_mcp.bridge_client.RECONNECT_DELAY_SECONDS", 0
            ),
            self.assertRaises(BridgeError) as raised,
        ):
            client.call("plugin.params", track=5, slot=0)

        self.assertIn("after 2 bounded attempts", str(raised.exception))
        self.assertEqual(len(transport.request_calls), 2)
        self.assertEqual(transport.close_calls, 2)
        self.assertEqual(client.transport, "none")

    def test_step_sequence_read_is_replay_safe(self):
        self.assertIn("sequencer.get", bridge_client.IDEMPOTENT_READ_COMMANDS)
        transport = ScriptedTransport(
            requests=[TimeoutError("response lost"), ok_result(cells=[False] * 16)]
        )
        client = client_with(transport)
        client._active = transport
        result = client.call("sequencer.get", pattern=1, channel=0)
        self.assertEqual(result["cells"], [False] * 16)
        self.assertEqual(len(transport.request_calls), 2)

    def test_non_read_timeout_is_never_replayed(self):
        transport = ScriptedTransport(
            requests=[TimeoutError("response lost"), ok_result(applied=True)]
        )
        client = client_with(transport)
        client._active = transport
        with self.assertRaises(BridgeError) as raised:
            client.call("mixer.set_volume", track=3, value=0.5)

        message = str(raised.exception)
        self.assertIn("was not replayed", message)
        self.assertIn("outcome may be unknown", message)
        self.assertEqual(len(transport.request_calls), 1)
        self.assertEqual(transport.close_calls, 1)

    def test_verified_write_commands_are_never_replayed(self):
        # A lost response to a write has an unknown outcome: FL may or may not
        # have applied it. None of the ten verified write commands may appear
        # in the replay allowlist, and none may be retried automatically.
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
            "transport.set_playing",
            "transport.stop",
            "transport.set_song_position",
            "transport.set_loop_mode",
            "transport.set_tempo",
            "channel.set_mix",
            "channel.set_identity",
            "channel.route_to_mixer",
            "sequencer.set",
            "channel.trigger_note",
        ):
            with self.subTest(command=command):
                self.assertNotIn(command, bridge_client.IDEMPOTENT_READ_COMMANDS)
                transport = ScriptedTransport(
                    requests=[
                        TimeoutError("response lost"),
                        ok_result(verified=True),
                    ]
                )
                client = client_with(transport)
                client._active = transport
                with self.assertRaises(BridgeError) as raised:
                    client.call(command, track=3, value=0.5)
                self.assertIn("was not replayed", str(raised.exception))
                self.assertEqual(len(transport.request_calls), 1)

    def test_remote_rejection_is_not_a_reconnect_signal(self):
        transport = ScriptedTransport(
            requests=[
                {
                    "id": 1,
                    "ok": False,
                    "error": "bridge is locked read-only",
                }
            ]
        )
        client = client_with(transport)
        client._active = transport
        with self.assertRaises(BridgeError) as raised:
            client.call("mixer.set_volume", track=3, value=0.5)

        self.assertIn("locked read-only", str(raised.exception))
        self.assertEqual(len(transport.request_calls), 1)
        self.assertEqual(transport.close_calls, 0)

    def test_midi_ownership_collision_remains_a_clear_terminal_error(self):
        class OwnedTransport(ScriptedTransport):
            def available(self):
                raise MidiTransportOwnedError("IAC already owned; owner pid 42")

        client = client_with(OwnedTransport())
        with self.assertRaises(MidiTransportOwnedError) as raised:
            client.call("ping")

        self.assertIn("owner pid 42", str(raised.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
