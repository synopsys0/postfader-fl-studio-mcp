"""Production-copilot workflows for mix diagnosis, metering, and gain staging.

Audio judgements in this module are derived from PostFader's real decoded-file
measurements. Live peak watches use the documented mixer peak getters. Artistic
recommendations are labelled as policy recommendations and are never applied
here; a gain-staging proposal is applied by the caller through the same
verified batch kernel as direct edits.
"""

from __future__ import annotations

import math
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import ConfigDict, Field

from .advisory import (
    AudioComparison,
    AudioFileAnalysis,
    MaskingAnalysis,
    analyze_audio_file,
    analyze_masking,
    compare_audio_files,
)
from .readonly_inspector import IncompatibleFLStudio, ReadOnlyGateway, connection_from_ping
from .workflows import (
    MAX_BATCH_OPERATIONS,
    BatchMixerVolumeDb,
    BatchOperation,
    validate_batch_operations,
)
from .contracts import ContractModel, SCHEMA_VERSION


MIX_POLICY_VERSION = "postfader-mix-policy-1"
MAX_PEAK_WATCHES = 8
MAX_RUNNING_PEAK_WATCHES = 2


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dbfs(amplitude: float | None) -> float | None:
    if amplitude is None or amplitude <= 0.0:
        return None
    return round(20.0 * math.log10(amplitude), 3)


class MixingModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


# ---------------------------------------------------------------------------
# Mix Doctor and bounce-backed recommendations
# ---------------------------------------------------------------------------


MixTarget = Literal["dynamic", "balanced", "streaming", "club"]
IssueSeverity = Literal["info", "warning", "critical"]


class MixIssue(MixingModel):
    code: str = Field(min_length=1, max_length=64)
    severity: IssueSeverity
    measurement: str = Field(min_length=1, max_length=256)
    policy_threshold: str = Field(min_length=1, max_length=256)
    diagnosis: str = Field(min_length=1, max_length=512)
    recommendation: str = Field(min_length=1, max_length=512)
    confidence: Literal["high", "medium", "low"]


class MixDoctorReport(MixingModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    diagnosed_at: datetime
    policy_version: Literal["postfader-mix-policy-1"] = MIX_POLICY_VERSION
    target: MixTarget
    analysis: AudioFileAnalysis
    reference_comparison: AudioComparison | None = None
    masking_analysis: MaskingAnalysis | None = None
    issues: list[MixIssue]
    critical_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    technical_export_ready: bool
    mutations_applied: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)


_TARGETS: dict[MixTarget, tuple[float, float, float]] = {
    # lower LUFS, upper LUFS, maximum true peak
    "dynamic": (-18.0, -13.0, -1.0),
    "balanced": (-16.0, -11.0, -1.0),
    "streaming": (-16.0, -13.0, -1.0),
    "club": (-10.0, -7.0, -0.5),
}


def _issue(
    code: str,
    severity: IssueSeverity,
    measurement: str,
    threshold: str,
    diagnosis: str,
    recommendation: str,
    confidence: Literal["high", "medium", "low"] = "high",
) -> MixIssue:
    return MixIssue(
        code=code,
        severity=severity,
        measurement=measurement,
        policy_threshold=threshold,
        diagnosis=diagnosis,
        recommendation=recommendation,
        confidence=confidence,
    )


def _diagnose_analysis(analysis: AudioFileAnalysis, target: MixTarget) -> list[MixIssue]:
    issues: list[MixIssue] = []
    loud = analysis.loudness
    spec = analysis.spectrum
    dyn = analysis.dynamics
    stereo = analysis.stereo
    low_lufs, high_lufs, max_tp = _TARGETS[target]

    if loud.clipped_samples > 0:
        issues.append(_issue(
            "clipped_samples", "critical",
            f"{loud.clipped_samples} full-scale channel samples",
            "0 clipped samples",
            "The exported waveform already contains full-scale clipping.",
            "Lower the offending source/bus or limiter input, re-export, and measure the new file.",
        ))
    if loud.true_peak_dbtp is not None and loud.true_peak_dbtp > max_tp:
        severity: IssueSeverity = "critical" if loud.true_peak_dbtp >= -0.1 else "warning"
        issues.append(_issue(
            "true_peak_headroom", severity,
            f"{loud.true_peak_dbtp:.2f} dBTP",
            f"at or below {max_tp:.1f} dBTP for {target}",
            "The candidate has insufficient reconstructed-peak headroom.",
            "Reduce final-stage gain/ceiling and verify a fresh bounce; do not infer success from the live fader alone.",
        ))
    if loud.lufs_integrated is not None:
        if loud.lufs_integrated < low_lufs:
            issues.append(_issue(
                "integrated_loudness_low", "info",
                f"{loud.lufs_integrated:.2f} LUFS",
                f"{low_lufs:.1f}..{high_lufs:.1f} LUFS for {target}",
                "The measured candidate is quieter than this policy target.",
                "Decide whether the extra dynamics are intentional before adding gain or limiting.",
                "medium",
            ))
        elif loud.lufs_integrated > high_lufs:
            issues.append(_issue(
                "integrated_loudness_high", "warning",
                f"{loud.lufs_integrated:.2f} LUFS",
                f"{low_lufs:.1f}..{high_lufs:.1f} LUFS for {target}",
                "The measured candidate is louder than this policy target.",
                "Compare at matched loudness and consider reducing limiter drive if impact or clarity suffered.",
                "medium",
            ))
    if loud.crest_factor_db is not None and loud.crest_factor_db < 6.0:
        issues.append(_issue(
            "low_crest_factor", "warning",
            f"{loud.crest_factor_db:.2f} dB crest factor", "at least 6 dB",
            "Peak-to-body contrast is very small, consistent with heavy compression or limiting.",
            "Audit bus compression/limiting at matched loudness and restore transient margin if this was not deliberate.",
            "medium",
        ))
    elif loud.crest_factor_db is not None and loud.crest_factor_db > 20.0:
        issues.append(_issue(
            "high_crest_factor", "info",
            f"{loud.crest_factor_db:.2f} dB crest factor", "20 dB or less",
            "Peaks sit far above the body of the mix.",
            "Check isolated transients and automation before applying broad compression.",
            "medium",
        ))
    if abs(loud.dc_offset) > 0.001:
        issues.append(_issue(
            "dc_offset", "warning", f"{loud.dc_offset:.6f}",
            "absolute DC offset at or below 0.001",
            "The decoded bounce carries measurable DC offset.",
            "Find the source and use an appropriate high-pass/DC filter, then re-export.",
        ))
    if spec.sub_40hz_share > 0.02:
        issues.append(_issue(
            "sub_40_rumble", "warning", f"{spec.sub_40hz_share * 100:.2f}% below 40 Hz",
            "2% or less of spectral energy below 40 Hz",
            "Sub-40 Hz energy is consuming headroom and may be inaudible on many systems.",
            "Inspect kick/bass and low-frequency effects before applying a selective high-pass.",
            "medium",
        ))
    low_mid = spec.bands.get("low_mid")
    if low_mid is not None and low_mid.energy_share > 0.40:
        issues.append(_issue(
            "low_mid_density", "warning", f"{low_mid.energy_share * 100:.1f}% low-mid energy",
            "40% or less in the analyzer's low-mid band",
            "The bounce is unusually concentrated in the low-mid band.",
            "Use track/stem context to identify the source before cutting; a full-mix curve alone cannot assign blame.",
            "medium",
        ))
    if spec.sibilance_ratio > 0.25:
        issues.append(_issue(
            "sibilance_energy", "warning", f"ratio {spec.sibilance_ratio:.3f}",
            "sibilance ratio at or below 0.25",
            "High-frequency energy is concentrated in the analyzer's sibilance region.",
            "Confirm on the vocal/stem and de-ess dynamically near the measured peak rather than dulling the whole mix.",
            "medium",
        ))
    if dyn.noise_floor_db is not None and dyn.noise_floor_db > -50.0:
        issues.append(_issue(
            "noise_floor", "warning", f"{dyn.noise_floor_db:.2f} dBFS",
            "-50 dBFS or lower",
            "The measured quiet-section floor may be audible.",
            "Inspect room noise, tails, and gain staging; avoid gating solely from this full-mix statistic.",
            "medium",
        ))
    if dyn.dynamic_spread_db is not None and dyn.dynamic_spread_db > 18.0:
        issues.append(_issue(
            "dynamic_spread", "info", f"{dyn.dynamic_spread_db:.2f} dB",
            "18 dB or less under this review policy",
            "The bounce has large short-window level swings.",
            "Check automation and section-to-section balance before adding global compression.",
            "medium",
        ))
    if stereo.correlation is not None and stereo.correlation < 0.30:
        issues.append(_issue(
            "stereo_correlation", "warning", f"correlation {stereo.correlation:.3f}",
            "correlation at or above 0.30",
            "The stereo channels may cancel materially in mono.",
            "Check polarity, stereo widening, and ambience in mono before export.",
        ))
    return issues


def run_mix_doctor(
    candidate_path: str,
    *,
    target: MixTarget = "balanced",
    reference_path: str | None = None,
    vocal_path: str | None = None,
    instrumental_path: str | None = None,
    max_seconds: float | None = None,
) -> MixDoctorReport:
    if target not in _TARGETS:
        raise ValueError("target must be dynamic, balanced, streaming, or club")
    if (vocal_path is None) != (instrumental_path is None):
        raise ValueError("vocal_path and instrumental_path must be supplied together")
    analysis = analyze_audio_file(candidate_path, max_seconds=max_seconds)
    comparison = (
        compare_audio_files(reference_path, candidate_path, max_seconds=max_seconds)
        if reference_path is not None
        else None
    )
    masking = (
        analyze_masking(vocal_path, instrumental_path, max_seconds=max_seconds)
        if vocal_path is not None and instrumental_path is not None
        else None
    )
    issues = _diagnose_analysis(analysis, target)
    warnings = [
        "Thresholds are PostFader review policy, not universal mastering rules.",
        "This report analyses decoded files; FL's MIDI scripting API exposes no live audio bus.",
    ]
    if comparison is not None and not comparison.comparison_ready:
        warnings.append("Reference alignment/readiness failed; no automatic tonal target should be inferred.")
    if masking is not None and not masking.context_ready:
        warnings.append("Masking inputs were not proven synchronous; masking recommendations are withheld.")
    critical = sum(issue.severity == "critical" for issue in issues)
    warning_count = sum(issue.severity == "warning" for issue in issues)
    ready = bool(
        critical == 0
        and warning_count == 0
        and analysis.confidence.level != "low"
        and (comparison is None or comparison.comparison_ready)
        and (masking is None or masking.context_ready)
    )
    return MixDoctorReport(
        diagnosed_at=_now(),
        target=target,
        analysis=analysis,
        reference_comparison=comparison,
        masking_analysis=masking,
        issues=issues,
        critical_count=critical,
        warning_count=warning_count,
        technical_export_ready=ready,
        warnings=warnings,
    )


class ReferenceAdjustment(MixingModel):
    band: str = Field(min_length=1, max_length=64)
    measured_difference_db: float
    direction: Literal["reduce_candidate", "increase_candidate"]
    suggested_review_range_db: tuple[float, float]
    rationale: str = Field(min_length=1, max_length=512)


class ReferenceRecommendationReport(MixingModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    generated_at: datetime
    comparison: AudioComparison
    actionable: bool
    adjustments: list[ReferenceAdjustment]
    mutations_applied: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)


def reference_recommendations(
    reference_path: str,
    candidate_path: str,
    *,
    max_seconds: float | None = None,
) -> ReferenceRecommendationReport:
    comparison = compare_audio_files(reference_path, candidate_path, max_seconds=max_seconds)
    adjustments: list[ReferenceAdjustment] = []
    if comparison.comparison_ready:
        for band, delta in comparison.band_deltas.items():
            if abs(delta.difference_db) < 1.5:
                continue
            amount = min(3.0, abs(delta.difference_db))
            adjustments.append(ReferenceAdjustment(
                band=band,
                measured_difference_db=delta.difference_db,
                direction="reduce_candidate" if delta.difference_db > 0 else "increase_candidate",
                suggested_review_range_db=(round(max(0.5, amount * 0.5), 2), round(amount, 2)),
                rationale=(
                    "The loudness-matched, time-aligned candidate has "
                    f"{abs(delta.difference_db):.2f} dB "
                    f"{'more' if delta.difference_db > 0 else 'less'} energy in this band."
                ),
            ))
    warnings = [
        "These are bounded review ranges, not automatic EQ settings; references can differ by arrangement and genre.",
        "No source file, plug-in, or FL project state was changed.",
    ]
    if not comparison.comparison_ready:
        warnings.insert(0, "Comparison readiness failed, so tonal adjustments are withheld.")
    return ReferenceRecommendationReport(
        generated_at=_now(),
        comparison=comparison,
        actionable=comparison.comparison_ready,
        adjustments=adjustments,
        warnings=warnings,
    )


class MaskingRemediation(MixingModel):
    band: str = Field(min_length=1, max_length=64)
    possible_masking_score: float = Field(ge=0.0)
    suggested_instrument_reduction_db: tuple[float, float]
    preferred_method: Literal["dynamic_eq_or_automation"] = "dynamic_eq_or_automation"
    rationale: str = Field(min_length=1, max_length=512)


class MaskingRecommendationReport(MixingModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    generated_at: datetime
    analysis: MaskingAnalysis
    actionable: bool
    remediations: list[MaskingRemediation]
    mutations_applied: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)


def masking_recommendations(
    vocal_path: str,
    instrumental_path: str,
    *,
    max_seconds: float | None = None,
) -> MaskingRecommendationReport:
    analysis = analyze_masking(vocal_path, instrumental_path, max_seconds=max_seconds)
    remediations: list[MaskingRemediation] = []
    if analysis.context_ready and analysis.masking is not None:
        for band in analysis.masking.candidate_bands:
            metric = analysis.masking.bands[band]
            score = metric.possible_masking_score
            upper = round(min(4.0, max(1.0, score * 4.0)), 2)
            remediations.append(MaskingRemediation(
                band=band,
                possible_masking_score=score,
                suggested_instrument_reduction_db=(0.5, upper),
                rationale=(
                    f"Measured spectral overlap is {metric.spectral_overlap:.3f}; "
                    f"the instrument sits within 6 dB for {metric.instrument_within_6db_share:.1%} "
                    "of active vocal frames."
                ),
            ))
    warnings = [
        "Prefer dynamic EQ or automation on the instrumental source; a static full-mix cut can create a new tonal problem.",
        "No mutation is applied by this recommendation tool.",
    ]
    if not analysis.context_ready:
        warnings.insert(0, "The two renders were not proven synchronous, so remediation is withheld.")
    return MaskingRecommendationReport(
        generated_at=_now(),
        analysis=analysis,
        actionable=analysis.context_ready,
        remediations=remediations,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Lightweight persistent peak watch
# ---------------------------------------------------------------------------


class PeakFrameTrack(MixingModel):
    track_index: int = Field(ge=0)
    name: str = Field(max_length=256)
    fader_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    fader_db: float | None = None
    muted: bool | None = None
    peak_left: float | None = Field(default=None, ge=0.0)
    peak_right: float | None = Field(default=None, ge=0.0)
    peak_max: float | None = Field(default=None, ge=0.0)
    peak_dbfs: float | None = None


class PeakFrame(MixingModel):
    observed_at: datetime
    session_fingerprint: str = Field(pattern=r"^[0-9a-f]{32}$")
    observed_idle_tick: int = Field(ge=0)
    playing: bool | None = None
    song_position_normalized: float | None = None
    total_track_count: int = Field(ge=0)
    scanned_track_count: int = Field(ge=0)
    partial: bool
    tracks: list[PeakFrameTrack]


class PeakTrackAggregate(MixingModel):
    track_index: int = Field(ge=0)
    name: str = Field(max_length=256)
    sample_count: int = Field(ge=1)
    max_peak_linear: float = Field(ge=0.0)
    max_peak_dbfs: float | None = None
    clipping_frame_count: int = Field(ge=0)
    last_fader_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    last_fader_db: float | None = None
    last_muted: bool | None = None


class PeakWatchReport(MixingModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    watch_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: Literal["running", "completed", "stopped", "error"]
    started_at: datetime
    finished_at: datetime | None = None
    requested_duration_seconds: float = Field(ge=1.0, le=3600.0)
    interval_ms: int = Field(ge=250, le=5000)
    only_used: bool
    max_tracks: int = Field(ge=1, le=126)
    frame_count: int = Field(ge=0)
    session_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    tracks: list[PeakTrackAggregate]
    error: str | None = Field(default=None, max_length=2048)
    limitations: list[str] = Field(default_factory=list)


class _PeakReader:
    def __init__(self, gateway: ReadOnlyGateway | None = None):
        self.gateway = gateway or ReadOnlyGateway()

    def read(self, *, only_used: bool, max_tracks: int) -> PeakFrame:
        ping = self.gateway.ping()
        connection = connection_from_ping(ping, self.gateway.transport)
        if not connection.connected or not connection.compatible:
            raise IncompatibleFLStudio(connection.error or connection.compatibility_reason)
        session = connection.session_fingerprint
        if session is None:
            raise ValueError("FL bridge did not report a meter-watch session fingerprint")
        raw = self.gateway.call(
            "mixer.peaks", only_used=only_used, max_tracks=max_tracks
        )
        if raw.get("command") != "mixer.peaks":
            raise ValueError("FL bridge returned the wrong peak-frame command")
        if raw.get("session_fingerprint") != session:
            raise ValueError("FL bridge session changed during the peak frame")
        rows = raw.get("tracks")
        if not isinstance(rows, list):
            raise ValueError("FL bridge returned malformed peak rows")
        tracks: list[PeakFrameTrack] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("FL bridge returned a malformed peak row")
            index = row.get("track")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise ValueError("FL bridge returned a malformed peak track index")
            left = _finite_optional(row.get("peak_l"), "peak_l", low=0.0)
            right = _finite_optional(row.get("peak_r"), "peak_r", low=0.0)
            values = [value for value in (left, right) if value is not None]
            maximum = max(values) if values else None
            tracks.append(PeakFrameTrack(
                track_index=index,
                name=str(row.get("name") or "")[:256],
                fader_normalized=_finite_optional(row.get("volume"), "volume", low=0.0, high=1.0),
                fader_db=_finite_optional(row.get("volume_db"), "volume_db"),
                muted=row.get("muted") if type(row.get("muted")) is bool else None,
                peak_left=left,
                peak_right=right,
                peak_max=maximum,
                peak_dbfs=_dbfs(maximum),
            ))
        tick = raw.get("observed_idle_tick")
        total = raw.get("track_count")
        scanned = raw.get("scanned_track_count")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (tick, total, scanned)):
            raise ValueError("FL bridge returned malformed peak-frame counts")
        return PeakFrame(
            observed_at=_now(),
            session_fingerprint=session,
            observed_idle_tick=tick,
            playing=raw.get("playing") if type(raw.get("playing")) is bool else None,
            song_position_normalized=_finite_optional(
                raw.get("song_position"), "song_position"
            ),
            total_track_count=total,
            scanned_track_count=scanned,
            partial=bool(raw.get("partial")),
            tracks=tracks,
        )


def _finite_optional(
    value: Any,
    label: str,
    *,
    low: float | None = None,
    high: float | None = None,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    if low is not None and number < low or high is not None and number > high:
        raise ValueError(f"{label} is outside its contract")
    return number


class _PeakWatch:
    def __init__(self, duration: float, interval_ms: int, only_used: bool, max_tracks: int):
        self.watch_id = secrets.token_hex(16)
        self.duration = duration
        self.interval_ms = interval_ms
        self.only_used = only_used
        self.max_tracks = max_tracks
        self.started_at = _now()
        self.finished_at: datetime | None = None
        self.status: Literal["running", "completed", "stopped", "error"] = "running"
        self.error: str | None = None
        self.session: str | None = None
        self.frame_count = 0
        self._tracks: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reader = _PeakReader()
        self._thread: threading.Thread | None = None

    def _add(self, frame: PeakFrame) -> None:
        with self._lock:
            if self.session is None:
                self.session = frame.session_fingerprint
            elif self.session != frame.session_fingerprint:
                raise ValueError("FL bridge session changed during the peak watch")
            self.frame_count += 1
            for track in frame.tracks:
                state = self._tracks.setdefault(track.track_index, {
                    "name": track.name,
                    "sample_count": 0,
                    "max_peak": 0.0,
                    "clipping": 0,
                    "fader": None,
                    "fader_db": None,
                    "muted": None,
                })
                state["name"] = track.name
                state["sample_count"] += 1
                peak = track.peak_max or 0.0
                state["max_peak"] = max(state["max_peak"], peak)
                if peak >= 1.0:
                    state["clipping"] += 1
                state["fader"] = track.fader_normalized
                state["fader_db"] = track.fader_db
                state["muted"] = track.muted

    def start(self) -> None:
        # Refuse early if FL cannot provide the first frame; a watch ID that
        # never observed anything is not a successful start.
        self._add(self._reader.read(only_used=self.only_used, max_tracks=self.max_tracks))
        self._thread = threading.Thread(
            target=self._run,
            name="postfader-peak-watch-" + self.watch_id[:8],
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        deadline = time.monotonic() + self.duration
        try:
            while not self._stop.wait(self.interval_ms / 1000.0):
                if time.monotonic() >= deadline:
                    with self._lock:
                        self.status = "completed"
                        self.finished_at = _now()
                    return
                self._add(self._reader.read(only_used=self.only_used, max_tracks=self.max_tracks))
        except Exception as exc:
            with self._lock:
                self.status = "error"
                self.error = (f"{type(exc).__name__}: {exc}")[:2048]
                self.finished_at = _now()
            return
        with self._lock:
            if self.status == "running":
                self.status = "stopped"
                self.finished_at = _now()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        with self._lock:
            if self.status == "running":
                self.status = "stopped"
                self.finished_at = _now()

    def report(self) -> PeakWatchReport:
        with self._lock:
            tracks = [
                PeakTrackAggregate(
                    track_index=index,
                    name=state["name"],
                    sample_count=state["sample_count"],
                    max_peak_linear=state["max_peak"],
                    max_peak_dbfs=_dbfs(state["max_peak"]),
                    clipping_frame_count=state["clipping"],
                    last_fader_normalized=state["fader"],
                    last_fader_db=state["fader_db"],
                    last_muted=state["muted"],
                )
                for index, state in sorted(self._tracks.items())
            ]
            return PeakWatchReport(
                watch_id=self.watch_id,
                status=self.status,
                started_at=self.started_at,
                finished_at=self.finished_at,
                requested_duration_seconds=self.duration,
                interval_ms=self.interval_ms,
                only_used=self.only_used,
                max_tracks=self.max_tracks,
                frame_count=self.frame_count,
                session_fingerprint=self.session,
                tracks=tracks,
                error=self.error,
                limitations=[
                    "FL's mixer peak getter is sampled state, not an audio stream; transients between samples can be missed.",
                    "Peak values are post-routing live observations and are not LUFS, true peak, or a substitute for bounce analysis.",
                ],
            )


class PeakWatchRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._watches: dict[str, _PeakWatch] = {}

    def start(
        self,
        *,
        duration_seconds: float,
        interval_ms: int,
        only_used: bool,
        max_tracks: int,
    ) -> PeakWatchReport:
        if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)):
            raise ValueError("duration_seconds must be a number")
        duration = float(duration_seconds)
        if not 1.0 <= duration <= 3600.0:
            raise ValueError("duration_seconds must be within 1..3600")
        if isinstance(interval_ms, bool) or not isinstance(interval_ms, int) or not 250 <= interval_ms <= 5000:
            raise ValueError("interval_ms must be an integer within 250..5000")
        if type(only_used) is not bool:
            raise ValueError("only_used must be true or false")
        if isinstance(max_tracks, bool) or not isinstance(max_tracks, int) or not 1 <= max_tracks <= 126:
            raise ValueError("max_tracks must be an integer within 1..126")
        with self._lock:
            running = sum(watch.report().status == "running" for watch in self._watches.values())
            if running >= MAX_RUNNING_PEAK_WATCHES:
                raise ValueError("two peak watches are already running; stop one first")
            completed = [
                key for key, watch in self._watches.items()
                if watch.report().status != "running"
            ]
            while len(self._watches) >= MAX_PEAK_WATCHES and completed:
                del self._watches[completed.pop(0)]
        watch = _PeakWatch(duration, interval_ms, only_used, max_tracks)
        watch.start()
        with self._lock:
            self._watches[watch.watch_id] = watch
        return watch.report()

    def get(self, watch_id: str) -> PeakWatchReport:
        if not isinstance(watch_id, str) or len(watch_id) != 32:
            raise ValueError("watch_id must be 32 lowercase hexadecimal characters")
        with self._lock:
            watch = self._watches.get(watch_id)
        if watch is None:
            raise ValueError("unknown or expired peak watch ID")
        return watch.report()

    def stop(self, watch_id: str) -> PeakWatchReport:
        with self._lock:
            watch = self._watches.get(watch_id)
        if watch is None:
            raise ValueError("unknown or expired peak watch ID")
        watch.stop()
        return watch.report()


PEAK_WATCHES = PeakWatchRegistry()


# ---------------------------------------------------------------------------
# Gain staging
# ---------------------------------------------------------------------------


class GainStagePlan(MixingModel):
    """Proposed dB fader moves from one peak watch; nothing is applied."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    generated_at: datetime
    watch: PeakWatchReport
    target_peak_dbfs: float = Field(ge=-30.0, le=-3.0)
    session_fingerprint: str = Field(pattern=r"^[0-9a-f]{32}$")
    operations: list[BatchOperation] = Field(
        default_factory=list, max_length=MAX_BATCH_OPERATIONS
    )
    rationale: list[str] = Field(default_factory=list)
    skipped_tracks: list[str] = Field(default_factory=list)
    mutations_applied: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)


def create_gain_stage_plan(
    watch_id: str,
    *,
    target_peak_dbfs: float = -12.0,
    max_adjustment_db: float = 12.0,
    allow_master: bool = False,
) -> GainStagePlan:
    """Propose guarded dB fader moves toward a target peak; apply nothing.

    Each operation carries the watch's last fader reading as expected_before,
    so applying the same proposal twice, or after the user moved a fader, is
    refused by the bridge instead of moving the fader again.
    """

    target = _finite_optional(target_peak_dbfs, "target_peak_dbfs", low=-30.0, high=-3.0)
    adjustment_cap = _finite_optional(max_adjustment_db, "max_adjustment_db", low=0.5, high=24.0)
    assert target is not None and adjustment_cap is not None
    if type(allow_master) is not bool:
        raise ValueError("allow_master must be true or false")
    watch = PEAK_WATCHES.get(watch_id)
    if watch.frame_count < 1 or watch.session_fingerprint is None:
        raise ValueError("the peak watch has no usable frames")
    operations: list[BatchOperation] = []
    skipped: list[str] = []
    rationale: list[str] = []
    for track in watch.tracks:
        label = f"track {track.track_index} ({track.name})"
        if track.track_index == 0 and not allow_master:
            skipped.append(label + ": Master excluded")
            continue
        if track.last_muted:
            skipped.append(label + ": muted")
            continue
        if track.max_peak_dbfs is None or track.last_fader_db is None:
            skipped.append(label + ": no non-silent peak/fader dB observation")
            continue
        delta = max(-adjustment_cap, min(adjustment_cap, target - track.max_peak_dbfs))
        if abs(delta) < 0.5:
            skipped.append(label + ": already within 0.5 dB of target")
            continue
        desired = max(-60.0, min(6.0, track.last_fader_db + delta))
        operation = BatchMixerVolumeDb(
            operation_id=f"gain-{track.track_index}",
            track_index=track.track_index,
            volume_db=round(desired, 2),
            tolerance_db=0.15,
            allow_master=track.track_index == 0 and allow_master,
            expected_before={
                "volume_normalized": track.last_fader_normalized,
                "volume_db": track.last_fader_db,
            },
        )
        operations.append(operation)
        rationale.append(
            f"{label}: watched peak {track.max_peak_dbfs:.2f} dBFS; "
            f"move fader {delta:+.2f} dB toward {target:.2f} dBFS."
        )
    if len(operations) > MAX_BATCH_OPERATIONS:
        raise ValueError(
            f"{len(operations)} tracks need adjustment, more than one edit call "
            f"applies ({MAX_BATCH_OPERATIONS}); watch fewer tracks with only_used "
            "or max_tracks"
        )
    if operations:
        validate_batch_operations(operations)
    return GainStagePlan(
        generated_at=_now(),
        watch=watch,
        target_peak_dbfs=target,
        session_fingerprint=watch.session_fingerprint,
        operations=operations,
        rationale=rationale,
        skipped_tracks=skipped,
        warnings=[
            "Peak staging uses sampled post-fader peaks; review the operations, then "
            "apply them with project_apply_edits and this session_fingerprint.",
            "The dB fader write searches FL's live curve and verifies the getter; no normalized-curve guess is used.",
            "A fresh full-song watch and bounce analysis are required after application.",
        ],
    )
