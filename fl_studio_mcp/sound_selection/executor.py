"""Live Sound Selection integration.

The models, scorer, and palette planner in :mod:`sound_selection` are
deliberately bridge-free.  This module is the small runtime seam around them:
it takes one Track B observation, enriches it with non-authoritative Atlas
metadata, and keeps the resulting palette/application state in bounded local
registries.

There are two important boundaries here:

* Atlas only describes a product match.  A name match never becomes proof that
  a plug-in is loaded, controllable, or writable.
* Applying a plan is a fail-closed sequence of one-way verified operations.
  There is no retry, rollback, save, or dispatch-only success path.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import Field, field_validator

from ..performance import TrackBController, TrackBInspector
from ..plugin_atlas import AtlasRegistry, load_bundled_registry
from ..plugin_atlas_mcp import (
    AtlasInspectLoadedRequest,
    AtlasInspectLoadedResponse,
    inspect_loaded_atlas,
)
from ..track_b_contracts import (
    ExpectedPluginPresetState,
    PluginCurrentPreset,
    PluginPad,
    PluginPadMap,
    PluginPresetPage,
    PluginPresetState,
    TargetedLoadedPluginInventory,
    TargetedPluginSummary,
    VerifiedPluginPresetSelection,
)
from ..verified_writer import WriteModeManager
from .descriptors import descriptor_names, descriptors_for_product
from .history import (
    LocalSoundSelectionHistory,
    SoundHistoryResetResult,
    SoundHistoryStatus,
)
from .models import (
    DEFAULT_DRUM_ROLES,
    MAX_REPORTED_PRESET_COUNT,
    MAX_ROLE_COUNT,
    DrumPad,
    DrumPadMap,
    DrumRoleMapping,
    PaletteApplyReceipt,
    SoundFeedbackRequest,
    SoundInventory,
    SoundPaletteAssignment,
    SoundPalettePlan,
    SoundPaletteState,
    SoundPaletteVariationPlan,
    SoundPresetDiscoveryCoverage,
    SoundSelectionModel,
    SoundSelectionRequest,
    SoundTargetInventory,
    canonical_digest,
    target_identity_key,
)
from .palette import (
    SoundPaletteStateRegistry,
    create_palette_variation,
    plan_palette,
)
from .preset_discovery import (
    PresetCandidateDiscoveryPolicy,
    discover_preset_candidates,
)


SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_SERVICE_WARNINGS = 64
MAX_TARGET_WARNINGS = 32
MAX_PRESET_PAGE = 256
MAX_MESSAGE_LENGTH = 1024


class SoundSelectionExecutionError(RuntimeError):
    """The live service could not complete a bounded operation safely."""


class SoundSelectionAuthorizationError(PermissionError, SoundSelectionExecutionError):
    """The caller did not explicitly authorize a project mutation."""


class SoundSelectionSessionError(ValueError, SoundSelectionExecutionError):
    """The caller omitted or supplied a stale session identity."""


class SoundSelectionApplyResult(SoundSelectionModel):
    """Compact result for one palette application.

    ``receipts`` contains the exact Track B receipt returned by each attempted
    preset mutation, including an unverified receipt when FL reported one.
    ``assignment_receipts`` is the immutable, per-role summary retained by the
    local palette registry and also covers ``keep_current`` roles.
    """

    schema_version: Literal["1.0"] = "1.0"
    palette_id: str = Field(min_length=1, max_length=128)
    status: Literal["planned", "applying", "applied", "partially_applied", "failed"]
    session_fingerprint: str = Field(min_length=1, max_length=128)
    state: SoundPaletteState
    assignment_scope: tuple[SoundPaletteAssignment, ...] = Field(
        default=(), max_length=MAX_ROLE_COUNT
    )
    receipts: tuple[VerifiedPluginPresetSelection, ...] = Field(default=(), max_length=128)
    assignment_receipts: tuple[PaletteApplyReceipt, ...] = Field(default=(), max_length=128)
    failed_assignment_id: str | None = Field(default=None, max_length=128)
    verified_count: int = Field(default=0, ge=0, le=128)
    history_written: int = Field(default=0, ge=0, le=128)
    warnings: tuple[str, ...] = Field(default=(), max_length=MAX_SERVICE_WARNINGS)
    blockers: tuple[str, ...] = Field(default=(), max_length=MAX_SERVICE_WARNINGS)

    @field_validator("receipts", mode="before")
    @classmethod
    def _restore_track_b_warning_lists(cls, value: object) -> object:
        """Keep serialized Track B receipts compatible with strict lists.

        ``SoundSelectionModel`` freezes nested JSON arrays to tuples.  Track B
        receipts intentionally retain their existing ``list[str]`` contract,
        so restore only that boundary before Pydantic validates the nested
        receipt.  This matters when a Production Run rehydrates a receipt
        from ``model_dump(mode="python")``.
        """

        if not isinstance(value, (list, tuple)):
            return value
        normalized: list[object] = []
        for item in value:
            if isinstance(item, dict):
                row = dict(item)
                track_b_warnings = row.get("warnings")
                if isinstance(track_b_warnings, tuple):
                    row["warnings"] = list(track_b_warnings)
                normalized.append(row)
            else:
                normalized.append(item)
        return tuple(normalized)

    @property
    def selection_receipts(self) -> tuple[VerifiedPluginPresetSelection, ...]:
        """Descriptive alias used by callers that prefer the longer name."""

        return self.receipts

    @property
    def preset_receipts(self) -> tuple[VerifiedPluginPresetSelection, ...]:
        """Alias for the exact preset-selection receipts."""

        return self.receipts


class SoundPaletteLookup(SoundSelectionModel):
    """Availability-honest lookup result for the process-local palette store."""

    schema_version: Literal["1.0"] = "1.0"
    found: bool
    process_local: Literal[True] = True
    message: str = Field(min_length=1, max_length=512)
    state: SoundPaletteState | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.found and self.state is None:
            raise ValueError("a found palette lookup must carry its state")
        if not self.found and self.state is not None:
            raise ValueError("an unavailable palette lookup cannot carry state")


class SoundFeedbackResult(SoundSelectionModel):
    """Result of one explicit local feedback operation."""

    schema_version: Literal["1.0"] = "1.0"
    feedback: SoundFeedbackRequest
    persisted: bool
    history: SoundHistoryStatus
    warnings: tuple[str, ...] = Field(default=(), max_length=16)

    @property
    def history_status(self) -> SoundHistoryStatus:
        return self.history


class _AtlasTargetView:
    """Small internal view; Atlas remains descriptive only."""

    __slots__ = ("product", "match", "record")

    def __init__(self, product: Any = None, match: Any = None, record: Any = None) -> None:
        self.product = product
        self.match = match
        self.record = record


class _Preflight:
    """Internal immutable-ish preflight output kept out of JSON contracts."""

    __slots__ = ("warnings", "blockers", "current")

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.blockers: list[str] = []
        self.current: dict[str, PluginPresetState] = {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _dedupe(values: Iterable[Any], *, limit: int) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if len(text) > MAX_MESSAGE_LENGTH:
            text = text[: MAX_MESSAGE_LENGTH - 1].rstrip() + "…"
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return tuple(result)


def _field(value: Any, name: str, default: Any = None) -> Any:
    return getattr(value, name, default)


def _target_kwargs(target: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {"target": target}
    if getattr(target, "kind", None) == "mixer_effect":
        arguments["allow_master"] = bool(getattr(target, "allow_master", False))
    return arguments


def _exact_preset_loader(inspector: Any, target: Any) -> Callable[..., Any] | None:
    """Adapt an optional inspector exact-preset surface to discovery."""

    method_names = (
        "lookup_plugin_preset",
        "find_plugin_preset",
        "resolve_plugin_preset",
        "get_plugin_preset",
        "exact_plugin_preset",
    )
    method = next(
        (
            getattr(inspector, name, None)
            for name in method_names
            if callable(getattr(inspector, name, None))
        ),
        None,
    )
    if method is None:
        return None

    def lookup(name: str) -> Any:
        target_kwargs = _target_kwargs(target)
        for key in ("name", "preset_name", "query"):
            try:
                return method(**target_kwargs, **{key: name})
            except TypeError:
                continue
        try:
            return method(target, name)
        except TypeError:
            return None

    return lookup


def _target_key(target: Any) -> str:
    try:
        return target_identity_key(target)
    except (TypeError, ValueError, AttributeError):
        return canonical_digest(getattr(target, "model_dump", lambda **_: target)())


def _as_loaded(value: Any) -> TargetedLoadedPluginInventory:
    if isinstance(value, TargetedLoadedPluginInventory):
        return value
    if isinstance(value, dict):
        return TargetedLoadedPluginInventory.model_validate(value, strict=False)
    # Test doubles may intentionally expose the same attributes without
    # importing Track B contracts.  They are normalized into the contract so
    # the rest of the service has one shape.
    plugins = tuple(_field(value, "plugins", ()))
    rows = tuple(
        row if isinstance(row, TargetedPluginSummary) else TargetedPluginSummary.model_validate(row, strict=False)
        for row in plugins
    )
    return TargetedLoadedPluginInventory(
        observed_at=_stamp(_field(value, "observed_at", _utc_now())),
        project_dirty_flag=_field(value, "project_dirty_flag", None),
        plugins=list(rows),
        warnings=list(_field(value, "warnings", ())),
    )


def _as_page(value: Any) -> PluginPresetPage | Any:
    if isinstance(value, PluginPresetPage):
        return value
    if isinstance(value, dict):
        return PluginPresetPage.model_validate(value, strict=False)
    return value


def _as_current(value: Any) -> PluginCurrentPreset | Any:
    if isinstance(value, PluginCurrentPreset):
        return value
    if isinstance(value, dict):
        return PluginCurrentPreset.model_validate(value, strict=False)
    return value


def _as_pad_map(value: Any) -> PluginPadMap | Any:
    if isinstance(value, PluginPadMap):
        return value
    if isinstance(value, dict):
        return PluginPadMap.model_validate(value, strict=False)
    return value


def _summary_from_observation(value: Any, fallback: TargetedPluginSummary) -> TargetedPluginSummary:
    summary = _field(value, "plugin", None)
    if isinstance(summary, TargetedPluginSummary):
        return summary
    if isinstance(summary, dict):
        return TargetedPluginSummary.model_validate(summary, strict=False)
    return fallback


def _status_current(value: Any) -> PluginPresetState | None:
    if value is None:
        return None
    current = _field(value, "current", None)
    if isinstance(current, PluginPresetState):
        return current
    if isinstance(current, dict):
        try:
            return PluginPresetState.model_validate(current, strict=False)
        except Exception:
            return None
    name = _field(value, "current_preset_name", None)
    index = _field(value, "current_preset_index", None)
    status = _field(value, "current_preset_status", None)
    if status is None:
        return None
    try:
        return PluginPresetState(name=name, index=index, identity_status=status)
    except Exception:
        return None


def _session_values(*values: Any) -> tuple[str, ...]:
    sessions: list[str] = []
    for value in values:
        session = _field(value, "session_fingerprint", None)
        if isinstance(session, str) and session:
            sessions.append(session)
    return tuple(sessions)


def _identity_values(*values: Any) -> tuple[str, ...]:
    identities: list[str] = []
    for value in values:
        summary = _field(value, "plugin", None)
        candidate = _field(summary, "target_fingerprint", None)
        if candidate is None:
            candidate = _field(value, "target_fingerprint", None)
        if isinstance(candidate, str) and candidate:
            identities.append(candidate)
    return tuple(identities)


def _stable_one(values: Iterable[str]) -> tuple[str | None, tuple[str, ...]]:
    rows = tuple(dict.fromkeys(values))
    if not rows:
        return None, ()
    if len(rows) == 1:
        return rows[0], ()
    return None, ("live observations disagreed about the target/session identity",)


def _preset_rows(page: Any) -> tuple[tuple[str, int], ...]:
    rows: list[tuple[str, int]] = []
    for row in tuple(_field(page, "presets", ())):
        name = _field(row, "name", None)
        index = _field(row, "index", None)
        if not isinstance(name, str) or not isinstance(index, int) or isinstance(index, bool):
            continue
        rows.append((name, index))
    return tuple(rows)


_DRUM_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("closed_hat", ("closed hat", "closed_hat", "closedhat", "hat closed", "chh")),
    ("open_hat", ("open hat", "open_hat", "openhat", "hat open", "ohh")),
    ("kick", ("kick", "bass drum", "bassdrum", "bd")),
    ("snare", ("snare", "snare drum", "sd")),
    ("clap", ("clap", "handclap", "hand clap")),
    ("crash", ("crash",)),
    ("ride", ("ride",)),
    ("tom", ("tom",)),
    ("percussion", ("percussion", "perc")),
)


def _semantic_roles(name: str | None) -> tuple[str, ...]:
    if not isinstance(name, str) or not name.strip():
        return ()
    normalized = " ".join(name.casefold().replace("_", " ").replace("-", " ").split())
    words = tuple(normalized.split())
    roles: list[str] = []
    for role, aliases in _DRUM_ALIASES:
        for alias in aliases:
            alias_words = tuple(alias.split())
            width = len(alias_words)
            if any(words[index : index + width] == alias_words for index in range(len(words))):
                roles.append(role)
                break
    return tuple(dict.fromkeys(roles))


def convert_plugin_pad_map(
    raw: Any,
    target: Any,
    *,
    target_fingerprint: str | None,
    required_roles: Sequence[str] = DEFAULT_DRUM_ROLES,
) -> DrumPadMap | None:
    """Convert one bounded FL pad observation into the shared semantic map."""

    if raw is None:
        return None
    pads: list[DrumPad] = []
    mapped: dict[str, DrumRoleMapping] = {}
    warnings = list(_field(raw, "warnings", ()))
    raw_pads = tuple(_field(raw, "pads", ()))
    for raw_pad in raw_pads:
        if isinstance(raw_pad, PluginPad):
            pad = raw_pad
        elif isinstance(raw_pad, dict):
            try:
                pad = PluginPad.model_validate(raw_pad, strict=False)
            except Exception:
                continue
        else:
            pad = raw_pad
        index = _field(pad, "pad_index", None)
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        note = _field(pad, "semitone", None)
        if not isinstance(note, int) or isinstance(note, bool) or not 0 <= note <= 127:
            note = None
        name = _field(pad, "semitone_name", None)
        roles = _semantic_roles(name)
        empty_value = _field(pad, "empty", None)
        muted_value = _field(pad, "muted", None)
        if empty_value is None or muted_value is None:
            warnings.append("FL did not report every pad empty/muted flag")
        drum_pad = DrumPad(
            pad_index=index,
            midi_note=note,
            color=_field(pad, "color", None),
            empty=bool(empty_value) if empty_value is not None else False,
            muted=bool(muted_value) if muted_value is not None else False,
            semitone_name=name if isinstance(name, str) else None,
            semantic_roles=roles,
        )
        pads.append(drum_pad)
        if drum_pad.empty or drum_pad.muted or note is None:
            continue
        for role in roles:
            if role in mapped:
                warnings.append(f"multiple pads reported the drum role {role!r}; lowest pad wins")
                continue
            mapped[role] = DrumRoleMapping(
                role=role,
                pad_index=index,
                midi_note=note,
                confidence=0.80,
                source="reported_name",
            )
    pads.sort(key=lambda item: item.pad_index)
    pad_count = _field(raw, "pad_count", len(pads))
    if not isinstance(pad_count, int) or pad_count < 0:
        pad_count = len(pads)
    # An incomplete bridge result cannot truthfully carry a reported count and
    # a shorter tuple under DrumPadMap's invariant, so retain the bounded rows
    # and make the partial condition explicit in warnings.
    if pads and pad_count != len(pads):
        warnings.append("FL returned a partial pad map")
        pad_count = len(pads)
    missing = tuple(role for role in required_roles if role.casefold() not in mapped)
    if missing:
        warnings.append("required drum roles are not mapped: " + ", ".join(missing))
    map_id = "drum-map-" + canonical_digest(
        {
            "target": None if target is None else target.model_dump(mode="json", exclude_none=False),
            "target_fingerprint": target_fingerprint,
            "pads": [item.model_dump(mode="json", exclude_none=False) for item in pads],
            "mappings": [item.model_dump(mode="json", exclude_none=False) for item in mapped.values()],
        }
    )[:24]
    try:
        return DrumPadMap(
            map_id=map_id,
            target=target,
            pad_count=pad_count,
            pads=tuple(pads),
            mappings=tuple(mapped.values()),
            missing_roles=missing,
            confidence=0.80 if mapped else 0.0,
            warnings=_dedupe(warnings, limit=MAX_TARGET_WARNINGS),
        )
    except Exception:
        return None


def _atlas_views(response: Any, registry: Any) -> dict[str, _AtlasTargetView]:
    views: dict[str, _AtlasTargetView] = {}
    for record in tuple(_field(response, "plugins", ())):
        target = _field(record, "target", None)
        if target is None:
            plugin = _field(record, "plugin", None)
            target = _field(plugin, "target", None)
        if target is None:
            continue
        match = _field(record, "best_match", None)
        product_id = _field(match, "product_id", None)
        product = None
        if isinstance(product_id, str) and product_id:
            try:
                product = registry.product(product_id)
            except (AttributeError, KeyError, TypeError):
                product = None
        views[_target_key(target)] = _AtlasTargetView(product, match, record)
    return views


class _LazyDependency:
    """Delay live bridge construction until a Sound Selection call needs it."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._value: Any | None = None
        self._lock = threading.Lock()

    def _get(self) -> Any:
        if self._value is None:
            with self._lock:
                if self._value is None:
                    self._value = self._factory()
        return self._value

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get(), name)


class SoundSelectionService:
    """Live inventory/planning/application service with injectable seams."""

    def __init__(
        self,
        *,
        inspector: TrackBInspector | Any | None = None,
        controller: TrackBController | Any | None = None,
        write_mode_manager: WriteModeManager | Any | None = None,
        atlas_registry: AtlasRegistry | Any | None = None,
        atlas_inspector: Callable[..., AtlasInspectLoadedResponse] | None = None,
        history: LocalSoundSelectionHistory | Any | None = None,
        palette_registry: SoundPaletteStateRegistry | None = None,
        now: Callable[[], datetime] | None = None,
        write_mode_already_enabled: bool = False,
    ) -> None:
        self.inspector = (
            inspector
            if inspector is not None
            else _LazyDependency(TrackBInspector)
        )
        self.controller = (
            controller
            if controller is not None
            else _LazyDependency(TrackBController)
        )
        self.write_mode_manager = (
            write_mode_manager
            if write_mode_manager is not None
            else _LazyDependency(WriteModeManager)
        )
        self.atlas_registry = atlas_registry or load_bundled_registry()
        self.atlas_inspector = atlas_inspector or inspect_loaded_atlas
        self.history = history or LocalSoundSelectionHistory()
        self.palette_registry = palette_registry or SoundPaletteStateRegistry()
        self._now = now or _utc_now
        if type(write_mode_already_enabled) is not bool:
            raise ValueError("write_mode_already_enabled must be true or false")
        self._write_mode_already_enabled = write_mode_already_enabled
        self._write_mode_session: str | None = None
        self._write_mode_enabled = False
        self._inventories: dict[str, SoundInventory] = {}
        self._persist_history: dict[str, bool] = {}
        self._verified_receipts: dict[str, tuple[VerifiedPluginPresetSelection, ...]] = {}

    # ------------------------------------------------------------------
    # Read-only inventory and planning
    # ------------------------------------------------------------------

    def inventory(
        self,
        request: SoundSelectionRequest | None = None,
        *,
        only_used: bool = False,
        include_effects: bool | None = None,
        preset_start: int = 0,
        preset_limit: int = 64,
        include_current: bool = True,
        include_empty_names: bool = False,
        include_pad_maps: bool = True,
        include_atlas: bool = True,
        discover_presets: bool = False,
        discovery_policy: PresetCandidateDiscoveryPolicy | Mapping[str, Any] | None = None,
    ) -> SoundInventory:
        """Read one compact loaded-target inventory.

        The target scan is intentionally performed exactly once and is passed
        into Atlas matching.  Preset/current/pad reads are all bounded per
        target and are never replaced by a static Atlas claim.  Callers can
        opt into bounded multi-page discovery; normal plan calls do so, while
        a direct inventory read retains the historical one-page behavior unless
        discover_presets=True is supplied.
        """

        if type(only_used) is not bool:
            raise ValueError("only_used must be true or false")
        if include_effects is not None and type(include_effects) is not bool:
            raise ValueError("include_effects must be true, false, or omitted")
        if type(include_current) is not bool or type(include_empty_names) is not bool:
            raise ValueError("include_current and include_empty_names must be booleans")
        if type(include_pad_maps) is not bool or type(include_atlas) is not bool:
            raise ValueError("include_pad_maps and include_atlas must be booleans")
        if type(discover_presets) is not bool:
            raise ValueError("discover_presets must be true or false")
        if (
            type(preset_start) is not int
            or isinstance(preset_start, bool)
            or not 0 <= preset_start <= MAX_REPORTED_PRESET_COUNT
        ):
            raise ValueError(
                f"preset_start must be between 0 and {MAX_REPORTED_PRESET_COUNT}"
            )
        if type(preset_limit) is not int or isinstance(preset_limit, bool) or not 1 <= preset_limit <= MAX_PRESET_PAGE:
            raise ValueError(f"preset_limit must be between 1 and {MAX_PRESET_PAGE}")
        request_model = None
        if request is not None:
            request_model = request if isinstance(request, SoundSelectionRequest) else SoundSelectionRequest.model_validate(request)
        effective_include_effects = (
            bool(request_model.allow_effect_presets)
            if include_effects is None and request_model is not None
            else bool(include_effects)
        )
        loaded = _as_loaded(self.inspector.scan_loaded_plugins(only_used=only_used))
        warnings: list[str] = list(_field(loaded, "warnings", ()))
        atlas_response: Any = None
        atlas_views: dict[str, _AtlasTargetView] = {}
        if include_atlas:
            try:
                atlas_response = self.atlas_inspector(
                    AtlasInspectLoadedRequest(only_used=only_used),
                    registry=self.atlas_registry,
                    inventory=loaded,
                )
                atlas_views = _atlas_views(atlas_response, self.atlas_registry)
                warnings.extend(_field(atlas_response, "warnings", ()))
            except Exception as exc:
                # Atlas is enrichment, never the loaded/control authority.  A
                # malformed optional catalog must not hide a live target.
                warnings.append(f"Atlas matching was unavailable for this inventory: {exc}")

        target_rows: list[SoundTargetInventory] = []
        target_coverages: list[SoundPresetDiscoveryCoverage] = []
        observed_sessions: list[str] = []
        matched_products: set[str] = set()
        project_key = None if request_model is None else request_model.project_key
        palette_state = self.palette_registry.current(project_key=project_key)
        requested_preset_names: tuple[str, ...] = ()
        excluded_preset_names: tuple[str, ...] = ()
        if request_model is not None:
            requested_preset_names = tuple(
                dict.fromkeys(
                    (
                        *request_model.preset_preferences,
                        *(
                            preset
                            for role in request_model.roles
                            for preset in role.preferred_presets
                        ),
                    )
                )
            )
            excluded_preset_names = tuple(
                dict.fromkeys(
                    (
                        *request_model.preset_exclusions,
                        *(
                            preset
                            for role in request_model.roles
                            for preset in role.excluded_presets
                        ),
                    )
                )
            )
        for summary in tuple(loaded.plugins):
            target = summary.target
            key = _target_key(target)
            atlas = atlas_views.get(key, _AtlasTargetView())
            product = atlas.product
            match = atlas.match
            product_id = _field(product, "product_id", None)
            product_name = _field(product, "name", None) or summary.name or summary.user_name or "Unknown loaded plug-in"
            product_origin = _field(product, "origin", "unknown")
            if product_origin not in {"stock", "third_party", "unknown"}:
                product_origin = "unknown"
            if isinstance(product_id, str):
                matched_products.add(product_id)
            if (
                getattr(target, "kind", None) == "mixer_effect"
                and not effective_include_effects
            ):
                continue
            target_warnings: list[str] = []
            if product is None and _field(match, "product_id", None):
                target_warnings.append("Atlas matched a product ID that was not present in the selected registry")
            if match is not None:
                target_warnings.append("Atlas product matching is descriptive; it is not loaded, ownership, or control proof")

            page: Any = None
            current: Any = None
            pad_map: Any = None
            if hasattr(self.inspector, "list_plugin_presets"):
                try:
                    page = _as_page(
                        self.inspector.list_plugin_presets(
                            **_target_kwargs(target),
                            start=preset_start,
                            limit=preset_limit,
                            include_current=include_current,
                            include_empty_names=include_empty_names,
                        )
                    )
                except Exception as exc:
                    target_warnings.append(f"bounded preset page unavailable: {exc}")
            if include_current and hasattr(self.inspector, "get_plugin_current_preset"):
                try:
                    current = _as_current(
                        self.inspector.get_plugin_current_preset(**_target_kwargs(target))
                    )
                except Exception as exc:
                    target_warnings.append(f"current preset observation unavailable: {exc}")
            if include_pad_maps and getattr(target, "kind", None) == "channel_generator" and hasattr(self.inspector, "inspect_plugin_pad_map"):
                try:
                    pad_map = _as_pad_map(
                        self.inspector.inspect_plugin_pad_map(**_target_kwargs(target))
                    )
                except Exception as exc:
                    target_warnings.append(f"pad-map observation unavailable: {exc}")

            page_summary = _summary_from_observation(page, summary)
            current_summary = _summary_from_observation(current, page_summary)
            pad_summary = _summary_from_observation(pad_map, current_summary)
            identity, identity_warnings = _stable_one(
                _identity_values(summary, page_summary, current_summary, pad_summary)
            )
            target_warnings.extend(identity_warnings)
            sessions = _session_values(page, current, pad_map)
            observed_sessions.extend(sessions)
            current_state = _status_current(current) if current is not None else _status_current(page)
            page_state = _status_current(page)
            if current_state is None:
                current_state = page_state
            rows = _preset_rows(page)
            preset_names = tuple(row[0] for row in rows)
            preset_indices = tuple(row[1] for row in rows)
            preset_count = _field(page, "preset_count", None)
            if preset_count is None:
                preset_count = _field(current, "preset_count", None)
            current_name = None if current_state is None else current_state.name
            current_index = None if current_state is None else current_state.index
            status = None if current_state is None else current_state.identity_status
            if status == "unsupported" or status == "unresolved":
                current_name = None
                current_index = None
            preset_readback = status == "stable"
            preset_identity_stable = status == "stable"
            preset_navigation = isinstance(preset_count, int) and preset_count > 0
            preset_coverage: SoundPresetDiscoveryCoverage | None = None
            if discover_presets:
                page_loader: Callable[..., Any] | None = None
                if callable(getattr(self.inspector, "list_plugin_presets", None)):
                    initial_start = _field(page, "start", preset_start)
                    initial_limit = _field(page, "limit", preset_limit)

                    def load_page(
                        *,
                        start: int,
                        limit: int,
                        _initial_page: Any = page,
                        _initial_start: int = initial_start,
                        _initial_limit: int = initial_limit,
                        _target: Any = target,
                    ) -> Any:
                        if (
                            _initial_page is not None
                            and start == _initial_start
                            and limit == _initial_limit
                        ):
                            return _initial_page
                        return _as_page(
                            self.inspector.list_plugin_presets(
                                **_target_kwargs(_target),
                                start=start,
                                limit=limit,
                                include_current=include_current,
                                include_empty_names=include_empty_names,
                            )
                        )

                    page_loader = load_page
                exact_lookup = _exact_preset_loader(self.inspector, target)
                try:
                    discovered = discover_preset_candidates(
                        catalog=page,
                        policy=discovery_policy,
                        page_loader=page_loader,
                        exact_lookup=exact_lookup,
                        requested_presets=requested_preset_names,
                        preferred_presets=requested_preset_names,
                        excluded_presets=excluded_preset_names,
                        anchors=(
                            ()
                            if palette_state is None
                            else palette_state.assignments
                        ),
                        history=self.history,
                        current_preset=current_name if include_current else None,
                        current_preset_index=current_index if include_current else None,
                        reported_preset_count=preset_count,
                        seed=0 if request_model is None else request_model.seed,
                    )
                    preset_coverage = SoundPresetDiscoveryCoverage.model_validate(
                        discovered.coverage.model_dump(mode="json", exclude_none=False)
                    )
                    target_coverages.append(preset_coverage)
                    if discovered.presets:
                        preset_names = tuple(
                            item.name for item in discovered.presets if item.name
                        )
                        preset_indices = tuple(
                            item.index for item in discovered.presets if item.name
                        )
                    if preset_coverage.reported_preset_count is not None:
                        preset_count = preset_coverage.reported_preset_count
                    target_warnings.extend(preset_coverage.limitations)
                except Exception as exc:
                    target_warnings.append(
                        f"bounded preset discovery unavailable: {exc}"
                    )
            converted_pad_map = convert_plugin_pad_map(
                pad_map,
                target,
                target_fingerprint=identity,
            )
            if pad_map is not None and converted_pad_map is None:
                target_warnings.append("the reported pad map could not be represented by the strict Sound Selection contract")
            target_warnings.extend(_field(page, "warnings", ()))
            target_warnings.extend(_field(current, "warnings", ()))
            target_warnings.extend(_field(pad_map, "warnings", ()))
            if getattr(target, "kind", None) == "mixer_effect":
                target_warnings.append("pad-map reads are limited to Channel Rack generators")
            descriptors = descriptors_for_product(product) if product is not None else ()
            atlas_confidence = _field(match, "confidence", "unknown") or "unknown"
            if atlas_confidence not in {"high", "medium", "low", "unknown"}:
                atlas_confidence = "unknown"
            aliases = tuple(
                item
                for item in dict.fromkeys(
                    item
                    for item in (
                        summary.name,
                        summary.user_name,
                        *_field(product, "aliases", ()),
                    )
                    if isinstance(item, str) and item and item != product_name
                )
            )
            target_rows.append(
                SoundTargetInventory(
                    target=target,
                    target_fingerprint=identity,
                    product_id=product_id,
                    product_name=product_name,
                    product_aliases=aliases,
                    product_origin=product_origin,
                    current_preset=current_name,
                    current_preset_index=current_index,
                    reported_parameter_count=summary.reported_parameter_count,
                    preset_count=preset_count,
                    preset_names=preset_names,
                    preset_indices=preset_indices,
                    descriptors=descriptor_names(descriptors),
                    descriptor_provenance=descriptors,
                    atlas_product=product,
                    atlas_product_id=product_id,
                    atlas_categories=tuple(_field(product, "categories", ())),
                    atlas_common_roles=tuple(
                        dict.fromkeys(
                            (
                                *_field(product, "common_instruments", ()),
                                *_field(product, "common_track_types", ()),
                            )
                        )
                    ),
                    atlas_technique_ids=tuple(_field(product, "technique_ids", ())),
                    # Atlas adapters are descriptive.  Only Track B's actual
                    # read/write surface can establish runtime availability.
                    control_adapter_id=None,
                    atlas_confidence=atlas_confidence,
                    adapter_available=False,
                    preset_navigation_available=preset_navigation,
                    preset_readback_available=preset_readback,
                    preset_identity_stable=preset_identity_stable,
                    preset_discovery_coverage=preset_coverage,
                    pad_map=converted_pad_map,
                    warnings=_dedupe(target_warnings, limit=MAX_TARGET_WARNINGS),
                )
            )

        session, session_warnings = _stable_one(observed_sessions)
        warnings.extend(session_warnings)
        if session is None and observed_sessions:
            warnings.append("live target reads did not agree on one session fingerprint")
        if session is None:
            candidate_session = _field(loaded, "session_fingerprint", None)
            if isinstance(candidate_session, str) and candidate_session:
                session = candidate_session
            else:
                warnings.append("live inventory did not expose a session fingerprint")

        current_palette_id = (
            None
            if request_model is None
            else request_model.current_palette_id
        ) or (None if palette_state is None else palette_state.palette_id)
        locked_roles: tuple[str, ...] = ()
        if palette_state is not None:
            locked_roles = tuple(
                item.role_id
                for item in palette_state.assignments
                if item.locked or item.assignment_id in palette_state.locked_assignments
            )
        try:
            atlas_products = tuple(self.atlas_registry.products)
        except AttributeError:
            atlas_products = ()
        unloaded = tuple(
            product.product_id
            for product in atlas_products
            if product.product_id not in matched_products
        )[:512]
        return SoundInventory(
            observed_at=_stamp(loaded.observed_at),
            session_fingerprint=session,
            loaded_generators=tuple(row for row in target_rows if row.target.kind == "channel_generator"),
            loaded_effects=tuple(row for row in target_rows if row.target.kind == "mixer_effect"),
            current_palette_id=current_palette_id,
            locked_roles=locked_roles,
            preset_discovery_coverage=tuple(target_coverages),
            known_unloaded_products=unloaded,
            warnings=_dedupe(warnings, limit=MAX_SERVICE_WARNINGS),
        )

    def plan(
        self,
        request: SoundSelectionRequest,
        inventory: SoundInventory | None = None,
        *,
        existing: SoundPaletteState | SoundPalettePlan | Sequence[SoundPaletteAssignment] | None = None,
    ) -> SoundPalettePlan:
        """Build and register a deterministic, read-only palette plan."""

        request_model = request if isinstance(request, SoundSelectionRequest) else SoundSelectionRequest.model_validate(request)
        observed = self.inventory(
            request_model,
            discover_presets=True,
        ) if inventory is None else (
            inventory if isinstance(inventory, SoundInventory) else SoundInventory.model_validate(inventory)
        )
        prior = existing
        if prior is None and request_model.current_palette_id and request_model.preserve_existing_roles:
            prior = self.palette_registry.get(request_model.current_palette_id)
        plan = plan_palette(request_model, observed, existing=prior, history=self.history)
        self.palette_registry.register_plan(plan, now=_stamp(self._now()))
        self._inventories[plan.palette_id] = observed
        self._persist_history[plan.palette_id] = request_model.persist_history
        self._trim_runtime_maps()
        return plan

    def get(self, palette_id: str) -> SoundPaletteState:
        """Return one process-local palette state without a bridge read."""

        if not isinstance(palette_id, str) or not palette_id.strip():
            raise ValueError("palette_id must contain text")
        return self.palette_registry.require(palette_id)

    def lookup(self, palette_id: str) -> SoundPaletteLookup:
        """Return a strict, non-throwing process-local palette lookup."""

        if not isinstance(palette_id, str) or not palette_id.strip():
            return SoundPaletteLookup(
                found=False,
                message="palette_id must contain text; no process-local palette was looked up",
            )
        state = self.palette_registry.get(palette_id)
        if state is None:
            return SoundPaletteLookup(
                found=False,
                message=(
                    f"Sound Palette {palette_id!r} is not in this process-local registry; "
                    "it may have expired or been created by another process"
                ),
            )
        return SoundPaletteLookup(
            found=True,
            message="Sound Palette state is available in this process-local registry",
            state=state,
        )

    def create_variation(
        self,
        palette_id: str,
        request: SoundSelectionRequest,
        section: str | None = None,
        replace_roles: Sequence[str] = (),
        *,
        inventory: SoundInventory | None = None,
    ) -> SoundPaletteVariationPlan:
        """Create a section delta while preserving the registered base state."""

        base = self.palette_registry.require(palette_id)
        request_model = request if isinstance(request, SoundSelectionRequest) else SoundSelectionRequest.model_validate(request)
        observed = self.inventory(
            request_model,
            discover_presets=True,
        ) if inventory is None else (
            inventory if isinstance(inventory, SoundInventory) else SoundInventory.model_validate(inventory)
        )
        variation = create_palette_variation(
            base,
            request_model,
            observed,
            section=section,
            history=self.history,
            replace_roles=replace_roles,
        )
        self.palette_registry.record_variation(
            palette_id,
            variation,
            now=_stamp(self._now()),
        )
        return variation

    # ------------------------------------------------------------------
    # Verified application
    # ------------------------------------------------------------------

    def _ensure_session(self, value: str | None) -> str:
        if not isinstance(value, str) or SESSION_RE.fullmatch(value) is None:
            raise SoundSelectionSessionError(
                "sound_selection_apply requires a non-null 32-character lowercase session fingerprint"
            )
        return value

    def _resolve_apply_input(
        self,
        value: SoundPalettePlan | SoundPaletteState | SoundPaletteVariationPlan | str,
    ) -> tuple[
        str,
        tuple[SoundPaletteAssignment, ...],
        str | None,
        str | None,
        tuple[str, ...],
    ]:
        if isinstance(value, SoundPalettePlan):
            state = self.palette_registry.register_plan(
                value, now=_stamp(self._now())
            )
            return (
                state.palette_id,
                state.assignments,
                state.session_identity,
                value.plan_digest,
                tuple(dict.fromkeys((*state.blockers, *value.blockers))),
            )
        if isinstance(value, SoundPaletteState):
            state = self.palette_registry.get(value.palette_id)
            if state is None:
                raise KeyError(
                    "caller-supplied palette state is not in this process-local registry"
                )
            if state != value:
                raise ValueError(
                    "caller-supplied palette state does not match immutable process-local state"
                )
            return (
                state.palette_id,
                state.assignments,
                state.session_identity,
                None,
                state.blockers,
            )
        if isinstance(value, SoundPaletteVariationPlan):
            state = self.palette_registry.require(value.base_palette_id)
            self.palette_registry.record_variation(
                value.base_palette_id,
                value,
                now=_stamp(self._now()),
            )
            return (
                value.base_palette_id,
                value.assignments,
                state.session_identity,
                value.plan_digest,
                tuple(dict.fromkeys((*state.blockers, *value.blockers))),
            )
        if isinstance(value, str) and value.strip():
            state = self.palette_registry.require(value)
            return (
                state.palette_id,
                state.assignments,
                state.session_identity,
                None,
                state.blockers,
            )
        raise TypeError(
            "apply expects a SoundPalettePlan, SoundPaletteState, "
            "SoundPaletteVariationPlan, or palette ID"
        )

    def _scan_preflight(
        self,
        assignments: Sequence[SoundPaletteAssignment],
        session_fingerprint: str,
    ) -> _Preflight:
        result = _Preflight()
        if not hasattr(self.inspector, "scan_loaded_plugins"):
            result.blockers.append(
                "live target revalidation is unavailable; no preset mutation was attempted"
            )
            return result
        if not hasattr(self.inspector, "connection_info"):
            result.blockers.append(
                "live session revalidation is unavailable; no preset mutation was attempted"
            )
            return result
        try:
            connection = self.inspector.connection_info()
        except Exception as exc:
            result.blockers.append(f"live session revalidation failed: {exc}")
            return result
        if _field(connection, "connected", False) is not True or _field(
            connection, "compatible", False
        ) is not True:
            result.blockers.append("the live FL bridge is unavailable or incompatible")
            return result
        observed_connection_session = _field(connection, "session_fingerprint", None)
        if observed_connection_session != session_fingerprint:
            result.blockers.append(
                "the live FL bridge session does not match the requested session fingerprint"
            )
            return result
        try:
            live = _as_loaded(self.inspector.scan_loaded_plugins(only_used=False))
        except Exception as exc:
            result.blockers.append(f"live target revalidation failed: {exc}")
            return result
        rows = {_target_key(item.target): item for item in live.plugins}
        scan_session = _field(live, "session_fingerprint", None)
        if isinstance(scan_session, str) and scan_session and scan_session != session_fingerprint:
            result.blockers.append("live inventory session does not match the requested session fingerprint")
        for assignment in sorted(assignments, key=lambda item: (item.role_id.casefold(), item.assignment_id)):
            if assignment.target is None:
                result.blockers.append(f"role {assignment.role_id!r} has no resolved live target")
                continue
            live_summary = rows.get(_target_key(assignment.target))
            if live_summary is None:
                result.blockers.append(f"role {assignment.role_id!r} target is no longer loaded")
                continue
            live_fp = _field(live_summary, "target_fingerprint", None)
            if assignment.target_fingerprint is None or live_fp is None:
                result.blockers.append(
                    f"role {assignment.role_id!r} target identity proof is unavailable"
                )
            elif assignment.target_fingerprint != live_fp:
                result.blockers.append(f"role {assignment.role_id!r} target identity changed")
        return result

    def _cached_inventory_preflight(
        self,
        assignments: Sequence[SoundPaletteAssignment],
        session_fingerprint: str,
        inventory: SoundInventory,
    ) -> _Preflight:
        """Validate assignment identities against the run's one inventory scan.

        Production Runs already perform a session check immediately before the
        palette phase, and each preset setter performs its own narrow target
        and readback checks.  Reusing this immutable inventory avoids a second
        full loaded-plug-in scan without weakening those mutation boundaries.
        """

        result = _Preflight()
        if inventory.session_fingerprint != session_fingerprint:
            result.blockers.append(
                "the cached inventory session does not match the requested session fingerprint"
            )
            return result
        rows = {
            _target_key(item.target): item
            for item in inventory.loaded_targets
        }
        for assignment in sorted(
            assignments,
            key=lambda item: (item.role_id.casefold(), item.assignment_id),
        ):
            if assignment.target is None:
                result.blockers.append(
                    f"role {assignment.role_id!r} has no resolved live target"
                )
                continue
            cached = rows.get(_target_key(assignment.target))
            if cached is None:
                result.blockers.append(
                    f"role {assignment.role_id!r} target was not present in the readiness inventory"
                )
                continue
            if (
                assignment.target_fingerprint is None
                or cached.target_fingerprint is None
            ):
                result.blockers.append(
                    f"role {assignment.role_id!r} target identity proof is unavailable"
                )
            elif assignment.target_fingerprint != cached.target_fingerprint:
                result.blockers.append(
                    f"role {assignment.role_id!r} target identity changed before palette planning"
                )
        return result

    def _read_current_for_target(
        self,
        target: Any,
        session_fingerprint: str,
        preflight: _Preflight,
        *,
        target_fingerprint: str | None,
    ) -> PluginPresetState | None:
        key = _target_key(target)
        if key in preflight.current:
            return preflight.current[key]
        if not hasattr(self.inspector, "get_plugin_current_preset"):
            preflight.blockers.append(
                f"current preset revalidation is unavailable for {key}"
            )
            return None
        try:
            current = _as_current(
                self.inspector.get_plugin_current_preset(**_target_kwargs(target))
            )
        except Exception as exc:
            preflight.blockers.append(f"current preset revalidation failed for {key}: {exc}")
            return None

        # The target scan and this current-preset read are separate live
        # observations.  FL can replace a plug-in at the same channel/slot
        # between them, so the nested plug-in proof on the current read must
        # still identify the exact assignment target from the scan.  Checking
        # only the requested session or the address is insufficient here.
        blocker_count = len(preflight.blockers)
        plugin = _field(current, "plugin", None)
        observed_target = _field(plugin, "target", None)
        if observed_target is None or _target_key(observed_target) != key:
            preflight.blockers.append(
                f"current preset read identified a different live target for {key}"
            )
        observed_target_fingerprint = _field(plugin, "target_fingerprint", None)
        if (
            target_fingerprint is None
            or not isinstance(observed_target_fingerprint, str)
            or not observed_target_fingerprint
        ):
            preflight.blockers.append(
                f"current preset read did not prove the assigned target identity for {key}"
            )
        elif observed_target_fingerprint != target_fingerprint:
            preflight.blockers.append(
                f"current preset read target identity changed for {key}"
            )
        observed_session = _field(current, "session_fingerprint", None)
        if observed_session != session_fingerprint:
            preflight.blockers.append(
                "current preset read did not prove the requested session boundary"
            )
        state = _status_current(current)
        if state is None or state.identity_status != "stable":
            preflight.blockers.append(
                f"current preset identity is not stable for {key}"
            )
        elif len(preflight.blockers) == blocker_count:
            preflight.current[key] = state
        return state

    def _ensure_write_mode(self, session_fingerprint: str) -> None:
        if self._write_mode_enabled and self._write_mode_session == session_fingerprint:
            return
        manager: Any = self.write_mode_manager
        if manager is None:
            raise SoundSelectionExecutionError("no write-mode boundary was configured")
        if hasattr(manager, "set_write_mode"):
            change = manager.set_write_mode(
                enabled=True,
                confirm_user_present=True,
                session_fingerprint=session_fingerprint,
            )
            enabled = _field(change, "after_enabled", _field(change, "verified_writes_enabled", True))
            if enabled is not True:
                raise SoundSelectionExecutionError("write-mode transition was not verified as enabled")
        elif hasattr(manager, "ensure_enabled"):
            enabled = manager.ensure_enabled(session_fingerprint=session_fingerprint, confirm_user_present=True)
            if enabled is False:
                raise SoundSelectionExecutionError("write-mode boundary did not enable writes")
        elif hasattr(manager, "enable"):
            enabled = manager.enable(session_fingerprint=session_fingerprint, confirm_user_present=True)
            if enabled is False:
                raise SoundSelectionExecutionError("write-mode boundary did not enable writes")
        else:
            raise SoundSelectionExecutionError("injected write-mode manager has no enable operation")
        self._write_mode_session = session_fingerprint
        self._write_mode_enabled = True

    @staticmethod
    def _coerce_selection_receipt(value: Any) -> VerifiedPluginPresetSelection | None:
        if isinstance(value, VerifiedPluginPresetSelection):
            return value
        if isinstance(value, dict):
            try:
                return VerifiedPluginPresetSelection.model_validate(value, strict=False)
            except Exception:
                return None
        return None

    @staticmethod
    def _receipt_for(
        assignment: SoundPaletteAssignment,
        *,
        verified: bool,
        summary: str,
        selected_preset: str | None = None,
        warnings: Iterable[str] = (),
    ) -> PaletteApplyReceipt:
        return PaletteApplyReceipt(
            assignment_id=assignment.assignment_id,
            role_id=assignment.role_id,
            verified=verified,
            verification_summary=summary or "Sound Selection did not receive a verified result.",
            selected_preset=selected_preset,
            warnings=_dedupe(warnings, limit=32),
        )

    @staticmethod
    def _receipt_matches_assignment(
        assignment: SoundPaletteAssignment,
        receipt: VerifiedPluginPresetSelection,
    ) -> str | None:
        """Return a blocker when verified readback contradicts the exact plan.

        Track B validates that a verified receipt is internally consistent with
        the request sent to FL.  Sound Selection has one more boundary: the
        request sent to Track B came from an immutable palette assignment.  A
        buggy adapter (or a replacement target) must not be able to turn a
        different but internally-consistent preset receipt into a successful
        role assignment.
        """

        checks = (
            (
                "requested preset name",
                receipt.requested_preset_name,
                assignment.selected_preset,
            ),
            (
                "after preset name",
                receipt.after.name,
                assignment.selected_preset,
            ),
            (
                "requested preset index",
                receipt.requested_preset_index,
                assignment.selected_preset_index,
            ),
            (
                "after preset index",
                receipt.after.index,
                assignment.selected_preset_index,
            ),
        )
        for label, observed, expected in checks:
            # An assignment may intentionally identify a preset by only its
            # name or only its index.  Extra identity fields supplied by FL are
            # useful evidence in that case; fields explicitly present in the
            # assignment, however, must match exactly.
            if expected is not None and observed != expected:
                return (
                    f"role {assignment.role_id!r} {label} contradicts the exact palette assignment"
                )
        return None

    def _finish_apply(
        self,
        palette_id: str,
        *,
        session_fingerprint: str,
        selection_receipts: Sequence[VerifiedPluginPresetSelection],
        new_assignment_receipts: Sequence[PaletteApplyReceipt],
        assignment_scope: Sequence[SoundPaletteAssignment],
        failed_assignment_id: str | None,
        warnings: Sequence[str],
        blockers: Sequence[str],
        history_written: int,
        status: Literal["planned", "applying", "applied", "partially_applied", "failed"],
    ) -> SoundSelectionApplyResult:
        if new_assignment_receipts:
            state = self.palette_registry.record_receipts(
                palette_id,
                new_assignment_receipts,
                status=status,
                now=_stamp(self._now()),
            )
        else:
            state = self.palette_registry.record_receipts(
                palette_id,
                (),
                status=status,
                now=_stamp(self._now()),
            )
        state = state.model_copy(
            update={
                "warnings": _dedupe((*state.warnings, *warnings), limit=MAX_SERVICE_WARNINGS),
                "blockers": _dedupe((*state.blockers, *blockers), limit=MAX_SERVICE_WARNINGS),
            }
        )
        self.palette_registry.put(state)
        exact_prior = self._verified_receipts.get(palette_id, ())
        combined_exact = tuple(exact_prior) + tuple(selection_receipts)
        self._verified_receipts[palette_id] = combined_exact
        self._trim_runtime_maps()
        return SoundSelectionApplyResult(
            palette_id=palette_id,
            status=status,
            session_fingerprint=session_fingerprint,
            state=state,
            assignment_scope=tuple(assignment_scope),
            receipts=tuple(selection_receipts),
            assignment_receipts=state.apply_receipts,
            failed_assignment_id=failed_assignment_id,
            verified_count=sum(1 for item in state.apply_receipts if item.verified),
            history_written=history_written,
            warnings=_dedupe(warnings, limit=MAX_SERVICE_WARNINGS),
            blockers=_dedupe(blockers, limit=MAX_SERVICE_WARNINGS),
        )

    def apply(
        self,
        plan_or_id: SoundPalettePlan | SoundPaletteState | SoundPaletteVariationPlan | str,
        session_fingerprint: str | None,
        authorized_to_modify: bool,
        *,
        role_ids: Sequence[str] = (),
        max_navigation_steps: int = 64,
        settle_tick_limit: int = 1,
        persist_history: bool | None = None,
        write_mode_already_enabled: bool | None = None,
        inventory: SoundInventory | None = None,
    ) -> SoundSelectionApplyResult:
        """Apply one plan using verified, deterministic preset mutations."""

        if type(authorized_to_modify) is not bool:
            raise ValueError("authorized_to_modify must be true or false")
        if not authorized_to_modify:
            raise SoundSelectionAuthorizationError(
                "sound_selection_apply requires explicit authorization to modify FL Studio"
            )
        session = self._ensure_session(session_fingerprint)
        (
            palette_id,
            assignments,
            expected_session,
            plan_digest,
            input_blockers,
        ) = self._resolve_apply_input(plan_or_id)
        if expected_session is None or not isinstance(expected_session, str) or not expected_session:
            raise SoundSelectionSessionError(
                "palette has no session identity; make a fresh live plan before applying"
            )
        if expected_session != session:
            raise SoundSelectionSessionError(
                "requested session fingerprint does not match the palette's live inventory session"
            )
        if isinstance(role_ids, (str, bytes)):
            raise ValueError("role_ids must be a bounded sequence of role identifiers")
        try:
            requested_role_ids = tuple(role_ids)
        except TypeError as exc:
            raise ValueError("role_ids must be a bounded sequence of role identifiers") from exc
        if len(requested_role_ids) > MAX_ROLE_COUNT:
            raise ValueError("role_ids exceeds the Sound Selection role bound")
        if any(not isinstance(item, str) or not item.strip() for item in requested_role_ids):
            raise ValueError("role_ids entries must contain text")
        role_keys = tuple(item.casefold() for item in requested_role_ids)
        if len(set(role_keys)) != len(role_keys):
            raise ValueError("role_ids must not contain duplicates")
        if type(max_navigation_steps) is not int or isinstance(max_navigation_steps, bool) or not 0 <= max_navigation_steps <= 256:
            raise ValueError("max_navigation_steps is outside the verified preset bound")
        if type(settle_tick_limit) is not int or isinstance(settle_tick_limit, bool) or not 1 <= settle_tick_limit <= 8:
            raise ValueError("settle_tick_limit is outside the verified preset bound")
        state = self.palette_registry.require(palette_id)
        persist = (
            self._persist_history.get(palette_id, True)
            if persist_history is None
            else persist_history
        )
        if type(persist) is not bool:
            raise ValueError("persist_history must be true, false, or omitted")
        previous = {item.assignment_id: item for item in state.apply_receipts}
        available_role_keys = {item.role_id.casefold() for item in assignments}
        missing_role_keys = tuple(item for item in role_keys if item not in available_role_keys)
        if missing_role_keys:
            raise ValueError(
                "role_ids contains unknown palette roles: " + ", ".join(missing_role_keys)
            )
        selected_role_keys = set(role_keys)
        selected_assignments = (
            tuple(item for item in assignments if item.role_id.casefold() in selected_role_keys)
            if role_keys
            else tuple(assignments)
        )
        ordered = tuple(sorted(selected_assignments, key=lambda item: (item.role_id.casefold(), item.assignment_id)))
        pending_receipt_ids = {
            item.assignment_id
            for item in ordered
            if item.assignment_id not in previous
        }
        if len(previous) + len(pending_receipt_ids) > MAX_ROLE_COUNT:
            return self._finish_apply(
                palette_id,
                session_fingerprint=session,
                selection_receipts=(),
                new_assignment_receipts=(),
                assignment_scope=ordered,
                failed_assignment_id=None,
                warnings=state.warnings,
                blockers=(
                    "the process-local palette receipt bound is full; no FL mutation was attempted",
                ),
                history_written=0,
                status="failed",
            )
        initial_blockers = _dedupe(
            (*state.blockers, *input_blockers), limit=MAX_SERVICE_WARNINGS
        )
        if initial_blockers:
            return self._finish_apply(
                palette_id,
                session_fingerprint=session,
                selection_receipts=(),
                new_assignment_receipts=(),
                assignment_scope=ordered,
                failed_assignment_id=None,
                warnings=state.warnings,
                blockers=initial_blockers,
                history_written=0,
                status="failed",
            )
        preflight = (
            self._scan_preflight(ordered, session)
            if inventory is None
            else self._cached_inventory_preflight(ordered, session, inventory)
        )
        if preflight.blockers:
            failed = ordered[0].assignment_id if ordered else None
            return self._finish_apply(
                palette_id,
                session_fingerprint=session,
                selection_receipts=(),
                new_assignment_receipts=(),
                assignment_scope=ordered,
                failed_assignment_id=failed,
                warnings=preflight.warnings,
                blockers=preflight.blockers,
                history_written=0,
                status="failed",
            )
        needs_write = any(
            item.selection_action in {"select_preset", "loop_starter_reroll"}
            and item.assignment_id not in previous
            for item in ordered
        )
        already_enabled = (
            self._write_mode_already_enabled
            if write_mode_already_enabled is None
            else write_mode_already_enabled
        )
        if type(already_enabled) is not bool:
            raise ValueError("write_mode_already_enabled must be true, false, or omitted")
        if needs_write:
            try:
                if already_enabled:
                    self._write_mode_session = session
                    self._write_mode_enabled = True
                else:
                    self._ensure_write_mode(session)
            except Exception as exc:
                return self._finish_apply(
                    palette_id,
                    session_fingerprint=session,
                    selection_receipts=(),
                    new_assignment_receipts=(),
                    assignment_scope=ordered,
                    failed_assignment_id=ordered[0].assignment_id if ordered else None,
                    warnings=preflight.warnings,
                    blockers=(f"write-mode enablement failed: {exc}",),
                    history_written=0,
                    status="failed",
                )

        new_receipts: list[PaletteApplyReceipt] = []
        exact_receipts: list[VerifiedPluginPresetSelection] = []
        warnings = list(preflight.warnings)
        blockers: list[str] = []
        history_written = 0
        failed_assignment_id: str | None = None
        failed = False

        def record_verified_usage(
            assignment: SoundPaletteAssignment,
            preset_name: str | None,
        ) -> None:
            nonlocal history_written
            if (
                not persist
                or not assignment.product_id
                or not assignment.preset_identity_digest
                or not preset_name
            ):
                return
            try:
                if self.history.record_usage(
                    assignment.product_id,
                    assignment.preset_identity_digest,
                    assignment.role_id,
                    preset_name=preset_name,
                    palette_digest=plan_digest,
                    now=_stamp(self._now()),
                    persist=True,
                ):
                    history_written += 1
            except Exception as exc:
                warnings.append(
                    f"usage history was not written for role {assignment.role_id!r}: {exc}"
                )

        for assignment in ordered:
            prior_receipt = previous.get(assignment.assignment_id)
            if prior_receipt is not None:
                # Completed receipts are immutable.  An unverified prior
                # outcome is a stop condition, never an invitation to retry.
                if not prior_receipt.verified:
                    failed = True
                    failed_assignment_id = assignment.assignment_id
                    blockers.append(f"role {assignment.role_id!r} already has an unverified receipt; no retry was attempted")
                    break
                continue

            current = (
                self._read_current_for_target(
                    assignment.target,
                    session,
                    preflight,
                    target_fingerprint=assignment.target_fingerprint,
                )
                if assignment.target is not None
                and assignment.selection_action != "loop_starter_reroll"
                else None
            )
            if preflight.blockers:
                failed = True
                failed_assignment_id = assignment.assignment_id
                blockers.extend(preflight.blockers)
                break
            if assignment.selection_action == "keep_current":
                if current is None or current.identity_status != "stable":
                    failed = True
                    failed_assignment_id = assignment.assignment_id
                    blockers.append(
                        f"role {assignment.role_id!r} current preset identity is unknown; no keep-current proof was recorded"
                    )
                    break
                if current is not None and current.identity_status == "stable":
                    if assignment.selected_preset is not None and current.name != assignment.selected_preset:
                        failed = True
                        failed_assignment_id = assignment.assignment_id
                        blockers.append(f"role {assignment.role_id!r} current preset no longer matches the plan")
                        break
                    if assignment.selected_preset_index is not None and current.index != assignment.selected_preset_index:
                        failed = True
                        failed_assignment_id = assignment.assignment_id
                        blockers.append(f"role {assignment.role_id!r} current preset index no longer matches the plan")
                        break
                new_receipts.append(
                    self._receipt_for(
                        assignment,
                        verified=True,
                        summary="Kept the currently observed preset; no FL mutation was dispatched.",
                        selected_preset=assignment.selected_preset,
                    )
                )
                record_verified_usage(assignment, assignment.selected_preset)
                continue

            if assignment.target is None:
                failed = True
                failed_assignment_id = assignment.assignment_id
                blockers.append(f"role {assignment.role_id!r} has no target for mutation")
                break
            if assignment.selection_action == "loop_starter_reroll":
                if assignment.target.kind != "channel_generator":
                    failed = True
                    failed_assignment_id = assignment.assignment_id
                    blockers.append(f"role {assignment.role_id!r} Loop Starter target is not a Channel Rack generator")
                    break
                try:
                    dispatch = self.controller.reroll_loop_starter_loop(
                        channel_index=assignment.target.channel_index,
                        session_fingerprint=session,
                    )
                except Exception as exc:
                    new_receipts.append(
                        self._receipt_for(
                            assignment,
                            verified=False,
                            summary=(
                                "Loop Starter outcome is unknown after dispatch; "
                                "no automatic retry is allowed."
                            ),
                        )
                    )
                    failed = True
                    failed_assignment_id = assignment.assignment_id
                    blockers.append(f"role {assignment.role_id!r} Loop Starter dispatch failed: {exc}")
                    break
                new_receipts.append(
                    self._receipt_for(
                        assignment,
                        verified=False,
                        summary="Loop Starter dispatch completed without authoritative selected-loop identity.",
                        warnings=_field(dispatch, "warnings", ()),
                    )
                )
                warnings.append(f"role {assignment.role_id!r} is dispatch-only and was not counted as a verified sound selection")
                failed = True
                failed_assignment_id = assignment.assignment_id
                blockers.append("Loop Starter identity is not read back by FL; application stopped")
                break

            if assignment.selected_preset is None and assignment.selected_preset_index is None:
                failed = True
                failed_assignment_id = assignment.assignment_id
                blockers.append(f"role {assignment.role_id!r} has no exact preset identity")
                break
            expected_current = None
            if current is not None and current.identity_status == "stable":
                expected_current = ExpectedPluginPresetState(name=current.name, index=current.index)
            try:
                raw_receipt = self.controller.select_plugin_preset(
                    **_target_kwargs(assignment.target),
                    preset_name=assignment.selected_preset,
                    preset_index=assignment.selected_preset_index,
                    expected_current=expected_current,
                    session_fingerprint=session,
                    target_fingerprint=assignment.target_fingerprint,
                    max_navigation_steps=max_navigation_steps,
                    settle_tick_limit=settle_tick_limit,
                )
            except Exception as exc:
                # The mutation may have reached FL before a transport error;
                # retain an unverified marker so a later call cannot replay it.
                new_receipts.append(
                    self._receipt_for(
                        assignment,
                        verified=False,
                        summary=(
                            "Preset selection outcome is unknown after dispatch; "
                            "no automatic retry is allowed."
                        ),
                    )
                )
                failed = True
                failed_assignment_id = assignment.assignment_id
                blockers.append(f"role {assignment.role_id!r} preset selection outcome is unknown: {exc}")
                break
            receipt = self._coerce_selection_receipt(raw_receipt)
            if receipt is None:
                new_receipts.append(
                    self._receipt_for(
                        assignment,
                        verified=False,
                        summary=(
                            "Preset selection returned no typed verification receipt; "
                            "no automatic retry is allowed."
                        ),
                    )
                )
                failed = True
                failed_assignment_id = assignment.assignment_id
                blockers.append(f"role {assignment.role_id!r} returned no typed preset-selection receipt")
                break
            exact_receipts.append(receipt)
            if assignment.target_fingerprint and receipt.target_fingerprint != assignment.target_fingerprint:
                new_receipts.append(
                    self._receipt_for(
                        assignment,
                        verified=False,
                        summary=(
                            "Preset selection target identity changed; no automatic retry is allowed."
                        ),
                        selected_preset=receipt.after.name,
                        warnings=receipt.warnings,
                    )
                )
                failed = True
                failed_assignment_id = assignment.assignment_id
                blockers.append(f"role {assignment.role_id!r} preset receipt target identity changed")
                break
            if receipt.session_fingerprint != session:
                new_receipts.append(
                    self._receipt_for(
                        assignment,
                        verified=False,
                        summary=(
                            "Preset selection session identity changed; no automatic retry is allowed."
                        ),
                        selected_preset=receipt.after.name,
                        warnings=receipt.warnings,
                    )
                )
                failed = True
                failed_assignment_id = assignment.assignment_id
                blockers.append(f"role {assignment.role_id!r} preset receipt session identity is unknown or stale")
                break
            if receipt.verified and receipt.outcome == "verified":
                assignment_mismatch = self._receipt_matches_assignment(
                    assignment, receipt
                )
                if assignment_mismatch is not None:
                    new_receipts.append(
                        self._receipt_for(
                            assignment,
                            verified=False,
                            summary=(
                                assignment_mismatch
                                + "; no automatic retry is allowed."
                            ),
                            selected_preset=receipt.after.name,
                            warnings=receipt.warnings,
                        )
                    )
                    failed = True
                    failed_assignment_id = assignment.assignment_id
                    blockers.append(
                        assignment_mismatch + "; no retry was attempted"
                    )
                    break
            if not receipt.verified or receipt.outcome != "verified":
                new_receipts.append(
                    self._receipt_for(
                        assignment,
                        verified=False,
                        summary=receipt.verification_summary,
                        selected_preset=receipt.after.name,
                        warnings=receipt.warnings,
                    )
                )
                failed = True
                failed_assignment_id = assignment.assignment_id
                blockers.append(f"role {assignment.role_id!r} preset selection was not verified; no retry was attempted")
                break
            new_receipts.append(
                self._receipt_for(
                    assignment,
                    verified=True,
                    summary=receipt.verification_summary,
                    selected_preset=receipt.after.name,
                    warnings=receipt.warnings,
                )
            )
            record_verified_usage(assignment, receipt.after.name)

        if failed:
            prior_verified = any(item.verified for item in state.apply_receipts)
            status: Literal["planned", "applying", "applied", "partially_applied", "failed"] = "partially_applied" if prior_verified or any(item.verified for item in new_receipts) else "failed"
        else:
            completed_ids = {item.assignment_id for item in state.apply_receipts}
            completed_ids.update(item.assignment_id for item in new_receipts)
            status = "partially_applied" if role_keys and len(completed_ids) < len(assignments) else "applied"
        return self._finish_apply(
            palette_id,
            session_fingerprint=session,
            selection_receipts=exact_receipts,
            new_assignment_receipts=new_receipts,
            assignment_scope=ordered,
            failed_assignment_id=failed_assignment_id,
            warnings=warnings,
            blockers=blockers,
            history_written=history_written,
            status=status,
        )

    # ------------------------------------------------------------------
    # Feedback/history wrappers
    # ------------------------------------------------------------------

    def record_feedback(self, request: SoundFeedbackRequest) -> SoundFeedbackResult:
        feedback = request if isinstance(request, SoundFeedbackRequest) else SoundFeedbackRequest.model_validate(request)
        state = self.palette_registry.get(feedback.palette_id)
        product_id = None
        preset_digest = None
        resolved_role_id = feedback.role_id
        resolved_assignment_id = feedback.assignment_id
        if state is not None and feedback.role_id is not None:
            assignment = next(
                (item for item in state.assignments if item.role_id.casefold() == feedback.role_id.casefold()),
                None,
            )
            if assignment is not None:
                product_id = assignment.product_id
                preset_digest = assignment.preset_identity_digest
                resolved_assignment_id = resolved_assignment_id or assignment.assignment_id
        elif state is not None and feedback.assignment_id is not None:
            assignment = next(
                (item for item in state.assignments if item.assignment_id == feedback.assignment_id),
                None,
            )
            if assignment is not None:
                product_id = assignment.product_id
                preset_digest = assignment.preset_identity_digest
                resolved_role_id = resolved_role_id or assignment.role_id
        stamp = _stamp(self._now())
        warnings: list[str] = []
        try:
            persisted = bool(
                self.history.record_feedback(
                    palette_id=feedback.palette_id,
                    verdict=feedback.verdict,
                    role_id=resolved_role_id,
                    assignment_id=resolved_assignment_id,
                    product_id=product_id,
                    preset_identity_digest=preset_digest,
                    descriptors=feedback.descriptors,
                    desired_descriptors=feedback.desired_descriptors,
                    undesired_descriptors=feedback.undesired_descriptors,
                    hard_exclusion=feedback.hard_exclusion,
                    hard_preference=feedback.hard_preference,
                    persistence=feedback.persistence,
                    note=feedback.note,
                    now=stamp,
                    persist=feedback.persist and feedback.persistence == "persist",
                )
            )
        except Exception as exc:
            persisted = False
            warnings.append(f"feedback was not persisted: {exc}")
        if state is not None and feedback.verdict in {"accepted", "rejected"}:
            feedback_id = "feedback-" + canonical_digest(
                {
                    "request": feedback.model_dump(mode="json", exclude_none=False),
                    "recorded_at": stamp.isoformat(),
                }
            )[:24]
            field_name = (
                "accepted_feedback"
                if feedback.verdict == "accepted"
                else "rejected_feedback"
            )
            prior_rows = getattr(state, field_name)
            if feedback_id not in prior_rows and len(prior_rows) >= MAX_ROLE_COUNT:
                warnings.append(
                    "process-local palette feedback state is full; persistent history status is reported separately"
                )
            else:
                rows = tuple(dict.fromkeys((*prior_rows, feedback_id)))
                state_stamp = max(stamp, state.updated_at)
                state = state.model_copy(
                    update={"updated_at": state_stamp, field_name: rows}
                )
                self.palette_registry.put(state)
        status = self.history.status()
        warnings.extend(status.warnings)
        return SoundFeedbackResult(
            feedback=feedback,
            persisted=persisted,
            history=status,
            warnings=_dedupe(warnings, limit=16),
        )

    def history_status(self) -> SoundHistoryStatus:
        return self.history.status()

    def history_reset(self, confirm: bool = True) -> SoundHistoryResetResult:
        if type(confirm) is not bool:
            raise ValueError("confirm must be true or false")
        if not confirm:
            raise SoundSelectionAuthorizationError(
                "history reset requires confirm=true; no history file was changed"
            )
        result = self.history.reset()
        self._verified_receipts.clear()
        return result

    def _trim_runtime_maps(self) -> None:
        # Keep service-side caches bounded in the same spirit as the core
        # palette registry.  The registry remains the source of truth.
        states = {item.palette_id for item in self.palette_registry.all_states()}
        for mapping in (self._inventories, self._persist_history, self._verified_receipts):
            for key in tuple(mapping):
                if key not in states:
                    mapping.pop(key, None)


# One process-local service is intentionally shared by the MCP wrappers and by
# Production Run integration.  Tests and embedders can construct an isolated
# SoundSelectionService with injected dependencies.
SOUND_SELECTION = SoundSelectionService()


__all__ = [
    "SOUND_SELECTION",
    "convert_plugin_pad_map",
    "SoundFeedbackResult",
    "SoundPaletteLookup",
    "SoundSelectionApplyResult",
    "SoundSelectionAuthorizationError",
    "SoundSelectionExecutionError",
    "SoundSelectionService",
    "SoundSelectionSessionError",
]
