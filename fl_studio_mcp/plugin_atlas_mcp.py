"""MCP-facing integration for the generic Plugin Atlas surface.

The Atlas itself is deliberately independent from MCP and from a live FL
Studio session.  This module is the small adapter between those two worlds:
the first three operations read the bundled, local registry, while
``inspect_loaded_plugins`` obtains one target-aware inventory from Track B and
then runs the Atlas matcher against those observations.

Nothing in this module inserts, changes, or otherwise controls a plug-in.  In
particular, a product-name match is kept as ``name_only`` by the existing
matcher and compatibility join; it is never promoted to control proof here.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import Field, TypeAdapter

from .performance import TrackBInspector
from .plugin_atlas import (
    AtlasId,
    AtlasModel,
    AtlasRegistry,
    CompatibilityEvidence,
    CompatibilityJoin,
    ControlAdapter,
    EvidenceReference,
    ProductKind,
    ProductKnowledge,
    ProductOrigin,
    ProductRecommendation,
    RecommendationRequest,
    RuntimeMatch,
    RuntimePluginInstance,
    ShortText,
    VendorKnowledge,
    WriteValidationEvidence,
    join_compatibility,
    load_bundled_registry,
    match_runtime,
    recommend_products,
    recommend_stock_alternatives,
)
from .track_b_contracts import (
    PluginTarget,
    TargetedLoadedPluginInventory,
    TargetedPluginSummary,
)


MAX_ATLAS_QUERY_LENGTH = 512
MAX_ATLAS_RESULTS = 128
MAX_ATLAS_RUNTIME_MATCHES = 128


class AtlasSearchRequest(AtlasModel):
    """Bounded filters for a local, deterministic product search."""

    query: str = Field(default="", max_length=MAX_ATLAS_QUERY_LENGTH, description="Catalog search text; an empty string lists products subject to the filters and limit.", examples=["reverb"])
    vendor_id: AtlasId | None = Field(default=None, description="Exact catalog vendor ID; omit to search all vendors.")
    origin: ProductOrigin | None = Field(default=None, description="Restrict to stock, third-party, or unknown product origin; omit for all origins.")
    # Atlas product kinds are intentionally open to the catalog's
    # ``plugin_kinds`` values (for example ``synthesizer``) in addition to the
    # coarse ProductKind enum.  It is still a strict bounded string.
    kind: ShortText | None = Field(default=None, description="Catalog product kind, including specific kinds such as synthesizer; omit for all kinds.")
    technique_id: AtlasId | None = Field(default=None, description="Exact catalog technique ID used to narrow products; omit when unknown.")
    stock_only: bool = Field(default=False, description="Return only FL Studio stock products when true; catalog inclusion does not establish licensing.")
    limit: int = Field(default=16, ge=1, le=MAX_ATLAS_RESULTS, description="Maximum number of ranked search hits to return, from 1 to 128.")


class AtlasSearchHit(AtlasModel):
    """One deterministic Atlas search result and its match explanation."""

    product: ProductKnowledge
    score: float = Field(ge=0.0, le=1.0, description="Deterministic text/filter relevance from 0 to 1, not a probability of installed availability.")
    matched_fields: tuple[ShortText, ...] = Field(default=(), max_length=32, description="Catalog fields explaining why this product matched the search.")


class AtlasSearchResponse(AtlasModel):
    """Typed response for :func:`plugins_atlas_search`."""

    schema_version: Literal["1.0"] = "1.0"
    query: str = Field(max_length=MAX_ATLAS_QUERY_LENGTH)
    registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$", description="SHA-256 identity of the bundled catalog used for this response, not of the live project.")
    results: tuple[AtlasSearchHit, ...] = Field(
        default=(), max_length=MAX_ATLAS_RESULTS
    )


class AtlasGetProductRequest(AtlasModel):
    """Exact product identifier lookup request."""

    product_id: AtlasId = Field(description="Exact product_id returned by Atlas search or recommendations; not a plugin display name.")


AtlasEvidence = EvidenceReference | CompatibilityEvidence | WriteValidationEvidence


class AtlasProductResponse(AtlasModel):
    """Static product knowledge and related descriptive Atlas records."""

    schema_version: Literal["1.0"] = "1.0"
    registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$", description="SHA-256 identity of the bundled catalog used for this response.")
    product: ProductKnowledge
    vendor: VendorKnowledge | None = None
    adapters: tuple[ControlAdapter, ...] = Field(default=(), max_length=4096, description="Descriptive catalog control mappings; these do not prove that a currently loaded instance exposes writable controls.")
    evidence: tuple[AtlasEvidence, ...] = Field(default=(), max_length=8192, description="Catalog source references and compatibility or write-validation records; inspect each record's scope and provenance before generalizing it to a live target.")
    stock_alternatives: tuple[ProductKnowledge, ...] = Field(
        default=(), max_length=256, description="Catalog FL Studio stock alternatives; this is not a claim of ownership or installation."
    )


class AtlasRecommendRequest(AtlasModel):
    """Bounded static recommendation criteria.

    ``product_id`` selects the explicit stock-alternative mode.  It remains a
    parameter of this generic recommendation tool, rather than creating one
    product-specific MCP tool for each Atlas record.
    """

    query: str = Field(default="", max_length=MAX_ATLAS_QUERY_LENGTH, description="Production goal in plain language; used in normal recommendation mode.", examples=["reduce harshness in a vocal"])
    problems: tuple[ShortText, ...] = Field(default=(), max_length=256, description="Problems to address, such as harshness; omit when the query is sufficient.")
    techniques: tuple[AtlasId, ...] = Field(default=(), max_length=256, description="Catalog technique IDs to favor in normal recommendation mode.")
    sources: tuple[ShortText, ...] = Field(default=(), max_length=256, description="Audio-source descriptions to match, such as vocal or drums; not file paths.")
    kind: ProductKind | None = Field(default=None, description="Optional coarse product-kind filter for normal recommendation mode.")
    prefer_stock: bool = Field(default=False, description="Favor FL Studio stock products in normal recommendation mode without asserting licensing.")
    limit: int = Field(default=16, ge=1, le=MAX_ATLAS_RESULTS, description="Maximum number of recommendations to return, from 1 to 128.")
    product_id: AtlasId | None = Field(default=None, description="Known catalog product ID for stock-alternative mode; requires stock_alternatives=True.")
    stock_alternatives: bool = Field(default=False, description="Select stock alternatives for product_id. Requires product_id and uses that product and limit instead of normal recommendation criteria.")


class AtlasRecommendationResponse(AtlasModel):
    """Typed, availability-honest recommendation response."""

    schema_version: Literal["1.0"] = "1.0"
    registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$", description="SHA-256 identity of the bundled catalog used for the static recommendations.")
    recommendations: tuple[ProductRecommendation, ...] = Field(
        default=(), max_length=MAX_ATLAS_RESULTS
    )


class AtlasInspectLoadedRequest(AtlasModel):
    """Bounded live-inventory matching options."""

    only_used: bool = Field(default=False, description="Apply the conservative used-track heuristic to mixer tracks; this does not restrict Channel Rack generator inventory.")
    match_limit: int = Field(default=16, ge=1, le=MAX_ATLAS_RUNTIME_MATCHES, description="Maximum catalog candidates per loaded plugin, from 1 to 128; not a limit on live targets.")
    include_weak: bool = Field(default=False, description="Include weak name matches for inspection; false filters them out. Neither setting proves identity or writable controls.")


class AtlasLoadedPluginRecord(AtlasModel):
    """One loaded target and its non-authoritative Atlas compatibility view."""

    # Keep ``target`` at this top level as well as inside ``plugin``.  The
    # Track B target is the identity boundary: a mixer slot and a global
    # Channel Rack generator with the same display name are different rows.
    target: PluginTarget = Field(description="Observed mixer effect or global Channel Rack generator address; do not identify a live target by its display name alone.")
    plugin: TargetedPluginSummary = Field(description="Live bridge summary and target fingerprint for this loaded instance.")
    runtime: RuntimePluginInstance = Field(description="Observation-scoped matcher input built from the live summary; loaded availability is known, ownership and installation elsewhere are not.")
    matches: tuple[RuntimeMatch, ...] = Field(
        default=(), max_length=MAX_ATLAS_RUNTIME_MATCHES, description="Ranked catalog candidates. This inventory supplies names, not parameter-control proof."
    )
    best_match: RuntimeMatch | None = Field(default=None, description="Highest-ranked candidate, or null when nothing matched; a best match is still not verified product identity.")
    compatibility: CompatibilityJoin | None = Field(default=None, description="Catalog compatibility evidence joined to the best candidate, or null; inspect evidence level and scope before relying on controls.")


class AtlasInspectLoadedResponse(AtlasModel):
    """Typed result of matching the current Track B loaded inventory."""

    schema_version: Literal["1.0"] = "1.0"
    observed_at: datetime = Field(description="Timestamp of the live inventory observation; a later project change can invalidate its targets.")
    registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$", description="SHA-256 identity of the bundled catalog used to match the live inventory.")
    plugins: tuple[AtlasLoadedPluginRecord, ...] = Field(
        default=(), max_length=4096, description="One record per observed loaded target, including targets without a catalog match."
    )
    warnings: tuple[ShortText, ...] = Field(default=(), max_length=256, description="Inventory and evidence limitations to retain when reporting matches; do not interpret warnings as verified control support.")


_PLUGIN_TARGET_ADAPTER = TypeAdapter(PluginTarget)


def _target_from_summary(summary: TargetedPluginSummary) -> PluginTarget:
    """Copy Track B's discriminated target into the Atlas response model."""

    # Validate through JSON so the strict AtlasModel boundary never accepts a
    # loose mapping or accidentally shares a mutable object from a bridge
    # response.
    return _PLUGIN_TARGET_ADAPTER.validate_json(
        json.dumps(summary.target.model_dump(mode="json"), allow_nan=False),
        strict=True,
    )


def _target_instance_id(target: PluginTarget) -> str:
    """Derive an observation-scoped identity from a target, not its name."""

    if target.kind == "mixer_effect":
        return f"mixer_effect:{target.track_index}:{target.slot_index}"
    return f"channel_generator:{target.channel_index}"


def _runtime_from_summary(
    summary: TargetedPluginSummary, target: PluginTarget
) -> RuntimePluginInstance:
    """Convert Track B's loaded summary into the Atlas matcher input."""

    return RuntimePluginInstance.model_validate_json(
        json.dumps(
            {
                "instance_id": _target_instance_id(target),
                "name": summary.name,
                "user_name": summary.user_name,
                "format": "unknown",
                "availability": {
                    "state": "loaded",
                    "source": "runtime_inventory",
                },
                # Track B's inventory intentionally reports only a parameter
                # count.  It does not fabricate names/displays, so this
                # runtime observation can result in name_only but never in
                # control evidence.
                "parameters": [],
            },
            allow_nan=False,
        ),
        strict=True,
    )


def search_atlas(
    request: AtlasSearchRequest,
    *,
    registry: AtlasRegistry | None = None,
) -> AtlasSearchResponse:
    """Search one local registry without touching FL Studio."""

    selected = registry or load_bundled_registry()
    hits = selected.search_hits(
        request.query,
        vendor_id=request.vendor_id,
        origin=request.origin,
        kind=request.kind,
        technique_id=request.technique_id,
        stock_only=request.stock_only,
        limit=request.limit,
    )
    return AtlasSearchResponse(
        query=request.query,
        registry_digest=selected.digest(),
        results=tuple(
            AtlasSearchHit(
                product=hit.product,
                score=hit.score,
                matched_fields=hit.matched_fields,
            )
            for hit in hits
        ),
    )


def get_atlas_product(
    request: AtlasGetProductRequest,
    *,
    registry: AtlasRegistry | None = None,
) -> AtlasProductResponse:
    """Get one product and its related static knowledge by exact ID."""

    selected = registry or load_bundled_registry()
    product = selected.require_product(request.product_id)
    vendor = selected.vendor(product.vendor_id)
    return AtlasProductResponse(
        registry_digest=selected.digest(),
        product=product,
        vendor=vendor,
        adapters=selected.adapters_for_product(product.product_id),
        evidence=selected.evidence_for_product(product.product_id),
        stock_alternatives=selected.stock_alternatives(product.product_id),
    )


def recommend_atlas(
    request: AtlasRecommendRequest,
    *,
    registry: AtlasRegistry | None = None,
) -> AtlasRecommendationResponse:
    """Rank static Atlas products or stock alternatives deterministically."""

    selected = registry or load_bundled_registry()
    if request.stock_alternatives:
        if request.product_id is None:
            raise ValueError(
                "stock_alternatives requires a product_id in the same request"
            )
        recommendations = recommend_stock_alternatives(
            selected, request.product_id, limit=request.limit
        )
    elif request.product_id is not None:
        raise ValueError(
            "product_id requires stock_alternatives=true for recommendation mode"
        )
    else:
        # Reuse the existing strict Atlas recommendation contract and scorer;
        # this adapter adds no second ranking policy.
        criteria = RecommendationRequest.model_validate_json(
            json.dumps(
                {
                    "query": request.query,
                    "problems": list(request.problems),
                    "techniques": list(request.techniques),
                    "sources": list(request.sources),
                    "kind": request.kind,
                    "prefer_stock": request.prefer_stock,
                    "limit": request.limit,
                },
                allow_nan=False,
            ),
            strict=True,
        )
        recommendations = recommend_products(selected, criteria)
    return AtlasRecommendationResponse(
        registry_digest=selected.digest(),
        recommendations=recommendations,
    )


def inspect_loaded_atlas(
    request: AtlasInspectLoadedRequest,
    *,
    registry: AtlasRegistry | None = None,
    inventory: TargetedLoadedPluginInventory | None = None,
    inspector: TrackBInspector | None = None,
) -> AtlasInspectLoadedResponse:
    """Match the current target-aware Track B inventory to static Atlas data.

    ``inventory`` and ``inspector`` are injectable for deterministic tests;
    production callers leave both unset and this function asks the existing
    TrackBInspector for the live inventory exactly once.
    """

    selected = registry or load_bundled_registry()
    observed = inventory
    if observed is None:
        observed = (inspector or TrackBInspector()).scan_loaded_plugins(
            only_used=request.only_used
        )
    records: list[AtlasLoadedPluginRecord] = []
    for summary in observed.plugins:
        target = _target_from_summary(summary)
        runtime = _runtime_from_summary(summary, target)
        matches = match_runtime(
            runtime,
            selected,
            limit=request.match_limit,
            include_weak=request.include_weak,
        )
        best = matches[0] if matches else None
        compatibility = (
            join_compatibility(best, selected) if best is not None else None
        )
        records.append(
            AtlasLoadedPluginRecord(
                target=target,
                plugin=summary,
                runtime=runtime,
                matches=matches,
                best_match=best,
                compatibility=compatibility,
            )
        )
    warnings = tuple(dict.fromkeys((*observed.warnings, "Atlas runtime matches are observation-scoped; ownership and installation remain unknown.")))
    return AtlasInspectLoadedResponse(
        observed_at=observed.observed_at,
        registry_digest=selected.digest(),
        plugins=tuple(records),
        warnings=warnings,
    )


__all__ = [
    "AtlasGetProductRequest",
    "AtlasInspectLoadedRequest",
    "AtlasInspectLoadedResponse",
    "AtlasLoadedPluginRecord",
    "AtlasProductResponse",
    "AtlasRecommendationResponse",
    "AtlasRecommendRequest",
    "AtlasSearchHit",
    "AtlasSearchRequest",
    "AtlasSearchResponse",
    "get_atlas_product",
    "inspect_loaded_atlas",
    "recommend_atlas",
    "search_atlas",
]
