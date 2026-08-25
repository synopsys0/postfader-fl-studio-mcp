"""Offline tempo, key, and monophonic melody analysis.

The FL controller API exposes no live audio bus. These estimators therefore
operate only on an explicitly named decoded audio file and disclose ambiguity
instead of converting a heuristic into a project mutation. Melody extraction
returns the same typed note sequence accepted by the Piano Roll and MIDI tools.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Literal

import numpy as np
from pydantic import ConfigDict, Field
from scipy import signal

from .audio import AudioError, Loaded, _f0_autocorr, load
from .contracts import ContractModel, SCHEMA_VERSION
from .creative import CreativeNote, NoteSequence, make_sequence


MUSIC_ANALYZER_VERSION = "postfader-music-analysis-1"
TEMPO_METHOD = "spectral-flux-normalized-autocorrelation-1"
KEY_METHOD = "energy-chroma-profile-correlation-1"
TRANSCRIPTION_METHOD = "frame-autocorrelation-segmentation-1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MusicAnalysisModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class TempoCandidate(MusicAnalysisModel):
    bpm: float = Field(ge=30.0, le=300.0)
    periodicity_score: float = Field(ge=0.0, le=1.0)
    relation: Literal["primary", "half_time", "double_time", "alternate"]


class TempoEstimate(MusicAnalysisModel):
    method: Literal["spectral-flux-normalized-autocorrelation-1"] = TEMPO_METHOD
    available: bool
    bpm: float | None = Field(default=None, ge=30.0, le=300.0)
    confidence: Literal["high", "medium", "low", "unavailable"]
    confidence_score: float = Field(ge=0.0, le=1.0)
    analyzed_onset_frames: int = Field(ge=0)
    detected_onset_count: int = Field(ge=0)
    candidates: list[TempoCandidate] = Field(default_factory=list, max_length=8)
    ambiguity: str | None = Field(default=None, max_length=512)


class KeyCandidate(MusicAnalysisModel):
    tonic: str = Field(min_length=1, max_length=3)
    mode: Literal["major", "minor"]
    key: str = Field(min_length=3, max_length=16)
    correlation: float = Field(ge=-1.0, le=1.0)


class KeyEstimate(MusicAnalysisModel):
    method: Literal["energy-chroma-profile-correlation-1"] = KEY_METHOD
    available: bool
    tonic: str | None = Field(default=None, max_length=3)
    mode: Literal["major", "minor"] | None = None
    key: str | None = Field(default=None, max_length=16)
    confidence: Literal["high", "medium", "low", "unavailable"]
    confidence_score: float = Field(ge=0.0, le=1.0)
    correlation: float | None = Field(default=None, ge=-1.0, le=1.0)
    runner_up_margin: float | None = Field(default=None, ge=0.0, le=2.0)
    chroma: list[float] = Field(default_factory=list, min_length=0, max_length=12)
    candidates: list[KeyCandidate] = Field(default_factory=list, max_length=8)
    ambiguity: str | None = Field(default=None, max_length=512)


class AudioMusicAnalysis(MusicAnalysisModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    analyzed_at: datetime
    analyzer_version: Literal["postfader-music-analysis-1"] = MUSIC_ANALYZER_VERSION
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sample_rate_hz: int = Field(gt=0)
    channels: int = Field(gt=0)
    source_duration_seconds: float = Field(gt=0.0)
    analyzed_duration_seconds: float = Field(gt=0.0)
    truncated: bool
    tempo: TempoEstimate
    key: KeyEstimate
    mutations_applied: Literal[False] = False
    limitations: list[str]


class MelodyTranscription(MusicAnalysisModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    transcribed_at: datetime
    method: Literal["frame-autocorrelation-segmentation-1"] = TRANSCRIPTION_METHOD
    source_path: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    analyzed_duration_seconds: float = Field(gt=0.0)
    fmin_hz: float = Field(gt=0.0)
    fmax_hz: float = Field(gt=0.0)
    frame_hop_seconds: float = Field(gt=0.0)
    voiced_frame_share: float = Field(ge=0.0, le=1.0)
    median_pitch_confidence: float = Field(ge=0.0, le=1.0)
    tempo_bpm_used: float = Field(ge=10.0, le=522.0)
    tempo_source: Literal["caller", "estimated", "fallback_120"]
    sequence: NoteSequence
    mutations_applied: Literal[False] = False
    limitations: list[str]


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _analysis_mono(loaded: Loaded, target_rate: int = 22050) -> tuple[np.ndarray, int]:
    mono = np.asarray(loaded.samples, dtype=np.float64)
    mono = mono - float(np.mean(mono))
    rate = int(loaded.rate)
    if rate > target_rate:
        divisor = math.gcd(rate, target_rate)
        mono = signal.resample_poly(mono, target_rate // divisor, rate // divisor)
        rate = target_rate
    peak = float(np.max(np.abs(mono))) if mono.size else 0.0
    if peak > 0:
        mono = mono / peak
    return mono, rate


def _onset_envelope(mono: np.ndarray, rate: int) -> tuple[np.ndarray, float]:
    nperseg = 2048 if rate >= 16000 else 1024
    hop = nperseg // 4
    if len(mono) < nperseg * 2:
        return np.zeros(0, dtype=np.float64), hop / rate
    _frequencies, _times, spectrum = signal.stft(
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
    if flux.size < 3:
        return flux.astype(np.float64), hop / rate
    kernel = min(31, flux.size if flux.size % 2 else flux.size - 1)
    floor = signal.medfilt(flux, kernel_size=max(3, kernel)) if kernel >= 3 else 0.0
    envelope = np.maximum(flux - floor, 0.0)
    scale = float(np.percentile(envelope, 95)) if np.any(envelope) else 0.0
    if scale > 0:
        envelope /= scale
    return envelope.astype(np.float64), hop / rate


def estimate_tempo(loaded: Loaded) -> TempoEstimate:
    mono, rate = _analysis_mono(loaded)
    envelope, hop_seconds = _onset_envelope(mono, rate)
    if envelope.size < 16 or float(np.max(envelope, initial=0.0)) < 1e-5:
        return TempoEstimate(
            available=False,
            confidence="unavailable",
            confidence_score=0.0,
            analyzed_onset_frames=int(envelope.size),
            detected_onset_count=0,
            ambiguity="Audio has insufficient onset energy for tempo estimation.",
        )
    centered = envelope - float(np.mean(envelope))
    correlation = signal.fftconvolve(centered, centered[::-1], mode="full")
    correlation = correlation[len(centered) - 1 :]
    lag_min = max(1, int(round(60.0 / (240.0 * hop_seconds))))
    lag_max = min(len(correlation) - 1, int(round(60.0 / (40.0 * hop_seconds))))
    if lag_max <= lag_min:
        return TempoEstimate(
            available=False,
            confidence="unavailable",
            confidence_score=0.0,
            analyzed_onset_frames=len(envelope),
            detected_onset_count=0,
            ambiguity="Audio is too short to span the requested tempo range.",
        )
    lags = np.arange(lag_min, lag_max + 1)
    energy = np.cumsum(centered * centered)
    denom = np.sqrt(
        np.maximum(energy[len(centered) - lags - 1], 1e-20)
        * np.maximum(energy[-1] - np.concatenate(([0.0], energy[:-1]))[lags], 1e-20)
    )
    scores = np.divide(
        correlation[lags],
        denom,
        out=np.zeros_like(lags, dtype=np.float64),
        where=denom > 0,
    )
    scores = np.maximum(scores, 0.0)
    peak_indices, _ = signal.find_peaks(scores, distance=max(1, int(0.05 / hop_seconds)))
    if peak_indices.size == 0:
        peak_indices = np.array([int(np.argmax(scores))])
    raw_ranked = sorted(
        peak_indices.tolist(), key=lambda index: float(scores[index]), reverse=True
    )
    strongest_score = float(scores[raw_ranked[0]])
    # Autocorrelation naturally gives multiples of a pulse interval very
    # similar scores. Prefer a conventional metrical band only when its peak
    # retains at least 75% of the strongest evidence; the half/double choices
    # remain in candidates either way.
    conventional = [
        index
        for index in raw_ranked
        if 70.0 <= 60.0 / (int(lags[index]) * hop_seconds) <= 180.0
        and float(scores[index]) >= strongest_score * 0.75
    ]
    primary_choice = conventional[0] if conventional else raw_ranked[0]
    ranked = [primary_choice] + [
        index for index in raw_ranked if index != primary_choice
    ]
    primary_index = ranked[0]
    primary_lag = int(lags[primary_index])
    primary_bpm = 60.0 / (primary_lag * hop_seconds)
    primary_score = max(0.0, min(1.0, float(scores[primary_index])))
    threshold = max(0.15, float(np.percentile(envelope, 75)))
    onset_peaks, _ = signal.find_peaks(
        envelope, height=threshold, distance=max(1, int(0.06 / hop_seconds))
    )
    candidates: list[TempoCandidate] = [
        TempoCandidate(
            bpm=round(primary_bpm, 3),
            periodicity_score=round(primary_score, 4),
            relation="primary",
        )
    ]
    for relation, bpm in (("half_time", primary_bpm / 2.0), ("double_time", primary_bpm * 2.0)):
        if 30.0 <= bpm <= 300.0:
            candidates.append(
                TempoCandidate(
                    bpm=round(bpm, 3),
                    periodicity_score=round(primary_score * 0.9, 4),
                    relation=relation,
                )
            )
    for index in ranked[1:]:
        bpm = 60.0 / (int(lags[index]) * hop_seconds)
        if any(abs(candidate.bpm - bpm) < 2.0 for candidate in candidates):
            continue
        candidates.append(
            TempoCandidate(
                bpm=round(bpm, 3),
                periodicity_score=round(max(0.0, min(1.0, float(scores[index]))), 4),
                relation="alternate",
            )
        )
        if len(candidates) >= 6:
            break
    runner_score = max(
        (candidate.periodicity_score for candidate in candidates if candidate.relation == "alternate"),
        default=0.0,
    )
    separation = max(0.0, primary_score - runner_score)
    confidence_score = max(0.0, min(1.0, 0.75 * primary_score + 0.25 * min(1.0, separation * 3.0)))
    confidence: Literal["high", "medium", "low", "unavailable"]
    if confidence_score >= 0.62 and len(onset_peaks) >= 8:
        confidence = "high"
    elif confidence_score >= 0.35 and len(onset_peaks) >= 4:
        confidence = "medium"
    else:
        confidence = "low"
    return TempoEstimate(
        available=True,
        bpm=round(primary_bpm, 3),
        confidence=confidence,
        confidence_score=round(confidence_score, 4),
        analyzed_onset_frames=len(envelope),
        detected_onset_count=len(onset_peaks),
        candidates=candidates,
        ambiguity=(
            "Tempo has an inherent half-time/double-time ambiguity; audition the listed metrical alternatives."
        ),
    )


_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def _correlation(left: np.ndarray, right: np.ndarray) -> float:
    left = left - float(np.mean(left))
    right = right - float(np.mean(right))
    denom = float(np.linalg.norm(left) * np.linalg.norm(right))
    return float(np.dot(left, right) / denom) if denom > 1e-20 else 0.0


def estimate_key(loaded: Loaded) -> KeyEstimate:
    mono, rate = _analysis_mono(loaded)
    if len(mono) < 4096 or float(np.sqrt(np.mean(mono * mono))) < 1e-5:
        return KeyEstimate(
            available=False,
            confidence="unavailable",
            confidence_score=0.0,
            ambiguity="Audio has insufficient pitched energy for key estimation.",
        )
    frequencies, _times, spectrum = signal.stft(
        mono,
        fs=rate,
        window="hann",
        nperseg=4096,
        noverlap=3072,
        boundary=None,
        padded=False,
    )
    power = np.abs(spectrum) ** 2
    chroma = np.zeros(12, dtype=np.float64)
    usable = (frequencies >= 40.0) & (frequencies <= min(5000.0, rate / 2.0))
    for bin_index in np.flatnonzero(usable):
        midi_pitch = 69.0 + 12.0 * math.log2(float(frequencies[bin_index]) / 440.0)
        pitch_class = int(round(midi_pitch)) % 12
        # Square-root compression prevents one loud partial from becoming the
        # entire key decision while retaining full-file energy weighting.
        chroma[pitch_class] += float(np.sum(np.sqrt(power[bin_index] + 1e-20)))
    total = float(np.sum(chroma))
    if total <= 1e-12:
        return KeyEstimate(
            available=False,
            confidence="unavailable",
            confidence_score=0.0,
            ambiguity="No stable chroma energy was measured.",
        )
    chroma /= total
    scored: list[tuple[float, int, Literal["major", "minor"]]] = []
    for tonic in range(12):
        scored.append((_correlation(chroma, np.roll(_MAJOR_PROFILE, tonic)), tonic, "major"))
        scored.append((_correlation(chroma, np.roll(_MINOR_PROFILE, tonic)), tonic, "minor"))
    scored.sort(reverse=True, key=lambda item: item[0])
    best, runner = scored[0], scored[1]
    margin = max(0.0, best[0] - runner[0])
    concentration = max(0.0, min(1.0, (float(np.max(chroma)) - 1.0 / 12.0) * 4.0))
    confidence_score = max(0.0, min(1.0, 0.55 * max(0.0, best[0]) + 0.30 * min(1.0, margin * 5.0) + 0.15 * concentration))
    confidence: Literal["high", "medium", "low", "unavailable"]
    if confidence_score >= 0.65 and margin >= 0.08:
        confidence = "high"
    elif confidence_score >= 0.42 and margin >= 0.025:
        confidence = "medium"
    else:
        confidence = "low"
    candidates = [
        KeyCandidate(
            tonic=_NOTE_NAMES[tonic],
            mode=mode,
            key=f"{_NOTE_NAMES[tonic]} {mode}",
            correlation=round(score, 5),
        )
        for score, tonic, mode in scored[:6]
    ]
    return KeyEstimate(
        available=True,
        tonic=_NOTE_NAMES[best[1]],
        mode=best[2],
        key=f"{_NOTE_NAMES[best[1]]} {best[2]}",
        confidence=confidence,
        confidence_score=round(confidence_score, 4),
        correlation=round(best[0], 5),
        runner_up_margin=round(margin, 5),
        chroma=[round(float(value), 6) for value in chroma],
        candidates=candidates,
        ambiguity=(
            "Relative major/minor, modal harmony, tuning drift, and sparse arrangements can produce the same pitch-class evidence."
        ),
    )


def analyze_tempo_and_key(
    path: str, *, max_seconds: float | None = 300.0
) -> AudioMusicAnalysis:
    loaded = load(path, max_seconds=max_seconds)
    return AudioMusicAnalysis(
        analyzed_at=_now(),
        path=loaded.path,
        sha256=_sha256(loaded.path),
        sample_rate_hz=loaded.rate,
        channels=loaded.channels,
        source_duration_seconds=float(loaded.meta["source_duration_sec"]),
        analyzed_duration_seconds=loaded.duration,
        truncated=bool(loaded.meta["truncated"]),
        tempo=estimate_tempo(loaded),
        key=estimate_key(loaded),
        limitations=[
            "Tempo is a periodicity estimate and always retains half-time/double-time ambiguity.",
            "Key is a global major/minor pitch-class profile estimate, not chord, mode, or modulation analysis.",
            "Percussive, atonal, sparse, detuned, or strongly modulating material can yield low-confidence results.",
            "No FL project value was read or changed; this result describes only the decoded file.",
        ],
    )


def _pitch_frames(
    loaded: Loaded, *, fmin: float, fmax: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    mono, rate = _analysis_mono(loaded, target_rate=22050)
    if fmin >= fmax or fmin < 30.0 or fmax > 4000.0:
        raise ValueError("pitch range must satisfy 30 <= fmin < fmax <= 4000")
    win = max(1024, int(round(0.055 * rate)))
    hop = max(1, int(round(0.010 * rate)))
    if len(mono) < win * 2:
        raise AudioError("audio is too short for monophonic transcription")
    starts = np.arange(0, len(mono) - win + 1, hop, dtype=int)
    rms = np.array(
        [math.sqrt(float(np.mean(mono[start:start + win] ** 2))) for start in starts],
        dtype=np.float64,
    )
    # A clip can be voiced for nearly its whole duration, so a lower-percentile
    # value is not necessarily a noise floor. Cap that estimate against a
    # modest share of the loud-frame RMS instead of letting it exceed the
    # performance itself.
    active_threshold = max(
        1e-4,
        min(
            float(np.percentile(rms, 25)) * 1.5,
            float(np.percentile(rms, 95)) * 0.10,
        ),
        float(np.percentile(rms, 95)) * 0.035,
    )
    midi = np.full(len(starts), -1, dtype=int)
    confidences = np.zeros(len(starts), dtype=np.float64)
    for frame_index, start in enumerate(starts):
        if rms[frame_index] < active_threshold:
            continue
        frame = mono[start:start + win]
        frequency = _f0_autocorr(frame, rate, fmin, fmax)
        if frequency <= 0:
            continue
        value = 69.0 + 12.0 * math.log2(frequency / 440.0)
        nearest = int(round(value))
        if not 0 <= nearest <= 127:
            continue
        cents = abs(value - nearest) * 100.0
        midi[frame_index] = nearest
        confidences[frame_index] = max(0.0, 1.0 - cents / 100.0)
    # Median smoothing only within voiced neighborhoods; unvoiced frames stay
    # unvoiced so rests do not turn into held notes.
    voiced = midi >= 0
    filled = np.where(voiced, midi, 0)
    smoothed = signal.medfilt(filled, kernel_size=5)
    midi[voiced] = smoothed[voiced]
    return midi, rms, confidences, hop / rate


def transcribe_monophonic(
    path: str,
    *,
    tempo_bpm: float | None = None,
    fmin_hz: float = 55.0,
    fmax_hz: float = 1760.0,
    minimum_note_seconds: float = 0.08,
    quantize_grid_beats: float | None = 0.25,
    max_seconds: float | None = 180.0,
) -> MelodyTranscription:
    if tempo_bpm is not None and not 10.0 <= tempo_bpm <= 522.0:
        raise ValueError("tempo_bpm must be within 10..522")
    if not 0.03 <= minimum_note_seconds <= 2.0:
        raise ValueError("minimum_note_seconds must be within 0.03..2")
    if quantize_grid_beats is not None and not 0.03125 <= quantize_grid_beats <= 4.0:
        raise ValueError("quantize_grid_beats must be null or within 0.03125..4")
    loaded = load(path, max_seconds=max_seconds)
    midi, rms, confidence, hop_seconds = _pitch_frames(
        loaded, fmin=fmin_hz, fmax=fmax_hz
    )
    if tempo_bpm is not None:
        bpm = tempo_bpm
        tempo_source: Literal["caller", "estimated", "fallback_120"] = "caller"
    else:
        estimate = estimate_tempo(loaded)
        if estimate.available and estimate.bpm is not None and estimate.confidence != "low":
            bpm = estimate.bpm
            tempo_source = "estimated"
        else:
            bpm = 120.0
            tempo_source = "fallback_120"
    raw_segments: list[tuple[int, int, int]] = []
    start_index: int | None = None
    current_pitch = -1
    for index, pitch in enumerate(np.append(midi, -1)):
        if pitch >= 0 and start_index is None:
            start_index, current_pitch = index, int(pitch)
        elif start_index is not None and pitch != current_pitch:
            if (index - start_index) * hop_seconds >= minimum_note_seconds:
                raw_segments.append((start_index, index, current_pitch))
            start_index = index if pitch >= 0 else None
            current_pitch = int(pitch)
    # Merge same-pitch segments separated by a very short unvoiced gap.
    merged: list[tuple[int, int, int]] = []
    max_gap_frames = max(1, int(round(0.05 / hop_seconds)))
    for segment in raw_segments:
        if merged and segment[2] == merged[-1][2] and segment[0] - merged[-1][1] <= max_gap_frames:
            merged[-1] = (merged[-1][0], segment[1], segment[2])
        else:
            merged.append(segment)
    p95 = max(1e-9, float(np.percentile(rms, 95)))
    notes: list[CreativeNote] = []
    seconds_per_beat = 60.0 / bpm
    for start, end, pitch in merged[:4096]:
        start_beats = start * hop_seconds / seconds_per_beat
        duration_beats = max(
            minimum_note_seconds / seconds_per_beat,
            (end - start) * hop_seconds / seconds_per_beat,
        )
        if quantize_grid_beats is not None:
            grid = quantize_grid_beats
            start_beats = round(start_beats / grid) * grid
            duration_beats = max(grid, round(duration_beats / grid) * grid)
        segment_rms = float(np.median(rms[start:end])) if end > start else 0.0
        velocity = max(0.2, min(1.0, 0.2 + 0.8 * math.sqrt(segment_rms / p95)))
        notes.append(
            CreativeNote(
                pitch=pitch,
                start_beats=round(max(0.0, start_beats), 6),
                duration_beats=round(max(1e-4, duration_beats), 6),
                velocity=round(velocity, 4),
            )
        )
    notes.sort(key=lambda note: (note.start_beats, note.pitch))
    # Keep the output monophonic after quantization by shortening an overlap.
    for index in range(len(notes) - 1):
        current = notes[index]
        following = notes[index + 1]
        end = current.start_beats + current.duration_beats
        if end > following.start_beats:
            available = following.start_beats - current.start_beats
            if available > 1e-4:
                notes[index] = current.model_copy(update={"duration_beats": round(available, 6)})
    sequence = make_sequence(
        name="Transcribed melody",
        generator="monophonic_transcription",
        notes=notes,
        tempo_bpm=bpm,
        warnings=[
            "Review octave errors, note boundaries, and rests before applying the sequence to FL.",
        ],
    )
    voiced = midi >= 0
    median_confidence = float(np.median(confidence[voiced])) if np.any(voiced) else 0.0
    return MelodyTranscription(
        transcribed_at=_now(),
        source_path=loaded.path,
        source_sha256=_sha256(loaded.path),
        analyzed_duration_seconds=loaded.duration,
        fmin_hz=fmin_hz,
        fmax_hz=fmax_hz,
        frame_hop_seconds=round(hop_seconds, 6),
        voiced_frame_share=round(float(np.mean(voiced)), 4),
        median_pitch_confidence=round(median_confidence, 4),
        tempo_bpm_used=bpm,
        tempo_source=tempo_source,
        sequence=sequence,
        limitations=[
            "Designed for one pitched source at a time; chords and dense accompaniment invalidate the model.",
            "Autocorrelation can make octave errors and cannot infer articulation, bends, lyrics, or instrument identity.",
            "The result is a reviewable host-side sequence. No Piano Roll mutation or MIDI file write occurred.",
        ],
    )
