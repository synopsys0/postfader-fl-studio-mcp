"""Immutable contracts for the PostFader plug-in knowledge graph.

The Atlas deliberately keeps static product knowledge separate from anything
observed in a running FL Studio session.  A product description is not a
control adapter, an adapter is not a runtime match, and a runtime match is not
write-validation evidence.  The small, bounded models in this module make
those boundaries explicit while remaining convenient to load from JSON.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


ATLAS_SCHEMA_VERSION = "1.0"
ATLAS_DIGEST_ALGORITHM = "sha256-canonical-json-v1"

MAX_ID_LENGTH = 128
MAX_NAME_LENGTH = 256
MAX_SHORT_TEXT_LENGTH = 512
MAX_DESCRIPTION_LENGTH = 4096
MAX_URL_LENGTH = 2048
MAX_DATE_LENGTH = 64
MAX_LIST_ITEMS = 256
MAX_PRODUCTS = 4096
MAX_VENDORS = 256
MAX_TECHNIQUES = 512
MAX_ADAPTERS = 4096
MAX_EVIDENCE = 8192
MAX_CONTROLS = 512
MAX_PARAMETER_INDEX = 8191
MAX_RESOURCE_COUNT = 128
MAX_RESOURCE_BYTES = 16 * 1024 * 1024


AtlasId = Annotated[str, Field(min_length=1, max_length=MAX_ID_LENGTH)]
ShortText = Annotated[str, Field(min_length=1, max_length=MAX_SHORT_TEXT_LENGTH)]
DescriptionText = Annotated[str, Field(max_length=MAX_DESCRIPTION_LENGTH)]

ProductKind = Literal["effect", "instrument", "utility", "analyzer", "unknown"]
ProductOrigin = Literal["stock", "third_party", "unknown"]
CatalogScope = Literal[
    "current_edition_matrix",
    "current_auxiliary",
    "manual_index_only",
    "legacy_discontinued",
    "selected_third_party",
    "unknown",
]
ProductLifecycle = Literal[
    "current", "auxiliary", "legacy", "deprecated", "discontinued", "unknown"
]
EditionMinimum = Literal[
    "fruity",
    "producer",
    "signature",
    "all_plugins",
    "not_applicable",
    "unknown",
]
PluginFormat = Literal["native", "vst", "vst3", "au", "unknown"]
ControlKind = Literal["numeric", "on_off", "enumerated", "unknown"]
AdapterScope = Literal["mixer_effect", "channel_generator", "both"]
EvidenceLevel = Literal[
    "detected", "read_profiled", "write_validated", "unknown"
]
EvidenceSource = Literal["maintainer", "community", "local", "runtime", "unknown"]
AvailabilityState = Literal["loaded", "not_observed", "availability_unknown"]


class AtlasModel(BaseModel):
    """Base class for strict, frozen, JSON-facing Atlas records."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        populate_by_name=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _freeze_json_arrays(cls, value: object) -> object:
        """Accept JSON/Python lists, then store every collection immutably."""

        def freeze(item: object) -> object:
            if isinstance(item, list):
                return tuple(freeze(child) for child in item)
            if isinstance(item, dict):
                return {key: freeze(child) for key, child in item.items()}
            return item

        return freeze(value)


def _normalise_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    """Trim bounded text while retaining caller order and duplicates.

    Duplicate references are meaningful validation errors at bundle-load time,
    so this validator intentionally does not silently deduplicate values.
    """

    return tuple(value.strip() for value in values)


class VendorKnowledge(AtlasModel):
    """A vendor identity used by one or more product records."""

    vendor_id: AtlasId = Field(
        validation_alias=AliasChoices("vendor_id", "id")
    )
    name: ShortText
    origin: ProductOrigin = "unknown"
    description: DescriptionText = ""
    website: str | None = Field(default=None, max_length=MAX_URL_LENGTH)
    evidence_ids: tuple[AtlasId, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )

    @property
    def id(self) -> str:
        """Compatibility accessor for data sources that call IDs ``id``."""

        return self.vendor_id


class TechniqueKnowledge(AtlasModel):
    """A reusable production technique, such as compression or filtering."""

    technique_id: AtlasId = Field(
        validation_alias=AliasChoices("technique_id", "id")
    )
    name: ShortText
    description: DescriptionText = ""
    tags: tuple[ShortText, ...] = Field(default=(), max_length=MAX_LIST_ITEMS)
    evidence_ids: tuple[AtlasId, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )

    @property
    def id(self) -> str:
        return self.technique_id


class ModuleKnowledge(AtlasModel):
    """One major product module or signal-flow section."""

    module_id: AtlasId = Field(validation_alias=AliasChoices("module_id", "id"))
    name: ShortText
    description: DescriptionText = ""
    modes: tuple[ShortText, ...] = Field(default=(), max_length=MAX_LIST_ITEMS)
    limitations: tuple[ShortText, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )

    @property
    def id(self) -> str:
        return self.module_id


class EvidenceReference(AtlasModel):
    """A concise source citation, never a copied manual or raw web page."""

    evidence_id: AtlasId = Field(
        validation_alias=AliasChoices("evidence_id", "id")
    )
    kind: Literal[
        "official_documentation",
        "official",
        "maintainer",
        "community",
        "source",
        "catalog",
        "read_profiled",
        "write_validated",
        "local_static_inspection",
        "runtime_observation",
        "unknown",
    ] = "unknown"
    source: ShortText | None = None
    detail: DescriptionText = ""
    locator: str | None = Field(default=None, max_length=MAX_URL_LENGTH)

    @property
    def id(self) -> str:
        return self.evidence_id


class ProductKnowledge(AtlasModel):
    """Static knowledge about what a plug-in is and when it is useful.

    This model intentionally contains no installation, ownership, loaded
    location, or runtime-control assertion.
    """

    product_id: AtlasId = Field(validation_alias=AliasChoices("product_id", "id"))
    vendor_id: AtlasId = "unknown"
    name: ShortText
    aliases: tuple[ShortText, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    plugin_kinds: tuple[ShortText, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    categories: tuple[ShortText, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    kind: ProductKind = Field(
        default="unknown",
        validation_alias=AliasChoices("kind", "product_kind", "product_type"),
    )
    origin: ProductOrigin = "unknown"
    catalog_scope: CatalogScope = Field(
        default="unknown",
        validation_alias=AliasChoices("catalog_scope", "scope"),
    )
    lifecycle: ProductLifecycle = "unknown"
    edition_min: EditionMinimum = "unknown"
    description: DescriptionText = Field(
        default="", validation_alias=AliasChoices("description", "summary")
    )
    problems: tuple[ShortText, ...] = Field(
        default=(),
        max_length=MAX_LIST_ITEMS,
        validation_alias=AliasChoices("problems", "problems_solved"),
    )
    use_cases: tuple[ShortText, ...] = Field(
        default=(),
        max_length=MAX_LIST_ITEMS,
        validation_alias=AliasChoices("use_cases", "appropriate_for"),
    )
    poor_fit_when: tuple[ShortText, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    common_sources: tuple[ShortText, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    common_instruments: tuple[ShortText, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    common_track_types: tuple[ShortText, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    common_buses: tuple[ShortText, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    technique_ids: tuple[AtlasId, ...] = Field(
        default=(),
        max_length=MAX_LIST_ITEMS,
        validation_alias=AliasChoices("technique_ids", "techniques"),
    )
    modules: tuple[ModuleKnowledge, ...] = Field(
        default=(),
        max_length=MAX_LIST_ITEMS,
        validation_alias=AliasChoices("modules", "major_modules"),
    )
    modes: tuple[ShortText, ...] = Field(default=(), max_length=MAX_LIST_ITEMS)
    limitations: tuple[ShortText, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    stock_alternative_ids: tuple[AtlasId, ...] = Field(
        default=(),
        max_length=MAX_LIST_ITEMS,
        validation_alias=AliasChoices(
            "stock_alternative_ids", "stock_alternatives"
        ),
    )
    evidence_ids: tuple[AtlasId, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    formats: tuple[PluginFormat, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    manual_path: str | None = Field(default=None, max_length=MAX_URL_LENGTH)
    knowledge_depth: Literal["minimal", "basic", "curated", "unknown"] = (
        "unknown"
    )
    commercial_model: str | None = Field(
        default=None, max_length=MAX_SHORT_TEXT_LENGTH
    )

    @model_validator(mode="after")
    def validate_product_text(self) -> ProductKnowledge:
        if self.product_id.casefold() == "unknown" and self.name.casefold() == "":
            raise ValueError("a product needs an identifier or name")
        if self.product_id in self.stock_alternative_ids:
            raise ValueError("a product cannot list itself as a stock alternative")
        return self

    @property
    def id(self) -> str:
        return self.product_id

    @property
    def stock(self) -> bool:
        """Whether the catalog classifies this as a stock product."""

        return self.origin == "stock"

    @property
    def is_stock(self) -> bool:
        return self.stock

    @property
    def techniques(self) -> tuple[str, ...]:
        return self.technique_ids

    @property
    def summary(self) -> str:
        return self.description

    @property
    def problems_solved(self) -> tuple[str, ...]:
        return self.problems

    @property
    def appropriate_for(self) -> tuple[str, ...]:
        return self.use_cases

    @property
    def major_modules(self) -> tuple[ModuleKnowledge, ...]:
        return self.modules


class AdapterControl(AtlasModel):
    """One expected control shape exposed by a specific adapter."""

    control_id: AtlasId = Field(validation_alias=AliasChoices("control_id", "id"))
    parameter_index: int | None = Field(
        default=None, ge=0, le=MAX_PARAMETER_INDEX
    )
    names: tuple[ShortText, ...] = Field(
        default=(),
        max_length=MAX_LIST_ITEMS,
        validation_alias=AliasChoices("names", "aliases", "parameter_names"),
    )
    display_names: tuple[ShortText, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    kind: ControlKind = "unknown"
    role: str | None = Field(default=None, max_length=MAX_NAME_LENGTH)
    options: tuple[ShortText, ...] = Field(default=(), max_length=MAX_LIST_ITEMS)
    unit: str | None = Field(
        default=None,
        max_length=MAX_NAME_LENGTH,
        validation_alias=AliasChoices("unit", "display_unit"),
    )
    preferred_write_tool: Literal[
        "fl_set_plugin_param_display",
        "fl_set_plugin_param_option",
        "fl_set_plugin_param",
        "unknown",
    ] = "unknown"
    required: bool = False
    evidence_ids: tuple[AtlasId, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )

    @model_validator(mode="after")
    def validate_control(self) -> AdapterControl:
        if self.kind != "enumerated" and self.options:
            raise ValueError("options are only valid for enumerated controls")
        if not self.names and not self.display_names and self.parameter_index is None:
            raise ValueError(
                "a control needs an index, a name, or a display-name selector"
            )
        return self

    @property
    def id(self) -> str:
        return self.control_id

    @property
    def display_unit(self) -> str | None:
        return self.unit

    @property
    def name_candidates(self) -> tuple[str, ...]:
        return self.names


class AdapterRecipe(AtlasModel):
    """A bounded processing-intent recipe attached to an adapter."""

    recipe_id: AtlasId = Field(validation_alias=AliasChoices("recipe_id", "id"))
    intent: ShortText
    parameter_roles: tuple[ShortText, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    guidance: DescriptionText

    @property
    def id(self) -> str:
        return self.recipe_id


class ControlAdapter(AtlasModel):
    """How one product/version/format can expose controls to PostFader.

    Adapter knowledge is descriptive.  It never implies that a runtime
    instance matched the product or that any write has been validated.
    """

    adapter_id: AtlasId = Field(
        validation_alias=AliasChoices("adapter_id", "id", "profile_id")
    )
    product_id: AtlasId
    name: ShortText | None = None
    reported_names: tuple[ShortText, ...] = Field(
        default=(),
        max_length=MAX_LIST_ITEMS,
        validation_alias=AliasChoices("reported_names", "plugin_names"),
    )
    formats: tuple[PluginFormat, ...] = Field(
        default=("unknown",), max_length=MAX_LIST_ITEMS
    )
    version_range: str | None = Field(default=None, max_length=MAX_NAME_LENGTH)
    scope: AdapterScope = "both"
    category: Literal[
        "equalizer",
        "compressor",
        "limiter",
        "reverb",
        "delay",
        "multiband",
        "other",
    ] = "other"
    supported_intents: tuple[ShortText, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    controls: tuple[AdapterControl, ...] = Field(
        default=(), max_length=MAX_CONTROLS
    )
    recipes: tuple[AdapterRecipe, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    evidence_ids: tuple[AtlasId, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    limitations: tuple[ShortText, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    provenance: str = Field(default="bundled_atlas", max_length=MAX_SHORT_TEXT_LENGTH)
    exact_version_required: bool = False
    warnings: tuple[ShortText, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )

    @property
    def id(self) -> str:
        return self.adapter_id

    @property
    def plugin_names(self) -> tuple[str, ...]:
        return self.reported_names

    @property
    def parameters(self) -> tuple[AdapterControl, ...]:
        """Alias matching the existing ``PluginAdapterProfile`` vocabulary."""

        return self.controls


class WriteValidationEvidence(AtlasModel):
    """Evidence from a real disposable-project write/readback test."""

    evidence_id: AtlasId = Field(
        validation_alias=AliasChoices("evidence_id", "id")
    )
    product_id: AtlasId
    adapter_id: AtlasId | None = None
    control_ids: tuple[AtlasId, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    status: Literal["validated", "failed", "not_tested"]
    source: EvidenceSource = "local"
    basis: Literal[
        "readback_on_a_later_fl_idle_tick",
        "static_documentation",
        "unknown",
    ] = "unknown"
    detail: DescriptionText = ""
    validation_scope: Literal["representative", "complete", "unknown"] = (
        "representative"
    )
    observed_parameters: tuple[int, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )

    @model_validator(mode="after")
    def validate_write_evidence(self) -> WriteValidationEvidence:
        if self.status == "validated":
            if self.basis != "readback_on_a_later_fl_idle_tick":
                raise ValueError(
                    "validated write evidence requires later-idle-tick readback"
                )
            if self.adapter_id is None or not self.control_ids:
                raise ValueError(
                    "validated write evidence must identify an adapter and controls"
                )
        if any(index < 0 or index > MAX_PARAMETER_INDEX for index in self.observed_parameters):
            raise ValueError("observed parameter indices exceed Atlas bounds")
        return self

    @property
    def id(self) -> str:
        return self.evidence_id


class CompatibilityEvidence(AtlasModel):
    """Non-authoritative evidence joining a product and optional adapter."""

    evidence_id: AtlasId = Field(
        validation_alias=AliasChoices("evidence_id", "id")
    )
    product_id: AtlasId
    adapter_id: AtlasId | None = None
    level: EvidenceLevel
    source: EvidenceSource = "unknown"
    detail: DescriptionText = ""
    control_ids: tuple[AtlasId, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    locator: str | None = Field(default=None, max_length=MAX_URL_LENGTH)

    @property
    def id(self) -> str:
        return self.evidence_id


class AvailabilityObservation(AtlasModel):
    """What can safely be said about a product's current availability."""

    state: AvailabilityState
    source: Literal["runtime_inventory", "user_inventory", "unknown"] = (
        "unknown"
    )
    detail: str | None = Field(default=None, max_length=MAX_SHORT_TEXT_LENGTH)

    @model_validator(mode="after")
    def prevent_unbounded_availability_claims(
        self,
    ) -> AvailabilityObservation:
        # The enum itself excludes ``not_owned`` and ``not_installed``.  Keep
        # this validator as an explicit guard because this is a trust boundary.
        if self.state not in {
            "loaded",
            "not_observed",
            "availability_unknown",
        }:
            raise ValueError("Atlas cannot assert ownership or installation state")
        return self


class RuntimeParameterObservation(AtlasModel):
    """A bounded, identity-neutral parameter observation from FL Studio."""

    index: int = Field(ge=0, le=MAX_PARAMETER_INDEX)
    name: str | None = Field(
        default=None,
        max_length=MAX_NAME_LENGTH,
        validation_alias=AliasChoices("name", "reported_name"),
    )
    display: str | None = Field(
        default=None,
        max_length=MAX_NAME_LENGTH,
        validation_alias=AliasChoices("display", "display_text"),
    )
    kind: ControlKind = "unknown"
    options: tuple[ShortText, ...] = Field(default=(), max_length=MAX_LIST_ITEMS)


class RuntimePluginInstance(AtlasModel):
    """A currently observed plug-in instance, independent of ownership."""

    instance_id: AtlasId | None = None
    name: ShortText = Field(
        validation_alias=AliasChoices("name", "plugin_name", "reported_name")
    )
    user_name: str | None = Field(default=None, max_length=MAX_NAME_LENGTH)
    format: PluginFormat = Field(
        default="unknown", validation_alias=AliasChoices("format", "plugin_format")
    )
    availability: AvailabilityObservation = Field(
        default_factory=lambda: AvailabilityObservation(
            state="loaded", source="runtime_inventory"
        )
    )
    parameters: tuple[RuntimeParameterObservation, ...] = Field(
        default=(), max_length=MAX_PARAMETER_INDEX + 1
    )

    @model_validator(mode="after")
    def loaded_instance_has_loaded_state(self) -> RuntimePluginInstance:
        if self.availability.state != "loaded" and self.parameters:
            raise ValueError(
                "only loaded runtime instances may carry live parameter observations"
            )
        return self


class ParameterMatchEvidence(AtlasModel):
    """Evidence that one observed parameter resembles one adapter control."""

    control_id: AtlasId
    parameter_index: int = Field(ge=0, le=MAX_PARAMETER_INDEX)
    basis: Literal["index", "name", "display", "name_and_display"]
    score: float = Field(ge=0.0, le=1.0)
    observed_name: str | None = Field(default=None, max_length=MAX_NAME_LENGTH)
    observed_display: str | None = Field(default=None, max_length=MAX_NAME_LENGTH)


class RuntimeMatch(AtlasModel):
    """A scored runtime-to-product/adapter match with explicit proof limits."""

    instance_id: AtlasId | None = None
    product_id: AtlasId | None = None
    adapter_id: AtlasId | None = None
    availability: AvailabilityObservation = Field(
        default_factory=lambda: AvailabilityObservation(
            state="loaded", source="runtime_inventory"
        )
    )
    name_score: float = Field(ge=0.0, le=1.0)
    parameter_score: float = Field(ge=0.0, le=1.0)
    overall_score: float = Field(ge=0.0, le=1.0)
    confidence: Literal["high", "medium", "low", "unknown"] = "unknown"
    control_status: Literal[
        "not_evaluated", "no_evidence", "name_only", "evidence"
    ] = "not_evaluated"
    parameter_evidence: tuple[ParameterMatchEvidence, ...] = Field(
        default=(), max_length=MAX_CONTROLS
    )
    reasons: tuple[ShortText, ...] = Field(default=(), max_length=MAX_LIST_ITEMS)
    warnings: tuple[ShortText, ...] = Field(default=(), max_length=MAX_LIST_ITEMS)

    @model_validator(mode="after")
    def prevent_name_only_control_proof(self) -> RuntimeMatch:
        if self.parameter_evidence and (
            self.product_id is None or self.adapter_id is None
        ):
            raise ValueError(
                "parameter evidence requires both a product and control adapter"
            )
        if self.control_status == "evidence" and not self.parameter_evidence:
            raise ValueError(
                "control evidence status requires observed parameter evidence"
            )
        if self.parameter_evidence and self.control_status != "evidence":
            raise ValueError(
                "parameter evidence requires the explicit evidence control status"
            )
        if self.control_status == "evidence" and self.availability.state != "loaded":
            raise ValueError(
                "runtime control evidence requires a loaded observation"
            )
        if self.control_status in {"no_evidence", "name_only"} and self.parameter_evidence:
            raise ValueError(
                "parameter evidence cannot be labelled as name-only evidence"
            )
        if self.control_status == "name_only" and self.product_id is None:
            raise ValueError("name-only matches require a product identity")
        return self

    @property
    def control_proven(self) -> bool:
        """True only when concrete parameter evidence exists.

        A product-name match, even an exact one, returns ``False`` here.
        """

        return bool(self.parameter_evidence) and self.control_status == "evidence"

    @property
    def control_proof(self) -> bool:
        """Alias used by integration callers and serialized report builders."""

        return self.control_proven


class CompatibilityJoin(AtlasModel):
    """Joined static/runtime/evidence view with no ownership inference."""

    instance_id: AtlasId | None = None
    product_id: AtlasId | None = None
    adapter_id: AtlasId | None = None
    availability: AvailabilityObservation
    product_match: bool = False
    adapter_match: bool = False
    control_proven: bool = False
    compatibility: Literal[
        "unknown", "name_only", "control_evidence", "write_validated"
    ] = "unknown"
    match_confidence: Literal["high", "medium", "low", "unknown"] = "unknown"
    evidence: tuple[CompatibilityEvidence, ...] = Field(
        default=(), max_length=MAX_EVIDENCE
    )
    write_validation: tuple[WriteValidationEvidence, ...] = Field(
        default=(), max_length=MAX_EVIDENCE
    )
    limitations: tuple[ShortText, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )

    @model_validator(mode="after")
    def enforce_join_proof(self) -> CompatibilityJoin:
        if self.product_match and self.product_id is None:
            raise ValueError("a product match requires a product identifier")
        if self.adapter_match and (
            not self.product_match or self.adapter_id is None
        ):
            raise ValueError(
                "an adapter match requires product and adapter identifiers"
            )
        if self.control_proven and not self.adapter_match:
            raise ValueError("control proof requires an adapter match")
        if self.compatibility == "write_validated":
            if not self.adapter_match or not self.control_proven:
                raise ValueError(
                    "write-validated compatibility requires adapter and control proof"
                )
            if not any(item.status == "validated" for item in self.write_validation):
                raise ValueError(
                    "write-validated compatibility needs validated write evidence"
                )
        elif self.compatibility == "control_evidence":
            if not self.adapter_match or not self.control_proven:
                raise ValueError(
                    "control-evidence compatibility requires adapter and control proof"
                )
        elif self.compatibility == "name_only":
            if not self.product_match or self.control_proven:
                raise ValueError(
                    "name-only compatibility requires a product match without control proof"
                )
        elif self.control_proven:
            raise ValueError(
                "control proof requires control-evidence or write-validated compatibility"
            )
        return self


class ResourceManifest(AtlasModel):
    """One bounded JSON resource declared by an Atlas manifest."""

    path: str = Field(
        validation_alias=AliasChoices("path", "file", "resource"),
        min_length=1,
        max_length=MAX_URL_LENGTH,
    )
    kind: Literal[
        "catalog",
        "catalog_snapshot",
        "products",
        "vendors",
        "techniques",
        "adapters",
        "evidence",
    ] = "catalog"
    required: bool = True
    max_bytes: int = Field(default=MAX_RESOURCE_BYTES, ge=1, le=MAX_RESOURCE_BYTES)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    record_count: int | None = Field(default=None, ge=0, le=MAX_EVIDENCE)
    expected_name_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )


class CatalogCategoryCount(AtlasModel):
    """One optional category count in a catalog completeness oracle."""

    category: AtlasId
    count: int = Field(ge=0, le=MAX_PRODUCTS)


CatalogRowCategory = Literal["audio_editor", "effect", "instrument", "visual"]
CatalogRowEdition = Literal[
    "fruity", "producer", "signature", "all_plugins", "unknown"
]


class CatalogProductRow(AtlasModel):
    """One row in a vendor pricing/edition catalog snapshot."""

    name: ShortText
    category: CatalogRowCategory
    edition_min: CatalogRowEdition = "unknown"
    product_id: AtlasId | None = None


class CatalogSnapshotManifestEntry(AtlasModel):
    """Manifest metadata for a row-level catalog completeness oracle."""

    resource: str = Field(min_length=1, max_length=MAX_URL_LENGTH)
    snapshot_id: AtlasId = Field(
        validation_alias=AliasChoices("snapshot_id", "id")
    )
    vendor_id: AtlasId
    catalog_scope: CatalogScope = Field(
        default="unknown",
        validation_alias=AliasChoices("catalog_scope", "scope"),
    )
    expected_row_count: int | None = Field(
        default=None,
        ge=0,
        le=MAX_PRODUCTS,
        validation_alias=AliasChoices(
            "expected_row_count", "expected_product_count"
        ),
    )
    expected_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        validation_alias=AliasChoices("expected_digest", "expected_name_digest"),
    )
    catalog_as_of: str | None = Field(default=None, max_length=MAX_DATE_LENGTH)
    fl_studio_version: str | None = Field(
        default=None, max_length=MAX_NAME_LENGTH
    )
    source_snapshot: str | None = Field(
        default=None, max_length=MAX_SHORT_TEXT_LENGTH
    )
    category_counts: tuple[CatalogCategoryCount, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )


class CatalogSnapshot(AtlasModel):
    """Validated catalog rows, resolved to unique Atlas products."""

    snapshot_id: AtlasId
    vendor_id: AtlasId
    catalog_scope: CatalogScope = Field(
        default="unknown",
        validation_alias=AliasChoices("catalog_scope", "scope"),
    )
    rows: tuple[CatalogProductRow, ...] = Field(
        default=(), max_length=MAX_PRODUCTS
    )
    catalog_as_of: str | None = Field(default=None, max_length=MAX_DATE_LENGTH)
    expected_row_count: int | None = Field(default=None, ge=0, le=MAX_PRODUCTS)
    expected_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class CatalogManifestEntry(AtlasModel):
    """A vendor-catalog completeness oracle, separate from product facts."""

    resource: str = Field(min_length=1, max_length=MAX_URL_LENGTH)
    vendor_id: AtlasId
    coverage: str = Field(default="unknown", max_length=MAX_SHORT_TEXT_LENGTH)
    expected_product_count: int | None = Field(
        default=None, ge=0, le=MAX_PRODUCTS
    )
    expected_name_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    source_base_url: str | None = Field(default=None, max_length=MAX_URL_LENGTH)
    index_sources: tuple[str, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    catalog_as_of: str | None = Field(default=None, max_length=MAX_DATE_LENGTH)
    fl_studio_version: str | None = Field(
        default=None, max_length=MAX_NAME_LENGTH
    )
    source_snapshot: str | None = Field(
        default=None, max_length=MAX_SHORT_TEXT_LENGTH
    )
    category_counts: tuple[CatalogCategoryCount, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )


class AtlasManifest(AtlasModel):
    """Manifest for a versioned, bounded, local-only Atlas bundle."""

    schema_version: Literal["1.0"] = ATLAS_SCHEMA_VERSION
    dataset_id: AtlasId = "postfader-plugin-atlas"
    dataset_version: str = Field(
        default=ATLAS_SCHEMA_VERSION,
        validation_alias=AliasChoices("dataset_version", "version"),
        max_length=MAX_NAME_LENGTH,
    )
    catalog_as_of: str | None = Field(default=None, max_length=MAX_DATE_LENGTH)
    fl_studio_version: str | None = Field(
        default=None, max_length=MAX_NAME_LENGTH
    )
    source_snapshot: str | None = Field(
        default=None, max_length=MAX_SHORT_TEXT_LENGTH
    )
    resources: tuple[ResourceManifest, ...] = Field(
        default=(ResourceManifest(path="atlas.json"),),
        max_length=MAX_RESOURCE_COUNT,
    )
    catalogs: tuple[CatalogManifestEntry, ...] = Field(
        default=(), max_length=MAX_VENDORS
    )
    catalog_snapshots: tuple[CatalogSnapshotManifestEntry, ...] = Field(
        default=(), max_length=MAX_VENDORS
    )
    expected_products: int | None = Field(default=None, ge=0, le=MAX_PRODUCTS)
    expected_adapters: int | None = Field(default=None, ge=0, le=MAX_ADAPTERS)
    expected_vendors: int | None = Field(default=None, ge=0, le=MAX_VENDORS)
    expected_techniques: int | None = Field(default=None, ge=0, le=MAX_TECHNIQUES)
    expected_evidence: int | None = Field(default=None, ge=0, le=MAX_EVIDENCE)


class AtlasBundle(AtlasModel):
    """Validated static Atlas data before it is indexed by a registry."""

    manifest: AtlasManifest
    vendors: tuple[VendorKnowledge, ...] = Field(
        default=(), max_length=MAX_VENDORS
    )
    techniques: tuple[TechniqueKnowledge, ...] = Field(
        default=(), max_length=MAX_TECHNIQUES
    )
    products: tuple[ProductKnowledge, ...] = Field(
        default=(), max_length=MAX_PRODUCTS
    )
    adapters: tuple[ControlAdapter, ...] = Field(
        default=(), max_length=MAX_ADAPTERS
    )
    catalog_snapshots: tuple[CatalogSnapshot, ...] = Field(
        default=(), max_length=MAX_VENDORS
    )
    evidence: tuple[
        EvidenceReference | CompatibilityEvidence | WriteValidationEvidence, ...
    ] = Field(
        default=(), max_length=MAX_EVIDENCE
    )


class ProductRecommendation(AtlasModel):
    """A deterministic recommendation row, not a claim of availability."""

    product_id: AtlasId
    score: float = Field(ge=0.0, le=1.0)
    reasons: tuple[ShortText, ...] = Field(default=(), max_length=MAX_LIST_ITEMS)
    availability: AvailabilityObservation = Field(
        default_factory=lambda: AvailabilityObservation(state="availability_unknown")
    )
    loaded_instance_id: AtlasId | None = None
    stock_alternative: bool = False
    source_product_id: AtlasId | None = None
    matched_fields: tuple[ShortText, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )


class RecommendationRequest(AtlasModel):
    """Bounded, deterministic inputs for product recommendations."""

    query: str = Field(default="", max_length=MAX_SHORT_TEXT_LENGTH)
    problems: tuple[ShortText, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    techniques: tuple[AtlasId, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    sources: tuple[ShortText, ...] = Field(
        default=(), max_length=MAX_LIST_ITEMS
    )
    kind: ProductKind | None = None
    prefer_stock: bool = False
    limit: int = Field(default=16, ge=1, le=128)


# Friendly aliases for integrations that use the domain vocabulary from the
# design brief.  The canonical classes above remain the serialized contract.
PluginProduct = ProductKnowledge
ControlAdapterKnowledge = ControlAdapter
RuntimeInstance = RuntimePluginInstance
OwnershipInstallationState = AvailabilityObservation
WriteEvidence = WriteValidationEvidence


__all__ = [
    "ATLAS_DIGEST_ALGORITHM",
    "ATLAS_SCHEMA_VERSION",
    "AdapterRecipe",
    "AdapterControl",
    "AdapterScope",
    "AtlasBundle",
    "AtlasId",
    "AtlasManifest",
    "AtlasModel",
    "AvailabilityObservation",
    "AvailabilityState",
    "CatalogProductRow",
    "CatalogRowCategory",
    "CatalogRowEdition",
    "CatalogSnapshot",
    "CatalogSnapshotManifestEntry",
    "CompatibilityEvidence",
    "CompatibilityJoin",
    "CatalogManifestEntry",
    "CatalogCategoryCount",
    "ControlAdapter",
    "ControlAdapterKnowledge",
    "ControlKind",
    "DescriptionText",
    "CatalogScope",
    "EditionMinimum",
    "EvidenceLevel",
    "EvidenceReference",
    "EvidenceSource",
    "MAX_ADAPTERS",
    "MAX_CONTROLS",
    "MAX_EVIDENCE",
    "MAX_ID_LENGTH",
    "MAX_LIST_ITEMS",
    "MAX_PARAMETER_INDEX",
    "MAX_PRODUCTS",
    "MAX_RESOURCE_BYTES",
    "MAX_RESOURCE_COUNT",
    "MAX_SHORT_TEXT_LENGTH",
    "MAX_TECHNIQUES",
    "MAX_VENDORS",
    "ModuleKnowledge",
    "ParameterMatchEvidence",
    "PluginFormat",
    "ProductKind",
    "ProductLifecycle",
    "ProductKnowledge",
    "PluginProduct",
    "ProductOrigin",
    "ProductRecommendation",
    "RecommendationRequest",
    "ResourceManifest",
    "RuntimeMatch",
    "RuntimeInstance",
    "RuntimeParameterObservation",
    "RuntimePluginInstance",
    "ShortText",
    "TechniqueKnowledge",
    "VendorKnowledge",
    "WriteValidationEvidence",
    "WriteEvidence",
    "OwnershipInstallationState",
]
