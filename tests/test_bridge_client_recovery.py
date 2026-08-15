"""Deterministic ownership and recovery tests for the local bridge client."""

from __future__ import annotations

import hashlib
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
    def test_enabled_midi_requires_an_explicit_windows_port_before_any_probe(self):
        with (
            mock.patch.object(bridge_client, "MIDI_ENABLED", True),
            mock.patch.object(
                bridge_client.subprocess,
                "run",
                side_effect=AssertionError("native MIDI probe spawned"),
            ) as probe,
        ):
            with self.assertRaises(BridgeError) as refused:
                BridgeClient(port=1, mailbox="/nonexistent", midi_port=None)
        self.assertIn("FL_BRIDGE_MIDI_PORT", str(refused.exception))
        probe.assert_not_called()

    def test_enabled_midi_accepts_a_configured_port_name_with_spaces(self):
        configured = "Configured Loopback With Spaces"
        with mock.patch.object(bridge_client, "MIDI_ENABLED", True):
            client = BridgeClient(
                port=1,
                mailbox="/nonexistent",
                midi_port=configured,
            )
        self.assertEqual(client.midi_port, configured)
        self.assertEqual(client._transports[-1].port_name, configured)

    def test_midi_ownership_acquire_is_idempotent_and_metadata_is_bounded(self):
        identity = "input=" + ("very-long-port-name-" * 1000)
        with tempfile.TemporaryDirectory(prefix="flmcp-lock-test-") as lock_dir:
            owner = _MidiPortOwnership(identity, lock_dir=lock_dir)
            owner.acquire()
            first_fd = owner._fd
            owner.acquire()

            self.assertEqual(owner._fd, first_fd)
            self.assertEqual(
                owner.evidence(),
                {
                    "port_identity": identity,
                    "lock_path": owner.path,
                    "owner_pid": os.getpid(),
                    "acquired": True,
                },
            )
            metadata = owner._owner_metadata(owner._fd)
            self.assertEqual(
                metadata["port_sha256"],
                hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
            )
            self.assertEqual(metadata["pid"], os.getpid())
            self.assertNotIn("port", metadata)
            self.assertLess(os.fstat(owner._fd).st_size, 256)

            owner.release()
            self.assertFalse(owner.evidence()["acquired"])

    def test_default_lock_namespace_is_stable_for_current_platform(self):
        identity = "input=Stable; output=Stable"
        if os.name == "nt":
            with (
                tempfile.TemporaryDirectory(prefix="flmcp-local-app-data-") as local,
                tempfile.TemporaryDirectory(prefix="flmcp-cwd-one-") as first_cwd,
                tempfile.TemporaryDirectory(prefix="flmcp-cwd-two-") as second_cwd,
                mock.patch.dict(os.environ, {"LOCALAPPDATA": local}, clear=False),
            ):
                original_cwd = os.getcwd()
                try:
                    os.chdir(first_cwd)
                    first = _MidiPortOwnership(identity)
                    os.chdir(second_cwd)
                    second = _MidiPortOwnership(identity)
                finally:
                    os.chdir(original_cwd)

                self.assertEqual(first.path, second.path)
                self.assertTrue(os.path.isabs(first.path))
                self.assertEqual(
                    os.path.commonpath([first.path, local]),
                    os.path.abspath(local),
                )
                self.assertIn(
                    os.path.join("Postfader", "midi-locks"), first.path
                )
                first.acquire()
                self.assertTrue(os.path.isdir(first.root))
                first.release()
        else:
            owner = _MidiPortOwnership(identity)
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            self.assertEqual(
                owner.path,
                "/tmp/fl-studio-mcp-iac-%s-%s.lock" % (os.getuid(), digest),
            )

    def test_posix_dispatch_retains_flock_acquire_and_release(self):
        fake_fcntl = types.SimpleNamespace(
            LOCK_EX=0x02,
            LOCK_NB=0x04,
            LOCK_UN=0x08,
            flock=mock.Mock(),
        )
        with tempfile.TemporaryDirectory(prefix="flmcp-posix-lock-test-") as lock_dir:
            owner = _MidiPortOwnership("input=POSIX; output=POSIX", lock_dir=lock_dir)
            # Exercise the POSIX dispatcher even in the Windows acceptance run.
            owner._windows = False
            owner._metadata_offset = 0
            with mock.patch.object(bridge_client, "fcntl", fake_fcntl):
                owner.acquire()
                locked_fd = owner._fd
                fake_fcntl.flock.assert_called_once_with(
                    locked_fd, fake_fcntl.LOCK_EX | fake_fcntl.LOCK_NB
                )
                owner.release()
                fake_fcntl.flock.assert_called_with(locked_fd, fake_fcntl.LOCK_UN)

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

    def test_midi_exact_match_takes_precedence_over_substring_matches(self):
        events = []

        class FakePort:
            def __init__(self, direction):
                self.direction = direction

            def get_ports(self):
                return ["Configured Port Extended", "cOnFiGuReD pOrT"]

            def open_port(self, index):
                events.append(("open", self.direction, index))

            def close_port(self):
                events.append(("close", self.direction))

            def ignore_types(self, **_kwargs):
                pass

        class FakeOwnership:
            def __init__(self, identity):
                self.identity = identity

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
            transport = _MidiTransport("Configured Port", timeout=1)
            self.assertTrue(transport._open())
            self.assertEqual(
                transport.resolved_input_name, "cOnFiGuReD pOrT"
            )
            self.assertEqual(
                transport.resolved_output_name, "cOnFiGuReD pOrT"
            )
            transport.close()

        self.assertIn(("open", "in", 1), events)
        self.assertIn(("open", "out", 1), events)

    def test_midi_unique_substring_match_remains_compatible(self):
        events = []

        class FakePort:
            def __init__(self, direction):
                self.direction = direction

            def get_ports(self):
                return ["Other", "Virtual IAC Driver Bus 1"]

            def open_port(self, index):
                events.append(("open", self.direction, index))

            def close_port(self):
                pass

            def ignore_types(self, **_kwargs):
                pass

        class FakeOwnership:
            def __init__(self, identity):
                self.identity = identity

            def acquire(self):
                pass

            def release(self):
                pass

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
            transport.close()

        self.assertEqual(events, [("open", "in", 1), ("open", "out", 1)])

    def test_midi_ambiguity_is_bounded_and_precedes_ownership_and_open(self):
        events = []
        candidates = ["Configured Port %d" % index for index in range(10)]

        class FakePort:
            def __init__(self, direction):
                self.direction = direction

            def get_ports(self):
                events.append(("get", self.direction))
                return candidates

            def open_port(self, index):
                events.append(("open", self.direction, index))

            def close_port(self):
                events.append(("close", self.direction))

        class UnexpectedOwnership:
            def __init__(self, _identity):
                events.append(("ownership",))

            def acquire(self):
                events.append(("acquire",))

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
                UnexpectedOwnership,
            ),
        ):
            transport = _MidiTransport("Configured Port", timeout=1)
            with self.assertRaises(BridgeError) as raised:
                transport._open()

        message = str(raised.exception)
        self.assertIn("ambiguous", message)
        self.assertIn("Configured Port 0", message)
        self.assertIn("Configured Port 7", message)
        self.assertNotIn("Configured Port 8", message)
        self.assertNotIn("Configured Port 9", message)
        self.assertIn("(+2 more)", message)
        self.assertFalse(
            any(event[0] in {"ownership", "acquire", "open"} for event in events),
            events,
        )

    def test_missing_midi_match_precedes_ownership_and_open(self):
        events = []

        class FakePort:
            def __init__(self, direction):
                self.direction = direction

            def get_ports(self):
                return ["Other Port"]

            def open_port(self, index):
                events.append(("open", self.direction, index))

            def close_port(self):
                pass

        class UnexpectedOwnership:
            def __init__(self, _identity):
                events.append(("ownership",))

            def acquire(self):
                events.append(("acquire",))

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
                UnexpectedOwnership,
            ),
        ):
            transport = _MidiTransport("Missing Port", timeout=1)
            with self.assertRaisesRegex(BridgeError, "matched no endpoint"):
                transport._open()

        self.assertFalse(
            any(event[0] in {"ownership", "acquire", "open"} for event in events),
            events,
        )

    def test_midi_ownership_failure_happens_before_either_endpoint_opens(self):
        events = []

        class FakePort:
            def __init__(self, direction):
                self.direction = direction

            def get_ports(self):
                return ["Configured Port"]

            def open_port(self, index):
                events.append(("open", self.direction, index))

            def close_port(self):
                events.append(("close", self.direction))

        class RefusingOwnership:
            def __init__(self, _identity):
                pass

            def acquire(self):
                events.append(("ownership-refused",))
                raise MidiTransportOwnedError("already owned")

            def release(self):
                events.append(("release",))

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
                RefusingOwnership,
            ),
        ):
            transport = _MidiTransport("Configured Port", timeout=1)
            with self.assertRaises(MidiTransportOwnedError):
                transport._open()

        self.assertIn(("ownership-refused",), events)
        self.assertFalse(any(event[0] == "open" for event in events), events)

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

    def test_midi_ownership_is_reclaimed_after_owner_process_exits(self):
        identity = "input=Exit Test; output=Exit Test"
        with tempfile.TemporaryDirectory(prefix="flmcp-lock-test-") as lock_dir:
            source = (
                "import sys; "
                "from fl_studio_mcp.bridge_client import _MidiPortOwnership; "
                "lock=_MidiPortOwnership(sys.argv[1], lock_dir=sys.argv[2]); "
                "lock.acquire(); "
                "print('READY:%d' % __import__('os').getpid(), flush=True); "
                "sys.stdin.buffer.read(1)"
            )
            child = subprocess.Popen(
                [sys.executable, "-B", "-c", source, identity, lock_dir],
                cwd=ROOT,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                ready = child.stdout.readline().strip()
                if not ready.startswith("READY:"):
                    child.stdin.close()
                    child.wait(timeout=5)
                    self.fail(child.stderr.read() or "owner child did not become ready")
                owner_pid = int(ready.split(":", 1)[1])

                contender = _MidiPortOwnership(identity, lock_dir=lock_dir)
                with self.assertRaises(MidiTransportOwnedError) as raised:
                    contender.acquire()
                # Some Windows Python launchers create the interpreter as a
                # child process, so Popen.pid need not be the lock owner's PID.
                self.assertEqual(raised.exception.owner_pid, owner_pid)

                # Do not call release in the child. A normal process exit must
                # close the descriptor and make the lock reclaimable.
                child.stdin.close()
                child.wait(timeout=5)
                self.assertEqual(child.returncode, 0, child.stderr.read())

                successor = _MidiPortOwnership(identity, lock_dir=lock_dir)
                successor.acquire()
                successor.release()
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

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
        request_ids = [request_id for request_id, _ in reloaded.request_calls]
        self.assertEqual(
            request_ids[1], (request_ids[0] % bridge_client.MAX_WIRE_ID) + 1
        )
        self.assertNotEqual(
            reloaded.request_calls[0][1]["request_token"],
            reloaded.request_calls[1][1]["request_token"],
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
                lambda rid, _payload: {
                    "id": rid,
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
