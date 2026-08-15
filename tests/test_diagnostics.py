"""Hermetic Windows/macOS coverage for strict doctor evidence."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, os.fspath(ROOT))

from fl_studio_mcp import diagnostics  # noqa: E402
from fl_studio_mcp.bridge_install import (  # noqa: E402
    expected_bridge_deployment,
    target_path,
)


class DoctorEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="postfader-doctor-")
        self.user_data = Path(self.tmp.name) / "FL Studio User Data"
        self.hardware = self.user_data / "Settings" / "Hardware"
        self.hardware.mkdir(parents=True)
        self.script = target_path(self.user_data)
        self.script.parent.mkdir(parents=True)
        stamped, self.source_hash = expected_bridge_deployment()
        self.script.write_bytes(stamped)
        self.fl_candidate = Path(self.tmp.name) / "FL Studio 2026" / "FL64.exe"

    def tearDown(self):
        self.tmp.cleanup()

    def live_value(self, **connection_overrides):
        connection = {
            "connected": True,
            "compatible": True,
            "compatibility_reason": "compatible",
            "program_title": "FL Studio Producer Edition",
            "fl_app_version": "26.1.3 [build 5336]",
            "fl_build": 5336,
            "midi_scripting_api_version": 44,
            "bridge_protocol_version": 2,
            "bridge_transport": "midi",
            "bridge_mode": "read_only",
            "bridge_read_only_enforced": True,
            "verified_writes_enabled": False,
            "bridge_source_sha256": self.source_hash,
            "bridge_provenance": "matching",
            "session_fingerprint": "0123456789abcdef",
        }
        connection.update(connection_overrides)
        return {
            "ping": {"pong": True, "midi_wire_protocol_version": 2},
            "connection": connection,
            "project": {
                "title": "PRIVATE PROJECT TITLE",
                "author": "PRIVATE AUTHOR",
                "genre": "PRIVATE GENRE",
            },
            "selected_transport": "midi",
            "attempted_transports": ["tcp", "files", "midi"],
        }

    def collect(self, *, environ=None, ports=None, live=None, platform_name="win32"):
        values = {"FL_BRIDGE_MIDI_PORT": "Chosen Port"}
        if environ is not None:
            values = dict(environ)
        return diagnostics.collect_evidence(
            user_data_dir=self.user_data,
            platform_name=platform_name,
            environ=values,
            fl_candidates=[self.fl_candidate],
            midi_probe=lambda: ports
            if ports is not None
            else {
                "in": ["Chosen Port Extended", "cHoSeN pOrT"],
                "out": ["Chosen Port Extended", "cHoSeN pOrT"],
            },
            live_probe=lambda: live if live is not None else self.live_value(),
        )

    def test_windows_exact_match_wins_and_reports_complete_evidence(self):
        evidence = self.collect()
        self.assertEqual(evidence["overall"], "pass")
        self.assertEqual(evidence["midi"]["selected_input"], "cHoSeN pOrT")
        self.assertEqual(evidence["midi"]["selected_output"], "cHoSeN pOrT")
        self.assertEqual(evidence["live"]["selected_transport"], "midi")
        self.assertEqual(evidence["live"]["bridge_protocol"], 2)
        self.assertEqual(evidence["live"]["midi_wire_protocol_version"], 2)
        self.assertIn(
            "Protocols: command 2; MIDI wire 2",
            diagnostics.render_human(evidence),
        )
        self.assertNotIn("project", evidence["live"])
        self.assertNotIn("PRIVATE PROJECT TITLE", json.dumps(evidence))
        self.assertEqual(
            evidence["live"]["attempted_transports"], ["tcp", "files", "midi"]
        )
        self.assertTrue(evidence["live"]["read_only_bridge"])
        self.assertEqual(evidence["bridge_deployment"]["status"], "current")
        self.assertTrue(evidence["bridge_deployment"]["repository_file_sha256"])
        rendered = diagnostics.render_human(evidence)
        for forbidden in ("IAC", "CoreMIDI", "macOS", "Audio MIDI Setup"):
            self.assertNotIn(forbidden, rendered)

    def test_unique_substring_retains_macos_iac_compatibility_and_guidance(self):
        evidence = self.collect(
            environ={},
            platform_name="darwin",
            ports={"in": ["IAC Driver Bus 1"], "out": ["IAC Driver Bus 1"]},
        )
        self.assertEqual(evidence["midi"]["configured_query"], "IAC Driver")
        self.assertEqual(evidence["midi"]["selected_input"], "IAC Driver Bus 1")

        missing = self.collect(
            environ={},
            platform_name="darwin",
            ports={"in": ["Other"], "out": ["Other"]},
        )
        self.assertIn("IAC", diagnostics.render_human(missing))
        self.assertIn("Audio MIDI Setup", diagnostics.render_human(missing))

    def test_missing_windows_midi_configuration_fails_before_any_probe(self):
        midi_probe = mock.Mock(side_effect=AssertionError("MIDI probe called"))
        live_probe = mock.Mock(side_effect=AssertionError("live probe called"))
        evidence = diagnostics.collect_evidence(
            user_data_dir=self.user_data,
            platform_name="win32",
            environ={},
            fl_candidates=[self.fl_candidate],
            midi_probe=midi_probe,
            live_probe=live_probe,
        )
        self.assertEqual(
            [item["code"] for item in evidence["failures"]],
            ["missing_midi_configuration"],
        )
        midi_probe.assert_not_called()
        live_probe.assert_not_called()

    def test_sandbox_marker_skips_native_enumeration_and_live_handshake(self):
        midi_probe = mock.Mock(side_effect=AssertionError("MIDI probe called"))
        live_probe = mock.Mock(side_effect=AssertionError("live probe called"))
        evidence = diagnostics.collect_evidence(
            user_data_dir=self.user_data,
            platform_name="win32",
            environ={
                "FL_BRIDGE_MIDI_PORT": "Chosen Port",
                "FL_BRIDGE_SANDBOXED": "1",
            },
            fl_candidates=[self.fl_candidate],
            midi_probe=midi_probe,
            live_probe=live_probe,
        )
        self.assertEqual(evidence["overall"], "partial")
        self.assertEqual(evidence["failures"], [])
        self.assertEqual(
            {item["code"] for item in evidence["skipped_checks"]},
            {"native_midi_enumeration", "live_bridge_handshake"},
        )
        midi_probe.assert_not_called()
        live_probe.assert_not_called()

    def test_empty_endpoint_lists_fail_without_ok_zero_wording(self):
        evidence = self.collect(ports={"in": [], "out": []})
        codes = [item["code"] for item in evidence["failures"]]
        self.assertEqual(codes.count("zero_midi_endpoints"), 2)
        self.assertNotIn("OK 0", diagnostics.render_human(evidence))

    def test_endpoint_failures_are_distinguished(self):
        cases = (
            (
                {"in": ["Other"], "out": ["Chosen Port"]},
                "missing_configured_endpoint",
            ),
            (
                {
                    "in": ["Chosen Port A", "Chosen Port B"],
                    "out": ["Chosen Port"],
                },
                "ambiguous_endpoint",
            ),
            (
                {"in": ["Chosen Port"], "out": ["Other"]},
                "missing_matching_output",
            ),
        )
        for ports, expected in cases:
            with self.subTest(expected=expected):
                evidence = self.collect(ports=ports)
                self.assertIn(
                    expected, [item["code"] for item in evidence["failures"]]
                )
                self.assertEqual(
                    evidence["live"]["status"], "blocked_endpoint_selection"
                )

    def test_installation_hardware_and_stale_bridge_states_are_distinct(self):
        missing_install = diagnostics.collect_evidence(
            user_data_dir=self.user_data,
            platform_name="win32",
            environ={"FL_BRIDGE_MIDI_PORT": "Chosen Port", "FL_BRIDGE_SANDBOXED": "1"},
            fl_candidates=[],
        )
        self.assertIn(
            "missing_fl_installation",
            [item["code"] for item in missing_install["failures"]],
        )

        self.hardware.rename(self.hardware.with_name("Hardware removed"))
        missing_hardware = diagnostics.collect_evidence(
            user_data_dir=self.user_data,
            platform_name="win32",
            environ={"FL_BRIDGE_MIDI_PORT": "Chosen Port", "FL_BRIDGE_SANDBOXED": "1"},
            fl_candidates=[self.fl_candidate],
        )
        self.assertIn(
            "missing_hardware_folder",
            [item["code"] for item in missing_hardware["failures"]],
        )

        self.hardware.mkdir(parents=True)
        self.script.parent.mkdir(parents=True)
        self.script.write_bytes(b"stale")
        stale = diagnostics.collect_evidence(
            user_data_dir=self.user_data,
            platform_name="win32",
            environ={"FL_BRIDGE_MIDI_PORT": "Chosen Port", "FL_BRIDGE_SANDBOXED": "1"},
            fl_candidates=[self.fl_candidate],
        )
        self.assertIn("stale_bridge", [item["code"] for item in stale["failures"]])

    def test_incompatible_live_bridge_is_actionable_and_separate_from_read_only(self):
        evidence = self.collect(
            live=self.live_value(
                compatible=False,
                compatibility_reason="unsupported bridge protocol 99",
                bridge_protocol_version=99,
                bridge_read_only_enforced=False,
            )
        )
        self.assertIn(
            "incompatible_fl_or_protocol",
            [item["code"] for item in evidence["failures"]],
        )
        self.assertFalse(evidence["live"]["read_only_bridge"])

    def test_json_mode_emits_only_parseable_evidence_and_nonzero_on_failures(self):
        evidence = self.collect(ports={"in": [], "out": []})
        stdout = io.StringIO()
        with (
            mock.patch.object(diagnostics, "collect_evidence", return_value=evidence),
            redirect_stdout(stdout),
        ):
            status = diagnostics.main(["--json"])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(stdout.getvalue()), evidence)

    def test_custom_absolute_executable_is_reported_as_explicit(self):
        custom = Path(self.tmp.name) / "Custom Image-Line" / "FL64.exe"
        custom.parent.mkdir(parents=True)
        custom.write_bytes(b"fixture executable")
        evidence = diagnostics.collect_evidence(
            user_data_dir=self.user_data,
            platform_name="win32",
            environ={"FL_BRIDGE_MIDI_PORT": "Chosen Port", "FL_BRIDGE_SANDBOXED": "1"},
            fl_executable=custom,
        )
        self.assertEqual(evidence["fl_studio"]["selection_source"], "explicit")
        self.assertEqual(evidence["fl_studio"]["selected_executable"], os.fspath(custom))

    def test_relative_custom_executable_is_strictly_refused(self):
        with self.assertRaisesRegex(diagnostics.HostConfigurationError, "absolute path"):
            diagnostics.collect_evidence(
                user_data_dir=self.user_data,
                platform_name="win32",
                environ={"FL_BRIDGE_SANDBOXED": "1"},
                fl_executable="relative/FL64.exe",
            )

    def test_invalid_user_data_environment_emits_one_json_object(self):
        stdout = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {
                    "FL_STUDIO_USER_DATA_DIR": "relative/user-data",
                    "FL_BRIDGE_SANDBOXED": "1",
                },
                clear=True,
            ),
            redirect_stdout(stdout),
        ):
            status = diagnostics.main(["--json"])
        value = json.loads(stdout.getvalue())
        self.assertEqual(status, 1)
        self.assertEqual(value["overall"], "fail")
        self.assertEqual(value["failures"][0]["code"], "invalid_host_configuration")

    def test_invalid_environment_is_safe_during_fresh_module_import(self):
        environment = dict(os.environ)
        environment.update(
            {
                "FL_STUDIO_USER_DATA_DIR": "relative/user-data",
                "FL_BRIDGE_SANDBOXED": "1",
                "FL_BRIDGE_ENABLE_MIDI": "0",
            }
        )
        completed = subprocess.run(
            [sys.executable, "-m", "fl_studio_mcp.diagnostics", "--json"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        value = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(value["failures"][0]["code"], "invalid_host_configuration")

    def test_deployed_bridge_read_error_is_actionable(self):
        original = Path.read_bytes

        def read_bytes(path):
            if path == self.script:
                raise PermissionError("fixture denied")
            return original(path)

        with mock.patch.object(Path, "read_bytes", read_bytes):
            evidence = self.collect(
                environ={
                    "FL_BRIDGE_MIDI_PORT": "Chosen Port",
                    "FL_BRIDGE_SANDBOXED": "1",
                }
            )
        self.assertIn(
            "deployed_bridge_unreadable",
            [item["code"] for item in evidence["failures"]],
        )

    def test_default_live_probe_always_closes_client(self):
        client = mock.Mock()
        client.ping.side_effect = RuntimeError("fixture ping failure")
        with mock.patch(
            "fl_studio_mcp.bridge_client.BridgeClient", return_value=client
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture ping failure"):
                diagnostics._default_live_probe()
        client.close.assert_called_once_with()

    def test_default_live_probe_enables_midi_before_bridge_construction(self):
        client = mock.Mock()
        client.ping.return_value = {"pong": True}
        client._transports = []
        connection = mock.Mock()
        connection.model_dump.return_value = {}

        def construct(**_kwargs):
            self.assertEqual(os.environ["FL_BRIDGE_ENABLE_MIDI"], "1")
            return client

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch(
                "fl_studio_mcp.bridge_client.BridgeClient",
                side_effect=construct,
            ),
            mock.patch(
                "fl_studio_mcp.readonly_inspector.connection_from_ping",
                return_value=connection,
            ),
        ):
            diagnostics._default_live_probe()
        client.close.assert_called_once_with()

    def test_cli_enables_macos_default_midi_before_collection(self):
        captured = {}
        expected = self.collect(
            environ={"FL_BRIDGE_SANDBOXED": "1"},
            platform_name="darwin",
        )

        def collect(**_kwargs):
            captured.update(os.environ)
            return expected

        with (
            mock.patch.object(
                diagnostics, "midi_port_query", return_value="IAC Driver"
            ),
            mock.patch.object(diagnostics, "collect_evidence", side_effect=collect),
            mock.patch.dict(os.environ, {}, clear=True),
            redirect_stdout(io.StringIO()),
        ):
            diagnostics.main(["--json"])
        self.assertEqual(captured["FL_BRIDGE_ENABLE_MIDI"], "1")
        self.assertNotIn("FL_BRIDGE_MIDI_PORT", captured)

    def test_cli_midi_port_is_configured_before_collection(self):
        captured = {}
        expected = self.collect(
            environ={
                "FL_BRIDGE_MIDI_PORT": "Exact Fixture Port",
                "FL_BRIDGE_SANDBOXED": "1",
            }
        )

        def collect(**kwargs):
            captured.update(os.environ)
            self.assertEqual(kwargs["midi_port"], "Exact Fixture Port")
            return expected

        stdout = io.StringIO()
        with (
            mock.patch.object(diagnostics, "collect_evidence", side_effect=collect),
            mock.patch.dict(os.environ, {}, clear=True),
            redirect_stdout(stdout),
        ):
            diagnostics.main(["--json", "--midi-port", "Exact Fixture Port"])
        self.assertEqual(captured["FL_BRIDGE_ENABLE_MIDI"], "1")
        self.assertEqual(captured["FL_BRIDGE_MIDI_PORT"], "Exact Fixture Port")


if __name__ == "__main__":
    unittest.main(verbosity=2)
