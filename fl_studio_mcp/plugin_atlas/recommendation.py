"""Deterministic product and stock-alternative recommendations."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from .models import (
    AvailabilityObservation,
    ProductKnowledge,
    ProductRecommendation,
    RecommendationRequest,
    RuntimeMatch,
)
from .registry import AtlasRegistry, normalize_search_text


MAX_RECOMMENDATIONS = 128


def _terms(values: Iterable[str]) -> frozenset[str]:
    result: set[str] = set()
    for value in values:
        result.update(normalize_search_text(value).split())
    return frozenset(result)


def _overlap(requested: frozenset[str], values: Iterable[str]) -> float:
    if not requested:
        return 0.0
    observed = _terms(values)
    return len(requested.intersection(observed)) / len(requested)


def _request(value: RecommendationRequest | Mapping[str, Any] | str | None) -> RecommendationRequest:
    if value is None:
        return RecommendationRequest()
    if isinstance(value, RecommendationRequest):
        return value
    if isinstance(value, str):
        return RecommendationRequest(query=value)
    if not isinstance(value, Mapping):
        raise TypeError("recommendation request must be text, mapping, or RecommendationRequest")
    source = dict(value)
    if "common_sources" in source and "sources" not in source:
        source["sources"] = source["common_sources"]
    if "technique_ids" in source and "techniques" not in source:
        source["techniques"] = source["technique_ids"]
    if "problems_solved" in source and "problems" not in source:
        source["problems"] = source["problems_solved"]
    try:
        # Strict Atlas models accept JSON arrays for immutable tuple fields
        # through the JSON validation path.
        import json

        return RecommendationRequest.model_validate_json(
            json.dumps(source, ensure_ascii=True, allow_nan=False)
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise ValueError(f"invalid recommendation request: {exc}") from exc


def _product_score(
    product: ProductKnowledge,
    request: RecommendationRequest,
    *,
    loaded_matches: Sequence[RuntimeMatch] = (),
) -> tuple[float, tuple[str, ...]]:
    query = normalize_search_text(request.query)
    query_terms = _terms((request.query,))
    score = 0.0
    matched: list[str] = []
    if query:
        name = normalize_search_text(product.name)
        aliases = tuple(normalize_search_text(value) for value in product.aliases)
        if query == name:
            score += 0.52
            matched.append("name")
        elif query in aliases:
            score += 0.48
            matched.append("alias")
        else:
            overlap = _overlap(query_terms, (product.name, *product.aliases))
            if overlap:
                score += 0.38 * overlap
                matched.append("name_tokens")
            field_values = (
                product.description,
                *product.categories,
                *product.problems,
                *product.use_cases,
                *product.poor_fit_when,
                *product.common_sources,
                *product.technique_ids,
            )
            field_overlap = _overlap(query_terms, field_values)
            if field_overlap:
                score += 0.28 * field_overlap
                matched.append("knowledge")
    requested_problems = _terms(request.problems)
    problem_overlap = _overlap(requested_problems, product.problems)
    if problem_overlap:
        score += 0.24 * problem_overlap
        matched.append("problem")
    requested_techniques = set(request.techniques)
    if requested_techniques:
        overlap = len(requested_techniques.intersection(product.technique_ids)) / len(
            requested_techniques
        )
        if overlap:
            score += 0.28 * overlap
            matched.append("technique")
    source_overlap = _overlap(_terms(request.sources), product.common_sources)
    if source_overlap:
        score += 0.15 * source_overlap
        matched.append("source")
    if request.kind is not None and (
        product.kind == request.kind or request.kind in product.plugin_kinds
    ):
        score += 0.10
        matched.append("kind")
    if request.prefer_stock and product.stock:
        score += 0.05
        matched.append("stock")
    runtime = next(
        (
            match
            for match in loaded_matches
            if match.product_id == product.product_id
        ),
        None,
    )
    if runtime is not None:
        score += 0.08 * runtime.overall_score
        matched.append("loaded_match")
    if not (query or requested_problems or requested_techniques or request.sources):
        score = 0.5 + (0.05 if request.prefer_stock and product.stock else 0.0)
    return min(1.0, score), tuple(dict.fromkeys(matched))


def score_product(
    product: ProductKnowledge,
    request: RecommendationRequest | Mapping[str, Any] | str | None = None,
    *,
    loaded_matches: Sequence[RuntimeMatch] = (),
) -> float:
    """Return the stable 0..1 recommendation score for one product."""

    value = _request(request)
    return _product_score(product, value, loaded_matches=loaded_matches)[0]


def _availability_for(
    product_id: str, loaded_matches: Sequence[RuntimeMatch]
) -> tuple[AvailabilityObservation, str | None]:
    for match in loaded_matches:
        if match.product_id == product_id:
            return match.availability, match.instance_id
    return AvailabilityObservation(state="availability_unknown"), None


def recommend_products(
    registry: AtlasRegistry,
    request: RecommendationRequest | Mapping[str, Any] | str | None = None,
    *,
    query: str | None = None,
    problems: Iterable[str] = (),
    techniques: Iterable[str] = (),
    sources: Iterable[str] = (),
    prefer_stock: bool | None = None,
    loaded_matches: Sequence[RuntimeMatch] = (),
    limit: int | None = None,
) -> tuple[ProductRecommendation, ...]:
    """Rank products from static knowledge without asserting availability."""

    if not isinstance(registry, AtlasRegistry):
        raise TypeError("registry must be an AtlasRegistry")
    base = _request(request)
    updates: dict[str, Any] = {}
    if query is not None:
        updates["query"] = query
    if problems:
        updates["problems"] = tuple(problems)
    if techniques:
        updates["techniques"] = tuple(techniques)
    if sources:
        updates["sources"] = tuple(sources)
    if prefer_stock is not None:
        updates["prefer_stock"] = prefer_stock
    if limit is not None:
        updates["limit"] = limit
    if updates:
        import json

        data = base.model_dump(mode="json")
        data.update(updates)
        base = RecommendationRequest.model_validate_json(
            json.dumps(data, ensure_ascii=True, allow_nan=False)
        )
    if type(base.limit) is not int or not (1 <= base.limit <= MAX_RECOMMENDATIONS):
        raise ValueError("recommendation limit is outside bounds")
    rows: list[ProductRecommendation] = []
    for product in registry.products:
        score, fields = _product_score(product, base, loaded_matches=loaded_matches)
        exact_query = normalize_search_text(base.query) in {
            normalize_search_text(product.name),
            *(normalize_search_text(alias) for alias in product.aliases),
        }
        if product.lifecycle in {"legacy", "deprecated", "discontinued"} and not exact_query:
            continue
        has_requested_match = bool(
            base.query
            or base.problems
            or base.techniques
            or base.sources
            or base.kind is not None
        )
        if has_requested_match and score <= 0.0:
            continue
        availability, instance_id = _availability_for(product.product_id, loaded_matches)
        reasons = tuple(f"matched {field.replace('_', ' ')}" for field in fields)
        if not reasons:
            reasons = ("catalogue baseline",)
        rows.append(
            ProductRecommendation(
                product_id=product.product_id,
                score=score,
                reasons=reasons,
                availability=availability,
                loaded_instance_id=instance_id,
                matched_fields=fields,
            )
        )
    rows.sort(key=lambda item: (-item.score, item.product_id))
    return tuple(rows[:base.limit])


def recommend(
    registry: AtlasRegistry,
    request: RecommendationRequest | Mapping[str, Any] | str | None = None,
    **kwargs: Any,
) -> tuple[ProductRecommendation, ...]:
    return recommend_products(registry, request, **kwargs)


def recommend_stock_alternatives(
    registry: AtlasRegistry,
    product: ProductKnowledge | str,
    *,
    limit: int = 16,
) -> tuple[ProductRecommendation, ...]:
    """Score explicit and inferred stock alternatives for a product."""

    if not isinstance(registry, AtlasRegistry):
        raise TypeError("registry must be an AtlasRegistry")
    if type(limit) is not int or not (1 <= limit <= MAX_RECOMMENDATIONS):
        raise ValueError("stock-alternative limit is outside bounds")
    source = product if isinstance(product, ProductKnowledge) else registry.product(product)
    if source is None:
        raise KeyError(f"unknown Atlas product: {product}")
    explicit = set(source.stock_alternative_ids)
    candidates = [
        item
        for item in registry.products
        if item.stock
        and item.product_id != source.product_id
        and item.lifecycle in {"current", "auxiliary"}
    ]
    scored: list[ProductRecommendation] = []
    source_terms = _terms(
        (
            *source.problems,
            *source.use_cases,
            *source.technique_ids,
            *source.categories,
        )
    )
    for candidate in candidates:
        candidate_terms = _terms(
            (
                *candidate.problems,
                *candidate.use_cases,
                *candidate.technique_ids,
                *candidate.categories,
            )
        )
        overlap = len(source_terms.intersection(candidate_terms)) / max(1, len(source_terms))
        score = min(1.0, 0.7 * overlap + (0.3 if candidate.product_id in explicit else 0.0))
        reasons = ("explicit stock alternative",) if candidate.product_id in explicit else (
            "shared production problems or techniques",
        )
        scored.append(
            ProductRecommendation(
                product_id=candidate.product_id,
                score=score,
                reasons=reasons,
                stock_alternative=True,
                source_product_id=source.product_id,
            )
        )
    scored.sort(key=lambda item: (-item.score, item.product_id))
    return tuple(scored[:limit])


stock_alternatives = recommend_stock_alternatives


__all__ = [
    "MAX_RECOMMENDATIONS",
    "recommend",
    "recommend_products",
    "recommend_stock_alternatives",
    "score_product",
    "stock_alternatives",
]
