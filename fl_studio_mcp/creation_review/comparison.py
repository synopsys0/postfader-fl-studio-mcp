"""Read-only before/after bounce comparison for Creation Review."""

# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from .. import audio
from .analysis import (
    _loaded_segment,
    _masking_from_stems,
    _safe_measure,
    _section_measurement,
)
from .assets import (
    DecodedAudioCache,
    ReviewAssetError,
    _canonical_path,
    _model,
    _value,
    get_decode_cache,
    sha256_file,
    validate_audio_asset,
)


COMPARISON_ANALYZER_VERSION = "creation-review-comparison-1"
MAX_COMPARISON_SECTIONS = 64
MAX_COMPARISON_STEMS = 32
MAX_OBJECTIVES = 32
VALID_USER_APPROVAL_STATES = frozenset(
    {
        "not_requested",
        "pending",
        "approved",
        "rejected",
        "unknown",
        "user_confirmed_draft",
        "user_approved",
        "user_rejected",
        "needs_revision",
    }
)


class RevisionComparisonError(ValueError):
    """A before/after comparison cannot produce a truthful aligned result."""


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=False)
    return {
        key: getattr(value, key)
        for key in (
            "metric", "direction", "target_value", "tolerance", "unit", "state",
            "rationale", "expected_or_requested_target", "parameters", "asset_id",
            "asset_kind", "path", "sha256", "role_id", "section_id",
        )
        if hasattr(value, key)
    }


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _get_metric(measurements: Any, metric: str) -> float | None:
    """Look up a scalar by its public dotted or underscored name."""

    names = [metric]
    if "." in metric:
        names.append(metric.replace(".", "_"))
    aliases = {
        "loudness": "lufs_integrated",
        "lufs": "lufs_integrated",
        "true_peak": "true_peak_dbtp",
        "peak": "sample_peak_db",
        "dynamics": "dynamic_spread_db",
        "brightness": "spectral_centroid_hz",
        "stereo_width": "stereo_width",
        "low_end": "low_end_share",
    }
    names.extend(aliases.get(name, name) for name in tuple(names))
    for name in names:
        current = measurements
        parts = name.split(".")
        for part in parts:
            if isinstance(current, Mapping):
                current = current.get(part)
            else:
                current = getattr(current, part, None)
            if current is None:
                break
        result = _number(current)
        if result is not None:
            return result
    # Global measurements nest common values under loudness/spectrum/dynamics.
    nested = {
        "lufs_integrated": ("loudness", "lufs_integrated"),
        "true_peak_dbtp": ("loudness", "true_peak_dbtp"),
        "sample_peak_db": ("loudness", "sample_peak_db"),
        "crest_factor_db": ("loudness", "crest_factor_db"),
        "dynamic_spread_db": ("dynamics", "dynamic_spread_db"),
        "spectral_centroid_hz": ("spectrum", "spectral_centroid_hz"),
        "low_end_share": ("spectrum", "low_end_share"),
        "stereo_width": ("stereo", "mid_side_ratio"),
        "stereo_correlation": ("stereo", "correlation"),
    }
    key = names[-1] if names else metric
    path = nested.get(key)
    if path:
        current = measurements
        for part in path:
            current = _value(current, part)
        return _number(current)
    return None


def _asset(value: Any, *, kind: str) -> Any:
    if isinstance(value, (str, bytes)) or hasattr(value, "__fspath__"):
        return validate_audio_asset(os.fsdecode(os.fspath(value)), asset_kind=kind)
    if _value(value, "path"):
        actual_kind = _value(value, "asset_kind")
        if actual_kind != kind:
            raise RevisionComparisonError(
                f"comparison {kind.replace('_full_mix', '')} asset must have "
                f"asset_kind={kind!r}; received {actual_kind!r}"
            )
        # Existing ReviewAudioAsset metadata is reused after digest checking by
        # the cache.  Do not re-read a file merely to rename its kind.
        return value
    raise RevisionComparisonError("before/after asset metadata has no readable path")


def _asset_id(value: Any, fallback: str) -> str:
    return str(_value(value, "asset_id", fallback))


def _alignment_inputs(
    before: audio.Loaded,
    after: audio.Loaded,
) -> tuple[np.ndarray, np.ndarray]:
    """Return before/after channel data at the before-bounce sample rate."""

    before_data = audio._channel_data(before)
    after_data = audio._channel_data(after)
    if after.rate != before.rate:
        after_data = audio._resample_channels(after_data, after.rate, before.rate)
    return before_data, after_data


def _timeline_offset(asset: Any) -> float:
    """Return the explicitly declared timeline origin for one asset."""

    declared = _number(_value(asset, "declared_offset_seconds"))
    if declared is not None:
        return declared
    expected = _number(_value(asset, "expected_start_seconds"))
    return expected if expected is not None else 0.0


def _explicit_common_section_window(
    section_map: Any | None,
    before: audio.Loaded,
    after: audio.Loaded,
    before_asset: Any,
    after_asset: Any,
) -> tuple[float, float] | None:
    """Return the complete explicit section-map span when it fits both files.

    A duration-mismatched full-timeline comparison may only proceed when the
    caller supplied a bounded section map whose complete declared span is
    present in both assets.  We deliberately do not select a section, clip a
    range to a shorter file, or infer a window from signal duration.
    """

    if section_map is None:
        return None
    raw_sections: Any = _value(section_map, "sections", ()) or ()
    if isinstance(raw_sections, (str, bytes, Mapping)):
        return None
    sections: tuple[Any, ...]
    try:
        sections = tuple(raw_sections)
    except TypeError:
        return None
    if not sections or len(sections) > MAX_COMPARISON_SECTIONS:
        return None
    explicit_sources = {
        "user_supplied",
        "production_run",
        "section_marker",
        "playlist_handoff",
    }
    bounds: list[tuple[float, float]] = []
    for section in sections:
        if _value(section, "source") not in explicit_sources:
            return None
        start = _number(_value(section, "start_seconds"))
        end = _number(_value(section, "end_seconds"))
        if start is None or end is None or start < 0.0 or end <= start:
            return None
        bounds.append((start, end))
    ordered_bounds = sorted(bounds)
    # A bounding box around disjoint sections would silently include an
    # undeclared gap.  Keep the duration-mismatch escape hatch to one
    # explicitly declared contiguous range (overlapping sections are fine).
    if any(
        current_start > previous_end + 1e-6
        for (_, previous_end), (current_start, _) in zip(
            ordered_bounds, ordered_bounds[1:]
        )
    ):
        return None
    window_start = ordered_bounds[0][0]
    window_end = max(item[1] for item in ordered_bounds)
    common_start = max(
        _timeline_offset(before_asset),
        _timeline_offset(after_asset),
    )
    common_end = min(
        _timeline_offset(before_asset) + before.duration,
        _timeline_offset(after_asset) + after.duration,
    )
    if (
        window_start < common_start - 1e-6
        or window_end > common_end + 1e-6
    ):
        return None
    return round(window_start, 9), round(window_end, 9)


def _slice_to_timeline_window(
    data: np.ndarray,
    rate: int,
    window: tuple[float, float],
    timeline_offset_seconds: float,
) -> np.ndarray | None:
    """Slice decoded channels for a validated absolute timeline window."""

    start_seconds, end_seconds = window
    start = max(
        0,
        int(math.floor((start_seconds - timeline_offset_seconds) * rate)),
    )
    end = min(
        len(data),
        int(math.ceil((end_seconds - timeline_offset_seconds) * rate)),
    )
    if end <= start:
        return None
    return data[start:end]


def _alignment(
    before: audio.Loaded,
    after: audio.Loaded,
    before_asset: Any,
    after_asset: Any,
    *,
    max_alignment_seconds: float,
    alignment_rate_hz: int,
    comparison_window: tuple[float, float] | None = None,
) -> tuple[Any, int, np.ndarray | None, np.ndarray | None]:
    before_offset = _number(_value(before_asset, "declared_offset_seconds"))
    after_offset = _number(_value(after_asset, "declared_offset_seconds"))
    before_start = _number(_value(before_asset, "expected_start_seconds"))
    after_start = _number(_value(after_asset, "expected_start_seconds"))
    metadata_conflicts: list[str] = []
    if (
        before_offset is not None
        and before_start is not None
        and abs(before_offset - before_start) > 1e-6
    ):
        metadata_conflicts.append(
            f"before asset declares offset {before_offset:g}s but expects start {before_start:g}s"
        )
    if (
        after_offset is not None
        and after_start is not None
        and abs(after_offset - after_start) > 1e-6
    ):
        metadata_conflicts.append(
            f"after asset declares offset {after_offset:g}s but expects start {after_start:g}s"
        )
    if metadata_conflicts:
        return (
            _model(
                "AlignmentResult",
                {
                    "state": "offset_mismatch",
                    "offset_seconds": None,
                    "duration_delta_seconds": round(after.duration - before.duration, 6),
                    "channel_match": before.channels == after.channels,
                    "confidence": 0.0,
                    "explanation": (
                        "Conflicting export-start metadata: "
                        + "; ".join(metadata_conflicts)
                        + ". Alignment and deltas are withheld."
                    ),
                },
            ),
            0,
            None,
            None,
        )
    declared_before = before_offset if before_offset is not None else before_start
    declared_after = after_offset if after_offset is not None else after_start
    if declared_before is not None or declared_after is not None:
        if declared_before is None or declared_after is None or abs(declared_before - declared_after) > 1e-6:
            return (
                _model(
                    "AlignmentResult",
                    {
                        "state": "offset_mismatch",
                        "offset_seconds": (declared_after - declared_before) if declared_before is not None and declared_after is not None else None,
                        "duration_delta_seconds": round(after.duration - before.duration, 6),
                        "channel_match": before.channels == after.channels,
                        "confidence": 0.0,
                        "explanation": "Before and after assets do not declare a common export start.",
                    },
                ),
                0,
                None,
                None,
            )
    if before.channels != after.channels:
        return (
            _model(
                "AlignmentResult",
                {
                    "state": "channel_mismatch",
                    "duration_delta_seconds": round(after.duration - before.duration, 6),
                    "channel_match": False,
                    "confidence": 0.0,
                    "explanation": "Before and after assets have different channel counts.",
                },
            ),
            0,
            None,
            None,
        )
    duration_delta = after.duration - before.duration
    # A materially different render length is a timeline mismatch. A small
    # tail difference is retained by the aligned-overlap comparison. A larger
    # difference is allowed only for a complete, explicitly declared common
    # section-map span; no range is inferred from the file durations.
    duration_mismatch = abs(duration_delta) > max(0.05, 2.0 / max(before.rate, after.rate))
    if not duration_mismatch:
        # A section map is used for section deltas on compatible full-timeline
        # bounces; it must not silently narrow the global comparison window.
        comparison_window = None
    if duration_mismatch and comparison_window is None:
        return (
            _model(
                "AlignmentResult",
                {
                    "state": "duration_mismatch",
                    "duration_delta_seconds": round(duration_delta, 6),
                    "channel_match": True,
                    "confidence": 0.0,
                    "explanation": "Before and after assets have incompatible durations for a full-timeline comparison.",
                },
            ),
            0,
            None,
            None,
        )
    before_data, after_data = _alignment_inputs(before, after)
    if comparison_window is not None:
        before_data = _slice_to_timeline_window(
            before_data,
            before.rate,
            comparison_window,
            _timeline_offset(before_asset),
        )
        after_data = _slice_to_timeline_window(
            after_data,
            before.rate,
            comparison_window,
            _timeline_offset(after_asset),
        )
        if before_data is None or after_data is None:
            return (
                _model(
                    "AlignmentResult",
                    {
                        "state": "duration_mismatch",
                        "duration_delta_seconds": round(duration_delta, 6),
                        "channel_match": True,
                        "confidence": 0.0,
                        "explanation": (
                            "The explicitly declared common section range does not have "
                            "usable decoded coverage in both bounces."
                        ),
                    },
                ),
                0,
                None,
                None,
            )
    try:
        report, lag = audio._estimate_alignment(
            before_data,
            after_data,
            before.rate,
            max_alignment_seconds,
            alignment_rate_hz,
        )
        aligned_before, aligned_after = audio._aligned_regions(before_data, after_data, lag)
    except (audio.AudioError, ValueError) as exc:
        return (
            _model(
                "AlignmentResult",
                {
                    "state": "failed",
                    "duration_delta_seconds": round(duration_delta, 6),
                    "channel_match": True,
                    "confidence": 0.0,
                    "explanation": f"Before/after alignment failed: {exc}",
                },
            ),
            0,
            None,
            None,
        )
    confidence = float(_value(_value(report, "confidence", {}), "score", 0.0) or 0.0)
    # A periodic signal can have a low distinct-peak confidence even when the
    # best overlap itself is exact.  Retain that warning in ``confidence`` but
    # allow a very strong, well-covered overlap to be compared; weak or
    # unrelated material still fails closed.
    absolute_correlation = float(report.get("absolute_correlation", 0.0) or 0.0)
    coverage = float(report.get("common_coverage", 0.0) or 0.0)
    state = "aligned" if (
        (confidence >= 0.55 and coverage >= 0.5)
        or (absolute_correlation >= 0.98 and coverage >= 0.8)
    ) else "failed"
    alignment_result = _model(
        "AlignmentResult",
        {
            "state": state,
            "offset_seconds": round(float(report.get("target_lag_seconds", 0.0)), 6),
            "duration_delta_seconds": round(duration_delta, 6),
            "channel_match": True,
            "confidence": max(0.0, min(1.0, confidence)),
            "explanation": (
                (
                    "Before and after bounces share a sufficiently strong aligned overlap "
                    f"within the explicit section window {comparison_window[0]:g}-"
                    f"{comparison_window[1]:g}s."
                )
                if state == "aligned" and comparison_window is not None
                else "Before and after bounces share a sufficiently strong aligned overlap."
                if state == "aligned" else "Alignment correlation is weak or ambiguous; deltas are withheld."
            ),
        },
    )
    return alignment_result, lag, aligned_before, aligned_after


def _scalar_features(data: np.ndarray, rate: int) -> dict[str, Any]:
    loaded = audio._loaded_from_channels("comparison-overlap", data, rate)
    loudness, _loudness_error = _safe_measure(audio.measure_loudness, loaded)
    spectrum, _spectrum_error = _safe_measure(audio.measure_spectrum, loaded)
    dynamics, _dynamics_error = _safe_measure(audio.measure_dynamics, loaded)
    stereo, _stereo_error = _safe_measure(audio.measure_stereo, loaded)
    bands = spectrum.get("bands", {}) if spectrum else {}
    low_end = sum(
        float(bands[name]["energy_share"])
        for name in ("sub", "low")
        if name in bands
    )
    return {
        "sample_peak_db": loudness.get("sample_peak_db") if loudness else None,
        "true_peak_dbtp": loudness.get("true_peak_dbtp") if loudness else None,
        "lufs_integrated": loudness.get("lufs_integrated") if loudness else None,
        "crest_factor_db": loudness.get("crest_factor_db") if loudness else None,
        "dynamic_spread_db": dynamics.get("dynamic_spread_db") if dynamics else None,
        "spectral_centroid_hz": spectrum.get("spectral_centroid_hz") if spectrum else None,
        "low_end_share": round(low_end, 6),
        "stereo_width": stereo.get("mid_side_ratio") if stereo else None,
        "stereo_correlation": stereo.get("correlation") if stereo else None,
        "clipped_samples": loudness.get("clipped_samples") if loudness else None,
    }


def _delta_map(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: round(float(after[key]) - float(before[key]), 6)
        for key in before.keys() & after.keys()
        if _number(before[key]) is not None and _number(after[key]) is not None
    }


def _objective(value: Any, global_deltas: Mapping[str, Any]) -> Any:
    item = _mapping(value)
    metric = str(item.get("metric", item.get("name", ""))).strip()
    direction = str(item.get("direction", "change"))
    target_value = _number(item.get("target_value"))
    tolerance = _number(item.get("tolerance"))
    state = str(item.get("state", "proxy_evaluable"))
    expectation = _model(
        "MetricExpectation",
        {
            "metric": metric or "unspecified",
            "direction": direction if direction in {"increase", "decrease", "maintain", "within", "change"} else "change",
            "target_value": target_value,
            "tolerance": tolerance,
            "unit": item.get("unit"),
            "state": state if state in {"technically_evaluable", "proxy_evaluable", "requires_user_judgment", "not_evaluable_from_supplied_assets"} else "proxy_evaluable",
            "rationale": item.get("rationale"),
        },
    )
    observed = _number(global_deltas.get(metric))
    if observed is None:
        # Try nested/alias names for common objective wording.
        observed = _get_metric(global_deltas, metric)
    if state in {"requires_user_judgment", "not_evaluable_from_supplied_assets"}:
        result_state = "not_measurable"
        explanation = "This objective requires producer judgment and is not established by metrics."
    elif observed is None:
        result_state = "insufficient_evidence"
        explanation = "The requested metric was not measurable on the aligned overlap."
    else:
        tolerance_value = tolerance if tolerance is not None else 0.0
        if direction in {"maintain", "within"}:
            result_state = "unchanged" if abs(observed) <= tolerance_value else "moved_away_from_target"
        elif direction == "increase":
            result_state = (
                "moved_toward_target"
                if observed > tolerance_value
                else "moved_away_from_target"
                if observed < -tolerance_value
                else "unchanged"
            )
        elif direction == "decrease":
            result_state = (
                "moved_toward_target"
                if observed < -tolerance_value
                else "moved_away_from_target"
                if observed > tolerance_value
                else "unchanged"
            )
        else:
            result_state = "moved_toward_target" if abs(observed) > tolerance_value else "unchanged"
        explanation = f"Observed after-minus-before delta for {metric}: {observed:+g}."
    observed_metric = _model(
        "ReviewMetric",
        {
            "name": metric or "unspecified",
            "value": observed,
            "unit": item.get("unit"),
            "confidence": "high" if observed is not None else "low",
            "evidence_source": "decoded_audio_measurement",
        },
    ) if observed is not None else None
    return _model(
        "ObjectiveResult",
        {
            "objective": metric or "unspecified objective",
            "state": result_state,
            "expected_movement": expectation,
            "observed_delta": observed_metric,
            "explanation": explanation,
        },
    )


def _section_deltas(
    before: audio.Loaded,
    after: audio.Loaded,
    section_map: Any,
    *,
    before_asset: Any,
    after_asset: Any,
    alignment_lag_samples: int = 0,
) -> tuple[Any, ...]:
    rows: list[Any] = []
    before_offset = _timeline_offset(before_asset)
    after_offset = _timeline_offset(after_asset)
    sections = tuple(_value(section_map, "sections", ()) or ())[:MAX_COMPARISON_SECTIONS]
    for index, section in enumerate(sections):
        section_id = str(_value(section, "section_id", f"section-{index + 1}"))
        start = float(_value(section, "start_seconds", 0.0))
        end = float(_value(section, "end_seconds", 0.0))
        before_timeline_offset = before_offset + min(alignment_lag_samples, 0) / max(before.rate, 1)
        before_part, before_coverage = _loaded_segment(
            before,
            start,
            end,
            timeline_offset_seconds=before_timeline_offset,
        )
        # ``audio._estimate_alignment`` reports positive lag when the target
        # (after bounce) starts later than the reference.  Shift the after
        # timeline for section slicing so section deltas use the same common
        # timeline as global deltas.
        after_timeline_offset = after_offset - max(alignment_lag_samples, 0) / max(after.rate, 1)
        after_part, after_coverage = _loaded_segment(
            after,
            start,
            end,
            timeline_offset_seconds=after_timeline_offset,
        )
        if before_part is None or after_part is None:
            rows.append(_model("SectionDelta", {
                "section_id": section_id,
                "deltas": {},
                "improvements": (),
                "regressions": (),
                "unknown": ("section has insufficient before/after coverage",),
            }))
            continue
        before_features = _section_measurement(before_part, coverage=before_coverage, section_duration=end - start)
        after_features = _section_measurement(after_part, coverage=after_coverage, section_duration=end - start)
        scalar_before = {
            "sample_peak_db": before_features.get("sample_peak_db"),
            "true_peak_dbtp": before_features.get("true_peak_dbtp"),
            "lufs_integrated": before_features.get("lufs_integrated"),
            "rms_db": before_features.get("rms_db"),
            "crest_factor_db": before_features.get("crest_factor_db"),
            "dynamic_spread_db": before_features.get("dynamic_spread_db"),
            "spectral_centroid_hz": before_features.get("spectral_centroid_hz"),
            "low_end_share": before_features.get("low_end_share"),
            "stereo_width": before_features.get("stereo_width"),
            "stereo_correlation": before_features.get("stereo_correlation"),
            "transient_density": before_features.get("onset_density"),
        }
        scalar_after = {
            "sample_peak_db": after_features.get("sample_peak_db"),
            "true_peak_dbtp": after_features.get("true_peak_dbtp"),
            "lufs_integrated": after_features.get("lufs_integrated"),
            "rms_db": after_features.get("rms_db"),
            "crest_factor_db": after_features.get("crest_factor_db"),
            "dynamic_spread_db": after_features.get("dynamic_spread_db"),
            "spectral_centroid_hz": after_features.get("spectral_centroid_hz"),
            "low_end_share": after_features.get("low_end_share"),
            "stereo_width": after_features.get("stereo_width"),
            "stereo_correlation": after_features.get("stereo_correlation"),
            "transient_density": after_features.get("onset_density"),
        }
        deltas = _delta_map(scalar_before, scalar_after)
        improvements: list[str] = []
        regressions: list[str] = []
        for key, delta in deltas.items():
            if abs(delta) < 0.05:
                continue
            if key in {"lufs_integrated", "rms_db", "transient_density", "low_end_share", "stereo_width"} and delta > 0:
                improvements.append(key)
            elif key in {"stereo_correlation", "crest_factor_db", "dynamic_spread_db"} and delta < 0:
                regressions.append(key)
            elif key in {"stereo_correlation", "crest_factor_db", "dynamic_spread_db"} and delta > 0:
                improvements.append(key)
        rows.append(_model("SectionDelta", {
            "section_id": section_id,
            "deltas": deltas,
            "improvements": tuple(improvements),
            "regressions": tuple(regressions),
            "unknown": () if deltas else ("section metrics were unavailable",),
        }))
    return tuple(rows)


def _cache_digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _stored_output_identities(
    outputs: Sequence[Any],
    *,
    output_kind: str,
) -> dict[str, str]:
    """Return role-scoped identities from bounded persisted review outputs."""

    identities: dict[str, str] = {}
    for output in outputs:
        if str(_value(output, "output_kind", "")) != output_kind:
            continue
        role_id = _value(output, "role_id")
        if not isinstance(role_id, str) or not role_id.strip():
            continue
        metadata = _value(output, "metadata", {}) or {}
        identity = _value(output, "digest")
        if not isinstance(identity, str) or len(identity) != 64:
            for name in (
                "note_digest_sha256",
                "preset_identity_digest",
                "plan_digest",
            ):
                candidate = _value(metadata, name)
                if isinstance(candidate, str) and len(candidate) == 64:
                    identity = candidate
                    break
        if not isinstance(identity, str) or len(identity) != 64:
            identity = _cache_digest(metadata)
        identities[role_id.casefold()] = identity
    return identities


def _palette_identities(palette: Any | None) -> dict[str, str]:
    """Return stable role-to-assignment identities from a stored palette."""

    if palette is None:
        return {}
    assignments = _value(palette, "assignments", ()) or ()
    if isinstance(assignments, Mapping):
        assignments = tuple(assignments.values())
    if not isinstance(assignments, Sequence) or isinstance(assignments, (str, bytes)):
        return {}
    result: dict[str, str] = {}
    for assignment in assignments:
        role_id = _value(assignment, "role_id")
        if not isinstance(role_id, str) or not role_id.strip():
            continue
        identity = _value(assignment, "preset_identity_digest")
        if not isinstance(identity, str) or len(identity) != 64:
            identity = _cache_digest(
                {
                    "product_id": _value(assignment, "product_id"),
                    "product_name": _value(assignment, "product_name"),
                    "selected_preset": _value(assignment, "selected_preset"),
                    "selected_preset_index": _value(
                        assignment, "selected_preset_index"
                    ),
                    "target_fingerprint": _value(
                        assignment, "target_fingerprint"
                    ),
                }
            )
        result[role_id.casefold()] = identity
    return result


def _identity_lock_results(
    locks: Sequence[Any],
    *,
    before_generated_outputs: Sequence[Any],
    after_generated_outputs: Sequence[Any],
    before_palette: Any | None,
    after_palette: Any | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Compare accepted identities only when persisted evidence supports it.

    Full-mix audio cannot identify a changed chord sequence or preset.  This
    check therefore uses only stored PostFader outputs and says ``unknown``
    when either side of the requested identity comparison is unavailable.
    """

    before_sequences = _stored_output_identities(
        before_generated_outputs, output_kind="note_sequence"
    )
    after_sequences = _stored_output_identities(
        after_generated_outputs, output_kind="note_sequence"
    )
    before_assignments = _palette_identities(before_palette)
    after_assignments = _palette_identities(after_palette)
    regressions: list[str] = []
    unknown: list[str] = []
    note_locks = frozenset({"note_content", "rhythm", "register", "role_identity"})
    for lock in locks:
        if bool(_value(lock, "released", False)):
            continue
        lock_id = str(_value(lock, "lock_id", "accepted-lock"))
        role_id = _value(lock, "role_id")
        lock_types = {
            str(item) for item in (_value(lock, "lock_types", ()) or ())
        }
        if not isinstance(role_id, str) or not role_id.strip():
            if lock_types.intersection(note_locks | {"sound_assignment"}):
                unknown.append(
                    f"accepted lock {lock_id} has no role identity for stored-data comparison"
                )
            continue
        role_key = role_id.casefold()
        if lock_types.intersection(note_locks):
            before_identity = before_sequences.get(role_key)
            after_identity = after_sequences.get(role_key)
            if before_identity is not None and after_identity is not None:
                if before_identity != after_identity:
                    regressions.append(
                        f"accepted {role_id} note identity changed "
                        "(stored generated-sequence digest evidence)"
                    )
            else:
                unknown.append(
                    f"accepted {role_id} note identity could not be checked from both stored sequence snapshots"
                )
        if "sound_assignment" in lock_types or "role_identity" in lock_types:
            before_identity = before_assignments.get(role_key)
            after_identity = after_assignments.get(role_key)
            if before_identity is not None and after_identity is not None:
                if before_identity != after_identity:
                    regressions.append(
                        f"accepted {role_id} sound identity changed "
                        "(stored Sound Palette assignment evidence)"
                    )
            else:
                unknown.append(
                    f"accepted {role_id} sound identity could not be checked from both stored palette snapshots"
                )
    return tuple(dict.fromkeys(regressions)), tuple(dict.fromkeys(unknown))


def _aligned_scalar_features(
    before: audio.Loaded,
    after: audio.Loaded,
    *,
    alignment_lag_samples: int,
    comparison_window: tuple[float, float] | None = None,
    before_timeline_offset: float = 0.0,
    after_timeline_offset: float = 0.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    before_data, after_data = _alignment_inputs(before, after)
    if comparison_window is not None:
        before_data = _slice_to_timeline_window(
            before_data,
            before.rate,
            comparison_window,
            before_timeline_offset,
        )
        after_data = _slice_to_timeline_window(
            after_data,
            before.rate,
            comparison_window,
            after_timeline_offset,
        )
        if before_data is None or after_data is None:
            raise RevisionComparisonError(
                "explicit comparison section window has no decoded coverage"
            )
    aligned_before, aligned_after = audio._aligned_regions(
        before_data,
        after_data,
        alignment_lag_samples,
    )
    return (
        _scalar_features(aligned_before, before.rate),
        _scalar_features(aligned_after, before.rate),
    )


def compare_revision_bounces(
    before_asset: Any,
    after_asset: Any,
    *,
    section_map: Any | None = None,
    expected_objectives: Sequence[Any] = (),
    before_stems: Sequence[Any] = (),
    after_stems: Sequence[Any] = (),
    accepted_element_locks: Sequence[Any] = (),
    before_generated_outputs: Sequence[Any] = (),
    after_generated_outputs: Sequence[Any] = (),
    before_palette: Any | None = None,
    after_palette: Any | None = None,
    user_approval_state: str = "not_requested",
    max_alignment_seconds: float = 2.0,
    alignment_rate_hz: int = 2000,
    max_seconds: float | None = 600.0,
    cache: DecodedAudioCache | None = None,
    expected_revision_pass_id: str | None = None,
) -> Any:
    """Compare two supplied bounces and return a typed, non-mutating result."""

    before = _asset(before_asset, kind="before_full_mix")
    after = _asset(after_asset, kind="after_full_mix")
    actual_revision_pass_id = _value(after, "revision_pass_id")
    if expected_revision_pass_id is None:
        if actual_revision_pass_id is not None:
            raise RevisionComparisonError(
                "after_full_mix revision_pass_id requires an expected revision pass binding"
            )
    elif actual_revision_pass_id != expected_revision_pass_id:
        raise RevisionComparisonError(
            "after_full_mix revision_pass_id does not match the expected revision pass"
        )
    before_hash = _value(before, "sha256")
    after_hash = _value(after, "sha256")
    if not before_hash:
        before_hash = sha256_file(
            _canonical_path(str(_value(before, "path")), label="before_asset.path")
        )
    if not after_hash:
        after_hash = sha256_file(
            _canonical_path(str(_value(after, "path")), label="after_asset.path")
        )
    if before_hash and after_hash and before_hash == after_hash:
        raise RevisionComparisonError("before and after assets must have different SHA-256 hashes")
    cache = cache or get_decode_cache()
    try:
        before_loaded = cache.get_or_decode(before, max_seconds=max_seconds)
        after_loaded = cache.get_or_decode(after, max_seconds=max_seconds)
    except ReviewAssetError as exc:
        raise RevisionComparisonError(str(exc)) from exc
    declared_comparison_window = _explicit_common_section_window(
        section_map,
        before_loaded,
        after_loaded,
        before,
        after,
    )
    duration_delta = after_loaded.duration - before_loaded.duration
    duration_mismatch = abs(duration_delta) > max(
        0.05,
        2.0 / max(before_loaded.rate, after_loaded.rate),
    )
    comparison_window = (
        declared_comparison_window if duration_mismatch else None
    )
    limited_to_section_window = duration_mismatch and comparison_window is not None
    section_map_digest = (
        str(_value(section_map, "map_digest") or _cache_digest(section_map))
        if section_map is not None
        else ""
    )
    comparison_policy_digest = _cache_digest(
        {
            "max_seconds": max_seconds,
            "max_alignment_seconds": max_alignment_seconds,
            "alignment_rate_hz": alignment_rate_hz,
            "before_offset_seconds": _value(before, "declared_offset_seconds"),
            "after_offset_seconds": _value(after, "declared_offset_seconds"),
            "before_start_seconds": _value(before, "expected_start_seconds"),
            "after_start_seconds": _value(after, "expected_start_seconds"),
            "comparison_window": comparison_window,
        }
    )
    if cache is not None:
        alignment_result, _lag = cache.get_or_compute(
            (before, after),
            analyzer_version=f"{COMPARISON_ANALYZER_VERSION}:alignment",
            section_map_digest=section_map_digest,
            analysis_policy_digest=comparison_policy_digest,
            compute=lambda loaded: _alignment(
                loaded[0],
                loaded[1],
                before,
                after,
                max_alignment_seconds=max_alignment_seconds,
                alignment_rate_hz=alignment_rate_hz,
                comparison_window=comparison_window,
            )[:2],
            max_seconds=max_seconds,
        )
        aligned_before = aligned_after = None
        if _value(alignment_result, "state", "unknown") == "aligned":
            try:
                before_data, after_data = _alignment_inputs(before_loaded, after_loaded)
                if comparison_window is not None:
                    before_data = _slice_to_timeline_window(
                        before_data,
                        before_loaded.rate,
                        comparison_window,
                        _timeline_offset(before),
                    )
                    after_data = _slice_to_timeline_window(
                        after_data,
                        before_loaded.rate,
                        comparison_window,
                        _timeline_offset(after),
                    )
                if before_data is None or after_data is None:
                    raise RevisionComparisonError(
                        "explicit comparison section window has no decoded coverage"
                    )
                aligned_before, aligned_after = audio._aligned_regions(
                    before_data,
                    after_data,
                    _lag,
                )
            except (audio.AudioError, ValueError):
                aligned_before = aligned_after = None
    else:
        alignment_result, _lag, aligned_before, aligned_after = _alignment(
            before_loaded,
            after_loaded,
            before,
            after,
            max_alignment_seconds=max_alignment_seconds,
            alignment_rate_hz=alignment_rate_hz,
            comparison_window=comparison_window,
        )
    comparison_id = "comparison-" + hashlib.sha256(
        f"{_asset_id(before, 'before')}:{_asset_id(after, 'after')}:{COMPARISON_ANALYZER_VERSION}".encode("utf-8")
    ).hexdigest()[:24]
    alignment_state = _value(alignment_result, "state", "unknown")
    if aligned_before is None or aligned_after is None or alignment_state != "aligned":
        warning = str(_value(alignment_result, "explanation", "alignment failed"))
        return _model(
            "RevisionComparison",
            {
                "comparison_id": comparison_id,
                "before_asset": before,
                "after_asset": after,
                "alignment_result": alignment_result,
                "global_deltas": {},
                "section_deltas": (),
                "stem_deltas": (),
                "expected_objective_results": tuple(
                    _objective(item, {}) for item in expected_objectives[:MAX_OBJECTIVES]
                ),
                "regressions": (),
                "improvements": (),
                "unchanged_metrics": (),
                "unknown_metrics": ("global and section deltas withheld until bounces align",),
                "technical_conclusion": "unknown",
                "user_approval_state": user_approval_state if user_approval_state in VALID_USER_APPROVAL_STATES else "unknown",
                "next_recommendation": "Export before and after with matching start, duration, and channel settings.",
                "warnings": (warning,),
            },
        )

    if cache is not None:
        before_features, after_features = cache.get_or_compute(
            (before, after),
            analyzer_version=f"{COMPARISON_ANALYZER_VERSION}:global-features",
            section_map_digest=section_map_digest,
            analysis_policy_digest=_cache_digest(
                {"comparison_policy_digest": comparison_policy_digest, "alignment_lag": _lag}
            ),
            compute=lambda loaded: _aligned_scalar_features(
                loaded[0],
                loaded[1],
                alignment_lag_samples=_lag,
                comparison_window=comparison_window,
                before_timeline_offset=_timeline_offset(before),
                after_timeline_offset=_timeline_offset(after),
            ),
            max_seconds=max_seconds,
        )
    else:
        before_features = _scalar_features(aligned_before, before_loaded.rate)
        after_features = _scalar_features(aligned_after, before_loaded.rate)
    global_deltas = _delta_map(before_features, after_features)
    if section_map is not None:
        if cache is not None:
            section_deltas = cache.get_or_compute(
                (before, after),
                analyzer_version=f"{COMPARISON_ANALYZER_VERSION}:sections",
                section_map_digest=section_map_digest,
                analysis_policy_digest=_cache_digest(
                    {"comparison_policy_digest": comparison_policy_digest, "alignment_lag": _lag}
                ),
                compute=lambda loaded: _section_deltas(
                    loaded[0],
                    loaded[1],
                    section_map,
                    before_asset=before,
                    after_asset=after,
                    alignment_lag_samples=_lag,
                ),
                max_seconds=max_seconds,
            )
        else:
            section_deltas = _section_deltas(
                before_loaded,
                after_loaded,
                section_map,
                before_asset=before,
                after_asset=after,
                alignment_lag_samples=_lag,
            )
    else:
        section_deltas = ()
    improvements: list[str] = []
    regressions: list[str] = []
    unchanged: list[str] = []
    unknown: list[str] = []
    if limited_to_section_window:
        assert comparison_window is not None
        before_timeline_start = _timeline_offset(before)
        after_timeline_start = _timeline_offset(after)
        before_timeline_end = before_timeline_start + before_loaded.duration
        after_timeline_end = after_timeline_start + after_loaded.duration
        before_unexamined = (
            max(0.0, comparison_window[0] - before_timeline_start)
            + max(0.0, before_timeline_end - comparison_window[1])
        )
        after_unexamined = (
            max(0.0, comparison_window[0] - after_timeline_start)
            + max(0.0, after_timeline_end - comparison_window[1])
        )
        unknown.append(
            "audio outside the explicit common section window was not examined"
        )
    for key, delta in global_deltas.items():
        significance = 0.01 if key == "stereo_correlation" else 0.05
        if abs(delta) < significance:
            unchanged.append(key)
        elif key == "clipped_samples" and delta < 0:
            improvements.append("clipping")
        elif key in {"true_peak_dbtp", "sample_peak_db"} and delta < 0:
            improvements.append(key)
        elif key in {"dynamic_spread_db", "crest_factor_db", "stereo_correlation"} and delta > 0:
            improvements.append(key)
        elif key in {"dynamic_spread_db", "crest_factor_db", "stereo_correlation"} and delta < 0:
            regressions.append(key)
        elif key in {"low_end_share", "lufs_integrated", "stereo_width", "transient_density"} and abs(delta) >= 0.05:
            improvements.append(key)
    if "clipping" in improvements and any(item in regressions for item in ("dynamic_spread_db", "crest_factor_db")):
        regressions.append("reduced clipping was accompanied by reduced dynamics")
    # A broad full-mix delta cannot attribute masking to a role. Stem deltas
    # below are the only place role-specific masking regressions are reported.
    stem_deltas: list[Any] = []
    if limited_to_section_window and (before_stems or after_stems):
        unknown.append(
            "role-specific stem and masking deltas were withheld outside the explicit common section window"
        )
    before_by_role = {str(_value(item, "role_id", _value(item, "asset_kind", "role"))): item for item in before_stems}
    after_by_role = {str(_value(item, "role_id", _value(item, "asset_kind", "role"))): item for item in after_stems}
    for role_id in (
        ()
        if limited_to_section_window
        else sorted(set(before_by_role) & set(after_by_role))
    ):
        try:
            before_stem = before_by_role[role_id]
            after_stem = after_by_role[role_id]

            def compute(loaded: tuple[audio.Loaded, audio.Loaded]) -> Any:
                b_loaded, a_loaded = loaded
                if b_loaded.channels != a_loaded.channels or abs(b_loaded.duration - a_loaded.duration) > 0.05:
                    return _model("SectionDelta", {
                        "section_id": role_id,
                        "deltas": {},
                        "improvements": (),
                        "regressions": (),
                        "unknown": ("stem alignment failed",),
                    })
                b = _scalar_features(audio._channel_data(b_loaded), b_loaded.rate)
                a = _scalar_features(audio._channel_data(a_loaded), a_loaded.rate)
                delta = _delta_map(b, a)
                stem_regressions = (
                    ("stereo_correlation",)
                    if delta.get("stereo_correlation", 0.0) < -0.02
                    else ()
                )
                return _model("SectionDelta", {
                    "section_id": role_id,
                    "deltas": delta,
                    "improvements": (),
                    "regressions": stem_regressions,
                    "unknown": (),
                })

            stem_delta = cache.get_or_compute(
                (before_stem, after_stem),
                analyzer_version=f"{COMPARISON_ANALYZER_VERSION}:stem-features",
                section_map_digest=section_map_digest,
                analysis_policy_digest=_cache_digest(
                    {"comparison_policy_digest": comparison_policy_digest, "role_id": role_id}
                ),
                compute=compute,
                max_seconds=max_seconds,
            )
            stem_deltas.append(stem_delta)
            stem_regressions = _value(stem_delta, "regressions", ()) or ()
            if stem_regressions:
                regressions.append(f"{role_id} stereo correlation worsened")
        except ReviewAssetError:
            unknown.append(f"{role_id} stem delta unavailable")
    # A full-mix delta cannot establish which role caused masking.  When both
    # bounces include matching vocal/instrumental stems, reuse the evaluation
    # masking analyzer and report only a directional regression with its
    # evidence basis.  The analyzer itself remains read-only and cache-backed.
    if before_stems and after_stems and not limited_to_section_window:
        before_masking, before_masking_limits = _masking_from_stems(
            {}, before_stems, cache=cache, max_seconds=max_seconds,
            analysis_policy_digest=comparison_policy_digest,
            section_map_digest=section_map_digest,
        )
        after_masking, after_masking_limits = _masking_from_stems(
            {}, after_stems, cache=cache, max_seconds=max_seconds,
            analysis_policy_digest=comparison_policy_digest,
            section_map_digest=section_map_digest,
        )
        before_score = _number(_value(before_masking, "possible_masking_index"))
        after_score = _number(_value(after_masking, "possible_masking_index"))
        if before_score is not None and after_score is not None:
            masking_delta = round(after_score - before_score, 6)
            if masking_delta > 0.05:
                regressions.append(
                    "supplied stem masking increased "
                    f"({before_score:.3f} to {after_score:.3f}; synchronized vocal/instrumental evidence)"
                )
        elif before_masking_limits or after_masking_limits:
            unknown.append("supplied stem masking comparison unavailable")
    expected_results = tuple(
        _objective(item, global_deltas) for item in expected_objectives[:MAX_OBJECTIVES]
    )
    for result in expected_results:
        state = _value(result, "state")
        objective = str(_value(result, "objective", "objective"))
        if state in {"improved", "moved_toward_target"}:
            improvements.append(objective)
        elif state in {"regressed", "moved_away_from_target"}:
            regressions.append(objective)
        elif state == "unchanged":
            unchanged.append(objective)
        else:
            unknown.append(objective)
    identity_regressions, identity_unknown = _identity_lock_results(
        accepted_element_locks,
        before_generated_outputs=before_generated_outputs,
        after_generated_outputs=after_generated_outputs,
        before_palette=before_palette,
        after_palette=after_palette,
    )
    regressions.extend(identity_regressions)
    unknown.extend(identity_unknown)
    if not global_deltas:
        technical_conclusion = "unknown"
    elif regressions and improvements:
        technical_conclusion = "mixed"
    elif regressions:
        technical_conclusion = "regressed"
    elif improvements:
        technical_conclusion = "improved"
    else:
        technical_conclusion = "unchanged"
    warnings = [
        "Before/after measurement establishes technical movement, not artistic approval.",
        "A full-mix comparison does not assign masking or identity changes to a role without supplied synchronized stems or generated-note receipts.",
    ]
    if limited_to_section_window:
        assert comparison_window is not None
        warnings.append(
            "Full-timeline duration differs by "
            f"{duration_delta:+.3f}s; deltas are limited to the explicit common "
            f"section window {comparison_window[0]:.3f}-{comparison_window[1]:.3f}s. "
            f"Unexamined audio outside that window: before {before_unexamined:.3f}s, "
            f"after {after_unexamined:.3f}s."
        )
    next_recommendation = (
        "Listen to the revised bounce and provide explicit approval or the next bounded revision objective."
        if not regressions else "Review the listed regressions before accepting the revision."
    )
    return _model(
        "RevisionComparison",
        {
            "comparison_id": comparison_id,
            "before_asset": before,
            "after_asset": after,
            "alignment_result": alignment_result,
            "global_deltas": global_deltas,
            "section_deltas": section_deltas,
            "stem_deltas": tuple(stem_deltas),
            "expected_objective_results": expected_results,
            "regressions": tuple(dict.fromkeys(regressions)),
            "improvements": tuple(dict.fromkeys(improvements)),
            "unchanged_metrics": tuple(dict.fromkeys(unchanged)),
            "unknown_metrics": tuple(dict.fromkeys(unknown)),
            "technical_conclusion": technical_conclusion,
            "user_approval_state": user_approval_state if user_approval_state in VALID_USER_APPROVAL_STATES else "unknown",
            "next_recommendation": next_recommendation,
            "warnings": tuple(warnings),
        },
    )


compare_bounces = compare_revision_bounces
compare_revision = compare_revision_bounces
compare_before_after = compare_revision_bounces


__all__ = [
    "COMPARISON_ANALYZER_VERSION",
    "RevisionComparisonError",
    "compare_before_after",
    "compare_bounces",
    "compare_revision",
    "compare_revision_bounces",
]
