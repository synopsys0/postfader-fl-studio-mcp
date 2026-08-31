"""Public inventory aliases and pure candidate expansion helpers."""

from __future__ import annotations

from collections.abc import Sequence

from .models import (
    SoundCandidate,
    SoundInventory,
    SoundInventoryItem,
    SoundTargetInventory,
    canonical_digest,
)


def inventory_fingerprint(inventory: SoundInventory) -> str:
    """Return a stable fingerprint of the observed loaded-target summary."""

    if not isinstance(inventory, SoundInventory):
        raise TypeError("inventory must be SoundInventory")
    if inventory.session_fingerprint:
        return inventory.session_fingerprint
    return canonical_digest(inventory.model_dump(mode="json", exclude_none=False))


def expand_inventory(
    inventory: SoundInventory | Sequence[SoundTargetInventory],
    *,
    include_effects: bool = False,
    max_presets: int = 4096,
) -> tuple[SoundCandidate, ...]:
    """Expand loaded targets into a bounded deterministic candidate sequence."""

    if isinstance(inventory, SoundInventory):
        return inventory.candidates(include_effects=include_effects, max_presets=max_presets)
    rows = tuple(inventory)
    candidates: list[SoundCandidate] = []
    for item in rows:
        target = item if isinstance(item, SoundTargetInventory) else SoundTargetInventory.model_validate(item)
        candidates.extend(target.candidates(max_presets=max_presets))
    return tuple(candidates)


LoadedTargetInventory = SoundTargetInventory
LoadedSoundInventory = SoundInventory


__all__ = [
    "LoadedSoundInventory",
    "LoadedTargetInventory",
    "SoundCandidate",
    "SoundInventory",
    "SoundInventoryItem",
    "SoundTargetInventory",
    "expand_inventory",
    "inventory_fingerprint",
]
