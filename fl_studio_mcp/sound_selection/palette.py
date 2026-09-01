"""Deterministic palette planning, section variation, and in-memory state."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .models import (
    MAX_ROLE_COUNT,
    SoundCandidate,
    SoundInventory,
    SoundPaletteAssignment,
    SoundPalettePlan,
    SoundPaletteState,
    SoundPaletteVariation,
    SoundPaletteVariationPlan,
    SoundRoleRequest,
    SoundSelectionRequest,
    canonical_digest,
)
from .scoring import score_candidates


MAX_PALETTE_STATES = 128


def migrate_palette_assignment(value: Any) -> SoundPaletteAssignment:
    """Migrate a legacy assignment into explicit quality-era semantics."""

    if isinstance(value, SoundPaletteAssignment):
        return value
    if not isinstance(value, dict):
        return SoundPaletteAssignment.model_validate(value, strict=False)
    data = dict(value)
    if "anchor_after_selection" not in data:
        data["anchor_after_selection"] = bool(data.get("anchor", False))
    data.setdefault("preserve_across_sections", True)
    return SoundPaletteAssignment.model_validate(data, strict=False)


def migrate_palette_state(value: Any) -> SoundPaletteState:
    """Read old stored palette snapshots without treating placeholders as locks."""

    if isinstance(value, SoundPaletteState):
        return value
    if not isinstance(value, dict):
        return SoundPaletteState.model_validate(value, strict=False)
    data = dict(value)
    data["schema_version"] = "1.0"
    if "assignments" in data:
        data["assignments"] = tuple(migrate_palette_assignment(item) for item in data["assignments"])
    if "locked_assignments" not in data:
        data["locked_assignments"] = tuple(
            item.assignment_id for item in data.get("assignments", ()) if item.locked
        )
    return SoundPaletteState.model_validate(data, strict=False)


migrate_palette_record = migrate_palette_assignment

_ALLOWED_STATE_TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset(
        {"planned", "applying", "applied", "partially_applied", "failed", "superseded"}
    ),
    "applying": frozenset(
        {"applying", "applied", "partially_applied", "failed", "superseded"}
    ),
    "partially_applied": frozenset({"partially_applied", "applied", "failed", "superseded"}),
    "failed": frozenset({"failed", "superseded"}),
    # An applied base palette may later receive a section variation.  If that
    # bounded delta stops, the truthful aggregate becomes partially applied or
    # failed without rewriting any receipt from the already-applied base.
    "applied": frozenset(
        {"applied", "partially_applied", "failed", "superseded"}
    ),
    "superseded": frozenset({"superseded"}),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_request(request: SoundSelectionRequest | Any) -> SoundSelectionRequest:
    return request if isinstance(request, SoundSelectionRequest) else SoundSelectionRequest.model_validate(request)


def _coerce_inventory(inventory: SoundInventory | Sequence[SoundCandidate] | Any) -> tuple[SoundCandidate, ...]:
    if isinstance(inventory, SoundInventory):
        return inventory.candidates(include_effects=True)
    if isinstance(inventory, Sequence) and not isinstance(inventory, (str, bytes)):
        return tuple(
            item if isinstance(item, SoundCandidate) else SoundCandidate.model_validate(item)
            for item in inventory
        )
    candidate_source: Any = inventory
    if hasattr(candidate_source, "candidates"):
        rows = candidate_source.candidates()
        return tuple(item if isinstance(item, SoundCandidate) else SoundCandidate.model_validate(item) for item in rows)
    raise TypeError("inventory must be SoundInventory or a sequence of SoundCandidate records")


def _unordered_field(value: Any) -> Any:
    """Canonicalize fields whose order is not part of inventory identity."""

    if isinstance(value, list):
        return sorted(value, key=lambda item: canonical_digest(item))
    return value


def _text_sort_key(value: str) -> tuple[str, str]:
    return value.casefold(), value


def _candidate_inventory_material(candidate: SoundCandidate) -> dict[str, Any]:
    """Return observed candidate state, excluding derived ranking output."""

    material = candidate.model_dump(mode="json", exclude_none=False)
    # Scores and explanations are outputs of planning, not inventory
    # observations.  Including them would make an otherwise identical plan
    # acquire a different identity after a caller reuses scored candidates.
    for field in (
        "score",
        "score_breakdown",
        "confidence",
        "metadata_confidence",
        "role_fit_confidence",
        "preset_identity_confidence",
        "total_confidence",
        "score_margin",
        "shortlist",
        "preference_provenance",
        "disqualification_reasons",
    ):
        material.pop(field, None)
    for field in (
        "product_aliases",
        "descriptors",
        "descriptor_provenance",
        "style_tags",
        "role_ids",
        "registers",
        "articulations",
        "atlas_categories",
        "atlas_common_roles",
        "atlas_technique_ids",
        "section_scope",
        "drum_missing_roles",
        "warnings",
    ):
        if field in material:
            material[field] = _unordered_field(material[field])
    return material


def _target_inventory_material(target: Any) -> dict[str, Any]:
    material = target.model_dump(mode="json", exclude_none=False)
    for field in (
        "product_aliases",
        "descriptors",
        "descriptor_provenance",
        "style_tags",
        "role_ids",
        "registers",
        "articulations",
        "atlas_categories",
        "atlas_common_roles",
        "atlas_technique_ids",
        "section_scope",
        "warnings",
    ):
        if field in material:
            material[field] = _unordered_field(material[field])
    return material


def _inventory_fingerprint(inventory: SoundInventory | Any, candidates: Sequence[SoundCandidate]) -> str:
    """Fingerprint all relevant observations independent of enumeration order."""

    material: dict[str, Any] = {
        "session_fingerprint": (
            inventory.session_fingerprint if isinstance(inventory, SoundInventory) else None
        ),
        "candidates": sorted(
            (_candidate_inventory_material(item) for item in candidates),
            key=canonical_digest,
        ),
    }
    if isinstance(inventory, SoundInventory):
        material.update(
            {
                "current_palette_id": inventory.current_palette_id,
                "locked_roles": sorted(inventory.locked_roles, key=_text_sort_key),
                "known_unloaded_products": sorted(
                    inventory.known_unloaded_products, key=_text_sort_key
                ),
                "warnings": sorted(inventory.warnings, key=_text_sort_key),
                "target_observations": sorted(
                    (_target_inventory_material(item) for item in inventory.loaded_targets),
                    key=canonical_digest,
                ),
            }
        )
    return canonical_digest(material)


def _state_assignments(
    existing: SoundPaletteState | SoundPalettePlan | Sequence[SoundPaletteAssignment] | None,
) -> tuple[SoundPaletteAssignment, ...]:
    if existing is None:
        return ()
    if isinstance(existing, (SoundPaletteState, SoundPalettePlan)):
        return existing.assignments
    return tuple(
        item if isinstance(item, SoundPaletteAssignment) else migrate_palette_assignment(item)
        for item in existing
    )


def _anchor_after_selection(role: SoundRoleRequest) -> bool:
    """Whether a newly selected identity should become a section anchor."""

    return role.anchor_after_selection or role.role_id in {
        "main_chords",
        "main_lead",
        "primary_bass",
        "sub_bass",
        "vocal_chop",
        "drums",
    } or role.continuity_priority >= 0.70


def _is_anchor(role: SoundRoleRequest) -> bool:
    """Order lock/anchor roles first without conflating their meanings."""

    return role.lock_existing or _anchor_after_selection(role)


def _assignment_from_candidate(
    role: SoundRoleRequest,
    candidate: SoundCandidate,
    *,
    anchor: bool,
    existing: SoundPaletteAssignment | None,
    fallbacks: Sequence[SoundCandidate],
) -> SoundPaletteAssignment:
    selected = candidate.selected_preset
    current = candidate.current_preset
    if candidate.is_loop_starter:
        action = "loop_starter_reroll"
    elif selected is None or selected.casefold() == (current or "").casefold():
        action = "keep_current"
    else:
        action = "select_preset"
    return SoundPaletteAssignment(
        role_id=role.role_id,
        target=candidate.target,
        target_fingerprint=candidate.target_fingerprint,
        product_id=candidate.product_id,
        product_name=candidate.product_name,
        selected_preset=selected,
        selected_preset_index=candidate.preset_index,
        preset_identity_digest=candidate.identity_digest,
        descriptors=candidate.descriptors,
        descriptor_provenance=candidate.descriptor_provenance,
        registers=candidate.registers,
        articulations=candidate.articulations,
        envelope_behavior=candidate.envelope_behavior,
        mono_poly=candidate.mono_poly,
        known_limitations=candidate.known_limitations,
        characteristic_provenance=candidate.metadata_provenance,
        brightness=candidate.brightness,
        width=candidate.width,
        motion=candidate.motion,
        aggression=candidate.aggression,
        softness=candidate.softness,
        density=candidate.density,
        complexity=candidate.complexity,
        energy=candidate.energy,
        anchor=anchor,
        locked=role.lock_existing or (existing.locked if existing is not None else False),
        anchor_after_selection=anchor,
        preserve_across_sections=role.preserve_across_sections,
        section_scope=role.section_scope or candidate.section_scope,
        parent_assignment_id=None if existing is None else existing.assignment_id,
        selection_action=action,
        drum_map_id=candidate.drum_map_id,
        score=candidate.score,
        score_breakdown=candidate.score_breakdown,
        selection_reason=(
            f"Selected {candidate.product_name} / {selected or 'current preset'} for {role.role_id}; "
            f"user direction and role fit led the ranking while continuity/history remained bounded."
        ),
        fallback_candidate_ids=tuple(
            item.candidate_id or item.identity_digest for item in fallbacks
        ),
        required_verification=action != "keep_current",
        confidence=candidate.confidence,
        metadata_confidence=candidate.metadata_confidence,
        metadata_provenance=candidate.metadata_provenance,
        metadata_source_id=candidate.metadata_source_id,
        metadata_family_id=candidate.metadata_family_id,
        role_fit_confidence=candidate.role_fit_confidence,
        preset_identity_confidence=candidate.preset_identity_confidence,
        total_confidence=candidate.total_confidence,
        score_margin=candidate.score_margin,
        shortlist=candidate.shortlist,
        preference_provenance=candidate.preference_provenance,
        warnings=candidate.warnings,
    )


def _plan_digest(plan: SoundPalettePlan) -> str:
    return canonical_digest(
        plan.model_dump(mode="json", exclude_none=False) | {"plan_digest": None}
    )


def plan_palette(
    request: SoundSelectionRequest,
    inventory: SoundInventory | Sequence[SoundCandidate],
    *,
    existing: SoundPaletteState | SoundPalettePlan | Sequence[SoundPaletteAssignment] | None = None,
    history: Any = None,
) -> SoundPalettePlan:
    """Plan one coherent palette without changing targets or usage history."""

    request = _coerce_request(request)
    all_candidates = _coerce_inventory(inventory)
    existing_assignments = _state_assignments(existing)
    roles = tuple(request.roles)
    request_digest = canonical_digest(request.model_dump(mode="json", exclude_none=False))
    inventory_fingerprint = _inventory_fingerprint(inventory, all_candidates)
    inventory_session = (
        inventory.session_fingerprint
        if isinstance(inventory, SoundInventory)
        else None
    )
    palette_id = "palette-" + canonical_digest(
        {"request": request_digest, "inventory": inventory_fingerprint, "project": request.project_key}
    )[:24]
    if not roles:
        empty_plan = SoundPalettePlan(
            palette_id=palette_id,
            request_digest=request_digest,
            inventory_session_fingerprint=inventory_session,
            project_key=request.project_key,
            policy=request.selection_policy,
            preset_discovery_coverage=(
                ()
                if not isinstance(inventory, SoundInventory)
                else inventory.preset_discovery_coverage
            ),
            blockers=("request contains no sound roles",),
            rationale="No roles were supplied; planning made no target or history changes.",
            plan_digest=None,
        )
        return empty_plan.model_copy(update={"plan_digest": _plan_digest(empty_plan)})

    ordered_roles = tuple(
        sorted(roles, key=lambda role: (not role.required, not _is_anchor(role), role.role_id))
    )
    selected_assignments: list[SoundPaletteAssignment] = []
    selected_candidates: list[SoundCandidate] = []
    # Existing roles remain physically assigned even when they are omitted
    # from a follow-up request.  Keep them in the joint conflict/cohesion
    # context so a newly requested role cannot silently reuse their target.
    preserved_context: list[SoundPaletteAssignment] = list(existing_assignments)
    blockers: list[str] = []
    warnings: list[str] = list(inventory.warnings) if isinstance(inventory, SoundInventory) else []
    conflicts: list[str] = []
    anchor_roles: list[str] = []
    flexible_roles: list[str] = []

    for role in ordered_roles:
        is_anchor_role = _is_anchor(role)
        anchor_after_selection = _anchor_after_selection(role)
        (anchor_roles if is_anchor_role else flexible_roles).append(role.role_id)
        scored = score_candidates(
            all_candidates,
            role,
            request,
            existing_assignments=existing_assignments,
            selected_candidates=(*preserved_context, *selected_candidates),
            history=history,
        )
        eligible = tuple(item for item in scored if item.eligible)
        existing_assignment = next(
            (item for item in existing_assignments if item.role_id.casefold() == role.role_id.casefold()),
            None,
        )
        if not eligible:
            message = f"no loaded candidate satisfies required role {role.role_id!r}"
            (blockers if role.required else warnings).append(message)
            continue
        winner = eligible[0].candidate
        fallback = tuple(item.candidate for item in eligible[1 : request.max_candidates_per_role])
        assignment = _assignment_from_candidate(
            role,
            winner,
            anchor=anchor_after_selection,
            existing=existing_assignment,
            fallbacks=fallback,
        )
        selected_assignments.append(assignment)
        selected_candidates.append(winner)
        if existing_assignment is not None and not assignment.locked and assignment.assignment_id != existing_assignment.assignment_id:
            conflicts.append(
                f"role {role.role_id!r} changed from its prior assignment by explicit direction"
            )

    selected_ids = {
        item.candidate_id or item.identity_digest for item in selected_candidates
    }
    unused = tuple(
        item.candidate_id or item.identity_digest
        for item in all_candidates
        if (item.candidate_id or item.identity_digest) not in selected_ids
    )
    unused_targets: list[Any] = []
    seen_targets: set[str] = set()
    for item in all_candidates:
        item_id = item.candidate_id or item.identity_digest
        if item_id in selected_ids or item.target is None:
            continue
        target_key = canonical_digest(
            item.target.model_dump(mode="json", exclude_none=False)
        )
        if target_key not in seen_targets:
            seen_targets.add(target_key)
            unused_targets.append(item.target)
    drum_map = None
    if isinstance(inventory, SoundInventory):
        for assignment in selected_assignments:
            if assignment.drum_map_id is None:
                continue
            for target in inventory.loaded_targets:
                if target.pad_map is not None and target.pad_map.map_id == assignment.drum_map_id:
                    drum_map = target.pad_map
                    break
            if drum_map is not None:
                break
    plan = SoundPalettePlan(
        palette_id=palette_id,
        request_digest=request_digest,
        inventory_session_fingerprint=inventory_session,
        project_key=request.project_key,
        policy=request.selection_policy,
        assignments=tuple(selected_assignments),
        preset_discovery_coverage=(
            ()
            if not isinstance(inventory, SoundInventory)
            else inventory.preset_discovery_coverage
        ),
        anchor_roles=tuple(anchor_roles),
        flexible_roles=tuple(flexible_roles),
        drum_map=drum_map,
        unused_candidate_ids=unused,
        unused_candidate_targets=tuple(unused_targets),
        conflicts=tuple(dict.fromkeys(conflicts)),
        blockers=tuple(dict.fromkeys(blockers)),
        warnings=tuple(dict.fromkeys(warnings)),
        rationale=(
            "Core identity roles were ranked first; existing anchors were preserved unless "
            "the request supplied explicit replacement direction. Selection uses metadata-level "
            "cohesion and bounded local history, not audio or random preset roulette."
        ),
        plan_digest=None,
    )
    return plan.model_copy(update={"plan_digest": _plan_digest(plan)})


plan_sound_palette = plan_palette
sound_selection_plan = plan_palette


def create_palette_variation(
    base: SoundPaletteState | SoundPalettePlan,
    request: SoundSelectionRequest,
    inventory: SoundInventory | Sequence[SoundCandidate],
    *,
    section: str | None = None,
    history: Any = None,
    replace_roles: Iterable[str] = (),
) -> SoundPaletteVariationPlan:
    """Create a section delta while keeping anchors stable by default."""

    request = _coerce_request(request)
    all_candidates = _coerce_inventory(inventory)
    # A variation request is explicitly about changing flexible roles.  Anchor
    # preservation is handled below per role, while disabling the broad
    # request-level preservation guard lets a flexible role receive a real
    # section delta.
    variation_request = request.model_copy(update={"preserve_existing_roles": False})
    base_assignments = base.assignments
    base_palette_id = base.palette_id
    requested_roles = {role.role_id.casefold(): role for role in request.roles}
    explicit_replacements = {item.casefold() for item in replace_roles}
    explicit_replacements.update(
        role_id
        for role_id, role in requested_roles.items()
        if role.preferred_products
        or role.preferred_presets
        or any(item.is_hard_constraint for item in role.preference_directives)
    )
    if request.product_preferences or request.preset_preferences or any(
        item.is_hard_constraint for item in request.preference_directives
    ):
        explicit_replacements.update(requested_roles)
    chosen_section = section or next(
        (scope for role in request.roles for scope in role.section_scope),
        "variation",
    )
    unchanged: list[str] = []
    deltas: list[SoundPaletteAssignment] = []
    blockers: list[str] = []
    warnings: list[str] = []
    selected_candidates: list[SoundCandidate] = []
    for assignment in base_assignments:
        role = requested_roles.get(assignment.role_id.casefold())
        is_anchor = assignment.anchor_after_selection or assignment.anchor or assignment.locked
        preserve_sections = (
            assignment.preserve_across_sections
            if role is None
            else role.preserve_across_sections
        )
        if role is None or (
            is_anchor
            and preserve_sections
            and assignment.role_id.casefold() not in explicit_replacements
        ):
            unchanged.append(assignment.role_id)
            continue
        if role is not None and not role.allow_section_variation and assignment.role_id.casefold() not in explicit_replacements:
            unchanged.append(assignment.role_id)
            continue
        scored = score_candidates(
            all_candidates,
            role,
            variation_request,
            existing_assignments=base_assignments,
            selected_candidates=(*selected_candidates, *base_assignments),
            history=history,
        )
        eligible = tuple(item for item in scored if item.eligible)
        if not eligible:
            blockers.append(f"no loaded candidate can vary role {assignment.role_id!r}")
            continue
        eligible_candidates = tuple(item.candidate for item in eligible)
        # Ranking intentionally gives continuity a strong voice.  Once this
        # function has decided that a role should vary, walk past the parent
        # identity to find the best genuinely different candidate instead of
        # returning an empty delta merely because the parent ranked first.
        winner_position = next(
            (
                index
                for index, item in enumerate(eligible_candidates)
                if _assignment_from_candidate(
                    role,
                    item,
                    anchor=False,
                    existing=assignment,
                    fallbacks=(),
                ).assignment_id
                != assignment.assignment_id
            ),
            None,
        )
        if winner_position is None:
            unchanged.append(assignment.role_id)
            continue
        winner = eligible_candidates[winner_position]
        replacement = _assignment_from_candidate(
            role,
            winner,
            anchor=False,
            existing=assignment,
            fallbacks=(
                *eligible_candidates[:winner_position],
                *eligible_candidates[winner_position + 1 : request.max_candidates_per_role],
            ),
        )
        # A variation delta must not redundantly repeat its parent assignment.
        if replacement.assignment_id == assignment.assignment_id:
            unchanged.append(assignment.role_id)
            continue
        replacement = replacement.model_copy(
            update={"section_scope": (chosen_section,), "parent_assignment_id": assignment.assignment_id}
        )
        deltas.append(replacement)
        selected_candidates.append(winner)
    for role in request.roles:
        if not any(item.role_id.casefold() == role.role_id.casefold() for item in base_assignments):
            if role.role_id.casefold() in explicit_replacements or role.allow_layering:
                scored = score_candidates(
                    all_candidates,
                    role,
                    variation_request,
                    existing_assignments=base_assignments,
                    selected_candidates=(*selected_candidates, *base_assignments),
                    history=history,
                )
                eligible = tuple(item for item in scored if item.eligible)
                if eligible:
                    deltas.append(
                        _assignment_from_candidate(
                            role,
                            eligible[0].candidate,
                            anchor=False,
                            existing=None,
                            fallbacks=tuple(item.candidate for item in eligible[1 : request.max_candidates_per_role]),
                        ).model_copy(update={"section_scope": (chosen_section,)})
                    )
                elif role.required:
                    blockers.append(f"no loaded candidate can add role {role.role_id!r}")
    request_digest = canonical_digest(request.model_dump(mode="json", exclude_none=False))
    variation_id = "variation-" + canonical_digest(
        {"base": base_palette_id, "request": request_digest, "section": chosen_section}
    )[:24]
    result = SoundPaletteVariationPlan(
        variation_id=variation_id,
        base_palette_id=base_palette_id,
        request_digest=request_digest,
        section=chosen_section,
        preserve_anchor_roles=True,
        assignments=tuple(deltas),
        preset_discovery_coverage=(
            inventory.preset_discovery_coverage
            if isinstance(inventory, SoundInventory)
            else ()
        ),
        unchanged_role_ids=tuple(dict.fromkeys(unchanged)),
        blockers=tuple(dict.fromkeys(blockers)),
        warnings=tuple(dict.fromkeys(warnings)),
        rationale=(
            "The variation keeps anchor assignments unchanged and emits only section-scoped "
            "deltas justified by role direction or layering."
        ),
    )
    return result.model_copy(update={"plan_digest": canonical_digest(result.model_dump(mode="json", exclude_none=False) | {"plan_digest": None})})


create_sound_palette_variation = create_palette_variation
sound_selection_create_variation = create_palette_variation


class SoundPalettePlanner:
    """Stateless façade for planning and section-scoped variation."""

    def __init__(self, *, history: Any = None) -> None:
        self.history = history

    def plan(
        self,
        request: SoundSelectionRequest,
        inventory: SoundInventory | Sequence[SoundCandidate],
        *,
        existing: SoundPaletteState | SoundPalettePlan | Sequence[SoundPaletteAssignment] | None = None,
        history: Any = None,
    ) -> SoundPalettePlan:
        return plan_palette(
            request,
            inventory,
            existing=existing,
            history=self.history if history is None else history,
        )

    def variation(
        self,
        base: SoundPaletteState | SoundPalettePlan,
        request: SoundSelectionRequest,
        inventory: SoundInventory | Sequence[SoundCandidate],
        *,
        section: str | None = None,
        history: Any = None,
        replace_roles: Iterable[str] = (),
    ) -> SoundPaletteVariationPlan:
        return create_palette_variation(
            base,
            request,
            inventory,
            section=section,
            history=self.history if history is None else history,
            replace_roles=replace_roles,
        )


PalettePlanner = SoundPalettePlanner


class SoundPaletteStateRegistry:
    """Bounded thread-safe in-memory registry for current palette state."""

    def __init__(self, *, max_states: int = MAX_PALETTE_STATES) -> None:
        if type(max_states) is not int or not (1 <= max_states <= MAX_PALETTE_STATES):
            raise ValueError("max_states is outside palette registry bounds")
        self.max_states = max_states
        self._lock = threading.RLock()
        self._states: dict[str, SoundPaletteState] = {}

    def register_plan(self, plan: SoundPalettePlan, *, now: datetime | None = None) -> SoundPaletteState:
        stamp = _now() if now is None else now
        locked_assignments = tuple(
            item.assignment_id for item in plan.assignments if item.locked
        )
        state = SoundPaletteState(
            palette_id=plan.palette_id,
            status="planned",
            created_at=stamp,
            updated_at=stamp,
            project_key=plan.project_key,
            session_identity=plan.inventory_session_fingerprint,
            assignments=plan.assignments,
            locked_assignments=locked_assignments,
            section_variations=plan.section_variations,
            warnings=plan.warnings,
            blockers=plan.blockers,
        )
        with self._lock:
            existing = self._states.get(plan.palette_id)
            if existing is not None:
                if (
                    existing.project_key != plan.project_key
                    or existing.session_identity
                    != plan.inventory_session_fingerprint
                    or existing.assignments != plan.assignments
                    or existing.locked_assignments != locked_assignments
                ):
                    raise ValueError(
                        "palette ID collision would overwrite existing process-local state"
                    )
                return existing
            self._states[plan.palette_id] = state
            self._prune_locked()
        return state

    @staticmethod
    def _is_append_only(previous: Sequence[Any], current: Sequence[Any]) -> bool:
        return len(current) >= len(previous) and tuple(current[: len(previous)]) == tuple(previous)

    @classmethod
    def _validate_monotonic_update(
        cls,
        previous: SoundPaletteState,
        current: SoundPaletteState,
    ) -> None:
        if current.palette_id != previous.palette_id:
            raise ValueError("palette identity is immutable")
        if (
            current.project_key != previous.project_key
            or current.session_identity != previous.session_identity
            or current.created_at != previous.created_at
            or current.assignments != previous.assignments
            or current.locked_assignments != previous.locked_assignments
        ):
            raise ValueError("palette identity and assignments are immutable")
        if current.updated_at < previous.updated_at:
            raise ValueError("palette updated_at cannot move backwards")
        allowed = _ALLOWED_STATE_TRANSITIONS.get(previous.status, frozenset())
        if current.status not in allowed:
            raise ValueError(
                f"palette status cannot transition from {previous.status!r} to {current.status!r}"
            )
        for field in (
            "section_variations",
            "apply_receipts",
            "accepted_feedback",
            "rejected_feedback",
            "warnings",
            "blockers",
        ):
            if not cls._is_append_only(getattr(previous, field), getattr(current, field)):
                raise ValueError(f"completed palette {field} are immutable and append-only")

    def _store_locked(self, state: SoundPaletteState) -> SoundPaletteState:
        previous = self._states.get(state.palette_id)
        if previous is not None:
            self._validate_monotonic_update(previous, state)
        self._states[state.palette_id] = state
        self._prune_locked()
        return state

    def record_variation(
        self,
        palette_id: str,
        variation: SoundPaletteVariationPlan,
        *,
        now: datetime | None = None,
    ) -> SoundPaletteState:
        """Attach one immutable section delta without replacing base assignments."""

        stamp = _now() if now is None else now
        row = SoundPaletteVariation(
            variation_id=variation.variation_id,
            section=variation.section,
            assignments=variation.assignments,
            unchanged_role_ids=variation.unchanged_role_ids,
            rationale=variation.rationale,
            blockers=variation.blockers[:32],
        )
        with self._lock:
            state = self.require(palette_id)
            prior = {
                item.variation_id: item for item in state.section_variations
            }
            existing = prior.get(row.variation_id)
            if existing is not None:
                if existing != row:
                    raise ValueError(
                        "completed section variation state is immutable"
                    )
                return state
            if len(state.section_variations) >= MAX_ROLE_COUNT:
                raise ValueError("palette section-variation bound is full")
            updated = state.model_copy(
                update={
                    "updated_at": stamp,
                    "section_variations": (*state.section_variations, row),
                }
            )
            return self._store_locked(updated)

    def put(self, state: SoundPaletteState) -> SoundPaletteState:
        with self._lock:
            return self._store_locked(state)

    def get(self, palette_id: str) -> SoundPaletteState | None:
        with self._lock:
            return self._states.get(palette_id)

    def require(self, palette_id: str) -> SoundPaletteState:
        state = self.get(palette_id)
        if state is None:
            raise KeyError(f"unknown Sound Palette {palette_id!r}")
        return state

    def current(self, project_key: str | None = None) -> SoundPaletteState | None:
        with self._lock:
            rows = tuple(self._states.values())
            if project_key is not None:
                rows = tuple(item for item in rows if item.project_key == project_key)
            return max(rows, key=lambda item: (item.updated_at, item.palette_id), default=None)

    def record_receipts(
        self,
        palette_id: str,
        receipts: Sequence[Any],
        *,
        status: str = "applied",
        now: datetime | None = None,
    ) -> SoundPaletteState:
        from .models import PaletteApplyReceipt

        if status not in {"planned", "applying", "applied", "partially_applied", "failed", "superseded"}:
            raise ValueError("invalid palette state status")
        stamp = _now() if now is None else now
        parsed = tuple(
            receipt if isinstance(receipt, PaletteApplyReceipt) else PaletteApplyReceipt.model_validate(receipt)
            for receipt in receipts
        )
        with self._lock:
            state = self.require(palette_id)
            prior = {item.assignment_id: item for item in state.apply_receipts}
            for receipt in parsed:
                if receipt.assignment_id in prior and prior[receipt.assignment_id] != receipt:
                    raise ValueError("completed palette receipts are immutable")
            merged = tuple(
                [*state.apply_receipts]
                + [item for item in parsed if item.assignment_id not in prior]
            )
            if len(merged) > MAX_ROLE_COUNT:
                raise ValueError("palette apply-receipt bound is full")
            updated = state.model_copy(update={"status": status, "updated_at": stamp, "apply_receipts": merged})
            return self._store_locked(updated)

    def _prune_locked(self) -> None:
        if len(self._states) <= self.max_states:
            return
        ordered = sorted(self._states.values(), key=lambda item: (item.updated_at, item.palette_id), reverse=True)
        self._states = {item.palette_id: item for item in ordered[: self.max_states]}

    def all_states(self) -> tuple[SoundPaletteState, ...]:
        with self._lock:
            return tuple(sorted(self._states.values(), key=lambda item: (item.updated_at, item.palette_id), reverse=True))


PaletteStateRegistry = SoundPaletteStateRegistry
SoundPaletteRegistry = SoundPaletteStateRegistry


__all__ = [
    "MAX_PALETTE_STATES",
    "PalettePlanner",
    "PaletteStateRegistry",
    "SoundPaletteRegistry",
    "SoundPalettePlanner",
    "SoundPaletteStateRegistry",
    "create_palette_variation",
    "create_sound_palette_variation",
    "plan_palette",
    "plan_sound_palette",
    "sound_selection_create_variation",
    "sound_selection_plan",
]
