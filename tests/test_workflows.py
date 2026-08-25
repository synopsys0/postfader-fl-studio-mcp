"""Hermetic tests for one-preflight verified workflow batches."""

from __future__ import annotations

import copy
import unittest
from typing import Any
from unittest import mock

from fl_studio_mcp.bridge_client import BridgeError
from fl_studio_mcp.bridge_install import expected_bridge_deployment
from fl_studio_mcp.workflows import VerifiedBatchExecutor


SESSION = "a" * 32


def compatible_ping() -> dict[str, Any]:
    return {
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


class ScriptedClient:
    transport = "tcp"

    def __init__(self, handler: Any):
        self.handler = handler
        self.ping_count = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def ping(self) -> dict[str, Any]:
        self.ping_count += 1
        return compatible_ping()

    def call(self, command: str, **arguments: Any) -> dict[str, Any]:
        self.calls.append((command, copy.deepcopy(arguments)))
        return self.handler(command, arguments, len(self.calls) - 1)


def mixer_handler(
    command: str, arguments: dict[str, Any], _index: int
) -> dict[str, Any]:
    common = {
        "command": command,
        "track": arguments["track"],
        "session_fingerprint": SESSION,
        "session_precondition_applied": True,
        "expected_before_applied": "expected_before" in arguments,
        "undo_point_created": True,
        "verified": True,
    }
    if command == "mixer.set_volume":
        common.update(
            before=0.8,
            after=arguments["value"],
            before_db=-1.9,
            after_db=-3.2,
        )
    elif command == "mixer.set_pan":
        common.update(before=0.0, after=arguments["value"])
    else:  # pragma: no cover - test setup must stay closed
        raise AssertionError(command)
    return common


class VerifiedBatchTests(unittest.TestCase):
    def test_one_live_ping_serves_every_item_and_session_is_internal(self) -> None:
        client = ScriptedClient(mixer_handler)
        operations = [
            {
                "operation_id": "gain",
                "operation": "mixer_volume",
                "track_index": 3,
                "volume_normalized": 0.7,
            },
            {
                "operation_id": "pan",
                "operation": "mixer_pan",
                "track_index": 3,
                "pan": -0.25,
            },
        ]
        with mock.patch("fl_studio_mcp.workflows.get_client", return_value=client):
            result = VerifiedBatchExecutor().apply(operations=operations)

        self.assertEqual(client.ping_count, 1)
        self.assertEqual([command for command, _ in client.calls], [
            "mixer.set_volume",
            "mixer.set_pan",
        ])
        self.assertTrue(all(args["session_fingerprint"] == SESSION for _, args in client.calls))
        self.assertTrue(result.completed)
        self.assertTrue(result.verified)
        self.assertEqual([item.status for item in result.results], ["verified", "verified"])
        self.assertTrue(all(item.receipt.session_precondition_applied for item in result.results))

    def test_duplicate_field_fails_preflight_before_the_handshake(self) -> None:
        client = ScriptedClient(mixer_handler)
        operations = [
            {
                "operation_id": "first",
                "operation": "mixer_volume",
                "track_index": 3,
                "volume_normalized": 0.7,
            },
            {
                "operation_id": "second",
                "operation": "mixer_volume",
                "track_index": 3,
                "volume_normalized": 0.6,
            },
        ]
        with mock.patch("fl_studio_mcp.workflows.get_client", return_value=client):
            with self.assertRaisesRegex(ValueError, "both write"):
                VerifiedBatchExecutor().apply(operations=operations)
        self.assertEqual(client.ping_count, 0)
        self.assertEqual(client.calls, [])

    def test_ambiguous_item_is_never_replayed_and_remaining_items_skip(self) -> None:
        def handler(command: str, arguments: dict[str, Any], index: int) -> dict[str, Any]:
            if index == 1:
                raise BridgeError("reply lost after dispatch")
            return mixer_handler(command, arguments, index)

        client = ScriptedClient(handler)
        operations = [
            {
                "operation_id": "one",
                "operation": "mixer_volume",
                "track_index": 3,
                "volume_normalized": 0.7,
            },
            {
                "operation_id": "two",
                "operation": "mixer_pan",
                "track_index": 3,
                "pan": -0.25,
            },
            {
                "operation_id": "three",
                "operation": "mixer_volume",
                "track_index": 4,
                "volume_normalized": 0.6,
            },
        ]
        with mock.patch("fl_studio_mcp.workflows.get_client", return_value=client):
            result = VerifiedBatchExecutor().apply(operations=operations)

        self.assertEqual(client.ping_count, 1)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(result.stopped_reason, "unknown_outcome")
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.results[-1].status, "error_unknown")
        self.assertFalse(result.automatic_replay_attempted)
        self.assertFalse(result.rollback_attempted)

    def test_master_refusal_is_part_of_structural_preflight(self) -> None:
        client = ScriptedClient(mixer_handler)
        with mock.patch("fl_studio_mcp.workflows.get_client", return_value=client):
            with self.assertRaisesRegex(ValueError, "allow_master=true"):
                VerifiedBatchExecutor().apply(
                    operations=[
                        {
                            "operation_id": "master",
                            "operation": "mixer_volume",
                            "track_index": 0,
                            "volume_normalized": 0.7,
                        }
                    ]
                )
        self.assertEqual(client.ping_count, 0)


if __name__ == "__main__":
    unittest.main()
