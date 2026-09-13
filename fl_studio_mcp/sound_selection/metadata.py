"""Versioned preset/family metadata with isolated local overlays.

Metadata in this module is descriptive evidence only.  The bundled file is a
small, reviewable package resource and user-local records are loaded as a
separate layer.  Merging returns a new immutable model, so a user annotation
cannot mutate process-global bundled knowledge (or another caller's catalog).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field, model_validator

from ..host_config import fl_studio_user_data_dir
from .models import (
    ConfidenceLevel,
    DescriptorEvidence,
    DescriptorIdentifier,
    Digest,
    Register,
    RoleIdentifier,
    SoundSelectionModel,
    canonical_digest,
    preset_identity_digest,
)


PRESET_METADATA_SCHEMA_VERSION = "1.0"
PRESET_METADATA_DATA_FILE = "data/preset-metadata-v1.json"
DEFAULT_PRESET_METADATA_FILENAME = "preset-metadata-v1.json"
MAX_METADATA_RECORDS = 4096
MAX_METADATA_FAMILIES = 1024
MAX_METADATA_FILE_BYTES = 4 * 1024 * 1024

MetadataProvenance = Literal[
    "bundled_reviewed",
    "user_local_reviewed",
    "name_inferred",
    "atlas_product",
    "unknown",
]
MonoPolyMode = Literal["mono", "poly", "both", "unknown"]


class PresetMetadataRecord(SoundSelectionModel):
    """Reviewed or locally annotated metadata for one exact preset."""

    product_id: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("product_id", "product"),
        serialization_alias="product_id",
    )
    preset_name: str = Field(
        min_length=1,
        max_length=256,
        validation_alias=AliasChoices("preset_name", "preset", "name"),
        serialization_alias="preset_name",
    )
    preset_index: int | None = Field(default=None, ge=0, lt=1_000_000)
    family_id: str | None = Field(default=None, min_length=1, max_length=128)
    descriptors: tuple[DescriptorIdentifier, ...] = Field(default=(), max_length=64)
    articulations: tuple[str, ...] = Field(default=(), max_length=32)
    registers: tuple[Register, ...] = Field(default=(), max_length=8)
    envelope_behavior: tuple[str, ...] = Field(default=(), max_length=16)
    mono_poly: MonoPolyMode = "unknown"
    common_roles: tuple[RoleIdentifier, ...] = Field(default=(), max_length=32)
    known_limitations: tuple[str, ...] = Field(default=(), max_length=32)
    provenance: MetadataProvenance = "unknown"
    review_date: str | None = Field(default=None, max_length=32)
    confidence: ConfidenceLevel = "metadata_insufficient"
    source_id: str | None = Field(default=None, max_length=128)
    identity_digest: Digest | None = None

    @model_validator(mode="after")
    def validate_metadata(self) -> "PresetMetadataRecord":
        if not any((self.descriptors, self.articulations, self.registers, self.common_roles, self.family_id)):
            # An exact record may intentionally only carry a review/limitation;
            # identity fields still make it useful.  Keep empty annotations
            # valid, but mark such rows honestly when callers omit confidence.
            if self.confidence == "high":
                raise ValueError("empty exact metadata cannot claim high confidence")
        if self.identity_digest is not None:
            expected = preset_identity_digest(self.product_id, self.preset_name, self.preset_index)
            if self.identity_digest != expected:
                raise ValueError("identity_digest does not match the preset identity")
        return self

    @property
    def digest(self) -> str:
        return self.identity_digest or preset_identity_digest(
            self.product_id, self.preset_name, self.preset_index
        )


class PresetFamilyMetadata(SoundSelectionModel):
    """Metadata shared by a family when exact preset review is unavailable."""

    family_id: str = Field(min_length=1, max_length=128)
    product_id: str | None = Field(default=None, max_length=128)
    family_name: str | None = Field(default=None, max_length=256)
    descriptors: tuple[DescriptorIdentifier, ...] = Field(default=(), max_length=64)
    articulations: tuple[str, ...] = Field(default=(), max_length=32)
    registers: tuple[Register, ...] = Field(default=(), max_length=8)
    envelope_behavior: tuple[str, ...] = Field(default=(), max_length=16)
    mono_poly: MonoPolyMode = "unknown"
    common_roles: tuple[RoleIdentifier, ...] = Field(default=(), max_length=32)
    known_limitations: tuple[str, ...] = Field(default=(), max_length=32)
    provenance: MetadataProvenance = "unknown"
    review_date: str | None = Field(default=None, max_length=32)
    confidence: ConfidenceLevel = "metadata_insufficient"
    source_id: str | None = Field(default=None, max_length=128)

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))


class PresetMetadataCoverage(SoundSelectionModel):
    """Truthful accounting of exact/family/inferred/unknown metadata."""

    exact_preset_records: int = Field(default=0, ge=0, le=MAX_METADATA_RECORDS)
    family_records: int = Field(default=0, ge=0, le=MAX_METADATA_FAMILIES)
    name_inferred_only: int = Field(default=0, ge=0, le=MAX_METADATA_RECORDS)
    unknown_records: int = Field(default=0, ge=0, le=MAX_METADATA_RECORDS)
    limitations: tuple[str, ...] = Field(default=(), max_length=32)

    @property
    def total_records(self) -> int:
        return self.exact_preset_records + self.family_records + self.name_inferred_only + self.unknown_records


class PresetMetadataCatalog(SoundSelectionModel):
    """Immutable versioned metadata layers used by candidate enrichment."""

    schema_version: Literal["1.0"] = PRESET_METADATA_SCHEMA_VERSION
    metadata_version: str = Field(min_length=1, max_length=32)
    records: tuple[PresetMetadataRecord, ...] = Field(default=(), max_length=MAX_METADATA_RECORDS)
    families: tuple[PresetFamilyMetadata, ...] = Field(default=(), max_length=MAX_METADATA_FAMILIES)
    layer: Literal["bundled", "user_local", "merged"] = "bundled"
    source: str | None = Field(default=None, max_length=512)
    warnings: tuple[str, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_catalog(self) -> "PresetMetadataCatalog":
        exact_keys = [
            (item.product_id.casefold(), item.preset_name.casefold(), item.preset_index)
            for item in self.records
        ]
        if len(set(exact_keys)) != len(exact_keys):
            raise ValueError("preset metadata exact identities must be unique")
        family_keys = [(item.product_id or "", item.family_id.casefold()) for item in self.families]
        if len(set(family_keys)) != len(family_keys):
            raise ValueError("preset metadata family identities must be unique")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))

    @property
    def coverage(self) -> PresetMetadataCoverage:
        exact = len(self.records)
        family_ids = {item.family_id for item in self.records if item.family_id}
        families = len(self.families)
        inferred = sum(1 for item in self.records if item.provenance == "name_inferred")
        unknown = sum(
            1
            for item in self.records
            if item.provenance not in {"name_inferred"}
            and (item.provenance == "unknown" or item.confidence in {"unknown", "metadata_insufficient"})
        )
        return PresetMetadataCoverage(
            exact_preset_records=exact,
            family_records=max(families, len(family_ids)),
            name_inferred_only=inferred,
            unknown_records=unknown,
            limitations=(
                "Bundled metadata is intentionally partial; absence is not evidence of unsuitability."
                if self.layer in {"bundled", "merged"}
                else "User-local metadata describes only records explicitly supplied by the user.",
            ),
        )

    def exact(
        self, product_id: str, preset_name: str, preset_index: int | None = None
    ) -> PresetMetadataRecord | None:
        product = product_id.casefold()
        name = preset_name.casefold()
        rows = tuple(
            item
            for item in self.records
            if item.product_id.casefold() == product and item.preset_name.casefold() == name
            and (
                preset_index is None
                or item.preset_index is None
                or item.preset_index == preset_index
            )
        )
        if preset_index is not None:
            indexed = tuple(item for item in rows if item.preset_index == preset_index)
            if len(indexed) == 1:
                return indexed[0]
            generic = tuple(item for item in rows if item.preset_index is None)
            if len(indexed) == 0 and len(generic) == 1:
                return generic[0]
            return None
        if len(rows) > 1:
            # Duplicate names are valid; without an index, avoid pretending
            # that one exact identity was resolved.
            return None
        return rows[0] if rows else None

    def family(self, product_id: str, family_id: str) -> PresetFamilyMetadata | None:
        product = product_id.casefold()
        family = family_id.casefold()
        return next(
            (
                item
                for item in self.families
                if item.family_id.casefold() == family
                and (item.product_id is None or item.product_id.casefold() == product)
            ),
            None,
        )

    def family_for_product(self, product_id: str) -> PresetFamilyMetadata | None:
        """Resolve one unambiguous product-level family annotation."""

        product = product_id.casefold()
        rows = tuple(
            item
            for item in self.families
            if item.product_id is not None and item.product_id.casefold() == product
        )
        return rows[0] if len(rows) == 1 else None


class PresetMetadataLoadError(ValueError):
    """Raised when a local metadata resource is invalid or unsafe to load."""


def _read_json_resource(source: Any) -> Any:
    if isinstance(source, (str, os.PathLike, Path)):
        path = Path(source)
        if path.is_dir():
            path = path / DEFAULT_PRESET_METADATA_FILENAME
        if not path.is_absolute():
            raise PresetMetadataLoadError("preset metadata path must be absolute")
        try:
            size = path.stat().st_size
            if size > MAX_METADATA_FILE_BYTES:
                raise PresetMetadataLoadError("preset metadata resource exceeds size bound")
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PresetMetadataLoadError(f"cannot read preset metadata resource: {exc}") from exc
    else:
        try:
            text = source.read_text(encoding="utf-8")
        except (AttributeError, OSError, UnicodeError) as exc:
            raise PresetMetadataLoadError("cannot read preset metadata resource") from exc
        if len(text.encode("utf-8")) > MAX_METADATA_FILE_BYTES:
            raise PresetMetadataLoadError("preset metadata resource exceeds size bound")
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PresetMetadataLoadError("preset metadata resource is not valid JSON") from exc


def _bundled_resource() -> Any:
    try:
        return resources.files("fl_studio_mcp.sound_selection").joinpath(PRESET_METADATA_DATA_FILE)
    except (ModuleNotFoundError, FileNotFoundError) as exc:
        raise PresetMetadataLoadError("bundled preset metadata is unavailable") from exc


def _parse_catalog(raw: Any, *, layer: Literal["bundled", "user_local", "merged"], source: str | None) -> PresetMetadataCatalog:
    if not isinstance(raw, Mapping):
        raise PresetMetadataLoadError("preset metadata root must be an object")
    data = dict(raw)
    # Permit concise user overlays that only supply records/families.
    data.setdefault("schema_version", PRESET_METADATA_SCHEMA_VERSION)
    data.setdefault("metadata_version", "1.0")
    data["layer"] = layer
    if source is not None:
        data["source"] = source
    try:
        return PresetMetadataCatalog.model_validate(data)
    except Exception as exc:
        raise PresetMetadataLoadError(f"preset metadata violates schema: {exc}") from exc


def load_preset_metadata(source: Any = None, *, user_overlay: Any = None, user_data_dir: str | os.PathLike[str] | None = None) -> PresetMetadataCatalog:
    """Load bundled metadata, optionally merged with an isolated user layer."""

    raw_source = _bundled_resource() if source is None else source
    bundled = _parse_catalog(raw_source if isinstance(raw_source, Mapping) else _read_json_resource(raw_source), layer="bundled", source=None)
    if user_overlay is None and user_data_dir is not None:
        user_overlay = default_preset_metadata_path(user_data_dir)
    if user_overlay is None:
        return bundled
    try:
        overlay_path = Path(user_overlay) if isinstance(user_overlay, (str, os.PathLike)) else None
        if overlay_path is not None and not overlay_path.exists():
            return bundled
        raw_overlay = user_overlay if isinstance(user_overlay, Mapping) else _read_json_resource(user_overlay)
        overlay = _parse_catalog(
            raw_overlay,
            layer="user_local",
            source=None if overlay_path is None else os.fspath(overlay_path.resolve()),
        )
    except PresetMetadataLoadError:
        raise
    return merge_preset_metadata(bundled, overlay)


load_bundled_preset_metadata = load_preset_metadata


def load_user_preset_metadata(
    source: Any = None, *, user_data_dir: str | os.PathLike[str] | None = None, required: bool = False
) -> PresetMetadataCatalog | None:
    """Load only the user layer; missing optional local data stays absent."""

    selected = source if source is not None else default_preset_metadata_path(user_data_dir)
    path = Path(selected) if isinstance(selected, (str, os.PathLike)) else None
    if path is not None and not path.exists():
        if required:
            raise PresetMetadataLoadError("user-local preset metadata does not exist")
        return None
    raw = selected if isinstance(selected, Mapping) else _read_json_resource(selected)
    return _parse_catalog(
        raw,
        layer="user_local",
        source=None if path is None else os.fspath(path.resolve()),
    )


def default_preset_metadata_path(user_data_dir: str | os.PathLike[str] | None = None) -> Path:
    root = Path(user_data_dir) if user_data_dir is not None else fl_studio_user_data_dir()
    return (root / "Settings" / "PostFader" / DEFAULT_PRESET_METADATA_FILENAME).resolve()


def merge_preset_metadata(*catalogs: PresetMetadataCatalog) -> PresetMetadataCatalog:
    """Merge layers by exact/family identity without mutating any input."""

    if not catalogs:
        return PresetMetadataCatalog(metadata_version="1.0", layer="merged")
    exact: dict[tuple[str, str, int | None], PresetMetadataRecord] = {}
    families: dict[tuple[str, str], PresetFamilyMetadata] = {}
    warnings: list[str] = []
    for catalog in catalogs:
        source_layer = catalog.layer
        for record in catalog.records:
            key = (record.product_id.casefold(), record.preset_name.casefold(), record.preset_index)
            row = record
            if source_layer == "user_local" and row.provenance in {"unknown", "name_inferred"}:
                row = row.model_copy(update={"provenance": "user_local_reviewed"})
            exact[key] = row
        for family in catalog.families:
            key = (family.product_id or "", family.family_id.casefold())
            row = family
            if source_layer == "user_local" and row.provenance == "unknown":
                row = row.model_copy(update={"provenance": "user_local_reviewed"})
            families[key] = row
        warnings.extend(catalog.warnings)
    return PresetMetadataCatalog(
        metadata_version=catalogs[-1].metadata_version,
        records=tuple(sorted(exact.values(), key=lambda item: (item.product_id.casefold(), item.preset_name.casefold(), item.preset_index if item.preset_index is not None else -1))),
        families=tuple(sorted(families.values(), key=lambda item: (item.product_id or "", item.family_id.casefold()))),
        layer="merged",
        source=";".join(filter(None, (catalog.source for catalog in catalogs))) or None,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def preset_metadata_for(
    catalog: PresetMetadataCatalog,
    product_id: str,
    preset_name: str,
    *,
    preset_index: int | None = None,
    family_id: str | None = None,
) -> PresetMetadataRecord | PresetFamilyMetadata | None:
    """Resolve exact metadata first, then an explicitly supplied family."""

    exact = catalog.exact(product_id, preset_name, preset_index)
    if exact is not None:
        return exact
    if family_id is not None:
        return catalog.family(product_id, family_id)
    return catalog.family_for_product(product_id)


def enrich_candidate_metadata(candidate: Any, catalog: PresetMetadataCatalog) -> Any:
    """Overlay reviewed/family evidence onto a candidate immutably.

    Existing explicit candidate observations win ties only when they are at
    least as specific as the metadata layer.  The function is intentionally
    duck-typed at the boundary to avoid making the metadata loader import the
    scoring module; callers still receive their concrete ``SoundCandidate``
    type from ``model_copy``.
    """

    if not isinstance(catalog, PresetMetadataCatalog):
        raise TypeError("catalog must be PresetMetadataCatalog")
    selected = getattr(candidate, "selected_preset", None)
    product = getattr(candidate, "product_id", None) or getattr(candidate, "product_name", None)
    if not isinstance(selected, str) or not selected.strip() or not isinstance(product, str):
        return candidate
    exact = catalog.exact(product, selected, getattr(candidate, "preset_index", None))
    family = None
    if exact is not None and exact.family_id:
        family = catalog.family(product, exact.family_id)
    elif getattr(candidate, "metadata_family_id", None):
        family = catalog.family(product, candidate.metadata_family_id)
    else:
        family = catalog.family_for_product(product)
    source = exact or family
    if source is None:
        inferred = any(
            getattr(item, "provenance", None) == "preset_name_token"
            for item in getattr(candidate, "descriptor_provenance", ())
        )
        updates = {
            "metadata_confidence": "low" if inferred else "metadata_insufficient",
            "metadata_provenance": "name_inferred" if inferred else "unknown",
            "characteristic_provenance": "name_inferred" if inferred else "unknown",
        }
        return candidate.model_copy(update=updates) if hasattr(candidate, "model_copy") else candidate

    from .descriptors import merge_descriptor_evidence, normalize_descriptor

    metadata_evidence = tuple(
        DescriptorEvidence(
            descriptor=item,
            provenance=(
                "bundled_reviewed"
                if source.provenance == "bundled_reviewed"
                else "user_local_reviewed"
                if source.provenance == "user_local_reviewed"
                else "atlas_product"
                if source.provenance == "atlas_product"
                else "unknown"
            ),
            confidence=(
                0.95
                if source.confidence == "high"
                else 0.75
                if source.confidence == "medium"
                else 0.50
            ),
            source_id=source.source_id or ("preset-metadata:" + source.digest),
            original_term=item,
            detail="reviewed preset/family metadata",
        )
        for item in source.descriptors
    )
    merged = merge_descriptor_evidence(
        getattr(candidate, "descriptor_provenance", ()), metadata_evidence
    )
    confidence: ConfidenceLevel = source.confidence
    if confidence == "unknown":
        confidence = "metadata_insufficient"
    updates: dict[str, Any] = {
        "descriptors": tuple(
            dict.fromkeys(
                item.normalized_descriptor
                for value in (
                    *getattr(candidate, "descriptors", ()),
                    *(item.descriptor for item in merged),
                )
                for item in (normalize_descriptor(value),)
            )
        ),
        "descriptor_provenance": merged,
        "metadata_confidence": confidence,
        "metadata_provenance": source.provenance,
        "metadata_source_id": source.source_id or ("preset-metadata:" + source.digest),
        "metadata_family_id": getattr(source, "family_id", None),
        "characteristic_provenance": source.provenance,
    }
    for field in (
        "registers",
        "articulations",
        "envelope_behavior",
        "known_limitations",
    ):
        values = getattr(source, field, ())
        if values and not getattr(candidate, field, ()):
            updates[field] = values
    if getattr(source, "mono_poly", "unknown") != "unknown" and getattr(candidate, "mono_poly", "unknown") == "unknown":
        updates["mono_poly"] = source.mono_poly
    if getattr(source, "common_roles", ()) and not getattr(candidate, "role_ids", ()):
        updates["role_ids"] = source.common_roles
    return candidate.model_copy(update=updates) if hasattr(candidate, "model_copy") else candidate


__all__ = [
    "DEFAULT_PRESET_METADATA_FILENAME",
    "MAX_METADATA_FAMILIES",
    "MAX_METADATA_RECORDS",
    "MetadataProvenance",
    "PRESET_METADATA_DATA_FILE",
    "PRESET_METADATA_SCHEMA_VERSION",
    "PresetFamilyMetadata",
    "FamilyMetadata",
    "PresetMetadata",
    "PresetMetadataCatalog",
    "PresetMetadataCoverage",
    "PresetMetadataLoadError",
    "PresetMetadataRecord",
    "default_preset_metadata_path",
    "enrich_candidate_metadata",
    "load_bundled_preset_metadata",
    "load_preset_metadata",
    "load_preset_metadata_catalog",
    "load_user_preset_metadata",
    "merge_preset_metadata",
    "merge_metadata_layers",
    "preset_metadata_for",
    "lookup_preset_metadata",
]


# Friendly compatibility names used by adapters that distinguish an exact
# record from the catalog container.
PresetMetadata = PresetMetadataRecord
FamilyMetadata = PresetFamilyMetadata
load_preset_metadata_catalog = load_preset_metadata
merge_metadata_layers = merge_preset_metadata
lookup_preset_metadata = preset_metadata_for
