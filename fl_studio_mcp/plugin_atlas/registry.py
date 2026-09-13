"""Immutable indexes and deterministic lookup for Plugin Atlas data."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from .loader import load_atlas
from .models import (
    ATLAS_DIGEST_ALGORITHM,
    AtlasBundle,
    AtlasManifest,
    CatalogSnapshot,
    CompatibilityEvidence,
    ControlAdapter,
    EvidenceReference,
    ProductKnowledge,
    TechniqueKnowledge,
    VendorKnowledge,
    WriteValidationEvidence,
)


MAX_SEARCH_RESULTS = 128
_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


def normalize_search_text(value: str) -> str:
    """Normalize user/catalog text for deterministic case-insensitive lookup."""

    return " ".join(_TOKEN_RE.findall(value.casefold()))


def _tokens(value: str) -> frozenset[str]:
    return frozenset(_TOKEN_RE.findall(value.casefold()))


def _model_json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


@dataclass(frozen=True, slots=True)
class ProductSearchHit:
    """A product plus its deterministic text-match score."""

    product: ProductKnowledge
    score: float
    matched_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AtlasRegistry:
    """Read-only indexes over one validated :class:`AtlasBundle`.

    All collections are tuples owned by the validated bundle.  No registry
    method mutates the bundle or caches mutable dictionaries, so sharing a
    registry between MCP calls is safe.
    """

    bundle: AtlasBundle

    @classmethod
    def from_bundle(cls, bundle: AtlasBundle) -> AtlasRegistry:
        return cls(bundle=bundle)

    @classmethod
    def from_parts(
        cls,
        manifest: AtlasManifest,
        *,
        products: tuple[ProductKnowledge, ...] = (),
        vendors: tuple[VendorKnowledge, ...] = (),
        techniques: tuple[TechniqueKnowledge, ...] = (),
        adapters: tuple[ControlAdapter, ...] = (),
        evidence: tuple[
            EvidenceReference | CompatibilityEvidence | WriteValidationEvidence,
            ...,
        ] = (),
        catalog_snapshots: tuple[CatalogSnapshot, ...] = (),
    ) -> AtlasRegistry:
        """Build a registry from already-validated immutable model parts."""

        return cls.from_bundle(
            AtlasBundle(
                manifest=manifest,
                products=products,
                vendors=vendors,
                techniques=techniques,
                adapters=adapters,
                evidence=evidence,
                catalog_snapshots=catalog_snapshots,
            )
        )

    @classmethod
    def load(
        cls, root: Any = None, *, limits: Any = None
    ) -> AtlasRegistry:
        kwargs = {} if limits is None else {"limits": limits}
        return cls.from_bundle(load_atlas(root, **kwargs))

    @property
    def manifest(self) -> AtlasManifest:
        return self.bundle.manifest

    @property
    def products(self) -> tuple[ProductKnowledge, ...]:
        return self.bundle.products

    @property
    def vendors(self) -> tuple[VendorKnowledge, ...]:
        return self.bundle.vendors

    @property
    def techniques(self) -> tuple[TechniqueKnowledge, ...]:
        return self.bundle.techniques

    @property
    def adapters(self) -> tuple[ControlAdapter, ...]:
        return self.bundle.adapters

    @property
    def catalog_snapshots(self) -> tuple[CatalogSnapshot, ...]:
        return self.bundle.catalog_snapshots

    @property
    def evidence(
        self,
    ) -> tuple[EvidenceReference | CompatibilityEvidence | WriteValidationEvidence, ...]:
        return self.bundle.evidence

    def product(self, product_id: str) -> ProductKnowledge | None:
        """Return an exact product-ID match, or ``None``."""

        return next(
            (item for item in self.products if item.product_id == product_id), None
        )

    def get_product(self, product_id: str) -> ProductKnowledge | None:
        return self.product(product_id)

    def require_product(self, product_id: str) -> ProductKnowledge:
        result = self.product(product_id)
        if result is None:
            raise KeyError(f"unknown Atlas product: {product_id}")
        return result

    def vendor(self, vendor_id: str) -> VendorKnowledge | None:
        return next(
            (item for item in self.vendors if item.vendor_id == vendor_id), None
        )

    def technique(self, technique_id: str) -> TechniqueKnowledge | None:
        return next(
            (item for item in self.techniques if item.technique_id == technique_id),
            None,
        )

    def adapter(self, adapter_id: str) -> ControlAdapter | None:
        return next(
            (item for item in self.adapters if item.adapter_id == adapter_id), None
        )

    def adapters_for_product(self, product_id: str) -> tuple[ControlAdapter, ...]:
        return tuple(item for item in self.adapters if item.product_id == product_id)

    def evidence_for_product(
        self, product_id: str
    ) -> tuple[CompatibilityEvidence | WriteValidationEvidence, ...]:
        return tuple(
            item
            for item in self.evidence
            if not isinstance(item, EvidenceReference)
            and item.product_id == product_id
        )

    def stock_alternatives(self, product_id: str) -> tuple[ProductKnowledge, ...]:
        product = self.product(product_id)
        if product is None:
            return ()
        alternatives = {
            item.product_id: item
            for item in self.products
            if item.product_id in product.stock_alternative_ids
        }
        return tuple(alternatives[key] for key in sorted(alternatives))

    def search_hits(
        self,
        query: str = "",
        *,
        vendor_id: str | None = None,
        origin: str | None = None,
        kind: str | None = None,
        technique_id: str | None = None,
        stock_only: bool = False,
        limit: int = MAX_SEARCH_RESULTS,
    ) -> tuple[ProductSearchHit, ...]:
        if type(query) is not str:
            raise TypeError("Atlas search query must be text")
        if type(limit) is not int or not (1 <= limit <= MAX_SEARCH_RESULTS):
            raise ValueError("Atlas search limit is outside bounds")
        query_text = normalize_search_text(query)
        query_tokens = _tokens(query)
        hits: list[ProductSearchHit] = []
        for product in self.products:
            if vendor_id is not None and product.vendor_id != vendor_id:
                continue
            if origin is not None and product.origin != origin:
                continue
            if kind is not None and product.kind != kind and kind not in product.plugin_kinds:
                continue
            if technique_id is not None and technique_id not in product.technique_ids:
                continue
            if stock_only and not product.stock:
                continue
            score, fields = _product_text_score(product, query_text, query_tokens)
            if query_text and score <= 0.0:
                continue
            hits.append(ProductSearchHit(product, score, tuple(fields)))
        hits.sort(key=lambda item: (-item.score, item.product.product_id))
        return tuple(hits[:limit])

    def search(self, query: str = "", **kwargs: Any) -> tuple[ProductKnowledge, ...]:
        """Return products ranked by deterministic text and filter matching."""

        return tuple(hit.product for hit in self.search_hits(query, **kwargs))

    def find_product(self, value: str) -> ProductKnowledge | None:
        """Resolve an exact ID/name/alias before falling back to ranked search."""

        needle = normalize_search_text(value)
        if not needle:
            return None
        identifier_matches = [
            product
            for product in self.products
            if normalize_search_text(product.product_id) == needle
        ]
        if len(identifier_matches) == 1:
            return identifier_matches[0]
        if len(identifier_matches) > 1:
            return None
        exact: list[ProductKnowledge] = []
        for product in self.products:
            labels = (product.name, *product.aliases)
            if any(normalize_search_text(label) == needle for label in labels):
                exact.append(product)
        if len(exact) != 1:
            return None
        return exact[0]

    def digest(self) -> str:
        """Return a stable SHA-256 digest of the complete logical bundle."""

        payload = {
            "algorithm": ATLAS_DIGEST_ALGORITHM,
            "manifest": _model_json(self.manifest),
            "vendors": [_model_json(item) for item in sorted(self.vendors, key=lambda item: item.vendor_id)],
            "techniques": [
                _model_json(item)
                for item in sorted(self.techniques, key=lambda item: item.technique_id)
            ],
            "products": [
                _model_json(item)
                for item in sorted(self.products, key=lambda item: item.product_id)
            ],
            "adapters": [
                _model_json(item)
                for item in sorted(self.adapters, key=lambda item: item.adapter_id)
            ],
            "evidence": [
                _model_json(item)
                for item in sorted(self.evidence, key=lambda item: item.evidence_id)
            ],
            "catalog_snapshots": [
                _model_json(item)
                for item in sorted(
                    self.catalog_snapshots, key=lambda item: item.snapshot_id
                )
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def content_digest(self) -> str:
        return self.digest()


def _product_text_score(
    product: ProductKnowledge, query: str, query_tokens: frozenset[str]
) -> tuple[float, list[str]]:
    if not query:
        return 1.0, []
    fields: tuple[tuple[str, str, float], ...] = (
        ("id", product.product_id, 1.0),
        ("name", product.name, 1.0),
        ("alias", " ".join(product.aliases), 0.9),
        ("category", " ".join(product.categories), 0.65),
        ("problem", " ".join(product.problems), 0.75),
        ("use_case", " ".join(product.use_cases), 0.75),
        ("source", " ".join(product.common_sources), 0.65),
        ("technique", " ".join(product.technique_ids), 0.7),
        ("description", product.description, 0.45),
    )
    score = 0.0
    matched: list[str] = []
    for field_name, value, weight in fields:
        text = normalize_search_text(value)
        if text == query:
            score += weight
            matched.append(field_name)
            continue
        value_tokens = _tokens(value)
        if query_tokens and query_tokens.issubset(value_tokens):
            score += weight * 0.8
            matched.append(field_name)
        elif query_tokens and value_tokens.intersection(query_tokens):
            overlap = len(value_tokens.intersection(query_tokens)) / len(query_tokens)
            score += weight * 0.35 * overlap
            matched.append(field_name)
    return min(1.0, score), matched


PluginAtlasRegistry = AtlasRegistry


def load_registry(root: Any = None, *, limits: Any = None) -> AtlasRegistry:
    """Load a local Atlas and return its immutable registry."""

    return AtlasRegistry.load(root, limits=limits)


@lru_cache(maxsize=1)
def load_bundled_registry() -> AtlasRegistry:
    """Load only the installed ``plugin_atlas_data`` package."""

    return AtlasRegistry.load()


__all__ = [
    "AtlasRegistry",
    "load_bundled_registry",
    "load_registry",
    "MAX_SEARCH_RESULTS",
    "PluginAtlasRegistry",
    "ProductSearchHit",
    "normalize_search_text",
]
