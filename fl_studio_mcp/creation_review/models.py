"""Immutable contracts for Creation Review.

Creation Review is deliberately a contract layer.  It records observations,
user decisions, and closed revision plans; it does not inspect FL Studio or
perform a project mutation.  Runtime services can use these records as a
small, stable boundary between the connected creative planner and the
bounded review-session executor.

The models in this module follow the creation-pipeline conventions: unknown
fields are rejected, scalar coercion is disabled, and collections are tuples.
``FrozenMap`` is used only for deliberately open-ended measurement/receipt
payloads.  It gives callers mapping ergonomics while recursively freezing its
contents, so a ``frozen=True`` Pydantic model is immutable below the top level
as well.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    model_validator,
)
from pydantic_core import core_schema

from ..creation_pipeline.models import CreationPipelineModel
from ..creation_pipeline.outcomes import CreationOutcome


CREATION_REVIEW_SCHEMA_VERSION = "1.0"
CREATION_REVIEW_DIGEST_ALGORITHM = "sha256-canonical-json-v1"
SHA256_PATTERN = r"^[0-9a-f]{64}$"

MAX_REVIEW_ID = 128
MAX_REVIEW_ROLE = 64
MAX_REVIEW_TEXT = 4096
MAX_REVIEW_SHORT_TEXT = 512
MAX_REVIEW_SESSIONS = 64
MAX_REVIEW_EVALUATIONS = 32
MAX_REVIEW_FEEDBACK = 128
MAX_REVIEW_ASSET_SETS = 64
MAX_REVIEW_ASSETS = 256
MAX_REVIEW_FINDINGS = 256
MAX_REVIEW_PRIORITIES = 32
MAX_REVIEW_SECTIONS = 128
MAX_REVIEW_STEMS = 128
MAX_REVIEW_PASSES = 8
MAX_REVIEW_OPERATIONS = 64
MAX_REVIEW_COMPARISONS = 64
MAX_REVIEW_MANIFESTS = 32
MAX_REVIEW_WARNINGS = 64
MAX_REVIEW_LIMITATIONS = 64
MAX_REVIEW_SERIALIZED_BYTES = 16 * 1024 * 1024
MAX_REVIEW_ASSET_BYTES = 4 * 1024 * 1024 * 1024
MAX_REVIEW_AUDIO_SECONDS = 24 * 60 * 60


Identifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_REVIEW_ID,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
RoleIdentifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_REVIEW_ROLE,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
Digest = Annotated[str, Field(pattern=SHA256_PATTERN)]
BoundedText = Annotated[str, Field(min_length=1, max_length=MAX_REVIEW_TEXT)]
ShortText = Annotated[str, Field(min_length=1, max_length=MAX_REVIEW_SHORT_TEXT)]

ReviewInteractionPolicy = Literal[
    "analyze_only",
    "analyze_and_plan",
    "apply_once",
    "iterate_on_new_bounces",
]
ReviewSessionStatus = Literal[
    "created",
    "awaiting_assets",
    "evaluating",
    "evaluated",
    "revision_planned",
    "revising",
    "awaiting_rebounce",
    "comparing",
    "accepted",
    "rejected",
    "completed",
    "blocked",
    "stopped",
]
ReviewAssetKind = Literal[
    "candidate_full_mix",
    "before_full_mix",
    "after_full_mix",
    "reference_full_mix",
    "instrumental_stem",
    "vocal_stem",
    "drum_stem",
    "bass_stem",
    "chord_stem",
    "lead_stem",
    "role_stem",
    "section_bounce",
]
AssetValidationState = Literal[
    "unvalidated",
    "valid",
    "invalid",
    "missing",
    "unsupported",
    "stale",
    "changing",
]
SectionSource = Literal[
    "production_run",
    "section_marker",
    "user_supplied",
    "playlist_handoff",
    "detected_suggestion",
]
ConfidenceLevel = Literal["high", "medium", "low", "unknown"]
FindingCategory = Literal[
    "technical_export",
    "clipping_or_headroom",
    "dynamics",
    "tonal_balance",
    "low_end",
    "stereo",
    "masking",
    "section_contrast",
    "arrangement_development",
    "sound_selection",
    "composition",
    "processing",
    "delivery",
    "user_feedback",
    "insufficient_evidence",
]
EvidenceSource = Literal[
    "decoded_audio_measurement",
    "synchronized_stem_measurement",
    "reference_comparison",
    "production_run_receipt",
    "sound_palette_metadata",
    "explicit_user_feedback",
    "connected_ai_interpretation",
]
FindingSeverity = Literal["critical", "high", "medium", "low", "info"]
Actionability = Literal[
    "actionable",
    "informational",
    "requires_user_judgment",
    "insufficient_evidence",
    "not_actionable",
]
GoalEvaluationState = Literal[
    "technically_evaluable",
    "proxy_evaluable",
    "requires_user_judgment",
    "not_evaluable_from_supplied_assets",
]
FeedbackSource = Literal[
    "user_explicit",
    "connected_ai_interpretation",
    "bounce_measurement",
    "system_default",
]
FeedbackVerdict = Literal[
    "approved",
    "accepted",
    "rejected",
    "needs_revision",
    "neutral",
    # Compatibility values used by the explicit feedback helper.
    "user_confirmed_draft",
    "user_approved",
    "user_rejected",
]
ApprovalLevel = Literal[
    "none",
    "element",
    "section",
    "overall",
    "final",
    "draft",
    "approved",
    "rejected",
]
LockKind = Literal[
    "sound_assignment",
    "note_content",
    "rhythm",
    "register",
    "processing",
    "level",
    "section_placement",
    "role_identity",
]
LockTarget = Literal[
    "overall",
    "section",
    "role",
    "palette_assignment",
    "composition_part",
    "drum_role",
    "processing_goal",
    "manual_delivery",
]
RiskLevel = Literal["low", "medium", "high", "critical"]
RevisionStrength = Literal["subtle", "balanced", "substantial", "custom"]
RegeneratePreference = Literal["prefer_transform", "prefer_regenerate", "auto"]
ProcessingPolicy = Literal["preserve", "adjust_allowed", "replace_allowed", "explicit_only"]
UserApprovalState = Literal[
    "not_requested",
    "pending",
    "approved",
    "rejected",
    "unknown",
    "user_confirmed_draft",
    "user_approved",
    "user_rejected",
    "needs_revision",
]
TechnicalConclusion = Literal[
    "improved",
    "regressed",
    "mixed",
    "unchanged",
    "unknown",
]

RevisionOperationKind = Literal[
    "transform_generated_sequence",
    "regenerate_role_sequence",
    "create_section_note_variation",
    "change_sound_assignment",
    "create_sound_palette_variation",
    "change_drum_kit",
    "change_drum_role_mapping",
    "adjust_role_level",
    "adjust_channel_mix",
    "apply_semantic_processing",
    "replace_processing_plan",
    "change_section_density",
    "change_section_register",
    "change_section_voicing",
    "change_section_rhythm",
    "change_section_velocity",
    "change_section_articulation",
    "add_supporting_layer",
    "remove_generated_layer",
    "update_section_markers",
    "create_playlist_handoff_delta",
    "record_feedback_lock",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_digest(value: object) -> str:
    """Return the repository's deterministic SHA-256 JSON digest."""

    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=lambda item: item.isoformat() if isinstance(item, datetime) else str(item),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _as_json_value(value: object, *, depth: int = 0) -> object:
    """Recursively convert a payload into immutable JSON-compatible values."""

    if depth > 8:
        raise ValueError("review payload nesting exceeds the safety bound")
    if isinstance(value, BaseModel):
        return _as_json_value(value.model_dump(mode="python"), depth=depth + 1)
    if isinstance(value, FrozenMap):
        return value
    if isinstance(value, Mapping):
        return FrozenMap(value, _depth=depth + 1)
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_REVIEW_ASSETS:
            raise ValueError("review payload collection exceeds the safety bound")
        return tuple(_as_json_value(item, depth=depth + 1) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("review payload numbers must be finite")
        if isinstance(value, str) and len(value) > MAX_REVIEW_TEXT:
            raise ValueError("review payload text exceeds the safety bound")
        return value
    raise ValueError(f"unsupported review payload value: {type(value).__name__}")


def _json_value(value: object) -> object:
    """Convert immutable payloads back to JSON-compatible output values."""

    if isinstance(value, FrozenMap):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="python"))
    if isinstance(value, datetime):
        return value.isoformat()
    return value


class FrozenMap(Mapping[str, object]):
    """A recursively immutable mapping with Pydantic JSON serialization."""

    __slots__ = ("_items", "_values")

    def __init__(self, value: Mapping[str, object] | FrozenMap, *, _depth: int = 0):
        if isinstance(value, FrozenMap):
            object.__setattr__(self, "_items", value._items)
            object.__setattr__(self, "_values", value._values)
            return
        if not isinstance(value, Mapping):
            raise TypeError("FrozenMap requires a mapping")
        if len(value) > MAX_REVIEW_ASSETS:
            raise ValueError("review mapping exceeds the safety bound")
        pairs: list[tuple[str, object]] = []
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError("review mapping keys must be non-empty strings")
            if len(key) > MAX_REVIEW_SHORT_TEXT:
                raise ValueError("review mapping key exceeds the safety bound")
            pairs.append((key, _as_json_value(item, depth=_depth + 1)))
        pairs.sort(key=lambda pair: pair[0])
        object.__setattr__(self, "_items", tuple(key for key, _ in pairs))
        object.__setattr__(self, "_values", tuple(item for _, item in pairs))

    def __setattr__(self, name: str, value: object) -> None:
        """Prevent callers from replacing the private immutable storage."""

        if name in self.__slots__ and hasattr(self, name):
            raise TypeError("FrozenMap is immutable")
        object.__setattr__(self, name, value)

    def __getitem__(self, key: str) -> object:
        try:
            index = self._items.index(key)
        except ValueError as exc:
            raise KeyError(key) from exc
        return self._values[index]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"FrozenMap({dict(self.items())!r})"

    def __hash__(self) -> int:
        return hash(self._items_and_values())

    def _items_and_values(self) -> tuple[tuple[str, object], ...]:
        return tuple(zip(self._items, self._values))

    def to_dict(self) -> dict[str, object]:
        return {key: _json_value(item) for key, item in self.items()}

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: Any, _handler: Any
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.any_schema(),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda value: value.to_dict(), return_schema=core_schema.any_schema()
            ),
        )


class CreationReviewModel(CreationPipelineModel):
    """Strict, frozen base for all Creation Review contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        validate_default=True,
        populate_by_name=True,
        serialize_by_alias=True,
        arbitrary_types_allowed=True,
    )

    @model_validator(mode="after")
    def _validate_text_tuple_bounds(self) -> "CreationReviewModel":
        for name in type(self).model_fields:
            value = getattr(self, name, None)
            if not isinstance(value, tuple):
                continue
            for index, item in enumerate(value):
                if isinstance(item, str) and len(item) > MAX_REVIEW_TEXT:
                    raise ValueError(f"{name}[{index}] exceeds review text bounds")
        return self


class ReviewMetric(CreationReviewModel):
    """One named scalar measurement and its unit/confidence."""

    name: ShortText
    value: float | int | StrictBool | str | None
    unit: str | None = Field(default=None, max_length=64)
    confidence: ConfidenceLevel | None = None
    evidence_source: EvidenceSource | None = None

    @model_validator(mode="after")
    def validate_value(self) -> "ReviewMetric":
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("measurement value must be finite")
        return self


class MetricExpectation(CreationReviewModel):
    """An expected direction/target used to assess a revision objective."""

    metric: ShortText
    direction: Literal["increase", "decrease", "maintain", "within", "change"] = "change"
    target_value: float | None = None
    tolerance: float | None = Field(default=None, ge=0.0)
    unit: str | None = Field(default=None, max_length=64)
    state: GoalEvaluationState = "proxy_evaluable"
    rationale: str | None = Field(default=None, max_length=MAX_REVIEW_TEXT)


class GoalEvaluation(CreationReviewModel):
    """Bounded evaluability/result state for one requested review goal."""

    goal: ShortText
    # ``origin``/``domain`` and the evidence fields retain the analyzer's
    # bounded classification without reopening the model to arbitrary dicts.
    # They are optional so older producers that only supplied ``goal`` and
    # ``state`` remain valid.
    origin: ShortText | None = Field(default=None, max_length=64)
    state: GoalEvaluationState = "not_evaluable_from_supplied_assets"
    classification: GoalEvaluationState | None = None
    domain: ShortText | None = Field(default=None, max_length=64)
    evidence: tuple[ShortText, ...] = Field(default=(), max_length=16)
    rationale: BoundedText | None = None
    required_additional_evidence: tuple[ShortText, ...] = Field(
        default=(), max_length=16
    )
    expected: MetricExpectation | None = Field(
        default=None,
        validation_alias=AliasChoices("expected", "expectation"),
    )
    observed: ReviewMetric | FrozenMap | None = Field(
        default=None,
        validation_alias=AliasChoices("observed", "observed_measurement"),
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    summary: BoundedText | None = None
    limitations: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_LIMITATIONS)

    @model_validator(mode="before")
    @classmethod
    def normalize_objective_alias(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        if "goal" not in data and "objective" in data:
            data["goal"] = data.pop("objective")
        # Early analyzer payloads exposed target hints at the goal row level.
        # Fold those hints into the typed expectation rather than permitting
        # an unbounded compatibility escape hatch on this strict model.
        if "expected" not in data and "expectation" not in data:
            expected_keys = {"direction", "target_value", "tolerance", "unit"}
            if any(key in data for key in expected_keys):
                expected: dict[str, object] = {
                    "metric": data.get("goal", "goal"),
                    "state": data.get("state", "proxy_evaluable"),
                }
                for key in expected_keys:
                    if key in data:
                        expected[key] = data[key]
                data["expected"] = expected
        for key in ("direction", "target_value", "tolerance", "unit"):
            data.pop(key, None)
        if "classification" not in data and "state" in data:
            data["classification"] = data["state"]
        return data


class ReviewRangeIssue(CreationReviewModel):
    """A bounded gap or overlap in a section map."""

    kind: Literal["gap", "overlap"]
    start_seconds: float = Field(ge=0.0)
    end_seconds: float = Field(ge=0.0)
    section_ids: tuple[Identifier, ...] = Field(default=(), max_length=8)
    explanation: str = Field(min_length=1, max_length=MAX_REVIEW_TEXT)

    @model_validator(mode="after")
    def validate_range(self) -> "ReviewRangeIssue":
        if self.end_seconds < self.start_seconds:
            raise ValueError("range issue end must not precede its start")
        return self


class TempoChange(CreationReviewModel):
    """A documented tempo change accepted by the section mapper.

    ``start_bar`` may be fractional because a transport checkpoint can place
    an automation change inside a bar; it remains one-based and bounded.
    """

    start_bar: float = Field(
        ge=1.0,
        le=1_000_000.0,
        validation_alias=AliasChoices("start_bar", "bar", "bar_number"),
    )
    tempo_bpm: float = Field(
        gt=0.0,
        le=522.0,
        validation_alias=AliasChoices("tempo_bpm", "bpm", "tempo"),
    )


class ReviewSectionRangeInput(CreationReviewModel):
    """One explicit caller section range before timeline conversion.

    A connected AI may provide bars, seconds, or both.  Complete pairs are
    required so the section mapper never invents the missing boundary.
    """

    section_id: Identifier
    name: ShortText | None = None
    start_bar: int | None = Field(default=None, ge=1, le=1_000_000)
    end_bar: int | None = Field(default=None, ge=1, le=1_000_000)
    start_seconds: float | None = Field(default=None, ge=0.0)
    end_seconds: float | None = Field(default=None, gt=0.0)
    source: Literal["user_supplied"] = "user_supplied"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    pattern_ids: tuple[Identifier, ...] = Field(default=(), max_length=512)
    palette_roles: tuple[RoleIdentifier, ...] = Field(
        default=(), max_length=MAX_REVIEW_ROLE
    )
    expected_function: str | None = Field(
        default=None, max_length=MAX_REVIEW_SHORT_TEXT
    )
    energy_intent: str | None = Field(
        default=None, max_length=MAX_REVIEW_SHORT_TEXT
    )

    @model_validator(mode="after")
    def validate_boundaries(self) -> "ReviewSectionRangeInput":
        bar_values = (self.start_bar, self.end_bar)
        second_values = (self.start_seconds, self.end_seconds)
        if any(item is not None for item in bar_values) and not all(
            item is not None for item in bar_values
        ):
            raise ValueError("section bar ranges need both start_bar and end_bar")
        if any(item is not None for item in second_values) and not all(
            item is not None for item in second_values
        ):
            raise ValueError(
                "section time ranges need both start_seconds and end_seconds"
            )
        if not all(item is not None for item in bar_values) and not all(
            item is not None for item in second_values
        ):
            raise ValueError("section ranges need a complete bar or time range")
        if self.start_bar is not None and self.end_bar is not None:
            if self.end_bar <= self.start_bar:
                raise ValueError("section end_bar must be after start_bar")
        if self.start_seconds is not None and self.end_seconds is not None:
            if self.end_seconds <= self.start_seconds:
                raise ValueError("section end_seconds must be after start_seconds")
        return self


class ReviewReferenceSectionWindow(CreationReviewModel):
    """An explicit time window on one side of a reference comparison."""

    section_id: Identifier
    start_seconds: float = Field(ge=0.0)
    end_seconds: float = Field(gt=0.0)

    @model_validator(mode="after")
    def validate_window(self) -> "ReviewReferenceSectionWindow":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("reference section end_seconds must be after start_seconds")
        return self


class ReviewReferenceSectionPair(CreationReviewModel):
    """Two explicit aligned-role windows selected for directional comparison."""

    reference: ReviewReferenceSectionWindow
    candidate: ReviewReferenceSectionWindow


class ReviewPreserveRules(CreationReviewModel):
    """Original preserve intent copied into a Review Session."""

    tempo: StrictBool = False
    note_content: StrictBool = False
    arrangement: StrictBool = False
    mixer_state: StrictBool = False
    pattern_identity: StrictBool = False
    sound_palette: StrictBool = False
    sound_roles: tuple[RoleIdentifier, ...] = Field(default=(), max_length=MAX_REVIEW_ROLE)
    named_elements: tuple[ShortText, ...] = Field(default=(), max_length=128)
    targets: tuple[Identifier, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def validate_unique(self) -> "ReviewPreserveRules":
        if len({item.casefold() for item in self.sound_roles}) != len(self.sound_roles):
            raise ValueError("preserved sound roles must be unique")
        if len({item.casefold() for item in self.named_elements}) != len(self.named_elements):
            raise ValueError("preserved elements must be unique")
        return self


class AcceptedElementLock(CreationReviewModel):
    """One explicit lock that a later RevisionPlan must respect."""

    lock_id: Identifier
    scope: Literal["overall", "section", "role", "assignment", "composition", "processing", "arrangement"]
    section_id: Identifier | None = None
    role_id: RoleIdentifier | None = None
    target_id: Identifier | None = None
    lock_types: tuple[
        Literal[
            "sound_assignment",
            "note_content",
            "rhythm",
            "register",
            "processing",
            "level",
            "section_placement",
            "role_identity",
        ],
        ...,
    ] = Field(default=(), max_length=8)
    directive: str = Field(min_length=1, max_length=MAX_REVIEW_TEXT)
    explicit: StrictBool = True
    released: StrictBool = False
    # These optional provenance fields keep the compact compatibility lock
    # useful to older revision contracts while preserving who granted and
    # later released the lock.  ``build_feedback_locks`` binds them to the
    # enclosing CreationFeedback record; callers constructing a lock for a
    # standalone RevisionRequest remain source-compatible when omitted.
    feedback_id: Identifier | None = None
    source: FeedbackSource | None = None
    released_by_feedback_id: Identifier | None = None
    released_by_source: FeedbackSource | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> "AcceptedElementLock":
        if self.scope == "section" and self.section_id is None:
            raise ValueError("section locks require section_id")
        if self.scope in {"role", "assignment"} and self.role_id is None:
            raise ValueError("role/assignment locks require role_id")
        if not self.lock_types:
            raise ValueError("an accepted element lock needs at least one lock type")
        return self


class FeedbackDirective(CreationReviewModel):
    """One already-interpreted producer directive.

    Natural-language interpretation belongs to the connected AI.  This record
    carries only the bounded, structured result that PostFader may use when
    producing findings or independent accepted-element locks.
    """

    directive_id: Identifier
    text: BoundedText
    target: LockTarget = "overall"
    section_id: Identifier | None = None
    role_id: RoleIdentifier | None = None
    assignment_id: Identifier | None = None
    lock_kinds: tuple[LockKind, ...] = Field(default=(), max_length=8)
    release_lock_kinds: tuple[LockKind, ...] = Field(default=(), max_length=8)
    preserve: StrictBool = False
    replacement: StrictBool = False
    source: FeedbackSource = "user_explicit"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_target(self) -> "FeedbackDirective":
        if self.target == "section" and self.section_id is None:
            raise ValueError("section feedback directives require section_id")
        if self.target in {"role", "palette_assignment", "composition_part", "drum_role"}:
            if self.role_id is None and self.assignment_id is None:
                raise ValueError("role-scoped feedback directives require role_id or assignment_id")
        if self.target == "palette_assignment" and self.assignment_id is None:
            raise ValueError("palette_assignment directives require assignment_id")
        if len(set(self.lock_kinds)) != len(self.lock_kinds):
            raise ValueError("lock_kinds must not contain duplicates")
        if len(set(self.release_lock_kinds)) != len(self.release_lock_kinds):
            raise ValueError("release_lock_kinds must not contain duplicates")
        if set(self.lock_kinds).intersection(self.release_lock_kinds):
            raise ValueError("a directive cannot lock and release the same kind")
        return self


class FeedbackLock(CreationReviewModel):
    """One independent, explicit feedback lock.

    This mirrors the public feedback module's lock shape so persisted and
    model-only callers have one strict contract for lock state.  Each kind is
    independent: preserving note content does not implicitly preserve a sound
    assignment, processing, or level.
    """

    schema_version: Literal["1.0"] = CREATION_REVIEW_SCHEMA_VERSION
    lock_id: Identifier
    review_session_id: Identifier
    feedback_id: Identifier
    source: FeedbackSource
    target: LockTarget
    section_id: Identifier | None = None
    role_id: RoleIdentifier | None = None
    assignment_id: Identifier | None = None
    kind: LockKind
    directive: BoundedText
    active: StrictBool = True
    released: StrictBool = False
    released_by_feedback_id: Identifier | None = None
    created_at: datetime = Field(default_factory=_now)

    @property
    def lock_kind(self) -> LockKind:
        return self.kind

    @model_validator(mode="after")
    def validate_state(self) -> "FeedbackLock":
        if self.released and self.active:
            raise ValueError("released feedback locks cannot remain active")
        if self.released and self.released_by_feedback_id is None:
            raise ValueError("released feedback locks require a releasing feedback ID")
        if self.target == "section" and self.section_id is None:
            raise ValueError("section locks require section_id")
        if self.target in {"role", "palette_assignment", "composition_part", "drum_role"}:
            if self.role_id is None and self.assignment_id is None:
                raise ValueError("role-scoped locks require role_id or assignment_id")
        return self


class SectionFeedback(CreationReviewModel):
    feedback_id: Identifier
    section_id: Identifier
    verdict: FeedbackVerdict = "neutral"
    note: str | None = Field(default=None, max_length=MAX_REVIEW_TEXT)
    desired_descriptors: tuple[ShortText, ...] = Field(default=(), max_length=32)
    undesired_descriptors: tuple[ShortText, ...] = Field(default=(), max_length=32)
    locks: tuple[AcceptedElementLock, ...] = Field(default=(), max_length=16)


class RoleFeedback(CreationReviewModel):
    feedback_id: Identifier
    role_id: RoleIdentifier
    verdict: FeedbackVerdict = "neutral"
    note: str | None = Field(default=None, max_length=MAX_REVIEW_TEXT)
    desired_descriptors: tuple[ShortText, ...] = Field(default=(), max_length=32)
    undesired_descriptors: tuple[ShortText, ...] = Field(default=(), max_length=32)
    keep_sound: StrictBool = False
    keep_notes: StrictBool = False
    keep_rhythm: StrictBool = False
    keep_register: StrictBool = False
    locks: tuple[AcceptedElementLock, ...] = Field(default=(), max_length=16)


class PaletteFeedback(CreationReviewModel):
    feedback_id: Identifier
    role_id: RoleIdentifier | None = None
    assignment_id: Identifier | None = None
    verdict: FeedbackVerdict = "neutral"
    note: str | None = Field(default=None, max_length=MAX_REVIEW_TEXT)
    keep_assignment: StrictBool = False
    replacement_requested: StrictBool = False
    locks: tuple[AcceptedElementLock, ...] = Field(default=(), max_length=16)


class ProcessingFeedback(CreationReviewModel):
    feedback_id: Identifier
    goal: ShortText
    verdict: FeedbackVerdict = "neutral"
    note: str | None = Field(default=None, max_length=MAX_REVIEW_TEXT)
    locks: tuple[AcceptedElementLock, ...] = Field(default=(), max_length=16)


class CreationFeedback(CreationReviewModel):
    """Explicit or derived feedback; silence never creates a lock."""

    feedback_id: Identifier
    review_session_id: Identifier | None = None
    source: FeedbackSource = "user_explicit"
    overall_verdict: FeedbackVerdict = "neutral"
    overall_note: str | None = Field(default=None, max_length=MAX_REVIEW_TEXT)
    section_feedback: tuple[FeedbackDirective | SectionFeedback | object, ...] = Field(
        default=(), max_length=MAX_REVIEW_FEEDBACK
    )
    role_feedback: tuple[FeedbackDirective | RoleFeedback | object, ...] = Field(
        default=(), max_length=MAX_REVIEW_FEEDBACK
    )
    palette_feedback: tuple[FeedbackDirective | PaletteFeedback | object, ...] = Field(
        default=(), max_length=MAX_REVIEW_FEEDBACK
    )
    arrangement_feedback: tuple[FeedbackDirective | ShortText | object, ...] = Field(
        default=(), max_length=MAX_REVIEW_FEEDBACK
    )
    processing_feedback: tuple[FeedbackDirective | ProcessingFeedback | object, ...] = Field(
        default=(), max_length=MAX_REVIEW_FEEDBACK
    )
    desired_descriptors: tuple[ShortText, ...] = Field(default=(), max_length=64)
    undesired_descriptors: tuple[ShortText, ...] = Field(default=(), max_length=64)
    preserve_directives: tuple[FeedbackDirective | ShortText | object, ...] = Field(
        default=(), max_length=MAX_REVIEW_FEEDBACK
    )
    replacement_directives: tuple[FeedbackDirective | ShortText | object, ...] = Field(
        default=(), max_length=MAX_REVIEW_FEEDBACK
    )
    accepted_locks: tuple[AcceptedElementLock, ...] = Field(default=(), max_length=MAX_REVIEW_FEEDBACK)
    approval_level: ApprovalLevel = "none"
    bounded_note: str | None = Field(default=None, max_length=MAX_REVIEW_TEXT)
    persist: StrictBool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_directive_models(cls, value: object) -> object:
        """Keep the standalone feedback helper's frozen directives intact.

        Older integrations import ``FeedbackDirective`` from
        ``creation_review.feedback``.  That class is intentionally separate
        to avoid a module cycle, but it is frozen and structurally identical.
        Preserve those instances so its lock compiler can inspect the
        structured lock kinds; mappings still flow through the canonical
        models below.  Other arbitrary objects are rejected before the
        ``object`` compatibility branch can accept them.
        """

        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        directive_fields = {
            "section_feedback",
            "role_feedback",
            "palette_feedback",
            "arrangement_feedback",
            "processing_feedback",
            "preserve_directives",
            "replacement_directives",
        }
        for name in directive_fields:
            rows = data.get(name)
            if rows is None:
                continue
            if isinstance(rows, (str, bytes)):
                rows = (rows,)
            elif isinstance(rows, Mapping):
                rows = (rows,)
            elif not isinstance(rows, (list, tuple)):
                raise ValueError(f"{name} must be a bounded sequence")
            normalized: list[object] = []
            for item in rows:
                if isinstance(item, BaseModel):
                    item_type = type(item)
                    if (
                        item_type.__name__ == "FeedbackDirective"
                        and item_type.__module__.endswith(".feedback")
                    ):
                        normalized.append(item)
                        continue
                    if isinstance(item, (FeedbackDirective, SectionFeedback, RoleFeedback, PaletteFeedback, ProcessingFeedback)):
                        normalized.append(item)
                        continue
                    raise ValueError(f"{name} contains an unsupported model")
                if isinstance(item, Mapping) or isinstance(item, str):
                    normalized.append(item)
                    continue
                raise ValueError(f"{name} contains an unsupported directive")
            data[name] = tuple(normalized)
        return data

    @model_validator(mode="after")
    def validate_feedback(self) -> "CreationFeedback":
        ids = [
            getattr(item, "feedback_id", getattr(item, "directive_id", None))
            for item in (
                *self.section_feedback,
                *self.role_feedback,
                *self.palette_feedback,
                *self.processing_feedback,
            )
        ]
        ids = [item for item in ids if item is not None]
        if len(ids) != len(set(ids)):
            raise ValueError("feedback IDs must be unique within a feedback record")
        desired = {item.casefold() for item in self.desired_descriptors}
        undesired = {item.casefold() for item in self.undesired_descriptors}
        if desired.intersection(undesired):
            raise ValueError("desired and undesired descriptors cannot overlap")
        return self


class ReviewEvaluationPolicy(CreationReviewModel):
    """Bounded analyzer policy supplied by the connected AI."""

    mode: Literal["default", "technical", "balanced", "focused", "custom"] = "default"
    requested_dimensions: tuple[ShortText, ...] = Field(default=(), max_length=32)
    compare_reference: StrictBool = True
    include_stems: StrictBool = True
    include_generated_content: StrictBool = True
    max_findings: int = Field(default=MAX_REVIEW_FINDINGS, ge=1, le=MAX_REVIEW_FINDINGS)

    @model_validator(mode="before")
    @classmethod
    def normalize_string_policy(cls, value: object) -> object:
        if isinstance(value, str):
            return {"mode": value}
        return value


class ReviewSessionRequest(CreationReviewModel):
    """Task-scoped request opening or continuing a Review Session."""

    schema_version: Literal["1.0"] = CREATION_REVIEW_SCHEMA_VERSION
    source_run_id: Identifier
    brief: BoundedText
    interaction_policy: ReviewInteractionPolicy = "analyze_only"
    requested_focus: tuple[ShortText, ...] = Field(default=(), max_length=32)
    section_scope: tuple[Identifier, ...] = Field(default=(), max_length=MAX_REVIEW_SECTIONS)
    role_scope: tuple[RoleIdentifier, ...] = Field(default=(), max_length=MAX_REVIEW_ROLE)
    preserve: ReviewPreserveRules = Field(default_factory=ReviewPreserveRules)
    reference_goals: tuple[ShortText, ...] = Field(default=(), max_length=32)
    reference_section_pairs: tuple[ReviewReferenceSectionPair, ...] = Field(
        default=(), max_length=MAX_REVIEW_SECTIONS
    )
    max_revision_passes: int = Field(default=3, ge=0, le=MAX_REVIEW_PASSES)
    max_revision_operations: int = Field(default=MAX_REVIEW_OPERATIONS, ge=1, le=MAX_REVIEW_OPERATIONS)
    authorized_to_modify: StrictBool = False
    persist_session: StrictBool = False
    persist_asset_paths: StrictBool = False
    project_key: str | None = Field(default=None, max_length=MAX_REVIEW_SHORT_TEXT)
    user_feedback: tuple[CreationFeedback, ...] = Field(default=(), max_length=MAX_REVIEW_FEEDBACK)
    evaluation_policy: ReviewEvaluationPolicy = Field(default_factory=ReviewEvaluationPolicy)

    @model_validator(mode="before")
    @classmethod
    def normalize_compact_feedback(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        feedback = data.get("user_feedback", ())
        if isinstance(feedback, str):
            data["user_feedback"] = (
                {"feedback_id": "feedback-1", "overall_note": feedback},
            )
        elif isinstance(feedback, Mapping):
            data["user_feedback"] = (feedback,)
        return data

    @model_validator(mode="after")
    def validate_request(self) -> "ReviewSessionRequest":
        if len({item.casefold() for item in self.section_scope}) != len(self.section_scope):
            raise ValueError("section_scope must not contain duplicates")
        if len({item.casefold() for item in self.role_scope}) != len(self.role_scope):
            raise ValueError("role_scope must not contain duplicates")
        pair_keys = [
            _canonical_digest(item.model_dump(mode="json"))
            for item in self.reference_section_pairs
        ]
        if len(pair_keys) != len(set(pair_keys)):
            raise ValueError("reference_section_pairs must not contain duplicates")
        if self.interaction_policy == "apply_once" and not self.authorized_to_modify:
            raise ValueError("apply_once requires authorized_to_modify")
        return self


class ReviewAudioAsset(CreationReviewModel):
    """Metadata for a caller-supplied audio file; audio bytes never enter a session."""

    asset_id: Identifier
    asset_kind: ReviewAssetKind
    path: str | None = Field(default=None, max_length=4096)
    display_label: ShortText
    role_id: RoleIdentifier | None = None
    section_id: Identifier | None = None
    source_run_id: Identifier | None = None
    revision_pass_id: Identifier | None = None
    expected_start_seconds: float | None = Field(default=None, ge=0.0)
    declared_offset_seconds: float | None = None
    sha256: Digest | None = None
    sample_rate_hz: int | None = Field(default=None, ge=1, le=384_000)
    channels: int | None = Field(default=None, ge=1, le=64)
    duration_seconds: float | None = Field(default=None, gt=0.0, le=MAX_REVIEW_AUDIO_SECONDS)
    file_size_bytes: int | None = Field(default=None, ge=1, le=MAX_REVIEW_ASSET_BYTES)
    format: Literal["wav", "wave", "aiff", "aif", "flac", "ogg", "oga", "mp3"] | None = None
    validation_state: AssetValidationState = "unvalidated"
    warnings: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_WARNINGS)

    @property
    def hash(self) -> str | None:
        """Compatibility spelling for callers that use ``hash`` as the identity."""

        return self.sha256

    @property
    def is_full_mix(self) -> bool:
        return self.asset_kind in {"candidate_full_mix", "before_full_mix", "after_full_mix", "reference_full_mix"}

    @model_validator(mode="after")
    def validate_asset(self) -> "ReviewAudioAsset":
        if self.declared_offset_seconds is not None and not math.isfinite(self.declared_offset_seconds):
            raise ValueError("declared_offset_seconds must be finite")
        if self.path is not None and not self.path.strip():
            raise ValueError("path must contain text when supplied")
        if self.format is None and self.path:
            suffix = self.path.rsplit(".", 1)[-1].casefold() if "." in self.path else ""
            if suffix in {"wav", "wave", "aiff", "aif", "flac", "ogg", "oga", "mp3"}:
                object.__setattr__(self, "format", suffix)  # type: ignore[misc]
        return self


class ReviewAssetSet(CreationReviewModel):
    """A bounded set of full mixes, references, stems, and section bounces."""

    asset_set_id: Identifier
    candidate_full_mix: ReviewAudioAsset | None = Field(
        default=None, validation_alias=AliasChoices("candidate_full_mix", "candidate")
    )
    before_full_mix: ReviewAudioAsset | None = Field(
        default=None, validation_alias=AliasChoices("before_full_mix", "before")
    )
    after_full_mix: ReviewAudioAsset | None = Field(
        default=None, validation_alias=AliasChoices("after_full_mix", "after")
    )
    reference: ReviewAudioAsset | None = None
    synchronized_stems: tuple[ReviewAudioAsset, ...] = Field(
        default=(), validation_alias=AliasChoices("synchronized_stems", "stems"), max_length=MAX_REVIEW_STEMS
    )
    section_bounces: tuple[ReviewAudioAsset, ...] = Field(default=(), max_length=MAX_REVIEW_SECTIONS)
    alignment_state: Literal["unknown", "aligned", "partially_aligned", "unsynchronized", "failed"] = "unknown"
    common_start_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    duration_compatible: bool | None = None
    sample_rate_compatible: bool | None = None
    asset_set_digest: Digest | None = None
    limitations: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_LIMITATIONS)

    @property
    def assets(self) -> tuple[ReviewAudioAsset, ...]:
        values: list[ReviewAudioAsset] = []
        for item in (
            self.candidate_full_mix,
            self.before_full_mix,
            self.after_full_mix,
            self.reference,
        ):
            if item is not None:
                values.append(item)
        values.extend(self.synchronized_stems)
        values.extend(self.section_bounces)
        return tuple(values)

    @property
    def digest(self) -> str:
        return self.asset_set_digest or _canonical_digest(
            {
                "asset_set_id": self.asset_set_id,
                "assets": [
                    item.model_dump(mode="json", exclude={"path", "asset_set_digest"})
                    for item in self.assets
                ],
                "alignment_state": self.alignment_state,
                "common_start_confidence": self.common_start_confidence,
                "duration_compatible": self.duration_compatible,
                "sample_rate_compatible": self.sample_rate_compatible,
            }
        )

    @model_validator(mode="after")
    def validate_set(self) -> "ReviewAssetSet":
        expected_kinds = (
            ("candidate_full_mix", self.candidate_full_mix, "candidate_full_mix"),
            ("before_full_mix", self.before_full_mix, "before_full_mix"),
            ("after_full_mix", self.after_full_mix, "after_full_mix"),
        )
        for field_name, asset, expected_kind in expected_kinds:
            if asset is not None and asset.asset_kind != expected_kind:
                raise ValueError(
                    f"{field_name} must contain an asset with asset_kind={expected_kind!r}"
                )
        full = [
            item
            for item in (self.candidate_full_mix, self.before_full_mix, self.after_full_mix)
            if item is not None
        ]
        if not full:
            raise ValueError("an asset set needs a candidate, before, or after full mix")
        ids = [item.asset_id for item in self.assets]
        if len(ids) != len(set(ids)):
            raise ValueError("asset IDs must be unique within an asset set")
        hashes = [item.sha256 for item in (self.before_full_mix, self.after_full_mix) if item and item.sha256]
        if len(hashes) == 2 and hashes[0] == hashes[1]:
            raise ValueError("before and after assets must have different hashes")
        if self.asset_set_digest is None:
            object.__setattr__(self, "asset_set_digest", self.digest)  # type: ignore[misc]
        return self


class ReviewSection(CreationReviewModel):
    section_id: Identifier
    name: ShortText
    start_bar: int = Field(ge=1, le=1_000_000)
    end_bar: int = Field(ge=1, le=1_000_000)
    start_seconds: float = Field(ge=0.0)
    end_seconds: float = Field(ge=0.0)
    source: SectionSource
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    pattern_ids: tuple[Identifier, ...] = Field(default=(), max_length=512)
    palette_roles: tuple[RoleIdentifier, ...] = Field(default=(), max_length=MAX_REVIEW_ROLE)
    expected_function: str | None = Field(default=None, max_length=MAX_REVIEW_SHORT_TEXT)
    energy_intent: str | None = Field(default=None, max_length=MAX_REVIEW_SHORT_TEXT)

    @model_validator(mode="after")
    def validate_section(self) -> "ReviewSection":
        if self.end_bar < self.start_bar:
            raise ValueError("section end_bar must not precede start_bar")
        if self.end_seconds <= self.start_seconds:
            raise ValueError("section end_seconds must be after start_seconds")
        if len({item.casefold() for item in self.pattern_ids}) != len(self.pattern_ids):
            raise ValueError("section pattern IDs must be unique")
        return self


class ReviewSectionMeasurement(CreationReviewModel):
    section_id: Identifier
    start_seconds: float = Field(ge=0.0)
    end_seconds: float = Field(gt=0.0)
    measurements: FrozenMap = Field(default_factory=lambda: FrozenMap({}))
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    sample_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    limitations: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_LIMITATIONS)

    @model_validator(mode="after")
    def validate_window(self) -> "ReviewSectionMeasurement":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("section measurement end must be after start")
        return self


class ReviewStemMeasurement(CreationReviewModel):
    asset_id: Identifier
    role_id: RoleIdentifier | None = None
    section_id: Identifier | None = None
    measurements: FrozenMap = Field(default_factory=lambda: FrozenMap({}))
    attribution_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    limitations: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_LIMITATIONS)


class ReviewSectionMap(CreationReviewModel):
    tempo_bpm: float = Field(gt=0.0, le=522.0)
    time_signature_numerator: int = Field(default=4, ge=1, le=32)
    time_signature_denominator: Literal[1, 2, 4, 8, 16, 32] = 4
    bar_to_time_basis: Literal["constant_tempo", "tempo_change_map", "declared_timestamps", "unknown"] = "constant_tempo"
    export_offset_seconds: float = Field(
        default=0.0,
        ge=-float(MAX_REVIEW_AUDIO_SECONDS),
        le=float(MAX_REVIEW_AUDIO_SECONDS),
    )
    tempo_changes: tuple[TempoChange, ...] = Field(default=(), max_length=64)
    sections: tuple[ReviewSection, ...] = Field(default=(), max_length=MAX_REVIEW_SECTIONS)
    gaps: tuple[ReviewRangeIssue, ...] = Field(default=(), max_length=MAX_REVIEW_SECTIONS)
    overlaps: tuple[ReviewRangeIssue, ...] = Field(default=(), max_length=MAX_REVIEW_SECTIONS)
    map_digest: Digest | None = None
    source_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    limitations: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_LIMITATIONS)

    @property
    def digest(self) -> str:
        return self.map_digest or _canonical_digest(
            self.model_dump(mode="json", exclude={"map_digest"})
        )

    def bar_to_seconds(self, bar: int, beat_offset: float = 0.0) -> float:
        if type(bar) is not int or bar < 1:
            raise ValueError("bar must be a positive integer")
        if (
            isinstance(beat_offset, bool)
            or not isinstance(beat_offset, (int, float))
            or not math.isfinite(float(beat_offset))
            or not 0.0 <= float(beat_offset) < float(self.time_signature_numerator)
        ):
            raise ValueError("beat_offset is outside the time-signature bound")
        # ``beat_offset`` is expressed in meter beats (the denominator's
        # note value), not quarter-note beats.  A denominator of 8 therefore
        # makes each beat half as long as a denominator of 4.  Keeping the
        # arithmetic in bars also lets tempo changes be integrated at their
        # documented one-based bar boundaries.
        target_bar = float(bar) + float(beat_offset) / float(self.time_signature_numerator)
        beats_per_bar = float(self.time_signature_numerator)
        beat_seconds_scale = 4.0 / float(self.time_signature_denominator)
        elapsed = 0.0
        cursor = 1.0
        tempo = self.tempo_bpm
        for change in self.tempo_changes:
            change_bar = float(change.start_bar)
            if change_bar <= 1.0:
                tempo = change.tempo_bpm
                continue
            if change_bar >= target_bar:
                break
            elapsed += (change_bar - cursor) * beats_per_bar * beat_seconds_scale * (60.0 / tempo)
            cursor = change_bar
            tempo = change.tempo_bpm
        if target_bar > cursor:
            elapsed += (target_bar - cursor) * beats_per_bar * beat_seconds_scale * (60.0 / tempo)
        return round(self.export_offset_seconds + elapsed, 9)

    @model_validator(mode="after")
    def validate_map(self) -> "ReviewSectionMap":
        ids = [item.section_id for item in self.sections]
        if len(ids) != len(set(ids)):
            raise ValueError("section IDs must be unique")
        for previous, current in zip(self.tempo_changes, self.tempo_changes[1:]):
            if current.start_bar <= previous.start_bar:
                raise ValueError("tempo_changes must be ordered by unique start_bar")
        if self.bar_to_time_basis == "tempo_change_map" and not self.tempo_changes:
            raise ValueError("tempo_change_map basis requires tempo_changes")
        if self.map_digest is None:
            object.__setattr__(self, "map_digest", self.digest)  # type: ignore[misc]
        return self


class EvaluationFinding(CreationReviewModel):
    finding_id: Identifier
    category: FindingCategory
    evidence_source: EvidenceSource
    severity: FindingSeverity = "medium"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    section_id: Identifier | None = None
    role_id: RoleIdentifier | None = None
    asset_id: Identifier | None = None
    measurement: ReviewMetric | FrozenMap | None = None
    expected_or_requested_target: (
        str | float | int | FrozenMap | None
    ) = Field(default=None, max_length=MAX_REVIEW_TEXT)
    explanation: BoundedText
    actionability: Actionability = "informational"
    candidate_techniques: tuple[ShortText, ...] = Field(default=(), max_length=32)
    required_additional_evidence: tuple[ShortText, ...] = Field(default=(), max_length=32)
    limitations: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_LIMITATIONS)


class ReferenceComparison(CreationReviewModel):
    comparison_id: Identifier
    reference_asset_id: Identifier
    candidate_asset_id: Identifier
    paired_section_ids: tuple[tuple[Identifier, Identifier], ...] = Field(default=(), max_length=MAX_REVIEW_SECTIONS)
    requested_dimensions: tuple[ShortText, ...] = Field(default=(), max_length=32)
    measurements: FrozenMap = Field(default_factory=lambda: FrozenMap({}))
    alignment_state: Literal["aligned", "failed", "unknown"] = "unknown"
    findings: tuple[Identifier, ...] = Field(default=(), max_length=MAX_REVIEW_FINDINGS)
    limitations: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_LIMITATIONS)


class EvaluationState(CreationReviewModel):
    state: Literal["verified", "measured", "proxy", "not_evaluated", "unknown"] = "unknown"
    summary: str = Field(min_length=1, max_length=MAX_REVIEW_TEXT)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    limitations: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_LIMITATIONS)


class CreationEvaluationReport(CreationReviewModel):
    evaluation_id: Identifier
    review_session_id: Identifier
    source_run_id: Identifier
    asset_set_digest: Digest
    section_map_digest: Digest
    analyzer_version: ShortText
    status: Literal[
        "complete",
        "partial",
        "blocked",
        "not_evaluable",
        # Compatibility spellings accepted by early review integrations.
        "evaluated",
        "completed",
        "failed",
        "unknown",
    ] = Field(
        default="complete",
        validation_alias=AliasChoices("status", "report_status"),
    )
    evaluated_at: datetime = Field(default_factory=_now)
    analysis_policy_digest: Digest | None = Field(
        default=None,
        validation_alias=AliasChoices("analysis_policy_digest", "policy_digest"),
    )
    global_measurements: FrozenMap = Field(default_factory=lambda: FrozenMap({}))
    per_section_measurements: tuple[ReviewSectionMeasurement, ...] = Field(
        default=(), validation_alias=AliasChoices("per_section_measurements", "section_measurements"), max_length=MAX_REVIEW_SECTIONS
    )
    stem_measurements: tuple[ReviewStemMeasurement, ...] = Field(default=(), max_length=MAX_REVIEW_STEMS)
    reference_comparisons: tuple[ReferenceComparison, ...] = Field(default=(), max_length=MAX_REVIEW_SECTIONS)
    masking_analysis: FrozenMap = Field(default_factory=lambda: FrozenMap({}))
    energy_contrast_analysis: FrozenMap = Field(default_factory=lambda: FrozenMap({}))
    generated_content_analysis: FrozenMap = Field(default_factory=lambda: FrozenMap({}))
    timing: FrozenMap = Field(
        default_factory=lambda: FrozenMap({}),
        validation_alias=AliasChoices("timing", "timings"),
    )
    findings: tuple[EvaluationFinding, ...] = Field(default=(), max_length=MAX_REVIEW_FINDINGS)
    top_priorities: tuple[Identifier, ...] = Field(default=(), max_length=MAX_REVIEW_PRIORITIES)
    unavailable_analyses: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_LIMITATIONS)
    goal_evaluations: tuple[GoalEvaluation, ...] = Field(
        default=(),
        validation_alias=AliasChoices(
            "goal_evaluations", "goal_results", "evaluated_goals"
        ),
        max_length=32,
    )
    technical_audio_state: EvaluationState = Field(
        default_factory=lambda: EvaluationState(state="unknown", summary="Not evaluated.")
    )
    arrangement_proxy_state: EvaluationState = Field(
        default_factory=lambda: EvaluationState(state="not_evaluated", summary="No arrangement proxy was evaluated.")
    )
    processing_review_state: EvaluationState = Field(
        default_factory=lambda: EvaluationState(state="not_evaluated", summary="No processing review was evaluated.")
    )
    audible_quality_state: EvaluationState = Field(
        default_factory=lambda: EvaluationState(state="not_evaluated", summary="Audible quality requires user judgment.")
    )
    mutations_applied: Literal[False] = False
    zero_mutations: Literal[True] = True
    warnings: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_WARNINGS)

    @model_validator(mode="after")
    def validate_report(self) -> "CreationEvaluationReport":
        ids = [item.finding_id for item in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation finding IDs must be unique")
        if any(item not in set(ids) for item in self.top_priorities):
            raise ValueError("top_priorities must reference findings in this report")
        if len({item.section_id for item in self.per_section_measurements}) != len(self.per_section_measurements):
            raise ValueError("section measurements must be unique per section")
        goals = [item.goal.casefold() for item in self.goal_evaluations]
        if len(goals) != len(set(goals)):
            raise ValueError("goal evaluations must be unique per goal")
        if self.status == "complete" and self.unavailable_analyses:
            # A report can complete its bounded pass while still lacking a
            # reference, stem, or other requested evidence.  Surface that
            # distinction without requiring every older producer of reports
            # to add a new field.
            object.__setattr__(self, "status", "partial")
        return self

    @property
    def timings(self) -> FrozenMap:
        """Compatibility plural spelling for local analysis timing data."""

        return self.timing

    @property
    def report_status(self) -> str:
        """Compatibility spelling for the bounded report lifecycle state."""

        return self.status

    @property
    def goal_results(self) -> tuple[GoalEvaluation, ...]:
        """Compatibility spelling for requested-goal evaluations."""

        return self.goal_evaluations


class RevisionOperationBase(CreationReviewModel):
    """Common traceability fields for every closed revision operation."""

    operation_id: Identifier
    after: tuple[Identifier, ...] = Field(default=(), max_length=MAX_REVIEW_OPERATIONS)
    finding_ids: tuple[Identifier, ...] = Field(default=(), max_length=MAX_REVIEW_FINDINGS)
    feedback_ids: tuple[Identifier, ...] = Field(default=(), max_length=MAX_REVIEW_FEEDBACK)
    section_id: Identifier | None = None
    role_id: RoleIdentifier | None = None
    preserves: tuple[ShortText, ...] = Field(default=(), max_length=16)
    expected_measurable_movements: tuple[MetricExpectation, ...] = Field(default=(), max_length=16)
    subjective_objective: str | None = Field(default=None, max_length=MAX_REVIEW_TEXT)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    fallback_behavior: str | None = Field(default=None, max_length=MAX_REVIEW_TEXT)
    verification_method: ShortText = "existing_verified_writer_readback"
    source_sequence_digest: Digest | None = None
    source_palette_digest: Digest | None = None
    effect_control_id: Identifier | None = None
    parameters: FrozenMap = Field(default_factory=lambda: FrozenMap({}))

    @model_validator(mode="after")
    def validate_traceability(self) -> "RevisionOperationBase":
        if (
            not self.finding_ids
            and not self.feedback_ids
            and getattr(self, "operation", "") != "record_feedback_lock"
        ):
            raise ValueError("revision operations must trace to a finding or feedback")
        if len(set(self.after)) != len(self.after):
            raise ValueError("revision dependencies must be unique")
        return self


class TransformGeneratedSequenceOperation(RevisionOperationBase):
    operation: Literal["transform_generated_sequence"] = "transform_generated_sequence"


class RegenerateRoleSequenceOperation(RevisionOperationBase):
    operation: Literal["regenerate_role_sequence"] = "regenerate_role_sequence"


class CreateSectionNoteVariationOperation(RevisionOperationBase):
    operation: Literal["create_section_note_variation"] = "create_section_note_variation"


class ChangeSoundAssignmentOperation(RevisionOperationBase):
    operation: Literal["change_sound_assignment"] = "change_sound_assignment"


class CreateSoundPaletteVariationOperation(RevisionOperationBase):
    operation: Literal["create_sound_palette_variation"] = "create_sound_palette_variation"


class ChangeDrumKitOperation(RevisionOperationBase):
    operation: Literal["change_drum_kit"] = "change_drum_kit"


class ChangeDrumRoleMappingOperation(RevisionOperationBase):
    operation: Literal["change_drum_role_mapping"] = "change_drum_role_mapping"


class AdjustRoleLevelOperation(RevisionOperationBase):
    operation: Literal["adjust_role_level"] = "adjust_role_level"


class AdjustChannelMixOperation(RevisionOperationBase):
    operation: Literal["adjust_channel_mix"] = "adjust_channel_mix"


class ApplySemanticProcessingOperation(RevisionOperationBase):
    operation: Literal["apply_semantic_processing"] = "apply_semantic_processing"


class ReplaceProcessingPlanOperation(RevisionOperationBase):
    operation: Literal["replace_processing_plan"] = "replace_processing_plan"


class ChangeSectionDensityOperation(RevisionOperationBase):
    operation: Literal["change_section_density"] = "change_section_density"


class ChangeSectionRegisterOperation(RevisionOperationBase):
    operation: Literal["change_section_register"] = "change_section_register"


class ChangeSectionVoicingOperation(RevisionOperationBase):
    operation: Literal["change_section_voicing"] = "change_section_voicing"


class ChangeSectionRhythmOperation(RevisionOperationBase):
    operation: Literal["change_section_rhythm"] = "change_section_rhythm"


class ChangeSectionVelocityOperation(RevisionOperationBase):
    operation: Literal["change_section_velocity"] = "change_section_velocity"


class ChangeSectionArticulationOperation(RevisionOperationBase):
    operation: Literal["change_section_articulation"] = "change_section_articulation"


class AddSupportingLayerOperation(RevisionOperationBase):
    operation: Literal["add_supporting_layer"] = "add_supporting_layer"


class RemoveGeneratedLayerOperation(RevisionOperationBase):
    operation: Literal["remove_generated_layer"] = "remove_generated_layer"


class UpdateSectionMarkersOperation(RevisionOperationBase):
    operation: Literal["update_section_markers"] = "update_section_markers"


class CreatePlaylistHandoffDeltaOperation(RevisionOperationBase):
    operation: Literal["create_playlist_handoff_delta"] = "create_playlist_handoff_delta"
    placements: tuple["PlaylistPlacement", ...] = Field(
        min_length=1,
        max_length=MAX_REVIEW_SECTIONS,
    )


class RecordFeedbackLockOperation(RevisionOperationBase):
    operation: Literal["record_feedback_lock"] = "record_feedback_lock"


RevisionOperation: TypeAlias = (
    TransformGeneratedSequenceOperation
    | RegenerateRoleSequenceOperation
    | CreateSectionNoteVariationOperation
    | ChangeSoundAssignmentOperation
    | CreateSoundPaletteVariationOperation
    | ChangeDrumKitOperation
    | ChangeDrumRoleMappingOperation
    | AdjustRoleLevelOperation
    | AdjustChannelMixOperation
    | ApplySemanticProcessingOperation
    | ReplaceProcessingPlanOperation
    | ChangeSectionDensityOperation
    | ChangeSectionRegisterOperation
    | ChangeSectionVoicingOperation
    | ChangeSectionRhythmOperation
    | ChangeSectionVelocityOperation
    | ChangeSectionArticulationOperation
    | AddSupportingLayerOperation
    | RemoveGeneratedLayerOperation
    | UpdateSectionMarkersOperation
    | CreatePlaylistHandoffDeltaOperation
    | RecordFeedbackLockOperation
)


class RevisionRequest(CreationReviewModel):
    source_evaluation_id: Identifier
    source_run_id: Identifier
    requested_objective: BoundedText
    section_scope: tuple[Identifier, ...] = Field(default=(), max_length=MAX_REVIEW_SECTIONS)
    role_scope: tuple[RoleIdentifier, ...] = Field(default=(), max_length=MAX_REVIEW_ROLE)
    allowed_changes: tuple[RevisionOperationKind, ...] = Field(default=(), max_length=MAX_REVIEW_OPERATIONS)
    preserved_elements: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_FEEDBACK)
    accepted_element_locks: tuple[AcceptedElementLock, ...] = Field(default=(), max_length=MAX_REVIEW_FEEDBACK)
    rejected_assignments: tuple[Identifier, ...] = Field(default=(), max_length=MAX_REVIEW_FEEDBACK)
    maximum_changed_roles: int = Field(default=MAX_REVIEW_ROLE, ge=0, le=MAX_REVIEW_ROLE)
    maximum_changed_sections: int = Field(default=MAX_REVIEW_SECTIONS, ge=0, le=MAX_REVIEW_SECTIONS)
    maximum_operations: int = Field(default=MAX_REVIEW_OPERATIONS, ge=1, le=MAX_REVIEW_OPERATIONS)
    maximum_risk_level: RiskLevel = "medium"
    revision_strength: RevisionStrength = "balanced"
    authorized_to_modify: StrictBool = False
    regenerate_versus_transform: RegeneratePreference = "prefer_transform"
    processing_policy: ProcessingPolicy = "explicit_only"
    manual_handoff_allowance: StrictBool = True

    @property
    def max_changed_roles(self) -> int:
        return self.maximum_changed_roles

    @property
    def max_changed_sections(self) -> int:
        return self.maximum_changed_sections

    @property
    def max_operations(self) -> int:
        return self.maximum_operations

    @model_validator(mode="after")
    def validate_request(self) -> "RevisionRequest":
        for name, values in (
            ("section_scope", self.section_scope),
            ("role_scope", self.role_scope),
            ("allowed_changes", self.allowed_changes),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must not contain duplicates")
        return self


class RevisionPlan(CreationReviewModel):
    revision_plan_id: Identifier
    review_session_id: Identifier
    source_evaluation_id: Identifier
    source_run_id: Identifier
    revision_request_digest: Digest | None = None
    plan_digest: Digest | None = None
    operations: tuple[RevisionOperation, ...] = Field(
        default=(), validation_alias=AliasChoices("operations", "ordered_operations"), max_length=MAX_REVIEW_OPERATIONS
    )
    targeted_findings: tuple[Identifier, ...] = Field(default=(), max_length=MAX_REVIEW_FINDINGS)
    protected_elements: tuple[AcceptedElementLock, ...] = Field(default=(), max_length=MAX_REVIEW_FEEDBACK)
    expected_objectives: tuple[MetricExpectation, ...] = Field(default=(), max_length=32)
    expected_measurable_movements: tuple[MetricExpectation, ...] = Field(
        default=(), max_length=32
    )
    subjective_objectives: tuple[ShortText, ...] = Field(default=(), max_length=32)
    manual_actions: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_LIMITATIONS)
    blockers: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_LIMITATIONS)
    warnings: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_WARNINGS)
    mutations_applied: StrictBool = False

    @property
    def ordered_operations(self) -> tuple[RevisionOperation, ...]:
        return self.operations

    @property
    def expected_movements(self) -> tuple[MetricExpectation, ...]:
        return self.expected_measurable_movements or self.expected_objectives

    @property
    def digest(self) -> str:
        return self.plan_digest or _canonical_digest(
            self.model_dump(mode="json", exclude={"plan_digest", "mutations_applied"})
        )

    @model_validator(mode="before")
    @classmethod
    def normalize_operation_union(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        operations = data.get("operations", data.get("ordered_operations", ()))
        # A discriminated union is used for JSON round-trips; direct callers
        # may still provide a generic mapping for an operation.
        normalized: list[object] = []
        operation_types: dict[str, type[RevisionOperationBase]] = {
            item.model_fields["operation"].default: item
            for item in (
                TransformGeneratedSequenceOperation,
                RegenerateRoleSequenceOperation,
                CreateSectionNoteVariationOperation,
                ChangeSoundAssignmentOperation,
                CreateSoundPaletteVariationOperation,
                ChangeDrumKitOperation,
                ChangeDrumRoleMappingOperation,
                AdjustRoleLevelOperation,
                AdjustChannelMixOperation,
                ApplySemanticProcessingOperation,
                ReplaceProcessingPlanOperation,
                ChangeSectionDensityOperation,
                ChangeSectionRegisterOperation,
                ChangeSectionVoicingOperation,
                ChangeSectionRhythmOperation,
                ChangeSectionVelocityOperation,
                ChangeSectionArticulationOperation,
                AddSupportingLayerOperation,
                RemoveGeneratedLayerOperation,
                UpdateSectionMarkersOperation,
                CreatePlaylistHandoffDeltaOperation,
                RecordFeedbackLockOperation,
            )
        }
        for item in operations:
            if isinstance(item, Mapping):
                operation = item.get("operation")
                concrete = operation_types.get(operation if isinstance(operation, str) else "")
                normalized.append(item if concrete is None else concrete(**item))
            else:
                normalized.append(item)
        data["operations"] = tuple(normalized)
        if data.get("plan_digest") is None:
            data.pop("plan_digest", None)
        return data

    @model_validator(mode="after")
    def validate_plan(self) -> "RevisionPlan":
        operation_ids = [item.operation_id for item in self.operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("revision operation IDs must be unique")
        prior: set[str] = set()
        for operation in self.operations:
            if any(reference not in prior for reference in operation.after):
                raise ValueError("revision operation dependencies must point backward")
            prior.add(operation.operation_id)
        if self.expected_objectives and not self.expected_measurable_movements:
            object.__setattr__(
                self, "expected_measurable_movements", self.expected_objectives
            )
        elif self.expected_measurable_movements and not self.expected_objectives:
            object.__setattr__(
                self, "expected_objectives", self.expected_measurable_movements
            )
        if self.plan_digest is None:
            object.__setattr__(self, "plan_digest", self.digest)  # type: ignore[misc]
        return self


class RevisionOperationReceipt(CreationReviewModel):
    operation_id: Identifier
    operation: RevisionOperationKind
    status: Literal["planned", "verified", "unverified", "error_unknown", "skipped"]
    mutating: StrictBool = True
    outcome_known: StrictBool = False
    verified: StrictBool = False
    finding_ids: tuple[Identifier, ...] = Field(default=(), max_length=MAX_REVIEW_FINDINGS)
    error: str | None = Field(default=None, max_length=MAX_REVIEW_TEXT)
    warnings: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_WARNINGS)
    source_receipts_unchanged: StrictBool = True
    automatic_replay_attempted: Literal[False] = False
    rollback_attempted: Literal[False] = False
    project_saved: Literal[False] = False


class ReviewGeneratedOutput(CreationReviewModel):
    output_id: Identifier
    output_kind: Literal[
        "note_sequence",
        "sound_palette",
        "palette_assignment",
        "processing_plan",
        "pattern",
        "handoff",
    ]
    role_id: RoleIdentifier | None = None
    section_id: Identifier | None = None
    digest: Digest | None = None
    metadata: FrozenMap = Field(default_factory=lambda: FrozenMap({}))


class ReviewRoleAssignment(CreationReviewModel):
    """Bounded accepted role/sound assignment retained for delivery review."""

    assignment_id: Identifier | None = None
    role_id: RoleIdentifier = Field(
        validation_alias=AliasChoices("role_id", "role")
    )
    target_id: Identifier | None = None
    target_name: ShortText | None = None
    product_id: Identifier | None = None
    product_name: ShortText | None = None
    preset_name: ShortText | None = Field(
        default=None,
        validation_alias=AliasChoices("preset_name", "selected_preset"),
    )
    section_scope: tuple[Identifier, ...] = Field(
        default=(),
        validation_alias=AliasChoices("section_scope", "sections"),
        max_length=MAX_REVIEW_SECTIONS,
    )
    assignment_digest: Digest | None = None
    metadata: FrozenMap = Field(default_factory=lambda: FrozenMap({}))

    @model_validator(mode="after")
    def fill_assignment_identity(self) -> "ReviewRoleAssignment":
        if self.assignment_id is None:
            object.__setattr__(
                self,
                "assignment_id",
                "assignment-"
                + _canonical_digest(
                    {
                        "role_id": self.role_id,
                        "target_id": self.target_id,
                        "product_id": self.product_id,
                        "preset_name": self.preset_name,
                        "section_scope": self.section_scope,
                    }
                )[:24],
            )
        if self.assignment_digest is None:
            object.__setattr__(
                self,
                "assignment_digest",
                _canonical_digest(
                    self.model_dump(mode="json", exclude={"assignment_digest"})
                ),
            )
        return self


class ManualHandoff(CreationReviewModel):
    action_id: Identifier
    instruction: BoundedText
    status: Literal["required", "pending", "confirmed", "not_verifiable", "completed"] = "required"
    section_id: Identifier | None = None
    pattern_id: Identifier | None = None
    evidence: tuple[ShortText, ...] = Field(default=(), max_length=16)


class RevisionPass(CreationReviewModel):
    revision_pass_id: Identifier
    # A pass is always attributable to its Review Session, even when an older
    # executor does not supply the field explicitly.
    review_session_id: Identifier = "review-session-unknown"
    source_evaluation_id: Identifier
    revision_plan_id: Identifier
    source_run_id: Identifier
    status: Literal["created", "preflight", "revising", "completed", "awaiting_rebounce", "blocked", "stopped"]
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
    source_production_run_id: Identifier | None = None
    continuation_run_id: Identifier | None = None
    # The post-pass checkpoint is retained independently of the source run's
    # in-memory registry record.  It lets a later Review Session turn verify
    # continuity even when the Production Run has been evicted or restarted.
    session_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{32}$",
        validation_alias=AliasChoices(
            "session_fingerprint", "write_session_fingerprint"
        ),
    )
    project_state_digest: Digest | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "project_state_digest", "post_project_state_digest"
        ),
    )
    readiness_preflight_count: int = Field(
        default=1,
        ge=1,
        le=1,
        validation_alias=AliasChoices("readiness_preflight_count", "preflight_count"),
    )
    preflight_result: FrozenMap = Field(default_factory=lambda: FrozenMap({}))
    authorization_count: int = Field(default=0, ge=0, le=1)
    write_mode_enable_count: int = Field(default=0, ge=0, le=1)
    write_mode_disable_count: int = Field(default=0, ge=0, le=1)
    shutdown_verified: StrictBool | None = Field(
        default=None,
        validation_alias=AliasChoices("shutdown_verified", "write_mode_shutdown_verified"),
    )
    operation_receipts: tuple[RevisionOperationReceipt, ...] = Field(default=(), max_length=MAX_REVIEW_OPERATIONS)
    generated_outputs: tuple[ReviewGeneratedOutput, ...] = Field(default=(), max_length=MAX_REVIEW_OPERATIONS)
    affected_roles: tuple[RoleIdentifier, ...] = Field(default=(), max_length=MAX_REVIEW_ROLE)
    affected_sections: tuple[Identifier, ...] = Field(default=(), max_length=MAX_REVIEW_SECTIONS)
    retained_anchors: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_FEEDBACK)
    manual_handoffs: tuple[ManualHandoff, ...] = Field(default=(), max_length=MAX_REVIEW_LIMITATIONS)
    technical_outcome: EvaluationState | None = None
    arrangement_outcome: EvaluationState | None = None
    processing_outcome: EvaluationState | None = None
    audible_quality_outcome: EvaluationState | None = None
    timing: FrozenMap = Field(default_factory=lambda: FrozenMap({}))
    after_bounce_state: Literal["not_requested", "awaiting", "attached", "compared"] = "not_requested"
    warnings: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_WARNINGS)
    blockers: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_LIMITATIONS)
    automatic_replay_attempted: Literal[False] = False
    rollback_attempted: Literal[False] = Field(
        default=False, validation_alias=AliasChoices("rollback_attempted", "rollback")
    )
    project_saved: Literal[False] = False

    @property
    def pass_id(self) -> str:
        return self.revision_pass_id

    @property
    def preflight_count(self) -> int:
        return self.readiness_preflight_count

    @property
    def rollback(self) -> Literal[False]:
        return self.rollback_attempted


class AlignmentResult(CreationReviewModel):
    state: Literal["aligned", "duration_mismatch", "offset_mismatch", "channel_mismatch", "failed", "unknown"] = "unknown"
    offset_seconds: float | None = None
    duration_delta_seconds: float | None = None
    channel_match: bool | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    explanation: str | None = Field(default=None, max_length=MAX_REVIEW_TEXT)


class SectionDelta(CreationReviewModel):
    section_id: Identifier
    deltas: FrozenMap = Field(default_factory=lambda: FrozenMap({}))
    improvements: tuple[ShortText, ...] = Field(default=(), max_length=32)
    regressions: tuple[ShortText, ...] = Field(default=(), max_length=32)
    unknown: tuple[ShortText, ...] = Field(default=(), max_length=32)


class ObjectiveResult(CreationReviewModel):
    objective: ShortText
    state: Literal[
        "moved_toward_target",
        "moved_away_from_target",
        "unchanged",
        "not_measurable",
        "insufficient_evidence",
    ]
    expected_movement: MetricExpectation | None = None
    observed_delta: ReviewMetric | None = None
    explanation: str | None = Field(default=None, max_length=MAX_REVIEW_TEXT)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_state(cls, value: object) -> object:
        """Accept pre-contract names while exposing only canonical states."""

        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        aliases = {
            "improved": "moved_toward_target",
            "regressed": "moved_away_from_target",
            "unknown": "insufficient_evidence",
        }
        state = data.get("state")
        if state in aliases:
            data["state"] = aliases[state]
        return data


class RevisionComparison(CreationReviewModel):
    comparison_id: Identifier
    before_asset: ReviewAudioAsset
    after_asset: ReviewAudioAsset
    alignment_result: AlignmentResult
    global_deltas: FrozenMap = Field(default_factory=lambda: FrozenMap({}))
    section_deltas: tuple[SectionDelta, ...] = Field(default=(), max_length=MAX_REVIEW_SECTIONS)
    stem_deltas: tuple[SectionDelta, ...] = Field(default=(), max_length=MAX_REVIEW_STEMS)
    expected_objective_results: tuple[ObjectiveResult, ...] = Field(default=(), max_length=32)
    regressions: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_FINDINGS)
    improvements: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_FINDINGS)
    unchanged_metrics: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_FINDINGS)
    unknown_metrics: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_FINDINGS)
    technical_conclusion: TechnicalConclusion = "unknown"
    user_approval_state: UserApprovalState = "not_requested"
    next_recommendation: ShortText | None = None
    timing: FrozenMap = Field(
        default_factory=lambda: FrozenMap({}),
        validation_alias=AliasChoices("timing", "timings"),
    )
    mutations_applied: Literal[False] = False
    zero_mutations: Literal[True] = True
    warnings: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_WARNINGS)

    @model_validator(mode="after")
    def validate_different_assets(self) -> "RevisionComparison":
        if self.before_asset.sha256 and self.after_asset.sha256 and self.before_asset.sha256 == self.after_asset.sha256:
            raise ValueError("before and after assets must have different hashes")
        return self

    @property
    def timings(self) -> FrozenMap:
        return self.timing


class PlaylistPlacement(CreationReviewModel):
    """One exact intended Playlist placement for a manual handoff.

    The older ``pattern_id``/``playlist_track`` spellings remain accepted as
    input and exposed as read-only properties.  The canonical fields mirror
    the handoff manifest so a user can place or replace one item without
    guessing.
    """

    handoff_item_id: Identifier | None = None
    pattern_number: int | None = Field(default=None, ge=1, le=999)
    pattern_name: ShortText | None = None
    source_operation_id: Identifier | None = None
    pattern_id: Identifier | None = None
    section_id: Identifier | None = None
    intended_playlist_track_number: int | None = Field(
        default=None,
        ge=1,
        validation_alias=AliasChoices(
            "intended_playlist_track_number", "playlist_track"
        ),
    )
    intended_playlist_track_name: ShortText | None = None
    start_bar: int = Field(ge=1)
    end_bar: int = Field(ge=1)
    length_bars: int | None = Field(default=None, ge=0)
    layer_order: int = Field(default=1, ge=1, le=4096)
    repeat_count: int = Field(default=1, ge=1, le=4096)
    expected_mute_state: Literal["muted", "unmuted", "unknown"] = "unknown"
    dependency: ShortText | None = None
    replacement_vs_addition: Literal["replacement", "addition"] = "addition"
    completed_state: Literal[
        "not_started", "pending", "completed", "not_verifiable", "unknown"
    ] = "not_started"
    user_confirmed_state: Literal[
        "not_requested", "pending", "confirmed", "rejected", "unknown"
    ] = "not_requested"
    notes: BoundedText | None = None
    # Compatibility status used by early callers.  New handoff manifests use
    # completed_state/user_confirmed_state instead.
    status: Literal["planned", "not_verifiable", "confirmed"] = "planned"

    @model_validator(mode="before")
    @classmethod
    def normalize_compatibility_values(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        completed = data.get("completed_state")
        if isinstance(completed, bool):
            data["completed_state"] = "completed" if completed else "not_started"
        confirmed = data.get("user_confirmed_state")
        if isinstance(confirmed, bool):
            data["user_confirmed_state"] = "confirmed" if confirmed else "not_requested"
        muted = data.get("expected_mute_state")
        if isinstance(muted, bool):
            data["expected_mute_state"] = "muted" if muted else "unmuted"
        replacement = data.get("replacement_vs_addition")
        if replacement in {"replace", "replace_existing", "replacement"}:
            data["replacement_vs_addition"] = "replacement"
        elif replacement in {"add", "add_new", "addition"}:
            data["replacement_vs_addition"] = "addition"
        return data

    @model_validator(mode="after")
    def validate_bars(self) -> "PlaylistPlacement":
        if self.end_bar < self.start_bar:
            raise ValueError("Playlist placement end_bar must not precede start_bar")
        if self.pattern_id is None and self.pattern_number is None and self.pattern_name is None:
            raise ValueError("Playlist placement needs a pattern ID, number, or name")
        if self.handoff_item_id is None:
            object.__setattr__(
                self,
                "handoff_item_id",
                "handoff-item-" + _canonical_digest(
                    {
                        "pattern_id": self.pattern_id,
                        "pattern_number": self.pattern_number,
                        "section_id": self.section_id,
                        "start_bar": self.start_bar,
                        "end_bar": self.end_bar,
                    }
                )[:24],
            )
        if self.length_bars is None:
            object.__setattr__(self, "length_bars", self.end_bar - self.start_bar)
        if self.length_bars is not None and self.length_bars < 0:
            raise ValueError("Playlist placement length_bars must not be negative")
        if self.status == "confirmed" and self.user_confirmed_state == "not_requested":
            object.__setattr__(self, "user_confirmed_state", "confirmed")
            object.__setattr__(self, "completed_state", "completed")
        elif self.status == "not_verifiable" and self.completed_state == "not_started":
            object.__setattr__(self, "completed_state", "not_verifiable")
        return self

    @property
    def playlist_track(self) -> int | None:
        return self.intended_playlist_track_number


class PlaylistHandoffManifest(CreationReviewModel):
    """Alias-compatible manifest for exact manual Playlist placement rows."""

    handoff_id: Identifier
    placements: tuple[PlaylistPlacement, ...] = Field(
        default=(), max_length=MAX_REVIEW_SECTIONS
    )
    delta_from_source: tuple[PlaylistPlacement, ...] = Field(
        default=(), max_length=MAX_REVIEW_SECTIONS
    )
    status: Literal[
        "none_required",
        "one_action_required",
        "multiple_actions_required",
        "user_confirmed_complete",
        "not_verifiable",
        # Compatibility values from the first draft of the contract.
        "not_requested",
        "required",
        "pending",
        "confirmed",
    ] = "none_required"
    warnings: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_WARNINGS)

    @model_validator(mode="after")
    def normalize_status(self) -> "PlaylistHandoffManifest":
        if self.status == "not_requested" and self.placements:
            object.__setattr__(
                self,
                "status",
                "one_action_required" if len(self.placements) == 1 else "multiple_actions_required",
            )
        elif self.status in {"required", "pending"}:
            object.__setattr__(
                self,
                "status",
                "one_action_required" if len(self.placements) == 1 else "multiple_actions_required",
            )
        elif self.status == "confirmed":
            object.__setattr__(self, "status", "user_confirmed_complete")
        return self


class PlaylistHandoff(CreationReviewModel):
    """Backwards-compatible name for :class:`PlaylistHandoffManifest`."""

    handoff_id: Identifier
    placements: tuple[PlaylistPlacement, ...] = Field(
        default=(), max_length=MAX_REVIEW_SECTIONS
    )
    delta_from_source: tuple[PlaylistPlacement, ...] = Field(
        default=(), max_length=MAX_REVIEW_SECTIONS
    )
    status: Literal[
        "none_required",
        "one_action_required",
        "multiple_actions_required",
        "user_confirmed_complete",
        "not_verifiable",
        "not_requested",
        "required",
        "pending",
        "confirmed",
    ] = "none_required"
    warnings: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_WARNINGS)

    @model_validator(mode="after")
    def normalize_status(self) -> "PlaylistHandoff":
        if self.status == "not_requested" and self.placements:
            object.__setattr__(
                self,
                "status",
                "one_action_required" if len(self.placements) == 1 else "multiple_actions_required",
            )
        elif self.status in {"required", "pending"}:
            object.__setattr__(
                self,
                "status",
                "one_action_required" if len(self.placements) == 1 else "multiple_actions_required",
            )
        elif self.status == "confirmed":
            object.__setattr__(self, "status", "user_confirmed_complete")
        return self


class ExportHandoff(CreationReviewModel):
    handoff_id: Identifier
    required_full_mix_export: StrictBool = True
    requested_stems: tuple[ReviewAssetKind, ...] = Field(default=(), max_length=MAX_REVIEW_STEMS)
    exact_start_bar: int | None = Field(default=None, ge=1)
    exact_end_bar: int | None = Field(default=None, ge=1)
    exact_start_seconds: float | None = Field(default=None, ge=0.0)
    exact_end_seconds: float | None = Field(default=None, ge=0.0)
    recommended_filename: ShortText
    before_after_naming_convention: ShortText = "before.wav / after.wav"
    include_tails: StrictBool = False
    normalization_off: StrictBool = True
    matching_settings_required: StrictBool = True
    expected_location: str | None = Field(default=None, max_length=4096)
    bounded_discovery_root: str | None = Field(default=None, max_length=4096)
    stem_reasons: FrozenMap = Field(default_factory=lambda: FrozenMap({}))
    next_action: BoundedText

    @model_validator(mode="after")
    def validate_range(self) -> "ExportHandoff":
        if self.exact_end_bar is not None and self.exact_start_bar is not None and self.exact_end_bar < self.exact_start_bar:
            raise ValueError("export bar range is reversed")
        if self.exact_end_seconds is not None and self.exact_start_seconds is not None and self.exact_end_seconds <= self.exact_start_seconds:
            raise ValueError("export time range is reversed")
        return self


class DeliveryManifest(CreationReviewModel):
    delivery_id: Identifier
    source_run_id: Identifier
    review_session_id: Identifier
    # Keep the human request and the bounded run checkpoints alongside their
    # IDs.  IDs alone are not enough to explain what was delivered after a
    # process restart or Production Run eviction.
    original_brief: BoundedText | None = Field(
        default=None,
        validation_alias=AliasChoices("original_brief", "brief"),
    )
    completion_target: ShortText | None = Field(default=None)
    source_run_status: Literal[
        "completed", "partial", "blocked", "failed", "stopped", "unknown"
    ] = "completed"
    source_state_digest: Digest | None = None
    source_run_details: FrozenMap | None = None
    final_revision_pass_id: Identifier | None = None
    final_run_id: Identifier | None = Field(
        default=None,
        validation_alias=AliasChoices("final_run_id", "continuation_run_id"),
    )
    final_run_status: Literal[
        "created",
        "preflight",
        "revising",
        "awaiting_rebounce",
        "completed",
        "partial",
        "blocked",
        "failed",
        "stopped",
        "unknown",
    ] | None = None
    final_run_details: RevisionPass | FrozenMap | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "final_run_details", "final_run", "final_revision_details"
        ),
    )
    final_revision_pass: RevisionPass | None = Field(
        default=None,
        validation_alias=AliasChoices("final_revision_pass", "final_pass"),
    )
    creation_outcome: CreationOutcome | FrozenMap | None = None
    # Keep outcome dimensions independent of the convenience aggregate.  The
    # aliases let integrations migrate from ``technical``/``audible`` names
    # without collapsing technical evidence into artistic approval.
    technical_outcome: CreationOutcome | EvaluationState | FrozenMap | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "technical_outcome", "technical_execution", "technical"
        ),
    )
    arrangement_outcome: CreationOutcome | EvaluationState | FrozenMap | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "arrangement_outcome", "arrangement_delivery", "arrangement"
        ),
    )
    processing_outcome: CreationOutcome | EvaluationState | FrozenMap | None = Field(
        default=None,
        validation_alias=AliasChoices("processing_outcome", "processing"),
    )
    audible_quality_outcome: CreationOutcome | EvaluationState | FrozenMap | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "audible_quality_outcome", "audible_quality", "audible"
        ),
    )
    accepted_palette: FrozenMap = Field(default_factory=lambda: FrozenMap({}))
    accepted_generated_outputs: tuple[ReviewGeneratedOutput, ...] = Field(
        default=(),
        validation_alias=AliasChoices(
            "accepted_generated_outputs", "accepted_outputs", "generated_outputs"
        ),
        max_length=MAX_REVIEW_OPERATIONS,
    )
    accepted_role_assignments: tuple[ReviewRoleAssignment | FrozenMap, ...] = Field(
        default=(),
        validation_alias=AliasChoices(
            "accepted_role_assignments", "role_assignments", "accepted_roles"
        ),
        max_length=MAX_REVIEW_ROLE,
    )
    accepted_sections: tuple[ReviewSection, ...] = Field(default=(), max_length=MAX_REVIEW_SECTIONS)
    pattern_placements: tuple[PlaylistPlacement, ...] = Field(default=(), max_length=MAX_REVIEW_SECTIONS)
    playlist_handoff: PlaylistHandoff | None = None
    export_handoff: ExportHandoff | None = None
    review_assets: tuple[ReviewAudioAsset, ...] = Field(default=(), max_length=MAX_REVIEW_ASSETS)
    evaluations: tuple[CreationEvaluationReport, ...] = Field(
        default=(),
        validation_alias=AliasChoices("evaluations", "evaluation_reports", "reports"),
        max_length=MAX_REVIEW_EVALUATIONS,
    )
    comparisons: tuple[RevisionComparison, ...] = Field(
        default=(),
        validation_alias=AliasChoices("comparisons", "revision_comparisons"),
        max_length=MAX_REVIEW_COMPARISONS,
    )
    remaining_manual_actions: tuple[ManualHandoff, ...] = Field(default=(), max_length=MAX_REVIEW_LIMITATIONS)
    unresolved_limitations: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_LIMITATIONS)
    final_user_approval: UserApprovalState = Field(
        default="not_requested",
        validation_alias=AliasChoices(
            "final_user_approval", "approval", "user_approval"
        ),
    )
    next_action: BoundedText
    manifest_digest: Digest | None = None
    generated_at: datetime = Field(default_factory=_now)

    @property
    def digest(self) -> str:
        return self.manifest_digest or _canonical_digest(
            self.model_dump(mode="json", exclude={"manifest_digest", "generated_at"})
        )

    @model_validator(mode="after")
    def validate_manifest(self) -> "DeliveryManifest":
        generated_ids = [item.output_id for item in self.accepted_generated_outputs]
        if len(generated_ids) != len(set(generated_ids)):
            raise ValueError("accepted generated output IDs must be unique")
        assignment_ids = [
            item.assignment_id
            if isinstance(item, ReviewRoleAssignment)
            else str(
                item.get("assignment_id", item.get("role_id"))
                or _canonical_digest(item.to_dict())
            )
            for item in self.accepted_role_assignments
        ]
        if len(assignment_ids) != len(set(assignment_ids)):
            raise ValueError("accepted role assignment IDs must be unique")
        evaluation_ids = [item.evaluation_id for item in self.evaluations]
        if len(evaluation_ids) != len(set(evaluation_ids)):
            raise ValueError("delivery evaluation IDs must be unique")
        comparison_ids = [item.comparison_id for item in self.comparisons]
        if len(comparison_ids) != len(set(comparison_ids)):
            raise ValueError("delivery comparison IDs must be unique")
        if self.final_revision_pass is not None:
            if self.final_revision_pass_id is not None and self.final_revision_pass_id != self.final_revision_pass.revision_pass_id:
                raise ValueError("final_revision_pass_id must match final_revision_pass")
            if self.final_revision_pass.source_run_id != self.source_run_id:
                raise ValueError("final revision pass must belong to source_run_id")
            if self.final_revision_pass_id is None:
                object.__setattr__(
                    self, "final_revision_pass_id", self.final_revision_pass.revision_pass_id
                )
        if isinstance(self.final_run_details, RevisionPass):
            if (
                self.final_revision_pass_id is not None
                and self.final_revision_pass_id
                != self.final_run_details.revision_pass_id
            ):
                raise ValueError("final run details must match final_revision_pass_id")
            if self.final_run_details.source_run_id != self.source_run_id:
                raise ValueError("final run details must belong to source_run_id")
            if self.final_revision_pass_id is None:
                object.__setattr__(
                    self,
                    "final_revision_pass_id",
                    self.final_run_details.revision_pass_id,
                )
        if self.manifest_digest is None:
            object.__setattr__(self, "manifest_digest", self.digest)  # type: ignore[misc]
        return self

    @property
    def technical_execution(self) -> FrozenMap | CreationOutcome | EvaluationState | None:
        return self.technical_outcome

    @property
    def arrangement_delivery(self) -> FrozenMap | CreationOutcome | EvaluationState | None:
        return self.arrangement_outcome

    @property
    def audible_quality(self) -> FrozenMap | CreationOutcome | EvaluationState | None:
        return self.audible_quality_outcome

    @property
    def approval(self) -> UserApprovalState:
        """Compatibility spelling for the explicit final approval state."""

        return self.final_user_approval

    @property
    def brief(self) -> BoundedText | None:
        """Compatibility spelling for the original review brief."""

        return self.original_brief

    @property
    def final_run(self) -> RevisionPass | FrozenMap | None:
        """Compatibility spelling for the retained final run checkpoint."""

        return self.final_run_details

    @property
    def generated_outputs(self) -> tuple[ReviewGeneratedOutput, ...]:
        """Compatibility spelling for accepted generated outputs."""

        return self.accepted_generated_outputs

    @property
    def accepted_outputs(self) -> tuple[ReviewGeneratedOutput, ...]:
        """Compatibility spelling for accepted generated outputs."""

        return self.accepted_generated_outputs

    @property
    def role_assignments(self) -> tuple[ReviewRoleAssignment | FrozenMap, ...]:
        """Compatibility spelling for accepted role assignments."""

        return self.accepted_role_assignments

    @property
    def accepted_roles(self) -> tuple[ReviewRoleAssignment | FrozenMap, ...]:
        """Compatibility spelling for accepted role assignments."""

        return self.accepted_role_assignments

    @property
    def evaluation_reports(self) -> tuple[CreationEvaluationReport, ...]:
        """Compatibility spelling for retained evaluation reports."""

        return self.evaluations

    @property
    def revision_comparisons(self) -> tuple[RevisionComparison, ...]:
        """Compatibility spelling for retained before/after comparisons."""

        return self.comparisons


class ReviewSourceSnapshot(CreationReviewModel):
    """Bounded immutable source-run data retained by a Review Session."""

    source_run_id: Identifier
    source_run_status: Literal["completed"] = "completed"
    source_state_digest: Digest | None = None
    session_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{32}$"
    )
    # Transport is copied with the source snapshot so section arithmetic can
    # be reconstructed after the Production Run record leaves memory.  The
    # fields are optional for compatibility with older snapshots that only
    # carried section timestamps.
    tempo_bpm: float | None = Field(default=None, gt=0.0, le=522.0)
    time_signature_numerator: int | None = Field(default=None, ge=1, le=32)
    time_signature_denominator: Literal[1, 2, 4, 8, 16, 32] | None = None
    tempo_changes: tuple[TempoChange, ...] = Field(default=(), max_length=64)
    original_brief: BoundedText
    completion_target: ShortText
    preserve: ReviewPreserveRules = Field(default_factory=ReviewPreserveRules)
    sound_palette: FrozenMap = Field(default_factory=lambda: FrozenMap({}))
    generated_note_sequences: tuple[ReviewGeneratedOutput, ...] = Field(default=(), max_length=MAX_REVIEW_OPERATIONS)
    processing_receipts: tuple[ReviewGeneratedOutput, ...] = Field(default=(), max_length=MAX_REVIEW_OPERATIONS)
    sections: tuple[ReviewSection, ...] = Field(default=(), max_length=MAX_REVIEW_SECTIONS)
    pattern_plan: tuple[PlaylistPlacement, ...] = Field(default=(), max_length=MAX_REVIEW_SECTIONS)
    manual_handoffs: tuple[ManualHandoff, ...] = Field(default=(), max_length=MAX_REVIEW_LIMITATIONS)
    creation_outcome: CreationOutcome | FrozenMap | None = None
    source_receipt_digests: tuple[Digest, ...] = Field(default=(), max_length=MAX_REVIEW_OPERATIONS)

    @model_validator(mode="after")
    def validate_transport(self) -> "ReviewSourceSnapshot":
        for previous, current in zip(self.tempo_changes, self.tempo_changes[1:]):
            if current.start_bar <= previous.start_bar:
                raise ValueError("tempo_changes must be ordered by unique start_bar")
        return self


class ReviewSession(CreationReviewModel):
    """Persistent, bounded review state linked to one completed run."""

    schema_version: Literal["1.0"] = CREATION_REVIEW_SCHEMA_VERSION
    review_session_id: Identifier
    source_run_id: Identifier
    request: ReviewSessionRequest
    source_snapshot: ReviewSourceSnapshot | None = None
    status: ReviewSessionStatus = "created"
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    source_creation_outcome: CreationOutcome | FrozenMap | None = None
    source_sound_palette: FrozenMap = Field(default_factory=lambda: FrozenMap({}))
    source_note_sequences: tuple[ReviewGeneratedOutput, ...] = Field(default=(), max_length=MAX_REVIEW_OPERATIONS)
    source_processing_receipts: tuple[ReviewGeneratedOutput, ...] = Field(default=(), max_length=MAX_REVIEW_OPERATIONS)
    source_sections: tuple[ReviewSection, ...] = Field(default=(), max_length=MAX_REVIEW_SECTIONS)
    source_pattern_plan: tuple[PlaylistPlacement, ...] = Field(default=(), max_length=MAX_REVIEW_SECTIONS)
    source_manual_handoffs: tuple[ManualHandoff, ...] = Field(default=(), max_length=MAX_REVIEW_LIMITATIONS)
    asset_sets: tuple[ReviewAssetSet, ...] = Field(default=(), max_length=MAX_REVIEW_ASSET_SETS)
    assets: tuple[ReviewAudioAsset, ...] = Field(default=(), max_length=MAX_REVIEW_ASSETS)
    section_map: ReviewSectionMap | None = None
    evaluations: tuple[CreationEvaluationReport, ...] = Field(default=(), max_length=MAX_REVIEW_EVALUATIONS)
    feedback: tuple[CreationFeedback, ...] = Field(default=(), max_length=MAX_REVIEW_FEEDBACK)
    revision_plans: tuple[RevisionPlan, ...] = Field(default=(), max_length=MAX_REVIEW_PASSES)
    revision_passes: tuple[RevisionPass, ...] = Field(default=(), max_length=MAX_REVIEW_PASSES)
    comparisons: tuple[RevisionComparison, ...] = Field(default=(), max_length=MAX_REVIEW_COMPARISONS)
    delivery_manifests: tuple[DeliveryManifest, ...] = Field(default=(), max_length=MAX_REVIEW_MANIFESTS)
    current_next_action: ShortText = "Attach one exported full-mix bounce."
    warnings: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_WARNINGS)
    blockers: tuple[ShortText, ...] = Field(default=(), max_length=MAX_REVIEW_LIMITATIONS)
    process_local: Literal[True] = True

    @property
    def session_id(self) -> str:
        return self.review_session_id

    @property
    def source_run(self) -> ReviewSourceSnapshot | None:
        return self.source_snapshot

    @model_validator(mode="after")
    def validate_session(self) -> "ReviewSession":
        if self.source_run_id != self.request.source_run_id:
            raise ValueError("session source_run_id must match request source_run_id")
        if self.updated_at < self.created_at:
            raise ValueError("session updated_at cannot precede created_at")
        for label, identifier, values in (
            ("asset_sets", "asset_set_id", self.asset_sets),
            ("assets", "asset_id", self.assets),
            ("evaluations", "evaluation_id", self.evaluations),
            ("feedback", "feedback_id", self.feedback),
            ("revision_plans", "revision_plan_id", self.revision_plans),
            ("revision_passes", "revision_pass_id", self.revision_passes),
            ("comparisons", "comparison_id", self.comparisons),
            ("delivery_manifests", "delivery_id", self.delivery_manifests),
        ):
            ids = [getattr(item, identifier) for item in values]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} IDs must be unique")
        if len(self.revision_passes) > self.request.max_revision_passes:
            raise ValueError("session revision pass limit exceeded")
        if len(self.assets) > MAX_REVIEW_ASSETS:
            raise ValueError("session asset limit exceeded")
        return self


class ReviewSessionDocument(CreationReviewModel):
    """Versioned on-disk envelope for local Review Sessions."""

    schema_version: Literal["1.0"] = CREATION_REVIEW_SCHEMA_VERSION
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    sessions: tuple[ReviewSession, ...] = Field(default=(), max_length=MAX_REVIEW_SESSIONS)

    @model_validator(mode="after")
    def validate_document(self) -> "ReviewSessionDocument":
        ids = [item.review_session_id for item in self.sessions]
        if len(ids) != len(set(ids)):
            raise ValueError("review session IDs must be unique")
        if self.updated_at < self.created_at:
            raise ValueError("document updated_at cannot precede created_at")
        return self


class ReviewStoreStatus(CreationReviewModel):
    path: str
    exists: bool
    healthy: bool
    corrupt: bool
    schema_version: str | None = None
    session_count: int = Field(default=0, ge=0, le=MAX_REVIEW_SESSIONS)
    max_sessions: int = Field(ge=1, le=MAX_REVIEW_SESSIONS)
    max_revision_passes: int = Field(ge=0, le=MAX_REVIEW_PASSES)
    max_assets: int = Field(default=MAX_REVIEW_ASSETS, ge=1, le=MAX_REVIEW_ASSETS)
    max_findings: int = Field(default=MAX_REVIEW_FINDINGS, ge=1, le=MAX_REVIEW_FINDINGS)
    max_feedback: int = Field(default=MAX_REVIEW_FEEDBACK, ge=1, le=MAX_REVIEW_FEEDBACK)
    max_asset_sets: int = Field(default=MAX_REVIEW_ASSET_SETS, ge=1, le=MAX_REVIEW_ASSET_SETS)
    max_evaluations: int = Field(default=MAX_REVIEW_EVALUATIONS, ge=1, le=MAX_REVIEW_EVALUATIONS)
    max_comparisons: int = Field(default=MAX_REVIEW_COMPARISONS, ge=1, le=MAX_REVIEW_COMPARISONS)
    max_manifests: int = Field(default=MAX_REVIEW_MANIFESTS, ge=1, le=MAX_REVIEW_MANIFESTS)
    warnings: tuple[ShortText, ...] = Field(default=(), max_length=16)
    error: str | None = Field(default=None, max_length=MAX_REVIEW_TEXT)


class ReviewStoreResetResult(CreationReviewModel):
    path: str
    existed: bool
    removed: bool
    recoverable: Literal[False] = False
    warnings: tuple[ShortText, ...] = Field(default=(), max_length=16)


__all__ = [
    "AcceptedElementLock",
    "Actionability",
    "AddSupportingLayerOperation",
    "AlignmentResult",
    "ApplySemanticProcessingOperation",
    "AssetValidationState",
    "AdjustChannelMixOperation",
    "AdjustRoleLevelOperation",
    "BoundedText",
    "ChangeDrumKitOperation",
    "ChangeDrumRoleMappingOperation",
    "ChangeSectionArticulationOperation",
    "ChangeSectionDensityOperation",
    "ChangeSectionRegisterOperation",
    "ChangeSectionRhythmOperation",
    "ChangeSectionVelocityOperation",
    "ChangeSectionVoicingOperation",
    "ChangeSoundAssignmentOperation",
    "ConfidenceLevel",
    "CreationEvaluationReport",
    "CreationFeedback",
    "CreationReviewModel",
    "CreationOutcome",
    "CREATION_REVIEW_DIGEST_ALGORITHM",
    "CREATION_REVIEW_SCHEMA_VERSION",
    "CreatePlaylistHandoffDeltaOperation",
    "CreateSectionNoteVariationOperation",
    "CreateSoundPaletteVariationOperation",
    "DeliveryManifest",
    "Digest",
    "EvaluationFinding",
    "EvaluationState",
    "ExportHandoff",
    "FeedbackDirective",
    "FeedbackLock",
    "FindingCategory",
    "FeedbackSource",
    "FeedbackVerdict",
    "FrozenMap",
    "GoalEvaluation",
    "ManualHandoff",
    "MetricExpectation",
    "ObjectiveResult",
    "PaletteFeedback",
    "PlaylistHandoff",
    "PlaylistHandoffManifest",
    "PlaylistPlacement",
    "ProcessingFeedback",
    "RecordFeedbackLockOperation",
    "ReferenceComparison",
    "RegenerateRoleSequenceOperation",
    "RemoveGeneratedLayerOperation",
    "ReviewAssetKind",
    "ReviewAssetSet",
    "ReviewAudioAsset",
    "ReviewEvaluationPolicy",
    "ReviewGeneratedOutput",
    "ReviewInteractionPolicy",
    "ReviewMetric",
    "ReviewPreserveRules",
    "ReviewRangeIssue",
    "ReviewReferenceSectionPair",
    "ReviewReferenceSectionWindow",
    "ReviewRoleAssignment",
    "ReviewSection",
    "ReviewSectionRangeInput",
    "ReviewSectionMap",
    "ReviewSectionMeasurement",
    "ReviewSession",
    "ReviewSessionDocument",
    "ReviewSessionRequest",
    "ReviewSessionStatus",
    "ReviewSourceSnapshot",
    "ReviewStemMeasurement",
    "ReviewStoreResetResult",
    "ReviewStoreStatus",
    "RevisionComparison",
    "RevisionOperation",
    "RevisionOperationBase",
    "RevisionOperationKind",
    "RevisionOperationReceipt",
    "RevisionPass",
    "RevisionPlan",
    "RevisionRequest",
    "SectionDelta",
    "SectionFeedback",
    "SectionSource",
    "ShortText",
    "TempoChange",
    "TransformGeneratedSequenceOperation",
    "UpdateSectionMarkersOperation",
    "RoleFeedback",
    "SHA256_PATTERN",
    "LockKind",
    "LockTarget",
]
