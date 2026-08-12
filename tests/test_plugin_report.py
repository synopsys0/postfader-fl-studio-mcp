"""Hermetic coverage for the installed, privacy-safe plug-in reporter."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fl_studio_mcp.plugin_profile import summarise  # noqa: E402
from fl_studio_mcp.plugin_report import (  # noqa: E402
    WriteValidationEvidence,
    build_public_report,
    main,
    render_public_markdown,
    validate_representative_write,
)


COMPLETE_SCAN = {
    "plugin": {
        "name": "Example Effect",
        "track_index": 8,
        "slot_index": 2,
    },
    "reported_parameter_count": 4,
    "scan_start": 0,
    "scan_end": 4,
    "examined_count": 4,
    "real_count": 3,
    "padding_skipped": 1,
    "truncated": False,
    "truncated_by": None,
    "parameters": [
        {
            "index": 0,
            "reported_name": "Private Parameter Label",
            "normalized_value": 0.73,
            "display_text": "73 ClientSession",
            "display_text_available": True,
        },
        {
            "index": 1,
            "reported_name": "Mode",
            "normalized_value": 0.25,
            "display_text": "Custom Preset Name",
            "display_text_available": True,
        },
        {
            "index": 3,
            "reported_name": "Gain",
            "normalized_value": 0.4,
            "display_text": "-3 dB",
            "display_text_available": True,
        },
    ],
}


def report_for(scan=COMPLETE_SCAN, write=None):
    return build_public_report(
        summarise(scan),
        plugin_version="2.1",
        plugin_origin="third-party",
        plugin_format="VST3",
        fl_studio_version="26.1.3 [build 5336] unexpected host suffix",
        fl_studio_edition="Producer",
        write_validation=write,
    )


class PublicReportTests(unittest.TestCase):
    def test_complete_scan_is_read_profiled_not_globally_supported(self):
        report = report_for()
        self.assertEqual(report.evidence_level, "read-profiled")
        self.assertEqual(report.plugin_version, "2.1")
        self.assertTrue(report.scan_complete)
        self.assertEqual(report.fl_studio_version, "26.1.3 build 5336")

    def test_partial_scan_is_only_detected(self):
        scan = dict(
            COMPLETE_SCAN,
            scan_end=2,
            examined_count=2,
            real_count=2,
            padding_skipped=0,
            truncated=True,
            truncated_by="end",
            parameters=COMPLETE_SCAN["parameters"][:2],
        )
        report = report_for(scan)
        self.assertEqual(report.evidence_level, "detected")
        self.assertFalse(report.scan_complete)

    def test_zero_control_scan_proves_detection_only(self):
        scan = {
            "plugin": {"name": "Empty Effect"},
            "reported_parameter_count": 0,
            "scan_start": 0,
            "scan_end": 0,
            "examined_count": 0,
            "real_count": 0,
            "padding_skipped": 0,
            "truncated": False,
            "truncated_by": None,
            "parameters": [],
        }
        report = report_for(scan)
        self.assertEqual(report.evidence_level, "detected")
        self.assertIn("detection only", " ".join(report.limitations))

    def test_missing_scan_bounds_do_not_claim_complete_coverage(self):
        scan = dict(COMPLETE_SCAN)
        scan.pop("scan_start")
        scan.pop("scan_end")
        report = report_for(scan)
        self.assertEqual(report.evidence_level, "detected")
        self.assertFalse(report.scan_complete)

    def test_successful_move_and_exact_restore_promotes_write_evidence(self):
        write = WriteValidationEvidence(
            attempted=True,
            move_verified=True,
            move_verification_basis="value_readback",
            restore_command_verified=True,
            restore_readback_verified=True,
            outcome="write-and-exact-restore-verified",
        )
        self.assertEqual(report_for(write=write).evidence_level, "write-validated")

    def test_partial_profile_cannot_be_promoted_by_a_successful_write(self):
        partial = dict(
            COMPLETE_SCAN,
            scan_end=2,
            examined_count=2,
            real_count=2,
            padding_skipped=0,
            truncated=True,
            truncated_by="end",
            parameters=COMPLETE_SCAN["parameters"][:2],
        )
        write = WriteValidationEvidence(
            attempted=True,
            move_verified=True,
            move_verification_basis="value_readback",
            restore_command_verified=True,
            restore_readback_verified=True,
            outcome="write-and-exact-restore-verified",
        )
        report = report_for(partial, write)
        self.assertEqual(report.evidence_level, "detected")
        self.assertIn("cannot promote", " ".join(report.limitations))

    def test_success_property_requires_every_proof_field(self):
        base = dict(
            attempted=True,
            move_verified=True,
            move_verification_basis="value_readback",
            restore_command_verified=True,
            restore_readback_verified=True,
            outcome="write-and-exact-restore-verified",
        )
        for field, value in (
            ("attempted", False),
            ("move_verified", False),
            ("move_verification_basis", "unknown"),
            ("restore_command_verified", False),
            ("restore_readback_verified", False),
            ("outcome", "validation-incomplete"),
        ):
            with self.subTest(field=field):
                evidence = WriteValidationEvidence(**dict(base, **{field: value}))
                self.assertFalse(evidence.successful)

    def test_public_json_excludes_every_session_bearing_field(self):
        rendered = json.dumps(report_for().as_public_dict(), sort_keys=True)
        for private in (
            "Private Parameter Label",
            "ClientSession",
            "Custom Preset Name",
            "0.73",
            "track_index",
            "slot_index",
        ):
            with self.subTest(private=private):
                self.assertNotIn(private, rendered)
        self.assertIn('"current_values_included": false', rendered)
        self.assertNotIn("control_kinds", rendered)
        self.assertNotIn("recognised_units", rendered)

    def test_markdown_escapes_a_hostile_product_name_without_adding_a_row(self):
        scan = dict(COMPLETE_SCAN, plugin={"name": "Example | Effect\nInjected row"})
        rendered = render_public_markdown(report_for(scan))
        self.assertIn("Example &#124; Effect Injected row", rendered)
        matrix_lines = [
            line for line in rendered.splitlines()
            if line.startswith("|") and "Example" in line
        ]
        self.assertEqual(len(matrix_lines), 1)

    def test_existing_backslash_cannot_reopen_a_table_separator(self):
        scan = dict(COMPLETE_SCAN, plugin={"name": "Example \\| Effect"})
        rendered = render_public_markdown(report_for(scan))
        self.assertIn(r"Example \&#124; Effect", rendered)
        product_lines = [
            line for line in rendered.splitlines()
            if line.startswith("|") and "Example" in line
        ]
        self.assertEqual(len(product_lines), 1)

    def test_private_host_markers_in_product_identity_are_refused(self):
        private_name = "/" + "Users" + "/someone/Effect"
        scan = dict(COMPLETE_SCAN, plugin={"name": private_name})
        with self.assertRaisesRegex(ValueError, "private home path"):
            report_for(scan)

    def test_terminal_and_bidi_control_characters_are_refused(self):
        for hostile in ("Effect\x1b]8;;https://example.invalid\x07link", "safe\u202eevil"):
            with self.subTest(hostile=repr(hostile)):
                scan = dict(COMPLETE_SCAN, plugin={"name": hostile})
                with self.assertRaisesRegex(ValueError, "unsafe control"):
                    report_for(scan)

    def test_inconsistent_scan_is_refused_as_evidence(self):
        scan = dict(COMPLETE_SCAN, real_count=99)
        with self.assertRaisesRegex(ValueError, "internally inconsistent"):
            report_for(scan)

    def test_markdown_keeps_every_limitation_complete_in_full_details(self):
        long_caveat = (
            "This deliberately long caveat must remain complete: "
            + "word " * 100
            + "END-MARKER."
        )
        report = replace(
            report_for(),
            limitations=(long_caveat, "Second required caveat."),
        )
        rendered = render_public_markdown(report)
        matrix_row = next(
            line for line in rendered.splitlines()
            if line.startswith("| Example Effect")
        )
        self.assertIn("See full limitations below (2).", matrix_row)
        self.assertNotIn(long_caveat, matrix_row)
        self.assertIn(long_caveat, rendered)
        self.assertIn("Second required caveat.", rendered)
        self.assertIn("END-MARKER.", rendered)

    def test_unknown_fl_version_is_omitted_from_environment(self):
        report = replace(
            report_for(),
            fl_studio_version="unknown",
            fl_studio_edition="unknown",
            platform="unknown",
        )
        rendered = render_public_markdown(report)
        matrix_row = next(
            line for line in rendered.splitlines()
            if line.startswith("| Example Effect")
        )
        self.assertNotIn("FL Studio unknown", matrix_row)
        self.assertIn(f"Postfader {report.postfader_version}", matrix_row)

    def test_one_nameless_control_uses_singular_grammar(self):
        parameters = [dict(item) for item in COMPLETE_SCAN["parameters"]]
        parameters[0]["reported_name"] = ""
        report = report_for(dict(COMPLETE_SCAN, parameters=parameters))
        rendered = render_public_markdown(report)
        self.assertIn("1 control was nameless in FL's report.", rendered)
        self.assertNotIn("1 controls were", rendered)

    def test_generated_markdown_defines_community_candidate_status(self):
        rendered = render_public_markdown(report_for())
        self.assertIn("community candidate", rendered)
        self.assertIn("has not yet been reviewed or merged", rendered)
        self.assertIn("maintained compatibility matrix", rendered)


class FakeWriteClient:
    def __init__(
        self,
        *,
        writes=True,
        playing=False,
        recording=False,
        safe_to_edit=True,
        move_verified=True,
        restore_lands=True,
        restore_reports_verified=None,
    ):
        self.writes = writes
        self.playing = playing
        self.recording = recording
        self.safe_to_edit = safe_to_edit
        self.move_verified = move_verified
        self.restore_lands = restore_lands
        self.restore_reports_verified = restore_reports_verified
        self.original = 0.2
        self.value = self.original
        self.display = "20 %"
        self.set_calls = 0
        self.commands = []

    def ping(self):
        self.commands.append("ping")
        return {"verified_writes_enabled": self.writes}

    def call(self, command, **arguments):
        self.commands.append(command)
        if command == "project.info":
            return {
                "playing": self.playing,
                "recording": self.recording,
                "safe_to_edit": self.safe_to_edit,
            }
        if command == "plugin.params":
            return {
                "params": [
                    {"index": 0, "value": self.value, "display": self.display}
                ]
            }
        if command == "plugin.set_param":
            self.set_calls += 1
            restoring = self.set_calls == 2
            if not restoring or self.restore_lands:
                self.value = float(arguments["value"])
                self.display = "20 %" if self.value == self.original else "75 %"
            verified = True if restoring else self.move_verified
            if restoring and self.restore_reports_verified is not None:
                verified = self.restore_reports_verified
            if restoring and not self.restore_lands:
                verified = False
            return {
                "verified": verified,
                "verification_basis": "value_readback" if verified else "none",
            }
        raise AssertionError(command)


class WriteValidationTests(unittest.TestCase):
    def test_verified_move_and_independent_exact_restore(self):
        client = FakeWriteClient()
        evidence = validate_representative_write(
            client, summarise(COMPLETE_SCAN), track=8, slot=2, parameter_index=0
        )
        self.assertTrue(evidence.successful)
        self.assertEqual(evidence.outcome, "write-and-exact-restore-verified")
        self.assertEqual(client.value, client.original)
        self.assertEqual(client.set_calls, 2)

    def test_unverified_move_never_claims_write_validation(self):
        evidence = validate_representative_write(
            FakeWriteClient(move_verified=False),
            summarise(COMPLETE_SCAN),
            track=8,
            slot=2,
            parameter_index=0,
        )
        self.assertFalse(evidence.successful)
        self.assertEqual(evidence.outcome, "write-not-verified")
        self.assertTrue(evidence.restore_readback_verified)

    def test_failed_restore_is_loud_evidence_not_success(self):
        evidence = validate_representative_write(
            FakeWriteClient(restore_lands=False),
            summarise(COMPLETE_SCAN),
            track=8,
            slot=2,
            parameter_index=0,
        )
        self.assertFalse(evidence.successful)
        self.assertEqual(evidence.outcome, "restore-not-confirmed")
        self.assertFalse(evidence.restore_readback_verified)

    def test_unverified_restore_cannot_be_rescued_by_a_matching_reread(self):
        evidence = validate_representative_write(
            FakeWriteClient(restore_lands=True, restore_reports_verified=False),
            summarise(COMPLETE_SCAN),
            track=8,
            slot=2,
            parameter_index=0,
        )
        self.assertFalse(evidence.successful)
        self.assertTrue(evidence.restore_readback_verified)
        self.assertEqual(evidence.outcome, "validation-incomplete")

    def test_refuses_master_playback_recording_and_disabled_writes(self):
        profile = summarise(COMPLETE_SCAN)
        cases = (
            (FakeWriteClient(), 0, "Master"),
            (FakeWriteClient(playing=True), 8, "playing"),
            (FakeWriteClient(recording=True), 8, "recording"),
            (FakeWriteClient(writes=False), 8, "disabled"),
            (FakeWriteClient(safe_to_edit=False), 8, "safe to edit"),
        )
        for client, track, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    validate_representative_write(
                        client,
                        profile,
                        track=track,
                        slot=2,
                        parameter_index=0,
                    )
                self.assertNotIn("plugin.set_param", client.commands)

    def test_refuses_non_numeric_controls_as_generic_write_targets(self):
        with self.assertRaisesRegex(ValueError, "numeric control"):
            validate_representative_write(
                FakeWriteClient(),
                summarise(COMPLETE_SCAN),
                track=8,
                slot=2,
                parameter_index=1,
            )


class OfflineCliTests(unittest.TestCase):
    def test_help_defines_community_candidate_status(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as stopped:
                main(["--help"])
        self.assertEqual(stopped.exception.code, 0)
        help_text = " ".join(stdout.getvalue().split())
        self.assertIn("community candidate for maintainer review", help_text)
        self.assertIn("not maintained compatibility evidence", help_text)

    def test_saved_raw_bridge_scan_generates_shareable_output(self):
        raw = {
            "plugin": "Example Effect",
            "reported_count": 2,
            "scan_start": 0,
            "scan_end": 2,
            "examined": 2,
            "real": 1,
            "padding_skipped": 1,
            "truncated": False,
            "truncated_by": None,
            "params": [
                {
                    "index": 0,
                    "name": "Private Parameter Label",
                    "value": 0.73,
                    "display": "Custom Preset Name",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scan.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main([
                    "--from-json", str(path),
                    "--plugin-origin", "third-party",
                    "--plugin-format", "VST3",
                ])
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertIn("read-profiled", stdout.getvalue())
        self.assertNotIn("Private Parameter Label", stdout.getvalue())
        self.assertNotIn("Custom Preset Name", stdout.getvalue())
        self.assertNotIn("0.73", stdout.getvalue())

    def test_write_mode_requires_explicit_disposable_project_acknowledgement(self):
        with self.assertRaises(SystemExit):
            main(["--track", "8", "--slot", "2", "--validate-write", "0"])

    def test_live_only_scan_bound_is_rejected_for_saved_json(self):
        with self.assertRaises(SystemExit):
            main(["--from-json", "unused.json", "--max-indices", "8"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
