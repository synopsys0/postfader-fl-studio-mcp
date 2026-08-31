"""Fail-closed loading and validation for bundled Plugin Atlas JSON.

The loader accepts either a local :class:`pathlib.Path` or an
``importlib.resources`` ``Traversable``.  It never follows URLs, searches the
filesystem, or executes data.  A manifest is optional only for small tests and
developer fixtures; bundled distributions should always ship one.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, cast

from pydantic import ValidationError

from .models import (
    MAX_ADAPTERS,
    MAX_EVIDENCE,
    MAX_LIST_ITEMS,
    MAX_PRODUCTS,
    MAX_RESOURCE_BYTES,
    MAX_RESOURCE_COUNT,
    MAX_TECHNIQUES,
    MAX_VENDORS,
    AtlasBundle,
    AtlasManifest,
    CatalogCategoryCount,
    CatalogManifestEntry,
    CatalogProductRow,
    CatalogSnapshot,
    CatalogSnapshotManifestEntry,
    CompatibilityEvidence,
    ControlAdapter,
    EvidenceReference,
    ProductKnowledge,
    ResourceManifest,
    TechniqueKnowledge,
    VendorKnowledge,
    WriteValidationEvidence,
)


DEFAULT_DATA_PACKAGE = "fl_studio_mcp.plugin_atlas_data"
DEFAULT_MANIFEST_NAMES = ("manifest.json", "manifests/atlas.json", "atlas.json")
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 200_000
MAX_TOTAL_RESOURCE_BYTES = 64 * 1024 * 1024
_NAME_SEPARATOR_RE = re.compile(r"[^\w]+", re.UNICODE)


class AtlasLoadError(ValueError):
    """Base error raised when a local Atlas bundle cannot be loaded safely."""


class AtlasValidationError(AtlasLoadError):
    """A syntactically valid resource violates the Atlas data contract."""


@dataclass(frozen=True, slots=True)
class LoaderLimits:
    """Resource and JSON limits applied before Pydantic validation."""

    max_resource_bytes: int = MAX_RESOURCE_BYTES
    max_total_resource_bytes: int = MAX_TOTAL_RESOURCE_BYTES
    max_resources: int = MAX_RESOURCE_COUNT
    max_json_depth: int = MAX_JSON_DEPTH
    max_json_nodes: int = MAX_JSON_NODES

    def __post_init__(self) -> None:
        if type(self.max_resource_bytes) is not int or not (
            1 <= self.max_resource_bytes <= MAX_RESOURCE_BYTES
        ):
            raise ValueError("max_resource_bytes is outside Atlas bounds")
        if type(self.max_total_resource_bytes) is not int or not (
            1 <= self.max_total_resource_bytes <= MAX_TOTAL_RESOURCE_BYTES
        ):
            raise ValueError("max_total_resource_bytes is outside Atlas bounds")
        if type(self.max_resources) is not int or not (
            1 <= self.max_resources <= MAX_RESOURCE_COUNT
        ):
            raise ValueError("max_resources is outside Atlas bounds")
        if type(self.max_json_depth) is not int or not (
            1 <= self.max_json_depth <= MAX_JSON_DEPTH
        ):
            raise ValueError("max_json_depth is outside Atlas bounds")
        if type(self.max_json_nodes) is not int or not (
            1 <= self.max_json_nodes <= MAX_JSON_NODES
        ):
            raise ValueError("max_json_nodes is outside Atlas bounds")


@dataclass(frozen=True, slots=True)
class _ResourceSpec:
    path: str
    kind: str
    required: bool
    max_bytes: int
    sha256: str | None
    record_count: int | None
    vendor_id: str | None = None
    expected_product_count: int | None = None
    expected_name_digest: str | None = None
    coverage: str | None = None
    source_base_url: str | None = None
    index_sources: tuple[str, ...] = ()
    catalog_as_of: str | None = None
    fl_studio_version: str | None = None
    source_snapshot: str | None = None
    category_counts: tuple[CatalogCategoryCount, ...] = ()
    snapshot_id: str | None = None
    catalog_scope: str | None = None
    expected_row_count: int | None = None
    expected_digest: str | None = None


def _duplicate_key_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AtlasValidationError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise AtlasValidationError(f"non-finite JSON number is not allowed: {value}")


def _json_load(raw: bytes, label: str, limits: LoaderLimits) -> Any:
    if len(raw) > limits.max_resource_bytes:
        raise AtlasLoadError(
            f"Atlas resource {label!r} exceeds the {limits.max_resource_bytes}-byte limit"
        )
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_key_object,
            parse_constant=_reject_nonfinite_json,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, AtlasLoadError) as exc:
        raise AtlasValidationError(f"invalid JSON in Atlas resource {label!r}: {exc}") from exc
    _check_json_shape(value, label, limits)
    return value


def _check_json_shape(value: Any, label: str, limits: LoaderLimits) -> None:
    nodes = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > limits.max_json_nodes:
            raise AtlasLoadError(
                f"Atlas resource {label!r} exceeds the JSON node limit"
            )
        if depth > limits.max_json_depth:
            raise AtlasLoadError(
                f"Atlas resource {label!r} exceeds the JSON depth limit"
            )
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise AtlasValidationError(
                        f"Atlas resource {label!r} contains a non-string key"
                    )
                visit(child, depth + 1)
        elif isinstance(item, list):
            if len(item) > MAX_EVIDENCE:
                raise AtlasLoadError(
                    f"Atlas resource {label!r} contains too many list entries"
                )
            for child in item:
                visit(child, depth + 1)

    try:
        visit(value, 0)
    except RecursionError as exc:
        raise AtlasLoadError(f"Atlas resource {label!r} is too deeply nested") from exc


def _root_path(root: Path | Traversable | str | None) -> Any:
    if root is None:
        try:
            return resources.files(DEFAULT_DATA_PACKAGE)
        except (ModuleNotFoundError, FileNotFoundError) as exc:
            raise AtlasLoadError(
                f"bundled Atlas package {DEFAULT_DATA_PACKAGE!r} is unavailable"
            ) from exc
    if isinstance(root, (str, Path)):
        return Path(root)
    if isinstance(root, Traversable) or (
        hasattr(root, "joinpath") and hasattr(root, "is_file")
    ):
        return root
    raise TypeError("Atlas root must be a Path, string, Traversable, or None")


def _is_file(root: Any) -> bool:
    try:
        return root.is_file()
    except OSError:
        return False


def _resolve_child(root: Any, relative: str) -> Any:
    """Resolve one manifest path without allowing traversal or absolute paths."""

    if type(relative) is not str or not relative or "\x00" in relative:
        raise AtlasValidationError("Atlas resource path must be a non-empty string")
    if "\\" in relative:
        raise AtlasValidationError("Atlas resource paths must use POSIX separators")
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise AtlasValidationError(f"unsafe Atlas resource path: {relative!r}")

    if isinstance(root, Path):
        base = root if root.is_dir() else root.parent
        base_resolved = base.resolve()
        candidate = (base / Path(*path.parts)).resolve()
        try:
            candidate.relative_to(base_resolved)
        except ValueError as exc:
            raise AtlasValidationError(
                f"Atlas resource escapes bundle root: {relative!r}"
            ) from exc
        return candidate

    candidate: Any = root
    for part in path.parts:
        candidate = candidate.joinpath(part)
    return candidate


def _read_resource(resource: Any, label: str, limit: int) -> bytes:
    try:
        if not resource.is_file():
            raise FileNotFoundError(label)
        if isinstance(resource, Path):
            size = resource.stat().st_size
            if size > limit:
                raise AtlasLoadError(
                    f"Atlas resource {label!r} exceeds its {limit}-byte limit"
                )
            return resource.read_bytes()
        handle: Any = resource.open("rb")
        with handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise AtlasLoadError(
                f"Atlas resource {label!r} exceeds its {limit}-byte limit"
            )
        return raw
    except AtlasLoadError:
        raise
    except (OSError, FileNotFoundError) as exc:
        raise AtlasLoadError(f"Atlas resource {label!r} is unavailable") from exc


def _text(value: Any, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise AtlasValidationError(f"Atlas manifest {field} must be non-empty text")
    return value.strip()


def _as_list(value: Any, field: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    raise AtlasValidationError(f"Atlas {field} must be a JSON array")


def _resource_spec(raw: Any, default_kind: str = "catalog") -> _ResourceSpec:
    if isinstance(raw, str):
        if default_kind == "snapshot":
            default_kind = "catalog_snapshot"
        return _ResourceSpec(
            path=_text(raw, "resource path"),
            kind=default_kind,
            required=True,
            max_bytes=MAX_RESOURCE_BYTES,
            sha256=None,
            record_count=None,
        )
    if not isinstance(raw, dict):
        raise AtlasValidationError("Atlas manifest resource must be text or object")
    allowed_keys = {
        "path",
        "resource",
        "file",
        "kind",
        "required",
        "max_bytes",
        "sha256",
        "record_count",
        "expected_record_count",
        "vendor_id",
        "expected_product_count",
        "expected_name_digest",
        "coverage",
        "source_base_url",
        "index_sources",
        "catalog_as_of",
        "fl_studio_version",
        "source_snapshot",
        "category_counts",
        "expected_category_counts",
        "snapshot_id",
        "id",
        "catalog_scope",
        "scope",
        "expected_row_count",
        "expected_digest",
    }
    unknown_keys = sorted(set(raw).difference(allowed_keys))
    if unknown_keys:
        raise AtlasValidationError(
            f"Atlas manifest resource has unknown fields: {', '.join(unknown_keys)}"
        )
    path = raw.get("path", raw.get("resource", raw.get("file")))
    if path is None:
        raise AtlasValidationError("Atlas manifest resource is missing path")
    kind = raw.get("kind", default_kind)
    if kind == "snapshot":
        kind = "catalog_snapshot"
    if type(kind) is not str or kind not in {
        "catalog",
        "catalog_snapshot",
        "products",
        "vendors",
        "techniques",
        "adapters",
        "evidence",
    }:
        raise AtlasValidationError(f"unsupported Atlas resource kind: {kind!r}")
    max_bytes = raw.get("max_bytes", MAX_RESOURCE_BYTES)
    if type(max_bytes) is not int or not (1 <= max_bytes <= MAX_RESOURCE_BYTES):
        raise AtlasValidationError("Atlas resource max_bytes is outside bounds")
    required = raw.get("required", True)
    if type(required) is not bool:
        raise AtlasValidationError("Atlas resource required must be boolean")
    sha256 = raw.get("sha256")
    if sha256 is not None:
        if type(sha256) is not str or len(sha256) != 64:
            raise AtlasValidationError("Atlas resource sha256 must be 64 hex characters")
        try:
            int(sha256, 16)
        except ValueError as exc:
            raise AtlasValidationError("Atlas resource sha256 must be hexadecimal") from exc
        sha256 = sha256.lower()
    record_count = raw.get("record_count", raw.get("expected_record_count"))
    if record_count is not None and (
        type(record_count) is not int or not (0 <= record_count <= MAX_EVIDENCE)
    ):
        raise AtlasValidationError("Atlas resource record_count is outside bounds")
    vendor_id = raw.get("vendor_id")
    if vendor_id is not None:
        vendor_id = _text(vendor_id, "vendor_id")
    expected_product_count = raw.get("expected_product_count")
    if expected_product_count is not None and (
        type(expected_product_count) is not int
        or not (0 <= expected_product_count <= MAX_PRODUCTS)
    ):
        raise AtlasValidationError(
            "Atlas catalog expected_product_count is outside bounds"
        )
    expected_name_digest = raw.get("expected_name_digest")
    if expected_name_digest is not None:
        if type(expected_name_digest) is not str or len(expected_name_digest) != 64:
            raise AtlasValidationError(
                "Atlas catalog expected_name_digest must be 64 hex characters"
            )
        try:
            int(expected_name_digest, 16)
        except ValueError as exc:
            raise AtlasValidationError(
                "Atlas catalog expected_name_digest must be hexadecimal"
            ) from exc
        expected_name_digest = expected_name_digest.lower()
    coverage = raw.get("coverage")
    if coverage is not None:
        coverage = _text(coverage, "coverage")
    source_base_url = raw.get("source_base_url")
    if source_base_url is not None:
        source_base_url = _text(source_base_url, "source_base_url")
    index_sources = tuple(
        _text(value, "index_sources entry")
        for value in _as_list(raw.get("index_sources"), "index_sources")
    )
    catalog_as_of = raw.get("catalog_as_of")
    if catalog_as_of is not None:
        catalog_as_of = _text(catalog_as_of, "catalog_as_of")
    fl_studio_version = raw.get("fl_studio_version")
    if fl_studio_version is not None:
        fl_studio_version = _text(fl_studio_version, "fl_studio_version")
    source_snapshot = raw.get("source_snapshot")
    if source_snapshot is not None:
        source_snapshot = _text(source_snapshot, "source_snapshot")
    raw_category_counts = raw.get(
        "category_counts", raw.get("expected_category_counts")
    )
    category_counts: list[CatalogCategoryCount] = []
    if raw_category_counts is not None:
        if isinstance(raw_category_counts, dict):
            raw_category_counts = [
                {"category": category, "count": count}
                for category, count in raw_category_counts.items()
            ]
        for item in _as_list(raw_category_counts, "category_counts"):
            if not isinstance(item, dict):
                raise AtlasValidationError("Atlas category count must be an object")
            try:
                category_counts.append(CatalogCategoryCount.model_validate_json(
                    json.dumps(item, ensure_ascii=True, allow_nan=False)
                ))
            except (TypeError, ValueError, ValidationError) as exc:
                raise AtlasValidationError(
                    f"invalid Atlas category count: {exc}"
                ) from exc
    if len(category_counts) > MAX_LIST_ITEMS:
        raise AtlasLoadError("Atlas declares too many category counts")
    if len({item.category for item in category_counts}) != len(category_counts):
        raise AtlasValidationError("Atlas catalog declares duplicate category counts")
    snapshot_id = raw.get("snapshot_id")
    if snapshot_id is not None:
        snapshot_id = _text(snapshot_id, "snapshot_id")
    catalog_scope = raw.get("catalog_scope", raw.get("scope"))
    if catalog_scope is not None:
        catalog_scope = _text(catalog_scope, "catalog_scope")
    expected_row_count = raw.get(
        "expected_row_count", raw.get("expected_product_count")
    )
    if expected_row_count is not None and (
        type(expected_row_count) is not int
        or not (0 <= expected_row_count <= MAX_PRODUCTS)
    ):
        raise AtlasValidationError("Atlas snapshot expected_row_count is outside bounds")
    expected_digest = raw.get("expected_digest", raw.get("expected_name_digest"))
    if expected_digest is not None:
        if type(expected_digest) is not str or len(expected_digest) != 64:
            raise AtlasValidationError("Atlas snapshot expected_digest must be 64 hex characters")
        try:
            int(expected_digest, 16)
        except ValueError as exc:
            raise AtlasValidationError(
                "Atlas snapshot expected_digest must be hexadecimal"
            ) from exc
        expected_digest = expected_digest.lower()
    return _ResourceSpec(
        path=_text(path, "resource path"),
        kind=kind,
        required=required,
        max_bytes=max_bytes,
        sha256=sha256,
        record_count=record_count,
        vendor_id=vendor_id,
        expected_product_count=expected_product_count,
        expected_name_digest=expected_name_digest,
        coverage=coverage,
        source_base_url=source_base_url,
        index_sources=index_sources,
        catalog_as_of=catalog_as_of,
        fl_studio_version=fl_studio_version,
        source_snapshot=source_snapshot,
        category_counts=tuple(category_counts),
        snapshot_id=snapshot_id,
        catalog_scope=catalog_scope,
        expected_row_count=expected_row_count,
        expected_digest=expected_digest,
    )


def _manifest_specs(raw: dict[str, Any]) -> tuple[_ResourceSpec, ...]:
    specs: list[_ResourceSpec] = []
    resources_value = raw.get("resources")
    if resources_value is not None:
        specs.extend(_resource_spec(item) for item in _as_list(resources_value, "resources"))

    # Atlas v1 data is commonly partitioned by vendor.  ``catalogs`` carries
    # useful coverage metadata but the loader only needs its declared resource.
    for item in _as_list(raw.get("catalogs"), "catalogs"):
        if not isinstance(item, dict):
            raise AtlasValidationError("Atlas manifest catalogs must be objects")
        if "resource" not in item and "path" not in item and "file" not in item:
            raise AtlasValidationError("Atlas catalog is missing resource")
        spec = _resource_spec(item, "catalog")
        specs.append(spec)

    for item in _as_list(raw.get("catalog_snapshots"), "catalog_snapshots"):
        if not isinstance(item, dict):
            raise AtlasValidationError(
                "Atlas manifest catalog_snapshots must be objects"
            )
        if "resource" not in item and "path" not in item and "file" not in item:
            raise AtlasValidationError("Atlas catalog snapshot is missing resource")
        spec = _resource_spec(item, "catalog_snapshot")
        if spec.snapshot_id is None:
            snapshot_id = item.get("id")
            if snapshot_id is None:
                snapshot_id = PurePosixPath(spec.path).stem
            spec = _ResourceSpec(
                path=spec.path,
                kind=spec.kind,
                required=spec.required,
                max_bytes=spec.max_bytes,
                sha256=spec.sha256,
                record_count=spec.record_count,
                vendor_id=spec.vendor_id,
                expected_product_count=spec.expected_product_count,
                expected_name_digest=spec.expected_name_digest,
                coverage=spec.coverage,
                source_base_url=spec.source_base_url,
                index_sources=spec.index_sources,
                catalog_as_of=spec.catalog_as_of,
                fl_studio_version=spec.fl_studio_version,
                source_snapshot=spec.source_snapshot,
                category_counts=spec.category_counts,
                snapshot_id=_text(snapshot_id, "snapshot_id"),
                catalog_scope=spec.catalog_scope,
                expected_row_count=spec.expected_row_count,
                expected_digest=spec.expected_digest,
            )
        specs.append(spec)

    for key, kind in (
        ("techniques_resource", "techniques"),
        ("adapters_resource", "adapters"),
        ("compatibility_resource", "evidence"),
        ("evidence_resource", "evidence"),
    ):
        value = raw.get(key)
        if value is not None:
            specs.append(_resource_spec(value, kind))

    # A short manifest may declare a single top-level catalog resource.
    for key in ("catalog_resource", "atlas_resource"):
        value = raw.get(key)
        if value is not None:
            specs.append(_resource_spec(value, "catalog"))

    if not specs:
        specs.append(_ResourceSpec("atlas.json", "catalog", True, MAX_RESOURCE_BYTES, None, None))

    seen: set[str] = set()
    for spec in specs:
        if spec.path in seen:
            raise AtlasValidationError(f"Atlas manifest declares duplicate resource: {spec.path}")
        seen.add(spec.path)
    return tuple(specs)


def _validate_manifest_keys(raw: dict[str, Any]) -> None:
    allowed = {
        "schema_version",
        "dataset_id",
        "atlas_id",
        "dataset_version",
        "atlas_version",
        "version",
        "catalog_as_of",
        "fl_studio_version",
        "source_snapshot",
        "resources",
        "catalogs",
        "catalog_snapshots",
        "techniques_resource",
        "adapters_resource",
        "compatibility_resource",
        "evidence_resource",
        "catalog_resource",
        "atlas_resource",
    }
    for label in ("products", "adapters", "vendors", "techniques", "evidence"):
        allowed.add(f"expected_{label}")
        allowed.add(f"expected_{label}_count")
    unknown = sorted(set(raw).difference(allowed))
    if unknown:
        raise AtlasValidationError(
            f"Atlas manifest has unknown fields: {', '.join(unknown)}"
        )


def _manifest_model(raw: dict[str, Any], specs: tuple[_ResourceSpec, ...]) -> AtlasManifest:
    values: dict[str, Any] = {
        "schema_version": raw.get("schema_version", "1.0"),
        "dataset_id": raw.get("dataset_id", raw.get("atlas_id", "postfader-plugin-atlas")),
        "dataset_version": raw.get(
            "dataset_version", raw.get("atlas_version", raw.get("version", "1.0"))
        ),
        "catalog_as_of": raw.get("catalog_as_of"),
        "fl_studio_version": raw.get("fl_studio_version"),
        "source_snapshot": raw.get("source_snapshot"),
        "resources": tuple(
            ResourceManifest(
                path=spec.path,
                kind=cast(Any, spec.kind),
                required=spec.required,
                max_bytes=spec.max_bytes,
                sha256=spec.sha256,
                record_count=spec.record_count,
                expected_name_digest=spec.expected_name_digest,
            )
            for spec in specs
        ),
        "catalogs": tuple(
            CatalogManifestEntry(
                resource=spec.path,
                vendor_id=spec.vendor_id,
                coverage=spec.coverage or "unknown",
                expected_product_count=spec.expected_product_count,
                expected_name_digest=spec.expected_name_digest,
                source_base_url=spec.source_base_url,
                index_sources=spec.index_sources,
                catalog_as_of=spec.catalog_as_of,
                fl_studio_version=spec.fl_studio_version,
                source_snapshot=spec.source_snapshot,
                category_counts=spec.category_counts,
            )
            for spec in specs
            if spec.kind == "catalog" and spec.vendor_id is not None
        ),
        "catalog_snapshots": tuple(
            CatalogSnapshotManifestEntry(
                resource=spec.path,
                snapshot_id=spec.snapshot_id,
                vendor_id=spec.vendor_id,
                catalog_scope=cast(Any, spec.catalog_scope or "unknown"),
                expected_row_count=spec.expected_row_count,
                expected_digest=spec.expected_digest,
                catalog_as_of=spec.catalog_as_of,
                fl_studio_version=spec.fl_studio_version,
                source_snapshot=spec.source_snapshot,
                category_counts=spec.category_counts,
            )
            for spec in specs
            if spec.kind == "catalog_snapshot"
            and spec.snapshot_id is not None
            and spec.vendor_id is not None
        ),
    }
    aliases = {
        "products": "expected_products",
        "adapters": "expected_adapters",
        "vendors": "expected_vendors",
        "techniques": "expected_techniques",
        "evidence": "expected_evidence",
    }
    for source, destination in aliases.items():
        value = raw.get(destination, raw.get(f"expected_{source}_count"))
        if value is not None:
            values[destination] = value
    try:
        return AtlasManifest.model_validate(values)
    except ValidationError as exc:
        raise AtlasValidationError(f"invalid Atlas manifest: {exc}") from exc


def _records(raw: Any, kind: str) -> list[dict[str, Any]]:
    """Extract records from either a list or a partitioned JSON object."""

    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, dict):
        key_map = {
            "products": ("products", "plugins", "items"),
            "vendors": ("vendors",),
            "techniques": ("techniques",),
            "adapters": ("adapters", "control_adapters", "plugin_adapters", "profiles"),
            "evidence": ("evidence", "compatibility", "write_validation"),
            "catalog_snapshot": ("rows", "catalog_rows", "products", "items"),
            "catalog": ("products", "plugins", "items"),
        }
        values: list[Any] = []
        if kind == "catalog_snapshot" and isinstance(raw.get("snapshot"), dict):
            snapshot = raw["snapshot"]
            for key in key_map[kind]:
                candidate = snapshot.get(key)
                if candidate is not None:
                    values.extend(_as_list(candidate, f"snapshot.{key}"))
        for key in key_map.get(kind, (kind,)):
            candidate = raw.get(key)
            if candidate is not None:
                values.extend(_as_list(candidate, key))
        if not values and kind == "vendors" and isinstance(raw.get("vendor"), dict):
            values.append(raw["vendor"])
        if not values and "records" in raw:
            values = _as_list(raw["records"], "records")
        if not values and kind in {"products", "techniques", "vendors", "adapters"}:
            # Mapping form: {"product-id": { ... }}.  Do not accept arbitrary
            # metadata keys as records; each value must be an object.
            reserved = {
                "schema_version",
                "vendor",
                "coverage",
                "source_base_url",
                "index_sources",
                "catalog_as_of",
            }
            candidates = [(key, value) for key, value in raw.items() if key not in reserved]
            if candidates and all(isinstance(value, dict) for _, value in candidates):
                for key, value in candidates:
                    item = dict(value)
                    item.setdefault("id", key)
                    values.append(item)
    else:
        raise AtlasValidationError(f"Atlas {kind} resource must be an object or array")
    if len(values) > MAX_EVIDENCE:
        raise AtlasLoadError(f"Atlas {kind} resource contains too many records")
    result: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            raise AtlasValidationError(f"Atlas {kind} records must be objects")
        result.append(item)
    return result


def _validate_resource_root(raw: Any, kind: str, label: str) -> None:
    if not isinstance(raw, dict):
        return
    common = {"schema_version", "records"}
    allowed_by_kind = {
        "catalog": common
        | {
            "vendor",
            "vendors",
            "products",
            "plugins",
            "items",
            "coverage",
            "source_base_url",
            "index_sources",
            "catalog_as_of",
            "fl_studio_version",
            "source_snapshot",
        },
        "products": common | {"products", "plugins", "items"},
        "vendors": common | {"vendor", "vendors"},
        "techniques": common | {"techniques"},
        "adapters": common | {"adapters", "control_adapters"},
        "evidence": common
        | {"evidence", "compatibility", "write_validation"},
        "catalog_snapshot": common
        | {"snapshot", "rows", "catalog_rows", "products", "items"},
    }
    unknown = sorted(set(raw).difference(allowed_by_kind[kind]))
    if unknown:
        raise AtlasValidationError(
            f"Atlas resource {label!r} has unknown root fields: {', '.join(unknown)}"
        )


def _id_value(record: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if value is not None:
            if type(value) is not str or not value.strip():
                raise AtlasValidationError(f"Atlas identifier {key} must be non-empty text")
            return value.strip()
    return None


def _list_text(record: dict[str, Any], *keys: str) -> list[Any]:
    for key in keys:
        if key in record:
            return _as_list(record[key], key)
    return []


def _identifier_list(value: Any, field: str) -> list[str]:
    values = _as_list(value, field)
    result: list[str] = []
    for item in values:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            identifier = _id_value(item, "id", f"{field[:-1]}_id", "technique_id")
            if identifier is None:
                raise AtlasValidationError(f"Atlas {field} reference is missing an id")
            result.append(identifier)
        else:
            raise AtlasValidationError(f"Atlas {field} references must be strings or objects")
    return result


def _module_values(value: Any) -> list[dict[str, Any]]:
    values = _as_list(value, "modules")
    result: list[dict[str, Any]] = []
    for position, item in enumerate(values):
        if isinstance(item, str):
            result.append({"id": f"module-{position + 1}", "name": item})
        elif isinstance(item, dict):
            result.append(item)
        else:
            raise AtlasValidationError("Atlas modules must be strings or objects")
    return result


def _normalise_vendor(record: dict[str, Any], fallback_id: str | None = None) -> dict[str, Any]:
    if isinstance(record.get("vendor"), dict):
        merged = dict(record["vendor"])
        for key in ("evidence_ids", "description", "website", "origin"):
            if key in record and key not in merged:
                merged[key] = record[key]
        record = merged
    result = dict(record)
    if _id_value(result, "vendor_id", "id") is None and fallback_id is not None:
        result["vendor_id"] = fallback_id
    return result


def _normalise_product(
    record: dict[str, Any], fallback_vendor_id: str | None = None
) -> dict[str, Any]:
    result = dict(record)
    if _id_value(result, "product_id", "id") is None:
        raise AtlasValidationError("Atlas product is missing product_id")
    if result.get("vendor_id") is None and fallback_vendor_id is not None:
        result["vendor_id"] = fallback_vendor_id
    for source, destination in (
        ("problems_solved", "problems"),
        ("appropriate_for", "use_cases"),
        ("major_modules", "modules"),
    ):
        if source in result:
            if destination not in result:
                result[destination] = result[source]
            result.pop(source, None)
    if "summary" in result and "description" not in result:
        result["description"] = result["summary"]
    result.pop("summary", None)
    if "plugin_kinds" in result:
        result["plugin_kinds"] = _list_text(result, "plugin_kinds")
    if "techniques" in result:
        result["technique_ids"] = _identifier_list(result["techniques"], "techniques")
        result.pop("techniques", None)
    if "stock_alternatives" in result:
        result["stock_alternative_ids"] = _identifier_list(
            result["stock_alternatives"], "stock_alternatives"
        )
        result.pop("stock_alternatives", None)
    if "modules" in result:
        result["modules"] = _module_values(result["modules"])
    if result.get("kind") is None and result.get("plugin_kinds"):
        first = result["plugin_kinds"][0]
        if first in {"effect", "instrument", "utility", "analyzer"}:
            result["kind"] = first
    return result


def _normalise_technique(record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    if "summary" in result and "description" not in result:
        result["description"] = result["summary"]
    return result


def _normalise_adapter(record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    if "profile_id" in result and "adapter_id" not in result and "id" not in result:
        result["adapter_id"] = result["profile_id"]
    result.pop("profile_id", None)
    if _id_value(result, "adapter_id", "id") is None:
        raise AtlasValidationError("Atlas adapter is missing adapter_id")
    if "control_evidence" in result and "controls" not in result:
        result["controls"] = result["control_evidence"]
    result.pop("control_evidence", None)
    if "parameters" in result and "controls" not in result:
        result["controls"] = result["parameters"]
    result.pop("parameters", None)
    if "plugin_names" in result and "reported_names" not in result:
        result["reported_names"] = result["plugin_names"]
    result.pop("plugin_names", None)
    controls: list[dict[str, Any]] = []
    for position, item in enumerate(_as_list(result.get("controls"), "controls")):
        if isinstance(item, str):
            item = {"id": f"control-{position + 1}", "names": [item]}
        elif not isinstance(item, dict):
            raise AtlasValidationError("Atlas adapter controls must be objects or strings")
        control = dict(item)
        control.setdefault("id", f"control-{position + 1}")
        if "name_candidates" in control and "names" not in control:
            control["names"] = control["name_candidates"]
        control.pop("name_candidates", None)
        if "display_unit" in control and "unit" not in control:
            control["unit"] = control["display_unit"]
        control.pop("display_unit", None)
        controls.append(control)
    result["controls"] = controls
    if "parameter_names" in result and not controls:
        result["controls"] = [
            {"id": f"control-{index + 1}", "names": [name]}
            for index, name in enumerate(_as_list(result["parameter_names"], "parameter_names"))
        ]
    if "recipe" in result and "recipes" not in result:
        result["recipes"] = result["recipe"]
    result.pop("recipe", None)
    return result


def catalog_name_digest(names: Iterable[str]) -> str:
    """Return the stable SHA-256 digest used by catalog completeness oracles.

    Names are trimmed, sorted, and encoded as UTF-8 lines with one final
    newline.  This keeps the oracle human-reviewable while making it
    independent of JSON whitespace or object key order.  Only product names
    are included; aliases and descriptions do not affect the oracle.
    """

    normalised = sorted(value.strip() for value in names)
    payload = ("\n".join(normalised) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def catalog_snapshot_digest(rows: Iterable[CatalogProductRow]) -> str:
    """Digest sorted ``category<TAB>edition<TAB>name`` snapshot rows.

    The category order is part of the oracle contract.  Names are sorted by
    their UTF-8 bytes rather than locale or platform collation; a final LF is
    always included.
    """

    category_order = {
        "audio_editor": 0,
        "effect": 1,
        "instrument": 2,
        "visual": 3,
    }
    ordered = sorted(
        rows,
        key=lambda row: (
            category_order[row.category],
            row.name.strip().encode("utf-8"),
            row.edition_min,
        ),
    )
    lines = (
        f"{row.category}\t{row.edition_min}\t{row.name.strip()}" for row in ordered
    )
    return hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()


def _normalise_snapshot_row(record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    if "edition" in result and "edition_min" not in result:
        result["edition_min"] = result["edition"]
    result.pop("edition", None)
    if "product_name" in result and "name" not in result:
        result["name"] = result["product_name"]
    result.pop("product_name", None)
    return result


def _resolve_snapshot_rows(
    rows: Iterable[CatalogProductRow],
    vendor_id: str,
    products: Iterable[ProductKnowledge],
) -> tuple[CatalogProductRow, ...]:
    vendor_products = tuple(item for item in products if item.vendor_id == vendor_id)
    resolved: list[CatalogProductRow] = []
    for row in rows:
        needle = _NAME_SEPARATOR_RE.sub(" ", row.name.casefold()).strip()
        candidates = tuple(
            product
            for product in vendor_products
            if needle in {
                _NAME_SEPARATOR_RE.sub(" ", label.casefold()).strip()
                for label in (product.name, *product.aliases)
            }
        )
        if len(candidates) != 1:
            reason = "no product" if not candidates else "multiple products"
            raise AtlasValidationError(
                f"catalog row {row.name!r} has {reason} for vendor {vendor_id!r}"
            )
        if row.product_id is not None and row.product_id != candidates[0].product_id:
            raise AtlasValidationError(
                f"catalog row {row.name!r} names product {candidates[0].product_id!r} "
                f"but declares {row.product_id!r}"
            )
        resolved.append(row.model_copy(update={"product_id": candidates[0].product_id}))
    resolved_ids = [row.product_id for row in resolved]
    if len(resolved_ids) != len({identifier for identifier in resolved_ids}):
        raise AtlasValidationError(
            f"catalog snapshot for vendor {vendor_id!r} contains duplicate products"
        )
    return tuple(resolved)


def _normalise_evidence(record: dict[str, Any], position: int) -> dict[str, Any]:
    result = dict(record)
    result.setdefault("id", f"evidence-{position + 1}")
    product = result.get("product")
    if isinstance(product, dict):
        result.setdefault("product_id", _id_value(product, "product_id", "id"))
    elif isinstance(product, str):
        result.setdefault("product_id", product)
    result.pop("product", None)
    adapter = result.get("adapter")
    if isinstance(adapter, dict):
        result.setdefault("adapter_id", _id_value(adapter, "adapter_id", "id"))
    elif isinstance(adapter, str):
        result.setdefault("adapter_id", adapter)
    result.pop("adapter", None)
    if "level" not in result and "evidence_level" in result:
        result["level"] = result["evidence_level"]
    if "status" not in result and "validation_status" in result:
        result["status"] = result["validation_status"]
    if result.get("basis") == "readback":
        result["basis"] = "readback_on_a_later_fl_idle_tick"
    elif result.get("basis") == "static":
        result["basis"] = "static_documentation"
    return result


def _parse_model(
    constructor: Any, record: dict[str, Any], label: str
) -> Any:
    try:
        # ``AtlasModel`` uses strict mode and immutable tuple fields.  Parsing
        # through Pydantic's JSON entry point preserves strict scalar checks
        # while correctly accepting JSON arrays for those tuple fields.
        encoded = json.dumps(record, ensure_ascii=True, allow_nan=False)
        return constructor.model_validate_json(encoded)
    except ValidationError as exc:
        raise AtlasValidationError(f"invalid {label}: {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise AtlasValidationError(f"invalid {label}: {exc}") from exc


def _unique_ids(records: Iterable[Any], field: str, label: str) -> None:
    seen: dict[str, str] = {}
    for record in records:
        identifier = getattr(record, field)
        normalised = identifier.strip().casefold()
        if normalised in seen:
            raise AtlasValidationError(
                f"duplicate {label} identifier after normalization: "
                f"{seen[normalised]!r} and {identifier!r}"
            )
        seen[normalised] = identifier


def _validate_unique_product_labels(products: Iterable[ProductKnowledge]) -> None:
    """Reject a name/alias that would resolve to more than one product."""

    labels: dict[str, tuple[str, str]] = {}
    for product in products:
        for label in (product.name, *product.aliases):
            normalised = _NAME_SEPARATOR_RE.sub(" ", label.casefold()).strip()
            previous = labels.get(normalised)
            if previous is not None:
                if previous[0] == product.product_id:
                    raise AtlasValidationError(
                        f"duplicate alias or name in product {product.product_id!r}: "
                        f"{previous[1]!r} and {label!r}"
                    )
                raise AtlasValidationError(
                    "ambiguous Atlas product name or alias after normalization: "
                    f"{previous[1]!r} ({previous[0]}) and {label!r} "
                    f"({product.product_id})"
                )
            labels[normalised] = (product.product_id, label)


def _validate_cross_references(bundle: AtlasBundle) -> None:
    vendor_ids = {item.vendor_id for item in bundle.vendors}
    technique_ids = {item.technique_id for item in bundle.techniques}
    product_ids = {item.product_id for item in bundle.products}
    adapter_ids = {item.adapter_id for item in bundle.adapters}
    evidence_ids = {item.evidence_id for item in bundle.evidence}

    for product in bundle.products:
        if product.vendor_id != "unknown" and product.vendor_id not in vendor_ids:
            raise AtlasValidationError(
                f"product {product.product_id!r} references unknown vendor {product.vendor_id!r}"
            )
        for identifier in product.technique_ids:
            if identifier not in technique_ids:
                raise AtlasValidationError(
                    f"product {product.product_id!r} references unknown technique {identifier!r}"
                )
        for identifier in product.stock_alternative_ids:
            if identifier not in product_ids:
                raise AtlasValidationError(
                    f"product {product.product_id!r} references unknown stock alternative {identifier!r}"
                )
        for identifier in product.evidence_ids:
            if identifier not in evidence_ids:
                raise AtlasValidationError(
                    f"product {product.product_id!r} references unknown evidence {identifier!r}"
                )

    for vendor in bundle.vendors:
        for identifier in vendor.evidence_ids:
            if identifier not in evidence_ids:
                raise AtlasValidationError(
                    f"vendor {vendor.vendor_id!r} references unknown evidence {identifier!r}"
                )
    for technique in bundle.techniques:
        for identifier in technique.evidence_ids:
            if identifier not in evidence_ids:
                raise AtlasValidationError(
                    f"technique {technique.technique_id!r} references unknown evidence {identifier!r}"
                )

    for adapter in bundle.adapters:
        if adapter.product_id not in product_ids:
            raise AtlasValidationError(
                f"adapter {adapter.adapter_id!r} references unknown product {adapter.product_id!r}"
            )
        control_ids = [control.control_id for control in adapter.controls]
        if len(control_ids) != len({identifier.casefold() for identifier in control_ids}):
            raise AtlasValidationError(f"adapter {adapter.adapter_id!r} has duplicate controls")
        for identifier in adapter.evidence_ids:
            if identifier not in evidence_ids:
                raise AtlasValidationError(
                    f"adapter {adapter.adapter_id!r} references unknown evidence {identifier!r}"
                )
        for control in adapter.controls:
            for identifier in control.evidence_ids:
                if identifier not in evidence_ids:
                    raise AtlasValidationError(
                        f"control {control.control_id!r} references unknown evidence "
                        f"{identifier!r}"
                    )

    for item in bundle.evidence:
        if isinstance(item, EvidenceReference):
            continue
        if item.product_id not in product_ids:
            raise AtlasValidationError(
                f"evidence {item.evidence_id!r} references unknown product {item.product_id!r}"
            )
        if item.adapter_id is not None:
            if item.adapter_id not in adapter_ids:
                raise AtlasValidationError(
                    f"evidence {item.evidence_id!r} references unknown adapter {item.adapter_id!r}"
                )
            adapter = next(
                candidate
                for candidate in bundle.adapters
                if candidate.adapter_id == item.adapter_id
            )
            if adapter.product_id != item.product_id:
                raise AtlasValidationError(
                    f"evidence {item.evidence_id!r} joins product {item.product_id!r} "
                    f"to adapter {item.adapter_id!r} for product {adapter.product_id!r}"
                )
            known_controls = {control.control_id for control in adapter.controls}
            for identifier in item.control_ids:
                if identifier not in known_controls:
                    raise AtlasValidationError(
                        f"evidence {item.evidence_id!r} references unknown control {identifier!r}"
                    )
        elif item.control_ids:
            raise AtlasValidationError(
                f"evidence {item.evidence_id!r} lists controls without an adapter"
            )

    for snapshot in bundle.catalog_snapshots:
        if snapshot.vendor_id not in vendor_ids:
            raise AtlasValidationError(
                f"catalog snapshot {snapshot.snapshot_id!r} references unknown vendor "
                f"{snapshot.vendor_id!r}"
            )
        for row in snapshot.rows:
            if row.product_id is None or row.product_id not in product_ids:
                raise AtlasValidationError(
                    f"catalog snapshot {snapshot.snapshot_id!r} has an unresolved product row"
                )

    expected = {
        "products": bundle.manifest.expected_products,
        "adapters": bundle.manifest.expected_adapters,
        "vendors": bundle.manifest.expected_vendors,
        "techniques": bundle.manifest.expected_techniques,
        "evidence": bundle.manifest.expected_evidence,
    }
    actual = {
        "products": len(bundle.products),
        "adapters": len(bundle.adapters),
        "vendors": len(bundle.vendors),
        "techniques": len(bundle.techniques),
        "evidence": len(bundle.evidence),
    }
    for label, value in expected.items():
        if value is not None and value != actual[label]:
            raise AtlasValidationError(
                f"Atlas manifest expected {value} {label}, loaded {actual[label]}"
            )


def _load_bundle(root: Any, limits: LoaderLimits) -> AtlasBundle:
    if _is_file(root):
        manifest_resource = root
        if isinstance(root, Path):
            root = root.parent
        else:
            parent = getattr(root, "parent", None)
            if parent is not None:
                root = parent
    else:
        manifest_resource = None
        for candidate in DEFAULT_MANIFEST_NAMES:
            try:
                path = _resolve_child(root, candidate)
            except AtlasValidationError:
                continue
            if _is_file(path):
                manifest_resource = path
                break

    if manifest_resource is None:
        raise AtlasLoadError("Atlas root does not contain a manifest JSON resource")
    manifest_raw = _json_load(
        _read_resource(manifest_resource, "manifest.json", limits.max_resource_bytes),
        "manifest.json",
        limits,
    )
    if not isinstance(manifest_raw, dict):
        raise AtlasValidationError("Atlas manifest must be a JSON object")
    _validate_manifest_keys(manifest_raw)
    specs = _manifest_specs(manifest_raw)
    if len(specs) > min(limits.max_resources, MAX_RESOURCE_COUNT):
        raise AtlasLoadError("Atlas manifest declares too many resources")
    manifest = _manifest_model(manifest_raw, specs)

    vendors: list[VendorKnowledge] = []
    techniques: list[TechniqueKnowledge] = []
    products: list[ProductKnowledge] = []
    adapters: list[ControlAdapter] = []
    evidence: list[
        EvidenceReference | CompatibilityEvidence | WriteValidationEvidence
    ] = []
    catalog_products: dict[str, list[ProductKnowledge]] = {}
    pending_snapshots: list[tuple[_ResourceSpec, tuple[CatalogProductRow, ...]]] = []
    loaded_paths: set[str] = set()
    total_bytes = 0

    for spec in specs:
        if spec.path in loaded_paths:
            raise AtlasValidationError(f"duplicate Atlas resource path: {spec.path}")
        loaded_paths.add(spec.path)
        resource = _resolve_child(root, spec.path)
        try:
            raw_bytes = _read_resource(resource, spec.path, min(spec.max_bytes, limits.max_resource_bytes))
        except AtlasLoadError:
            if spec.required:
                raise
            continue
        total_bytes += len(raw_bytes)
        if total_bytes > limits.max_total_resource_bytes:
            raise AtlasLoadError("Atlas resources exceed the total byte limit")
        if spec.sha256 is not None:
            digest = hashlib.sha256(raw_bytes).hexdigest()
            if digest != spec.sha256:
                raise AtlasValidationError(f"Atlas resource {spec.path!r} failed sha256 validation")
        resource_raw = _json_load(raw_bytes, spec.path, limits)
        _validate_resource_root(resource_raw, spec.kind, spec.path)
        if spec.record_count is not None:
            count = len(_records(resource_raw, spec.kind))
            if count != spec.record_count:
                raise AtlasValidationError(
                    f"Atlas resource {spec.path!r} declares {spec.record_count} records, loaded {count}"
                )

        if spec.kind in {"catalog", "vendors"}:
            for record in _records(resource_raw, "vendors"):
                vendor = _normalise_vendor(record, spec.vendor_id)
                vendors.append(_parse_model(VendorKnowledge, vendor, "vendor"))
        if spec.kind in {"catalog", "products"}:
            # A vendor-file catalog carries its vendor alongside products.
            fallback_vendor = spec.vendor_id
            fallback_origin: str | None = None
            if isinstance(resource_raw, dict) and isinstance(resource_raw.get("vendor"), dict):
                fallback_vendor = _id_value(resource_raw["vendor"], "vendor_id", "id") or fallback_vendor
                raw_origin = resource_raw["vendor"].get("origin")
                if isinstance(raw_origin, str):
                    fallback_origin = raw_origin
            parsed_products: list[ProductKnowledge] = []
            for record in _records(resource_raw, "products"):
                product = _normalise_product(record, fallback_vendor)
                if product.get("origin") is None and fallback_origin is not None:
                    product["origin"] = fallback_origin
                parsed = _parse_model(ProductKnowledge, product, "product")
                products.append(parsed)
                parsed_products.append(parsed)
            catalog_products[spec.path] = parsed_products
        if spec.kind == "techniques":
            for record in _records(resource_raw, "techniques"):
                techniques.append(
                    _parse_model(TechniqueKnowledge, _normalise_technique(record), "technique")
                )
        if spec.kind == "adapters":
            for record in _records(resource_raw, "adapters"):
                adapters.append(
                    _parse_model(ControlAdapter, _normalise_adapter(record), "control adapter")
                )
        if spec.kind == "evidence":
            for position, record in enumerate(_records(resource_raw, "evidence")):
                normalised = _normalise_evidence(record, position)
                if "product_id" not in normalised:
                    evidence.append(
                        _parse_model(
                            EvidenceReference,
                            normalised,
                            "evidence reference",
                        )
                    )
                elif "status" in normalised:
                    evidence.append(
                        _parse_model(
                            WriteValidationEvidence,
                            normalised,
                            "write-validation evidence",
                        )
                    )
                else:
                    evidence.append(
                        _parse_model(
                            CompatibilityEvidence,
                            normalised,
                            "compatibility evidence",
                        )
                    )
        if spec.kind == "catalog_snapshot":
            if spec.snapshot_id is None or spec.vendor_id is None:
                raise AtlasValidationError(
                    f"Atlas snapshot resource {spec.path!r} needs snapshot_id and vendor_id"
                )
            rows = tuple(
                _parse_model(
                    CatalogProductRow,
                    _normalise_snapshot_row(record),
                    "catalog snapshot row",
                )
                for record in _records(resource_raw, "catalog_snapshot")
            )
            pending_snapshots.append((spec, rows))

    if len(vendors) > MAX_VENDORS:
        raise AtlasLoadError("Atlas contains too many vendors")
    if len(techniques) > MAX_TECHNIQUES:
        raise AtlasLoadError("Atlas contains too many techniques")
    if len(products) > MAX_PRODUCTS:
        raise AtlasLoadError("Atlas contains too many products")
    if len(adapters) > MAX_ADAPTERS:
        raise AtlasLoadError("Atlas contains too many adapters")
    if len(evidence) > MAX_EVIDENCE:
        raise AtlasLoadError("Atlas contains too many evidence records")

    _unique_ids(vendors, "vendor_id", "vendor")
    _unique_ids(techniques, "technique_id", "technique")
    _unique_ids(products, "product_id", "product")
    _validate_unique_product_labels(products)
    _unique_ids(adapters, "adapter_id", "adapter")
    _unique_ids(evidence, "evidence_id", "evidence")

    snapshots: list[CatalogSnapshot] = []
    for spec, rows in pending_snapshots:
        if spec.snapshot_id is None or spec.vendor_id is None:
            raise AtlasValidationError("Atlas snapshot metadata is incomplete")
        resolved_rows = _resolve_snapshot_rows(rows, spec.vendor_id, products)
        if spec.expected_row_count is not None and len(resolved_rows) != spec.expected_row_count:
            raise AtlasValidationError(
                f"Atlas snapshot {spec.snapshot_id!r} expected "
                f"{spec.expected_row_count} rows, loaded {len(resolved_rows)}"
            )
        snapshot_digest = catalog_snapshot_digest(resolved_rows)
        if spec.expected_digest is not None and snapshot_digest != spec.expected_digest:
            raise AtlasValidationError(
                f"Atlas snapshot {spec.snapshot_id!r} failed expected digest validation"
            )
        if spec.category_counts:
            actual_categories: dict[str, int] = {}
            for row in resolved_rows:
                actual_categories[row.category] = actual_categories.get(row.category, 0) + 1
            for expected in spec.category_counts:
                if actual_categories.get(expected.category, 0) != expected.count:
                    raise AtlasValidationError(
                        f"Atlas snapshot {spec.snapshot_id!r} has an unexpected "
                        f"{expected.category} row count"
                    )
        snapshots.append(
            CatalogSnapshot(
                snapshot_id=spec.snapshot_id,
                vendor_id=spec.vendor_id,
                catalog_scope=cast(Any, spec.catalog_scope or "unknown"),
                rows=resolved_rows,
                catalog_as_of=spec.catalog_as_of,
                expected_row_count=spec.expected_row_count,
                expected_digest=spec.expected_digest,
                digest=snapshot_digest,
            )
        )
    if len(snapshots) != len({item.snapshot_id for item in snapshots}):
        raise AtlasValidationError("duplicate Atlas catalog snapshot identifier")

    for spec in specs:
        if spec.expected_product_count is None and spec.expected_name_digest is None:
            continue
        loaded = catalog_products.get(spec.path, [])
        if spec.expected_product_count is not None and (
            len(loaded) != spec.expected_product_count
        ):
            raise AtlasValidationError(
                f"Atlas catalog {spec.path!r} expected "
                f"{spec.expected_product_count} products, loaded {len(loaded)}"
            )
        if spec.expected_name_digest is not None:
            digest = catalog_name_digest(product.name for product in loaded)
            if digest != spec.expected_name_digest:
                raise AtlasValidationError(
                    f"Atlas catalog {spec.path!r} failed expected product-name digest"
                )
        if spec.category_counts:
            actual_categories: dict[str, int] = {}
            for product in loaded:
                actual_categories[product.kind] = actual_categories.get(product.kind, 0) + 1
            for expected in spec.category_counts:
                if actual_categories.get(expected.category, 0) != expected.count:
                    raise AtlasValidationError(
                        f"Atlas catalog {spec.path!r} has an unexpected "
                        f"{expected.category} product count"
                    )
    try:
        bundle = AtlasBundle(
            manifest=manifest,
            vendors=tuple(vendors),
            techniques=tuple(techniques),
            products=tuple(products),
            adapters=tuple(adapters),
            evidence=tuple(evidence),
            catalog_snapshots=tuple(snapshots),
        )
    except ValidationError as exc:
        raise AtlasValidationError(f"invalid Atlas bundle: {exc}") from exc
    _validate_cross_references(bundle)
    return bundle


class AtlasLoader:
    """Reusable safe loader for bundled or test-supplied Traversable roots."""

    def __init__(
        self,
        root: Path | Traversable | str | None = None,
        *,
        limits: LoaderLimits | None = None,
    ) -> None:
        self.root = root
        self.limits = limits or LoaderLimits()

    def load(self) -> AtlasBundle:
        return _load_bundle(_root_path(self.root), self.limits)


def load_atlas(
    root: Path | Traversable | str | None = None,
    *,
    limits: LoaderLimits | None = None,
) -> AtlasBundle:
    """Load and validate one local Atlas bundle.

    ``root`` may be a directory or a manifest file.  Passing ``None`` selects
    the installed ``fl_studio_mcp.plugin_atlas_data`` package through
    ``importlib.resources``; no network or unrestricted filesystem search is
    performed.
    """

    return AtlasLoader(root, limits=limits).load()


load_bundle = load_atlas


__all__ = [
    "AtlasLoadError",
    "AtlasLoader",
    "AtlasValidationError",
    "DEFAULT_DATA_PACKAGE",
    "LoaderLimits",
    "catalog_name_digest",
    "catalog_snapshot_digest",
    "load_atlas",
    "load_bundle",
]
