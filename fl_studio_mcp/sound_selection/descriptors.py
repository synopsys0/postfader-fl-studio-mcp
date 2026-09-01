"""Versioned, local preset descriptor knowledge for Sound Selection.

Descriptor data is intentionally small and reviewable.  The loader reads one
explicit local resource only; it does not search preset folders, contact a
network service, or make an audible claim.  Name-token classification is
marked with deliberately low confidence so user and reviewed metadata can
override it honestly.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import Field, model_validator

from ..plugin_atlas.models import ProductKnowledge
from .models import (
    MAX_DESCRIPTORS,
    DescriptorEvidence,
    DescriptorIdentifier,
    PresetDescriptorProvenance,
    SoundSelectionModel,
    canonical_digest,
)


DESCRIPTOR_SCHEMA_VERSION = "1.0"
DESCRIPTOR_DATA_FILE = "data/descriptors-v1.json"
MAX_DESCRIPTOR_RULES = 256
MAX_DESCRIPTOR_FILE_BYTES = 2 * 1024 * 1024
_TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)

# This is deliberately a closed vocabulary.  It handles common wording
# variants without fuzzy matching unrelated terms.  Values not in this map
# are normalized for case/separator consistency but retain their meaning.
DESCRIPTOR_SYNONYMS: dict[str, str] = {
    "plucky": "plucked",
    "pluck": "plucked",
    "plucked": "plucked",
    "punchy": "punchy",
    "transient": "punchy",
    "hard-attack": "punchy",
    "hard attack": "punchy",
    "smooth": "smooth",
    "soft": "smooth",
    "rounded": "smooth",
    "wide": "wide",
    "stereo": "wide",
    "spacious": "wide",
    "dark": "dark",
    "warm": "warm",
    "subdued": "dark",
    "bright": "bright",
    "airy": "airy",
    "crisp": "bright",
    "aggressive": "aggressive",
    "distorted": "distorted",
    "hard": "aggressive",
    "moving": "evolving",
    "evolving": "evolving",
    "animated": "evolving",
    "sub": "sub-heavy",
    "sub-heavy": "sub-heavy",
    "subheavy": "sub-heavy",
    "lead": "lead",
    "solo": "lead",
    "melodic": "lead",
    "pad": "pad",
    "atmosphere": "pad",
    "sustained": "pad",
}


class DescriptorNormalization(SoundSelectionModel):
    """A controlled descriptor conversion retaining caller wording."""

    original_term: str = Field(min_length=1, max_length=128)
    normalized_descriptor: DescriptorIdentifier
    matched_synonym: bool = False

    @property
    def normalized(self) -> str:
        return self.normalized_descriptor


class DescriptorRule(SoundSelectionModel):
    """One descriptor and its explicit name-token aliases."""

    descriptor: DescriptorIdentifier
    aliases: tuple[str, ...] = Field(default=(), max_length=32)
    role_bias: tuple[str, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_aliases(self) -> "DescriptorRule":
        if any(not value.strip() for value in self.aliases):
            raise ValueError("descriptor aliases must contain text")
        if len({value.casefold() for value in self.aliases}) != len(self.aliases):
            raise ValueError("descriptor aliases must be unique")
        return self


class DescriptorSynonym(SoundSelectionModel):
    """One explicit source term mapped to a controlled descriptor ID."""

    original_term: str = Field(min_length=1, max_length=128)
    normalized_descriptor: DescriptorIdentifier


class DescriptorVocabulary(SoundSelectionModel):
    """Validated versioned descriptor vocabulary and name-token rules."""

    schema_version: Literal["1.0"] = DESCRIPTOR_SCHEMA_VERSION
    vocabulary_version: str = Field(min_length=1, max_length=32)
    descriptors: tuple[DescriptorRule, ...] = Field(default=(), max_length=MAX_DESCRIPTOR_RULES)
    synonyms: tuple[DescriptorSynonym, ...] = Field(default=(), max_length=256)

    @model_validator(mode="before")
    @classmethod
    def default_controlled_synonyms(cls, value: object) -> object:
        if not isinstance(value, dict) or "synonyms" in value:
            return value
        data = dict(value)
        data["synonyms"] = tuple(
            {
                "original_term": original,
                "normalized_descriptor": normalized,
            }
            for original, normalized in DESCRIPTOR_SYNONYMS.items()
        )
        return data

    @model_validator(mode="after")
    def validate_descriptors(self) -> "DescriptorVocabulary":
        ids = [item.descriptor for item in self.descriptors]
        if len({item.casefold().replace("_", "-") for item in ids}) != len(ids):
            raise ValueError("descriptor identifiers must be unique")
        terms = [item.original_term.casefold() for item in self.synonyms]
        if len(set(terms)) != len(terms):
            raise ValueError("descriptor synonym terms must be unique")
        return self

    @property
    def descriptor_ids(self) -> tuple[str, ...]:
        return tuple(item.descriptor for item in self.descriptors)

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude_none=False))

    def rule(self, descriptor: str) -> DescriptorRule | None:
        needle = descriptor.casefold()
        return next(
            (item for item in self.descriptors if item.descriptor.casefold() == needle),
            None,
        )

    @property
    def synonym_map(self) -> dict[str, str]:
        return {
            item.original_term.casefold(): item.normalized_descriptor
            for item in self.synonyms
        }


# ``DescriptorCatalog`` is the terminology used by some integrations; both
# names retain one model and one digest/version contract.
DescriptorCatalog = DescriptorVocabulary


class DescriptorLoadError(ValueError):
    """Raised when a local descriptor resource is malformed or too large."""


def _read_json_resource(source: Any) -> Any:
    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.is_dir():
            path = path / Path(DESCRIPTOR_DATA_FILE).name
        if not path.is_absolute():
            raise DescriptorLoadError("descriptor path must be absolute")
        try:
            size = path.stat().st_size
            if size > MAX_DESCRIPTOR_FILE_BYTES:
                raise DescriptorLoadError("descriptor resource exceeds size bound")
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DescriptorLoadError(f"cannot read descriptor resource: {exc}") from exc
    else:
        try:
            text = source.read_text(encoding="utf-8")
        except (AttributeError, OSError, UnicodeError) as exc:
            raise DescriptorLoadError("cannot read descriptor resource") from exc
        if len(text.encode("utf-8")) > MAX_DESCRIPTOR_FILE_BYTES:
            raise DescriptorLoadError("descriptor resource exceeds size bound")
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DescriptorLoadError("descriptor resource is not valid JSON") from exc


def _bundled_resource() -> Any:
    try:
        return resources.files("fl_studio_mcp.sound_selection").joinpath(
            DESCRIPTOR_DATA_FILE
        )
    except (ModuleNotFoundError, FileNotFoundError) as exc:
        raise DescriptorLoadError("bundled Sound Selection descriptors are unavailable") from exc


def load_descriptor_vocabulary(source: Any = None) -> DescriptorVocabulary:
    """Load one validated local descriptor data file.

    ``source`` may be an absolute JSON file or directory path, or a Traversable
    supplied by an importer.  The default resolves only the bundled file.
    """

    raw = _read_json_resource(_bundled_resource() if source is None else source)
    if not isinstance(raw, dict):
        raise DescriptorLoadError("descriptor root must be an object")
    try:
        return DescriptorVocabulary.model_validate(raw)
    except Exception as exc:
        raise DescriptorLoadError(f"descriptor data violates schema: {exc}") from exc


load_bundled_descriptors = load_descriptor_vocabulary
load_descriptor_catalog = load_descriptor_vocabulary


_PROVENANCE_RANK: dict[str, int] = {
    "unknown": 0,
    "preset_name_token": 1,
    "atlas_product": 2,
    "explicit_feedback": 3,
    "user_local_reviewed": 4,
    "bundled_reviewed": 5,
    "user_explicit": 6,
}


def _normalise_descriptor(value: str) -> str:
    # Descriptor data uses hyphenated IDs for compound concepts (for example
    # ``sub-heavy``); accepting spaces/underscores as equivalent keeps local
    # overlays from creating duplicate semantic descriptors.
    normalized = value.strip().casefold().replace("_", "-")
    normalized = re.sub(r"\s+", "-", normalized)
    return DESCRIPTOR_SYNONYMS.get(normalized, normalized)


def normalize_descriptor(value: str) -> DescriptorNormalization:
    """Normalize one controlled descriptor while preserving its source term."""

    if type(value) is not str or not value.strip():
        raise ValueError("descriptor term must contain text")
    original = value.strip()
    normalized = _normalise_descriptor(original)
    comparable = original.casefold().replace("_", "-")
    comparable = re.sub(r"\s+", "-", comparable)
    return DescriptorNormalization(
        original_term=original,
        normalized_descriptor=normalized,
        matched_synonym=normalized != comparable,
    )


normalize_descriptor_term = normalize_descriptor


def normalize_descriptors(values: Iterable[str]) -> tuple[DescriptorNormalization, ...]:
    """Normalize bounded descriptor input in first-seen order, deduplicated."""

    rows: list[DescriptorNormalization] = []
    seen: set[str] = set()
    for value in values:
        row = normalize_descriptor(value)
        if row.normalized_descriptor in seen:
            continue
        seen.add(row.normalized_descriptor)
        rows.append(row)
    return tuple(rows[:MAX_DESCRIPTORS])


def _evidence_rank(item: DescriptorEvidence) -> tuple[int, float, str, str]:
    return (
        _PROVENANCE_RANK.get(item.provenance, 0),
        item.confidence,
        item.source_id or "",
        item.detail or "",
    )


def merge_descriptor_evidence(
    *groups: Iterable[DescriptorEvidence],
) -> tuple[DescriptorEvidence, ...]:
    """Merge layered evidence, retaining the strongest deterministic record."""

    selected: dict[str, DescriptorEvidence] = {}
    for group in groups:
        for evidence in group:
            normalization = normalize_descriptor(
                evidence.original_term or evidence.descriptor
            )
            key = normalization.normalized_descriptor
            normalized_evidence = evidence.model_copy(
                update={
                    "descriptor": key,
                    "original_term": evidence.original_term or normalization.original_term,
                }
            )
            current = selected.get(key)
            if current is None or _evidence_rank(normalized_evidence) > _evidence_rank(current):
                selected[key] = normalized_evidence
    return tuple(selected[key] for key in sorted(selected))


def classify_preset_name(
    name: str,
    vocabulary: DescriptorVocabulary | None = None,
    *,
    explicit_descriptors: Iterable[str] = (),
    local_descriptors: Iterable[str] = (),
    feedback_descriptors: Iterable[str] = (),
) -> tuple[DescriptorEvidence, ...]:
    """Classify weak preset-name tokens and layer stronger local annotations."""

    if type(name) is not str:
        raise TypeError("preset name must be text")
    catalog = load_descriptor_vocabulary() if vocabulary is None else vocabulary
    token_text = name.casefold()
    tokens = set(_TOKEN_RE.findall(token_text))
    token_evidence: list[DescriptorEvidence] = []
    controlled_synonyms = {**DESCRIPTOR_SYNONYMS, **catalog.synonym_map}
    for rule in catalog.descriptors:
        matching_aliases = [
            alias
            for alias in (rule.descriptor, *rule.aliases)
            if set(_TOKEN_RE.findall(alias.casefold())).issubset(tokens)
        ]
        matched = max(
            matching_aliases,
            key=lambda alias: (len(_TOKEN_RE.findall(alias)), alias),
            default=None,
        )
        if matched is not None:
            matched_tokens = set(_TOKEN_RE.findall(matched.casefold()))
            if any(
                matched_tokens < set(_TOKEN_RE.findall(term.casefold()))
                and normalized != _normalise_descriptor(rule.descriptor)
                and set(_TOKEN_RE.findall(term.casefold())).issubset(tokens)
                for term, normalized in controlled_synonyms.items()
            ):
                matched = None
        if matched is None:
            continue
        normalization = normalize_descriptor(matched)
        token_evidence.append(
            DescriptorEvidence(
                descriptor=normalization.normalized_descriptor,
                provenance="preset_name_token",
                # A name token is useful direction, never high-confidence
                # acoustic evidence.
                confidence=0.35,
                source_id="descriptor-vocabulary-" + catalog.vocabulary_version,
                detail=f"matched preset-name token {matched!r}",
                original_term=matched,
            )
        )

    # Include controlled aliases whose canonical concept is not yet in the
    # reviewed vocabulary (for example ``punchy``/``smooth``).  Prefer the
    # longest matching phrase so ``hard attack`` does not also become the
    # unrelated single-token ``hard`` descriptor.
    synonym_matches = [
        (term, normalized)
        for term, normalized in controlled_synonyms.items()
        if set(_TOKEN_RE.findall(term.casefold())).issubset(tokens)
    ]
    seen_synonym_descriptors: set[str] = set()
    for term, normalized in sorted(
        set(synonym_matches), key=lambda item: (-len(_TOKEN_RE.findall(item[0])), item[0], item[1])
    ):
        term_tokens = set(_TOKEN_RE.findall(term.casefold()))
        if any(
            term_tokens < other_tokens
            for other_term, _ in synonym_matches
            if (other_tokens := set(_TOKEN_RE.findall(other_term.casefold())))
        ):
            continue
        if normalized in seen_synonym_descriptors:
            continue
        seen_synonym_descriptors.add(normalized)
        token_evidence.append(
            DescriptorEvidence(
                descriptor=normalized,
                provenance="preset_name_token",
                confidence=0.35,
                source_id="descriptor-vocabulary-" + catalog.vocabulary_version,
                detail=f"matched controlled descriptor synonym {term!r}",
                original_term=term,
            )
        )

    def explicit(values: Iterable[str], provenance: PresetDescriptorProvenance) -> tuple[DescriptorEvidence, ...]:
        return tuple(
            DescriptorEvidence(
                descriptor=normalize_descriptor(value).normalized_descriptor,
                provenance=provenance,
                confidence=1.0 if provenance == "user_explicit" else 0.80,
                source_id="local-annotation",
                original_term=value.strip(),
            )
            for value in values
            if type(value) is str and value.strip()
        )

    return merge_descriptor_evidence(
        token_evidence,
        explicit(feedback_descriptors, "explicit_feedback"),
        explicit(local_descriptors, "user_local_reviewed"),
        explicit(explicit_descriptors, "user_explicit"),
    )[:MAX_DESCRIPTORS]


def descriptor_names(evidence: Iterable[DescriptorEvidence]) -> tuple[str, ...]:
    """Return sorted descriptor IDs from evidence records."""

    return tuple(
        sorted(
            {
                normalize_descriptor(item.original_term or item.descriptor).normalized_descriptor
                for item in evidence
            }
        )
    )


def descriptors_for_product(product: ProductKnowledge) -> tuple[DescriptorEvidence, ...]:
    """Map Atlas metadata to low-confidence product-level descriptors."""

    if not isinstance(product, ProductKnowledge):
        raise TypeError("product must be ProductKnowledge")
    terms = " ".join(
        (
            product.name,
            *product.categories,
            *product.common_instruments,
            *product.common_sources,
            *product.common_track_types,
            *product.modes,
        )
    ).casefold()
    mapping = (
        ("synthetic", ("synth", "synthetic", "wavetable", "subtractive")),
        ("acoustic", ("acoustic", "piano", "orchestral", "sample")),
        ("digital", ("digital", "wavetable", "fm", "granular")),
        ("percussive", ("drum", "percussion", "sampler")),
        ("sub-heavy", ("bass", "sub")),
        ("evolving", ("evolving", "modular", "motion")),
        ("cinematic", ("cinematic", "orchestral", "film")),
    )
    return tuple(
        DescriptorEvidence(
            descriptor=descriptor,
            provenance="atlas_product",
            confidence=0.55,
            source_id=product.product_id,
            detail="derived from Plugin Atlas product metadata",
        )
        for descriptor, needles in mapping
        if any(needle in terms for needle in needles)
    )


__all__ = [
    "DESCRIPTOR_DATA_FILE",
    "DESCRIPTOR_SCHEMA_VERSION",
    "DESCRIPTOR_SYNONYMS",
    "DescriptorCatalog",
    "DescriptorLoadError",
    "DescriptorNormalization",
    "DescriptorRule",
    "DescriptorSynonym",
    "DescriptorVocabulary",
    "classify_preset_name",
    "descriptor_names",
    "descriptors_for_product",
    "load_bundled_descriptors",
    "load_descriptor_catalog",
    "load_descriptor_vocabulary",
    "merge_descriptor_evidence",
    "normalize_descriptor",
    "normalize_descriptor_term",
    "normalize_descriptors",
]
