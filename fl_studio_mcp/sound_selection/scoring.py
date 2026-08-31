"""Deterministic, explainable Sound Selection scoring.

The scorer is metadata-only.  It never auditions audio, calls an LLM, uses
network data, or changes a target.  Hard constraints are evaluated before
weighted ranking; bounded history can influence close choices but cannot
override a substantially better user-directed fit.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .models import (
    SoundCandidate,
    SoundPaletteAssignment,
    SoundRoleRequest,
    SoundScoreBreakdown,
    SoundScoreResult,
    SoundSelectionPolicy,
    SoundSelectionRequest,
    canonical_digest,
)


_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_EFFECT_ROLE_TYPES = {
    "effect",
    "fx_effect",
    "mixer_effect",
    "audio_effect",
    "send_effect",
    "fx",
}
_DRUM_ROLE_TYPES = {
    "drums",
    "drum",
    "drum_kit",
    "kit",
    "percussion",
}
_INSTRUMENT_ROLE_TYPES = {
    "chords",
    "lead",
    "bass",
    "sub",
    "sub_bass",
    "vocal",
    "vocal_chop",
    "instrument",
    "texture",
    "countermelody",
    "main_chords",
    "main_lead",
    "primary_bass",
    "sub_bass",
}
_ANCHOR_ROLE_IDS = {
    "main_chords",
    "main_lead",
    "primary_bass",
    "sub_bass",
    "vocal_chop",
    "drums",
}


def _tokens(value: str | None) -> frozenset[str]:
    return frozenset(_TOKEN_RE.findall((value or "").casefold()))


def _normalise(value: str | None) -> str:
    return " ".join(_TOKEN_RE.findall((value or "").casefold()))


def _descriptor_key(value: str) -> str:
    """Normalize compound descriptor spellings for semantic comparison."""

    return _normalise(value).replace(" ", "-")


def _effective_role_type(role: SoundRoleRequest) -> str:
    """Resolve common built-in role IDs when ``role_type`` stays custom."""

    role_type = _normalise(role.role_type).replace(" ", "_")
    if role_type != "custom":
        return role_type
    role_id = role.role_id.casefold().replace("-", "_")
    if role_id in _EFFECT_ROLE_TYPES | _DRUM_ROLE_TYPES | _INSTRUMENT_ROLE_TYPES:
        return role_id
    return role_type


def _is_anchor_role(role: SoundRoleRequest) -> bool:
    return (
        role.lock_existing
        or role.role_id.casefold() in _ANCHOR_ROLE_IDS
        or role.continuity_priority >= 0.70
    )


def _product_key(candidate: SoundCandidate) -> str:
    """Return the best local identity for product-level comparisons.

    Runtime inventory can be unprofiled, so ``product_id`` is optional.  A
    normalized product name remains useful as a local fallback, but it is not
    treated as Atlas ownership or installation proof.
    """

    return _normalise(candidate.product_id or candidate.product_name)


def _overlap(needles: Iterable[str], haystack: Iterable[str]) -> float:
    left = set(needles)
    right = set(haystack)
    if not left:
        return 0.0
    return len(left.intersection(right)) / len(left)


def _target_equal(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is right
    left_dump = getattr(left, "model_dump", None)
    right_dump = getattr(right, "model_dump", None)
    if callable(left_dump) and callable(right_dump):
        return left_dump(mode="json", exclude_none=False) == right_dump(
            mode="json", exclude_none=False
        )
    return left == right


def _product_labels(candidate: SoundCandidate) -> tuple[str, ...]:
    labels = [
        candidate.product_id or "",
        candidate.atlas_product_id or "",
        candidate.product_name,
        *candidate.product_aliases,
    ]
    if candidate.atlas_product is not None:
        labels.extend(
            (
                candidate.atlas_product.product_id,
                candidate.atlas_product.name,
                *candidate.atlas_product.aliases,
            )
        )
    return tuple(labels)


def product_matches(candidate: SoundCandidate, requested: str) -> bool:
    """Resolve explicit product preferences by exact normalized ID/name/alias."""

    needle = _normalise(requested)
    return bool(needle) and any(_normalise(label) == needle for label in _product_labels(candidate))


def preset_matches(candidate: SoundCandidate, requested: str) -> bool:
    return _normalise(candidate.selected_preset) == _normalise(requested)


def _descriptor_names(candidate: SoundCandidate) -> frozenset[str]:
    return frozenset(
        _descriptor_key(item)
        for item in (
            *candidate.descriptors,
            *(item.descriptor for item in candidate.descriptor_provenance),
            *candidate.style_tags,
        )
    )


def _creative_text(request: SoundSelectionRequest) -> tuple[str, ...]:
    # The brief is the user's primary free-form direction.  Keep structured
    # direction as additional evidence, but never discard the brief merely
    # because the caller did not also provide a creative-direction object.
    texts: list[str] = [request.brief]
    direction = request.creative_direction
    if isinstance(direction, str):
        texts.append(direction)
    elif direction is not None:
        texts.extend(
            item
            for item in (
                direction.genre,
                *direction.mood,
                *direction.references,
                *direction.style,
                direction.energy,
                direction.production_notes,
            )
            if item
        )
    return tuple(texts)


def _existing_for_role(
    role_id: str,
    existing: Sequence[SoundPaletteAssignment],
) -> SoundPaletteAssignment | None:
    needle = role_id.casefold()
    return next((item for item in existing if item.role_id.casefold() == needle), None)


def _explicit_change_requested(request: SoundSelectionRequest, role: SoundRoleRequest) -> bool:
    return bool(
        request.product_preferences
        or request.preset_preferences
        or role.preferred_products
        or role.preferred_presets
    )


def _hard_constraints(
    candidate: SoundCandidate,
    role: SoundRoleRequest,
    request: SoundSelectionRequest,
    existing_assignments: Sequence[SoundPaletteAssignment],
) -> tuple[str, ...]:
    """Return every hard-constraint violation in stable order."""

    reasons: list[str] = []
    if candidate.target is None:
        reasons.append("missing loaded target")

    labels = (*request.product_exclusions, *role.excluded_products)
    if any(product_matches(candidate, value) for value in labels):
        reasons.append("explicit product exclusion")
    preset_exclusions = (*request.preset_exclusions, *role.excluded_presets)
    if any(preset_matches(candidate, value) for value in preset_exclusions):
        reasons.append("explicit preset exclusion")

    product_preferences = (*request.product_preferences, *role.preferred_products)
    if product_preferences:
        # Product preferences are explicit must-use direction at this core
        # boundary.  If the requested product is not loaded, every executable
        # candidate is rejected and the planner exposes a blocker rather than
        # silently substituting an unrelated plug-in.
        if not any(product_matches(candidate, value) for value in product_preferences):
            reasons.append("explicit product preference requires another loaded product")

    preset_preferences = (*request.preset_preferences, *role.preferred_presets)
    if preset_preferences:
        if not any(preset_matches(candidate, value) for value in preset_preferences):
            reasons.append("explicit preset preference requires another loaded preset")

    if request.stock_only or not request.third_party_allowed:
        if not candidate.is_stock:
            reasons.append("stock-only policy excludes this product")

    role_type = _effective_role_type(role)
    if role_type in _EFFECT_ROLE_TYPES and candidate.target_kind != "mixer_effect":
        reasons.append("role requires a mixer-effect target")
    elif role_type in _INSTRUMENT_ROLE_TYPES or role_type in _DRUM_ROLE_TYPES:
        if candidate.target_kind != "channel_generator":
            reasons.append("instrument role requires a Channel Rack generator target")
    if candidate.target_kind == "mixer_effect" and not request.allow_effect_presets and role_type not in _EFFECT_ROLE_TYPES:
        reasons.append("effect presets are not allowed for this request")

    if role.target_candidates and not any(
        _target_equal(candidate.target, target) for target in role.target_candidates
    ):
        reasons.append("candidate target is outside the role target scope")

    if role.section_scope and candidate.section_scope:
        # Runtime inventory commonly cannot observe a section label.  An
        # explicitly reported scope remains authoritative when present, but
        # an empty observation means "unknown", not "outside the section".
        role_sections = {item.casefold() for item in role.section_scope}
        candidate_sections = {item.casefold() for item in candidate.section_scope}
        if not role_sections.issubset(candidate_sections):
            reasons.append("candidate is outside the role section scope")

    if request.source_strategy == "instrument_pool" and candidate.is_loop_starter:
        reasons.append("Loop Starter is not allowed for an original instrument-pool request")
    elif request.source_strategy == "loop_starter" and not candidate.is_loop_starter:
        reasons.append("explicit Loop Starter strategy requires a Loop Starter candidate")

    existing = _existing_for_role(role.role_id, existing_assignments)
    if existing is not None:
        same = (
            _target_equal(candidate.target, existing.target)
            and _product_key(candidate) == _normalise(existing.product_id or existing.product_name)
            and _normalise(candidate.selected_preset) == _normalise(existing.selected_preset)
            and candidate.preset_index == existing.selected_preset_index
        )
        # A caller may explicitly replace an ordinary preserved role, but a
        # role marked locked (in the request or prior palette) is a hard
        # invariant.  This keeps ``preserve_existing_roles=False`` from
        # accidentally unlocking an established assignment.
        hard_lock = role.lock_existing or existing.locked
        exploratory_flexible = (
            request.selection_policy.mode == "exploratory"
            and role.allow_section_variation
            and not _is_anchor_role(role)
        )
        preserve = hard_lock or (
            request.preserve_existing_roles
            and not _explicit_change_requested(request, role)
            and not exploratory_flexible
        )
        if preserve and not same:
            reasons.append("preserved or locked role assignment")
        if (
            role_type in _DRUM_ROLE_TYPES
            and not request.allow_drum_kit_change
            and candidate.drum_map_id != existing.drum_map_id
        ):
            reasons.append("drum-kit changes are disabled for this request")

    selected = candidate.selected_preset
    if selected is not None and _normalise(selected) != _normalise(candidate.current_preset):
        if not candidate.preset_navigation_available:
            reasons.append("preset navigation is unavailable for the requested change")

    drum_role = role_type in _DRUM_ROLE_TYPES
    if drum_role and role.required_drum_roles:
        required = {item.casefold() for item in role.required_drum_roles}
        missing = {item.casefold() for item in candidate.drum_missing_roles}
        if not candidate.pad_map_available or required.intersection(missing):
            reasons.append("required drum mapping cannot be established")

    return tuple(dict.fromkeys(reasons))


def _user_direction_fit(
    candidate: SoundCandidate,
    role: SoundRoleRequest,
    request: SoundSelectionRequest,
) -> float:
    descriptors = _descriptor_names(candidate)
    wanted = {_descriptor_key(item) for item in (*role.desired_descriptors,)}
    unwanted = {_descriptor_key(item) for item in role.undesired_descriptors}
    descriptor_match = _overlap(wanted, descriptors) if wanted else 0.0
    descriptor_penalty = _overlap(unwanted, descriptors) if unwanted else 0.0
    product_match = 0.0
    preferences = (*request.product_preferences, *role.preferred_products)
    if preferences:
        product_match = 1.0 if any(product_matches(candidate, value) for value in preferences) else 0.0
    preset_match = 0.0
    preset_preferences = (*request.preset_preferences, *role.preferred_presets)
    if preset_preferences:
        preset_match = 1.0 if any(preset_matches(candidate, value) for value in preset_preferences) else 0.0

    direction_terms = frozenset(
        token
        for text in _creative_text(request)
        for token in _tokens(text)
    )
    candidate_terms = frozenset(
        token
        for text in (
            candidate.product_name,
            candidate.product_id,
            *candidate.product_aliases,
            *candidate.descriptors,
            *candidate.style_tags,
            *candidate.role_ids,
            *candidate.articulations,
            *candidate.registers,
        )
        for token in _tokens(text)
    )
    creative_match = _overlap(direction_terms, candidate_terms) if direction_terms else 0.0

    numeric_matches: list[float] = []
    for name in (
        "brightness",
        "width",
        "motion",
        "aggression",
        "softness",
        "density",
        "complexity",
        "energy",
    ):
        requested = getattr(role, name)
        observed = getattr(candidate, name)
        if requested is not None and observed is not None:
            numeric_matches.append(1.0 - abs(requested - observed))
    numeric_fit = sum(numeric_matches) / len(numeric_matches) if numeric_matches else 0.0

    components = [descriptor_match, product_match, preset_match, creative_match, numeric_fit]
    active = sum(value > 0.0 for value in components)
    base = sum(components) / active if active else 0.45
    return max(0.0, min(1.0, base - 0.40 * descriptor_penalty))


def _role_fit(candidate: SoundCandidate, role: SoundRoleRequest) -> float:
    descriptors = _descriptor_names(candidate)
    descriptor = _overlap((_descriptor_key(item) for item in role.desired_descriptors), descriptors)
    role_terms = _tokens(role.role_id) | _tokens(role.role_type) | _tokens(role.display_name)
    known_roles = frozenset(
        token
        for value in (*candidate.role_ids, candidate.product_name)
        for token in _tokens(value)
    )
    role_label = _overlap(role_terms, known_roles) if role_terms else 0.0
    register = 0.0
    if role.register is not None:
        register = 1.0 if role.register in candidate.registers else 0.0
    articulation = 0.0
    if role.articulation is not None:
        articulation = 1.0 if any(
            _normalise(role.articulation) == _normalise(value)
            for value in candidate.articulations
        ) else 0.0
    atlas_fit = 0.0
    atlas_values = (
        *candidate.atlas_categories,
        *candidate.atlas_common_roles,
        *candidate.atlas_technique_ids,
    )
    if candidate.atlas_product is not None:
        atlas_values = (
            *atlas_values,
            *candidate.atlas_product.categories,
            *candidate.atlas_product.common_instruments,
            *candidate.atlas_product.common_track_types,
            *candidate.atlas_product.use_cases,
        )
    if atlas_values:
        atlas_terms = frozenset(
            token for value in atlas_values for token in _tokens(value)
        )
        atlas_fit = _overlap(role_terms, atlas_terms) if role_terms else 0.0
    values = [candidate.role_compatibility, descriptor, role_label, register, articulation, atlas_fit]
    active = [value for value in values if value > 0.0]
    # A candidate with no semantic metadata remains eligible, but has lower
    # suitability confidence than a role-tagged/Atlas-described candidate.
    return max(0.0, min(1.0, sum(active) / len(active) if active else 0.25))


def _assignment_matches(candidate: SoundCandidate, assignment: SoundPaletteAssignment) -> bool:
    return (
        _target_equal(candidate.target, assignment.target)
        and _product_key(candidate) == _normalise(assignment.product_id or assignment.product_name)
        and _normalise(candidate.selected_preset) == _normalise(assignment.selected_preset)
        and candidate.preset_index == assignment.selected_preset_index
    )


def _cohesion_fit(
    candidate: SoundCandidate,
    role: SoundRoleRequest,
    peers: Sequence[SoundPaletteAssignment | SoundCandidate],
) -> float:
    if not peers:
        return 0.50
    candidate_descriptors = _descriptor_names(candidate)
    candidate_styles = {item.casefold() for item in candidate.style_tags}
    scores: list[float] = []
    duplicate_penalty = 0.0
    for peer in peers:
        if isinstance(peer, SoundPaletteAssignment):
            peer_product = peer.product_id
            peer_preset = peer.selected_preset
            peer_descriptors: set[str] = set()
            peer_styles: set[str] = set()
            peer_registers: set[str] = set()
        else:
            peer_product = peer.product_id
            peer_preset = peer.selected_preset
            peer_descriptors = set(_descriptor_names(peer))
            peer_styles = {item.casefold() for item in peer.style_tags}
            peer_registers = {item.casefold() for item in peer.registers}
        candidate_product = _product_key(candidate)
        peer_product_key = _normalise(peer_product)
        if peer_product_key and candidate_product == peer_product_key and _normalise(candidate.selected_preset) == _normalise(peer_preset):
            if not role.allow_layering:
                duplicate_penalty += 0.25
        overlap = 0.0
        if candidate_descriptors and peer_descriptors:
            overlap = len(candidate_descriptors.intersection(peer_descriptors)) / len(candidate_descriptors.union(peer_descriptors))
        if candidate_styles and peer_styles:
            overlap = max(overlap, len(candidate_styles.intersection(peer_styles)) / len(candidate_styles.union(peer_styles)))
        if peer_registers and set(item.casefold() for item in candidate.registers):
            if peer_registers.isdisjoint(set(item.casefold() for item in candidate.registers)):
                overlap += 0.15
        scores.append(overlap)
    return max(-1.0, min(1.0, (sum(scores) / len(scores)) + 0.50 - duplicate_penalty))


def _selected_assignment_conflicts(
    candidate: SoundCandidate,
    role: SoundRoleRequest,
    selected: Sequence[SoundCandidate | SoundPaletteAssignment],
) -> tuple[str, ...]:
    """Reject simultaneous identities one loaded target cannot truthfully hold."""

    layering_reason = role.allow_layering or bool(role.source_roles)
    candidate_product = _product_key(candidate)
    candidate_preset = _normalise(candidate.selected_preset)
    reasons: list[str] = []
    for peer in selected:
        if isinstance(peer, SoundPaletteAssignment):
            if peer.role_id.casefold() == role.role_id.casefold():
                continue
            peer_target = peer.target
            peer_product = _normalise(peer.product_id or peer.product_name)
            peer_preset = _normalise(peer.selected_preset)
        else:
            peer_target = peer.target
            peer_product = _product_key(peer)
            peer_preset = _normalise(peer.selected_preset)
        same_target = _target_equal(candidate.target, peer_target)
        same_identity = bool(
            candidate_product
            and candidate_product == peer_product
            and candidate_preset
            and candidate_preset == peer_preset
        )
        if same_target and not (same_identity and layering_reason):
            reasons.append("loaded target is already assigned to another sound role")
        elif same_identity and not layering_reason:
            reasons.append(
                "exact product and preset are already assigned without a layering reason"
            )
    return tuple(dict.fromkeys(reasons))


def _continuity_fit(
    candidate: SoundCandidate,
    role: SoundRoleRequest,
    existing_assignments: Sequence[SoundPaletteAssignment],
    policy: SoundSelectionPolicy | None = None,
) -> float:
    existing = _existing_for_role(role.role_id, existing_assignments)
    if existing is None:
        return 0.25 if role.continuity_priority < 0.5 else 0.40
    if _assignment_matches(candidate, existing):
        if (
            policy is not None
            and policy.mode == "exploratory"
            and role.allow_section_variation
            and not _is_anchor_role(role)
        ):
            # Exploration explicitly opens flexible roles.  A negative parent
            # continuity value lets an equally good alternative win while a
            # materially better role/user fit can still prevail.
            return -0.50
        return 1.0
    if _product_key(candidate) and _product_key(candidate) == _normalise(existing.product_id or existing.product_name):
        if (
            policy is not None
            and policy.mode == "exploratory"
            and role.allow_section_variation
            and not _is_anchor_role(role)
        ):
            return 0.10
        return 0.65
    if (
        policy is not None
        and policy.mode == "exploratory"
        and role.allow_section_variation
        and not _is_anchor_role(role)
    ):
        return 0.10
    return 0.0


@dataclass(frozen=True, slots=True)
class _HistoryView:
    records: tuple[Any, ...] = ()
    feedback: tuple[Any, ...] = ()


def _history_view(history: Any) -> _HistoryView:
    if history is None:
        return _HistoryView()
    try:
        from .history import SoundHistoryDocument

        if isinstance(history, SoundHistoryDocument):
            return _HistoryView(history.records, history.feedback)
        snapshot = history.snapshot() if hasattr(history, "snapshot") else history
        if isinstance(snapshot, SoundHistoryDocument):
            return _HistoryView(snapshot.records, snapshot.feedback)
    except (TypeError, AttributeError):
        pass
    if isinstance(history, Mapping):
        return _HistoryView(
            tuple(history.get("records", ())), tuple(history.get("feedback", ()))
        )
    rows = tuple(history) if isinstance(history, Sequence) else ()
    return _HistoryView(rows, ())


def _history_fit(
    candidate: SoundCandidate,
    role: SoundRoleRequest,
    policy: SoundSelectionPolicy,
    history: Any,
) -> tuple[float, float]:
    def _fold(value: object) -> str:
        return value.casefold() if isinstance(value, str) else ""

    view = _history_view(history)
    records = [
        row
        for row in view.records
        if hasattr(row, "last_used_at")
    ]
    records.sort(key=lambda row: (row.last_used_at, row.record_id), reverse=True)
    recent = records[: policy.recent_use_window]
    digest = candidate.identity_digest.casefold()
    product = (candidate.product_id or candidate.product_name).casefold()
    role_key = role.role_id.casefold()
    exact_count = sum(
        1
        for row in recent
        if _fold(getattr(row, "preset_identity_digest", None)) == digest
        and _fold(getattr(row, "role_id", None)) == role_key
    )
    same_product_count = sum(
        1
        for row in recent
        if _fold(getattr(row, "product_id", None)) == product
        and _fold(getattr(row, "role_id", None)) == role_key
    )
    if not recent:
        exact_count = min(policy.recent_use_window, candidate.cross_project_usage)
        same_product_count = exact_count
    exact_ratio = min(1.0, exact_count / policy.recent_use_window)
    product_ratio = min(1.0, same_product_count / policy.recent_use_window)
    underused_bonus = 0.18 if exact_count == 0 else 0.0
    value = underused_bonus
    value -= policy.exact_preset_repeat_penalty * exact_ratio
    value -= policy.same_product_repeat_penalty * product_ratio

    accepted = candidate.accepted_count
    rejected = candidate.rejected_count
    for row in view.records:
        if (
            _fold(getattr(row, "product_id", None)) == product
            and _fold(getattr(row, "preset_identity_digest", None)) == digest
            and _fold(getattr(row, "role_id", None)) == role_key
        ):
            accepted += getattr(row, "accepted_count", 0)
            rejected += getattr(row, "rejected_count", 0)

    descriptor_feedback = 0.0
    role_feedback = 0.0
    candidate_descriptors = _descriptor_names(candidate)
    feedback_rows = [row for row in view.feedback if hasattr(row, "recorded_at")]
    feedback_rows.sort(
        key=lambda row: (
            (
                getattr(row, "recorded_at").isoformat()
                if hasattr(getattr(row, "recorded_at", None), "isoformat")
                else str(getattr(row, "recorded_at", ""))
            ),
            _fold(getattr(row, "feedback_id", None)),
        ),
        reverse=True,
    )
    # The document itself is bounded, and explicit feedback should remain
    # effective until the bounded store prunes it.  ``recent_use_window`` is
    # for repeat-use penalties, not a silent expiry for user verdicts.
    for row in feedback_rows:
        row_product = getattr(row, "product_id", None)
        row_digest = getattr(row, "preset_identity_digest", None)
        row_role = getattr(row, "role_id", None)
        row_role_key = _fold(row_role)
        if row_role_key and row_role_key != role_key:
            continue
        identity_match = (
            isinstance(row_product, str)
            and isinstance(row_digest, str)
            and row_product.casefold() == product
            and row_digest.casefold() == digest
        )
        if identity_match:
            accepted += 1 if row.verdict == "accepted" else 0
            rejected += 1 if row.verdict == "rejected" else 0

        # Descriptor and complete-palette feedback has no executable identity
        # by design.  It is therefore a soft signal only: match descriptors
        # (and optionally the role), then cap its total contribution below the
        # direct user-direction component.  This lets feedback adapt future
        # ranking without turning a past verdict into an absolute rule.
        descriptor_keys = {
            _descriptor_key(item)
            for item in getattr(row, "descriptors", ())
            if isinstance(item, str)
        }
        descriptor_overlap = _overlap(descriptor_keys, candidate_descriptors)
        if descriptor_overlap > 0.0:
            signal = (
                policy.accepted_choice_bonus
                if row.verdict == "accepted"
                else -policy.rejected_choice_penalty
                if row.verdict == "rejected"
                else 0.0
            )
            descriptor_feedback += signal * descriptor_overlap
        elif (
            not identity_match
            and not descriptor_keys
            and row_role_key
            and row.verdict in {"accepted", "rejected"}
        ):
            # Role-only feedback is intentionally weaker because it cannot
            # identify which sound within the role the user meant.
            role_feedback += 0.10 if row.verdict == "accepted" else -0.15

    descriptor_feedback = max(-0.35, min(0.35, descriptor_feedback))
    role_feedback = max(-0.15, min(0.15, role_feedback))
    feedback = (
        policy.accepted_choice_bonus * min(1.0, accepted)
        - policy.rejected_choice_penalty * min(1.0, rejected)
        + descriptor_feedback
        + role_feedback
    )
    return value, max(-1.0, min(1.0, feedback))


def _verification_fit(candidate: SoundCandidate) -> float:
    value = 0.0
    if candidate.target is not None:
        value += 0.25
    if candidate.product_id is not None:
        value += 0.15
    if candidate.preset_identity_stable:
        value += 0.20
    if candidate.preset_navigation_available:
        value += 0.15
    if candidate.preset_readback_available:
        value += 0.20
    if candidate.adapter_available:
        value += 0.05
    return min(1.0, value)


def _confidence(candidate: SoundCandidate, score: float) -> str:
    if candidate.preset_readback_available and candidate.preset_identity_stable and candidate.atlas_confidence == "high":
        return "high"
    if candidate.target is not None and (candidate.product_id is not None or candidate.atlas_confidence in {"medium", "high"}):
        return "medium"
    if candidate.target is not None:
        return "low"
    return "unknown"


def _candidate_rationale(
    candidate: SoundCandidate,
    role: SoundRoleRequest,
    breakdown: SoundScoreBreakdown,
    reasons: Sequence[str],
) -> str:
    if reasons:
        return f"Not eligible for {role.role_id}: " + "; ".join(reasons)
    selected = candidate.selected_preset or "current state"
    return (
        f"{candidate.product_name} / {selected} matched {role.role_id} "
        f"with user-direction {breakdown.user_direction:.2f}, role fit "
        f"{breakdown.role_fit:.2f}, continuity {breakdown.continuity:.2f}, "
        "and metadata-level palette evidence."
    )


def score_candidate(
    candidate: SoundCandidate,
    role: SoundRoleRequest,
    request: SoundSelectionRequest | None = None,
    *,
    candidates: Sequence[SoundCandidate] = (),
    existing_assignments: Sequence[SoundPaletteAssignment] = (),
    selected_candidates: Sequence[SoundCandidate | SoundPaletteAssignment] = (),
    history: Any = None,
) -> SoundScoreResult:
    """Score one candidate after applying all hard constraints."""

    if not isinstance(candidate, SoundCandidate):
        candidate = SoundCandidate.model_validate(candidate)
    if not isinstance(role, SoundRoleRequest):
        role = SoundRoleRequest.model_validate(role)
    if request is None:
        request = SoundSelectionRequest(brief=f"select {role.role_id}", roles=(role,))
    elif not isinstance(request, SoundSelectionRequest):
        request = SoundSelectionRequest.model_validate(request)
    reasons = (
        *_hard_constraints(candidate, role, request, existing_assignments),
        *_selected_assignment_conflicts(candidate, role, selected_candidates),
    )
    if reasons:
        breakdown = SoundScoreBreakdown(hard_constraints=-100.0, total=-100.0)
        scored = candidate.model_copy(
            update={
                "score": -100.0,
                "score_breakdown": breakdown,
                "confidence": _confidence(candidate, -100.0),
                "disqualification_reasons": tuple(reasons),
            }
        )
        return SoundScoreResult(
            candidate=scored,
            role_id=role.role_id,
            eligible=False,
            score=-100.0,
            breakdown=breakdown,
            rationale=_candidate_rationale(scored, role, breakdown, reasons),
            disqualification_reasons=tuple(reasons),
        )

    policy = request.selection_policy
    user = _user_direction_fit(candidate, role, request)
    role_fit = _role_fit(candidate, role)
    cohesion = _cohesion_fit(candidate, role, selected_candidates)
    continuity = _continuity_fit(candidate, role, existing_assignments, policy)
    diversity, feedback = _history_fit(candidate, role, policy, history)
    verification = _verification_fit(candidate)
    continuity_weight, novelty_weight = policy.effective_weights()
    # Role fit and direction remain the dominant terms.  History is bounded by
    # its own small policy weight and therefore cannot make an unsuitable sound
    # win solely because it is novel.
    breakdown = SoundScoreBreakdown(
        hard_constraints=0.0,
        user_direction=round(user * policy.user_direction_weight, 8),
        role_fit=round(role_fit * policy.role_fit_weight, 8),
        palette_cohesion=round(cohesion * policy.palette_cohesion_weight, 8),
        continuity=round(continuity * continuity_weight * max(0.25, role.continuity_priority), 8),
        cross_project_diversity=round(diversity * novelty_weight * max(0.25, role.novelty_priority), 8),
        feedback=round(feedback, 8),
        verification=round(verification * policy.verification_weight, 8),
        total=0.0,
    )
    total = round(breakdown.component_total, 8)
    breakdown = breakdown.model_copy(update={"total": total})
    scored = candidate.model_copy(
        update={
            "score": total,
            "score_breakdown": breakdown,
            "confidence": _confidence(candidate, total),
            "disqualification_reasons": (),
        }
    )
    return SoundScoreResult(
        candidate=scored,
        role_id=role.role_id,
        eligible=True,
        score=total,
        breakdown=breakdown,
        rationale=_candidate_rationale(scored, role, breakdown, ()),
    )


def _tie_key(candidate: SoundCandidate, seed: int) -> str:
    material = {
        "seed": seed,
        "candidate_id": candidate.candidate_id,
        "target": None if candidate.target is None else candidate.target.model_dump(mode="json"),
        "product_id": candidate.product_id,
        "preset_identity_digest": candidate.identity_digest,
    }
    return canonical_digest(material)


def score_candidates(
    candidates: Sequence[SoundCandidate],
    role: SoundRoleRequest,
    request: SoundSelectionRequest,
    *,
    existing_assignments: Sequence[SoundPaletteAssignment] = (),
    selected_candidates: Sequence[SoundCandidate | SoundPaletteAssignment] = (),
    history: Any = None,
) -> tuple[SoundScoreResult, ...]:
    """Return all candidate score results in deterministic input-independent order."""

    rows = tuple(
        score_candidate(
            candidate,
            role,
            request,
            candidates=candidates,
            existing_assignments=existing_assignments,
            selected_candidates=selected_candidates,
            history=history,
        )
        for candidate in candidates
    )
    return tuple(
        sorted(
            rows,
            key=lambda item: (
                0 if item.eligible else 1,
                -round(item.score, 8),
                _tie_key(item.candidate, request.seed),
                item.candidate.candidate_id or item.candidate.identity_digest,
            ),
        )
    )


def rank_candidates(
    candidates: Sequence[SoundCandidate],
    role: SoundRoleRequest,
    request: SoundSelectionRequest,
    **kwargs: Any,
) -> tuple[SoundCandidate, ...]:
    """Return ranked candidate models with score breakdowns attached."""

    return tuple(item.candidate for item in score_candidates(candidates, role, request, **kwargs))


def select_best_candidate(
    candidates: Sequence[SoundCandidate],
    role: SoundRoleRequest,
    request: SoundSelectionRequest,
    **kwargs: Any,
) -> SoundCandidate | None:
    for result in score_candidates(candidates, role, request, **kwargs):
        if result.eligible:
            return result.candidate
    return None


score_sound_candidate = score_candidate
score_sound_candidates = score_candidates
rank_sound_candidates = rank_candidates


class SoundSelectionScorer:
    """Small stateless façade for callers that keep a request/history context.

    The façade intentionally stores references only; scoring itself remains a
    pure function and never records usage or mutates a candidate.
    """

    def __init__(
        self,
        request: SoundSelectionRequest | None = None,
        *,
        history: Any = None,
    ) -> None:
        self.request = request
        self.history = history

    def score(
        self,
        candidate: SoundCandidate,
        role: SoundRoleRequest,
        request: SoundSelectionRequest | None = None,
        **kwargs: Any,
    ) -> SoundScoreResult:
        return score_candidate(
            candidate,
            role,
            self.request if request is None else request,
            history=self.history if "history" not in kwargs else kwargs.pop("history"),
            **kwargs,
        )

    def rank(
        self,
        candidates: Sequence[SoundCandidate],
        role: SoundRoleRequest,
        request: SoundSelectionRequest | None = None,
        **kwargs: Any,
    ) -> tuple[SoundCandidate, ...]:
        resolved = self.request if request is None else request
        if resolved is None:
            resolved = SoundSelectionRequest(brief=f"select {role.role_id}", roles=(role,))
        return rank_candidates(
            candidates,
            role,
            resolved,
            history=self.history if "history" not in kwargs else kwargs.pop("history"),
            **kwargs,
        )

    def select(
        self,
        candidates: Sequence[SoundCandidate],
        role: SoundRoleRequest,
        request: SoundSelectionRequest | None = None,
        **kwargs: Any,
    ) -> SoundCandidate | None:
        resolved = self.request if request is None else request
        if resolved is None:
            resolved = SoundSelectionRequest(brief=f"select {role.role_id}", roles=(role,))
        return select_best_candidate(
            candidates,
            role,
            resolved,
            history=self.history if "history" not in kwargs else kwargs.pop("history"),
            **kwargs,
        )


CandidateScorer = SoundSelectionScorer


__all__ = [
    "CandidateScorer",
    "SoundSelectionScorer",
    "product_matches",
    "preset_matches",
    "rank_candidates",
    "rank_sound_candidates",
    "score_candidate",
    "score_candidates",
    "score_sound_candidate",
    "score_sound_candidates",
    "select_best_candidate",
]
