"""Production-copilot workflows for diagnosis, metering, profiles, and plans.

Audio judgements in this module are derived from PostFader's real decoded-file
measurements. Live peak watches use the documented mixer peak getters. Artistic
recommendations are labelled as policy recommendations and are never applied by
the read tools; mutations cross the same verified batch kernel as direct tools.
"""

from __future__ import annotations

import math
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Literal, cast

from pydantic import ConfigDict, Field

from .advisory import (
    AudioComparison,
    AudioFileAnalysis,
    MaskingAnalysis,
    analyze_audio_file,
    analyze_masking,
    compare_audio_files,
)
from .bridge_client import get_client
from .readonly_inspector import IncompatibleFLStudio, ReadOnlyGateway, connection_from_ping
from .track_b_contracts import MixerEffectTarget, PluginTarget, TargetedPluginSummary
from .performance import TrackBInspector
from .workflows import (
    BatchMixerVolumeDb,
    BatchOperation,
    VerifiedBatchExecutor,
    VerifiedBatchResult,
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
# Plug-in profiles and processing intents
# ---------------------------------------------------------------------------


class PluginParameterProfile(MixingModel):
    role: str = Field(min_length=1, max_length=64)
    name_candidates: tuple[str, ...]
    display_unit: str = Field(min_length=1, max_length=32)
    preferred_write_tool: Literal[
        "fl_set_plugin_param_display", "fl_set_plugin_param_option", "fl_set_plugin_param"
    ]


class ProcessingRecipe(MixingModel):
    recipe_id: str = Field(min_length=1, max_length=64)
    intent: str = Field(min_length=1, max_length=64)
    parameter_roles: tuple[str, ...]
    guidance: str = Field(min_length=1, max_length=512)


class PluginAdapterProfile(MixingModel):
    profile_id: str = Field(min_length=1, max_length=64)
    plugin_names: tuple[str, ...]
    category: Literal["equalizer", "compressor", "limiter", "reverb", "delay", "multiband"]
    supported_intents: tuple[str, ...]
    parameters: tuple[PluginParameterProfile, ...]
    recipes: tuple[ProcessingRecipe, ...]
    provenance: Literal["bundled_postfader_profile"] = "bundled_postfader_profile"
    exact_version_required: Literal[False] = False
    warnings: tuple[str, ...] = ()


def _parameter(role: str, names: tuple[str, ...], unit: str, tool: str = "fl_set_plugin_param_display") -> PluginParameterProfile:
    return PluginParameterProfile(
        role=role,
        name_candidates=names,
        display_unit=unit,
        preferred_write_tool=cast(Any, tool),
    )


PLUGIN_PROFILES: tuple[PluginAdapterProfile, ...] = (
    PluginAdapterProfile(
        profile_id="fl-parametric-eq-2",
        plugin_names=("Fruity Parametric EQ 2",),
        category="equalizer",
        supported_intents=("reduce_mud", "tame_harshness", "add_presence", "add_air", "tighten_low_end"),
        parameters=(
            _parameter("band_gain", ("Band 1 level", "Band 2 level", "Band 3 level"), "dB"),
            _parameter("band_frequency", ("Band 1 freq", "Band 2 freq", "Band 3 freq"), "Hz"),
            _parameter("band_width", ("Band 1 width", "Band 2 width", "Band 3 width"), "percent"),
        ),
        recipes=(
            ProcessingRecipe(recipe_id="eq-mud-review", intent="reduce_mud", parameter_roles=("band_frequency", "band_gain", "band_width"), guidance="Sweep only to identify the source, then audition a 1-3 dB reduction rather than applying a blind full-mix cut."),
            ProcessingRecipe(recipe_id="eq-air-review", intent="add_air", parameter_roles=("band_frequency", "band_gain"), guidance="Review a broad 0.5-2 dB high-band lift against sibilance and true-peak measurements."),
        ),
    ),
    PluginAdapterProfile(
        profile_id="fl-fruity-compressor",
        plugin_names=("Fruity Compressor",),
        category="compressor",
        supported_intents=("control_dynamics", "add_punch", "level_vocal"),
        parameters=(
            _parameter("threshold", ("Threshold",), "dB"),
            _parameter("ratio", ("Ratio",), "ratio"),
            _parameter("attack", ("Attack",), "ms"),
            _parameter("release", ("Release",), "ms"),
            _parameter("makeup_gain", ("Gain",), "dB"),
        ),
        recipes=(
            ProcessingRecipe(recipe_id="compressor-control", intent="control_dynamics", parameter_roles=("threshold", "ratio", "attack", "release"), guidance="Set threshold from measured gain reduction; start around 2:1-4:1 and preserve transients with program-dependent attack/release."),
        ),
    ),
    PluginAdapterProfile(
        profile_id="fl-fruity-limiter",
        plugin_names=("Fruity Limiter",),
        category="limiter",
        supported_intents=("limit_peaks", "control_dynamics"),
        parameters=(
            _parameter("input_gain", ("GAIN", "Gain"), "dB"),
            _parameter("ceiling", ("CEIL", "Ceiling"), "dB"),
            _parameter("attack", ("ATT", "Attack"), "ms"),
            _parameter("release", ("REL", "Release"), "ms"),
        ),
        recipes=(
            ProcessingRecipe(recipe_id="limiter-headroom", intent="limit_peaks", parameter_roles=("ceiling", "input_gain"), guidance="Set ceiling from a new true-peak bounce; do not use live sample peaks as proof of codec headroom."),
        ),
    ),
    PluginAdapterProfile(
        profile_id="fl-reeverb-2",
        plugin_names=("Fruity Reeverb 2",),
        category="reverb",
        supported_intents=("add_depth", "shorten_space", "darken_reverb"),
        parameters=(
            _parameter("decay", ("Decay",), "seconds"),
            _parameter("wet", ("Wet",), "percent"),
            _parameter("dry", ("Dry",), "percent"),
            _parameter("high_cut", ("HighCut", "High cut"), "Hz"),
        ),
        recipes=(
            ProcessingRecipe(recipe_id="reverb-depth", intent="add_depth", parameter_roles=("decay", "wet", "high_cut"), guidance="Use a send when possible; raise wet amount in context and filter the return to preserve intelligibility."),
        ),
    ),
    PluginAdapterProfile(
        profile_id="fl-delay-3",
        plugin_names=("Fruity Delay 3",),
        category="delay",
        supported_intents=("add_depth", "rhythmic_echo"),
        parameters=(
            _parameter("feedback", ("Feedback",), "percent"),
            _parameter("time", ("Time",), "beats_or_ms"),
        ),
        recipes=(
            ProcessingRecipe(recipe_id="delay-depth", intent="add_depth", parameter_roles=("time", "feedback"), guidance="Choose a tempo-related time and automate/filter the return; verify that feedback remains bounded."),
        ),
    ),
)


class PluginProfileCatalog(MixingModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    profiles: tuple[PluginAdapterProfile, ...]
    profile_count: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)


def list_plugin_profiles(category: str | None = None) -> PluginProfileCatalog:
    if category is not None and category not in {profile.category for profile in PLUGIN_PROFILES}:
        raise ValueError("unknown plug-in profile category")
    profiles = tuple(
        profile for profile in PLUGIN_PROFILES
        if category is None or profile.category == category
    )
    return PluginProfileCatalog(
        profiles=profiles,
        profile_count=len(profiles),
        warnings=[
            "Profiles match reported plug-in names and parameter labels; FL exposes no exact plug-in version.",
            "Factory preset names/current preset are not invented because FL exposes no authoritative current-preset getter.",
        ],
    )


class LoadedPluginProfileMatch(MixingModel):
    plugin: TargetedPluginSummary
    profile_id: str | None = Field(default=None, max_length=64)
    compatibility: Literal["profiled", "unprofiled"]
    matched_reported_name: str | None = Field(default=None, max_length=256)


class PluginCompatibilityReport(MixingModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    observed_at: datetime
    matches: list[LoadedPluginProfileMatch]
    profiled_count: int = Field(ge=0)
    unprofiled_count: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)


def _profile_for_name(name: str) -> PluginAdapterProfile | None:
    folded = name.casefold().strip()
    for profile in PLUGIN_PROFILES:
        if any(folded == candidate.casefold() for candidate in profile.plugin_names):
            return profile
    return None


def inspect_plugin_compatibility(*, only_used: bool = True) -> PluginCompatibilityReport:
    inventory = TrackBInspector().scan_loaded_plugins(only_used=only_used)
    matches: list[LoadedPluginProfileMatch] = []
    for plugin in inventory.plugins:
        profile = _profile_for_name(plugin.name)
        matches.append(LoadedPluginProfileMatch(
            plugin=plugin,
            profile_id=None if profile is None else profile.profile_id,
            compatibility="unprofiled" if profile is None else "profiled",
            matched_reported_name=None if profile is None else plugin.name,
        ))
    profiled = sum(match.compatibility == "profiled" for match in matches)
    return PluginCompatibilityReport(
        observed_at=_now(),
        matches=matches,
        profiled_count=profiled,
        unprofiled_count=len(matches) - profiled,
        warnings=list(inventory.warnings) + [
            "A profile means PostFader knows parameter roles; it does not prove the plug-in version or audible result."
        ],
    )


ProcessingIntent = Literal[
    "reduce_mud", "tame_harshness", "add_presence", "add_air",
    "tighten_low_end", "control_dynamics", "add_punch", "level_vocal",
    "limit_peaks", "add_depth", "shorten_space", "rhythmic_echo",
]


class ProcessingIntentStep(MixingModel):
    order: int = Field(ge=1)
    goal: str = Field(min_length=1, max_length=512)
    category: str = Field(min_length=1, max_length=64)
    compatible_profile_ids: tuple[str, ...]
    loaded_targets: tuple[PluginTarget, ...]
    parameter_roles: tuple[str, ...]
    mutation_available: bool
    verification_basis: str = Field(min_length=1, max_length=256)


class ProcessingIntentResolution(MixingModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    resolved_at: datetime
    intent: ProcessingIntent
    track_index: int = Field(ge=0)
    strength: float = Field(ge=0.0, le=1.0)
    ready: bool
    steps: list[ProcessingIntentStep]
    missing_categories: tuple[str, ...]
    mutations_applied: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)


_INTENT_CATEGORIES: dict[str, tuple[str, ...]] = {
    "reduce_mud": ("equalizer",),
    "tame_harshness": ("equalizer",),
    "add_presence": ("equalizer",),
    "add_air": ("equalizer",),
    "tighten_low_end": ("equalizer", "compressor"),
    "control_dynamics": ("compressor",),
    "add_punch": ("compressor",),
    "level_vocal": ("compressor",),
    "limit_peaks": ("limiter",),
    "add_depth": ("reverb",),
    "shorten_space": ("reverb",),
    "rhythmic_echo": ("delay",),
}


def resolve_processing_intent(
    intent: ProcessingIntent,
    *,
    track_index: int,
    strength: float = 0.5,
) -> ProcessingIntentResolution:
    if intent not in _INTENT_CATEGORIES:
        raise ValueError("unsupported processing intent")
    if isinstance(track_index, bool) or not isinstance(track_index, int) or track_index < 0:
        raise ValueError("track_index must be a non-negative integer")
    if isinstance(strength, bool) or not isinstance(strength, (int, float)) or not 0.0 <= float(strength) <= 1.0:
        raise ValueError("strength must be within 0..1")
    inventory = TrackBInspector().scan_loaded_plugins(only_used=False)
    loaded: dict[str, list[PluginTarget]] = {}
    for plugin in inventory.plugins:
        if not isinstance(plugin.target, MixerEffectTarget) or plugin.target.track_index != track_index:
            continue
        profile = _profile_for_name(plugin.name)
        if profile is not None:
            loaded.setdefault(profile.category, []).append(plugin.target)
    steps: list[ProcessingIntentStep] = []
    missing: list[str] = []
    for order, category in enumerate(_INTENT_CATEGORIES[intent], start=1):
        profiles = tuple(
            profile for profile in PLUGIN_PROFILES
            if profile.category == category and intent in profile.supported_intents
        )
        targets = tuple(loaded.get(category, []))
        if not targets:
            missing.append(category)
        roles = tuple(dict.fromkeys(
            parameter.role for profile in profiles for parameter in profile.parameters
        ))
        steps.append(ProcessingIntentStep(
            order=order,
            goal=f"Use {category} processing for {intent.replace('_', ' ')} at reviewed strength {float(strength):.2f}.",
            category=category,
            compatible_profile_ids=tuple(profile.profile_id for profile in profiles),
            loaded_targets=targets,
            parameter_roles=roles,
            mutation_available=bool(targets),
            verification_basis="parameter display/value readback on later FL idle ticks",
        ))
    return ProcessingIntentResolution(
        resolved_at=_now(),
        intent=intent,
        track_index=track_index,
        strength=float(strength),
        ready=not missing,
        steps=steps,
        missing_categories=tuple(missing),
        warnings=list(inventory.warnings) + [
            "This resolves an artistic intent into profiled controls but does not choose thresholds from thin air or insert missing plug-ins.",
            "Use a distinct reviewed plan/application call for mutations.",
        ],
    )


# ---------------------------------------------------------------------------
# Reviewable plans, gain staging, and finish workflow
# ---------------------------------------------------------------------------


PlanStatus = Literal["draft", "applied", "partial", "failed"]


class MixPlan(MixingModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    plan_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    created_at: datetime
    title: str = Field(min_length=1, max_length=128)
    source: Literal["agent", "gain_stage"]
    status: PlanStatus = "draft"
    session_fingerprint: str = Field(pattern=r"^[0-9a-f]{32}$")
    operations: list[BatchOperation]
    rationale: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MixPlanApplication(MixingModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    applied_at: datetime
    plan: MixPlan
    batch: VerifiedBatchResult


class MixPlanRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._plans: dict[str, MixPlan] = {}
        # A plan remains claimed for its entire lifetime after the first apply
        # attempt.  Keeping this separate from the public status preserves the
        # draft/applied/partial contract while closing the check-then-dispatch
        # race between concurrent mix_apply_plan calls.
        self._attempted: set[str] = set()

    def create(
        self,
        *,
        title: str,
        operations: list[BatchOperation] | list[dict[str, Any]],
        rationale: list[str] | None = None,
        source: Literal["agent", "gain_stage"] = "agent",
        session_fingerprint: str | None = None,
    ) -> MixPlan:
        if not isinstance(title, str) or not title.strip() or len(title) > 128:
            raise ValueError("title must contain 1..128 characters")
        parsed = validate_batch_operations(operations)
        if rationale is None:
            rationale = []
        if not isinstance(rationale, list) or any(
            not isinstance(item, str) or not item or len(item) > 512 for item in rationale
        ):
            raise ValueError("rationale must be a list of non-empty strings up to 512 characters")
        client = get_client()
        ping = client.ping()
        connection = connection_from_ping(ping, getattr(client, "transport", "unknown"))
        if not connection.connected or not connection.compatible:
            raise IncompatibleFLStudio(connection.error or connection.compatibility_reason)
        session = connection.session_fingerprint
        if session is None:
            raise ValueError("FL bridge did not report a plan session fingerprint")
        if session_fingerprint is not None and session_fingerprint != session:
            raise ValueError("plan session precondition failed")
        plan = MixPlan(
            plan_id=secrets.token_hex(16),
            created_at=_now(),
            title=title.strip(),
            source=source,
            session_fingerprint=session,
            operations=parsed,
            rationale=rationale,
            warnings=[
                "A plan is reviewable and process-local; it mutates nothing until mix_apply_plan.",
                "Plans are session-bound and refuse after the FL bridge reloads.",
            ],
        )
        with self._lock:
            if len(self._plans) >= 128:
                evictable = [
                    item
                    for item in self._plans.values()
                    if not (
                        item.status == "draft" and item.plan_id in self._attempted
                    )
                ]
                if not evictable:
                    raise ValueError(
                        "mix plan registry is full while applications are in progress"
                    )
                oldest = min(evictable, key=lambda item: item.created_at)
                del self._plans[oldest.plan_id]
                self._attempted.discard(oldest.plan_id)
            self._plans[plan.plan_id] = plan
        return plan

    def get(self, plan_id: str) -> MixPlan:
        with self._lock:
            plan = self._plans.get(plan_id)
        if plan is None:
            raise ValueError("unknown or expired mix plan ID")
        return plan

    def apply(self, plan_id: str, *, stop_on_unverified: bool = True) -> MixPlanApplication:
        with self._lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                raise ValueError("unknown or expired mix plan ID")
            if plan.status != "draft" or plan_id in self._attempted:
                raise ValueError(
                    "this mix plan has already been attempted; create a fresh plan to reapply"
                )
            # Claim before dispatch.  The executor may perform live mutations
            # before returning an error, so a failed attempt must not become
            # eligible for a second, potentially duplicating application.
            self._attempted.add(plan_id)

        try:
            batch = VerifiedBatchExecutor().apply(
                operations=plan.operations,
                stop_on_unverified=stop_on_unverified,
                session_fingerprint=plan.session_fingerprint,
            )
            status: PlanStatus = "applied" if batch.verified else "partial"
            updated = plan.model_copy(update={"status": status})
            application = MixPlanApplication(
                applied_at=_now(), plan=updated, batch=batch
            )
        except Exception:
            # There is no safe way to infer that an exception occurred before
            # the first live mutation. Keep the public state terminal, but
            # distinguish a missing batch receipt from a returned partial one.
            with self._lock:
                current = self._plans.get(plan_id)
                if current is not None and current.status == "draft":
                    self._plans[plan_id] = current.model_copy(
                        update={
                            "status": "failed",
                            "warnings": [
                                *current.warnings,
                                "The apply attempt failed without a batch receipt; "
                                "its outcome may be unknown, so this plan cannot be retried.",
                            ],
                        }
                    )
            raise

        with self._lock:
            self._plans[plan_id] = updated
        return application


MIX_PLANS = MixPlanRegistry()


class GainStagePlanResult(MixingModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    generated_at: datetime
    watch: PeakWatchReport
    target_peak_dbfs: float = Field(ge=-30.0, le=-3.0)
    plan: MixPlan | None = None
    skipped_tracks: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def create_gain_stage_plan(
    watch_id: str,
    *,
    target_peak_dbfs: float = -12.0,
    max_adjustment_db: float = 12.0,
    allow_master: bool = False,
) -> GainStagePlanResult:
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
    plan = None
    if operations:
        plan = MIX_PLANS.create(
            title=f"Gain stage {len(operations)} mixer tracks",
            operations=operations,
            rationale=rationale,
            source="gain_stage",
            session_fingerprint=watch.session_fingerprint,
        )
    return GainStagePlanResult(
        generated_at=_now(),
        watch=watch,
        target_peak_dbfs=target,
        plan=plan,
        skipped_tracks=skipped,
        warnings=[
            "Peak staging uses sampled post-fader peaks; inspect the plan and apply it separately.",
            "The dB fader tool searches FL's live curve and verifies the getter; no normalized-curve guess is used.",
            "A fresh full-song watch and bounce analysis are required after application.",
        ],
    )


class FinishMixAssessment(MixingModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    assessed_at: datetime
    doctor: MixDoctorReport
    plugin_compatibility: PluginCompatibilityReport
    next_steps: list[str]
    stopping_boundary: Literal["user_exports_next_candidate"] = "user_exports_next_candidate"
    render_available_through_fl_api: Literal[False] = False
    mutations_applied: Literal[False] = False


def finish_mix_assessment(
    candidate_path: str,
    *,
    target: MixTarget = "balanced",
    reference_path: str | None = None,
    vocal_path: str | None = None,
    instrumental_path: str | None = None,
    max_seconds: float | None = None,
) -> FinishMixAssessment:
    doctor = run_mix_doctor(
        candidate_path,
        target=target,
        reference_path=reference_path,
        vocal_path=vocal_path,
        instrumental_path=instrumental_path,
        max_seconds=max_seconds,
    )
    compatibility = inspect_plugin_compatibility(only_used=True)
    steps = [
        "Review the measured issues and any ready reference/masking evidence.",
        "Resolve relevant processing intents against the loaded profiled plug-ins.",
        "Create and review a verified mix plan; apply it only through mix_apply_plan.",
        "Export a new candidate manually from FL Studio, then rerun Mix Doctor/reference comparison.",
    ]
    if doctor.technical_export_ready:
        steps = [
            "The candidate passes this technical policy; perform the final artistic/listening review.",
            "Export/finalize manually because FL's scripting API cannot render the project.",
        ]
    return FinishMixAssessment(
        assessed_at=_now(),
        doctor=doctor,
        plugin_compatibility=compatibility,
        next_steps=steps,
    )
