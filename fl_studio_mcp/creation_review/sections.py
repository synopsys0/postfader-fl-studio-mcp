"""Authoritative section mapping for Creation Review.

Section boundaries come from the source Production Run and user handoffs
before any signal-based suggestion is considered.  This module performs only
timeline arithmetic and deterministic bookkeeping; it does not silently turn
detected boundaries into project state.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .assets import _model, _value


SECTION_ANALYZER_VERSION = "creation-review-sections-1"
MAX_REVIEW_SECTIONS = 64
MAX_TEMPO_POINTS = 64


class ReviewSectionError(ValueError):
    """A section map could not be constructed from the supplied metadata."""


@dataclass(frozen=True)
class _SourceTransport:
    """Transport facts retained by a Production Run, when they are known.

    Production Run state has existed in a few shapes while the feature has
    evolved.  In particular, the authoritative project observation is nested
    at ``run_context.project_checkpoint.transport``.  Keep extraction in one
    small compatibility helper so section arithmetic and session snapshots do
    not silently fall back to 120 BPM when that observation is present.
    """

    tempo_bpm: float | None = None
    time_signature_numerator: int | None = None
    time_signature_denominator: int | None = None
    tempo_map: Sequence[Any] | Mapping[Any, Any] | None = None


def _source_state(source_run: Any) -> Any:
    state = _value(source_run, "state")
    return source_run if state is None else state


def _valid_tempo(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        return None
    return result


def _valid_meter_value(value: Any) -> int | None:
    if type(value) is int and 1 <= value <= 32:
        return value
    return None


def _source_transport(source_run: Any) -> _SourceTransport:
    """Extract the best-known source transport without broad object walking.

    The nested checkpoint transport is authoritative.  Older wrappers may
    expose the same scalar values directly on the state, request, or plan;
    those are retained as bounded compatibility fallbacks.  A tempo map is
    intentionally returned as supplied and validated by ``_normalise_tempo_map``
    so unsupported or malformed maps cannot be mistaken for constant tempo.
    """

    if source_run is None:
        return _SourceTransport()
    state = _source_state(source_run)
    request = _value(state, "request", _value(source_run, "request"))
    contexts: list[Any] = []
    for parent in (state, source_run):
        context = _value(parent, "run_context")
        if context is not None and all(context is not item for item in contexts):
            contexts.append(context)
    # A context snapshot may itself be passed as ``source_run``.
    if _value(state, "project_checkpoint") is not None and all(
        state is not item for item in contexts
    ):
        contexts.append(state)

    containers: list[Any] = []
    for context in contexts:
        checkpoint = _value(context, "project_checkpoint")
        transport = _value(checkpoint, "transport")
        if transport is not None:
            containers.append(transport)
        if checkpoint is not None:
            containers.append(checkpoint)
        containers.append(context)
    containers.extend((state, source_run, request))

    # Operations occasionally carry the captured transport alongside their
    # generated material.  They are the least authoritative fallback.
    plan = _value(state, "plan", _value(source_run, "plan"))
    operations = _value(plan, "operations", ())
    if isinstance(operations, (list, tuple)):
        for operation in operations:
            operation_transport = _value(operation, "transport")
            if operation_transport is not None:
                containers.append(operation_transport)
            containers.append(operation)

    tempo: float | None = None
    numerator: int | None = None
    denominator: int | None = None
    tempo_map: Any = None
    for container in containers:
        if container is None:
            continue
        if tempo is None:
            for key in ("tempo_bpm", "tempo", "bpm"):
                tempo = _valid_tempo(_value(container, key))
                if tempo is not None:
                    break
        if numerator is None:
            for key in (
                "time_signature_numerator",
                "numerator",
                "beats_per_bar",
            ):
                numerator = _valid_meter_value(_value(container, key))
                if numerator is not None:
                    break
        if denominator is None:
            for key in (
                "time_signature_denominator",
                "denominator",
                "beat_unit",
            ):
                denominator = _valid_meter_value(_value(container, key))
                if denominator is not None:
                    break
        if tempo_map is None:
            for key in ("tempo_map", "tempo_changes", "tempo_points"):
                candidate = _value(container, key)
                if candidate is not None and not (
                    isinstance(candidate, (Mapping, list, tuple))
                    and len(candidate) == 0
                ):
                    tempo_map = candidate
                    break
        if tempo is not None and numerator is not None and denominator is not None and tempo_map is not None:
            break
    if tempo_map is not None:
        # A map whose first point is bar 1 is itself an authoritative opening
        # tempo, even when an older checkpoint omitted (or disagreed on) the
        # scalar tempo field.  Maps beginning later normalize their leading
        # interval to the scalar/default tempo, so this assignment preserves
        # that value unchanged.
        try:
            first_point = _normalise_tempo_map(tempo_map, tempo or 120.0)[0]
        except (TypeError, ValueError):
            first_point = None
        if first_point is not None:
            tempo = first_point[1]
    return _SourceTransport(tempo, numerator, denominator, tempo_map)


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, (list, tuple)):
        # Accept compact caller-selected ranges without treating arbitrary
        # iterables as section records: (start_bar, end_bar) or
        # (section_id, start_seconds, end_seconds).
        if len(value) == 2:
            return {"start_bar": value[0], "end_bar": value[1]}
        if len(value) == 3:
            return {
                "section_id": value[0],
                "start_seconds": value[1],
                "end_seconds": value[2],
            }
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json", exclude_none=False)
        if isinstance(dumped, Mapping):
            return dumped
    values: dict[str, Any] = {}
    for key in (
        "section_id", "id", "name", "label", "start_bar", "end_bar",
        "start_seconds", "end_seconds", "start_time", "end_time", "source",
        "confidence", "pattern_ids", "palette_roles", "expected_function",
        "energy_intent", "tempo_bpm", "time_signature_numerator",
        "time_signature_denominator",
    ):
        if hasattr(value, key):
            values[key] = getattr(value, key)
    return values


def _slug(value: str, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(value).strip()).strip("-")
    return text or fallback


def bar_duration_seconds(
    tempo_bpm: float,
    numerator: int = 4,
    denominator: int = 4,
) -> float:
    """Return the duration of one bar for a constant tempo/time signature."""

    try:
        tempo = float(tempo_bpm)
    except (TypeError, ValueError):
        raise ReviewSectionError("tempo_bpm must be numeric") from None
    if not math.isfinite(tempo) or tempo <= 0:
        raise ReviewSectionError("tempo_bpm must be positive and finite")
    if not isinstance(numerator, int) or isinstance(numerator, bool) or not 1 <= numerator <= 32:
        raise ReviewSectionError("time-signature numerator must be between 1 and 32")
    if not isinstance(denominator, int) or isinstance(denominator, bool) or denominator not in (1, 2, 4, 8, 16, 32):
        raise ReviewSectionError("time-signature denominator must be a supported power of two")
    return 60.0 / tempo * numerator * (4.0 / denominator)


def _normalise_tempo_map(
    tempo_map: Sequence[Any] | Mapping[Any, Any] | None,
    default_tempo: float,
) -> tuple[tuple[float, float], ...]:
    """Normalize supported ``start_bar``/``bpm`` points.

    A point's ``start_bar`` is one-based (bar 1 is the beginning of the
    timeline), matching the Production Run section contract.  A bare tempo
    map without an explicit bar is rejected rather than guessed.
    """

    default_value = _valid_tempo(default_tempo)
    if default_value is None:
        raise ReviewSectionError("tempo_bpm must be positive and finite")
    if tempo_map is None:
        return ((1.0, default_value),)
    if isinstance(tempo_map, Mapping):
        if any(
            key in tempo_map
            for key in ("start_bar", "bar", "bar_number", "bpm", "tempo_bpm", "tempo")
        ):
            values = [tempo_map]
        else:
            values = [
                {"start_bar": key, "bpm": value}
                for key, value in tempo_map.items()
            ]
    else:
        values = list(tempo_map)
    if not values or len(values) > MAX_TEMPO_POINTS:
        raise ReviewSectionError(
            f"tempo_map must contain between 1 and {MAX_TEMPO_POINTS} points"
        )
    points: list[tuple[float, float]] = []
    for value in values:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            start, bpm = value
        else:
            item = _mapping(value)
            start = item.get("start_bar", item.get("bar", item.get("bar_number")))
            bpm = item.get("bpm", item.get("tempo_bpm", item.get("tempo")))
        if start is None or bpm is None:
            raise ReviewSectionError("tempo_map points need start_bar and bpm")
        try:
            start_value = float(start)
            bpm_value = float(bpm)
        except (TypeError, ValueError):
            raise ReviewSectionError("tempo_map points must contain numeric values") from None
        if not math.isfinite(start_value) or start_value < 1:
            raise ReviewSectionError("tempo_map start_bar must be at least 1")
        if not math.isfinite(bpm_value) or bpm_value <= 0:
            raise ReviewSectionError("tempo_map bpm must be positive and finite")
        # Tempo automation can begin between integer bar boundaries. Preserve
        # that documented position so later section conversion integrates the
        # real timeline instead of rounding a change earlier or later.
        points.append((start_value, bpm_value))
    points.sort()
    if any(left[0] == right[0] for left, right in zip(points, points[1:])):
        raise ReviewSectionError("tempo_map cannot contain duplicate start bars")
    if points[0][0] > 1.0:
        # Keep the leading interval explicit and use the caller's default
        # tempo until the first documented change.  This avoids silently
        # stretching the opening bar when a map starts at bar 3 or later.
        points.insert(0, (1.0, default_value))
    return tuple(points)


def bars_to_seconds(
    bar: float,
    *,
    tempo_bpm: float,
    numerator: int = 4,
    denominator: int = 4,
    tempo_map: Sequence[Any] | Mapping[Any, Any] | None = None,
) -> float:
    """Convert a one-based bar position to seconds, integrating tempo points."""

    try:
        position = float(bar)
    except (TypeError, ValueError):
        raise ReviewSectionError("bar must be numeric") from None
    if not math.isfinite(position) or position < 1:
        raise ReviewSectionError("bar must be finite and at least 1")
    points = _normalise_tempo_map(tempo_map, tempo_bpm)
    elapsed = 0.0
    # Integrate [bar 1, requested bar).  End points are allowed to be
    # fractional so callers can express half-bar handoffs precisely.
    for index, (point_bar, bpm) in enumerate(points):
        segment_start = max(1.0, point_bar)
        segment_end = position
        if index + 1 < len(points):
            segment_end = min(segment_end, points[index + 1][0])
        if segment_end > segment_start:
            elapsed += (segment_end - segment_start) * bar_duration_seconds(
                bpm, numerator, denominator
            )
        if position <= segment_start or position <= segment_end:
            break
    return round(max(0.0, elapsed), 9)


def _section_entries(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        # A mapping of IDs to range payloads is a common persisted form.
        if any(key in value for key in ("start_bar", "start_seconds", "start_time")):
            return [value]
        entries: list[Any] = []
        for key, item in value.items():
            if isinstance(item, Mapping):
                item = dict(item)
                item.setdefault("section_id", key)
            entries.append(item)
        return entries
    if isinstance(value, (str, bytes)):
        return []
    if (
        isinstance(value, (list, tuple))
        and len(value) in {2, 3}
        and not any(isinstance(item, (Mapping, list, tuple)) for item in value)
    ):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


def _find_source_sections(source_run: Any) -> tuple[list[Any], str | None]:
    """Read known Production Run section fields without broad object walking."""

    if source_run is None:
        return [], None
    state = _source_state(source_run)
    request = _value(state, "request", _value(source_run, "request"))
    plan = _value(state, "plan", _value(source_run, "plan"))
    candidates: tuple[tuple[str, Any], ...] = (
        ("production_run.sections", _value(source_run, "sections")),
        ("production_run.state.sections", _value(state, "sections")),
        ("production_run.section_plan", _value(source_run, "section_plan")),
        ("production_run.state.section_plan", _value(state, "section_plan")),
        ("production_run.request.section_scope", _value(request, "section_scope")),
        ("production_run.request.sections", _value(request, "sections")),
        ("production_run.plan.sections", _value(plan, "sections")),
        ("production_run.state.plan.sections", _value(_value(state, "plan"), "sections")),
        ("production_run.delivery_handoff.sections", _value(_value(source_run, "delivery_handoff"), "sections")),
        ("production_run.outcome.sections", _value(_value(source_run, "outcome"), "sections")),
    )
    for source_name, candidate in candidates:
        entries = _section_entries(candidate)
        if entries:
            return entries, source_name
    return [], None


def _coerce_section(
    value: Any,
    *,
    index: int,
    source: str,
    confidence: float,
    tempo_bpm: float,
    numerator: int,
    denominator: int,
    tempo_map: Sequence[Any] | Mapping[Any, Any] | None,
    export_offset_seconds: float,
    limitations: list[str],
) -> dict[str, Any] | None:
    item = _mapping(value)
    raw_id = item.get("section_id", item.get("id"))
    raw_name = item.get("name", item.get("label", raw_id))
    section_id = _slug(str(raw_id or raw_name or f"section-{index + 1}"), f"section-{index + 1}")
    name = str(raw_name or section_id).strip() or section_id
    start_bar = item.get("start_bar", item.get("bar_start"))
    end_bar = item.get("end_bar", item.get("bar_end"))
    start_seconds = item.get("start_seconds", item.get("start_time"))
    end_seconds = item.get("end_seconds", item.get("end_time"))
    declared_timestamps = (
        (start_seconds is not None or end_seconds is not None)
        and not (start_bar is not None and end_bar is not None)
    )
    if start_seconds is None and start_bar is not None:
        try:
            start_seconds = bars_to_seconds(
                float(start_bar),
                tempo_bpm=tempo_bpm,
                numerator=numerator,
                denominator=denominator,
                tempo_map=tempo_map,
            )
        except ReviewSectionError as exc:
            limitations.append(f"section {section_id}: {exc}")
            return None
    if end_seconds is None and end_bar is not None:
        try:
            end_seconds = bars_to_seconds(
                float(end_bar),
                tempo_bpm=tempo_bpm,
                numerator=numerator,
                denominator=denominator,
                tempo_map=tempo_map,
            )
        except ReviewSectionError as exc:
            limitations.append(f"section {section_id}: {exc}")
            return None
    # User-supplied ranges may use seconds only.  The ReviewSection contract
    # still carries integer bar bounds, so derive conservative display bars
    # from the selected tempo without replacing the authoritative timestamps.
    try:
        start_value = float(start_seconds) if start_seconds is not None else None
        end_value = float(end_seconds) if end_seconds is not None else None
    except (TypeError, ValueError):
        limitations.append(f"section {section_id}: boundaries must be numeric")
        return None
    if start_value is None or end_value is None:
        limitations.append(f"section {section_id}: missing start or end boundary")
        return None
    if not math.isfinite(start_value) or not math.isfinite(end_value):
        limitations.append(f"section {section_id}: boundaries must be finite")
        return None
    start_value += export_offset_seconds
    end_value += export_offset_seconds
    # ReviewSection timestamps are intentionally bounded at zero even when a
    # caller exports a count-in/lead-in before bar 1.  Keep the fact that the
    # source timeline was clipped visible instead of allowing a negative
    # value to fail model validation or silently pretending the file starts at
    # bar 1.
    if start_value < 0.0:
        limitations.append(
            f"section {section_id}: lead-in before bar 1 was clipped at 0 seconds"
        )
        start_value = 0.0
    if end_value < 0.0:
        limitations.append(
            f"section {section_id}: boundary ends before the supplied audio timeline"
        )
        return None
    if end_value <= start_value:
        limitations.append(f"section {section_id}: end must be after start")
        return None
    bar_length = bar_duration_seconds(tempo_bpm, numerator, denominator)
    if start_bar is None:
        start_bar = max(1, int(math.floor(max(0.0, start_value) / bar_length)) + 1)
    if end_bar is None:
        end_bar = max(int(start_bar), int(math.ceil(max(0.0, end_value) / bar_length)))
    section_source = str(item.get("source", source))
    if section_source not in {
        "production_run", "section_marker", "user_supplied", "playlist_handoff",
        "detected_suggestion",
    }:
        section_source = source
    confidence_value = item.get("confidence", confidence)
    try:
        confidence_value = float(confidence_value)
    except (TypeError, ValueError):
        confidence_value = confidence
    confidence_value = max(0.0, min(1.0, confidence_value))
    pattern_ids = item.get("pattern_ids", item.get("patterns", ())) or ()
    palette_roles = item.get("palette_roles", item.get("roles", ())) or ()
    if isinstance(pattern_ids, str):
        pattern_ids = (pattern_ids,)
    if isinstance(palette_roles, str):
        palette_roles = (palette_roles,)
    return {
        "section_id": section_id,
        "name": name,
        "start_bar": int(start_bar),
        "end_bar": int(end_bar),
        "start_seconds": round(start_value, 6),
        "end_seconds": round(end_value, 6),
        "source": section_source,
        "confidence": round(confidence_value, 4),
        "pattern_ids": tuple(str(item) for item in pattern_ids),
        "palette_roles": tuple(str(item) for item in palette_roles),
        "expected_function": item.get("expected_function"),
        "energy_intent": item.get("energy_intent"),
        "_declared_timestamps": declared_timestamps,
    }


def _digest(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=False)
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def build_review_section_map(
    source_run: Any = None,
    *,
    section_ranges: Sequence[Any] | Mapping[Any, Any] | None = None,
    markers: Sequence[Any] | Mapping[Any, Any] | None = None,
    playlist_handoff: Any = None,
    detected_suggestions: Sequence[Any] | Mapping[Any, Any] | None = None,
    tempo_bpm: float | None = None,
    time_signature_numerator: int | None = None,
    time_signature_denominator: int | None = None,
    tempo_map: Sequence[Any] | Mapping[Any, Any] | None = None,
    export_offset_seconds: float = 0.0,
    asset_offset_seconds: float | None = None,
) -> Any:
    """Build an ordered, digestible map with explicit source precedence.

    Precedence is user ranges, source Production Run, markers, Playlist
    handoff, then detected suggestions.  Lower-precedence data is ignored once
    a higher-precedence source yields usable boundaries, and detected data is
    always labelled as a suggestion.
    """

    run_request = _value(_source_state(source_run), "request", _value(source_run, "request"))
    source_transport = _source_transport(source_run)
    tempo = tempo_bpm
    if tempo is None:
        tempo = source_transport.tempo_bpm
    if tempo is None:
        tempo = _value(source_run, "tempo_bpm")
    if tempo is None:
        tempo = _value(run_request, "tempo_bpm")
    tempo = float(tempo if tempo is not None else 120.0)
    numerator = time_signature_numerator
    if numerator is None:
        numerator = source_transport.time_signature_numerator
    if numerator is None:
        numerator = _value(source_run, "time_signature_numerator")
    if numerator is None:
        numerator = _value(run_request, "time_signature_numerator")
    if numerator is None:
        numerator = 4
    denominator = time_signature_denominator
    if denominator is None:
        denominator = source_transport.time_signature_denominator
    if denominator is None:
        denominator = _value(source_run, "time_signature_denominator")
    if denominator is None:
        denominator = _value(run_request, "time_signature_denominator")
    if denominator is None:
        denominator = 4
    try:
        numerator_value = int(numerator)
        denominator_value = int(denominator)
    except (TypeError, ValueError):
        raise ReviewSectionError("time signature values must be integers") from None
    if type(numerator) is not int or numerator_value != numerator:
        raise ReviewSectionError("time-signature numerator must be an integer")
    if type(denominator) is not int or denominator_value != denominator:
        raise ReviewSectionError("time-signature denominator must be an integer")
    if tempo_map is None:
        tempo_map = source_transport.tempo_map
    # Normalize once before validating the map or deriving any boundaries.
    # When an explicit map starts at bar 1, its first point is the authoritative
    # opening tempo; otherwise the scalar/default tempo remains the leading
    # interval inserted by ``_normalise_tempo_map``.
    tempo_points = _normalise_tempo_map(tempo_map, tempo)
    if tempo_points and tempo_points[0][0] == 1.0:
        tempo = tempo_points[0][1]
    # Validate the meter even when the source contains no usable ranges.  A
    # ReviewSectionMap with an unsupported denominator would otherwise be
    # accepted by the looser model contract and fail later during arithmetic.
    bar_duration_seconds(tempo, numerator_value, denominator_value)
    tempo_map = tempo_points
    try:
        offset = float(export_offset_seconds)
    except (TypeError, ValueError):
        raise ReviewSectionError("export_offset_seconds must be numeric") from None
    if not math.isfinite(offset):
        raise ReviewSectionError("export_offset_seconds must be finite")
    if asset_offset_seconds is not None:
        try:
            offset += float(asset_offset_seconds)
        except (TypeError, ValueError):
            raise ReviewSectionError("asset_offset_seconds must be numeric") from None

    limitations: list[str] = []
    candidates: tuple[tuple[str, float, Sequence[Any] | Mapping[Any, Any] | None], ...]
    source_sections, source_label = _find_source_sections(source_run)
    handoff_sections = None
    if playlist_handoff is not None:
        handoff_sections = _value(playlist_handoff, "sections")
        if handoff_sections is None:
            # PlaylistHandoff stores placements rather than a separate
            # sections field.  They remain a lower-confidence timeline hint;
            # no Playlist CRUD or inferred placement is performed here.
            handoff_sections = _value(playlist_handoff, "placements")
    candidates = (
        ("user_supplied", 1.0, section_ranges),
        ("production_run", 0.95, source_sections),
        ("section_marker", 0.8, markers),
        ("playlist_handoff", 0.75, handoff_sections),
        ("detected_suggestion", 0.35, detected_suggestions),
    )
    selected_source: str | None = None
    selected: list[dict[str, Any]] = []
    for source, confidence, values in candidates:
        entries = _section_entries(values)
        if not entries:
            continue
        trial_limitations: list[str] = []
        trial = []
        for index, value in enumerate(entries[:MAX_REVIEW_SECTIONS]):
            record = _coerce_section(
                value,
                index=index,
                source=source,
                confidence=confidence,
                tempo_bpm=tempo,
                numerator=numerator_value,
                denominator=denominator_value,
                tempo_map=tempo_map,
                export_offset_seconds=offset,
                limitations=trial_limitations,
            )
            if record is not None:
                trial.append(record)
        if trial:
            selected_source = source
            selected = trial
            limitations.extend(trial_limitations)
            if len(entries) > MAX_REVIEW_SECTIONS:
                limitations.append(
                    f"{source} section list was capped at {MAX_REVIEW_SECTIONS} entries"
                )
            break
        limitations.extend(trial_limitations)
    if selected_source is None:
        limitations.append(
            "no explicit section boundaries were supplied; section-level findings are unavailable"
        )
        selected = []
    if selected_source == "production_run" and source_label and source_label != "production_run.sections":
        limitations.append(f"section boundaries read from {source_label}")
    if selected_source == "detected_suggestion":
        limitations.append(
            "detected boundaries are suggestions only and do not override explicit run or user ranges"
        )
    # De-duplicate IDs deterministically while retaining first occurrence.
    unique: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for record in sorted(selected, key=lambda value: (value["start_seconds"], value["end_seconds"], value["section_id"])):
        section_id = record["section_id"]
        if section_id in seen_ids:
            record = dict(record)
            record["section_id"] = f"{section_id}-{len(unique) + 1}"
            limitations.append(f"duplicate section id {section_id!r} was disambiguated")
        seen_ids.add(record["section_id"])
        unique.append(record)
    selected = unique
    gaps: list[str] = []
    overlaps: list[str] = []
    for left, right in zip(selected, selected[1:]):
        if right["start_seconds"] > left["end_seconds"] + 1e-6:
            gaps.append(f"{left['section_id']}->{right['section_id']}")
        elif right["start_seconds"] < left["end_seconds"] - 1e-6:
            overlaps.append(f"{left['section_id']}->{right['section_id']}")
    if gaps:
        limitations.append("section map contains gaps between supplied boundaries")
    if overlaps:
        limitations.append("section map contains overlapping supplied boundaries")
    section_models = tuple(
        _model(
            "ReviewSection",
            {key: value for key, value in record.items() if not key.startswith("_")},
        )
        for record in selected
    )
    gap_models = tuple(
        _model(
            "ReviewRangeIssue",
            {
                "kind": "gap",
                "start_seconds": next(
                    item["end_seconds"] for item in selected if item["section_id"] == edge.split("->", 1)[0]
                ),
                "end_seconds": next(
                    item["start_seconds"] for item in selected if item["section_id"] == edge.split("->", 1)[1]
                ),
                "section_ids": tuple(edge.split("->", 1)),
                "explanation": "No supplied section boundary covers this interval.",
            },
        )
        for edge in gaps
    )
    overlap_models = tuple(
        _model(
            "ReviewRangeIssue",
            {
                "kind": "overlap",
                "start_seconds": next(
                    item["start_seconds"] for item in selected if item["section_id"] == edge.split("->", 1)[1]
                ),
                "end_seconds": next(
                    item["end_seconds"] for item in selected if item["section_id"] == edge.split("->", 1)[0]
                ),
                "section_ids": tuple(edge.split("->", 1)),
                "explanation": "Supplied section boundaries overlap.",
            },
        )
        for edge in overlaps
    )
    tempo_change_models = tuple(
        _model("TempoChange", {"start_bar": start, "tempo_bpm": bpm})
        for start, bpm in tempo_points[1:]
    )
    map_basis = "tempo_change_map" if len(tempo_points) > 1 else "declared_timestamps" if any(
        item.get("_declared_timestamps", False) for item in selected
    ) else "constant_tempo"
    material = {
        "analyzer_version": SECTION_ANALYZER_VERSION,
        "tempo_bpm": tempo,
        "time_signature_numerator": numerator_value,
        "time_signature_denominator": denominator_value,
        "tempo_map": tempo_points,
        "export_offset_seconds": offset,
        "sections": selected,
        "gaps": gaps,
        "overlaps": overlaps,
        "source": selected_source,
    }
    digest = _digest(material)
    payload = {
        "tempo_bpm": tempo,
        "time_signature_numerator": numerator_value,
        "time_signature_denominator": denominator_value,
        "bar_to_time_basis": map_basis,
        # Keep the offset that was applied to each section boundary on the
        # public map as well.  Section records and ``bar_to_seconds`` must use
        # the same timeline coordinate system after model construction and
        # persistence.
        "export_offset_seconds": offset,
        "tempo_changes": tempo_change_models,
        "sections": section_models,
        "gaps": gap_models,
        "overlaps": overlap_models,
        "map_digest": digest,
        "source_confidence": max((record["confidence"] for record in selected), default=0.0),
        "limitations": tuple(limitations),
    }
    return _model("ReviewSectionMap", payload)


# Compatibility aliases used by early review integrations.
build_section_map = build_review_section_map
create_section_map = build_review_section_map


__all__ = [
    "MAX_REVIEW_SECTIONS",
    "ReviewSectionError",
    "bar_duration_seconds",
    "bars_to_seconds",
    "build_review_section_map",
    "build_section_map",
    "create_section_map",
]
