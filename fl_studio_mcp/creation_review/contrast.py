"""Arrangement proxies and directional reference comparisons.

These measurements are intentionally named proxies.  A larger loudness delta
or brighter spectrum can describe a reference relationship, but cannot prove
that one production is artistically better.  No operation in this module
mutates FL Studio or writes an audio file.
"""

# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from .. import audio
from .assets import _model, _value


CONTRAST_ANALYZER_VERSION = "creation-review-contrast-2"
MAX_REFERENCE_DIMENSIONS = 16
MAX_REFERENCE_SECTION_PAIRS = 64

# Keep this vocabulary deliberately small.  Reference comparisons are
# directional context and an unrecognised request must never silently broaden
# into every available metric.
_REFERENCE_DIMENSION_ALIASES = {
    "tonal": "tonal_balance",
    "tonality": "tonal_balance",
    "tonal_balance": "tonal_balance",
    "brightness": "tonal_balance",
    "spectrum": "tonal_balance",
    "dynamics": "dynamics",
    "dynamic": "dynamics",
    "stereo": "stereo_behavior",
    "stereo_behavior": "stereo_behavior",
    "stereo_width": "stereo_behavior",
    "low_end": "low_end_balance",
    "low_end_balance": "low_end_balance",
    "loudness": "loudness",
    "level": "loudness",
    "transient": "transient_density",
    "transient_density": "transient_density",
    "contrast": "section_contrast",
    "section_contrast": "section_contrast",
    "energy_curve": "energy_curve",
}


class ContrastAnalysisError(ValueError):
    """A contrast analysis request is malformed or cannot be aligned."""


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=False)
    return {
        key: getattr(value, key)
        for key in (
            "section_id", "name", "start_seconds", "end_seconds", "start_bar",
            "end_bar", "loudness", "energy", "rms_db", "spectral_centroid_hz",
            "low_end_share", "stereo_width", "stereo_correlation", "transient_density",
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


def _metric(features: Any, *keys: str) -> float | None:
    current = features
    for key in keys:
        if isinstance(current, Mapping):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
        if current is None:
            return None
    return _number(current)


def _section_id(section: Any, index: int) -> str:
    return str(_value(section, "section_id", _value(section, "id", f"section-{index + 1}")))


def _section_name(section: Any) -> str:
    return str(_value(section, "name", _value(section, "section_id", ""))).casefold()


def _section_feature_value(feature: Any, *keys: str) -> float | None:
    # Analysis reports commonly nest global features under ``measurements``.
    direct = _metric(feature, *keys)
    if direct is not None:
        return direct
    return _metric(feature, "measurements", *keys)


def _normalise_reference_dimensions(
    requested_dimensions: Sequence[Any] | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return explicit supported dimensions and bounded unknown requests.

    An empty request intentionally remains empty.  In particular, it must not
    turn into an implicit request for every reference metric: the reference is
    supplied as context for a stated goal, not as a universal quality target.
    """

    if requested_dimensions is None:
        return (), ()
    values: Sequence[Any]
    if isinstance(requested_dimensions, (str, bytes)):
        values = (requested_dimensions,)
    elif isinstance(requested_dimensions, Mapping):
        values = tuple(requested_dimensions.keys())
    elif not isinstance(requested_dimensions, Sequence):
        values = ()
    else:
        values = requested_dimensions[: MAX_REFERENCE_DIMENSIONS + 1]
    dimensions: list[str] = []
    unsupported: list[str] = []
    for item in values[:MAX_REFERENCE_DIMENSIONS]:
        if isinstance(item, Mapping):
            raw = item.get("dimension", item.get("metric", item.get("name", "")))
        else:
            raw = item
        text = str(raw).strip().casefold()
        canonical = _REFERENCE_DIMENSION_ALIASES.get(text)
        if canonical is None:
            if text:
                unsupported.append(text[:128])
            continue
        if canonical not in dimensions:
            dimensions.append(canonical)
    if len(values) > MAX_REFERENCE_DIMENSIONS:
        unsupported.append(
            f"reference dimension request capped at {MAX_REFERENCE_DIMENSIONS} entries"
        )
    return tuple(dimensions), tuple(dict.fromkeys(unsupported))


def _transient_density(data: np.ndarray, rate: int) -> float | None:
    """Estimate onset density from bounded frame-envelope changes.

    This is deliberately a simple proxy.  It is useful for directional
    comparison but is not a beat tracker or a claim about musical groove.
    """

    values = np.asarray(data, dtype=np.float64)
    if values.ndim == 2:
        values = np.mean(values, axis=1)
    if values.size < 256 or rate <= 0:
        return None
    window = max(64, min(2048, int(rate * 0.025)))
    if values.size < window * 2:
        return None
    frame_count = 1 + (values.size - window) // window
    frames = values[: frame_count * window].reshape(frame_count, window)
    envelope = np.sqrt(np.mean(frames ** 2, axis=1))
    changes = np.maximum(np.diff(envelope), 0.0)
    if changes.size == 0 or not np.any(changes > 0.0):
        return 0.0
    threshold = max(
        float(np.percentile(changes, 75)),
        float(np.mean(changes) + np.std(changes)),
        1e-9,
    )
    count = int(np.sum(changes >= threshold))
    return round(count / max(values.size / rate, 1e-9), 6)


def _safe_measure(callable_: Any, loaded: audio.Loaded) -> tuple[dict[str, Any] | None, str | None]:
    try:
        result = callable_(loaded)
    except Exception as exc:
        return None, str(exc)
    return dict(result) if isinstance(result, Mapping) else None, None


def _scalar_features(data: np.ndarray, rate: int) -> dict[str, Any]:
    """Extract only scalar features used by requested reference dimensions."""

    loaded = audio._loaded_from_channels("reference-comparison-window", data, rate)
    loudness, _loudness_error = _safe_measure(audio.measure_loudness, loaded)
    spectrum, _spectrum_error = _safe_measure(audio.measure_spectrum, loaded)
    dynamics, _dynamics_error = _safe_measure(audio.measure_dynamics, loaded)
    stereo, _stereo_error = _safe_measure(audio.measure_stereo, loaded)
    loudness = loudness or {}
    spectrum = spectrum or {}
    dynamics = dynamics or {}
    stereo = stereo or {}
    bands = spectrum.get("bands", {}) if isinstance(spectrum, Mapping) else {}
    low_end = None
    if isinstance(bands, Mapping) and any(name in bands for name in ("sub", "low")):
        low_end = round(
            sum(float(_value(bands.get(name, {}), "energy_share", 0.0) or 0.0) for name in ("sub", "low")),
            6,
        )
    return {
        "lufs_integrated": _number(loudness.get("lufs_integrated")),
        "dynamic_spread_db": _number(dynamics.get("dynamic_spread_db")),
        "crest_factor_db": _number(loudness.get("crest_factor_db")),
        "spectral_centroid_hz": _number(spectrum.get("spectral_centroid_hz")),
        "spectral_band_shares": {
            str(name): _number(_value(band, "energy_share"))
            for name, band in (bands.items() if isinstance(bands, Mapping) else ())
            if _number(_value(band, "energy_share")) is not None
        },
        "low_end_share": low_end,
        "stereo_width": _number(stereo.get("mid_side_ratio")),
        "stereo_correlation": _number(stereo.get("correlation")),
        "transient_density": _transient_density(data, rate),
    }


def _difference(left: Any, right: Any) -> float | None:
    a = _number(left)
    b = _number(right)
    if a is None or b is None:
        return None
    return round(b - a, 6)


def _dimension_metrics(
    reference_features: Mapping[str, Any],
    candidate_features: Mapping[str, Any],
    dimensions: Sequence[str],
) -> dict[str, Any]:
    """Build a nested candidate-minus-reference metric map.

    Keeping each requested dimension as one top-level key prevents a request
    for (for example) loudness from accidentally exposing tonal or stereo
    deltas.  ``None`` values remain visible so unavailable measurements are
    distinguishable from zero movement.
    """

    result: dict[str, Any] = {}
    for dimension in dimensions:
        if dimension == "tonal_balance":
            ref_bands = reference_features.get("spectral_band_shares", {})
            cand_bands = candidate_features.get("spectral_band_shares", {})
            bands = {
                str(name): _difference(ref_bands.get(name), cand_bands.get(name))
                for name in sorted(set(ref_bands) | set(cand_bands))
            }
            result[dimension] = {
                "spectral_band_share_deltas": bands,
                "spectral_centroid_hz_delta": _difference(
                    reference_features.get("spectral_centroid_hz"),
                    candidate_features.get("spectral_centroid_hz"),
                ),
            }
        elif dimension == "dynamics":
            result[dimension] = {
                "dynamic_spread_db_delta": _difference(
                    reference_features.get("dynamic_spread_db"),
                    candidate_features.get("dynamic_spread_db"),
                ),
                "crest_factor_db_delta": _difference(
                    reference_features.get("crest_factor_db"),
                    candidate_features.get("crest_factor_db"),
                ),
            }
        elif dimension == "stereo_behavior":
            result[dimension] = {
                "stereo_width_delta": _difference(
                    reference_features.get("stereo_width"),
                    candidate_features.get("stereo_width"),
                ),
                "stereo_correlation_delta": _difference(
                    reference_features.get("stereo_correlation"),
                    candidate_features.get("stereo_correlation"),
                ),
            }
        elif dimension == "low_end_balance":
            result[dimension] = {
                "low_end_share_delta": _difference(
                    reference_features.get("low_end_share"),
                    candidate_features.get("low_end_share"),
                ),
            }
        elif dimension == "loudness":
            result[dimension] = {
                "lufs_delta": _difference(
                    reference_features.get("lufs_integrated"),
                    candidate_features.get("lufs_integrated"),
                ),
            }
        elif dimension == "transient_density":
            result[dimension] = {
                "transient_density_delta": _difference(
                    reference_features.get("transient_density"),
                    candidate_features.get("transient_density"),
                ),
            }
        elif dimension == "section_contrast":
            # A paired section row can expose the small set of movement
            # proxies that are available from the same aligned windows.  The
            # global comparison intentionally has no section-construction
            # semantics, so this dimension is meaningful only per pair.
            result[dimension] = {
                "lufs_delta": _difference(
                    reference_features.get("lufs_integrated"),
                    candidate_features.get("lufs_integrated"),
                ),
                "spectral_centroid_hz_delta": _difference(
                    reference_features.get("spectral_centroid_hz"),
                    candidate_features.get("spectral_centroid_hz"),
                ),
                "transient_density_delta": _difference(
                    reference_features.get("transient_density"),
                    candidate_features.get("transient_density"),
                ),
            }
        elif dimension == "energy_curve":
            # ``energy_curve`` is an explicitly paired section proxy, not a
            # beat tracker or an assertion about musical expectation.
            result[dimension] = {
                "lufs_delta": _difference(
                    reference_features.get("lufs_integrated"),
                    candidate_features.get("lufs_integrated"),
                ),
                "dynamic_spread_db_delta": _difference(
                    reference_features.get("dynamic_spread_db"),
                    candidate_features.get("dynamic_spread_db"),
                ),
            }
    return result


def _find_named(sections: Sequence[Any], *needles: str) -> list[tuple[int, Any]]:
    out = []
    for index, section in enumerate(sections):
        name = _section_name(section)
        if any(needle in name for needle in needles):
            out.append((index, section))
    return out


def _pair_delta(left: Any, right: Any, key_options: Sequence[tuple[str, ...]]) -> float | None:
    for keys in key_options:
        a = _section_feature_value(left, *keys)
        b = _section_feature_value(right, *keys)
        if a is not None and b is not None:
            return round(b - a, 4)
    return None


def analyze_energy_contrast(
    sections: Sequence[Any],
    section_measurements: Mapping[str, Any] | Sequence[Any],
    *,
    max_pairs: int = 64,
) -> Any:
    """Calculate bounded section-to-section energy and contrast proxies."""

    if len(sections) > max_pairs + 1:
        sections = sections[: max_pairs + 1]
    if isinstance(section_measurements, Mapping):
        measurements = section_measurements
    else:
        measurements = {
            _section_id(value, index): value
            for index, value in enumerate(section_measurements)
        }
    pairs: list[dict[str, Any]] = []
    for index, (left, right) in enumerate(zip(sections, sections[1:])):
        left_id = _section_id(left, index)
        right_id = _section_id(right, index + 1)
        left_feature = measurements.get(left_id, {})
        right_feature = measurements.get(right_id, {})
        energy_delta = _pair_delta(
            left_feature,
            right_feature,
            (("lufs_integrated",), ("rms_db",), ("energy_db",), ("energy",)),
        )
        brightness_delta = _pair_delta(
            left_feature,
            right_feature,
            (("spectral_centroid_hz",), ("brightness",)),
        )
        low_delta = _pair_delta(
            left_feature,
            right_feature,
            (("low_end_share",), ("low_end_energy_share",)),
        )
        stereo_delta = _pair_delta(
            left_feature,
            right_feature,
            (("stereo_width",), ("mid_side_ratio",)),
        )
        transient_delta = _pair_delta(
            left_feature,
            right_feature,
            (("transient_density",), ("onset_density",)),
        )
        pairs.append(
            {
                "from_section_id": left_id,
                "to_section_id": right_id,
                "energy_delta_db": energy_delta,
                "brightness_delta": brightness_delta,
                "low_end_delta": low_delta,
                "stereo_delta": stereo_delta,
                "transient_density_delta": transient_delta,
            }
        )

    def feature(index: int, *names: str) -> Any:
        if index < 0 or index >= len(sections):
            return None
        section_id = _section_id(sections[index], index)
        return measurements.get(section_id, {})

    def transition(indices: Sequence[tuple[int, int]]) -> dict[str, Any] | None:
        for left_index, right_index in indices:
            if left_index >= len(sections) or right_index >= len(sections):
                continue
            left = feature(left_index)
            right = feature(right_index)
            energy = _pair_delta(
                left,
                right,
                (("lufs_integrated",), ("rms_db",), ("energy_db",), ("energy",)),
            )
            if energy is not None:
                return {
                    "from_section_id": _section_id(sections[left_index], left_index),
                    "to_section_id": _section_id(sections[right_index], right_index),
                    "energy_delta_db": energy,
                    "brightness_delta": _pair_delta(left, right, (("spectral_centroid_hz",), ("brightness",))),
                    "low_end_delta": _pair_delta(left, right, (("low_end_share",), ("low_end_energy_share",))),
                    "transient_density_delta": _pair_delta(left, right, (("transient_density",), ("onset_density",))),
                }
        return None

    intro = _find_named(sections, "intro", "opening")
    build = _find_named(sections, "build", "riser", "pre-drop")
    drop = _find_named(sections, "drop")
    breakdown = _find_named(sections, "breakdown", "break", "bridge")
    # Fallbacks are position-only conveniences and are labelled as such in
    # the report; they never change section boundaries.
    if not build and len(sections) >= 2:
        build = [(1, sections[1])]
    if not drop and len(sections) >= 3:
        drop = [(2, sections[2])]
    if not breakdown and len(sections) >= 4:
        breakdown = [(3, sections[3])]
    intro_index = intro[0][0] if intro else 0
    build_index = build[0][0] if build else 1
    drop_index = drop[0][0] if drop else 2
    breakdown_index = breakdown[0][0] if breakdown else 3
    build_to_drop = transition(((build_index, drop_index),))
    intro_to_build = transition(((intro_index, build_index),))
    drop_to_breakdown = transition(((drop_index, breakdown_index),))
    second_drop = [
        (index, section)
        for index, section in _find_named(sections, "drop")
        if index != drop_index
    ]
    drop_similarity: dict[str, Any] | None = None
    if second_drop:
        first = feature(drop_index)
        second_index = second_drop[0][0]
        second = feature(second_index)
        # A compact similarity proxy from normalized measured movements.  It
        # is deliberately ``None`` when no common metrics are available.
        values_a = [
            _section_feature_value(first, "lufs_integrated") or _section_feature_value(first, "rms_db"),
            _section_feature_value(first, "spectral_centroid_hz"),
            _section_feature_value(first, "low_end_share"),
            _section_feature_value(first, "transient_density") or _section_feature_value(first, "onset_density"),
        ]
        values_b = [
            _section_feature_value(second, "lufs_integrated") or _section_feature_value(second, "rms_db"),
            _section_feature_value(second, "spectral_centroid_hz"),
            _section_feature_value(second, "low_end_share"),
            _section_feature_value(second, "transient_density") or _section_feature_value(second, "onset_density"),
        ]
        pairs_numeric = [(a, b) for a, b in zip(values_a, values_b) if a is not None and b is not None]
        if pairs_numeric:
            scales = [max(abs(a), abs(b), 1e-9) for a, b in pairs_numeric]
            distance = sum(abs(a - b) / scale for (a, b), scale in zip(pairs_numeric, scales)) / len(pairs_numeric)
            drop_similarity = {
                "section_a_id": _section_id(sections[drop_index], drop_index),
                "section_b_id": _section_id(sections[second_index], second_index),
                "similarity_score": round(max(0.0, min(1.0, 1.0 - distance / 2.0)), 4),
                "basis": "shared section loudness/energy, brightness, low-end, and transient proxies",
            }
    limitations: list[str] = [
        "Energy, brightness, low-end, stereo, and drop-impact values are arrangement proxies, not proof of artistic impact.",
        "Section boundaries and labels come from supplied metadata; position fallbacks are lower-confidence suggestions.",
    ]
    if not sections:
        limitations.append("no section boundaries were available for contrast analysis")
    ending_tail_behavior = None
    if sections:
        final_index = len(sections) - 1
        final_id = _section_id(sections[final_index], final_index)
        final_features = feature(final_index)
        ending_tail_behavior = {
            "section_id": final_id,
            "sustained_tail_proportion": _section_feature_value(
                final_features, "sustained_tail_proportion"
            ),
            "silence_proportion": _section_feature_value(
                final_features, "silence_proportion"
            ),
            "basis": "last supplied section measurement",
        }
    payload = {
        "analyzer_version": CONTRAST_ANALYZER_VERSION,
        "pairs": tuple(pairs),
        "intro_to_build": intro_to_build,
        "build_progression": tuple(pairs[max(0, intro_index): max(0, build_index)]),
        "build_to_drop": build_to_drop,
        "drop_impact_proxy": build_to_drop,
        "drop_to_breakdown_relief": drop_to_breakdown,
        "breakdown_to_second_drop": transition(((breakdown_index, second_drop[0][0]),)) if second_drop else None,
        "drop_a_drop_b_similarity": drop_similarity,
        "density_movement": tuple(
            {"from_section_id": pair["from_section_id"], "to_section_id": pair["to_section_id"], "delta": pair["transient_density_delta"]}
            for pair in pairs
            if pair["transient_density_delta"] is not None
        ),
        "brightness_movement": tuple(
            {"from_section_id": pair["from_section_id"], "to_section_id": pair["to_section_id"], "delta": pair["brightness_delta"]}
            for pair in pairs
            if pair["brightness_delta"] is not None
        ),
        "low_end_movement": tuple(
            {"from_section_id": pair["from_section_id"], "to_section_id": pair["to_section_id"], "delta": pair["low_end_delta"]}
            for pair in pairs
            if pair["low_end_delta"] is not None
        ),
        "stereo_movement": tuple(
            {"from_section_id": pair["from_section_id"], "to_section_id": pair["to_section_id"], "delta": pair["stereo_delta"]}
            for pair in pairs
            if pair["stereo_delta"] is not None
        ),
        "ending_tail_behavior": ending_tail_behavior,
        "limitations": tuple(limitations),
    }
    return _model("EnergyContrastAnalysis", payload)


def _loaded_features(loaded: audio.Loaded) -> dict[str, Any]:
    return _scalar_features(audio._channel_data(loaded), loaded.rate)


def _aligned_reference_payload(
    reference: audio.Loaded,
    candidate: audio.Loaded,
    *,
    max_alignment_seconds: float = 2.0,
    alignment_rate_hz: int = 2000,
) -> tuple[dict[str, Any], np.ndarray, np.ndarray]:
    reference_data = audio._channel_data(reference)
    candidate_data = audio._channel_data(candidate)
    if candidate.rate != reference.rate:
        candidate_data = audio._resample_channels(candidate_data, candidate.rate, reference.rate)
    alignment, lag = audio._estimate_alignment(
        reference_data,
        candidate_data,
        reference.rate,
        max_alignment_seconds,
        alignment_rate_hz,
    )
    aligned_reference, aligned_candidate = audio._aligned_regions(reference_data, candidate_data, lag)
    matched_candidate, matching = audio._loudness_match(
        aligned_reference,
        aligned_candidate,
        reference.rate,
    )
    return {
        "alignment": alignment,
        "loudness_matching": matching,
        "rate_conversion": (
            {"from_hz": candidate.rate, "to_hz": reference.rate, "method": "scipy-resample-poly"}
            if candidate.rate != reference.rate else None
        ),
    }, aligned_reference, matched_candidate


def _section_side(value: Any, side: str) -> dict[str, Any]:
    """Read one side of an explicit reference/candidate section pair."""

    nested = value
    if isinstance(value, Mapping):
        nested = value.get(f"{side}_section", value.get(side, value))
    section_id = _value(nested, "section_id", _value(nested, "id"))
    start = _value(nested, "start_seconds")
    end = _value(nested, "end_seconds")
    if isinstance(value, Mapping):
        section_id = value.get(
            f"{side}_section_id",
            value.get(f"{side}_id", section_id),
        )
        start = value.get(
            f"{side}_start_seconds",
            value.get(f"{side}_start", start),
        )
        end = value.get(
            f"{side}_end_seconds",
            value.get(f"{side}_end", end),
        )
    result: dict[str, Any] = {
        "section_id": str(section_id) if section_id is not None else None,
        "start_seconds": _number(start),
        "end_seconds": _number(end),
    }
    return result


def _normalise_section_pair(value: Any) -> dict[str, Any] | None:
    """Normalise a pair while refusing ID-only pairs for metric work.

    IDs remain useful traceability, but they do not identify samples.  A pair
    therefore needs explicit boundaries on both sides before it can produce a
    section delta.
    """

    if isinstance(value, (list, tuple)) and len(value) == 2:
        reference = _section_side(value[0], "reference")
        candidate = _section_side(value[1], "candidate")
    elif isinstance(value, Mapping):
        reference = _section_side(value, "reference")
        candidate = _section_side(value, "candidate")
    else:
        return None
    if reference["section_id"] is None or candidate["section_id"] is None:
        return None
    return {"reference": reference, "candidate": candidate}


def _canonical_section_pair(value: Any) -> Any:
    """Return a stable, bounded cache-key representation for one pair."""

    normal = _normalise_section_pair(value)
    if normal is not None:
        return normal
    if isinstance(value, Mapping):
        return {
            str(key): str(item)[:256]
            for key, item in list(value.items())[:16]
        }
    if isinstance(value, (list, tuple)):
        return tuple(str(item)[:256] for item in value[:4])
    return str(value)[:256]


def _section_pair_values(section_pairs: Sequence[Any] | Mapping[str, Any] | None) -> tuple[Any, ...]:
    if section_pairs is None or isinstance(section_pairs, (str, bytes)):
        return ()
    if isinstance(section_pairs, Mapping):
        pair_keys = {
            "reference",
            "candidate",
            "reference_section",
            "candidate_section",
            "reference_section_id",
            "candidate_section_id",
            "reference_id",
            "candidate_id",
        }
        if pair_keys.intersection(section_pairs):
            return (section_pairs,)
        # A named mapping (for example ``{"drop": {"reference": ...}}``)
        # is a convenient caller shape; retain the bounded pair values while
        # refusing any value that lacks explicit boundaries later.
        return tuple(section_pairs.values())[: MAX_REFERENCE_SECTION_PAIRS + 1]
    if not isinstance(section_pairs, Sequence):
        return ()
    return tuple(section_pairs[: MAX_REFERENCE_SECTION_PAIRS + 1])


def _window(
    data: np.ndarray,
    rate: int,
    *,
    start_seconds: float,
    end_seconds: float,
    origin_sample: int,
) -> tuple[np.ndarray | None, float]:
    """Slice a section against the aligned overlap and report coverage."""

    if end_seconds <= start_seconds:
        return None, 0.0
    start = int(math.floor(start_seconds * rate)) - int(origin_sample)
    end = int(math.ceil(end_seconds * rate)) - int(origin_sample)
    requested = max(1, end - start)
    clipped_start = max(0, start)
    clipped_end = min(len(data), end)
    if clipped_end <= clipped_start:
        return None, 0.0
    coverage = (clipped_end - clipped_start) / requested
    return data[clipped_start:clipped_end], min(1.0, max(0.0, coverage))


def _paired_section_metrics(
    reference_data: np.ndarray,
    candidate_data: np.ndarray,
    *,
    alignment_payload: Mapping[str, Any],
    rate: int,
    dimensions: Sequence[str],
    section_pairs: Sequence[Any] | Mapping[str, Any],
) -> tuple[tuple[dict[str, Any], ...], tuple[tuple[str, str], ...], tuple[str, ...]]:
    """Calculate bounded metrics for explicitly bounded section pairs."""

    pair_values = _section_pair_values(section_pairs)
    if not pair_values:
        return (), (), ()
    alignment = alignment_payload.get("alignment", {})
    ref_origin = int(_number(alignment.get("reference_common_start_sample")) or 0)
    candidate_origin = int(
        _number(alignment.get("target_common_start_sample_at_reference_rate")) or 0
    )
    if rate <= 0:
        return (), (), ("section metric sample rate was unavailable; metrics withheld",)
    rows: list[dict[str, Any]] = []
    paired_ids: list[tuple[str, str]] = []
    limitations: list[str] = []
    for raw_pair in pair_values[:MAX_REFERENCE_SECTION_PAIRS]:
        pair = _normalise_section_pair(raw_pair)
        if pair is None:
            limitations.append(
                "section pair lacks explicit reference/candidate IDs and boundaries; metrics withheld"
            )
            continue
        reference_section = pair["reference"]
        candidate_section = pair["candidate"]
        ref_start = reference_section["start_seconds"]
        ref_end = reference_section["end_seconds"]
        cand_start = candidate_section["start_seconds"]
        cand_end = candidate_section["end_seconds"]
        if (
            ref_start is None
            or ref_end is None
            or cand_start is None
            or cand_end is None
            or ref_end <= ref_start
            or cand_end <= cand_start
        ):
            limitations.append(
                f"section pair {reference_section['section_id']}->{candidate_section['section_id']} has invalid boundaries; metrics withheld"
            )
            continue
        ref_window, ref_coverage = _window(
            reference_data,
            rate,
            start_seconds=ref_start,
            end_seconds=ref_end,
            origin_sample=ref_origin,
        )
        if ref_window is None:
            limitations.append(
                f"reference section {reference_section['section_id']} has no aligned audio coverage; metrics withheld"
            )
            continue
        cand_window, cand_coverage = _window(
            candidate_data,
            rate,
            start_seconds=cand_start,
            end_seconds=cand_end,
            origin_sample=candidate_origin,
        )
        if cand_window is None:
            limitations.append(
                f"candidate section {candidate_section['section_id']} has no aligned audio coverage; metrics withheld"
            )
            continue
        if ref_coverage < 0.8 or cand_coverage < 0.8:
            limitations.append(
                f"section pair {reference_section['section_id']}->{candidate_section['section_id']} has insufficient sample coverage; metrics withheld"
            )
            continue
        reference_features = _scalar_features(ref_window, rate)
        candidate_features = _scalar_features(cand_window, rate)
        metrics = _dimension_metrics(reference_features, candidate_features, dimensions)
        if not metrics:
            limitations.append(
                f"section pair {reference_section['section_id']}->{candidate_section['section_id']} has no requested section metric"
            )
            continue
        pair_row = {
            "reference_section_id": reference_section["section_id"],
            "candidate_section_id": candidate_section["section_id"],
            "reference_start_seconds": round(ref_start, 6),
            "reference_end_seconds": round(ref_end, 6),
            "candidate_start_seconds": round(cand_start, 6),
            "candidate_end_seconds": round(cand_end, 6),
            "reference_sample_coverage": round(ref_coverage, 6),
            "candidate_sample_coverage": round(cand_coverage, 6),
            "metrics": metrics,
            "basis": "explicit paired section boundaries within the aligned overlap",
        }
        rows.append(pair_row)
        paired_ids.append((reference_section["section_id"], candidate_section["section_id"]))
    if len(pair_values) > MAX_REFERENCE_SECTION_PAIRS:
        limitations.append(
            f"reference section pairing capped at {MAX_REFERENCE_SECTION_PAIRS} pairs"
        )
    return tuple(rows), tuple(paired_ids), tuple(dict.fromkeys(limitations))


def _compare_reference_features_uncached(
    reference: audio.Loaded,
    candidate: audio.Loaded,
    *,
    reference_asset_id: str | None = None,
    candidate_asset_id: str | None = None,
    comparison_id: str | None = None,
    requested_dimensions: Sequence[Any] = (),
    section_pairs: Sequence[Any] | Mapping[str, Any] = (),
    max_alignment_seconds: float = 2.0,
    alignment_rate_hz: int = 2000,
) -> Any:
    """Compare requested directional properties against a user reference."""

    dimensions, unsupported_dimensions = _normalise_reference_dimensions(
        requested_dimensions
    )
    try:
        alignment_payload, reference_data, candidate_data = _aligned_reference_payload(
            reference,
            candidate,
            max_alignment_seconds=max_alignment_seconds,
            alignment_rate_hz=alignment_rate_hz,
        )
    except Exception as exc:
        reference_id = reference_asset_id or "reference-unknown"
        candidate_id = candidate_asset_id or "candidate-unknown"
        return _model(
            "ReferenceComparison",
            {
                "comparison_id": comparison_id or "reference-comparison-failed",
                "reference_asset_id": reference_id,
                "candidate_asset_id": candidate_id,
                "requested_dimensions": dimensions,
                "measurements": {
                    "comparison_ready": False,
                    "alignment": None,
                    "comparisons": {},
                    "paired_section_metrics": (),
                    "section_comparisons": (),
                    "directional_only": True,
                    "reference_not_defect": True,
                    "interpretation": "reference alignment failed; directional comparison is withheld",
                },
                "alignment_state": "failed",
                "findings": (),
                "limitations": (
                    f"reference alignment failed: {exc}",
                    *(
                        ("Unsupported reference dimensions were ignored: " + ", ".join(unsupported_dimensions),)
                        if unsupported_dimensions else ()
                    ),
                    "reference comparison provides directional evidence only; it is not a defect finding",
                ),
            },
        )
    ref_loaded = audio._loaded_from_channels("reference-aligned", reference_data, reference.rate)
    candidate_loaded = audio._loaded_from_channels("candidate-aligned", candidate_data, reference.rate)
    ref_features = _loaded_features(ref_loaded)
    target_features = _loaded_features(candidate_loaded)
    comparisons = _dimension_metrics(ref_features, target_features, dimensions)
    limitations = [
        "A reference is directional evidence, not a defect finding, copy target, or universal quality ranking.",
        "Only explicitly requested dimensions should influence connected-AI revision planning.",
        "Comparison does not transcribe, copy, or generate the reference melody, progression, or arrangement.",
    ]
    if unsupported_dimensions:
        limitations.append(
            "Unsupported reference dimensions were ignored: "
            + ", ".join(unsupported_dimensions)
        )
    if not dimensions:
        limitations.append("no supported reference dimensions were explicitly requested")
    ready = bool(
        alignment_payload["alignment"].get("confidence", {}).get("level") == "high"
        and alignment_payload["alignment"].get("common_coverage", 0.0) >= 0.8
        and alignment_payload["loudness_matching"].get("applied")
    )
    if not ready:
        limitations.append("alignment or loudness matching confidence is insufficient for strong conclusions")
        # Retain the alignment/matching audit trail above, but withhold
        # directional feature deltas when the common timeline is ambiguous.
        comparisons = {}
    section_metrics: tuple[dict[str, Any], ...] = ()
    paired_ids: tuple[tuple[str, str], ...] = ()
    section_limitations: tuple[str, ...] = ()
    if ready and section_pairs:
        section_metrics, paired_ids, section_limitations = _paired_section_metrics(
            reference_data,
            candidate_data,
            alignment_payload=alignment_payload,
            rate=reference.rate,
            dimensions=dimensions,
            section_pairs=section_pairs,
        )
        limitations.extend(section_limitations)
    elif section_pairs:
        limitations.append(
            "aligned section metrics were withheld because global reference alignment was not ready"
        )
    reference_id = reference_asset_id or "reference-unknown"
    candidate_id = candidate_asset_id or "candidate-unknown"
    stable_id = comparison_id or "reference-comparison-" + hashlib.sha256(
        f"{reference_id}:{candidate_id}:{','.join(f'{a}:{b}' for a, b in paired_ids)}".encode("utf-8")
    ).hexdigest()[:24]
    payload = {
        "comparison_id": stable_id,
        "reference_asset_id": reference_id,
        "candidate_asset_id": candidate_id,
        "requested_dimensions": dimensions,
        "measurements": {
            "comparison_ready": ready,
            "alignment": alignment_payload["alignment"],
            "loudness_matching": alignment_payload["loudness_matching"],
            "rate_conversion": alignment_payload["rate_conversion"],
            "comparisons": comparisons,
            "paired_section_metrics": section_metrics,
            # A stable alias is useful to clients that call these rows section
            # comparisons; both names contain the same bounded rows.
            "section_comparisons": section_metrics,
            "directional_only": True,
            "reference_not_defect": True,
            "interpretation": "candidate-minus-reference directional context; not an automatic defect",
        },
        "paired_section_ids": paired_ids,
        "alignment_state": "aligned" if ready else "failed",
        "findings": (),
        "limitations": tuple(limitations),
    }
    return _model("ReferenceComparison", payload)


def compare_reference_features(
    reference: audio.Loaded,
    candidate: audio.Loaded,
    *,
    reference_asset_id: str | None = None,
    candidate_asset_id: str | None = None,
    comparison_id: str | None = None,
    requested_dimensions: Sequence[Any] = (),
    section_pairs: Sequence[Any] | Mapping[str, Any] = (),
    max_alignment_seconds: float = 2.0,
    alignment_rate_hz: int = 2000,
    cache: Any | None = None,
    reference_asset: Any | None = None,
    candidate_asset: Any | None = None,
    section_map_digest: str = "",
    analysis_policy_digest: str = "",
    max_seconds: float | None = None,
) -> Any:
    """Compare a reference and candidate, reusing cached alignment/features.

    The low-level helper remains usable with manually-created ``Loaded``
    values.  When a :class:`DecodedAudioCache` is supplied, validated asset
    metadata is preferred for source-file identity and the complete immutable
    comparison result is retained under both file hashes and all comparison
    controls that can affect the result.
    """

    if cache is None:
        return _compare_reference_features_uncached(
            reference,
            candidate,
            reference_asset_id=reference_asset_id,
            candidate_asset_id=candidate_asset_id,
            comparison_id=comparison_id,
            requested_dimensions=requested_dimensions,
            section_pairs=section_pairs,
            max_alignment_seconds=max_alignment_seconds,
            alignment_rate_hz=alignment_rate_hz,
        )

    dimensions, unsupported_dimensions = _normalise_reference_dimensions(
        requested_dimensions
    )
    cache_dimensions = dimensions
    cache_reference = reference_asset or reference
    cache_candidate = candidate_asset or candidate
    comparison_policy_digest = hashlib.sha256(
        json.dumps(
            {
                "analysis_policy_digest": analysis_policy_digest,
                "reference_asset_id": reference_asset_id,
                "candidate_asset_id": candidate_asset_id,
                "comparison_id": comparison_id,
                "requested_dimensions": cache_dimensions,
                "unsupported_dimensions": unsupported_dimensions,
                "section_pairs": tuple(
                    _canonical_section_pair(item)
                    for item in _section_pair_values(section_pairs)[:MAX_REFERENCE_SECTION_PAIRS]
                ),
                "max_alignment_seconds": max_alignment_seconds,
                "alignment_rate_hz": alignment_rate_hz,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return cache.get_or_compute(
        (cache_reference, cache_candidate),
        analyzer_version=f"{CONTRAST_ANALYZER_VERSION}:reference-comparison",
        section_map_digest=section_map_digest,
        analysis_policy_digest=comparison_policy_digest,
        compute=lambda loaded: _compare_reference_features_uncached(
            loaded[0],
            loaded[1],
            reference_asset_id=reference_asset_id,
            candidate_asset_id=candidate_asset_id,
            comparison_id=comparison_id,
            requested_dimensions=requested_dimensions,
            section_pairs=_section_pair_values(section_pairs),
            max_alignment_seconds=max_alignment_seconds,
            alignment_rate_hz=alignment_rate_hz,
        ),
        max_seconds=max_seconds,
    )


# Names likely used by callers and older branches.
calculate_energy_contrast = analyze_energy_contrast
analyze_arrangement_contrast = analyze_energy_contrast
compare_reference = compare_reference_features


__all__ = [
    "CONTRAST_ANALYZER_VERSION",
    "ContrastAnalysisError",
    "analyze_arrangement_contrast",
    "analyze_energy_contrast",
    "calculate_energy_contrast",
    "compare_reference",
    "compare_reference_features",
]
