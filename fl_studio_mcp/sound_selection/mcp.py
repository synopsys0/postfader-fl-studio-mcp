"""Thin MCP/Production Run adapters for the live Sound Selection service.

These functions intentionally contain no bridge logic of their own.  Keeping
the public call names as small wrappers makes them easy to register from the
normal MCP server and lets Production Runs inject an isolated service in
tests, while the process default remains :data:`SOUND_SELECTION`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .executor import (
    SOUND_SELECTION,
    SoundFeedbackResult,
    SoundPaletteLookup,
    SoundSelectionApplyResult,
    SoundSelectionService,
)
from .history import SoundHistoryResetResult, SoundHistoryStatus
from .models import (
    SoundFeedbackRequest,
    SoundInventory,
    SoundPaletteAssignment,
    SoundPalettePlan,
    SoundPaletteState,
    SoundPaletteVariationPlan,
    SoundSelectionRequest,
)


def _service(value: SoundSelectionService | None) -> SoundSelectionService:
    return SOUND_SELECTION if value is None else value


def sound_selection_inventory(
    request: SoundSelectionRequest | None = None,
    *,
    service: SoundSelectionService | None = None,
    **options: Any,
) -> SoundInventory:
    return _service(service).inventory(request, **options)


def sound_selection_plan(
    request: SoundSelectionRequest,
    inventory: SoundInventory | None = None,
    *,
    existing: SoundPaletteState | SoundPalettePlan | Sequence[SoundPaletteAssignment] | None = None,
    service: SoundSelectionService | None = None,
) -> SoundPalettePlan:
    return _service(service).plan(request, inventory, existing=existing)


def sound_selection_get(
    palette_id: str,
    *,
    service: SoundSelectionService | None = None,
) -> SoundPaletteLookup:
    return _service(service).lookup(palette_id)


def sound_selection_apply(
    plan_or_id: SoundPalettePlan | SoundPaletteState | SoundPaletteVariationPlan | str,
    session_fingerprint: str,
    authorized_to_modify: bool,
    *,
    role_ids: Sequence[str] = (),
    max_navigation_steps: int = 64,
    settle_tick_limit: int = 1,
    persist_history: bool | None = None,
    write_mode_already_enabled: bool | None = None,
    service: SoundSelectionService | None = None,
) -> SoundSelectionApplyResult:
    return _service(service).apply(
        plan_or_id,
        session_fingerprint=session_fingerprint,
        authorized_to_modify=authorized_to_modify,
        role_ids=role_ids,
        max_navigation_steps=max_navigation_steps,
        settle_tick_limit=settle_tick_limit,
        persist_history=persist_history,
        write_mode_already_enabled=write_mode_already_enabled,
    )


def sound_selection_create_variation(
    palette_id: str,
    request: SoundSelectionRequest,
    section: str | None = None,
    replace_roles: Sequence[str] = (),
    *,
    inventory: SoundInventory | None = None,
    service: SoundSelectionService | None = None,
) -> SoundPaletteVariationPlan:
    return _service(service).create_variation(
        palette_id,
        request,
        section=section,
        replace_roles=replace_roles,
        inventory=inventory,
    )


def sound_selection_record_feedback(
    request: SoundFeedbackRequest,
    *,
    service: SoundSelectionService | None = None,
) -> SoundFeedbackResult:
    return _service(service).record_feedback(request)


def sound_selection_history_status(
    *,
    service: SoundSelectionService | None = None,
) -> SoundHistoryStatus:
    return _service(service).history_status()


def sound_selection_history_reset(
    confirm: bool = True,
    *,
    service: SoundSelectionService | None = None,
) -> SoundHistoryResetResult:
    return _service(service).history_reset(confirm=confirm)


# Names used by the Production Run brief and by integrations that prefer
# operation-oriented verbs.  They remain aliases, not additional state.
plan_sound_palette = sound_selection_plan
apply_sound_palette = sound_selection_apply
create_sound_palette_variation = sound_selection_create_variation
record_sound_feedback = sound_selection_record_feedback


__all__ = [
    "SOUND_SELECTION",
    "apply_sound_palette",
    "create_sound_palette_variation",
    "plan_sound_palette",
    "record_sound_feedback",
    "SoundPaletteLookup",
    "sound_selection_apply",
    "sound_selection_create_variation",
    "sound_selection_get",
    "sound_selection_history_reset",
    "sound_selection_history_status",
    "sound_selection_inventory",
    "sound_selection_plan",
    "sound_selection_record_feedback",
]
