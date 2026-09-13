"""Closed Creation Review revision planning and validation.

The canonical operation and plan contracts live in :mod:`creation_review.models`.
This module intentionally contains no parallel operation hierarchy: it only
compiles connected-AI input into those contracts and performs pure validation
before the existing Production Run executor is called.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .models import (
    AcceptedElementLock,
    CreationReviewModel,
    FrozenMap,
    MetricExpectation,
    RevisionOperation,
    RevisionOperationBase,
    RevisionPlan,
    RevisionRequest,
)


MAX_REVISION_BLOCKERS = 64
MAX_REVISION_WARNINGS = 64
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class RevisionPlanValidation(CreationReviewModel):
    """All independently-known blockers for one closed revision plan."""

    valid: bool
    executable: bool
    revision_plan_id: str
    plan_digest: str
    resolved_operation_order: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    changed_roles: tuple[str, ...] = ()
    changed_sections: tuple[str, ...] = ()


# These two operation kinds are local review bookkeeping.  They produce
# structured records/manual instructions and must never be sent to the
# Production Run mutation boundary or require a project-write authorization.
_LOCAL_OPERATION_KINDS = frozenset(
    {"record_feedback_lock", "create_playlist_handoff_delta"}
)


def _canonical(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _canonical(value.model_dump(mode="json", exclude_none=False))
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    return value


def revision_plan_digest(plan: RevisionPlan | Mapping[str, Any]) -> str:
    """Return the canonical digest without the self-referential fields."""

    payload = _canonical(plan)
    if isinstance(payload, dict):
        payload = dict(payload)
        payload.pop("plan_digest", None)
        payload.pop("mutations_applied", None)
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()


def revision_request_digest(request: RevisionRequest | Mapping[str, Any]) -> str:
    """Bind a plan to every request constraint except live authorization."""

    payload = _canonical(request)
    if isinstance(payload, dict):
        payload = dict(payload)
        # Authorization is asserted afresh at the mutating boundary.  It is
        # deliberately task-scoped and may be granted by a later user turn.
        payload.pop("authorized_to_modify", None)
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()


def _params(operation: Any) -> dict[str, Any]:
    value = getattr(operation, "parameters", {})
    if isinstance(value, FrozenMap):
        value = value.to_dict()
    elif isinstance(value, Mapping):
        value = dict(value)
    else:
        value = {}
    return value


def _param(operation: Any, name: str, default: Any = None) -> Any:
    return _params(operation).get(name, default)


def _role(operation: Any) -> str | None:
    value = getattr(operation, "role_id", None) or _param(operation, "role_id")
    return None if value is None else str(value)


def _section(operation: Any) -> str | None:
    value = getattr(operation, "section_id", None) or _param(operation, "section_id")
    return None if value is None else str(value)


def _source_operation_id(operation: Any) -> str | None:
    value = _param(operation, "source_operation_id")
    return None if value is None else str(value)


def _lock_blocks(lock: AcceptedElementLock, operation: Any, kind: str) -> bool:
    if lock.released or kind not in lock.lock_types:
        return False
    role = _role(operation)
    section = _section(operation)
    if lock.scope == "section" and lock.section_id not in {None, section}:
        return False
    if lock.scope in {"role", "assignment", "composition", "processing", "arrangement"}:
        if lock.role_id is not None and lock.role_id.casefold() != (role or "").casefold():
            return False
    assignment_id = _param(operation, "assignment_id")
    target_id = assignment_id or _param(operation, "target_id") or _param(operation, "source_operation_id")
    if lock.scope == "assignment" and lock.target_id is not None and lock.target_id != target_id:
        return False
    if lock.scope in {"role", "composition", "processing"} and lock.target_id is not None and lock.target_id not in {target_id, role, section}:
        return False
    return True


def _operation_lock_kind(operation: Any) -> str | None:
    kinds = _operation_lock_kinds(operation)
    return kinds[0] if kinds else None


def _operation_lock_kinds(operation: Any) -> tuple[str, ...]:
    """Return every independent lock kind a proposed operation may change."""

    name = getattr(operation, "operation", "")
    if name in {
        "change_sound_assignment",
        "create_sound_palette_variation",
        "change_drum_kit",
        "change_drum_role_mapping",
    }:
        return ("sound_assignment", "role_identity") if name == "change_drum_role_mapping" else ("sound_assignment",)
    if name in {"transform_generated_sequence", "regenerate_role_sequence", "create_section_note_variation", "change_section_voicing", "change_section_articulation", "add_supporting_layer", "remove_generated_layer"}:
        return ("note_content",)
    if name == "change_section_register":
        return ("register", "note_content")
    if name == "change_section_rhythm":
        return ("rhythm", "note_content")
    if name == "change_section_velocity":
        return ("level", "note_content")
    if name in {"change_section_density"}:
        return ("rhythm", "note_content")
    if name in {"adjust_role_level", "adjust_channel_mix"}:
        return ("level",)
    if name in {"apply_semantic_processing", "replace_processing_plan"}:
        return ("processing",)
    if name in {"update_section_markers", "create_playlist_handoff_delta"}:
        return ("section_placement",)
    return ()


def validate_revision_plan(
    plan: RevisionPlan | Mapping[str, Any],
    *,
    request: RevisionRequest | None = None,
    known_finding_ids: Sequence[str] = (),
    known_feedback_ids: Sequence[str] = (),
    available_sequence_digests: Mapping[str, str] | Sequence[str] = (),
    source_note_sequence_digests: Mapping[str, str] | None = None,
    source_palette_assignments: Sequence[str] = (),
    available_effect_controls: Sequence[str] = (),
    source_review_session_id: str | None = None,
    source_run_available: bool = True,
    source_evaluation_available: bool = True,
    completed_operation_ids: Sequence[str] = (),
    completed_revision_operation_ids: Sequence[str] = (),
    completed_revision_operations: int = 0,
    completed_revision_passes: int = 0,
    maximum_revision_passes: int | None = None,
    maximum_revision_operations: int | None = None,
    session_max_revision_operations: int | None = None,
    source_palette_digest: str | None = None,
    target_fingerprints: Mapping[str, str] | None = None,
) -> RevisionPlanValidation:
    """Validate references, scopes, locks, capabilities, and change budgets."""

    try:
        if not isinstance(plan, RevisionPlan):
            plan = RevisionPlan.model_validate(plan, strict=False)
    except Exception as exc:
        digest = hashlib.sha256(repr(plan).encode("utf-8")).hexdigest()
        return RevisionPlanValidation(
            valid=False,
            executable=False,
            revision_plan_id="invalid-plan",
            plan_digest=digest,
            blockers=(f"malformed revision plan: {exc}",),
        )

    blockers: list[str] = list(plan.blockers)
    warnings: list[str] = list(plan.warnings)
    if not source_run_available:
        blockers.append("source Production Run is unavailable")
    if not source_evaluation_available:
        blockers.append("source evaluation is unavailable")
    operations = plan.operations
    ids = [item.operation_id for item in operations]
    if len(ids) != len(set(ids)):
        blockers.append("revision operation IDs must be unique")
    position = {item_id: index for index, item_id in enumerate(ids)}
    known_findings = set(known_finding_ids)
    known_feedback = set(known_feedback_ids)
    available_digests = set(available_sequence_digests.values()) if isinstance(available_sequence_digests, Mapping) else set(available_sequence_digests)
    available_roles: set[str] = set()
    available_assignments: set[str] = set()
    for item in source_palette_assignments:
        if isinstance(item, Mapping):
            values = (item.get("role_id"), item.get("assignment_id"), item.get("id"))
        else:
            values = (
                getattr(item, "role_id", None),
                getattr(item, "assignment_id", None),
                getattr(item, "assignment_id", None),
            )
            if all(value is None for value in values):
                values = (item,)
        available_roles.update(str(value).casefold() for value in values[:1] if value is not None)
        available_assignments.update(str(value).casefold() for value in values if value is not None)
    available_controls = set(available_effect_controls)
    completed = {
        *completed_operation_ids,
        *completed_revision_operation_ids,
    }
    if type(completed_revision_operations) is not int or completed_revision_operations < 0:
        blockers.append("completed revision-operation count must be a non-negative integer")
    operation_cap = (
        maximum_revision_operations
        if maximum_revision_operations is not None
        else session_max_revision_operations
    )
    if operation_cap is not None and (
        type(operation_cap) is not int or operation_cap < 0
    ):
        blockers.append("maximum revision-operation count must be a non-negative integer")
        operation_cap = None
    if type(completed_revision_passes) is not int or completed_revision_passes < 0:
        blockers.append("completed revision-pass count must be a non-negative integer")
    elif maximum_revision_passes is not None:
        if type(maximum_revision_passes) is not int or maximum_revision_passes < 0:
            blockers.append("maximum revision-pass count must be a non-negative integer")
        elif completed_revision_passes >= maximum_revision_passes:
            blockers.append("maximum revision-pass limit exceeded")
    if operation_cap is not None and completed_revision_operations + len(operations) > operation_cap:
        blockers.append("maximum session revision-operation limit exceeded")
    if plan.mutations_applied:
        blockers.append("revision plan has already been applied")
    if known_findings and any(
        item not in known_findings for item in plan.targeted_findings
    ):
        blockers.append("revision plan targets an unknown finding")
    for index, operation in enumerate(operations):
        for dependency in operation.after:
            if dependency not in position:
                blockers.append(f"operation {operation.operation_id!r} references missing dependency {dependency!r}")
            elif position[dependency] >= index:
                blockers.append(f"operation {operation.operation_id!r} references a future operation {dependency!r}")
        if known_findings and any(item not in known_findings for item in operation.finding_ids):
            blockers.append(f"operation {operation.operation_id!r} references an unknown finding")
        if known_feedback and any(item not in known_feedback for item in operation.feedback_ids):
            blockers.append(f"operation {operation.operation_id!r} references unknown feedback")
        if operation.operation_id in completed:
            blockers.append(f"operation {operation.operation_id!r} would rewrite an already-completed receipt")
        if operation.operation in {
            "transform_generated_sequence",
            "create_section_note_variation",
            "change_section_density",
            "change_section_register",
            "change_section_voicing",
            "change_section_rhythm",
            "change_section_velocity",
            "change_section_articulation",
        } and operation.source_sequence_digest is None:
            blockers.append(
                f"operation {operation.operation_id!r} requires a stored NoteSequence digest"
            )
        if (
            source_palette_digest is not None
            and operation.source_palette_digest is not None
            and operation.source_palette_digest != source_palette_digest
        ):
            blockers.append(
                f"operation {operation.operation_id!r} source Sound Palette digest does not match"
            )
        if operation.source_palette_digest is not None and source_palette_digest is None:
            warnings.append(
                f"operation {operation.operation_id!r} has no live Sound Palette digest for continuity verification"
            )
        if request is not None:
            if request.allowed_changes and operation.operation not in request.allowed_changes:
                blockers.append(f"operation {operation.operation_id!r} is outside allowed revision changes")
            risk = str(_param(operation, "risk_level", "low"))
            if _RISK_ORDER.get(risk, 3) > _RISK_ORDER[request.maximum_risk_level]:
                blockers.append(f"operation {operation.operation_id!r} exceeds maximum revision risk")
            role = _role(operation)
            section = _section(operation)
            if request.role_scope and role and role.casefold() not in {item.casefold() for item in request.role_scope}:
                blockers.append(f"operation {operation.operation_id!r} targets a role outside revision scope")
            if request.section_scope and section and section.casefold() not in {item.casefold() for item in request.section_scope}:
                blockers.append(f"operation {operation.operation_id!r} targets a section outside revision scope")
            for kind in _operation_lock_kinds(operation):
                for lock in (*request.accepted_element_locks, *plan.protected_elements):
                    if _lock_blocks(lock, operation, kind):
                        blockers.append(f"operation {operation.operation_id!r} violates accepted lock {lock.lock_id!r}")
            assignment_id = _param(operation, "assignment_id")
            if assignment_id is not None and str(assignment_id).casefold() in {str(item).casefold() for item in request.rejected_assignments}:
                blockers.append(f"operation {operation.operation_id!r} targets a rejected palette assignment")
            if operation.operation in {"apply_semantic_processing", "replace_processing_plan"} and request.processing_policy == "preserve":
                blockers.append(f"operation {operation.operation_id!r} changes processing while processing_policy=preserve")
            if operation.operation == "replace_processing_plan" and request.processing_policy not in {"replace_allowed"}:
                blockers.append(f"operation {operation.operation_id!r} requires processing replacement allowance")
            if operation.operation == "apply_semantic_processing" and request.processing_policy == "explicit_only" and not operation.feedback_ids:
                blockers.append(f"operation {operation.operation_id!r} requires explicit processing feedback")
            if operation.operation == "regenerate_role_sequence" and request.regenerate_versus_transform == "prefer_transform":
                warnings.append(f"operation {operation.operation_id!r} regenerates despite prefer_transform")
        if operation.source_sequence_digest:
            if available_digests and operation.source_sequence_digest not in available_digests:
                blockers.append(f"operation {operation.operation_id!r} references an unavailable NoteSequence digest")
            if source_note_sequence_digests is not None:
                source_operation = _source_operation_id(operation)
                actual = source_note_sequence_digests.get(source_operation or "")
                if actual is None and operation.role_id is not None:
                    actual = source_note_sequence_digests.get(operation.role_id)
                if actual is None:
                    blockers.append(f"operation {operation.operation_id!r} source NoteSequence is unavailable")
                elif actual != operation.source_sequence_digest:
                    blockers.append(f"operation {operation.operation_id!r} source NoteSequence digest does not match")
        if operation.effect_control_id and operation.effect_control_id not in available_controls:
            blockers.append(f"operation {operation.operation_id!r} references unavailable semantic effect control")
        target = _param(operation, "target")
        target_id = _param(operation, "target_id")
        expected_target_fingerprint = _param(operation, "target_fingerprint")
        if isinstance(target, Mapping):
            target_id = target_id or target.get("target_id") or target.get("id")
            expected_target_fingerprint = (
                expected_target_fingerprint or target.get("target_fingerprint")
            )
        elif target is not None:
            target_id = target_id or getattr(target, "target_id", None) or getattr(
                target, "id", None
            )
            expected_target_fingerprint = (
                expected_target_fingerprint
                or getattr(target, "target_fingerprint", None)
            )
        if (
            target_id is not None
            and expected_target_fingerprint is not None
            and target_fingerprints is not None
            and target_fingerprints.get(str(target_id)) != expected_target_fingerprint
        ):
            blockers.append(
                f"operation {operation.operation_id!r} target fingerprint does not match"
            )
        if operation.operation in {"change_sound_assignment", "change_drum_kit", "change_drum_role_mapping"}:
            role = _role(operation)
            assignment = _param(operation, "assignment_id")
            if role and role.casefold() not in available_roles and str(assignment).casefold() not in available_assignments:
                blockers.append(f"operation {operation.operation_id!r} references a missing palette assignment")
            elif not role and str(assignment).casefold() not in available_assignments:
                blockers.append(f"operation {operation.operation_id!r} references a missing palette assignment")
        if operation.operation in {"render_project", "save_project", "create_playlist_clip", "insert_plugin"}:
            blockers.append(f"unsupported revision operation {operation.operation!r}")
        if operation.operation == "create_playlist_handoff_delta" and request is not None and not request.manual_handoff_allowance:
            blockers.append("Playlist handoff is outside the requested allowance")
        target = _param(operation, "target", {})
        target_track_index = _param(operation, "track_index", _param(operation, "mixer_track_index", None))
        target_allow_master = _param(operation, "allow_master", False)
        if isinstance(target, Mapping):
            target_track_index = target.get("track_index", target_track_index)
            target_allow_master = target.get("allow_master", target_allow_master)
        elif target is not None:
            target_track_index = getattr(target, "track_index", target_track_index)
            target_allow_master = getattr(target, "allow_master", target_allow_master)
        if operation.operation in {"adjust_channel_mix", "adjust_role_level"} and target_allow_master and request is None:
            warnings.append("Master authorization is supplied by the operation; keep this explicit in the high-level request")
        if operation.operation in {"adjust_channel_mix", "adjust_role_level"} and (
            target_track_index == 0
        ) and not target_allow_master:
            blockers.append(f"operation {operation.operation_id!r} targets Master without explicit allow_master authorization")
    roles = tuple(sorted({role for role in (_role(item) for item in operations) if role}))
    sections = tuple(sorted({section for section in (_section(item) for item in operations) if section}))
    if request is not None:
        if len(operations) > request.maximum_operations:
            blockers.append("revision operation limit exceeded")
        if len(roles) > request.maximum_changed_roles:
            blockers.append("maximum changed-role limit exceeded")
        if len(sections) > request.maximum_changed_sections:
            blockers.append("maximum changed-section limit exceeded")
        executable = request.authorized_to_modify or not any(
            item.operation not in _LOCAL_OPERATION_KINDS for item in operations
        )
        if not executable:
            warnings.append("revision plan is valid but awaits one task-scoped authorization before mutation")
        if source_review_session_id is not None and plan.review_session_id != source_review_session_id:
            blockers.append("revision plan belongs to a different Review Session")
    else:
        executable = True
    unique_blockers = tuple(dict.fromkeys(item for item in blockers if item))
    unique_warnings = tuple(dict.fromkeys(item for item in warnings if item))
    return RevisionPlanValidation(
        valid=not unique_blockers,
        executable=not unique_blockers and executable,
        revision_plan_id=plan.revision_plan_id,
        plan_digest=revision_plan_digest(plan),
        resolved_operation_order=tuple(ids),
        blockers=unique_blockers,
        warnings=unique_warnings,
        changed_roles=roles,
        changed_sections=sections,
    )


def compile_revision_plan(
    request: RevisionRequest,
    operations: Sequence[RevisionOperation | Mapping[str, Any]],
    *,
    review_session_id: str,
    revision_plan_id: str = "revision-plan",
    targeted_findings: Sequence[str] = (),
    protected_elements: Sequence[AcceptedElementLock] = (),
    expected_objectives: Sequence[MetricExpectation] = (),
    expected_measurable_movements: Sequence[MetricExpectation] = (),
    subjective_objectives: Sequence[str] = (),
    manual_actions: Sequence[str] = (),
    known_finding_ids: Sequence[str] = (),
    known_feedback_ids: Sequence[str] = (),
    available_sequence_digests: Mapping[str, str] | Sequence[str] = (),
    source_note_sequence_digests: Mapping[str, str] | None = None,
    source_palette_assignments: Sequence[str] = (),
    available_effect_controls: Sequence[str] = (),
    source_palette_digest: str | None = None,
    target_fingerprints: Mapping[str, str] | None = None,
    completed_operation_ids: Sequence[str] = (),
    completed_revision_operation_ids: Sequence[str] = (),
    completed_revision_operations: int = 0,
    completed_revision_passes: int = 0,
    maximum_revision_passes: int | None = None,
    maximum_revision_operations: int | None = None,
    session_max_revision_operations: int | None = None,
) -> RevisionPlan:
    """Build one immutable plan and raise before mutation if it is invalid."""

    parsed: list[RevisionOperation] = []
    for item in operations:
        if isinstance(item, RevisionOperationBase):
            parsed.append(item)
            continue
        candidate = {
            "revision_plan_id": revision_plan_id,
            "review_session_id": review_session_id,
            "source_evaluation_id": request.source_evaluation_id,
            "source_run_id": request.source_run_id,
            "operations": (item,),
        }
        parsed.append(RevisionPlan.model_validate(candidate, strict=False).operations[0])
    plan = RevisionPlan(
        revision_plan_id=revision_plan_id,
        review_session_id=review_session_id,
        source_evaluation_id=request.source_evaluation_id,
        source_run_id=request.source_run_id,
        revision_request_digest=revision_request_digest(request),
        operations=tuple(parsed),
        targeted_findings=tuple(targeted_findings),
        protected_elements=tuple(protected_elements or request.accepted_element_locks),
        expected_objectives=tuple(expected_objectives),
        expected_measurable_movements=tuple(expected_measurable_movements),
        subjective_objectives=tuple(subjective_objectives),
        manual_actions=tuple(manual_actions),
    )
    validation = validate_revision_plan(
        plan,
        request=request,
        known_finding_ids=known_finding_ids,
        known_feedback_ids=known_feedback_ids,
        available_sequence_digests=available_sequence_digests,
        source_note_sequence_digests=source_note_sequence_digests,
        source_palette_assignments=source_palette_assignments,
        available_effect_controls=available_effect_controls,
        source_palette_digest=source_palette_digest,
        target_fingerprints=target_fingerprints,
        source_review_session_id=review_session_id,
        completed_operation_ids=completed_operation_ids,
        completed_revision_operation_ids=completed_revision_operation_ids,
        completed_revision_operations=completed_revision_operations,
        completed_revision_passes=completed_revision_passes,
        maximum_revision_passes=maximum_revision_passes,
        maximum_revision_operations=maximum_revision_operations,
        session_max_revision_operations=session_max_revision_operations,
    )
    if not validation.valid:
        raise ValueError("invalid revision plan: " + "; ".join(validation.blockers))
    return plan.model_copy(update={"plan_digest": validation.plan_digest})


plan_revision = compile_revision_plan
assert_revision_plan_valid = validate_revision_plan


__all__ = [
    "RevisionPlanValidation",
    "assert_revision_plan_valid",
    "compile_revision_plan",
    "plan_revision",
    "revision_plan_digest",
    "revision_request_digest",
    "validate_revision_plan",
]
