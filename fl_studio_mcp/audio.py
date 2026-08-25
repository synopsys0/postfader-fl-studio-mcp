"""Audio measurement for mix and master decisions.

The FL Studio API exposes control surfaces but no audio, so anything that
depends on how a track actually sounds is measured here, from a rendered file
on disk. Everything returns plain numbers plus short plain-language readings,
so the caller can reason about a mix rather than guess at it.
"""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass, field

import numpy as np
import pyloudnorm as pyln
import soundfile as sf
from scipy import signal

# Bands used for spectral balance reporting, in Hz.
BANDS = [
    ("sub", 20, 60),
    ("low", 60, 120),
    ("low_mid", 120, 400),
    ("mid", 400, 2000),
    ("high_mid", 2000, 6000),
    ("presence", 6000, 10000),
    ("air", 10000, 20000),
]

# Sibilance lives here; compared against the body of the voice below it.
SIBILANCE_LO, SIBILANCE_HI = 5000, 9000
BODY_LO, BODY_HI = 300, 3000

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

ANALYZER_VERSION = "audio-analysis-2.0"
TRUE_PEAK_VERSION = "4x-polyphase-chunked-1"
ALIGNMENT_VERSION = "bounded-normalized-xcorr-1"
INTEGRATED_LOUDNESS_VERSION = "pyloudnorm-integrated-loudness-1"
LOUDNESS_MATCH_VERSION = "integrated-loudness-delta-gain-1"
MASKING_ANALYSIS_VERSION = "sample-synchronous-stft-overlap-1"


# Ceiling on the samples one load may hold in memory, independent of how large
# the file on disk is.
#
# The caller-facing size limit bounds bytes on disk, and max_seconds bounds
# duration, but neither bounds what those bytes become once decoded: samples
# are read as float64, so a 16-bit file quadruples, and sample rate and channel
# count multiply on top of that. A ten-minute file is 0.6 GiB decoded at 48 kHz
# stereo and 7.7 GiB at 192 kHz with eight channels -- and a compressed
# container reaches either while staying far under the on-disk limit.
#
# 2 GiB leaves every realistic master analysable in full: ten minutes of 96 kHz
# stereo needs 1.3 GiB. Past that the read is clamped rather than refused, and
# the result says it was clamped and why, because a partial measurement that
# announces itself is more useful than an error.
MAX_DECODED_AUDIO_BYTES = 2 * 1024 * 1024 * 1024


class AudioError(RuntimeError):
    pass


@dataclass
class Loaded:
    path: str
    samples: np.ndarray  # float64, shape (n,) mono mix
    stereo: np.ndarray | None  # float64, shape (n, 2) if stereo
    rate: int
    channels: int
    duration: float
    meta: dict = field(default_factory=dict)
    # Full channel data is retained for measurements where folding to mono can
    # hide a real peak (for example, equal and opposite stereo channels).
    # The default keeps manually-created Loaded fixtures source compatible.
    channel_samples: np.ndarray | None = None


def load(path: str, max_seconds: float | None = None) -> Loaded:
    path = os.path.realpath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise AudioError(f"No such audio file: {path}")
    try:
        # sf.info reads the header only, so the decoded footprint is known
        # before a single sample is committed to memory.
        info = sf.info(path)
        rate = int(info.samplerate)
        channels = max(1, int(info.channels))
        source_frames = int(info.frames)

        wanted = source_frames
        if max_seconds is not None:
            wanted = min(wanted, max(0, int(max_seconds * rate)))

        # sf.read returns (frames, channels) float64 and the mono fold below
        # adds one more column's worth, so a frame costs (channels + 1) * 8.
        per_frame_bytes = (channels + 1) * 8
        affordable_frames = MAX_DECODED_AUDIO_BYTES // per_frame_bytes
        too_wide = affordable_frames < rate

        frames_to_read = min(wanted, affordable_frames)
        truncated_by = None
        if frames_to_read < source_frames:
            truncated_by = (
                "decode_limit" if frames_to_read < wanted else "max_seconds"
            )

        if not too_wide:
            data, rate = sf.read(
                path, frames=frames_to_read, always_2d=True, dtype="float64"
            )
    except Exception as e:
        raise AudioError(f"Could not read {path}: {e}") from None

    # Raised outside the reader's except so it reaches the caller as the
    # refusal it is, rather than being re-wrapped as an unreadable file.
    if too_wide:
        raise AudioError(
            f"{path} declares {channels} channels at {rate} Hz, so a single "
            f"second decodes past the {MAX_DECODED_AUDIO_BYTES}-byte limit; "
            "refusing to read it"
        )
    if data.size == 0:
        raise AudioError(f"{path} contains no audio.")
    channels = data.shape[1]
    stereo = data[:, :2] if channels >= 2 else None
    mono = data.mean(axis=1)
    source_frames = int(info.frames)
    analysed_frames = int(data.shape[0])
    return Loaded(
        path=path,
        samples=mono,
        stereo=stereo,
        rate=rate,
        channels=channels,
        duration=len(mono) / rate,
        meta={
            "format": info.format,
            "subtype": info.subtype,
            "source_frames": source_frames,
            "analyzed_frames": analysed_frames,
            "source_duration_sec": source_frames / rate,
            "truncated": analysed_frames < source_frames,
            # Which bound stopped the read, so a partial measurement is never
            # mistaken for a whole-file one: "max_seconds" is the caller's own
            # limit, "decode_limit" is the memory ceiling.
            "truncated_by": truncated_by,
            "max_seconds": max_seconds,
        },
        channel_samples=data,
    )


# ---------------------------------------------------------------------------
# level
# ---------------------------------------------------------------------------


def _channel_data(a: Loaded) -> np.ndarray:
    """Return all available channels as ``(frames, channels)`` float data."""
    if a.channel_samples is not None:
        data = np.asarray(a.channel_samples, dtype=np.float64)
        return data[:, None] if data.ndim == 1 else data
    if a.stereo is not None:
        data = np.asarray(a.stereo, dtype=np.float64)
        return data[:, None] if data.ndim == 1 else data
    return np.asarray(a.samples, dtype=np.float64)[:, None]


def _true_peak_per_channel(
    x: np.ndarray, rate: int, oversample: int = 4, chunk_seconds: float = 30.0
) -> tuple[list[float], str]:
    """Estimate true peak for every channel over the entire supplied array.

    Processing is chunked to avoid allocating a four-times-larger copy of a
    full song. Overlap is discarded after resampling, so artificial chunk
    boundaries cannot become reported peaks. This is a deterministic 4x
    polyphase estimate, not a claim of certified ITU/EBU meter conformance.
    """
    data = np.asarray(x, dtype=np.float64)
    if data.ndim == 1:
        data = data[:, None]
    if data.ndim != 2:
        raise AudioError("Audio samples must be one- or two-dimensional.")
    if data.shape[0] == 0:
        return [float("-inf")] * data.shape[1], TRUE_PEAK_VERSION

    chunk_frames = max(1, int(rate * chunk_seconds))
    overlap = 128
    peaks = np.zeros(data.shape[1], dtype=np.float64)
    method = TRUE_PEAK_VERSION
    try:
        for start in range(0, data.shape[0], chunk_frames):
            end = min(data.shape[0], start + chunk_frames)
            seg_start = max(0, start - overlap)
            seg_end = min(data.shape[0], end + overlap)
            up = signal.resample_poly(
                data[seg_start:seg_end], oversample, 1, axis=0
            )
            core_start = (start - seg_start) * oversample
            core_end = core_start + (end - start) * oversample
            core = up[core_start:core_end]
            if core.size:
                peaks = np.maximum(peaks, np.max(np.abs(core), axis=0))
    except Exception:
        # A conservative, disclosed fallback is preferable to returning no
        # peak at all. It must not be labelled as true peak.
        peaks = np.max(np.abs(data), axis=0)
        method = "sample-peak-fallback"
    return [_to_db(float(v)) for v in peaks], method


def _true_peak_db(x: np.ndarray, rate: int) -> float:
    """Highest full-duration 4x true-peak estimate across all channels."""
    peaks, _method = _true_peak_per_channel(x, rate)
    return max(peaks) if peaks else float("-inf")


def _to_db(v: float) -> float:
    return -np.inf if v <= 0 else round(20.0 * math.log10(v), 2)


def _fmt_db(v: float) -> float | None:
    return None if not np.isfinite(v) else round(float(v), 2)


def measure_loudness(a: Loaded) -> dict:
    """Integrated / short-term loudness, peaks, crest and headroom."""
    channels = _channel_data(a)
    # pyloudnorm accepts mono and stereo directly. For files with more than two
    # channels, retain the established first-pair behavior and disclose it in
    # analyze() limitations; peak and clipping metrics still inspect all tracks.
    audio = channels[:, 0] if channels.shape[1] == 1 else channels[:, :2]
    meter = pyln.Meter(a.rate)
    try:
        lufs_i = float(meter.integrated_loudness(audio))
    except Exception:
        lufs_i = float("-inf")

    # Short-term (3 s) loudness spread tells us how consistent the level is.
    st = []
    win = int(3 * a.rate)
    hop = int(1 * a.rate)
    if len(a.samples) >= win:
        for start in range(0, len(a.samples) - win + 1, hop):
            chunk = audio[start:start + win]
            try:
                v = float(meter.integrated_loudness(chunk))
            except Exception:
                continue
            if np.isfinite(v):
                st.append(v)

    sample_peaks = np.max(np.abs(channels), axis=0)
    sample_peak_db_per_channel = [_to_db(float(v)) for v in sample_peaks]
    sample_peak = max(sample_peak_db_per_channel)
    true_peak_db_per_channel, true_peak_method = _true_peak_per_channel(
        channels, a.rate
    )
    true_peak = max(true_peak_db_per_channel)
    rms_per_channel = np.sqrt(np.mean(channels ** 2, axis=0))
    rms_db_per_channel = [_to_db(float(v)) for v in rms_per_channel]
    rms = _to_db(float(np.sqrt(np.mean(channels ** 2))))

    clipped_mask = np.abs(channels) >= 0.999
    clipped_per_channel = np.sum(clipped_mask, axis=0).astype(int)
    clipped = int(np.sum(clipped_per_channel))
    dc_per_channel = np.mean(channels, axis=0)
    dc_index = int(np.argmax(np.abs(dc_per_channel)))
    dc_offset = float(dc_per_channel[dc_index])
    out = {
        "lufs_integrated": _fmt_db(lufs_i),
        "lufs_short_term_min": round(min(st), 2) if st else None,
        "lufs_short_term_max": round(max(st), 2) if st else None,
        "loudness_range_lu": round(max(st) - min(st), 2) if len(st) > 1 else None,
        "sample_peak_db": _fmt_db(sample_peak),
        "sample_peak_db_per_channel": [
            _fmt_db(v) for v in sample_peak_db_per_channel
        ],
        "true_peak_dbtp": _fmt_db(true_peak),
        "true_peak_dbtp_per_channel": [
            _fmt_db(v) for v in true_peak_db_per_channel
        ],
        "true_peak_method": true_peak_method,
        "rms_db": _fmt_db(rms),
        "rms_db_per_channel": [_fmt_db(v) for v in rms_db_per_channel],
        "crest_factor_db": (
            round(sample_peak - rms, 2)
            if np.isfinite(sample_peak) and np.isfinite(rms) else None
        ),
        "clipped_samples": clipped,
        "clipped_samples_per_channel": clipped_per_channel.tolist(),
        "clipped_frames": int(np.sum(np.any(clipped_mask, axis=1))),
        "dc_offset": round(dc_offset, 6),
        "dc_offset_per_channel": [round(float(v), 6) for v in dc_per_channel],
        "channel_count": int(channels.shape[1]),
    }
    if np.isfinite(lufs_i) and np.isfinite(true_peak):
        out["headroom_to_0dbtp"] = round(-true_peak, 2)
    return out


# ---------------------------------------------------------------------------
# spectrum
# ---------------------------------------------------------------------------


def _spectrum(a: Loaded, nfft: int = 8192) -> tuple[np.ndarray, np.ndarray]:
    """Average magnitude spectrum over the file (Welch)."""
    nper = min(nfft, len(a.samples))
    if nper < 256:
        raise AudioError("Clip is too short to analyse spectrally.")
    freqs, psd = signal.welch(
        a.samples, fs=a.rate, nperseg=nper, noverlap=nper // 2, scaling="density"
    )
    return freqs, psd


def measure_spectrum(a: Loaded) -> dict:
    freqs, psd = _spectrum(a)
    total = float(np.sum(psd)) or 1e-20

    bands = {}
    for name, lo, hi in BANDS:
        m = (freqs >= lo) & (freqs < hi)
        share = float(np.sum(psd[m])) / total
        bands[name] = {
            "range_hz": [lo, hi],
            "energy_share": round(share, 4),
            "level_db": _fmt_db(_to_db(math.sqrt(share)) if share > 0 else 0.0),
        }

    # Spectral centroid: a rough "brightness" number.
    centroid = float(np.sum(freqs * psd) / (np.sum(psd) or 1e-20))

    # Sibilance: energy in the ess band relative to vocal body.
    sib_m = (freqs >= SIBILANCE_LO) & (freqs < SIBILANCE_HI)
    body_m = (freqs >= BODY_LO) & (freqs < BODY_HI)
    sib = float(np.sum(psd[sib_m]))
    body = float(np.sum(psd[body_m])) or 1e-20
    sib_ratio = sib / body

    # Where in the ess band the energy actually peaks — the de-esser target.
    sib_peak_hz = None
    if np.any(sib_m):
        sub = psd[sib_m]
        sib_peak_hz = round(float(freqs[sib_m][int(np.argmax(sub))]), 1)

    # Rumble below the useful range.
    rumble_m = freqs < 40
    rumble = float(np.sum(psd[rumble_m])) / total

    return {
        "bands": bands,
        "spectral_centroid_hz": round(centroid, 1),
        "sibilance_ratio": round(sib_ratio, 4),
        "sibilance_peak_hz": sib_peak_hz,
        "sub_40hz_share": round(rumble, 5),
        "dominant_hz": round(float(freqs[int(np.argmax(psd))]), 1),
    }


# ---------------------------------------------------------------------------
# dynamics and noise
# ---------------------------------------------------------------------------


def measure_dynamics(a: Loaded) -> dict:
    """Short-window RMS distribution — how much the level moves around."""
    win = max(256, int(0.05 * a.rate))
    hop = win // 2
    n = (len(a.samples) - win) // hop + 1
    if n < 4:
        return {"note": "clip too short for dynamics analysis"}
    frames = np.lib.stride_tricks.sliding_window_view(a.samples, win)[::hop]
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    rms_db = 20 * np.log10(np.maximum(rms, 1e-10))

    active = rms_db[rms_db > (np.max(rms_db) - 60)]
    if active.size == 0:
        active = rms_db

    p = np.percentile(active, [10, 50, 90, 95])
    noise_floor = float(np.percentile(rms_db, 5))

    return {
        "rms_p10_db": round(float(p[0]), 2),
        "rms_median_db": round(float(p[1]), 2),
        "rms_p90_db": round(float(p[2]), 2),
        "rms_p95_db": round(float(p[3]), 2),
        "dynamic_spread_db": round(float(p[2] - p[0]), 2),
        "noise_floor_db": round(noise_floor, 2),
        "silence_share": round(float(np.mean(rms_db < noise_floor + 6)), 4),
    }


def measure_stereo(a: Loaded) -> dict:
    if a.stereo is None:
        return {"channels": a.channels, "note": "mono file"}
    left, right = a.stereo[:, 0], a.stereo[:, 1]
    denom = float(np.sqrt(np.sum(left ** 2) * np.sum(right ** 2)))
    corr = float(np.sum(left * right) / denom) if denom > 0 else 1.0
    mid = (left + right) / 2
    side = (left - right) / 2
    m_e = float(np.sum(mid ** 2))
    s_e = float(np.sum(side ** 2))
    width = s_e / (m_e + 1e-20)
    return {
        "channels": a.channels,
        "correlation": round(corr, 4),
        "mid_side_ratio": round(width, 4),
        "mono_compatible": bool(corr > 0.3),
        "balance_db": round(_to_db(float(np.sqrt(np.mean(right ** 2))))
                             - _to_db(float(np.sqrt(np.mean(left ** 2)))), 2)
        if np.any(left) and np.any(right) else 0.0,
    }


# ---------------------------------------------------------------------------
# pitch
# ---------------------------------------------------------------------------


def _f0_autocorr(frame: np.ndarray, rate: int, fmin: float, fmax: float) -> float:
    """Fundamental of one frame via autocorrelation; 0.0 if unvoiced."""
    frame = frame - np.mean(frame)
    if np.max(np.abs(frame)) < 1e-4:
        return 0.0
    frame = frame * np.hanning(len(frame))
    corr = signal.fftconvolve(frame, frame[::-1], mode="full")[len(frame) - 1:]
    if corr[0] <= 0:
        return 0.0
    corr /= corr[0]

    lag_min = int(rate / fmax)
    lag_max = min(int(rate / fmin), len(corr) - 1)
    if lag_max <= lag_min:
        return 0.0
    window = corr[lag_min:lag_max]
    peak = int(np.argmax(window)) + lag_min
    if corr[peak] < 0.35:  # too weak to call voiced
        return 0.0
    # Parabolic interpolation for sub-sample accuracy.
    if 0 < peak < len(corr) - 1:
        a0, b0, c0 = corr[peak - 1], corr[peak], corr[peak + 1]
        denom = a0 - 2 * b0 + c0
        if denom != 0:
            peak = peak + 0.5 * (a0 - c0) / denom
    return float(rate / peak) if peak > 0 else 0.0


def _hz_to_note(hz: float) -> tuple[str, float]:
    """Nearest note name and the deviation from it in cents."""
    if hz <= 0:
        return "", 0.0
    midi = 69 + 12 * math.log2(hz / 440.0)
    nearest = round(midi)
    cents = (midi - nearest) * 100.0
    name = f"{NOTE_NAMES[int(nearest) % 12]}{int(nearest) // 12 - 1}"
    return name, cents


def measure_pitch(a: Loaded, fmin: float = 65.0, fmax: float = 1200.0) -> dict:
    """Track the fundamental and report how in-tune the performance is."""
    win = int(0.046 * a.rate)  # ~46 ms, enough for low male voice
    hop = int(0.010 * a.rate)
    if len(a.samples) < win * 2:
        return {"note": "clip too short for pitch analysis"}

    starts = range(0, len(a.samples) - win, hop)
    total_frames = max(1, len(starts))
    f0s = []
    for start in starts:
        f = _f0_autocorr(a.samples[start:start + win], a.rate, fmin, fmax)
        if f > 0:
            f0s.append(f)

    if len(f0s) < 5:
        return {
            "voiced_share": 0.0,
            "note": "no stable pitch detected — percussive, noisy or silent?",
        }

    f0 = np.array(f0s)
    # Reject octave errors: drop frames far from the median.
    med = float(np.median(f0))
    keep = (f0 > med / 1.9) & (f0 < med * 1.9)
    f0c = f0[keep]
    if f0c.size < 5:
        f0c = f0

    cents = np.array([_hz_to_note(float(f))[1] for f in f0c])
    abs_cents = np.abs(cents)

    lo_name, _ = _hz_to_note(float(np.percentile(f0c, 5)))
    hi_name, _ = _hz_to_note(float(np.percentile(f0c, 95)))
    med_name, med_cents = _hz_to_note(med)

    # Frame-to-frame wobble, in cents — high values mean unsteady pitch.
    if f0c.size > 1:
        step = 1200 * np.log2(np.maximum(f0c[1:], 1e-6) / np.maximum(f0c[:-1], 1e-6))
        jitter = float(np.median(np.abs(step)))
    else:
        jitter = 0.0

    return {
        "voiced_share": round(len(f0s) / total_frames, 3),
        "median_hz": round(med, 2),
        "median_note": med_name,
        "median_cents_off": round(float(med_cents), 1),
        "range_low_note": lo_name,
        "range_high_note": hi_name,
        "cents_off_mean_abs": round(float(np.mean(abs_cents)), 1),
        "cents_off_p90": round(float(np.percentile(abs_cents, 90)), 1),
        "in_tune_share_within_20c": round(float(np.mean(abs_cents <= 20)), 3),
        "in_tune_share_within_50c": round(float(np.mean(abs_cents <= 50)), 3),
        "pitch_jitter_cents": round(jitter, 1),
    }


# ---------------------------------------------------------------------------
# readings
# ---------------------------------------------------------------------------


def _readings(loud: dict, spec: dict, dyn: dict, stereo: dict,
              pitch: dict | None, target_lufs: float) -> list[str]:
    """Turn numbers into short observations. Facts, not prescriptions."""
    out = []

    li = loud.get("lufs_integrated")
    if li is not None:
        delta = target_lufs - li
        if abs(delta) < 0.5:
            out.append(f"Integrated loudness {li} LUFS — on target ({target_lufs}).")
        else:
            direction = "quieter" if delta > 0 else "louder"
            out.append(
                f"Integrated loudness {li} LUFS, {abs(delta):.1f} LU {direction} "
                f"than the {target_lufs} LUFS target."
            )

    tp = loud.get("true_peak_dbtp")
    if tp is not None:
        if tp > -0.1:
            out.append(f"True peak {tp} dBTP — at or over full scale; lossy encoders will distort.")
        elif tp > -1.0:
            out.append(f"True peak {tp} dBTP — under 1 dB of headroom.")
    if loud.get("clipped_samples", 0) > 0:
        out.append(f"{loud['clipped_samples']} samples at full scale — already clipped.")
    if abs(loud.get("dc_offset") or 0) > 0.001:
        out.append(f"DC offset {loud['dc_offset']} — worth a high-pass.")

    crest = loud.get("crest_factor_db")
    if crest is not None:
        if crest < 6:
            out.append(f"Crest factor {crest} dB — heavily compressed, little transient left.")
        elif crest > 20:
            out.append(f"Crest factor {crest} dB — very dynamic, peaks far above the body.")

    bands = spec.get("bands", {})
    if bands:
        sub = bands.get("sub", {}).get("energy_share", 0)
        low = bands.get("low", {}).get("energy_share", 0)
        air = bands.get("air", {}).get("energy_share", 0)
        lmid = bands.get("low_mid", {}).get("energy_share", 0)
        if sub + low > 0.55:
            out.append(f"Low end holds {round((sub + low) * 100)}% of total energy — bass-heavy.")
        if lmid > 0.4:
            out.append(f"Low-mids {round(lmid * 100)}% of energy — likely boxy around 200-400 Hz.")
        if air < 0.005:
            out.append("Very little above 10 kHz — dull, or low-passed somewhere.")
    if spec.get("sub_40hz_share", 0) > 0.02:
        out.append(f"{round(spec['sub_40hz_share'] * 100, 1)}% of energy below 40 Hz — inaudible rumble eating headroom.")

    sib = spec.get("sibilance_ratio")
    if sib is not None and sib > 0.25:
        out.append(
            f"Sibilance ratio {sib} (peaks near {spec.get('sibilance_peak_hz')} Hz) — "
            "harsh esses, a de-esser there would help."
        )

    nf = dyn.get("noise_floor_db")
    if nf is not None and nf > -50:
        out.append(f"Noise floor {nf} dBFS — audible hiss/room between phrases.")
    spread = dyn.get("dynamic_spread_db")
    if spread is not None and spread > 18:
        out.append(f"Level swings {spread} dB between quiet and loud passages — uneven delivery.")

    corr = stereo.get("correlation")
    if corr is not None and corr < 0.3:
        out.append(f"Stereo correlation {corr} — phase issues, will thin out in mono.")

    if pitch and pitch.get("median_hz"):
        it = pitch.get("in_tune_share_within_20c")
        if it is not None:
            pct = round(it * 100)
            if pct < 60:
                out.append(
                    f"Only {pct}% of voiced frames land within 20 cents of a note "
                    f"(mean {pitch['cents_off_mean_abs']}c off) — noticeably out of tune."
                )
            elif pct < 85:
                out.append(f"{pct}% of frames within 20 cents — some drift, light correction would tidy it.")
            else:
                out.append(f"{pct}% of frames within 20 cents — pitch is solid.")
        if (pitch.get("pitch_jitter_cents") or 0) > 25:
            out.append(f"Pitch jitter {pitch['pitch_jitter_cents']}c between frames — unsteady, wide vibrato or a tracking artefact.")
        out.append(
            f"Range {pitch['range_low_note']}–{pitch['range_high_note']}, "
            f"centred on {pitch['median_note']}."
        )

    return out


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_provenance(a: Loaded) -> dict:
    source_frames = int(a.meta.get("source_frames", len(a.samples)))
    analysed_frames = int(a.meta.get("analyzed_frames", len(a.samples)))
    return {
        "canonical_path": os.path.realpath(a.path),
        "sha256": _sha256_file(a.path),
        "sample_rate_hz": int(a.rate),
        "channels": int(a.channels),
        "source_frames": source_frames,
        "analyzed_frames": analysed_frames,
        "source_duration_sec": round(source_frames / a.rate, 6),
        "analyzed_duration_sec": round(analysed_frames / a.rate, 6),
        "truncated": bool(a.meta.get("truncated", analysed_frames < source_frames)),
    }


def _analysis_limitations(a: Loaded, include_pitch: bool) -> list[str]:
    limitations = [
        "True peak is a deterministic 4x polyphase estimate, not a certified ITU/EBU meter.",
        "The reported loudness_range_lu is a spread of 3-second windows, not standards-compliant EBU Loudness Range (LRA).",
        "Spectrum, dynamics, and pitch use an arithmetic mono fold-down; phase-opposed channels can cancel.",
        "Measurements describe the rendered file only; they do not identify artistic intent or rank a mix automatically.",
    ]
    if a.channels > 2:
        limitations.append(
            "Integrated loudness and stereo analysis use the first channel pair for this multichannel file; peak and clipping metrics use every channel."
        )
    if bool(a.meta.get("truncated")):
        limitations.append(
            "Analysis was truncated by max_seconds; conclusions do not cover the unanalysed tail."
        )
    if include_pitch:
        limitations.append(
            "Pitch uses a simple monophonic autocorrelation tracker and is not reliable for polyphonic, noisy, or heavily effected audio."
        )
    return limitations


def _analysis_confidence(a: Loaded, loud: dict) -> dict:
    truncated = bool(a.meta.get("truncated"))
    finite_loudness = loud.get("lufs_integrated") is not None
    score = 0.97
    basis = [
        "Peak, clipping, RMS, and DC metrics scanned every analysed frame and channel."
    ]
    if a.duration < 3.0:
        score -= 0.2
        basis.append("The render is shorter than a 3-second short-term loudness window.")
    if truncated:
        score -= 0.25
        basis.append("The source was truncated by max_seconds.")
    if not finite_loudness:
        score -= 0.2
        basis.append("Integrated loudness could not be measured, usually because audio is too short or silent.")
    if loud.get("true_peak_method") == "sample-peak-fallback":
        score -= 0.2
        basis.append("True-peak oversampling failed, so only sample peak is available.")
    if a.channels > 2:
        score -= 0.1
        basis.append("Some non-peak analyzers inspect only the first channel pair.")
    score = round(max(0.0, min(1.0, score)), 2)
    level = "high" if score >= 0.8 else "medium" if score >= 0.55 else "low"
    return {
        "level": level,
        "score": score,
        "basis": basis,
        "scope": "the analysed frames of this render",
    }


def _analyze_loaded(a: Loaded, include_pitch: bool, target_lufs: float) -> dict:
    loud = measure_loudness(a)
    spec = measure_spectrum(a)
    dyn = measure_dynamics(a)
    stereo = measure_stereo(a)
    pitch = measure_pitch(a) if include_pitch else None
    provenance = _file_provenance(a)

    result = {
        "file": {
            "path": a.path,
            "duration_sec": round(a.duration, 3),
            "sample_rate": a.rate,
            "channels": a.channels,
            "sha256": provenance["sha256"],
            **a.meta,
        },
        "loudness": loud,
        "spectrum": spec,
        "dynamics": dyn,
        "stereo": stereo,
        "target_lufs": target_lufs,
        "provenance": {
            "analyzer_version": ANALYZER_VERSION,
            "input": provenance,
            "analyzer_versions": {
                "true_peak": loud.get("true_peak_method", TRUE_PEAK_VERSION),
                "integrated_loudness": INTEGRATED_LOUDNESS_VERSION,
                "spectrum": "welch-psd-mono-fold-down-1",
                "dynamics": "50ms-rms-percentiles-1",
                "pitch": "autocorrelation-monophonic-1" if include_pitch else None,
            },
        },
        "confidence": _analysis_confidence(a, loud),
        "limitations": _analysis_limitations(a, include_pitch),
    }
    if pitch is not None:
        result["pitch"] = pitch
    result["readings"] = _readings(loud, spec, dyn, stereo, pitch, target_lufs)
    return result


def analyze(path: str, include_pitch: bool = False,
            target_lufs: float = -14.0, max_seconds: float | None = 600) -> dict:
    """Analyse one render and attach deterministic provenance and limitations."""
    return _analyze_loaded(
        load(path, max_seconds=max_seconds), include_pitch, target_lufs
    )


def _resample_channels(data: np.ndarray, source_rate: int,
                       target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return np.asarray(data, dtype=np.float64)
    divisor = math.gcd(int(source_rate), int(target_rate))
    return signal.resample_poly(
        np.asarray(data, dtype=np.float64),
        target_rate // divisor,
        source_rate // divisor,
        axis=0,
    )


def _alignment_source(data: np.ndarray) -> tuple[np.ndarray, str]:
    """Choose a deterministic correlation signal without silent cancellation."""
    data = np.asarray(data, dtype=np.float64)
    if data.ndim == 1:
        return data, "mono"
    mono = np.mean(data, axis=1)
    channel_rms = np.sqrt(np.mean(data ** 2, axis=0))
    strongest = int(np.argmax(channel_rms))
    mono_rms = float(np.sqrt(np.mean(mono ** 2)))
    if data.shape[1] > 1 and mono_rms < float(channel_rms[strongest]) * 0.05:
        return data[:, strongest], f"channel_{strongest + 1}_anti_cancellation_fallback"
    return mono, "arithmetic_channel_mean"


def _normalised_correlations(
    reference: np.ndarray, target: np.ndarray, lags: np.ndarray,
    raw_correlation: np.ndarray
) -> np.ndarray:
    """Pearson-like overlap correlation for each target-relative lag."""
    ref_n = len(reference)
    tgt_n = len(target)
    tgt_start = np.maximum(lags, 0).astype(int)
    ref_start = np.maximum(-lags, 0).astype(int)
    overlap = np.minimum(tgt_n - tgt_start, ref_n - ref_start).astype(int)

    ref_sum = np.concatenate(([0.0], np.cumsum(reference, dtype=np.float64)))
    tgt_sum = np.concatenate(([0.0], np.cumsum(target, dtype=np.float64)))
    ref_sq = np.concatenate(([0.0], np.cumsum(reference ** 2, dtype=np.float64)))
    tgt_sq = np.concatenate(([0.0], np.cumsum(target ** 2, dtype=np.float64)))

    sum_r = ref_sum[ref_start + overlap] - ref_sum[ref_start]
    sum_t = tgt_sum[tgt_start + overlap] - tgt_sum[tgt_start]
    energy_r = ref_sq[ref_start + overlap] - ref_sq[ref_start]
    energy_t = tgt_sq[tgt_start + overlap] - tgt_sq[tgt_start]
    safe_n = np.maximum(overlap, 1)
    covariance = raw_correlation - (sum_r * sum_t / safe_n)
    variance_r = np.maximum(energy_r - sum_r ** 2 / safe_n, 0.0)
    variance_t = np.maximum(energy_t - sum_t ** 2 / safe_n, 0.0)
    denominator = np.sqrt(variance_r * variance_t)
    return np.divide(
        covariance,
        denominator,
        out=np.zeros_like(covariance, dtype=np.float64),
        where=(denominator > 1e-20) & (overlap > 1),
    )


def _estimate_alignment(
    reference: np.ndarray,
    target: np.ndarray,
    rate: int,
    max_alignment_seconds: float,
    alignment_rate_hz: int,
) -> tuple[dict, int]:
    if max_alignment_seconds < 0:
        raise AudioError("max_alignment_seconds must be zero or positive.")
    if alignment_rate_hz <= 0:
        raise AudioError("alignment_rate_hz must be positive.")

    ref_source, ref_source_method = _alignment_source(reference)
    tgt_source, tgt_source_method = _alignment_source(target)
    analysis_rate = min(int(alignment_rate_hz), int(rate))
    ref_ds = _resample_channels(ref_source[:, None], rate, analysis_rate)[:, 0]
    tgt_ds = _resample_channels(tgt_source[:, None], rate, analysis_rate)[:, 0]
    if min(len(ref_ds), len(tgt_ds)) < 2:
        raise AudioError("Audio is too short for time alignment.")

    corr = signal.correlate(tgt_ds, ref_ds, mode="full", method="fft")
    all_lags = signal.correlation_lags(len(tgt_ds), len(ref_ds), mode="full")
    requested_max_lag = int(round(max_alignment_seconds * analysis_rate))
    possible_max_lag = max(0, min(len(ref_ds), len(tgt_ds)) - 2)
    max_lag = min(requested_max_lag, possible_max_lag)
    mask = (all_lags >= -max_lag) & (all_lags <= max_lag)
    lags = all_lags[mask].astype(int)
    raw = corr[mask]
    normalised = _normalised_correlations(ref_ds, tgt_ds, lags, raw)
    magnitudes = np.abs(normalised)
    peak = float(np.max(magnitudes)) if magnitudes.size else 0.0
    candidates = np.flatnonzero(np.isclose(magnitudes, peak, rtol=0.0, atol=1e-12))
    best_index = min(candidates, key=lambda i: (abs(int(lags[i])), int(lags[i])))
    best_lag_ds = int(lags[best_index])
    best_correlation = float(normalised[best_index])

    exclusion = max(1, int(round(0.01 * analysis_rate)))
    alternatives = magnitudes[np.abs(lags - best_lag_ds) > exclusion]
    second = float(np.max(alternatives)) if alternatives.size else 0.0
    ambiguity_ratio = peak / second if second > 1e-12 else (999.0 if peak > 0 else 0.0)

    lag_seconds = best_lag_ds / analysis_rate
    lag_samples = int(round(lag_seconds * rate))
    if lag_samples >= 0:
        reference_start = 0
        target_start = lag_samples
        common_frames = min(len(reference), len(target) - lag_samples)
    else:
        reference_start = -lag_samples
        target_start = 0
        common_frames = min(len(reference) + lag_samples, len(target))
    common_frames = max(0, int(common_frames))
    coverage = common_frames / max(1, min(len(reference), len(target)))

    unambiguous = max_lag == 0 or ambiguity_ratio >= 1.2
    if peak >= 0.9 and unambiguous and coverage >= 0.8:
        level, score = "high", min(0.99, 0.85 + 0.14 * peak)
    elif peak >= 0.6 and (max_lag == 0 or ambiguity_ratio >= 1.05) and coverage >= 0.5:
        level, score = "medium", min(0.79, 0.5 + 0.3 * peak)
    else:
        level, score = "low", min(0.49, 0.15 + 0.35 * peak)

    report = {
        "method": ALIGNMENT_VERSION,
        "lag_convention": "positive target_lag means the target starts later than the reference",
        "analysis_rate_hz": analysis_rate,
        "lag_resolution_seconds": round(1.0 / analysis_rate, 9),
        "max_lag_seconds": round(max_lag / analysis_rate, 6),
        "target_lag_samples_at_reference_rate": lag_samples,
        "target_lag_seconds": round(lag_seconds, 6),
        "normalised_correlation": round(best_correlation, 6),
        "absolute_correlation": round(peak, 6),
        "peak_to_next_distinct_peak_ratio": round(ambiguity_ratio, 4),
        "reference_alignment_source": ref_source_method,
        "target_alignment_source": tgt_source_method,
        "common_frames": common_frames,
        "common_duration_sec": round(common_frames / rate, 6),
        "common_coverage": round(coverage, 6),
        "reference_common_start_sample": reference_start,
        "reference_common_end_sample_exclusive": reference_start + common_frames,
        "target_common_start_sample_at_reference_rate": target_start,
        "target_common_end_sample_exclusive_at_reference_rate": target_start + common_frames,
        "confidence": {
            "level": level,
            "score": round(score, 2),
            "basis": (
                "correlation strength, distinctness of the best lag, and common-overlap coverage"
            ),
        },
    }
    return report, lag_samples


def _aligned_regions(reference: np.ndarray, target: np.ndarray,
                     target_lag_samples: int) -> tuple[np.ndarray, np.ndarray]:
    if target_lag_samples >= 0:
        ref_start, tgt_start = 0, target_lag_samples
    else:
        ref_start, tgt_start = -target_lag_samples, 0
    frames = min(len(reference) - ref_start, len(target) - tgt_start)
    if frames <= 0:
        raise AudioError("No common audio remains after alignment.")
    return (
        reference[ref_start:ref_start + frames],
        target[tgt_start:tgt_start + frames],
    )


def _integrated_loudness(data: np.ndarray, rate: int) -> float:
    data = np.asarray(data, dtype=np.float64)
    if data.ndim == 2:
        meter_audio = data[:, 0] if data.shape[1] == 1 else data[:, :2]
    else:
        meter_audio = data
    try:
        return float(pyln.Meter(rate).integrated_loudness(meter_audio))
    except Exception:
        return float("-inf")


def _loudness_match(reference: np.ndarray, target: np.ndarray,
                    rate: int) -> tuple[np.ndarray, dict]:
    ref_lufs = _integrated_loudness(reference, rate)
    target_lufs = _integrated_loudness(target, rate)
    applied = bool(np.isfinite(ref_lufs) and np.isfinite(target_lufs))
    gain_db = float(ref_lufs - target_lufs) if applied else 0.0
    # Refuse pathological boosts. This is an in-memory comparison transform,
    # but an extreme gain would still make the resulting metrics meaningless.
    if applied and abs(gain_db) > 30.0:
        applied = False
    matched = target * (10.0 ** (gain_db / 20.0)) if applied else target.copy()
    after_lufs = _integrated_loudness(matched, rate) if applied else target_lufs
    residual = after_lufs - ref_lufs if np.isfinite(after_lufs) and np.isfinite(ref_lufs) else None
    peak_before = _to_db(float(np.max(np.abs(target)))) if target.size else float("-inf")
    peak_after = _to_db(float(np.max(np.abs(matched)))) if matched.size else float("-inf")
    report = {
        "method": LOUDNESS_MATCH_VERSION,
        "applied": applied,
        "in_memory_only": True,
        "source_files_modified": False,
        "reference_lufs": _fmt_db(ref_lufs),
        "target_lufs_before": _fmt_db(target_lufs),
        "gain_db_applied": round(gain_db, 3) if applied else None,
        "target_lufs_after": _fmt_db(after_lufs),
        "residual_lu": round(float(residual), 3) if residual is not None else None,
        "target_sample_peak_db_before": _fmt_db(peak_before),
        "target_sample_peak_db_after": _fmt_db(peak_after),
        "clipped_channel_samples_after": int(np.sum(np.abs(matched) >= 0.999)),
    }
    if not applied:
        report["reason_not_applied"] = (
            "Integrated loudness was unavailable or the required gain exceeded the 30 dB comparison guard."
        )
    return matched, report


def _loaded_from_channels(path: str, data: np.ndarray, rate: int) -> Loaded:
    data = np.asarray(data, dtype=np.float64)
    if data.ndim == 1:
        data = data[:, None]
    return Loaded(
        path=path,
        samples=np.mean(data, axis=1),
        stereo=data[:, :2] if data.shape[1] >= 2 else None,
        rate=rate,
        channels=data.shape[1],
        duration=len(data) / rate,
        channel_samples=data,
    )


def compare(reference: str, target: str, max_seconds: float | None = 600,
            max_alignment_seconds: float = 2.0,
            alignment_rate_hz: int = 2000) -> dict:
    """Compare the common, aligned overlap after in-memory loudness matching.

    No file is written or modified. A fail-closed ``comparison_ready`` flag is
    true only for an unambiguous same-material alignment with adequate overlap.
    Metrics are still returned for a reference song, but low confidence keeps
    those numbers from being treated as an automatic candidate ranking.
    """
    ref_loaded = load(reference, max_seconds=max_seconds)
    tgt_loaded = load(target, max_seconds=max_seconds)
    ref_data = _channel_data(ref_loaded)
    target_source_data = _channel_data(tgt_loaded)
    rate_conversion = None
    if tgt_loaded.rate != ref_loaded.rate:
        target_data = _resample_channels(
            target_source_data, tgt_loaded.rate, ref_loaded.rate
        )
        rate_conversion = {
            "from_hz": tgt_loaded.rate,
            "to_hz": ref_loaded.rate,
            "method": "scipy-resample-poly",
        }
    else:
        target_data = target_source_data

    alignment, target_lag = _estimate_alignment(
        ref_data,
        target_data,
        ref_loaded.rate,
        max_alignment_seconds,
        alignment_rate_hz,
    )
    aligned_ref, aligned_target = _aligned_regions(ref_data, target_data, target_lag)
    matched_target, loudness_match = _loudness_match(
        aligned_ref, aligned_target, ref_loaded.rate
    )

    ref_spec = measure_spectrum(
        _loaded_from_channels(ref_loaded.path, aligned_ref, ref_loaded.rate)
    )
    target_spec = measure_spectrum(
        _loaded_from_channels(tgt_loaded.path, matched_target, ref_loaded.rate)
    )

    deltas = {}
    for name, _lo, _hi in BANDS:
        r = ref_spec["bands"][name]["energy_share"]
        t = target_spec["bands"][name]["energy_share"]
        ratio = (t + 1e-12) / (r + 1e-12)
        deltas[name] = {
            "reference_share": r,
            "target_share": t,
            "difference_db": round(10 * math.log10(ratio), 2),
        }

    band_notes = []
    for name, d in deltas.items():
        diff = d["difference_db"]
        if abs(diff) >= 2.0:
            word = "more" if diff > 0 else "less"
            band_notes.append(
                f"{name.replace('_', ' ')}: {abs(diff)} dB {word} than the reference.")
    if not band_notes:
        band_notes.append("Tonal balance closely matches the reference.")

    notes = []
    rl = loudness_match["reference_lufs"]
    tl = loudness_match["target_lufs_before"]
    if rl is not None and tl is not None:
        if loudness_match["applied"]:
            notes.append(
                f"Loudness before matching: target {tl} LUFS vs reference {rl} LUFS "
                f"({round(tl - rl, 2):+} LU); comparison gain "
                f"{loudness_match['gain_db_applied']:+} dB."
            )
        else:
            notes.append(
                f"Loudness before matching: target {tl} LUFS vs reference {rl} LUFS; "
                "the guarded comparison gain was not applied."
            )
    notes.append(
        f"Alignment: target lag {alignment['target_lag_seconds']:+} s, "
        f"correlation {alignment['normalised_correlation']} "
        f"({alignment['confidence']['level']} confidence)."
    )
    notes.extend(band_notes)

    same_channels = ref_data.shape[1] == target_data.shape[1]
    untruncated = not (
        bool(ref_loaded.meta.get("truncated")) or bool(tgt_loaded.meta.get("truncated"))
    )
    residual = loudness_match.get("residual_lu")
    ready = bool(
        alignment["confidence"]["level"] == "high"
        and alignment["common_coverage"] >= 0.8
        and loudness_match["applied"]
        and residual is not None
        and abs(residual) <= 0.1
        and same_channels
        and untruncated
    )
    confidence_score = float(alignment["confidence"]["score"])
    confidence_basis = [
        "Candidate readiness requires high-confidence time alignment, at least 80% common coverage, successful loudness matching, matching channel counts, and complete analysed inputs."
    ]
    if not same_channels:
        confidence_score *= 0.8
        confidence_basis.append("Input channel counts differ.")
    if not untruncated:
        confidence_score *= 0.7
        confidence_basis.append("At least one input was truncated by max_seconds.")
    if not loudness_match["applied"]:
        confidence_score *= 0.5
        confidence_basis.append("Loudness matching could not be applied.")
    if alignment["confidence"]["level"] != "high":
        confidence_basis.append("Time alignment is ambiguous or weak; do not rank candidates automatically.")
    confidence_score = round(max(0.0, min(1.0, confidence_score)), 2)
    confidence_level = (
        "high" if ready and confidence_score >= 0.8
        else "medium" if confidence_score >= 0.55
        else "low"
    )

    limitations = [
        "Only the common aligned overlap is compared; non-overlapping heads and tails are excluded.",
        "Alignment is correlation-based and can be ambiguous for periodic, silent, unrelated, or substantially rearranged audio.",
        "Loudness matching applies one constant in-memory gain; it does not model section loudness, dynamics, or listener preference.",
        "Spectral comparison uses an arithmetic mono fold-down and can under-report phase-opposed content.",
        "These measurements do not constitute an artistic verdict and never select the loudest or brightest candidate automatically.",
    ]
    if rate_conversion is not None:
        limitations.append(
            "The target was resampled in memory to the reference rate before alignment and comparison."
        )
    if not untruncated:
        limitations.append(
            "At least one input was truncated by max_seconds; comparison_ready is false."
        )

    return {
        "reference": reference,
        "target": target,
        "band_deltas": deltas,
        "centroid_hz": {
            "reference": ref_spec["spectral_centroid_hz"],
            "target": target_spec["spectral_centroid_hz"],
        },
        "alignment": alignment,
        "loudness_matching": loudness_match,
        "comparison_ready": ready,
        "confidence": {
            "level": confidence_level,
            "score": confidence_score,
            "basis": confidence_basis,
        },
        "provenance": {
            "analyzer_version": ANALYZER_VERSION,
            "reference": _file_provenance(ref_loaded),
            "target": _file_provenance(tgt_loaded),
            "analyzer_versions": {
                "alignment": ALIGNMENT_VERSION,
                "integrated_loudness": INTEGRATED_LOUDNESS_VERSION,
                "loudness_matching": LOUDNESS_MATCH_VERSION,
                "spectrum": "welch-psd-mono-fold-down-1",
            },
            "parameters": {
                "max_seconds": max_seconds,
                "max_alignment_seconds": max_alignment_seconds,
                "alignment_rate_hz": alignment_rate_hz,
            },
            "target_rate_conversion": rate_conversion,
        },
        "limitations": limitations,
        "readings": notes,
    }


# ---------------------------------------------------------------------------
# vocal / instrumental context
# ---------------------------------------------------------------------------


def _channel_power_spectrogram(
    data: np.ndarray, rate: int, nperseg: int = 2048
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return phase-safe mean channel power as ``(frequency, time)``.

    Each channel is transformed independently before power is averaged. This
    avoids falsely calling anti-phase stereo content silent through a mono
    fold-down. The result is descriptive rather than a perceptual masking
    model.
    """
    channels = np.asarray(data, dtype=np.float64)
    if channels.ndim == 1:
        channels = channels[:, None]
    if channels.ndim != 2 or channels.shape[0] < 256:
        raise AudioError("Audio is too short for contextual masking analysis.")
    window = min(int(nperseg), int(channels.shape[0]))
    if window < 256:
        raise AudioError("Audio is too short for contextual masking analysis.")
    overlap = min(window - 1, (window * 3) // 4)
    powers = []
    frequencies = times = None
    for channel in range(channels.shape[1]):
        frequencies, times, transformed = signal.stft(
            channels[:, channel],
            fs=rate,
            window="hann",
            nperseg=window,
            noverlap=overlap,
            boundary=None,
            padded=False,
        )
        powers.append(np.abs(transformed) ** 2)
    assert frequencies is not None and times is not None
    return frequencies, times, np.mean(np.stack(powers, axis=0), axis=0)


def _masking_limitations() -> list[str]:
    return [
        "Inputs must be sample-synchronous renders of the same section; the analyzer does not infer independent stem offsets.",
        "Spectral overlap and level margins are correlates of possible masking, not proof that masking is audible or undesirable.",
        "Vocal-active frames use a deterministic energy threshold and can include loud breaths, effects, bleed, or instrumental spill.",
        "The STFT trades time resolution for frequency resolution and does not model auditory masking curves, binaural localization, or artistic intent.",
        "No processing recommendation is applied and neither source file is modified.",
    ]


def analyze_masking(
    vocal: str,
    instrumental: str,
    max_seconds: float | None = 600,
) -> dict:
    """Measure possible vocal/instrument masking in synchronized renders.

    The result is actionable for later planning only when both files have the
    same sample rate and frame count and neither input was truncated. A failed
    synchronization gate returns provenance and reasons but no masking score.
    """
    vocal_loaded = load(vocal, max_seconds=max_seconds)
    instrumental_loaded = load(instrumental, max_seconds=max_seconds)
    vocal_provenance = _file_provenance(vocal_loaded)
    instrumental_provenance = _file_provenance(instrumental_loaded)
    reasons = []
    if vocal_loaded.rate != instrumental_loaded.rate:
        reasons.append("sample rates differ")
    if vocal_provenance["source_frames"] != instrumental_provenance["source_frames"]:
        reasons.append("source frame counts differ")
    if vocal_provenance["analyzed_frames"] != instrumental_provenance["analyzed_frames"]:
        reasons.append("analysed frame counts differ")
    if vocal_provenance["truncated"] or instrumental_provenance["truncated"]:
        reasons.append("at least one input was truncated")

    base = {
        "vocal": vocal_provenance,
        "instrumental": instrumental_provenance,
        "context_ready": not reasons,
        "readiness_reasons": reasons,
        "provenance": {
            "analyzer_version": ANALYZER_VERSION,
            "masking_analyzer_version": MASKING_ANALYSIS_VERSION,
            "parameters": {"max_seconds": max_seconds, "stft_window_frames": 2048},
        },
        "limitations": _masking_limitations(),
    }
    if reasons:
        return base

    rate = vocal_loaded.rate
    frequencies, times, vocal_power = _channel_power_spectrogram(
        _channel_data(vocal_loaded), rate
    )
    instrumental_frequencies, instrumental_times, instrumental_power = (
        _channel_power_spectrogram(_channel_data(instrumental_loaded), rate)
    )
    if not np.array_equal(frequencies, instrumental_frequencies) or not np.array_equal(
        times, instrumental_times
    ):
        base["context_ready"] = False
        base["readiness_reasons"] = ["STFT grids differ despite matching file metadata"]
        return base

    voice_band = (frequencies >= 80.0) & (frequencies < 12000.0)
    vocal_frame_power = np.sum(vocal_power[voice_band], axis=0)
    vocal_frame_db = 10.0 * np.log10(np.maximum(vocal_frame_power, 1e-20))
    activity_threshold_db = max(
        float(np.max(vocal_frame_db)) - 45.0,
        float(np.percentile(vocal_frame_db, 30)),
    )
    active = vocal_frame_db > activity_threshold_db
    if int(np.sum(active)) < 3:
        base["context_ready"] = False
        base["readiness_reasons"] = [
            "fewer than three vocal-active analysis frames were detected"
        ]
        return base

    epsilon = 1e-20

    def band_metrics(name: str, lo: int, hi: int) -> tuple[str, dict]:
        mask = (frequencies >= lo) & (frequencies < hi)
        vocal_band_power = np.sum(vocal_power[mask], axis=0)
        instrumental_band_power = np.sum(instrumental_power[mask], axis=0)
        margins = 10.0 * np.log10(
            (vocal_band_power[active] + epsilon)
            / (instrumental_band_power[active] + epsilon)
        )
        vocal_shape = np.mean(vocal_power[mask][:, active], axis=1)
        instrumental_shape = np.mean(instrumental_power[mask][:, active], axis=1)
        vocal_shape = vocal_shape / (float(np.sum(vocal_shape)) + epsilon)
        instrumental_shape = instrumental_shape / (
            float(np.sum(instrumental_shape)) + epsilon
        )
        overlap = float(np.sum(np.minimum(vocal_shape, instrumental_shape)))
        conflict_share = float(np.mean(margins <= 6.0))
        score = max(0.0, min(1.0, overlap * conflict_share))
        return name, {
            "range_hz": [lo, hi],
            "vocal_minus_instrument_median_db": round(float(np.median(margins)), 2),
            "vocal_minus_instrument_p10_db": round(float(np.percentile(margins, 10)), 2),
            "vocal_minus_instrument_p90_db": round(float(np.percentile(margins, 90)), 2),
            "instrument_within_6db_share": round(conflict_share, 4),
            "spectral_overlap": round(overlap, 4),
            "possible_masking_score": round(score, 4),
        }

    bands = dict(band_metrics(name, lo, hi) for name, lo, hi in BANDS)
    vocal_shape = np.mean(vocal_power[voice_band][:, active], axis=1)
    instrumental_shape = np.mean(instrumental_power[voice_band][:, active], axis=1)
    vocal_shape = vocal_shape / (float(np.sum(vocal_shape)) + epsilon)
    instrumental_shape = instrumental_shape / (
        float(np.sum(instrumental_shape)) + epsilon
    )
    overall_overlap = float(np.sum(np.minimum(vocal_shape, instrumental_shape)))
    vocal_total = np.sum(vocal_power[voice_band], axis=0)
    instrumental_total = np.sum(instrumental_power[voice_band], axis=0)
    overall_margins = 10.0 * np.log10(
        (vocal_total[active] + epsilon) / (instrumental_total[active] + epsilon)
    )
    overall_conflict = float(np.mean(overall_margins <= 6.0))
    overall_score = max(0.0, min(1.0, overall_overlap * overall_conflict))

    vocal_loudness = measure_loudness(vocal_loaded)
    instrumental_loudness = measure_loudness(instrumental_loaded)
    vocal_lufs = vocal_loudness.get("lufs_integrated")
    instrumental_lufs = instrumental_loudness.get("lufs_integrated")
    lufs_margin = (
        round(float(vocal_lufs - instrumental_lufs), 2)
        if vocal_lufs is not None and instrumental_lufs is not None
        else None
    )
    candidate_bands = [
        name
        for name, metrics in sorted(
            bands.items(),
            key=lambda item: item[1]["possible_masking_score"],
            reverse=True,
        )
        if metrics["possible_masking_score"] >= 0.35
    ]
    base.update(
        {
            "context_ready": True,
            "vocal_activity": {
                "active_frame_count": int(np.sum(active)),
                "total_frame_count": int(len(active)),
                "active_share": round(float(np.mean(active)), 4),
                "threshold_db_relative_to_stft_power": round(activity_threshold_db, 2),
            },
            "balance": {
                "vocal_lufs_integrated": vocal_lufs,
                "instrumental_lufs_integrated": instrumental_lufs,
                "vocal_minus_instrument_lu": lufs_margin,
                "vocal_minus_instrument_active_median_db": round(
                    float(np.median(overall_margins)), 2
                ),
            },
            "masking": {
                "frequency_scope_hz": [80, 12000],
                "spectral_overlap": round(overall_overlap, 4),
                "instrument_within_6db_share": round(overall_conflict, 4),
                "possible_masking_index": round(overall_score, 4),
                "candidate_bands": candidate_bands,
                "bands": bands,
            },
            "readings": [
                "Possible masking is reported as spectral overlap multiplied by the share of vocal-active frames where the instrumental is within 6 dB of the vocal.",
                "Candidate bands are diagnostic listening targets, not automatic EQ or ducking instructions.",
            ],
        }
    )
    return base
