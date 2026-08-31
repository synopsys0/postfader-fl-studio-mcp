"""Joins between static knowledge, runtime matches, and evidence."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .matcher import match_runtime_plugin
from .models import (
    AvailabilityObservation,
    CompatibilityEvidence,
    CompatibilityJoin,
    EvidenceReference,
    RuntimeMatch,
    RuntimePluginInstance,
    WriteValidationEvidence,
)
from .registry import AtlasRegistry


def _as_match(
    value: RuntimeMatch | RuntimePluginInstance | Mapping[str, Any] | None,
    registry: AtlasRegistry,
) -> RuntimeMatch | None:
    if value is None or isinstance(value, RuntimeMatch):
        return value
    return match_runtime_plugin(value, registry)


def _evidence_for(
    registry: AtlasRegistry,
    product_id: str | None,
    adapter_id: str | None,
) -> tuple[tuple[CompatibilityEvidence, ...], tuple[WriteValidationEvidence, ...]]:
    compatibility: list[CompatibilityEvidence] = []
    writes: list[WriteValidationEvidence] = []
    if product_id is None:
        return (), ()
    for item in registry.evidence:
        if isinstance(item, EvidenceReference) or item.product_id != product_id:
            continue
        if item.adapter_id is not None and item.adapter_id != adapter_id:
            continue
        if isinstance(item, WriteValidationEvidence):
            writes.append(item)
        else:
            compatibility.append(item)
    compatibility.sort(key=lambda item: item.evidence_id)
    writes.sort(key=lambda item: item.evidence_id)
    return tuple(compatibility), tuple(writes)


def _validate_supplied_evidence(
    product_id: str | None,
    adapter_id: str | None,
    evidence: tuple[CompatibilityEvidence, ...],
    writes: tuple[WriteValidationEvidence, ...],
) -> None:
    """Keep caller-supplied evidence bound to the runtime match it describes."""

    if product_id is None and (evidence or writes):
        raise ValueError("compatibility evidence requires a matched product")
    for item in evidence:
        if not isinstance(item, CompatibilityEvidence):
            raise TypeError("evidence must contain CompatibilityEvidence records")
        if item.product_id != product_id:
            raise ValueError(
                f"compatibility evidence {item.evidence_id!r} targets "
                f"product {item.product_id!r}, not {product_id!r}"
            )
        if item.adapter_id is not None and item.adapter_id != adapter_id:
            raise ValueError(
                f"compatibility evidence {item.evidence_id!r} targets "
                f"adapter {item.adapter_id!r}, not {adapter_id!r}"
            )
    for item in writes:
        if not isinstance(item, WriteValidationEvidence):
            raise TypeError(
                "write_validation must contain WriteValidationEvidence records"
            )
        if item.product_id != product_id:
            raise ValueError(
                f"write evidence {item.evidence_id!r} targets product "
                f"{item.product_id!r}, not {product_id!r}"
            )
        if item.adapter_id is not None and item.adapter_id != adapter_id:
            raise ValueError(
                f"write evidence {item.evidence_id!r} targets adapter "
                f"{item.adapter_id!r}, not {adapter_id!r}"
            )


def join_compatibility(
    match: RuntimeMatch | RuntimePluginInstance | Mapping[str, Any] | None,
    registry: AtlasRegistry,
    *,
    evidence: Iterable[CompatibilityEvidence] = (),
    write_validation: Iterable[WriteValidationEvidence] = (),
) -> CompatibilityJoin:
    """Create one explicit compatibility join.

    The function accepts a precomputed ``RuntimeMatch`` to avoid duplicate
    matching work, or a runtime inventory row and computes the strongest match.
    A product-name match is represented as ``name_only`` and can never set
    ``control_proven``.
    """

    if not isinstance(registry, AtlasRegistry):
        raise TypeError("registry must be an AtlasRegistry")
    runtime_match = _as_match(match, registry)
    if runtime_match is None:
        availability = AvailabilityObservation(state="availability_unknown")
        return CompatibilityJoin(availability=availability)
    product = (
        registry.product(runtime_match.product_id)
        if runtime_match.product_id is not None
        else None
    )
    adapter = (
        registry.adapter(runtime_match.adapter_id)
        if runtime_match.adapter_id is not None
        else None
    )
    static_compatibility, static_writes = _evidence_for(
        registry, runtime_match.product_id, runtime_match.adapter_id
    )
    supplied_compatibility = tuple(evidence)
    supplied_writes = tuple(write_validation)
    _validate_supplied_evidence(
        runtime_match.product_id,
        runtime_match.adapter_id,
        supplied_compatibility,
        supplied_writes,
    )
    all_compatibility = tuple(
        sorted(
            (*static_compatibility, *supplied_compatibility),
            key=lambda item: item.evidence_id,
        )
    )
    all_writes = tuple(
        sorted(
            (*static_writes, *supplied_writes),
            key=lambda item: item.evidence_id,
        )
    )
    observed_control_ids = {
        item.control_id for item in runtime_match.parameter_evidence
    }
    successful_write = any(
        item.status == "validated"
        and bool(observed_control_ids.intersection(item.control_ids))
        for item in all_writes
    )
    control_proven = runtime_match.control_proof
    adapter_match = adapter is not None and control_proven
    if successful_write and adapter_match:
        level = "write_validated"
    elif control_proven:
        level = "control_evidence"
    elif product is not None:
        level = "name_only"
    else:
        level = "unknown"
    limitations: list[str] = []
    if product is not None:
        limitations.extend(product.limitations)
        limitations.extend(product.poor_fit_when)
    if adapter is not None:
        limitations.extend(adapter.limitations)
    limitations.extend(runtime_match.warnings)
    return CompatibilityJoin(
        instance_id=runtime_match.instance_id,
        product_id=runtime_match.product_id,
        adapter_id=runtime_match.adapter_id,
        availability=runtime_match.availability,
        product_match=product is not None,
        adapter_match=adapter_match,
        control_proven=control_proven,
        compatibility=level,
        match_confidence=runtime_match.confidence,
        evidence=all_compatibility,
        write_validation=all_writes,
        limitations=tuple(dict.fromkeys(limitations)),
    )


def compatibility_join(
    match: RuntimeMatch | RuntimePluginInstance | Mapping[str, Any] | None,
    registry: AtlasRegistry,
    **kwargs: Any,
) -> CompatibilityJoin:
    return join_compatibility(match, registry, **kwargs)


def joins_for_runtime(
    runtime: RuntimePluginInstance | Mapping[str, Any],
    registry: AtlasRegistry,
    *,
    limit: int = 16,
) -> tuple[CompatibilityJoin, ...]:
    """Return ranked compatibility joins for one runtime instance."""

    from .matcher import match_runtime

    matches = match_runtime(runtime, registry, limit=limit)
    return tuple(join_compatibility(match, registry) for match in matches)


join_runtime_compatibility = join_compatibility


__all__ = [
    "compatibility_join",
    "join_compatibility",
    "join_runtime_compatibility",
    "joins_for_runtime",
]
