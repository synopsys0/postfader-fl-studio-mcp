"""Focused contracts for Playlist/export handoffs and delivery manifests."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fl_studio_mcp.creation_review.delivery as review_delivery
from fl_studio_mcp.creation_review.delivery import (
    confirm_playlist_handoff,
    create_delivery_manifest,
    create_export_handoff,
    create_playlist_handoff,
    write_delivery_manifest,
)
from fl_studio_mcp.creation_review.models import (
    AlignmentResult,
    CreationEvaluationReport,
    FrozenMap,
    PlaylistPlacement,
    ReviewAudioAsset,
    ReviewGeneratedOutput,
    ReviewRoleAssignment,
    ReviewSection,
    ReviewSession,
    ReviewSessionRequest,
    ReviewSourceSnapshot,
    RevisionComparison,
    RevisionPass,
)


class CreationReviewDeliveryTests(unittest.TestCase):
    def test_playlist_handoff_contains_exact_rows_and_only_changed_delta(self) -> None:
        original = PlaylistPlacement(pattern_number=3, pattern_name="PF Drop A", playlist_track=3, start_bar=17, end_bar=25)
        changed = original.model_copy(update={"end_bar": 33, "length_bars": 16})
        first = create_playlist_handoff((original,), handoff_id="handoff-1")
        second = create_playlist_handoff((changed,), previous=first.placements, handoff_id="handoff-2")
        self.assertEqual(first.placements[0].pattern_name, "PF Drop A")
        self.assertEqual(len(first.delta_from_source), 1)
        self.assertEqual(len(second.delta_from_source), 1)
        unchanged = create_playlist_handoff((original,), previous=first.placements, handoff_id="handoff-3")
        self.assertEqual(unchanged.delta_from_source, ())
        self.assertEqual(confirm_playlist_handoff(first).status, "user_confirmed_complete")

    def test_export_handoff_requests_stems_only_for_attribution_findings(self) -> None:
        masking = create_export_handoff(finding_categories=("masking",))
        loudness = create_export_handoff(finding_categories=("dynamics",))
        self.assertEqual(set(masking.requested_stems), {"instrumental_stem", "vocal_stem"})
        self.assertEqual(loudness.requested_stems, ())
        self.assertTrue(masking.normalization_off)
        self.assertFalse(masking.include_tails)

    def test_delivery_manifest_is_create_only_and_reports_digest(self) -> None:
        manifest = create_delivery_manifest(
            source_run_id="run-1",
            review_session_id="session-1",
            delivery_id="delivery-1",
            next_action="Export PF_Review_01_After.wav.",
        )
        with tempfile.TemporaryDirectory() as directory:
            result = write_delivery_manifest(manifest, directory)
            self.assertTrue(result.json_path and result.json_path.exists())
            self.assertTrue(result.markdown_path and result.markdown_path.exists())
            self.assertEqual(result.digest, manifest.digest)
            self.assertEqual(result.manifest_digest, manifest.digest)
            assert result.json_path is not None
            assert result.markdown_path is not None
            self.assertEqual(
                result.json_sha256,
                hashlib.sha256(result.json_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                result.markdown_sha256,
                hashlib.sha256(result.markdown_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(write_delivery_manifest(manifest, directory).digest, manifest.digest)
            different = create_delivery_manifest(
                source_run_id="run-1",
                review_session_id="session-1",
                delivery_id="delivery-1",
                next_action="Different action.",
            )
            with self.assertRaises(FileExistsError):
                write_delivery_manifest(different, directory)
            assert result.markdown_path is not None
            result.markdown_path.write_text(
                f"different content that repeats {manifest.digest}\n",
                encoding="utf-8",
            )
            with self.assertRaises(FileExistsError):
                write_delivery_manifest(manifest, directory)

    def test_delivery_preflights_both_formats_before_creating_either(self) -> None:
        manifest = create_delivery_manifest(
            source_run_id="run-1",
            review_session_id="session-1",
            delivery_id="delivery-preflight",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            markdown_path = root / "delivery-preflight.md"
            markdown_path.write_text("conflicting content\n", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                write_delivery_manifest(manifest, directory)
            self.assertFalse((root / "delivery-preflight.json").exists())

    def test_delivery_companion_failure_removes_only_new_files(self) -> None:
        manifest = create_delivery_manifest(
            source_run_id="run-1",
            review_session_id="session-1",
            delivery_id="delivery-pair",
        )
        original = review_delivery._write_create_only

        def fail_markdown(path: Path, content: str, *, digest: str) -> bool:
            if path.suffix == ".md":
                raise OSError("injected companion failure")
            return original(path, content, digest=digest)

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            review_delivery,
            "_write_create_only",
            side_effect=fail_markdown,
        ):
            root = Path(directory)
            with self.assertRaisesRegex(OSError, "injected companion failure"):
                write_delivery_manifest(manifest, directory)
            self.assertFalse((root / "delivery-pair.json").exists())
            self.assertFalse((root / "delivery-pair.md").exists())

    def test_delivery_redacts_private_metadata_but_retains_asset_path(self) -> None:
        asset = ReviewAudioAsset(
            asset_id="asset-safe",
            asset_kind="candidate_full_mix",
            path="/private/selected/bounce.wav",
            display_label="bounce",
            sha256="a" * 64,
        )
        output = ReviewGeneratedOutput(
            output_id="output-safe",
            output_kind="pattern",
            metadata=FrozenMap(
                {
                    "filename": "/private/project.flp",
                    "encoded": "UklGRgAAAAA=",
                    "apiToken": "tok-secret-value",
                    "raw_transcript": "PRIVATE TRANSCRIPT",
                    "retained": "pattern 4",
                }
            ),
        )
        manifest = create_delivery_manifest(
            source_run_id="run-1",
            review_session_id="session-1",
            delivery_id="delivery-safe",
            review_assets=(asset,),
            accepted_generated_outputs=(output,),
        )
        with tempfile.TemporaryDirectory() as directory:
            result = write_delivery_manifest(manifest, directory)
            assert result.json_path is not None
            encoded = result.json_path.read_text(encoding="utf-8")
            payload = json.loads(encoded)
        self.assertEqual(
            payload["review_assets"][0]["path"],
            "/private/selected/bounce.wav",
        )
        self.assertIn("pattern 4", encoded)
        for private_value in (
            "/private/project.flp",
            "UklGRgAAAAA=",
            "tok-secret-value",
            "PRIVATE TRANSCRIPT",
        ):
            self.assertNotIn(private_value, encoded)

    def test_delivery_manifest_retains_review_checkpoint_and_decisions(self) -> None:
        output = ReviewGeneratedOutput(
            output_id="source-output",
            output_kind="note_sequence",
            role_id="lead",
            digest="a" * 64,
        )
        assignment = ReviewRoleAssignment(
            role_id="lead",
            product_name="Synth",
            preset_name="Bright Lead",
        )
        section = ReviewSection(
            section_id="intro",
            name="Intro",
            start_bar=1,
            end_bar=3,
            start_seconds=0.0,
            end_seconds=4.0,
            source="production_run",
        )
        request = ReviewSessionRequest(
            source_run_id="run-1",
            brief="Make a playable draft.",
            persist_session=True,
        )
        snapshot = ReviewSourceSnapshot(
            source_run_id="run-1",
            source_state_digest="b" * 64,
            original_brief=request.brief,
            completion_target="playable_draft",
            sound_palette=FrozenMap({"assignments": (assignment,)}),
            generated_note_sequences=(output,),
            sections=(section,),
        )
        evaluation = CreationEvaluationReport(
            evaluation_id="evaluation-1",
            review_session_id="review-1",
            source_run_id="run-1",
            asset_set_digest="c" * 64,
            section_map_digest="d" * 64,
            analyzer_version="test",
        )
        before = ReviewAudioAsset(
            asset_id="before",
            asset_kind="before_full_mix",
            display_label="before",
            sha256="e" * 64,
        )
        after = ReviewAudioAsset(
            asset_id="after",
            asset_kind="after_full_mix",
            display_label="after",
            sha256="f" * 64,
        )
        comparison = RevisionComparison(
            comparison_id="comparison-1",
            before_asset=before,
            after_asset=after,
            alignment_result=AlignmentResult(state="aligned"),
        )
        session = ReviewSession(
            review_session_id="review-1",
            source_run_id="run-1",
            request=request,
            source_snapshot=snapshot,
            evaluations=(evaluation,),
            comparisons=(comparison,),
        )
        final_pass = RevisionPass(
            revision_pass_id="pass-1",
            review_session_id="review-1",
            source_evaluation_id="evaluation-1",
            revision_plan_id="plan-1",
            source_run_id="run-1",
            continuation_run_id="run-2",
            session_fingerprint="1" * 32,
            project_state_digest="2" * 64,
            status="completed",
        )
        manifest = create_delivery_manifest(
            review_session=session,
            final_revision_pass=final_pass,
            final_user_approval="user_approved",
            next_action="Listen and confirm the final bounce.",
        )
        self.assertEqual(manifest.original_brief, request.brief)
        self.assertEqual(manifest.final_revision_pass_id, "pass-1")
        self.assertEqual(manifest.final_run_id, "run-2")
        self.assertEqual(manifest.source_state_digest, "b" * 64)
        self.assertEqual(manifest.accepted_generated_outputs[0].output_id, "source-output")
        self.assertEqual(manifest.accepted_role_assignments[0].role_id, "lead")
        self.assertEqual(manifest.accepted_sections[0].section_id, "intro")
        self.assertEqual(manifest.evaluations[0].evaluation_id, "evaluation-1")
        self.assertEqual(manifest.comparisons[0].comparison_id, "comparison-1")
        self.assertEqual(manifest.approval, "user_approved")

    def test_delivery_accepts_latest_output_and_assignment_per_role(self) -> None:
        source_output = ReviewGeneratedOutput(
            output_id="lead-v1",
            output_kind="note_sequence",
            role_id="lead",
            digest="1" * 64,
        )
        revised_output = ReviewGeneratedOutput(
            output_id="lead-v2",
            output_kind="note_sequence",
            role_id="lead",
            digest="2" * 64,
        )
        source_assignment = ReviewRoleAssignment(
            assignment_id="assignment-v1",
            role_id="lead",
            preset_name="Old Lead",
        )
        revised_assignment = ReviewRoleAssignment(
            assignment_id="assignment-v2",
            role_id="lead",
            preset_name="New Lead",
        )
        manifest = create_delivery_manifest(
            source_run_id="run-1",
            review_session_id="session-1",
            accepted_generated_outputs=(source_output, revised_output),
            accepted_role_assignments=(source_assignment, revised_assignment),
            next_action="Review the latest bounce.",
        )
        self.assertEqual(
            tuple(item.output_id for item in manifest.accepted_generated_outputs),
            ("lead-v2",),
        )
        self.assertEqual(len(manifest.accepted_role_assignments), 1)
        self.assertEqual(
            manifest.accepted_role_assignments[0].preset_name,
            "New Lead",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
