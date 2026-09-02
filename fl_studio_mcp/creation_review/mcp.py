"""High-level Creation Review services used by MCP and Production Runs.

The functions in this module keep the public workflow small: start a bounded
session, attach explicit audio, evaluate it, retain structured feedback, plan
and apply one revision, compare a new bounce, and prepare delivery.  Audio
evaluation never touches FL Studio.  Revision application delegates to the
existing Production Run registry so there is no parallel mutation engine.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import Field, StrictBool, model_validator

from ..production_runs import PRODUCTION_RUNS
from .analysis import evaluate_creation
from .assets import build_review_asset_set, validate_audio_asset
from .comparison import compare_revision_bounces
from .feedback import build_feedback_locks
from .models import (
    MAX_REVIEW_ASSETS,
    MAX_REVIEW_OPERATIONS,
    MAX_REVIEW_SECTIONS,
    AcceptedElementLock,
    CreationEvaluationReport,
    CreationFeedback,
    CreationReviewModel,
    DeliveryManifest,
    ExportHandoff,
    MetricExpectation,
    PlaylistPlacement,
    ReviewAssetKind,
    ReviewAudioAsset,
    ReviewReferenceSectionPair,
    ReviewSectionRangeInput,
    ReviewSession,
    ReviewSessionRequest,
    RevisionComparison,
    RevisionOperation,
    RevisionPass,
    RevisionPlan,
    RevisionRequest,
    UserApprovalState,
    _canonical_digest,
)
from .persistence import (
    LocalReviewSessionStore,
    ReviewSessionCorruptionError,
    ReviewSessionWriteError,
)
from .revision_planner import compile_revision_plan, revision_request_digest
from .sections import build_review_section_map
from .sessions import ReviewSessionRegistry


ReviewIdentifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]


class ReviewAssetInput(CreationReviewModel):
    """One caller-selected path and its explicit review role."""

    path: str = Field(min_length=1, max_length=4096)
    asset_kind: ReviewAssetKind
    asset_id: ReviewIdentifier | None = None
    display_label: str | None = Field(default=None, min_length=1, max_length=512)
    role_id: ReviewIdentifier | None = None
    section_id: ReviewIdentifier | None = None
    revision_pass_id: ReviewIdentifier | None = None
    expected_start_seconds: float | None = Field(default=None, ge=0.0)
    declared_offset_seconds: float | None = None


class ReviewAttachAssetsRequest(CreationReviewModel):
    review_session_id: ReviewIdentifier
    assets: tuple[ReviewAssetInput, ...] = Field(
        min_length=1, max_length=MAX_REVIEW_ASSETS
    )

    @model_validator(mode="after")
    def unique_supplied_ids(self) -> "ReviewAttachAssetsRequest":
        ids = [item.asset_id for item in self.assets if item.asset_id is not None]
        if len(ids) != len(set(ids)):
            raise ValueError("supplied review asset IDs must be unique")
        return self


class ReviewEvaluateRequest(CreationReviewModel):
    review_session_id: ReviewIdentifier
    asset_set_id: ReviewIdentifier | None = None
    section_ranges: tuple[ReviewSectionRangeInput, ...] = Field(
        default=(), max_length=MAX_REVIEW_SECTIONS
    )
    reference_section_pairs: tuple[ReviewReferenceSectionPair, ...] = Field(
        default=(), max_length=MAX_REVIEW_SECTIONS
    )
    tempo_bpm: float | None = Field(default=None, gt=0.0, le=522.0)
    time_signature_numerator: int | None = Field(default=None, ge=1, le=32)
    time_signature_denominator: Literal[1, 2, 4, 8, 16, 32] | None = None
    export_offset_seconds: float = 0.0

    @model_validator(mode="after")
    def validate_ranges(self) -> "ReviewEvaluateRequest":
        section_ids = [item.section_id.casefold() for item in self.section_ranges]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("section_ranges must use unique section IDs")
        pair_keys = [
            _canonical_digest(item.model_dump(mode="json"))
            for item in self.reference_section_pairs
        ]
        if len(pair_keys) != len(set(pair_keys)):
            raise ValueError("reference_section_pairs must not contain duplicates")
        return self


class ReviewCompareRequest(CreationReviewModel):
    review_session_id: ReviewIdentifier
    before_asset_id: ReviewIdentifier
    after_asset_id: ReviewIdentifier
    revision_plan_id: ReviewIdentifier | None = None
    user_approval_state: UserApprovalState = "not_requested"

    @model_validator(mode="after")
    def different_assets(self) -> "ReviewCompareRequest":
        if self.before_asset_id == self.after_asset_id:
            raise ValueError("before and after asset IDs must differ")
        return self


class ReviewPlanRevisionRequest(CreationReviewModel):
    review_session_id: ReviewIdentifier
    request: RevisionRequest
    operations: tuple[RevisionOperation, ...] = Field(
        min_length=1, max_length=MAX_REVIEW_OPERATIONS
    )
    revision_plan_id: ReviewIdentifier = "revision-plan"
    targeted_findings: tuple[ReviewIdentifier, ...] = Field(default=(), max_length=256)
    expected_objectives: tuple[MetricExpectation, ...] = Field(default=(), max_length=32)
    subjective_objectives: tuple[str, ...] = Field(default=(), max_length=32)
    manual_actions: tuple[str, ...] = Field(default=(), max_length=64)


class ReviewApplyRevisionRequest(CreationReviewModel):
    review_session_id: ReviewIdentifier
    revision_plan_id: ReviewIdentifier
    request: RevisionRequest
    authorized_to_modify: StrictBool
    expected_session_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{32}$"
    )


class ReviewSessionLookup(CreationReviewModel):
    found: bool
    process_local: Literal[True] = True
    message: str = Field(min_length=1, max_length=512)
    session: ReviewSession | None = None

    @model_validator(mode="after")
    def found_matches_session(self) -> "ReviewSessionLookup":
        if self.found != (self.session is not None):
            raise ValueError("found must match whether a Review Session is present")
        return self


class ReviewDeleteResult(CreationReviewModel):
    review_session_id: ReviewIdentifier
    deleted: bool
    process_local: Literal[True] = True
    message: str = Field(min_length=1, max_length=512)


class ReviewDeliveryExportRequest(CreationReviewModel):
    review_session_id: ReviewIdentifier
    formats: tuple[Literal["json", "markdown"], ...] = ("json", "markdown")
    output_directory: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def validate_formats(self) -> "ReviewDeliveryExportRequest":
        if not self.formats:
            raise ValueError("at least one delivery-manifest format is required")
        if len(self.formats) != len(set(self.formats)):
            raise ValueError("delivery-manifest formats must not contain duplicates")
        return self


class ReviewDeliveryExportResult(CreationReviewModel):
    review_session_id: ReviewIdentifier
    delivery_id: ReviewIdentifier
    # ``digest`` remains the deterministic logical-manifest identity.  The
    # explicit artifact hashes below prove the exact bytes written.
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    json_path: str | None = Field(default=None, max_length=4096)
    markdown_path: str | None = Field(default=None, max_length=4096)
    json_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    markdown_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    project_saved: Literal[False] = False


def _source_lookup(run_id: str) -> object | None:
    snapshot = PRODUCTION_RUNS.snapshot(run_id)
    return snapshot if snapshot.found else None


REVIEW_SESSIONS = ReviewSessionRegistry(
    source_lookup=_source_lookup,
    # Constructing the store is read-only.  Keeping it attached from process
    # start lets persisted sessions be recovered before another session is
    # created; files/directories are only created when persistence is used.
    store=LocalReviewSessionStore(),
)


def _require_session(review_session_id: str) -> ReviewSession:
    session = REVIEW_SESSIONS.get(review_session_id)
    if session is None:
        raise ValueError(
            f"Review Session {review_session_id!r} was not found. It may have "
            "belonged to a previous MCP process unless local persistence was enabled."
        )
    return session


def _asset_set(session: ReviewSession, asset_set_id: str | None) -> object:
    if asset_set_id is None:
        if not session.asset_sets:
            raise ValueError("attach one full-mix audio asset before evaluation")
        return session.asset_sets[-1]
    for item in session.asset_sets:
        if item.asset_set_id == asset_set_id:
            return item
    raise ValueError(f"unknown review asset set: {asset_set_id}")


def _asset(
    session: ReviewSession,
    asset_id: str,
    *,
    expected_kind: ReviewAssetKind | None = None,
) -> ReviewAudioAsset:
    for item in session.assets:
        if item.asset_id == asset_id:
            if expected_kind is not None and item.asset_kind != expected_kind:
                raise ValueError(
                    f"review comparison asset {asset_id!r} must have "
                    f"asset_kind={expected_kind!r}; received {item.asset_kind!r}"
                )
            return item
    raise ValueError(f"unknown review audio asset: {asset_id}")


def _stems_for_full_mix(
    session: ReviewSession, asset_id: str
) -> tuple[ReviewAudioAsset, ...]:
    """Return only stems attached alongside the selected full-mix asset."""

    for asset_set in session.asset_sets:
        full_mix_ids = {
            item.asset_id
            for item in (
                asset_set.candidate_full_mix,
                asset_set.before_full_mix,
                asset_set.after_full_mix,
            )
            if item is not None
        }
        if asset_id in full_mix_ids:
            return (
                asset_set.synchronized_stems
                if asset_set.alignment_state == "aligned"
                else ()
            )
    return ()


def _mapping_value(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        candidate = value.to_dict()
    elif hasattr(value, "model_dump"):
        candidate = value.model_dump(mode="json", exclude_none=False)
    elif isinstance(value, Mapping):
        candidate = dict(value)
    else:
        return {}
    return dict(candidate) if isinstance(candidate, Mapping) else {}


def _palette_after_revision(
    session: ReviewSession,
    revision_pass: RevisionPass | None,
) -> dict[str, Any]:
    """Overlay verified persisted assignment outputs on the source palette."""

    source = _mapping_value(session.source_sound_palette)
    assignments = source.get("assignments", ())
    by_role: dict[str, dict[str, Any]] = {}
    if isinstance(assignments, Sequence) and not isinstance(assignments, (str, bytes)):
        for assignment in assignments:
            row = _mapping_value(assignment)
            role_id = row.get("role_id")
            if isinstance(role_id, str):
                by_role[role_id.casefold()] = row
    passes: Sequence[RevisionPass] = ()
    if revision_pass is not None:
        for index, item in enumerate(session.revision_passes):
            if item.revision_pass_id == revision_pass.revision_pass_id:
                passes = session.revision_passes[: index + 1]
                break
        else:
            passes = (revision_pass,)
    for pass_item in passes:
        for output in pass_item.generated_outputs:
            if output.output_kind != "palette_assignment":
                continue
            metadata = _mapping_value(output.metadata)
            nested = metadata.get("assignments")
            if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
                for assignment in nested:
                    row = _mapping_value(assignment)
                    role_id = row.get("role_id")
                    if isinstance(role_id, str):
                        by_role[role_id.casefold()] = row
                continue
            role_id = output.role_id or metadata.get("role_id")
            if isinstance(role_id, str):
                metadata.setdefault("role_id", role_id)
                by_role[role_id.casefold()] = metadata
    return {**source, "assignments": tuple(by_role.values())}


def _feedback_locks(session: ReviewSession) -> tuple[AcceptedElementLock, ...]:
    locks: tuple[AcceptedElementLock, ...] = ()
    for feedback in session.feedback:
        locks = build_feedback_locks(feedback, existing=locks)
    return tuple(item for item in locks if not item.released)


def _current_note_outputs(session: ReviewSession) -> tuple[Any, ...]:
    """Return the latest persisted sequence per output and role identity."""

    by_output = {item.output_id: item for item in session.source_note_sequences}
    role_to_output = {
        item.role_id.casefold(): item.output_id
        for item in session.source_note_sequences
        if item.role_id is not None
    }
    for revision_pass in session.revision_passes:
        for item in revision_pass.generated_outputs:
            if item.output_kind != "note_sequence":
                continue
            if item.role_id is not None:
                old_output = role_to_output.get(item.role_id.casefold())
                if old_output is not None:
                    by_output.pop(old_output, None)
                role_to_output[item.role_id.casefold()] = item.output_id
            by_output[item.output_id] = item
    return tuple(by_output.values())


def _current_generated_outputs(session: ReviewSession) -> tuple[Any, ...]:
    """Return the latest accepted output for each output/role identity."""

    by_output: dict[str, Any] = {}
    by_role_kind: dict[tuple[str, str], str] = {}
    rows = (
        *session.source_note_sequences,
        *session.source_processing_receipts,
        *(
            output
            for revision_pass in session.revision_passes
            for output in revision_pass.generated_outputs
        ),
    )
    for item in rows:
        role_id = item.role_id
        if role_id is not None:
            identity = (item.output_kind, role_id.casefold())
            prior_id = by_role_kind.get(identity)
            if prior_id is not None and prior_id != item.output_id:
                by_output.pop(prior_id, None)
            by_role_kind[identity] = item.output_id
        by_output[item.output_id] = item
    return tuple(by_output.values())


def _playlist_after_revision(
    session: ReviewSession,
) -> tuple[tuple[PlaylistPlacement, ...], tuple[PlaylistPlacement, ...]]:
    """Overlay exact manual revision rows while retaining the source handoff."""

    source = tuple(session.source_pattern_plan)
    current = list(source)
    revision_rows: list[PlaylistPlacement] = []

    def match_index(placement: PlaylistPlacement) -> int | None:
        for index, existing in enumerate(current):
            if (
                placement.handoff_item_id is not None
                and placement.handoff_item_id == existing.handoff_item_id
            ):
                return index
        if placement.replacement_vs_addition != "replacement":
            return None
        for index, existing in enumerate(current):
            same_pattern = any(
                left is not None and left == right
                for left, right in (
                    (placement.pattern_id, existing.pattern_id),
                    (placement.pattern_number, existing.pattern_number),
                    (placement.pattern_name, existing.pattern_name),
                )
            )
            if (
                same_pattern
                and placement.section_id == existing.section_id
                and placement.intended_playlist_track_number
                == existing.intended_playlist_track_number
                and placement.layer_order == existing.layer_order
            ):
                return index
        return None

    for revision_pass in session.revision_passes:
        for output in revision_pass.generated_outputs:
            if output.output_kind != "handoff":
                continue
            metadata = _mapping_value(output.metadata)
            placements = metadata.get("placements", ())
            if not isinstance(placements, Sequence) or isinstance(
                placements, (str, bytes)
            ):
                continue
            for value in placements:
                try:
                    placement = (
                        value
                        if isinstance(value, PlaylistPlacement)
                        else PlaylistPlacement.model_validate(value, strict=False)
                    )
                except (TypeError, ValueError):
                    continue
                index = match_index(placement)
                if index is not None:
                    existing = current[index]
                    if placement.handoff_item_id != existing.handoff_item_id:
                        placement = placement.model_copy(
                            update={"handoff_item_id": existing.handoff_item_id}
                        )
                    current[index] = placement
                else:
                    current.append(placement)
                revision_rows.append(placement)
    return tuple(current), tuple(revision_rows)


def _sequence_digests(session: ReviewSession) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in _current_note_outputs(session):
        if item.digest is None:
            continue
        result[item.output_id] = item.digest
        if item.role_id is not None:
            result[item.role_id] = item.digest
    return result


def _sequences(session: ReviewSession) -> dict[str, object]:
    result: dict[str, object] = {}
    for item in _current_note_outputs(session):
        result[item.output_id] = item
        if item.role_id is not None:
            result[item.role_id] = item
    return result


def _palette_assignment_ids(session: ReviewSession) -> tuple[str, ...]:
    raw = _palette_after_revision(
        session, session.revision_passes[-1] if session.revision_passes else None
    )
    assignments = raw.get("assignments", ())
    result: list[str] = []
    if isinstance(assignments, (list, tuple)):
        for item in assignments:
            if isinstance(item, dict):
                for key in ("assignment_id", "role_id"):
                    value = item.get(key)
                    if isinstance(value, str) and value not in result:
                        result.append(value)
    return tuple(result)


def _palette_assignments(session: ReviewSession) -> dict[str, object]:
    raw = _palette_after_revision(
        session, session.revision_passes[-1] if session.revision_passes else None
    )
    assignments = raw.get("assignments", ())
    result: dict[str, object] = {}
    if not isinstance(assignments, (list, tuple)):
        return result
    for item in assignments:
        if not isinstance(item, dict):
            continue
        assignment_id = item.get("assignment_id")
        role_id = item.get("role_id")
        if isinstance(assignment_id, str):
            result[assignment_id] = item
        if isinstance(role_id, str):
            result[role_id] = item
    return result


def _palette_digest(session: ReviewSession) -> str | None:
    if session.revision_passes:
        return _canonical_digest(
            _palette_after_revision(session, session.revision_passes[-1])
        )
    for name in ("digest", "palette_digest", "plan_digest"):
        value = session.source_sound_palette.get(name)
        if isinstance(value, str) and len(value) == 64:
            return value
    return None


def _target_fingerprints(session: ReviewSession) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, assignment in _palette_assignments(session).items():
        if not isinstance(assignment, dict):
            continue
        fingerprint = assignment.get("target_fingerprint")
        if isinstance(fingerprint, str) and len(fingerprint) == 64:
            result[key] = fingerprint
            for identity_name in ("assignment_id", "role_id"):
                identity = assignment.get(identity_name)
                if isinstance(identity, str):
                    result[identity] = fingerprint
    return result


def _processing_plans(session: ReviewSession) -> dict[str, object]:
    """Recover typed persisted ProcessingPlans without trusting receipts."""

    from ..creation_pipeline.processing import ProcessingPlan

    result: dict[str, object] = {}
    outputs = [*session.source_processing_receipts]
    outputs.extend(
        output
        for revision_pass in session.revision_passes
        for output in revision_pass.generated_outputs
        if output.output_kind == "processing_plan"
    )
    for item in outputs:
        payload = item.metadata.to_dict()
        try:
            plan = ProcessingPlan.model_validate(payload, strict=False)
        except (TypeError, ValueError):
            continue
        result[item.output_id] = plan
        result[plan.plan_id] = plan
    return result


def _effect_controls(processing_plans: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for plan in processing_plans.values():
        for action in getattr(plan, "actions", ()):
            control = getattr(getattr(action, "resolution", None), "control", None)
            control_id = getattr(control, "control_id", None)
            if isinstance(control_id, str):
                result[control_id] = control
    return result


def review_start(request: ReviewSessionRequest) -> ReviewSession:
    """Open a bounded Review Session from one completed Production Run."""

    return REVIEW_SESSIONS.create(request)


def review_attach_assets(request: ReviewAttachAssetsRequest) -> ReviewSession:
    """Validate explicit audio paths and attach one coherent asset set."""

    session = _require_session(request.review_session_id)
    assets = tuple(
        validate_audio_asset(
            item.path,
            asset_kind=item.asset_kind,
            asset_id=item.asset_id,
            display_label=item.display_label,
            role_id=item.role_id,
            section_id=item.section_id,
            source_run_id=session.source_run_id,
            revision_pass_id=item.revision_pass_id,
            expected_start_seconds=item.expected_start_seconds,
            declared_offset_seconds=item.declared_offset_seconds,
        )
        for item in request.assets
    )
    asset_set = build_review_asset_set(
        assets,
        source_run_id=session.source_run_id,
        persist_asset_paths=session.request.persist_asset_paths,
    )
    REVIEW_SESSIONS.attach_asset_set(
        session.review_session_id, asset_set, validate=False
    )
    current = _require_session(session.review_session_id)
    if current.status == "created":
        current = REVIEW_SESSIONS.transition(
            current.review_session_id,
            "awaiting_assets",
            next_action="Evaluate the attached full-mix bounce.",
        )
    return current


def review_evaluate(request: ReviewEvaluateRequest) -> CreationEvaluationReport:
    """Evaluate one attached bounce without enabling writes or mutating FL."""

    session = _require_session(request.review_session_id)
    selected = _asset_set(session, request.asset_set_id)
    if session.status not in {"evaluating", "evaluated"}:
        REVIEW_SESSIONS.transition(
            session.review_session_id,
            "evaluating",
            next_action="Analyze the selected bounce.",
        )
    section_map = build_review_section_map(
        session.source_snapshot,
        section_ranges=request.section_ranges or None,
        tempo_bpm=request.tempo_bpm,
        time_signature_numerator=request.time_signature_numerator,
        time_signature_denominator=request.time_signature_denominator,
        export_offset_seconds=request.export_offset_seconds,
    )
    policy = session.request.evaluation_policy.model_dump(mode="python")
    report = evaluate_creation(
        selected,
        section_map=section_map,
        source_run=session.source_snapshot,
        review_session_id=session.review_session_id,
        source_run_id=session.source_run_id,
        user_feedback=session.feedback,
        requested_focus=session.request.requested_focus,
        reference_goals=session.request.reference_goals,
        reference_section_pairs=(
            request.reference_section_pairs
            or session.request.reference_section_pairs
        ),
        analysis_policy=policy,
        max_findings=session.request.evaluation_policy.max_findings,
    )
    REVIEW_SESSIONS.set_section_map(session.review_session_id, section_map)
    REVIEW_SESSIONS.add_evaluation(session.review_session_id, report)
    return report


def review_get(review_session_id: str) -> ReviewSessionLookup:
    """Return current Review Session state and its next action."""

    session = REVIEW_SESSIONS.get(review_session_id)
    if session is None:
        return ReviewSessionLookup(
            found=False,
            message=(
                f"Review Session {review_session_id!r} was not found. It may have "
                "belonged to a previous MCP process unless persistence was enabled."
            ),
        )
    return ReviewSessionLookup(
        found=True,
        message=f"Review Session {review_session_id} is {session.status}.",
        session=session,
    )


def review_record_feedback(feedback: CreationFeedback) -> ReviewSession:
    """Store explicit structured producer feedback; silence is never approval."""

    if feedback.review_session_id is None:
        raise ValueError("feedback must identify its Review Session")
    return REVIEW_SESSIONS.add_feedback(feedback.review_session_id, feedback)


def review_plan_revision(request: ReviewPlanRevisionRequest) -> RevisionPlan:
    """Compile and fully validate one closed, traceable revision plan."""

    session = _require_session(request.review_session_id)
    if session.status in {"accepted", "completed", "stopped"}:
        raise ValueError(
            f"Review Session {session.review_session_id} is {session.status}; no further revision is pending"
        )
    if request.request.source_run_id != session.source_run_id:
        raise ValueError("revision request belongs to a different source run")
    if not any(
        item.evaluation_id == request.request.source_evaluation_id
        for item in session.evaluations
    ):
        raise ValueError("revision request references an unknown evaluation")
    lock_rows = [*_feedback_locks(session)]
    by_id = {item.lock_id: item for item in lock_rows}
    for lock in request.request.accepted_element_locks:
        prior = by_id.get(lock.lock_id)
        if prior is not None and prior != lock:
            raise ValueError(
                f"accepted lock {lock.lock_id!r} conflicts with explicit Review Session feedback"
            )
        if prior is None:
            lock_rows.append(lock)
            by_id[lock.lock_id] = lock
    locks = tuple(lock_rows)
    revision_request = request.request.model_copy(
        update={"accepted_element_locks": locks}
    )
    finding_ids = tuple(
        finding.finding_id
        for report in session.evaluations
        for finding in report.findings
    )
    feedback_ids = tuple(item.feedback_id for item in session.feedback)
    completed_revision_operation_ids = tuple(
        receipt.operation_id
        for revision_pass in session.revision_passes
        for receipt in revision_pass.operation_receipts
        if receipt.status != "planned"
    )
    completed_revision_operations = len(
        {
            receipt.operation_id
            for revision_pass in session.revision_passes
            for receipt in revision_pass.operation_receipts
            if receipt.status != "planned"
        }
    )
    source_palette_digest = _palette_digest(session)
    processing_plans = _processing_plans(session)
    effect_controls = _effect_controls(processing_plans)
    plan = compile_revision_plan(
        revision_request,
        request.operations,
        review_session_id=session.review_session_id,
        revision_plan_id=request.revision_plan_id,
        targeted_findings=request.targeted_findings,
        protected_elements=locks,
        expected_objectives=request.expected_objectives,
        subjective_objectives=request.subjective_objectives,
        manual_actions=request.manual_actions,
        known_finding_ids=finding_ids,
        known_feedback_ids=feedback_ids,
        available_sequence_digests=_sequence_digests(session),
        source_note_sequence_digests=_sequence_digests(session),
        source_palette_assignments=_palette_assignment_ids(session),
        completed_revision_operation_ids=completed_revision_operation_ids,
        completed_revision_operations=completed_revision_operations,
        completed_revision_passes=len(session.revision_passes),
        maximum_revision_passes=session.request.max_revision_passes,
        maximum_revision_operations=session.request.max_revision_operations,
        source_palette_digest=source_palette_digest,
        target_fingerprints=_target_fingerprints(session),
        available_effect_controls=tuple(effect_controls),
    )
    REVIEW_SESSIONS.add_revision_plan(session.review_session_id, plan)
    return plan


def review_apply_revision(request: ReviewApplyRevisionRequest) -> RevisionPass:
    """Apply one validated revision through the existing Production Run engine."""

    session = _require_session(request.review_session_id)
    if session.status in {"accepted", "completed", "stopped"}:
        raise ValueError(
            f"Review Session {session.review_session_id} is {session.status}; no further revision is pending"
        )
    if request.authorized_to_modify != request.request.authorized_to_modify:
        raise ValueError(
            "current authorization must match RevisionRequest.authorized_to_modify"
        )
    if not request.authorized_to_modify:
        raise ValueError(
            "this revision call is not authorized to modify the open project"
        )
    plan = next(
        (
            item
            for item in session.revision_plans
            if item.revision_plan_id == request.revision_plan_id
        ),
        None,
    )
    if plan is None:
        raise ValueError("revision plan is not recorded in this Review Session")
    if request.request.source_run_id != session.source_run_id:
        raise ValueError("revision request belongs to a different source run")
    if request.request.source_evaluation_id != plan.source_evaluation_id:
        raise ValueError("revision request belongs to a different source evaluation")
    request_locks = [*_feedback_locks(session)]
    lock_by_id = {item.lock_id: item for item in request_locks}
    for lock in request.request.accepted_element_locks:
        prior = lock_by_id.get(lock.lock_id)
        if prior is not None and prior != lock:
            raise ValueError(
                f"accepted lock {lock.lock_id!r} conflicts with current Review Session feedback"
            )
        if prior is None:
            request_locks.append(lock)
            lock_by_id[lock.lock_id] = lock
    bound_request = request.request.model_copy(
        update={"accepted_element_locks": tuple(request_locks)}
    )
    if plan.revision_request_digest is None:
        raise ValueError(
            "revision plan lacks its request binding; create a new revision plan before applying"
        )
    if revision_request_digest(bound_request) != plan.revision_request_digest:
        raise ValueError(
            "revision request no longer matches the validated plan; create a new revision plan"
        )
    source = PRODUCTION_RUNS.snapshot(session.source_run_id)
    source_state = source.state
    continuity_state = source_state
    persisted_pass = session.revision_passes[-1] if session.revision_passes else None
    if session.revision_passes:
        prior_run_id = session.revision_passes[-1].source_production_run_id
        if prior_run_id is not None:
            prior_run = PRODUCTION_RUNS.snapshot(prior_run_id)
            if prior_run.state is not None:
                continuity_state = prior_run.state
    source_session = (
        persisted_pass.session_fingerprint
        if continuity_state is None
        and persisted_pass is not None
        and persisted_pass.session_fingerprint is not None
        else session.source_snapshot.session_fingerprint
        if continuity_state is None and session.source_snapshot is not None
        else None
        if continuity_state is None
        else continuity_state.session_fingerprint
    )
    if (
        request.expected_session_fingerprint is not None
        and source_session is not None
        and request.expected_session_fingerprint != source_session
    ):
        raise ValueError("expected FL session does not match the source run snapshot")
    processing_plans = _processing_plans(session)
    context = {
        "review_session_id": session.review_session_id,
        "source_run_id": session.source_run_id,
        "source_evaluation_id": plan.source_evaluation_id,
        "session_fingerprint": request.expected_session_fingerprint or source_session,
        "project_state_digest": (
            persisted_pass.project_state_digest
            if continuity_state is None
            and persisted_pass is not None
            and persisted_pass.project_state_digest is not None
            else session.source_snapshot.source_state_digest
            if continuity_state is None and session.source_snapshot is not None
            else None
            if continuity_state is None
            else continuity_state.project_state_digest
        ),
        "source_run_available": session.source_snapshot is not None,
        "source_evaluation_available": True,
        "known_finding_ids": tuple(
            finding.finding_id
            for report in session.evaluations
            for finding in report.findings
        ),
        "known_feedback_ids": tuple(item.feedback_id for item in session.feedback),
        "sequence_digests": _sequence_digests(session),
        "sequences": _sequences(session),
        "palette_assignments": _palette_assignments(session),
        "processing_plans": processing_plans,
        "effect_controls": _effect_controls(processing_plans),
        "target_fingerprints": _target_fingerprints(session),
        "palette_id": session.source_sound_palette.get("palette_id"),
        "palette_digest": _palette_digest(session),
        "completed_revision_operation_ids": tuple(
            receipt.operation_id
            for revision_pass in session.revision_passes
            for receipt in revision_pass.operation_receipts
            if receipt.status != "planned"
        ),
        "completed_revision_operations": len(
            {
                receipt.operation_id
                for revision_pass in session.revision_passes
                for receipt in revision_pass.operation_receipts
                if receipt.status != "planned"
            }
        ),
        "completed_revision_passes": len(session.revision_passes),
        "maximum_revision_passes": session.request.max_revision_passes,
        "maximum_revision_operations": session.request.max_revision_operations,
    }
    from .revision_executor import RevisionExecutor

    REVIEW_SESSIONS.transition(
        session.review_session_id,
        "revising",
        next_action="Apply the validated bounded revision pass.",
    )
    result = RevisionExecutor(run_registry=PRODUCTION_RUNS).apply(
        plan,
        bound_request,
        context,
        current_authorization=request.authorized_to_modify,
    )
    try:
        REVIEW_SESSIONS.add_revision_pass(session.review_session_id, result)
        return result
    except (ReviewSessionWriteError, ReviewSessionCorruptionError, OSError) as exc:
        blocker = (
            "Revision execution finished, but its durable Review Session receipt "
            "could not be written; the receipt is process-local and no replay was attempted."
        )
        recovered = result.model_copy(
            update={
                "status": "blocked",
                "warnings": tuple(dict.fromkeys((*result.warnings, blocker))),
                "blockers": tuple(dict.fromkeys((*result.blockers, blocker))),
            }
        )
        REVIEW_SESSIONS.record_revision_pass_after_persistence_failure(
            session.review_session_id,
            recovered,
            persistence_error=exc,
        )
        return recovered


def review_compare(request: ReviewCompareRequest) -> RevisionComparison:
    """Compare aligned before/after exports without mutating FL Studio."""

    session = _require_session(request.review_session_id)
    before = _asset(
        session,
        request.before_asset_id,
        expected_kind="before_full_mix",
    )
    after = _asset(
        session,
        request.after_asset_id,
        expected_kind="after_full_mix",
    )
    requested_plan = None
    if request.revision_plan_id is not None:
        requested_plan = next(
            (
                item
                for item in session.revision_plans
                if item.revision_plan_id == request.revision_plan_id
            ),
            None,
        )
        if requested_plan is None:
            raise ValueError("comparison references an unknown revision plan")
    if after.revision_pass_id is None:
        raise ValueError(
            "after_full_mix asset must declare the revision pass ID produced by its bounce"
        )
    revision_pass = next(
        (
            item
            for item in reversed(session.revision_passes)
            if item.revision_pass_id == after.revision_pass_id
        ),
        None,
    )
    if revision_pass is None:
        raise ValueError(
            "after_full_mix asset references an unknown revision pass"
        )
    if revision_pass.review_session_id != session.review_session_id:
        raise ValueError(
            "after_full_mix revision pass belongs to a different Review Session"
        )
    if revision_pass.source_run_id != session.source_run_id:
        raise ValueError(
            "after_full_mix revision pass belongs to a different source run"
        )
    if revision_pass.after_bounce_state not in {"attached", "compared"}:
        raise ValueError(
            "after_full_mix must be attached to the revision pass before comparison"
        )
    plan = next(
        (
            item
            for item in session.revision_plans
            if item.revision_plan_id == revision_pass.revision_plan_id
        ),
        None,
    )
    if plan is None:
        raise ValueError(
            "after_full_mix revision pass references an unknown revision plan"
        )
    if requested_plan is not None:
        if requested_plan.revision_plan_id != revision_pass.revision_plan_id:
            raise ValueError(
                "after_full_mix revision pass does not match the selected revision plan"
            )
    explicit_approval = _explicit_user_approval(session)
    requested_approval = request.user_approval_state
    approval_aliases = {
        "approved": "user_approved",
        "rejected": "user_rejected",
    }
    normalized_request = approval_aliases.get(
        requested_approval, requested_approval
    )
    if requested_approval not in {"not_requested", "pending", "unknown"} and (
        normalized_request != explicit_approval
    ):
        raise ValueError(
            "record the producer's explicit approval or revision decision before attaching it to a comparison"
        )
    comparison = compare_revision_bounces(
        before,
        after,
        section_map=session.section_map,
        expected_objectives=() if plan is None else plan.expected_objectives,
        before_stems=_stems_for_full_mix(session, before.asset_id),
        after_stems=_stems_for_full_mix(session, after.asset_id),
        accepted_element_locks=_feedback_locks(session),
        before_generated_outputs=session.source_note_sequences,
        after_generated_outputs=(
            () if revision_pass is None else revision_pass.generated_outputs
        ),
        before_palette=session.source_sound_palette,
        after_palette=_palette_after_revision(session, revision_pass),
        user_approval_state=explicit_approval,
        expected_revision_pass_id=(
            None if revision_pass is None else revision_pass.revision_pass_id
        ),
    )
    REVIEW_SESSIONS.add_comparison(
        session.review_session_id,
        comparison,
        revision_pass_id=revision_pass.revision_pass_id,
        revision_plan_id=plan.revision_plan_id,
    )
    return comparison


def review_export_handoff(review_session_id: str) -> ExportHandoff:
    """Return the single precise next export requested for this session."""

    session = _require_session(review_session_id)
    from .delivery import create_export_handoff

    findings = session.evaluations[-1].findings if session.evaluations else ()
    sections = session.section_map.sections if session.section_map is not None else session.source_sections
    start_bar = min((item.start_bar for item in sections), default=None)
    end_bar = max((item.end_bar for item in sections), default=None)
    start_seconds = min((item.start_seconds for item in sections), default=None)
    end_seconds = max((item.end_seconds for item in sections), default=None)
    revision_number = len(session.revision_passes)
    label = "After" if revision_number else "Before"
    return create_export_handoff(
        handoff_id=f"export-{session.review_session_id}-{revision_number + 1}",
        recommended_filename=f"PF_Review_{revision_number + 1:02d}_{label}.wav",
        findings=findings,
        exact_start_bar=start_bar,
        exact_end_bar=end_bar,
        exact_start_seconds=start_seconds,
        exact_end_seconds=end_seconds,
        next_action=(
            "Export this full mix with normalization off and matching settings, then attach it to this Review Session."
        ),
    )


def _explicit_user_approval(session: ReviewSession) -> UserApprovalState:
    """Return the newest explicit producer decision, never a metric inference."""

    for feedback in reversed(session.feedback):
        if feedback.source != "user_explicit":
            continue
        if (
            feedback.overall_verdict in {"user_rejected", "rejected"}
            or feedback.approval_level == "rejected"
        ):
            return "user_rejected"
        if feedback.overall_verdict == "needs_revision":
            return "needs_revision"
        if feedback.overall_verdict == "user_confirmed_draft" or feedback.approval_level == "draft":
            return "user_confirmed_draft"
        if (
            feedback.overall_verdict in {"user_approved", "approved", "accepted"}
            or feedback.approval_level in {"final", "approved"}
        ):
            return "user_approved"
    return "pending"


def delivery_manifest(review_session_id: str) -> DeliveryManifest:
    """Build the current read-only delivery view without saving the project."""

    session = _require_session(review_session_id)
    from .delivery import create_delivery_manifest, create_playlist_handoff

    final_pass = session.revision_passes[-1] if session.revision_passes else None
    accepted_palette = _palette_after_revision(session, final_pass)
    accepted_assignments = accepted_palette.get("assignments", ())
    if not isinstance(accepted_assignments, Sequence) or isinstance(
        accepted_assignments, (str, bytes)
    ):
        accepted_assignments = ()
    accepted_sections = (
        session.section_map.sections
        if session.section_map is not None and session.section_map.sections
        else session.source_sections
    )
    manual_by_id = {
        item.action_id: item
        for item in (
            *session.source_manual_handoffs,
            *(
                handoff
                for revision_pass in session.revision_passes
                for handoff in revision_pass.manual_handoffs
            ),
        )
    }
    current_placements, revision_placements = _playlist_after_revision(session)
    playlist = create_playlist_handoff(
        current_placements,
        previous=session.source_pattern_plan if revision_placements else (),
        delta_only=bool(revision_placements),
    )
    export = review_export_handoff(review_session_id)
    limitations = tuple(
        dict.fromkeys(
            (
                *session.blockers,
                *session.warnings,
                *(
                    warning
                    for revision_pass in session.revision_passes
                    for warning in revision_pass.warnings
                ),
                *(
                    blocker
                    for revision_pass in session.revision_passes
                    for blocker in revision_pass.blockers
                ),
                *(
                    session.evaluations[-1].unavailable_analyses
                    if session.evaluations
                    else ()
                ),
                "PostFader does not save the FL Studio project.",
                "Playlist placement is not verifiable through the public FL Studio API.",
            )
        )
    )
    # Technical workflow completion is not artistic approval.  Only the
    # explicit accepted/rejected lifecycle states may determine this field.
    approval = _explicit_user_approval(session)
    return create_delivery_manifest(
        review_session=session,
        final_revision_pass=final_pass,
        creation_outcome=session.source_creation_outcome,
        accepted_palette=accepted_palette,
        accepted_generated_outputs=_current_generated_outputs(session),
        accepted_role_assignments=tuple(accepted_assignments),
        accepted_sections=accepted_sections,
        pattern_placements=current_placements,
        playlist_handoff=playlist,
        export_handoff=export,
        review_assets=session.assets,
        evaluations=session.evaluations,
        comparisons=session.comparisons,
        remaining_manual_actions=tuple(manual_by_id.values()),
        unresolved_limitations=limitations,
        final_user_approval=approval,
        next_action=export.next_action,
        delivery_id=f"delivery-{session.review_session_id}",
    )


def delivery_export_manifest(
    request: ReviewDeliveryExportRequest,
) -> ReviewDeliveryExportResult:
    """Create local JSON/Markdown delivery artifacts without overwriting files."""

    session = _require_session(request.review_session_id)
    from .delivery import write_delivery_manifest

    manifest = delivery_manifest(session.review_session_id)
    result = write_delivery_manifest(
        manifest,
        request.output_directory,
        write_json="json" in request.formats,
        write_markdown="markdown" in request.formats,
    )
    try:
        REVIEW_SESSIONS.set_delivery_manifest(session.review_session_id, manifest)
    except Exception:
        cleanup_failures: list[str] = []
        for path, created in (
            (result.json_path, result.json_created),
            (result.markdown_path, result.markdown_created),
        ):
            if path is None or not created:
                continue
            try:
                path.unlink()
            except OSError as cleanup_error:
                cleanup_failures.append(f"{path}: {cleanup_error}")
        if cleanup_failures:
            raise RuntimeError(
                "delivery state could not be recorded and newly created artifacts "
                "could not all be removed: " + "; ".join(cleanup_failures)
            ) from None
        raise
    return ReviewDeliveryExportResult(
        review_session_id=session.review_session_id,
        delivery_id=manifest.delivery_id,
        digest=result.digest,
        manifest_digest=result.manifest_digest,
        json_path=None if result.json_path is None else str(result.json_path),
        markdown_path=(
            None if result.markdown_path is None else str(result.markdown_path)
        ),
        json_sha256=result.json_sha256,
        markdown_sha256=result.markdown_sha256,
    )


def review_stop(review_session_id: str) -> ReviewSession:
    """Stop future review work without undoing completed project changes."""

    return REVIEW_SESSIONS.stop(review_session_id)


def review_delete(review_session_id: str, *, confirm: bool) -> ReviewDeleteResult:
    """Explicitly delete one process-local and persisted Review Session."""

    deleted = REVIEW_SESSIONS.delete(review_session_id, explicit=confirm)
    return ReviewDeleteResult(
        review_session_id=review_session_id,
        deleted=deleted,
        message=(
            "Review Session metadata was deleted; audio and FL project data were untouched."
            if deleted
            else "Review Session was not found; no audio or FL project data was changed."
        ),
    )


__all__ = [
    "REVIEW_SESSIONS",
    "ReviewApplyRevisionRequest",
    "ReviewAssetInput",
    "ReviewAttachAssetsRequest",
    "ReviewCompareRequest",
    "ReviewDeleteResult",
    "ReviewDeliveryExportRequest",
    "ReviewDeliveryExportResult",
    "ReviewEvaluateRequest",
    "ReviewPlanRevisionRequest",
    "ReviewSessionLookup",
    "delivery_export_manifest",
    "delivery_manifest",
    "review_apply_revision",
    "review_attach_assets",
    "review_compare",
    "review_delete",
    "review_evaluate",
    "review_export_handoff",
    "review_get",
    "review_plan_revision",
    "review_record_feedback",
    "review_start",
    "review_stop",
]
