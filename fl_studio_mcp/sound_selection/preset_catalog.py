"""Bounded pure models/helpers for preset pages and exact identity resolution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import AliasChoices, Field, model_validator

from ..track_b_contracts import PluginTarget
from .models import (
    MAX_PRESETS,
    MAX_REPORTED_PRESET_COUNT,
    MAX_SOUND_NAME,
    Digest,
    SoundSelectionModel,
    canonical_digest,
    preset_identity_digest,
)


class PresetRecord(SoundSelectionModel):
    """One bounded preset index/name observation."""

    index: int = Field(ge=0, lt=MAX_REPORTED_PRESET_COUNT)
    name: str | None = Field(default=None, max_length=MAX_SOUND_NAME)
    occurrence: int = Field(default=1, ge=1, le=MAX_PRESETS)
    identity_digest: Digest | None = None
    is_current: bool = False

    @property
    def is_blank(self) -> bool:
        return not (self.name or "").strip()

    @property
    def digest(self) -> str:
        return self.identity_digest or preset_identity_digest(
            "unknown", self.name, self.index
        )


class PresetPage(SoundSelectionModel):
    """Read-only deterministic page of a target's preset catalog."""

    target: PluginTarget
    product_id: str | None = Field(default=None, max_length=128)
    product_name: str = Field(min_length=1, max_length=256)
    reported_preset_count: int | None = Field(
        default=None,
        ge=0,
        le=MAX_REPORTED_PRESET_COUNT,
        validation_alias=AliasChoices("reported_preset_count", "preset_count"),
    )
    current_preset_name: str | None = Field(default=None, max_length=MAX_SOUND_NAME)
    current_preset_index: int | None = Field(default=None, ge=0, lt=MAX_REPORTED_PRESET_COUNT)
    start: int = Field(default=0, ge=0, le=MAX_REPORTED_PRESET_COUNT)
    limit: int = Field(default=64, ge=1, le=MAX_PRESETS)
    presets: tuple[PresetRecord, ...] = Field(default=(), max_length=MAX_PRESETS)
    duplicate_names: tuple[str, ...] = Field(default=(), max_length=MAX_PRESETS)
    blank_indices: tuple[int, ...] = Field(
        default=(),
        max_length=MAX_PRESETS,
        validation_alias=AliasChoices("blank_indices", "blank_name_indices"),
    )
    partial: bool = False
    truncated: bool = False
    warnings: tuple[str, ...] = Field(default=(), max_length=32)
    session_fingerprint: str | None = Field(default=None, max_length=128)

    @model_validator(mode="before")
    @classmethod
    def derive_page_flags(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        rows = data.get("presets", ())
        if not isinstance(rows, (list, tuple)):
            return data

        def row_value(row: object, key: str) -> object:
            if isinstance(row, Mapping):
                return row.get(key)
            return getattr(row, key, None)

        names = [row_value(row, "name") for row in rows]
        if "duplicate_names" not in data:
            data["duplicate_names"] = tuple(
                sorted(
                    {
                        name
                        for name in names
                        if isinstance(name, str) and name
                        and names.count(name) > 1
                    }
                )
            )
        if "blank_indices" not in data and "blank_name_indices" not in data:
            data["blank_indices"] = tuple(
                row_value(row, "index")
                for row in rows
                if (
                    (name := row_value(row, "name")) is None
                    or (isinstance(name, str) and not name.strip())
                )
            )
        return data

    @model_validator(mode="after")
    def validate_page(self) -> "PresetPage":
        if len(self.presets) > self.limit:
            raise ValueError("preset page exceeds its limit")
        indexes = [item.index for item in self.presets]
        if indexes != sorted(set(indexes)):
            raise ValueError("preset rows must be ordered and have unique indices")
        if any(item.index < self.start or item.index >= self.start + self.limit for item in self.presets):
            raise ValueError("preset rows must fall inside the requested page")
        if self.reported_preset_count is not None and self.start > self.reported_preset_count:
            raise ValueError("preset page start cannot exceed reported_preset_count")
        if any(
            self.reported_preset_count is not None
            and item.index >= self.reported_preset_count
            for item in self.presets
        ):
            raise ValueError("preset rows cannot exceed reported_preset_count")
        if sum(item.is_current for item in self.presets) > 1:
            raise ValueError("a preset page cannot mark multiple rows current")
        expected_duplicates = tuple(
            sorted(
                {
                    item.name
                    for item in self.presets
                    if item.name and sum(other.name == item.name for other in self.presets) > 1
                }
            )
        )
        if self.duplicate_names != expected_duplicates:
            raise ValueError("duplicate_names does not match the page")
        expected_blank = tuple(item.index for item in self.presets if item.is_blank)
        if self.blank_indices != expected_blank:
            raise ValueError("blank_indices does not match the page")
        return self

    @property
    def preset_count(self) -> int | None:
        """Compatibility accessor matching the Track B preset-page field."""

        return self.reported_preset_count

    @property
    def blank_name_indices(self) -> tuple[int, ...]:
        """Compatibility accessor matching the Track B page spelling."""

        return self.blank_indices


class CurrentPresetObservation(SoundSelectionModel):
    """Current preset readback with only a uniquely resolved index."""

    target: PluginTarget
    product_id: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=MAX_SOUND_NAME)
    index: int | None = Field(default=None, ge=0, lt=MAX_REPORTED_PRESET_COUNT)
    index_resolution: Literal["unique", "not_found", "ambiguous", "unsupported"] = "unsupported"
    warnings: tuple[str, ...] = Field(default=(), max_length=16)


class PresetCatalog(SoundSelectionModel):
    """A full local catalog assembled from bounded pages."""

    target: PluginTarget
    product_id: str | None = Field(default=None, max_length=128)
    product_name: str = Field(min_length=1, max_length=256)
    reported_preset_count: int | None = Field(
        default=None,
        ge=0,
        le=MAX_REPORTED_PRESET_COUNT,
        validation_alias=AliasChoices("reported_preset_count", "preset_count"),
    )
    current_preset_name: str | None = Field(default=None, max_length=MAX_SOUND_NAME)
    current_preset_index: int | None = Field(default=None, ge=0, lt=MAX_REPORTED_PRESET_COUNT)
    presets: tuple[PresetRecord, ...] = Field(default=(), max_length=MAX_PRESETS)
    complete: bool = False
    duplicate_names: tuple[str, ...] = Field(default=(), max_length=MAX_PRESETS)
    blank_indices: tuple[int, ...] = Field(
        default=(),
        max_length=MAX_PRESETS,
        validation_alias=AliasChoices("blank_indices", "blank_name_indices"),
    )

    @model_validator(mode="before")
    @classmethod
    def derive_catalog_flags(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        rows = data.get("presets", ())
        if not isinstance(rows, (list, tuple)):
            return data

        def row_value(row: object, key: str) -> object:
            if isinstance(row, Mapping):
                return row.get(key)
            return getattr(row, key, None)

        names = [row_value(row, "name") for row in rows]
        if "duplicate_names" not in data:
            data["duplicate_names"] = tuple(
                sorted(
                    {
                        name
                        for name in names
                        if isinstance(name, str) and name
                        and names.count(name) > 1
                    }
                )
            )
        if "blank_indices" not in data and "blank_name_indices" not in data:
            data["blank_indices"] = tuple(
                row_value(row, "index")
                for row in rows
                if (
                    (name := row_value(row, "name")) is None
                    or (isinstance(name, str) and not name.strip())
                )
            )
        return data
    warnings: tuple[str, ...] = Field(default=(), max_length=32)

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))

    @property
    def preset_count(self) -> int | None:
        return self.reported_preset_count

    @property
    def blank_name_indices(self) -> tuple[int, ...]:
        return self.blank_indices

    @model_validator(mode="after")
    def validate_catalog(self) -> "PresetCatalog":
        indexes = [item.index for item in self.presets]
        if indexes != sorted(set(indexes)):
            raise ValueError("preset records must be ordered and have unique indices")
        if self.reported_preset_count is not None and any(
            item.index >= self.reported_preset_count for item in self.presets
        ):
            raise ValueError("preset records cannot exceed reported_preset_count")
        expected_duplicates = tuple(
            sorted(
                {
                    item.name
                    for item in self.presets
                    if item.name and sum(other.name == item.name for other in self.presets) > 1
                }
            )
        )
        if self.duplicate_names != expected_duplicates:
            raise ValueError("duplicate_names does not match the catalog")
        expected_blank = tuple(item.index for item in self.presets if item.is_blank)
        if self.blank_indices != expected_blank:
            raise ValueError("blank_indices does not match the catalog")
        return self

    def resolve_name(self, name: str) -> tuple[PresetRecord, ...]:
        return tuple(item for item in self.presets if item.name == name)

    def resolve_exact(self, *, name: str | None = None, index: int | None = None) -> PresetRecord:
        if name is None and index is None:
            raise ValueError("preset selection requires a name or index")
        if index is not None:
            matches = tuple(item for item in self.presets if item.index == index)
            if len(matches) != 1:
                raise KeyError(f"preset index {index} is not uniquely observed")
            if name is not None and matches[0].name != name:
                raise ValueError("preset name and index identify different records")
            return matches[0]
        matches = self.resolve_name(name or "")
        if len(matches) != 1:
            raise ValueError("preset name is absent or ambiguous; provide an index")
        return matches[0]


def resolve_preset_name(records: Sequence[PresetRecord], name: str) -> PresetRecord:
    matches = tuple(item for item in records if item.name == name)
    if len(matches) != 1:
        raise ValueError("preset name is absent or ambiguous; provide an index")
    return matches[0]


__all__ = [
    "CurrentPresetObservation",
    "PresetCatalog",
    "PresetPage",
    "PresetRecord",
    "resolve_preset_name",
]
