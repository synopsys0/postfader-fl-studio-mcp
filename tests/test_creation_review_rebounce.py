"""Truthful lifecycle tests for revision after-bounce attachments."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import fl_studio_mcp.creation_review.mcp as review_api
from fl_studio_mcp.creation_review.models import (
    AlignmentResult,
    CreationEvaluationReport,
    ReviewAssetSet,
    ReviewAudioAsset,
    ReviewSessionRequest,
    RevisionComparison,
    RevisionPass,
    RevisionPlan,
)
from fl_studio_mcp.creation_review.sessions import (
    ReviewSessionError,
    ReviewSessionRegistry,
)


def _source() -> dict[str, object]:
    return {
        "state": {
            "run_id": "run-1",
            "status": "completed",
            "request": {
                "brief": "Review this bounded draft.",
                "completion_target": "playable draft",
            },
            "generated_outputs": (),
            "receipts": (),
        }
    }


def _asset(asset_id: str, kind: str, digest: str, *, revision_pass_id: str | None = None) -> ReviewAudioAsset:
    return ReviewAudioAsset(
        asset_id=asset_id,
        asset_kind=kind,  # type: ignore[arg-type]
        path=f"/private/{asset_id}.wav",
        display_label=asset_id,
        sha256=digest * 64,
        sample_rate_hz=48_000,
        channels=2,
        duration_seconds=4.0,
        file_size_bytes=128,
        validation_state="valid",
        revision_pass_id=revision_pass_id,
    )


class CreationReviewRebounceTests(unittest.TestCase):
    def _registry_with_pass(self) -> tuple[ReviewSessionRegistry, str]:
        registry = ReviewSessionRegistry(source_runs={"run-1": _source()})
        session = registry.create(
            ReviewSessionRequest(
                source_run_id="run-1",
                brief="Review this draft.",
                persist_session=False,
            ),
            review_session_id="review-1",
        )
        registry.transition(session.review_session_id, "evaluating")
        registry.add_evaluation(
            session.review_session_id,
            CreationEvaluationReport(
                evaluation_id="evaluation-1",
                review_session_id=session.review_session_id,
                source_run_id="run-1",
                asset_set_digest="a" * 64,
                section_map_digest="b" * 64,
                analyzer_version="test",
            ),
        )
        registry.add_revision_plan(
            session.review_session_id,
            RevisionPlan(
                revision_plan_id="plan-1",
                review_session_id=session.review_session_id,
                source_evaluation_id="evaluation-1",
                source_run_id="run-1",
            ),
        )
        registry.add_revision_plan(
            session.review_session_id,
            RevisionPlan(
                revision_plan_id="plan-2",
                review_session_id=session.review_session_id,
                source_evaluation_id="evaluation-1",
                source_run_id="run-1",
            ),
        )
        registry.transition(session.review_session_id, "revising")
        registry.add_revision_pass(
            session.review_session_id,
            RevisionPass(
                revision_pass_id="pass-1",
                review_session_id=session.review_session_id,
                source_evaluation_id="evaluation-1",
                revision_plan_id="plan-1",
                source_run_id="run-1",
                status="awaiting_rebounce",
                after_bounce_state="awaiting",
            ),
        )
        return registry, session.review_session_id

    def test_after_bounce_requires_known_pass_without_partial_attachment(self) -> None:
        registry, session_id = self._registry_with_pass()
        before = _asset("before", "before_full_mix", "a")

        for after in (
            _asset("after-no-pass", "after_full_mix", "b"),
            _asset("after-unknown-pass", "after_full_mix", "c", revision_pass_id="pass-missing"),
        ):
            with self.subTest(asset_id=after.asset_id), self.assertRaises(ReviewSessionError):
                registry.attach_asset_set(
                    session_id,
                    ReviewAssetSet(
                        asset_set_id=f"set-{after.asset_id}",
                        before_full_mix=before,
                        after_full_mix=after,
                    ),
                    validate=False,
                )
            current = registry.get(session_id)
            assert current is not None
            self.assertEqual(current.assets, ())
            self.assertEqual(current.revision_passes[0].after_bounce_state, "awaiting")

    def test_after_bounce_attachment_and_pass_checkpoint_are_atomic(self) -> None:
        registry, session_id = self._registry_with_pass()
        before = _asset("before", "before_full_mix", "a")
        after = _asset("after", "after_full_mix", "b", revision_pass_id="pass-1")

        registry.attach_asset_set(
            session_id,
            ReviewAssetSet(
                asset_set_id="asset-set-1",
                before_full_mix=before,
                after_full_mix=after,
            ),
            validate=False,
        )

        current = registry.get(session_id)
        assert current is not None
        self.assertEqual({item.asset_id for item in current.assets}, {"before", "after"})
        self.assertEqual(current.revision_passes[0].after_bounce_state, "attached")

    def test_compare_binds_after_pass_plan_and_marks_pass_compared(self) -> None:
        registry, session_id = self._registry_with_pass()
        before = _asset("before", "before_full_mix", "a")
        after = _asset("after", "after_full_mix", "b", revision_pass_id="pass-1")
        registry.attach_asset_set(
            session_id,
            ReviewAssetSet(
                asset_set_id="asset-set-1",
                before_full_mix=before,
                after_full_mix=after,
            ),
            validate=False,
        )
        comparison = RevisionComparison(
            comparison_id="comparison-1",
            before_asset=before,
            after_asset=after,
            alignment_result=AlignmentResult(state="aligned"),
        )
        with patch.object(review_api, "REVIEW_SESSIONS", registry), patch.object(
            review_api,
            "compare_revision_bounces",
            return_value=comparison,
        ):
            result = review_api.review_compare(
                review_api.ReviewCompareRequest(
                    review_session_id=session_id,
                    before_asset_id="before",
                    after_asset_id="after",
                )
            )

        self.assertEqual(result.comparison_id, "comparison-1")
        current = registry.get(session_id)
        assert current is not None
        self.assertEqual(current.revision_passes[0].after_bounce_state, "compared")
        self.assertEqual(current.revision_passes[0].revision_plan_id, "plan-1")
        self.assertEqual(len(current.comparisons), 1)
        with patch.object(review_api, "REVIEW_SESSIONS", registry):
            manifest = review_api.delivery_manifest(session_id)
        assert manifest.final_revision_pass is not None
        self.assertEqual(manifest.final_revision_pass.after_bounce_state, "compared")
        self.assertEqual(manifest.final_revision_pass_id, "pass-1")

    def test_compare_rejects_selected_plan_that_differs_from_after_pass(self) -> None:
        registry, session_id = self._registry_with_pass()
        before = _asset("before", "before_full_mix", "a")
        after = _asset("after", "after_full_mix", "b", revision_pass_id="pass-1")
        registry.attach_asset_set(
            session_id,
            ReviewAssetSet(
                asset_set_id="asset-set-1",
                before_full_mix=before,
                after_full_mix=after,
            ),
            validate=False,
        )
        with patch.object(review_api, "REVIEW_SESSIONS", registry), patch.object(
            review_api,
            "compare_revision_bounces",
            side_effect=AssertionError("mismatched plan reached comparison"),
        ), self.assertRaisesRegex(ValueError, "does not match"):
            review_api.review_compare(
                review_api.ReviewCompareRequest(
                    review_session_id=session_id,
                    before_asset_id="before",
                    after_asset_id="after",
                    revision_plan_id="plan-2",
                )
            )
        current = registry.get(session_id)
        assert current is not None
        self.assertEqual(current.revision_passes[0].after_bounce_state, "attached")
        self.assertEqual(current.comparisons, ())


if __name__ == "__main__":
    unittest.main(verbosity=2)
