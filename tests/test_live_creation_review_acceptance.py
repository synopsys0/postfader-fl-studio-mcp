"""Deterministic acceptance-harness tests; none require FL Studio."""

from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "live_creation_review_acceptance.py"


def _module():
    spec = importlib.util.spec_from_file_location("live_creation_review_acceptance", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load acceptance script")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


acceptance = _module()


class CreationReviewAcceptanceTests(unittest.TestCase):
    def test_default_is_offline_plan_and_does_not_require_inputs(self) -> None:
        args = acceptance.parse_args([])
        self.assertTrue(args.plan)
        report = asyncio.run(acceptance.async_main(args))
        self.assertEqual(report["mode"], "plan_only")
        self.assertEqual(report["overall"], "pass")
        self.assertFalse(report["contact_started"])
        self.assertFalse(report["physical_io_performed"])
        self.assertFalse(report["checks"]["offline_only"]["mcp_imported"])

    def test_private_output_accepts_only_bounded_private_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / ".private").mkdir()
            with mock.patch.object(acceptance, "ROOT", checkout):
                inside = acceptance._private_output(".private" + "/evidence.json")
                self.assertEqual(inside, (checkout / ".private" / "evidence.json").resolve())
                with self.assertRaises(acceptance.EvidenceOutputError):
                    acceptance._private_output(str(checkout / "public.json"))
                with self.assertRaises(acceptance.EvidenceOutputError):
                    acceptance._private_output(str(checkout / ".private" / ".." / "public.json"))

    def test_live_flow_uses_selected_assets_and_all_review_steps_without_forbidden_calls(self) -> None:
        args = acceptance.parse_args(
            [
                "--live",
                "--source-run-id",
                "run-1",
                "--bounce",
                "/tmp/candidate.wav",
                "--before-bounce",
                "/tmp/before.wav",
                "--after-bounce",
                "/tmp/after.wav",
                "--feedback",
                "Keep the lead identity; increase drop contrast.",
                "--apply",
                "--authorize-apply",
                "--confirm-user-present",
                "--confirm-disposable-project",
                "--confirm-safe-to-edit",
                "--approve",
            ]
        )
        calls: list[tuple[str, dict[str, object]]] = []

        async def caller(name: str, arguments: dict[str, object]) -> object:
            calls.append((name, arguments))
            if name == "postfader_review_start":
                return {"review_session_id": "review-1", "source_run_id": "run-1"}
            if name == "postfader_review_attach_assets":
                return {
                    "review_session_id": "review-1",
                    "source_run_id": "run-1",
                    "asset_sets": [{"asset_set_id": "assets-1"}],
                    "assets": [
                        {"asset_id": "before-bounce", "asset_kind": "before_full_mix"},
                        {"asset_id": "after-bounce", "asset_kind": "after_full_mix"},
                    ],
                }
            if name == "postfader_review_evaluate":
                return {"evaluation_id": "evaluation-1", "findings": []}
            if name == "postfader_review_record_feedback":
                return {"review_session_id": "review-1", "status": "accepted"}
            if name == "postfader_review_plan_revision":
                return {"revision_plan_id": "plan-1", "operations": []}
            if name == "postfader_review_apply_revision":
                return {"revision_pass_id": "pass-1", "status": "awaiting_rebounce"}
            if name == "postfader_review_compare":
                return {"comparison_id": "comparison-1", "technical_conclusion": "mixed"}
            if name == "postfader_delivery_manifest":
                return {"delivery_id": "delivery-1", "final_user_approval": "pending"}
            return {}

        with mock.patch("fl_studio_mcp.evidence.configure_acceptance_transport"):
            report = asyncio.run(acceptance.async_main(args, caller=caller))

        self.assertEqual(report["overall"], "pass")
        self.assertEqual(report["phase"], "complete")
        self.assertEqual(
            [name for name, _arguments in calls],
            [
                "postfader_review_start",
                "postfader_review_attach_assets",
                "postfader_review_evaluate",
                "postfader_review_record_feedback",
                "postfader_review_plan_revision",
                "postfader_review_apply_revision",
                "postfader_review_compare",
                "postfader_review_record_feedback",
                "postfader_review_export_handoff",
                "postfader_delivery_manifest",
            ],
        )
        apply_call = next(arguments for name, arguments in calls if name == "postfader_review_apply_revision")
        self.assertIs(True, apply_call["authorized_to_modify"])
        self.assertIs(True, apply_call["request"]["authorized_to_modify"])
        self.assertTrue(report["timings"]["steps"])
        self.assertTrue(report["fl_project_io_performed"])
        self.assertFalse(report["private_artifact_commit_attempted"])
        self.assertEqual(report["checks"]["no_render_save_click"]["forbidden_tool_calls"], [])

    def test_apply_requires_explicit_authorization_before_any_mcp_contact(self) -> None:
        args = acceptance.parse_args(
            [
                "--live",
                "--step",
                "apply",
                "--review-session-id",
                "review-1",
                "--confirm-user-present",
                "--confirm-disposable-project",
                "--confirm-safe-to-edit",
            ]
        )
        with self.assertRaises(acceptance.AcceptanceConfigurationError) as raised:
            asyncio.run(acceptance.async_main(args, caller=mock.AsyncMock()))
        self.assertEqual(raised.exception.blockers[0]["code"], "apply_authorization_required")

    def test_failed_read_step_is_timed_and_never_replayed(self) -> None:
        args = acceptance.parse_args(
            [
                "--live",
                "--source-run-id",
                "run-1",
                "--bounce",
                "/tmp/candidate.wav",
                "--confirm-user-present",
                "--confirm-disposable-project",
            ]
        )
        calls: list[str] = []

        async def caller(name: str, _arguments: dict[str, object]) -> object:
            calls.append(name)
            if name == "postfader_review_start":
                return {"review_session_id": "review-1", "source_run_id": "run-1"}
            if name == "postfader_review_attach_assets":
                return {"review_session_id": "review-1", "asset_sets": [{"asset_set_id": "assets-1"}]}
            raise RuntimeError("synthetic analyzer failure")

        with mock.patch("fl_studio_mcp.evidence.configure_acceptance_transport"):
            report = asyncio.run(acceptance.async_main(args, caller=caller))
        self.assertEqual(report["overall"], "fail")
        self.assertEqual(report["phase"], "blocked")
        self.assertEqual(calls, [
            "postfader_review_start",
            "postfader_review_attach_assets",
            "postfader_review_evaluate",
        ])
        self.assertEqual(report["blockers"][0]["code"], "step_failed")
        failed_timing = report["timings"]["steps"][-1]
        self.assertEqual(failed_timing["status"], "failed")
        self.assertFalse(report["automatic_replay_attempted"])

    def test_named_resume_step_reads_existing_session_before_evaluate(self) -> None:
        args = acceptance.parse_args(
            [
                "--live",
                "--step",
                "evaluate",
                "--review-session-id",
                "review-1",
                "--asset-set-id",
                "assets-1",
                "--confirm-user-present",
                "--confirm-disposable-project",
            ]
        )
        calls: list[str] = []

        async def caller(name: str, _arguments: dict[str, object]) -> object:
            calls.append(name)
            if name == "postfader_review_get":
                return {"review_session_id": "review-1", "source_run_id": "run-1"}
            return {"evaluation_id": "evaluation-2", "findings": []}

        with mock.patch("fl_studio_mcp.evidence.configure_acceptance_transport"):
            report = asyncio.run(acceptance.async_main(args, caller=caller))
        self.assertEqual(calls, ["postfader_review_get", "postfader_review_evaluate"])
        self.assertEqual(report["source_run_id"], "run-1")
        self.assertEqual(report["evaluation_id"], "evaluation-2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
