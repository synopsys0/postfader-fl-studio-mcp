"""Decoded-audio evaluation for Creation Review.

The existing :mod:`fl_studio_mcp.audio` module remains the DSP authority.  We
reuse its level, spectrum, dynamics, stereo, alignment, and masking routines
over samples obtained from :class:`~creation_review.assets.DecodedAudioCache`.
This layer adds section windows, arrangement proxies, source-run note evidence,
and bounded evidence-aware findings; it does not mutate a project.
"""

# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

import numpy as np
from scipy import signal

from .. import audio
from .assets import (
    DecodedAudioCache,
    ReviewAssetError,
    _model,
    _value,
    build_review_asset_set,
    get_decode_cache,
    validate_audio_asset,
)
from .contrast import analyze_energy_contrast, compare_reference_features
from .findings import build_evaluation_findings, rank_findings
from .sections import build_review_section_map


EVALUATION_ANALYZER_VERSION = "creation-review-evaluation-2"
MAX_SECTION_MEASUREMENTS = 64
MAX_STEM_MEASUREMENTS = 32
MAX_NOTE_SEQUENCES = 64
MAX_UNAVAILABLE_ANALYSES = 32
MAX_TIMING_RECORDS = 16
MAX_GOAL_EVALUATIONS = 32
MAX_GOAL_TEXT = 512
MAX_NOTE_EVENTS = 4096
MAX_NOTE_COMPARISONS = 32

GOAL_EVALUATION_STATES = frozenset(
    {
        "technically_evaluable",
        "proxy_evaluable",
        "requires_user_judgment",
        "not_evaluable_from_supplied_assets",
    }
)


class CreationEvaluationError(ValueError):
    """Evaluation could not be completed from the supplied review context."""


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=False)
    return {
        key: getattr(value, key)
        for key in (
            "asset_id", "asset_kind", "path", "sha256", "role_id", "section_id",
            "duration_seconds", "sample_rate_hz", "channels", "declared_offset_seconds",
            "sections", "global_measurements", "loudness", "spectrum", "dynamics",
            "stereo", "notes", "note_count", "duration_beats", "tempo_bpm",
            "note_digest_sha256", "output", "output_kind", "value", "metadata", "operation_id", "role_id",
            "state", "direction", "target_value", "tolerance", "unit", "metric", "goal",
            "objective", "description", "requested_target", "rationale", "goals", "objectives",
            "requested_goals", "processing", "processing_outcome", "actions", "candidates",
            "missing_capabilities", "requested_count", "attempted_count", "completed", "verified",
            "outcome_known", "results", "applied_actions", "verified_actions", "missing_effects",
            "unresolved_controls", "warnings", "plan_id", "status",
        )
        if hasattr(value, key)
    }


def _section_value(section: Any, key: str, default: Any = None) -> Any:
    return _value(section, key, default)


def _loaded_segment(loaded: audio.Loaded, start_seconds: float, end_seconds: float, *, timeline_offset_seconds: float = 0.0) -> tuple[audio.Loaded | None, float]:
    """Slice a cached Loaded value and return sample coverage in [0, 1]."""

    requested = max(0.0, float(end_seconds) - float(start_seconds))
    if requested <= 0:
        return None, 0.0
    local_start = float(start_seconds) - float(timeline_offset_seconds)
    local_end = float(end_seconds) - float(timeline_offset_seconds)
    start = int(math.floor(local_start * loaded.rate))
    end = int(math.ceil(local_end * loaded.rate))
    requested_frames = max(1, end - start)
    clipped_start = max(0, start)
    clipped_end = min(len(loaded.samples), end)
    if clipped_end <= clipped_start:
        return None, 0.0
    data = audio._channel_data(loaded)[clipped_start:clipped_end]
    segment = audio._loaded_from_channels(
        f"{loaded.path}#section:{max(0, start)}:{max(0, end)}",
        data,
        loaded.rate,
    )
    coverage = min(1.0, max(0.0, (clipped_end - clipped_start) / requested_frames))
    segment.meta.update({
        "section_source_start_sample": start,
        "section_source_end_sample_exclusive": end,
        "section_analyzed_frames": int(clipped_end - clipped_start),
        "section_requested_frames": int(requested_frames),
    })
    return segment, coverage


def _safe_measure(callable_, loaded: audio.Loaded) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return callable_(loaded), None
    except (audio.AudioError, ValueError, FloatingPointError) as exc:
        return None, str(exc)


def _onset_transient_features(loaded: audio.Loaded) -> dict[str, Any]:
    mono = np.asarray(loaded.samples, dtype=np.float64)
    rate = int(loaded.rate)
    nperseg = min(2048, len(mono))
    if nperseg < 256:
        raise audio.AudioError("section is too short for onset analysis")
    hop = max(64, nperseg // 4)
    _freqs, _times, spectrum = signal.stft(
        mono,
        fs=rate,
        window="hann",
        nperseg=nperseg,
        noverlap=nperseg - hop,
        boundary=None,
        padded=False,
    )
    magnitude = np.log1p(100.0 * np.abs(spectrum))
    flux = np.sum(np.maximum(np.diff(magnitude, axis=1), 0.0), axis=0)
    if flux.size == 0:
        return {"onset_density": 0.0, "transient_strength": 0.0, "onset_count": 0}
    threshold = max(float(np.percentile(flux, 75)), float(np.mean(flux) + np.std(flux)))
    peaks, _ = signal.find_peaks(flux, height=threshold, distance=max(1, int(0.04 * rate / hop)))
    duration = max(len(mono) / rate, 1e-9)
    return {
        "onset_density": round(float(len(peaks) / duration), 4),
        "transient_strength": round(float(np.percentile(flux, 90)), 4),
        "onset_count": int(len(peaks)),
    }


def _sustained_tail_proportion(loaded: audio.Loaded) -> float:
    mono = np.asarray(loaded.samples, dtype=np.float64)
    if len(mono) < 256:
        return 0.0
    window = max(128, int(0.05 * loaded.rate))
    if len(mono) < window:
        return 0.0
    frames = np.lib.stride_tricks.sliding_window_view(mono, window)[:: max(1, window // 2)]
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    if not np.any(rms > 1e-9):
        return 0.0
    threshold = max(float(np.percentile(rms, 35)), 1e-6)
    tail_start = max(0, int(len(rms) * 0.8))
    return round(float(np.mean(rms[tail_start:] > threshold)), 4)


def _section_measurement(loaded: audio.Loaded, *, coverage: float, section_duration: float) -> dict[str, Any]:
    loudness, loudness_error = _safe_measure(audio.measure_loudness, loaded)
    spectrum, spectrum_error = _safe_measure(audio.measure_spectrum, loaded)
    dynamics, dynamics_error = _safe_measure(audio.measure_dynamics, loaded)
    stereo, stereo_error = _safe_measure(audio.measure_stereo, loaded)
    try:
        transients = _onset_transient_features(loaded)
        transient_error = None
    except (audio.AudioError, ValueError, FloatingPointError) as exc:
        transients = {}
        transient_error = str(exc)
    errors = [
        error for error in (
            loudness_error, spectrum_error, dynamics_error, stereo_error, transient_error
        ) if error
    ]
    bands = _value(spectrum, "bands", {}) if spectrum else {}
    low_end_share = None
    if isinstance(bands, Mapping):
        low_end_share = round(sum(float(_value(bands.get(name, {}), "energy_share", 0.0) or 0.0) for name in ("sub", "low")), 6)
    sample_count = len(loaded.samples)
    result = {
        "section_duration_seconds": round(float(section_duration), 6),
        "sample_coverage": round(float(coverage), 6),
        "sample_count": int(sample_count),
        "sample_peak_db": _value(loudness, "sample_peak_db"),
        "true_peak_dbtp": _value(loudness, "true_peak_dbtp"),
        "lufs_integrated": _value(loudness, "lufs_integrated"),
        "rms_db": _value(loudness, "rms_db"),
        "crest_factor_db": _value(loudness, "crest_factor_db"),
        "dynamic_spread_db": _value(dynamics, "dynamic_spread_db"),
        "spectral_band_shares": {
            name: _value(band, "energy_share") for name, band in (bands.items() if isinstance(bands, Mapping) else ())
        },
        "spectral_centroid_hz": _value(spectrum, "spectral_centroid_hz"),
        "low_end_share": low_end_share,
        "stereo_width": _value(stereo, "mid_side_ratio"),
        "stereo_correlation": _value(stereo, "correlation"),
        "onset_density": transients.get("onset_density"),
        "onset_count": transients.get("onset_count"),
        "transient_strength": transients.get("transient_strength"),
        "silence_proportion": _value(dynamics, "silence_share"),
        "sustained_tail_proportion": _sustained_tail_proportion(loaded),
        "confidence": 0.9 if coverage >= 0.999 and not errors else 0.65 if coverage >= 0.75 else 0.35,
        "limitations": tuple(errors),
    }
    return result


def _global_measurement(loaded: audio.Loaded) -> dict[str, Any]:
    loudness, loudness_error = _safe_measure(audio.measure_loudness, loaded)
    spectrum, spectrum_error = _safe_measure(audio.measure_spectrum, loaded)
    dynamics, dynamics_error = _safe_measure(audio.measure_dynamics, loaded)
    stereo, stereo_error = _safe_measure(audio.measure_stereo, loaded)
    measurement_errors = tuple(
        error
        for error in (loudness_error, spectrum_error, dynamics_error, stereo_error)
        if error
    )
    limitations = [
        "Global values reuse the existing PostFader decoded-audio analyzers.",
        "Measured values describe this bounce and do not establish artistic approval.",
    ]
    limitations.extend(measurement_errors)
    return {
        "loudness": loudness,
        "spectrum": spectrum,
        "dynamics": dynamics,
        "stereo": stereo,
        "duration_seconds": round(float(loaded.duration), 6),
        "sample_rate_hz": int(loaded.rate),
        "channels": int(loaded.channels),
        "source_duration_seconds": float(loaded.meta.get("source_duration_sec", loaded.duration)),
        "analyzed_duration_seconds": float(loaded.duration),
        "truncated": bool(loaded.meta.get("truncated", False)),
        "analyzer_version": EVALUATION_ANALYZER_VERSION,
        "limitations": tuple(limitations),
    }


def _asset_from_set(asset_set: Any, kind_preference: Sequence[str] = ()) -> Any | None:
    for key in kind_preference:
        value = _value(asset_set, key)
        if value is not None:
            return value
    assets = _value(asset_set, "assets", ()) or ()
    for value in assets:
        if _value(value, "asset_kind") in kind_preference:
            return value
    return None


def _assets_from_set(asset_set: Any) -> tuple[Any, ...]:
    values = _value(asset_set, "assets")
    if values:
        return tuple(values)
    values = []
    for key in (
        "candidate_full_mix", "before_full_mix", "after_full_mix", "candidate", "before", "after",
        "reference", "synchronized_stems", "stems", "section_bounces",
    ):
        value = _value(asset_set, key)
        if value is None:
            continue
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, Mapping)):
            values.extend(value)
        else:
            values.append(value)
    return tuple(values)


def _section_measurements(
    loaded: audio.Loaded,
    sections: Sequence[Any],
    *,
    timeline_offset_seconds: float = 0.0,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Measure all bounded section windows for one decoded source.

    Keeping the complete section pass as one derived value means callers that
    request the same bounce with the same section map do not repeat the DSP
    work for every section.  The unavailable rows are cached with the values
    so a short/truncated section produces the same truthful report on a hit.
    """

    measurements: dict[str, Any] = {}
    unavailable: list[str] = []
    for index, section in enumerate(sections[:MAX_SECTION_MEASUREMENTS]):
        section_id = str(_value(section, "section_id", f"section-{index + 1}"))
        start_seconds = float(_value(section, "start_seconds", 0.0))
        end_seconds = float(_value(section, "end_seconds", 0.0))
        segment, coverage = _loaded_segment(
            loaded,
            start_seconds,
            end_seconds,
            timeline_offset_seconds=timeline_offset_seconds,
        )
        if segment is None:
            unavailable.append(f"section {section_id}: no audio coverage")
            continue
        measurements[section_id] = _section_measurement(
            segment,
            coverage=coverage,
            section_duration=end_seconds - start_seconds,
        )
    if len(sections) > MAX_SECTION_MEASUREMENTS:
        unavailable.append(f"section analysis capped at {MAX_SECTION_MEASUREMENTS} sections")
    return measurements, tuple(unavailable)


def _stem_measurements(
    stems: Sequence[Any],
    *,
    cache: DecodedAudioCache,
    max_seconds: float | None,
    section_map: Any,
    analysis_policy_digest: str = "",
) -> tuple[dict[str, Any], list[str]]:
    values: dict[str, Any] = {}
    unavailable: list[str] = []
    for stem in stems[:MAX_STEM_MEASUREMENTS]:
        role_value = _value(stem, "role_id") or _value(stem, "asset_kind") or "role_stem"
        role_id = str(role_value)
        try:
            stem_policy_digest = _canonical_digest(
                {
                    "analysis_policy_digest": analysis_policy_digest,
                    "role_id": role_id,
                    "asset_kind": _value(stem, "asset_kind"),
                    "declared_offset_seconds": _value(stem, "declared_offset_seconds", 0.0),
                }
            )
            stem_sections = tuple(_value(section_map, "sections", ()) or ())

            def compute(loaded: audio.Loaded) -> dict[str, Any]:
                sections, _section_unavailable = _section_measurements(
                    loaded,
                    stem_sections,
                    timeline_offset_seconds=float(
                        _value(stem, "declared_offset_seconds", 0.0) or 0.0
                    ),
                )
                return {
                    "asset_id": _value(stem, "asset_id"),
                    "role_id": role_id,
                    "asset_kind": _value(stem, "asset_kind"),
                    "global": _global_measurement(loaded),
                    "sections": sections,
                }

            values[role_id] = cache.get_or_compute_features(
                stem,
                analyzer_version=f"{EVALUATION_ANALYZER_VERSION}:stem",
                section_map_digest=str(
                    _value(section_map, "map_digest") or _canonical_digest(section_map)
                ),
                analysis_policy_digest=stem_policy_digest,
                compute=compute,
                max_seconds=max_seconds,
            )
        except ReviewAssetError as exc:
            unavailable.append(f"stem {role_id}: {exc}")
    if len(stems) > MAX_STEM_MEASUREMENTS:
        unavailable.append(f"stem analysis capped at {MAX_STEM_MEASUREMENTS} assets")
    return values, unavailable


def _masking_measurement(
    vocal_loaded: audio.Loaded,
    instrument_loaded: audio.Loaded,
    *,
    vocal: Any,
    instrumental: Any,
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    """Calculate masking evidence from two already-decoded synchronized stems."""

    reasons: list[str] = []
    if vocal_loaded.rate != instrument_loaded.rate:
        reasons.append("sample rates differ")
    if len(vocal_loaded.samples) != len(instrument_loaded.samples):
        reasons.append("decoded frame counts differ")
    if reasons:
        return {
            "context_ready": False,
            "attribution_confidence": "unknown",
            "readiness_reasons": tuple(reasons),
            "vocal_role_id": _value(vocal, "role_id") or "vocal",
            "instrument_role_id": _value(instrumental, "role_id") or "instrumental",
        }, ("synchronized stem masking is unavailable",)
    try:
        frequencies, _times, vocal_power = audio._channel_power_spectrogram(audio._channel_data(vocal_loaded), vocal_loaded.rate)
        _instrument_frequencies, _instrument_times, instrumental_power = audio._channel_power_spectrogram(audio._channel_data(instrument_loaded), instrument_loaded.rate)
    except (audio.AudioError, ValueError, FloatingPointError) as exc:
        return None, (f"masking analysis unavailable: {exc}",)
    voice_band = (frequencies >= 80.0) & (frequencies < 12000.0)
    vocal_frame = np.sum(vocal_power[voice_band], axis=0)
    if vocal_frame.size < 3:
        return None, ("fewer than three vocal-active masking frames were available",)
    vocal_db = 10.0 * np.log10(np.maximum(vocal_frame, 1e-20))
    threshold = max(float(np.max(vocal_db)) - 45.0, float(np.percentile(vocal_db, 30)))
    active = vocal_db > threshold
    if int(np.sum(active)) < 3:
        return None, ("fewer than three vocal-active masking frames were detected",)
    epsilon = 1e-20
    bands: dict[str, Any] = {}
    for name, lo, hi in audio.BANDS:
        band = (frequencies >= lo) & (frequencies < hi)
        vp = np.sum(vocal_power[band], axis=0)
        ip = np.sum(instrumental_power[band], axis=0)
        margins = 10.0 * np.log10((vp[active] + epsilon) / (ip[active] + epsilon))
        vs = np.mean(vocal_power[band][:, active], axis=1)
        ins = np.mean(instrumental_power[band][:, active], axis=1)
        vs /= float(np.sum(vs) + epsilon)
        ins /= float(np.sum(ins) + epsilon)
        overlap = float(np.sum(np.minimum(vs, ins)))
        within = float(np.mean(margins <= 6.0))
        bands[name] = {
            "range_hz": (lo, hi),
            "vocal_minus_instrument_median_db": round(float(np.median(margins)), 4),
            "instrument_within_6db_share": round(within, 5),
            "spectral_overlap": round(overlap, 5),
            "possible_masking_score": round(max(0.0, min(1.0, overlap * within)), 5),
        }
    vocal_shape = np.mean(vocal_power[voice_band][:, active], axis=1)
    instrument_shape = np.mean(instrumental_power[voice_band][:, active], axis=1)
    vocal_shape /= float(np.sum(vocal_shape) + epsilon)
    instrument_shape /= float(np.sum(instrument_shape) + epsilon)
    overall_overlap = float(np.sum(np.minimum(vocal_shape, instrument_shape)))
    total_vocal = np.sum(vocal_power[voice_band], axis=0)
    total_instrument = np.sum(instrumental_power[voice_band], axis=0)
    margins = 10.0 * np.log10((total_vocal[active] + epsilon) / (total_instrument[active] + epsilon))
    conflict = float(np.mean(margins <= 6.0))
    payload = {
        "context_ready": True,
        "attribution_confidence": "high",
        "readiness_reasons": (),
        "vocal_role_id": _value(vocal, "role_id") or "vocal",
        "instrument_role_id": _value(instrumental, "role_id") or "instrumental",
        "possible_masking_index": round(max(0.0, min(1.0, overall_overlap * conflict)), 5),
        "spectral_overlap": round(overall_overlap, 5),
        "instrument_within_6db_share": round(conflict, 5),
        "active_frame_share": round(float(np.mean(active)), 5),
        "bands": bands,
        "limitations": (
            "Synchronized stem overlap and margins are correlates of possible masking, not proof of audibility.",
            "Attribution is limited to the supplied vocal/instrumental stem mapping.",
        ),
    }
    return payload, ()


def _masking_from_stems(
    stem_values: Mapping[str, Any],
    stems: Sequence[Any],
    *,
    cache: DecodedAudioCache,
    max_seconds: float | None,
    analysis_policy_digest: str = "",
    section_map_digest: str = "",
) -> tuple[dict[str, Any] | None, list[str]]:
    del stem_values  # Measurements are keyed by the supplied stem assets.
    vocal = next((item for item in stems if _value(item, "asset_kind") == "vocal_stem"), None)
    instrumental = next((item for item in stems if _value(item, "asset_kind") == "instrumental_stem"), None)
    if vocal is None or instrumental is None:
        return None, ["masking attribution requires supplied vocal_stem and instrumental_stem assets"]
    masking_policy_digest = _canonical_digest(
        {
            "analysis_policy_digest": analysis_policy_digest,
            "vocal_role_id": _value(vocal, "role_id"),
            "instrumental_role_id": _value(instrumental, "role_id"),
            "vocal_offset_seconds": _value(vocal, "declared_offset_seconds", 0.0),
            "instrumental_offset_seconds": _value(instrumental, "declared_offset_seconds", 0.0),
        }
    )
    try:
        result = cache.get_or_compute(
            (vocal, instrumental),
            analyzer_version=f"{EVALUATION_ANALYZER_VERSION}:masking",
            section_map_digest=section_map_digest,
            analysis_policy_digest=masking_policy_digest,
            compute=lambda loaded: _masking_measurement(
                loaded[0],
                loaded[1],
                vocal=vocal,
                instrumental=instrumental,
            ),
            max_seconds=max_seconds,
        )
    except ReviewAssetError as exc:
        return None, [f"masking decode unavailable: {exc}"]
    masking, limitations = result
    return masking, list(limitations)


def _finite_note_number(value: Any, *, minimum: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        return None
    return number


def _normalised_note_rows(sequence: Any) -> tuple[dict[str, float], ...]:
    """Return finite note facts from one persisted sequence, with a hard cap."""

    notes = _value(sequence, "notes", ()) or ()
    if isinstance(notes, (str, bytes, Mapping)) or not isinstance(notes, Sequence):
        return ()
    result: list[dict[str, float]] = []
    for note in notes[:MAX_NOTE_EVENTS]:
        note_map = _mapping(note)
        pitch = _finite_note_number(_value(note_map, "pitch"), minimum=0.0)
        start = _finite_note_number(_value(note_map, "start_beats"), minimum=0.0)
        duration = _finite_note_number(_value(note_map, "duration_beats"), minimum=0.0)
        if pitch is None or start is None or duration is None or duration <= 0.0:
            continue
        velocity = _finite_note_number(_value(note_map, "velocity"), minimum=0.0)
        result.append(
            {
                "pitch": min(131.0, pitch),
                "start_beats": start,
                "duration_beats": duration,
                "velocity": velocity if velocity is not None else 0.0,
            }
        )
    result.sort(key=lambda item: (item["start_beats"], item["pitch"], item["duration_beats"]))
    return tuple(result)


def _note_sequence_summary(
    sequence: Any,
    note_rows: Sequence[Mapping[str, float]] | None = None,
) -> dict[str, Any]:
    note_rows = tuple(note_rows) if note_rows is not None else _normalised_note_rows(sequence)
    pitches = [row["pitch"] for row in note_rows]
    starts = [row["start_beats"] for row in note_rows]
    durations = [row["duration_beats"] for row in note_rows]
    velocities = [row["velocity"] for row in note_rows]
    sequence_duration = _finite_note_number(_value(sequence, "duration_beats", 0.0), minimum=0.0) or 0.0
    if not sequence_duration:
        sequence_duration = max(
            (start + duration for start, duration in zip(starts, durations)),
            default=0.0,
        )
    active_intervals = sorted(
        (row["start_beats"], row["start_beats"] + row["duration_beats"])
        for row in note_rows
    )
    occupied = 0.0
    current_end = 0.0
    for start, end in active_intervals:
        if end > current_end:
            occupied += end - max(start, current_end)
            current_end = end
    max_polyphony = 0
    for start, _end in active_intervals:
        max_polyphony = max(
            max_polyphony,
            sum(other_start <= start < other_end for other_start, other_end in active_intervals),
        )
    pitch_intervals = tuple(
        round(pitches[index] - pitches[index - 1], 4)
        for index in range(1, min(len(pitches), 65))
    )
    rhythmic_signature = tuple(
        (
            round(starts[index] - starts[index - 1], 4) if index else round(starts[index], 4),
            round(durations[index], 4),
        )
        for index in range(min(len(starts), 64))
    )
    return {
        "name": _value(sequence, "name"),
        "generator": _value(sequence, "generator"),
        "note_digest_sha256": _value(sequence, "note_digest_sha256"),
        "note_count": len(note_rows),
        "duration_beats": round(sequence_duration, 6),
        "note_density": round(len(note_rows) / max(sequence_duration, 1e-9), 6),
        "pitch_low": min(pitches) if pitches else None,
        "pitch_high": max(pitches) if pitches else None,
        "pitch_range": (min(pitches), max(pitches)) if pitches else None,
        "pitch_median": round(float(np.median(pitches)), 4) if pitches else None,
        "register": round(float(np.median(pitches)), 4) if pitches else None,
        "velocity_mean": round(float(np.mean(velocities)), 6) if velocities else None,
        "velocity_p10": round(float(np.percentile(velocities, 10)), 6) if velocities else None,
        "velocity_p90": round(float(np.percentile(velocities, 90)), 6) if velocities else None,
        "velocity_distribution": {
            "mean": round(float(np.mean(velocities)), 6) if velocities else None,
            "p10": round(float(np.percentile(velocities, 10)), 6) if velocities else None,
            "p90": round(float(np.percentile(velocities, 90)), 6) if velocities else None,
        },
        "max_polyphony": int(max_polyphony),
        "polyphony": int(max_polyphony),
        "rhythmic_occupancy": round(occupied / max(sequence_duration, 1e-9), 6),
        "pitch_classes": tuple(sorted({int(pitch) % 12 for pitch in pitches})),
        "pitch_interval_signature": pitch_intervals,
        "rhythmic_signature": rhythmic_signature,
    }


def _motif_overlap_score(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> float | None:
    """Compare bounded normalized interval/rhythm signatures, not audio."""

    left_signature = tuple(left.get("summary", {}).get("pitch_interval_signature", ()))
    right_signature = tuple(right.get("summary", {}).get("pitch_interval_signature", ()))
    left_rhythm = tuple(left.get("summary", {}).get("rhythmic_signature", ()))
    right_rhythm = tuple(right.get("summary", {}).get("rhythmic_signature", ()))
    if not left_signature and not right_signature and not left_rhythm and not right_rhythm:
        digest_a = left.get("summary", {}).get("note_digest_sha256")
        digest_b = right.get("summary", {}).get("note_digest_sha256")
        return 1.0 if digest_a and digest_a == digest_b else None
    pitch_a, pitch_b = set(left_signature), set(right_signature)
    rhythm_a, rhythm_b = set(left_rhythm), set(right_rhythm)
    components: list[float] = []
    if pitch_a or pitch_b:
        components.append(len(pitch_a & pitch_b) / max(len(pitch_a | pitch_b), 1))
    if rhythm_a or rhythm_b:
        components.append(len(rhythm_a & rhythm_b) / max(len(rhythm_a | rhythm_b), 1))
    if not components:
        return None
    return round(float(np.mean(components)), 6)


def _role_family(record: Mapping[str, Any]) -> str | None:
    role_text = " ".join(
        str(value or "")
        for value in (record.get("role_id"), record.get("name"), record.get("key"))
    ).casefold()
    if any(token in role_text for token in ("chord", "harmony", "pad", "voicing")):
        return "chord"
    if "sub" in role_text:
        return "sub"
    if "bass" in role_text:
        return "bass"
    if any(token in role_text for token in ("lead", "melody", "melodic", "vocal")):
        return "lead"
    return None


def _active_notes(
    notes: Sequence[Mapping[str, float]],
    start: float,
) -> tuple[Mapping[str, float], ...]:
    return tuple(
        note
        for note in notes
        if note["start_beats"] <= start < note["start_beats"] + note["duration_beats"]
    )


def _agreement_metric(
    source_notes: Sequence[Mapping[str, float]],
    target_notes: Sequence[Mapping[str, float]],
    predicate: Any,
) -> dict[str, Any] | None:
    if not source_notes or not target_notes:
        return None
    matched = 0
    total = 0
    for target in target_notes:
        active_source = _active_notes(source_notes, target["start_beats"])
        if not active_source:
            continue
        total += 1
        if predicate(active_source, target):
            matched += 1
    if not total:
        return None
    return {
        "score": round(matched / total, 6),
        "matched_events": matched,
        "compared_events": total,
    }


def _harmonic_agreement(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    families: dict[str, list[Mapping[str, Any]]] = {"chord": [], "lead": [], "bass": [], "sub": []}
    for record in records:
        family = _role_family(record)
        if family:
            families[family].append(record)
    chord_notes = tuple(note for record in families["chord"] for note in record.get("notes", ()))
    lead_notes = tuple(note for record in families["lead"] for note in record.get("notes", ()))
    bass_notes = tuple(note for record in families["bass"] for note in record.get("notes", ()))
    sub_notes = tuple(note for record in families["sub"] for note in record.get("notes", ()))

    def chord_members(active: Sequence[Mapping[str, float]], _target: Mapping[str, float]) -> bool:
        return int(_target["pitch"]) % 12 in {int(note["pitch"]) % 12 for note in active}

    def root_match(active: Sequence[Mapping[str, float]], target: Mapping[str, float]) -> bool:
        root = min(int(note["pitch"]) for note in active) % 12
        return int(target["pitch"]) % 12 == root

    metrics = {
        "lead_to_chord": _agreement_metric(chord_notes, lead_notes, chord_members),
        "chord_root_to_lead": _agreement_metric(chord_notes, lead_notes, root_match),
        "chord_root_to_bass": _agreement_metric(chord_notes, bass_notes, root_match),
        "chord_root_to_sub": _agreement_metric(chord_notes, sub_notes, root_match),
        "bass_to_sub": _agreement_metric(bass_notes, sub_notes, lambda active, target: int(target["pitch"]) % 12 == int(active[0]["pitch"]) % 12),
    }
    metrics = {key: value for key, value in metrics.items() if value is not None}
    limitations: list[str] = [
        "Harmony metrics use only persisted NoteSequence events and cannot establish timbral, tuning, or perceptual agreement.",
    ]
    if chord_notes and (bass_notes or sub_notes):
        limitations.append("Chord root is approximated as the lowest active chord note; inversions and explicit harmonic labels are not inferred.")
    if not metrics:
        limitations.append("matching persisted chord, lead, bass, and/or sub NoteSequences were not supplied")
    return {
        "available": bool(metrics),
        "metrics": metrics,
        "roles_present": tuple(key for key, values in families.items() if values),
        "basis": "active-note pitch-class agreement in persisted generated NoteSequences",
        "limitations": tuple(limitations),
    }


def _section_development(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        section_id = record.get("section_id")
        role_id = record.get("role_id") or record.get("key")
        if section_id is not None and role_id is not None:
            grouped.setdefault(str(role_id), []).append(record)
    pairs: list[dict[str, Any]] = []
    for role_id, values in list(grouped.items())[:MAX_NOTE_COMPARISONS]:
        for left, right in zip(values, values[1:]):
            left_summary = left["summary"]
            right_summary = right["summary"]
            changed: list[str] = []
            left_density = _finite_note_number(left_summary.get("note_density"))
            right_density = _finite_note_number(right_summary.get("note_density"))
            left_register = _finite_note_number(left_summary.get("register"))
            right_register = _finite_note_number(right_summary.get("register"))
            comparisons = {
                "note_density_delta": round(right_density - left_density, 6)
                if right_density is not None and left_density is not None else None,
                "register_delta": round(right_register - left_register, 6)
                if right_register is not None and left_register is not None else None,
                "motif_overlap_score": _motif_overlap_score(left, right),
            }
            if comparisons["note_density_delta"] not in (None, 0.0):
                changed.append("density")
            if comparisons["register_delta"] not in (None, 0.0):
                changed.append("register")
            if comparisons["motif_overlap_score"] is not None and comparisons["motif_overlap_score"] < 1.0:
                changed.append("motif")
            pairs.append(
                {
                    "role_id": role_id,
                    "from_section_id": left.get("section_id"),
                    "to_section_id": right.get("section_id"),
                    "metrics": comparisons,
                    "transformation": {
                        "changed_dimensions": tuple(changed),
                        "changed": bool(changed),
                    },
                    "basis": "explicit section IDs on persisted generated NoteSequences",
                }
            )
    limitations: list[str] = []
    if not pairs:
        limitations.append("section development requires repeated persisted NoteSequences with explicit section IDs and role IDs")
    return {
        "available": bool(pairs),
        "pairs": tuple(pairs),
        "basis": "bounded density/register/motif transformations between explicitly labelled sections",
        "limitations": tuple(limitations),
    }


def _generated_note_analysis(source_run: Any) -> tuple[dict[str, Any], list[str]]:
    if source_run is None:
        return {}, ["source Production Run was not supplied; generated-note analysis is unavailable"]
    # ProductionRun snapshots expose generated outputs under
    # ``generated_outputs``; the immutable ReviewSourceSnapshot stores the
    # same bounded records as ``generated_note_sequences`` with their
    # NoteSequence payload in ``metadata``.  Accept both shapes without
    # attempting to inspect arbitrary Piano Roll state.
    outputs = _value(source_run, "generated_outputs")
    if outputs is None:
        outputs = _value(source_run, "generated_note_sequences", ())
    if isinstance(outputs, (str, bytes, Mapping)) or not isinstance(outputs, Sequence):
        outputs = ()
    outputs = outputs or ()
    rows: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    ignored = 0
    for output in tuple(outputs)[:MAX_NOTE_SEQUENCES]:
        output_map = _mapping(output)
        output_kind = _value(output_map, "output", _value(output_map, "output_kind"))
        if output_kind != "note_sequence":
            continue
        sequence = _value(output_map, "value")
        if sequence is None:
            sequence = _value(output_map, "metadata")
        note_rows = _normalised_note_rows(sequence) if sequence is not None else ()
        if sequence is None or _value(sequence, "notes") is None:
            ignored += 1
            continue
        role_id = _value(output_map, "role_id")
        section_id = _value(output_map, "section_id", _value(sequence, "section_id"))
        key = str(
            role_id
            or _value(output_map, "output_id")
            or _value(output_map, "operation_id")
            or f"sequence-{len(records) + 1}"
        )
        if key in rows:
            suffix = 2
            while f"{key}-{suffix}" in rows:
                suffix += 1
            key = f"{key}-{suffix}"
        summary = _note_sequence_summary(sequence, note_rows)
        record = {
            "key": key,
            "operation_id": _value(output_map, "operation_id", _value(output_map, "output_id")),
            "role_id": role_id,
            "section_id": section_id,
            "name": summary.get("name"),
            "notes": note_rows,
            "summary": summary,
        }
        records.append(record)
        rows[key] = {
            "operation_id": record["operation_id"],
            "role_id": role_id,
            "section_id": section_id,
            "summary": summary,
        }
    limitations: list[str] = []
    if not rows:
        limitations.append("the source run has no persisted PostFader-generated NoteSequence outputs; arbitrary Piano Roll notes are not inferred")
    if ignored:
        limitations.append(f"{ignored} generated output(s) did not contain a usable persisted NoteSequence and were omitted")
    if len(outputs) > MAX_NOTE_SEQUENCES:
        limitations.append(f"generated-note analysis capped at {MAX_NOTE_SEQUENCES} sequences")

    motif_pairs: list[dict[str, Any]] = []
    for index, left in enumerate(records[:MAX_NOTE_COMPARISONS]):
        for right in records[index + 1 : MAX_NOTE_COMPARISONS]:
            same_role = left.get("role_id") and left.get("role_id") == right.get("role_id")
            drop_pair = "drop" in " ".join(str(value or "") for value in (left.get("name"), right.get("name"), left.get("key"), right.get("key"))).casefold()
            if not same_role and not drop_pair:
                continue
            score = _motif_overlap_score(left, right)
            if score is None:
                continue
            motif_pairs.append(
                {
                    "sequence_a": left["key"],
                    "sequence_b": right["key"],
                    "role_id": left.get("role_id") if same_role else None,
                    "section_a": left.get("section_id"),
                    "section_b": right.get("section_id"),
                    "overlap_score": score,
                    "basis": "normalized pitch-interval and rhythmic signatures from persisted NoteSequence events",
                }
            )
            if len(motif_pairs) >= MAX_NOTE_COMPARISONS:
                break
        if len(motif_pairs) >= MAX_NOTE_COMPARISONS:
            break
    if not motif_pairs:
        limitations.append("motif overlap requires at least two explicitly comparable persisted sequences sharing a role or drop label")
    motif_analysis = {
        "available": bool(motif_pairs),
        "pairs": tuple(motif_pairs),
        "basis": "bounded normalized note-event signatures; not audio similarity",
        "limitations": (
            () if motif_pairs else ("no comparable persisted motif pair was available",)
        ),
    }

    harmonic_analysis = _harmonic_agreement(records)
    limitations.extend(harmonic_analysis.get("limitations", ()))
    section_analysis = _section_development(records)
    limitations.extend(section_analysis.get("limitations", ()))

    # Drop A/B similarity is retained for compatibility and now includes the
    # explicit motif score when one is available.
    drops = [
        record for record in records
        if "drop" in str(record.get("name", "")).casefold()
        or "drop" in str(record.get("key", "")).casefold()
    ]
    if len(drops) >= 2:
        first = drops[0]["summary"]
        second = drops[1]["summary"]
        similarities = []
        for key in ("note_density", "register", "rhythmic_occupancy", "max_polyphony"):
            left, right = first.get(key), second.get(key)
            if left is not None and right is not None:
                similarities.append(1.0 - min(1.0, abs(left - right) / max(abs(left), abs(right), 1e-9)))
        motif_score = _motif_overlap_score(drops[0], drops[1])
        if motif_score is not None:
            similarities.append(motif_score)
        if similarities:
            rows["drop_a_drop_b_comparison"] = {
                "sequence_a": drops[0]["key"],
                "sequence_b": drops[1]["key"],
                "similarity_score": round(float(np.mean(similarities)), 5),
                "motif_overlap_score": motif_score,
                "basis": "persisted generated NoteSequence summaries and normalized motif signatures",
            }
    if motif_pairs or harmonic_analysis.get("available") or section_analysis.get("available"):
        limitations.append("generated-note metrics describe persisted event structure; they do not substitute for listening judgment")
    return {
        **rows,
        "motif_overlap": motif_analysis,
        "harmonic_agreement": harmonic_analysis,
        "section_development": section_analysis,
    }, list(dict.fromkeys(limitations))


def _bounded_text(value: Any, limit: int = MAX_GOAL_TEXT) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _iter_goal_values(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        return (value.decode(errors="replace") if isinstance(value, bytes) else value,)
    if isinstance(value, Mapping):
        for key in ("goals", "objectives", "requested_goals", "focus"):
            nested = value.get(key)
            if nested is not None:
                return _iter_goal_values(nested)
        return (value,)
    if isinstance(value, Sequence):
        return tuple(value[:MAX_GOAL_EVALUATIONS])
    return (value,)


def _goal_text(value: Any) -> str:
    if isinstance(value, (str, bytes)):
        return _bounded_text(value.decode(errors="replace") if isinstance(value, bytes) else value)
    item = _mapping(value)
    for key in (
        "metric",
        "goal",
        "objective",
        "text",
        "description",
        "requested_target",
        "target",
        "rationale",
    ):
        candidate = item.get(key)
        if candidate is not None and str(candidate).strip():
            return _bounded_text(candidate)
    return _bounded_text(value)


def _source_brief(source_run: Any) -> str:
    if source_run is None:
        return ""
    brief = _value(source_run, "original_brief")
    if brief is None:
        brief = _value(source_run, "brief")
    if brief is None:
        request = _value(source_run, "request")
        brief = _value(request, "brief")
    return _bounded_text(brief, 4096)


def _goal_entries(
    source_run: Any,
    requested_focus: Sequence[Any] | Any,
    reference_goals: Sequence[Any] | Any,
) -> tuple[dict[str, Any], ...]:
    entries: list[dict[str, Any]] = []
    brief = _source_brief(source_run)
    if brief:
        # Preserve the complete original brief as one auditable goal, while
        # splitting explicit line/semicolon/sentence clauses when possible so
        # mixed technical and subjective requests do not receive one blanket
        # classification.
        clauses = tuple(
            _bounded_text(part)
            for part in re.split(r"(?:[\r\n;]+|(?<=[.!?])\s+)", brief)
            if part.strip()
        )[:8]
        if clauses:
            for index, clause in enumerate(clauses):
                entries.append({"goal": clause, "origin": "original_brief", "index": index})
        else:
            entries.append({"goal": brief, "origin": "original_brief", "index": 0})
    request = _value(source_run, "request")
    for owner in (source_run, request):
        for key in (
            "goals",
            "requested_goals",
            "objectives",
            "expected_objectives",
            "expected_measurable_movements",
        ):
            values = _value(owner, key)
            if values is None:
                continue
            for index, value in enumerate(_iter_goal_values(values)):
                text = _goal_text(value)
                if text:
                    entries.append({
                        "goal": text,
                        "origin": "original_goal",
                        "index": index,
                        "raw": value,
                    })
    for origin, values in (
        ("requested_focus", requested_focus),
        ("reference_goal", reference_goals),
    ):
        for index, value in enumerate(_iter_goal_values(values)):
            text = _goal_text(value)
            if text:
                entries.append({
                    "goal": text,
                    "origin": origin,
                    "index": index,
                    "raw": value,
                })
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries[:MAX_GOAL_EVALUATIONS]:
        identity = (str(entry["origin"]), str(entry["goal"]).casefold())
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(entry)
    return tuple(unique)


def _reference_comparison_map(reference_comparisons: Any) -> tuple[Mapping[str, Any], ...]:
    if reference_comparisons is None:
        return ()
    values = (
        tuple(reference_comparisons)
        if isinstance(reference_comparisons, Sequence)
        and not isinstance(reference_comparisons, (str, bytes, Mapping))
        else (reference_comparisons,)
    )
    return tuple(_mapping(value) for value in values[:MAX_NOTE_COMPARISONS])


def _canonical_goal_dimension(text: str) -> str | None:
    lowered = text.casefold()
    aliases = {
        "tonal": "tonal_balance",
        "tonality": "tonal_balance",
        "brightness": "tonal_balance",
        "spectrum": "tonal_balance",
        "tonal_balance": "tonal_balance",
        "dynamics": "dynamics",
        "dynamic": "dynamics",
        "stereo": "stereo_behavior",
        "stereo_behavior": "stereo_behavior",
        "width": "stereo_behavior",
        "low_end": "low_end_balance",
        "low-end": "low_end_balance",
        "low_end_balance": "low_end_balance",
        "loudness": "loudness",
        "level": "loudness",
        "transient": "transient_density",
        "transient_density": "transient_density",
        "section_contrast": "section_contrast",
        "energy_curve": "energy_curve",
    }
    for key, value in aliases.items():
        if key in lowered:
            return value
    return None


def _global_dimension_available(
    global_measurements: Mapping[str, Any],
    dimension: str,
) -> bool:
    feature_key = {
        "tonal_balance": "spectrum",
        "dynamics": "dynamics",
        "stereo_behavior": "stereo",
        "low_end_balance": "spectrum",
        "loudness": "loudness",
        "transient_density": "dynamics",
    }.get(dimension)
    if feature_key is None:
        return False
    feature = _value(global_measurements, feature_key)
    return isinstance(feature, Mapping) and bool(feature)


def _goal_classification(
    entry: Mapping[str, Any],
    *,
    sections: Sequence[Any],
    global_measurements: Mapping[str, Any],
    generated_analysis: Mapping[str, Any],
    processing_state: Mapping[str, Any],
    reference_comparisons: Any,
) -> dict[str, Any]:
    text = _bounded_text(entry.get("goal"))
    lowered = text.casefold()
    raw = entry.get("raw")
    raw_map = _mapping(raw) if raw is not None else {}
    explicit_state = str(raw_map.get("state", "")) if raw_map else ""
    if explicit_state in GOAL_EVALUATION_STATES:
        return {
            "state": explicit_state,
            "domain": "requested_goal",
            "rationale": "The supplied goal record explicitly declared its evaluation state.",
            "evidence": ("supplied goal metadata",),
            "required_additional_evidence": (),
        }

    comparison_maps = _reference_comparison_map(reference_comparisons)
    reference_ready = any(
        bool(_value(_value(value, "measurements", {}), "comparison_ready", False))
        for value in comparison_maps
    )
    requested_dimensions = {
        str(dimension).casefold()
        for value in comparison_maps
        for dimension in (_value(value, "requested_dimensions", ()) or ())
    }
    generated_motif = bool(_value(generated_analysis.get("motif_overlap"), "available", False))
    generated_harmonic = bool(_value(generated_analysis.get("harmonic_agreement"), "available", False))
    has_global = bool(global_measurements)
    has_generated_sequence = any(
        isinstance(value, Mapping) and "summary" in value
        for key, value in generated_analysis.items()
        if key not in {"motif_overlap", "harmonic_agreement", "section_development"}
    )
    has_processing = str(processing_state.get("state", "not_evaluated")) != "not_evaluated"

    if entry.get("origin") == "reference_goal":
        dimension = _canonical_goal_dimension(lowered)
        canonical_requested = dimension in requested_dimensions if dimension else False
        if not reference_ready or not canonical_requested:
            return {
                "state": "not_evaluable_from_supplied_assets",
                "domain": "reference_comparison",
                "rationale": "A reference goal was supplied without a matching ready comparison dimension.",
                "evidence": (),
                "required_additional_evidence": ("a validated reference asset and explicitly requested comparison dimension",),
            }
        if dimension in {"section_contrast", "energy_curve"}:
            return {
                "state": "proxy_evaluable",
                "domain": "arrangement_development",
                "rationale": "The requested reference dimension is represented by bounded section/energy proxies.",
                "evidence": ("reference comparison",),
                "required_additional_evidence": ("producer listening judgment for artistic impact",),
            }
        return {
            "state": "technically_evaluable",
            "domain": "technical_audio",
            "rationale": "The requested reference dimension has a ready candidate-minus-reference measurement.",
            "evidence": ("reference comparison",),
            "required_additional_evidence": (),
        }

    if any(token in lowered for token in ("emotional", "emotion", "vibe", "feel", "feels", "artistic", "taste", "moving", "exciting", "character")):
        return {
            "state": "requires_user_judgment",
            "domain": "audible_quality",
            "rationale": "The goal describes listener experience or artistic preference rather than a measurable fact.",
            "evidence": (),
            "required_additional_evidence": ("explicit producer listening feedback",),
        }
    if any(token in lowered for token in ("motif", "melody", "melodic", "chord root", "harmonic agreement", "harmonic", "lead", "bass", "sub")):
        if "motif" in lowered and generated_motif:
            return {
                "state": "technically_evaluable",
                "domain": "composition",
                "rationale": "Motif overlap can be measured from persisted generated note-event signatures.",
                "evidence": ("persisted generated NoteSequence",),
                "required_additional_evidence": (),
            }
        if any(token in lowered for token in ("harmonic", "chord root", "bass", "sub")) and generated_harmonic:
            return {
                "state": "technically_evaluable",
                "domain": "composition",
                "rationale": "Pitch-class agreement is available for the explicitly supplied generated role sequences.",
                "evidence": ("persisted generated NoteSequence",),
                "required_additional_evidence": ("listening judgment for tuning, timbre, and musical function",),
            }
        return {
            "state": "not_evaluable_from_supplied_assets",
            "domain": "composition",
            "rationale": "The supplied assets do not contain the matching persisted role sequences needed for this note-level check.",
            "evidence": (),
            "required_additional_evidence": ("persisted generated NoteSequences with explicit role labels",),
        }
    if any(token in lowered for token in ("processing", "effect", "reverb", "delay", "compression", "limiter", "eq", "plugin")):
        if has_processing:
            state = "technically_evaluable" if processing_state.get("state") in {"verified", "measured"} else "proxy_evaluable"
            return {
                "state": state,
                "domain": "processing",
                "rationale": "Source processing plans/receipts describe requested controls, but do not establish their audible result.",
                "evidence": ("production-run processing receipt",),
                "required_additional_evidence": ("post-processing bounce and producer listening judgment",),
            }
        return {
            "state": "not_evaluable_from_supplied_assets",
            "domain": "processing",
            "rationale": "No source processing plan or receipt was supplied for this processing goal.",
            "evidence": (),
            "required_additional_evidence": ("source-run processing plan or receipt",),
        }
    if any(token in lowered for token in ("drop", "build", "breakdown", "arrangement", "section", "transition", "development", "energy", "contrast", "impact")):
        if sections:
            return {
                "state": "proxy_evaluable",
                "domain": "arrangement_development",
                "rationale": "Supplied section boundaries support bounded energy, contrast, and development proxies.",
                "evidence": ("decoded audio section measurements",),
                "required_additional_evidence": ("producer listening judgment for musical impact",),
            }
        return {
            "state": "not_evaluable_from_supplied_assets",
            "domain": "arrangement_development",
            "rationale": "No section boundaries were supplied for the requested arrangement check.",
            "evidence": (),
            "required_additional_evidence": ("explicit section boundaries or labeled section bounces",),
        }
    dimension = _canonical_goal_dimension(lowered)
    if dimension is not None and _global_dimension_available(global_measurements, dimension):
        return {
            "state": "technically_evaluable",
            "domain": "technical_audio",
            "rationale": "The supplied bounce exposes the requested bounded audio measurement.",
            "evidence": ("decoded audio measurement",),
            "required_additional_evidence": (),
        }
    if any(token in lowered for token in ("sample", "peak", "clip", "headroom", "mono", "phase", "frequency", "spectrum", "tempo", "bpm", "duration")) and has_global:
        return {
            "state": "technically_evaluable",
            "domain": "technical_audio",
            "rationale": "The supplied candidate and bounded audio/sequence measurements cover this technical request.",
            "evidence": ("decoded audio measurement",),
            "required_additional_evidence": (),
        }
    if any(token in lowered for token in ("note count", "density", "register", "velocity", "pitch", "rhythm")) and has_generated_sequence:
        return {
            "state": "technically_evaluable",
            "domain": "composition",
            "rationale": "Persisted generated NoteSequence summaries expose the requested bounded note metric.",
            "evidence": ("persisted generated NoteSequence",),
            "required_additional_evidence": (),
        }
    if any(token in lowered for token in ("sound", "timbre", "palette", "balance", "masking", "overlap", "groove")):
        return {
            "state": "proxy_evaluable" if has_global else "not_evaluable_from_supplied_assets",
            "domain": "sound_selection" if "sound" in lowered or "timbre" in lowered or "palette" in lowered else "technical_audio",
            "rationale": "Only broad audio or supplied metadata proxies are available for this request.",
            "evidence": ("decoded audio measurement",) if has_global else (),
            "required_additional_evidence": ("role stems or explicit sound-palette metadata",),
        }
    return {
        "state": "not_evaluable_from_supplied_assets",
        "domain": "requested_goal",
        "rationale": "The supplied review assets do not expose a bounded metric for this goal.",
        "evidence": (),
        "required_additional_evidence": ("a concrete measurable target or explicit producer feedback",),
    }


def _goal_evaluations(
    source_run: Any,
    *,
    requested_focus: Sequence[Any] | Any,
    reference_goals: Sequence[Any] | Any,
    sections: Sequence[Any],
    global_measurements: Mapping[str, Any],
    generated_analysis: Mapping[str, Any],
    processing_state: Mapping[str, Any],
    reference_comparisons: Any,
) -> tuple[tuple[dict[str, Any], ...], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    counts = {state: 0 for state in GOAL_EVALUATION_STATES}
    for entry in _goal_entries(source_run, requested_focus, reference_goals):
        classification = _goal_classification(
            entry,
            sections=sections,
            global_measurements=global_measurements,
            generated_analysis=generated_analysis,
            processing_state=processing_state,
            reference_comparisons=reference_comparisons,
        )
        state = classification["state"]
        counts[state] += 1
        raw_map = _mapping(entry.get("raw")) if entry.get("raw") is not None else {}
        row = {
            "goal": _bounded_text(entry.get("goal")),
            "origin": _bounded_text(entry.get("origin"), 64),
            "state": state,
            "classification": state,
            "domain": _bounded_text(classification.get("domain"), 64),
            "evidence": tuple(_bounded_text(value) for value in classification.get("evidence", ())),
            "rationale": _bounded_text(classification.get("rationale")),
            "required_additional_evidence": tuple(
                _bounded_text(value) for value in classification.get("required_additional_evidence", ())
            ),
        }
        for key in ("direction", "target_value", "tolerance", "unit"):
            if key in raw_map and raw_map[key] is not None:
                row[key] = raw_map[key]
        rows.append(row)
    return tuple(rows), counts


def _processing_payloads(source_run: Any) -> tuple[Mapping[str, Any], ...]:
    if source_run is None:
        return ()
    candidates: list[Any] = []
    for key in ("processing_receipts", "source_processing_receipts", "receipts"):
        values = _value(source_run, key)
        if isinstance(values, Mapping):
            candidates.append(values)
        elif isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
            candidates.extend(tuple(values)[:MAX_NOTE_COMPARISONS])
    outputs = _value(source_run, "generated_outputs")
    if isinstance(outputs, Sequence) and not isinstance(outputs, (str, bytes)):
        candidates.extend(
            output for output in tuple(outputs)[:MAX_NOTE_SEQUENCES]
            if _value(output, "output", _value(output, "output_kind")) == "processing_plan"
        )
    outcome = _value(source_run, "creation_outcome")
    processing = _value(outcome, "processing", _value(outcome, "processing_outcome"))
    if processing is not None:
        candidates.append(processing)
    rows: list[Mapping[str, Any]] = []
    for candidate in candidates[:MAX_NOTE_SEQUENCES]:
        item = _mapping(candidate)
        kind = str(_value(item, "output_kind", _value(item, "output", "")))
        metadata = _value(item, "metadata")
        if isinstance(metadata, Mapping):
            item = metadata
        for key in ("result", "value", "processing", "processing_outcome"):
            nested = _value(item, key)
            if nested is not None and isinstance(_mapping(nested), Mapping):
                item = _mapping(nested)
                break
        if item or kind:
            rows.append(item)
    return tuple(rows)


def _processing_review_state(source_run: Any) -> dict[str, Any]:
    payloads = _processing_payloads(source_run)
    plans = 0
    receipts = 0
    requested = 0
    attempted = 0
    verified = 0
    completed = 0
    known = 0
    missing: list[str] = []
    plan_ids: list[str] = []
    statuses: list[str] = []
    for payload in payloads[:MAX_NOTE_SEQUENCES]:
        plan_id = _value(payload, "plan_id")
        if plan_id is not None and str(plan_id) not in plan_ids:
            plan_ids.append(_bounded_text(plan_id, 128))
        actions = _value(payload, "actions", ()) or ()
        is_plan = bool(actions) or _value(payload, "candidates") is not None or _value(payload, "missing_capabilities") is not None
        if is_plan:
            plans += 1
        if _value(payload, "completed") is not None or _value(payload, "attempted_count") is not None or _value(payload, "results") is not None:
            receipts += 1
        requested_count = _finite_note_number(_value(payload, "requested_count"), minimum=0.0)
        if requested_count is None:
            techniques = _value(payload, "requested_techniques", ()) or ()
            requested_count = float(len(techniques)) if isinstance(techniques, Sequence) and not isinstance(techniques, (str, bytes, Mapping)) else 0.0
        applied_count = _finite_note_number(_value(payload, "applied_actions"), minimum=0.0)
        attempted_count = _finite_note_number(_value(payload, "attempted_count"), minimum=0.0)
        if attempted_count is None:
            attempted_count = applied_count or 0.0
        requested += int(requested_count)
        attempted += int(attempted_count)
        direct_verified = _finite_note_number(_value(payload, "verified_actions"), minimum=0.0)
        if direct_verified is not None:
            verified += int(direct_verified)
        result_rows = _value(payload, "results", ()) or ()
        for result in tuple(result_rows)[:MAX_NOTE_EVENTS] if isinstance(result_rows, Sequence) and not isinstance(result_rows, (str, bytes, Mapping)) else ():
            if bool(_value(result, "verified", False)) or str(_value(result, "status", "")) == "verified":
                verified += 1
            if _value(result, "outcome_known") is True:
                known += 1
        if bool(_value(payload, "verified", False)):
            verified = max(verified, requested or attempted or 1)
        if bool(_value(payload, "completed", False)):
            completed += 1
        if _value(payload, "outcome_known") is True:
            known += 1
        status = _value(payload, "status")
        if status:
            statuses.append(_bounded_text(status, 64))
        for key in ("missing_effects", "unresolved_controls", "missing_capabilities", "warnings"):
            values = _value(payload, key, ()) or ()
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes, Mapping)):
                missing.extend(_bounded_text(value) for value in tuple(values)[:8])
    if not payloads or (
        not plans
        and not receipts
        and not missing
        and requested == 0
        and attempted == 0
        and verified == 0
        and all(status == "not_requested" for status in statuses)
    ):
        return {
            "state": "not_evaluated",
            "summary": "No source processing plan or receipt was supplied.",
            "confidence": 0.0,
            "limitations": ("Processing review requires source-run processing metadata or receipts.",),
        }
    outcome_status = next((status for status in statuses if status != "not_requested"), None)
    outcome_verified = outcome_status == "processed" and attempted > 0 and verified >= attempted
    if (completed and requested > 0 and verified >= requested) or outcome_verified:
        state = "verified"
        confidence = 0.95
        summary = f"Verified source processing receipt(s) cover {verified} requested action(s)."
    elif receipts or outcome_status in {"processed", "partially_processed", "restrained_first_pass"}:
        state = "measured"
        confidence = 0.7
        summary = "Source processing execution evidence is present but not fully verified."
    else:
        state = "proxy"
        confidence = 0.45
        summary = "A source processing plan is present; execution and audible effect are not established."
    limitations = [
        "Processing plans/receipts document controls and readbacks; they do not establish the audible result.",
    ]
    if missing:
        limitations.append("Source processing evidence reports unresolved or unavailable controls: " + ", ".join(dict.fromkeys(missing))[:MAX_GOAL_TEXT])
    return {
        "state": state,
        "summary": summary,
        "confidence": confidence,
        "limitations": tuple(dict.fromkeys(limitations)),
        "plan_count": plans,
        "receipt_count": receipts,
        "requested_actions": requested,
        "attempted_actions": attempted,
        "verified_actions": verified,
        "plan_ids": tuple(plan_ids[:MAX_NOTE_COMPARISONS]),
    }


def _canonical_digest(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=False)
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


_EVALUATION_LOCK = threading.RLock()


def evaluate_creation(
    asset_set: Any,
    *,
    section_map: Any | None = None,
    source_run: Any | None = None,
    review_session_id: str | None = None,
    source_run_id: str | None = None,
    user_feedback: Any | None = None,
    requested_focus: Sequence[Any] = (),
    reference_goals: Sequence[Any] = (),
    reference_section_pairs: Sequence[Any] | Mapping[str, Any] = (),
    analysis_policy: Mapping[str, Any] | None = None,
    max_seconds: float | None = 600.0,
    max_findings: int = 32,
    cache: DecodedAudioCache | None = None,
) -> Any:
    """Evaluate a supplied candidate bounce with section/stem/reference context."""

    if isinstance(asset_set, (str, bytes)):
        asset_set = build_review_asset_set((validate_audio_asset(str(asset_set)),))
    assets = _assets_from_set(asset_set)
    if not assets:
        raise CreationEvaluationError("review evaluation requires one validated audio asset")
    candidate = _asset_from_set(
        asset_set,
        ("candidate_full_mix", "candidate", "after_full_mix", "after", "before_full_mix", "before"),
    ) or assets[0]
    if section_map is None:
        section_map = build_review_section_map(source_run)
    cache = cache or get_decode_cache()
    if isinstance(max_findings, bool) or not isinstance(max_findings, int) or max_findings < 1:
        raise CreationEvaluationError("max_findings must be a positive integer")
    max_findings = min(max_findings, 64)
    policy = dict(analysis_policy or {})
    policy_digest = _canonical_digest(policy)
    cache_policy_digest = _canonical_digest(
        {"analysis_policy_digest": policy_digest, "max_seconds": max_seconds}
    )
    section_map_digest = str(
        _value(section_map, "map_digest") or _canonical_digest(section_map)
    )
    timings: dict[str, float] = {}
    started = perf_counter()
    try:
        candidate_loaded = cache.get_or_decode(candidate, max_seconds=max_seconds)
    except ReviewAssetError as exc:
        raise CreationEvaluationError(str(exc)) from exc
    timings["decoding"] = round(perf_counter() - started, 6)
    started = perf_counter()
    global_measurements = cache.get_or_compute_features(
        candidate,
        analyzer_version=EVALUATION_ANALYZER_VERSION,
        section_map_digest=section_map_digest,
        analysis_policy_digest=cache_policy_digest,
        compute=_global_measurement,
        max_seconds=max_seconds,
    )
    # The feature cache is keyed by file/policy and therefore intentionally
    # omits caller-facing labels and asset IDs.  Add those traceability fields
    # on this report copy rather than contaminating a reusable cached value.
    global_measurements = dict(global_measurements)
    global_measurements.setdefault("asset_id", _value(candidate, "asset_id"))
    global_measurements.setdefault("asset_kind", _value(candidate, "asset_kind"))
    timings["global_analysis"] = round(perf_counter() - started, 6)
    started = perf_counter()
    unavailable: list[str] = []
    sections = tuple(_value(section_map, "sections", ()) or ())
    candidate_offset = float(_value(candidate, "declared_offset_seconds", 0.0) or 0.0)
    section_policy_digest = _canonical_digest(
        {
            "analysis_policy_digest": cache_policy_digest,
            "timeline_offset_seconds": candidate_offset,
        }
    )
    section_measurements, section_unavailable = cache.get_or_compute_features(
        candidate,
        analyzer_version=f"{EVALUATION_ANALYZER_VERSION}:sections",
        section_map_digest=section_map_digest,
        analysis_policy_digest=section_policy_digest,
        compute=lambda loaded: _section_measurements(
            loaded,
            sections,
            timeline_offset_seconds=candidate_offset,
        ),
        max_seconds=max_seconds,
    )
    unavailable.extend(section_unavailable)
    timings["section_analysis"] = round(perf_counter() - started, 6)

    stems = tuple(
        _value(asset_set, "synchronized_stems", _value(asset_set, "stems", ())) or ()
    )
    stems_are_aligned = _value(asset_set, "alignment_state") == "aligned"
    started = perf_counter()
    stem_values, stem_unavailable = _stem_measurements(
        stems,
        cache=cache,
        max_seconds=max_seconds,
        section_map=section_map,
        analysis_policy_digest=cache_policy_digest,
    )
    unavailable.extend(stem_unavailable)
    if stems and stems_are_aligned:
        masking_analysis, masking_unavailable = _masking_from_stems(
            stem_values,
            stems,
            cache=cache,
            max_seconds=max_seconds,
            analysis_policy_digest=cache_policy_digest,
            section_map_digest=section_map_digest,
        )
    elif stems:
        masking_analysis, masking_unavailable = (
            None,
            [
                "supplied stems lack verified common-start alignment; masking attribution is unavailable"
            ],
        )
    else:
        masking_analysis, masking_unavailable = (
            None,
            ["no synchronized stems supplied; masking attribution is unavailable"],
        )
    if masking_analysis is None:
        masking_analysis = {
            "context_ready": False,
            "attribution_confidence": "unknown",
            "readiness_reasons": tuple(masking_unavailable),
            "limitations": (
                "Role masking attribution requires matching supplied vocal and instrumental stems.",
                "Masking attribution remains unknown until synchronized vocal and instrumental context is verified.",
            ),
        }
    elif not bool(_value(masking_analysis, "context_ready", False)):
        # A non-ready masking payload may still contain useful readiness
        # diagnostics, but it cannot support high-confidence role attribution.
        masking_analysis = {
            **dict(masking_analysis),
            "attribution_confidence": "unknown",
            "limitations": tuple(
                dict(masking_analysis).get("limitations", ())
            )
            + (
                "Masking attribution remains unknown until synchronized vocal and instrumental context is verified.",
            ),
        }
    unavailable.extend(masking_unavailable)
    timings["stem_analysis"] = round(perf_counter() - started, 6)

    started = perf_counter()
    contrast_analysis = analyze_energy_contrast(sections, section_measurements)
    timings["contrast_analysis"] = round(perf_counter() - started, 6)

    reference_asset = _asset_from_set(asset_set, ("reference",))
    reference_comparisons = None
    if reference_asset is not None:
        started = perf_counter()
        try:
            reference_loaded = cache.get_or_decode(reference_asset, max_seconds=max_seconds)
            reference_comparison = compare_reference_features(
                reference_loaded,
                candidate_loaded,
                reference_asset_id=str(_value(reference_asset, "asset_id", "reference-unknown")),
                candidate_asset_id=str(_value(candidate, "asset_id", "candidate-unknown")),
                requested_dimensions=reference_goals,
                section_pairs=reference_section_pairs,
                cache=cache,
                reference_asset=reference_asset,
                candidate_asset=candidate,
                section_map_digest=section_map_digest,
                analysis_policy_digest=cache_policy_digest,
                max_alignment_seconds=2.0,
                alignment_rate_hz=2000,
                max_seconds=max_seconds,
            )
            reference_comparisons = (reference_comparison,)
        except ReviewAssetError as exc:
            unavailable.append(f"reference analysis unavailable: {exc}")
        timings["reference_analysis"] = round(perf_counter() - started, 6)
    else:
        unavailable.append("no reference asset supplied; reference comparison is unavailable")

    _generated_analysis, generated_limitations = _generated_note_analysis(source_run)
    unavailable.extend(generated_limitations)
    processing_review = _processing_review_state(source_run)
    goal_rows, goal_summary = _goal_evaluations(
        source_run,
        requested_focus=requested_focus,
        reference_goals=reference_goals,
        sections=sections,
        global_measurements=global_measurements,
        generated_analysis=_generated_analysis,
        processing_state=processing_review,
        reference_comparisons=reference_comparisons,
    )
    global_measurements["goal_evaluations"] = goal_rows
    global_measurements["goal_evaluation_summary"] = goal_summary
    global_measurements["processing_review"] = processing_review
    findings = build_evaluation_findings(
        global_measurements=global_measurements,
        section_measurements=section_measurements,
        stem_measurements=stem_values,
        masking_analysis=masking_analysis,
        reference_comparisons=reference_comparisons,
        contrast_analysis=contrast_analysis,
        goal_evaluations=goal_rows,
        user_feedback=user_feedback,
        brief=_source_brief(source_run) or None,
        max_findings=max_findings,
    )
    findings = rank_findings(findings, max_findings=max_findings)
    top_priorities = tuple(
        str(_value(item, "finding_id")) for item in findings[: min(8, len(findings))]
    )
    if not findings:
        unavailable.append("no actionable findings were produced from supplied evidence")
    # Convert the open internal dictionaries into the canonical bounded
    # section/stem contracts.  The detailed measurements remain FrozenMaps in
    # the model, which keeps the report immutable while preserving all facts.
    section_models = tuple(
        _model(
            "ReviewSectionMeasurement",
            {
                "section_id": section_id,
                "start_seconds": float(_value(section, "start_seconds", 0.0)),
                "end_seconds": float(_value(section, "end_seconds", 0.0)),
                "measurements": measurement,
                "confidence": float(measurement.get("confidence", 0.0)),
                "sample_coverage": measurement.get("sample_coverage"),
                "limitations": tuple(measurement.get("limitations", ())),
            },
        )
        for index, section in enumerate(sections[:MAX_SECTION_MEASUREMENTS])
        for section_id in (str(_value(section, "section_id", f"section-{index + 1}")),)
        for measurement in (section_measurements.get(section_id, {}),)
        if measurement
        and float(_value(section, "end_seconds", 0.0))
        > float(_value(section, "start_seconds", 0.0))
    )
    masking_context_ready = bool(_value(masking_analysis, "context_ready", False))
    stem_attribution_confidence = 0.9 if masking_context_ready else 0.35
    stem_attribution_limitation = (
        ()
        if masking_context_ready
        else (
            "Role masking attribution is low-confidence because synchronized vocal and instrumental context is not verified.",
        )
    )
    stem_models = tuple(
        _model(
            "ReviewStemMeasurement",
            {
                "asset_id": str(value.get("asset_id")),
                "role_id": value.get("role_id"),
                "section_id": None,
                "measurements": value.get("global", {}),
                "attribution_confidence": stem_attribution_confidence,
                "limitations": tuple(value.get("global", {}).get("limitations", ()))
                + stem_attribution_limitation,
            },
        )
        for value in stem_values.values()
        if value.get("asset_id")
    )
    source_id = (
        source_run_id
        or _value(source_run, "run_id")
        or _value(source_run, "source_run_id")
        or "source-run-unknown"
    )
    session_id = review_session_id or "review-session-unknown"
    technical_state = _model(
        "EvaluationState",
        {
            "state": "measured",
            "summary": "Technical audio measurements were extracted from the supplied bounce.",
            "confidence": 0.9 if not global_measurements.get("truncated") else 0.6,
            "limitations": tuple(global_measurements.get("limitations", ())),
        },
    )
    arrangement_state = _model(
        "EvaluationState",
        {
            "state": "proxy" if sections else "not_evaluated",
            "summary": "Section energy and contrast proxies were calculated." if sections else "No section boundaries were available.",
            "confidence": float(_value(section_map, "source_confidence", 0.0) or 0.0),
            "limitations": tuple(_value(contrast_analysis, "limitations", ()) or ()),
        },
    )
    processing_state = _model(
        "EvaluationState",
        {
            "state": processing_review["state"],
            "summary": processing_review["summary"],
            "confidence": processing_review["confidence"],
            "limitations": processing_review["limitations"],
        },
    )
    audible_state = _model(
        "EvaluationState",
        {
            "state": "not_evaluated",
            "summary": "Audible quality still requires producer listening judgment.",
            "confidence": 0.0,
            "limitations": ("Measured audio quality is not artistic approval.",),
        },
    )
    goal_evidence_gap = any(
        _value(goal, "state") == "not_evaluable_from_supplied_assets"
        for goal in goal_rows
    )
    report_payload = {
        "evaluation_id": "evaluation-" + _canonical_digest({
            "asset_set_digest": _value(asset_set, "asset_set_digest"),
            "section_map_digest": section_map_digest,
            "reference_section_pairs": reference_section_pairs,
            "policy_digest": policy_digest,
            "analyzer_version": EVALUATION_ANALYZER_VERSION,
        })[:24],
        "review_session_id": session_id,
        "source_run_id": source_id,
        "asset_set_digest": _value(asset_set, "asset_set_digest") or _canonical_digest(assets),
        "section_map_digest": section_map_digest,
        "analyzer_version": EVALUATION_ANALYZER_VERSION,
        "evaluated_at": datetime.now(timezone.utc),
        "analysis_policy_digest": policy_digest,
        "global_measurements": global_measurements,
        "per_section_measurements": section_models,
        "stem_measurements": stem_models,
        "reference_comparisons": reference_comparisons or (),
        "masking_analysis": masking_analysis or {},
        "energy_contrast_analysis": (
            contrast_analysis.model_dump(mode="json")
            if hasattr(contrast_analysis, "model_dump") else contrast_analysis or {}
        ),
        "generated_content_analysis": _generated_analysis,
        "timing": timings,
        "findings": tuple(findings),
        "top_priorities": top_priorities,
        "unavailable_analyses": tuple(unavailable[:MAX_UNAVAILABLE_ANALYSES]),
        "technical_audio_state": technical_state,
        "arrangement_proxy_state": arrangement_state,
        "processing_review_state": processing_state,
        "audible_quality_state": audible_state,
        "warnings": tuple(
            [
                "Audio measurements are evidence for connected-AI interpretation; they do not certify artistic approval.",
            ]
            + (["At least one analysis was unavailable; see unavailable_analyses."] if unavailable else [])
            + (["At least one requested goal lacks supplied evidence; see goal_evaluations."] if goal_evidence_gap else [])
        ),
    }
    # A candidate bounce always gives us at least one technical observation in
    # normal operation.  Keep the report's lifecycle status honest when one
    # or more requested analyses/assets were unavailable; the nested state
    # fields retain the more precise reason for each axis.
    technical_observations = any(
        isinstance(global_measurements.get(key), Mapping) and bool(global_measurements.get(key))
        for key in ("loudness", "spectrum", "dynamics", "stereo")
    )
    report_payload["status"] = (
        "not_evaluable" if not technical_observations
        else "partial" if unavailable or goal_evidence_gap
        else "complete"
    )
    # ``goal_evaluations`` is the preferred top-level field when a newer
    # creation-review model provides it.  Older model versions still expose
    # the exact same bounded rows under global_measurements above.
    try:
        from . import models as _review_models

        if "goal_evaluations" in _review_models.CreationEvaluationReport.model_fields:
            report_payload["goal_evaluations"] = goal_rows
    except (ImportError, AttributeError):
        pass
    return _model("CreationEvaluationReport", report_payload)


# Compatibility names used by integrations and tests.
evaluate_bounce = evaluate_creation
analyze_creation = evaluate_creation
analyze_review_assets = evaluate_creation


__all__ = [
    "EVALUATION_ANALYZER_VERSION",
    "CreationEvaluationError",
    "analyze_creation",
    "analyze_review_assets",
    "evaluate_bounce",
    "evaluate_creation",
]
