"""Deterministic runtime matching with explicit control-evidence boundaries."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from .models import (
    AdapterControl,
    ControlAdapter,
    ParameterMatchEvidence,
    ProductKnowledge,
    RuntimeMatch,
    RuntimeParameterObservation,
    RuntimePluginInstance,
)
from .registry import AtlasRegistry, normalize_search_text


MAX_RUNTIME_PARAMETERS = 8192
MAX_MATCHES = 128
def _coerce_runtime(value: RuntimePluginInstance | Mapping[str, Any]) -> RuntimePluginInstance:
    if isinstance(value, RuntimePluginInstance):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("runtime plug-in must be a RuntimePluginInstance or mapping")
    source = dict(value)
    plugin = source.get("plugin")
    if isinstance(plugin, Mapping):
        source.setdefault("name", plugin.get("name", plugin.get("plugin_name")))
    source.setdefault("name", source.get("plugin_name", source.get("reported_name")))
    if source.get("availability") is None:
        source["availability"] = {
            "state": source.get("availability_state", "loaded"),
            "source": "runtime_inventory",
        }
    elif isinstance(source["availability"], str):
        source["availability"] = {
            "state": source["availability"],
            "source": "runtime_inventory",
        }
    rows = source.get("parameters", source.get("params", ()))
    if rows is None:
        rows = ()
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise ValueError("runtime parameter observations must be an array")
    normalised_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("runtime parameter observations must be objects")
        item = dict(row)
        if "name" not in item:
            item["name"] = item.get("reported_name")
        if "display" not in item:
            item["display"] = item.get("display_text")
        item.pop("reported_name", None)
        item.pop("display_text", None)
        normalised_rows.append(item)
    source["parameters"] = normalised_rows
    # Ignore bridge-only fields while still allowing the public model to fail
    # closed for actual Atlas fields.
    allowed = {
        "instance_id",
        "name",
        "plugin_name",
        "reported_name",
        "user_name",
        "format",
        "plugin_format",
        "availability",
        "availability_state",
        "parameters",
        "params",
    }
    source = {key: item for key, item in source.items() if key in allowed}
    for alias in (
        "plugin_name",
        "reported_name",
        "plugin_format",
        "availability_state",
        "params",
    ):
        source.pop(alias, None)
    try:
        encoded = json.dumps(source, ensure_ascii=True, allow_nan=False)
        return RuntimePluginInstance.model_validate_json(encoded)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ValueError(f"invalid runtime plug-in observation: {exc}") from exc


def _label(value: str | None) -> str:
    if not value:
        return ""
    return normalize_search_text(value)


def _text_similarity(observed: str, expected: Sequence[str]) -> tuple[float, str]:
    actual = _label(observed)
    if not actual:
        return 0.0, ""
    best = (0.0, "")
    for candidate in expected:
        target = _label(candidate)
        if not target:
            continue
        if actual == target:
            score = 1.0
            basis = "exact"
        else:
            score = 0.0
            basis = ""
        if score > best[0]:
            best = (score, basis)
    return best


def _name_score(runtime_name: str, product: ProductKnowledge) -> tuple[float, str]:
    return _text_similarity(runtime_name, (product.name, *product.aliases))


def _control_evidence(
    runtime_parameters: Sequence[RuntimeParameterObservation],
    control: AdapterControl,
) -> tuple[ParameterMatchEvidence | None, float]:
    best: tuple[ParameterMatchEvidence | None, float] = (None, 0.0)
    for parameter in runtime_parameters:
        score = 0.0
        basis = "name"
        if (
            control.parameter_index is not None
            and parameter.index == control.parameter_index
        ):
            score = 0.96
            basis = "index"
        name_score, name_basis = _text_similarity(parameter.name or "", control.names)
        display_score, display_basis = _text_similarity(
            parameter.display or "", control.display_names
        )
        if name_score and display_score:
            candidate = min(1.0, max(name_score, display_score) + 0.05)
            if candidate > score:
                score = candidate
                basis = "name_and_display"
        elif name_score > score:
            score = name_score
            basis = "name"
        elif display_score > score:
            score = display_score
            basis = "display"
        if score <= 0.0:
            continue
        evidence = ParameterMatchEvidence(
            control_id=control.control_id,
            parameter_index=parameter.index,
            basis=basis,  # type: ignore[arg-type]
            score=score,
            observed_name=parameter.name,
            observed_display=parameter.display,
        )
        if score > best[1] or (
            score == best[1]
            and best[0] is not None
            and parameter.index < best[0].parameter_index
        ):
            best = (evidence, score)
    return best


def _adapter_candidates(
    registry: AtlasRegistry, product: ProductKnowledge, runtime: RuntimePluginInstance
) -> tuple[tuple[ControlAdapter, tuple[ParameterMatchEvidence, ...], float], ...]:
    result: list[tuple[ControlAdapter, tuple[ParameterMatchEvidence, ...], float]] = []
    for adapter in registry.adapters_for_product(product.product_id):
        if runtime.format != "unknown" and adapter.formats:
            known_formats = set(adapter.formats)
            if "unknown" not in known_formats and runtime.format not in known_formats:
                continue
        evidence: list[ParameterMatchEvidence] = []
        scores: list[float] = []
        for control in adapter.controls:
            match, score = _control_evidence(runtime.parameters, control)
            if match is not None:
                evidence.append(match)
                scores.append(score)
        parameter_score = sum(scores) / len(adapter.controls) if adapter.controls else 0.0
        result.append((adapter, tuple(evidence), parameter_score))
    result.sort(
        key=lambda item: (
            -item[2],
            -len(item[1]),
            item[0].adapter_id,
        )
    )
    return tuple(result)


def _match_one(
    runtime: RuntimePluginInstance,
    product: ProductKnowledge,
    registry: AtlasRegistry,
) -> RuntimeMatch:
    name_score, name_basis = _name_score(runtime.name, product)
    candidates = _adapter_candidates(registry, product, runtime)
    adapter: ControlAdapter | None = None
    evidence: tuple[ParameterMatchEvidence, ...] = ()
    parameter_score = 0.0
    if candidates:
        adapter, evidence, parameter_score = candidates[0]
    overall_score = min(1.0, 0.6 * name_score + 0.4 * parameter_score)
    if evidence:
        control_status = "evidence"
    elif name_score > 0.0:
        control_status = "name_only"
    elif adapter is not None:
        control_status = "no_evidence"
    else:
        control_status = "not_evaluated"
    if evidence and overall_score >= 0.88:
        confidence = "high"
    elif overall_score >= 0.55:
        confidence = "medium"
    elif overall_score > 0.0:
        confidence = "low"
    else:
        confidence = "unknown"
    reasons: list[str] = []
    if name_basis == "exact":
        reasons.append("runtime name exactly matches a product name or alias")
    elif name_score > 0.0:
        reasons.append("runtime name partially matches a product name or alias")
    if adapter is not None and evidence:
        reasons.append(f"{len(evidence)} observed parameter(s) match adapter controls")
    elif adapter is not None:
        reasons.append("adapter candidate has no observed parameter evidence")
    if not evidence:
        reasons.append("product name alone is not control proof")
    warnings: list[str] = []
    if adapter is not None:
        warnings.append(
            "FL Studio does not expose the exact plug-in version; this is an adapter candidate, not version proof"
        )
    if runtime.availability.state != "loaded":
        warnings.append("ownership and installation remain unknown from runtime data")
    return RuntimeMatch(
        instance_id=runtime.instance_id,
        product_id=product.product_id,
        adapter_id=adapter.adapter_id if adapter is not None else None,
        availability=runtime.availability,
        name_score=name_score,
        parameter_score=parameter_score,
        overall_score=overall_score,
        confidence=confidence,  # type: ignore[arg-type]
        control_status=control_status,  # type: ignore[arg-type]
        parameter_evidence=evidence,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
    )


def match_runtime(
    runtime: RuntimePluginInstance | Mapping[str, Any],
    registry: AtlasRegistry,
    *,
    limit: int = MAX_MATCHES,
    include_weak: bool = False,
) -> tuple[RuntimeMatch, ...]:
    """Return deterministic product/adapter candidates for one runtime row.

    Product identity can be suggested by a name.  ``control_status`` remains
    ``name_only`` until at least one observed parameter matches an adapter
    control; no caller can mistake an exact product name for control proof.
    """

    if not isinstance(registry, AtlasRegistry):
        raise TypeError("registry must be an AtlasRegistry")
    if type(limit) is not int or not (1 <= limit <= MAX_MATCHES):
        raise ValueError("runtime match limit is outside bounds")
    observation = _coerce_runtime(runtime)
    candidates: list[RuntimeMatch] = []
    for product in registry.products:
        name_score, _ = _name_score(observation.name, product)
        if name_score != 1.0:
            continue
        match = _match_one(observation, product, registry)
        if match.overall_score > 0.0 or match.parameter_evidence:
            if include_weak or match.overall_score >= 0.2 or match.parameter_evidence:
                candidates.append(match)
    candidates.sort(
        key=lambda item: (
            -item.overall_score,
            -item.parameter_score,
            item.product_id or "",
            item.adapter_id or "",
        )
    )
    return tuple(candidates[:limit])


def match_runtime_plugin(
    runtime: RuntimePluginInstance | Mapping[str, Any], registry: AtlasRegistry
) -> RuntimeMatch | None:
    """Return the strongest candidate, preserving ``None`` for no match."""

    matches = match_runtime(runtime, registry, limit=1)
    return matches[0] if matches else None


def match_plugin(
    runtime: RuntimePluginInstance | Mapping[str, Any], registry: AtlasRegistry
) -> RuntimeMatch | None:
    return match_runtime_plugin(runtime, registry)


match_loaded_plugin = match_runtime_plugin


__all__ = [
    "MAX_MATCHES",
    "MAX_RUNTIME_PARAMETERS",
    "match_loaded_plugin",
    "match_plugin",
    "match_runtime",
    "match_runtime_plugin",
]
