"""Read-only audio measurement surface for mix advice.

The FL Studio MIDI scripting API exposes control surfaces but no audio, so any
question about how a bounce actually sounds has to be answered from a rendered
file on disk.  ``fl_studio_mcp.audio`` already implements that measurement
engine and carries its own deterministic suite.  This module is the thin,
typed, fail-closed boundary that makes the engine reachable from an MCP client
and pairs it with the read-only mixer inspection tools.

It implements no DSP of its own, invents no thresholds, and emits no
prescriptive advice.  The engine's free-text ``readings`` are deliberately not
forwarded for that last reason: what is returned is measurements plus the
engine's own confidence and limitation fields, and the agent does the
interpreting.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from . import audio


ADVISORY_SCHEMA_VERSION = "1.0"

# Container formats the bundled libsndfile can decode.  An unrecognised suffix
# is refused before the file is opened rather than after a decoder failure.
AUDIO_SUFFIXES = frozenset(
    {".wav", ".wave", ".w64", ".rf64", ".aif", ".aiff", ".aifc", ".flac",
     ".ogg", ".caf", ".mp3"}
)

# A full-length 24-bit stereo master is roughly 170 MB; this cap accepts a long
# render while refusing an accidental multi-gigabyte read.
MAX_AUDIO_FILE_BYTES = 512 * 1024 * 1024

# The measurement engine's own default read bound, restated so the tool layer
# never silently analyses more audio than it declares.
DEFAULT_MAX_SECONDS = 600.0

# Discovery bounds.  This is a fixed-root lookup, not a filesystem search: the
# roots are not caller-controlled and the walk is bounded in depth and work.
MAX_DISCOVERY_DEPTH = 4
MAX_DISCOVERY_ENTRIES = 20_000

def _fl_studio_user_root() -> Path:
    """FL Studio's user-data folder, honouring the same override as the installer.

    install.sh and doctor.py both read FL_STUDIO_USER_DATA_DIR. Discovery has to
    as well: on a host whose FL data folder has been moved, hardcoding the
    default means recent-bounce discovery quietly searches a directory that does
    not exist and reports nothing, with no indication that it looked in the
    wrong place. The roots stay fixed at import and remain not caller-controlled
    -- an agent cannot point this at an arbitrary directory by passing a tool
    argument.
    """
    override = os.environ.get("FL_STUDIO_USER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Documents" / "Image-Line" / "FL Studio"


FL_STUDIO_USER_ROOT = _fl_studio_user_root()
REPO_ROOT = Path(__file__).resolve().parents[1]

# FL writes bounces to Audio/Rendered, recorded takes to Audio/Recorded, and
# per-project audio under the project folders.
DEFAULT_DISCOVERY_ROOTS: tuple[Path, ...] = (
    FL_STUDIO_USER_ROOT / "Audio" / "Rendered",
    FL_STUDIO_USER_ROOT / "Audio",
    FL_STUDIO_USER_ROOT / "Projects",
)

# A symlinked input may only resolve inside the locations this connector is
# meant to read from.
ALLOWED_SYMLINK_ROOTS: tuple[Path, ...] = (FL_STUDIO_USER_ROOT, REPO_ROOT)

DISCOVERY_LIMITATIONS: tuple[str, ...] = (
    "Only FL Studio's usual output and project folders are searched; the roots are fixed and not caller-controlled.",
    "The walk is depth- and work-bounded, skips hidden entries, and never follows directory symlinks, so a deeply nested or very large tree can be reported as truncated.",
    "Entries are ranked by filesystem modification time, which is not proof of render order or of which take is musically current.",
    "No file is opened or decoded during discovery; size and timestamp come from the directory entry only.",
)


class AdvisoryError(RuntimeError):
    """A request was refused before any audio was read."""


class AdvisoryModel(BaseModel):
    """Strict, immutable base for every value returned to an MCP client."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


# ---------------------------------------------------------------------------
# measurement models
#
# Field names mirror the engine's own keys.  Sub-models are constructed with
# ``**payload`` so a new or renamed engine key fails closed here instead of
# being dropped silently on the way to the agent.
# ---------------------------------------------------------------------------


class AudioFileDescription(AdvisoryModel):
    path: str
    duration_sec: float
    sample_rate: int
    channels: int
    sha256: str
    format: str
    subtype: str | None = None
    source_frames: int
    analyzed_frames: int
    source_duration_sec: float
    truncated: bool
    # Which bound stopped the read, so a partial measurement is never mistaken
    # for a whole-file one. "max_seconds" is the caller's own limit;
    # "decode_limit" is the ceiling on how much audio may be held as samples,
    # which a high sample rate or channel count can reach on a file that is
    # well under the on-disk size limit.
    truncated_by: Literal["max_seconds", "decode_limit"] | None = None
    max_seconds: float | None = None


class InputProvenance(AdvisoryModel):
    canonical_path: str
    sha256: str
    sample_rate_hz: int
    channels: int
    source_frames: int
    analyzed_frames: int
    source_duration_sec: float
    analyzed_duration_sec: float
    truncated: bool


class LoudnessMeasurements(AdvisoryModel):
    lufs_integrated: float | None = None
    lufs_short_term_min: float | None = None
    lufs_short_term_max: float | None = None
    loudness_range_lu: float | None = None
    sample_peak_db: float | None = None
    sample_peak_db_per_channel: tuple[float | None, ...] = ()
    true_peak_dbtp: float | None = None
    true_peak_dbtp_per_channel: tuple[float | None, ...] = ()
    true_peak_method: str
    rms_db: float | None = None
    rms_db_per_channel: tuple[float | None, ...] = ()
    crest_factor_db: float | None = None
    clipped_samples: int
    clipped_samples_per_channel: tuple[int, ...] = ()
    clipped_frames: int
    dc_offset: float
    dc_offset_per_channel: tuple[float, ...] = ()
    channel_count: int
    headroom_to_0dbtp: float | None = None


class SpectralBand(AdvisoryModel):
    range_hz: tuple[int, int]
    energy_share: float
    level_db: float | None = None


class SpectrumMeasurements(AdvisoryModel):
    bands: dict[str, SpectralBand]
    spectral_centroid_hz: float
    sibilance_ratio: float
    sibilance_peak_hz: float | None = None
    sub_40hz_share: float
    dominant_hz: float


class DynamicsMeasurements(AdvisoryModel):
    rms_p10_db: float | None = None
    rms_median_db: float | None = None
    rms_p90_db: float | None = None
    rms_p95_db: float | None = None
    dynamic_spread_db: float | None = None
    noise_floor_db: float | None = None
    silence_share: float | None = None
    # The engine substitutes a note for the numbers when a clip is too short.
    note: str | None = None


class StereoMeasurements(AdvisoryModel):
    channels: int
    correlation: float | None = None
    mid_side_ratio: float | None = None
    mono_compatible: bool | None = None
    balance_db: float | None = None
    note: str | None = None


class PitchMeasurements(AdvisoryModel):
    voiced_share: float | None = None
    median_hz: float | None = None
    median_note: str | None = None
    median_cents_off: float | None = None
    range_low_note: str | None = None
    range_high_note: str | None = None
    cents_off_mean_abs: float | None = None
    cents_off_p90: float | None = None
    in_tune_share_within_20c: float | None = None
    in_tune_share_within_50c: float | None = None
    pitch_jitter_cents: float | None = None
    note: str | None = None


class MeasurementConfidence(AdvisoryModel):
    """The engine's own confidence, forwarded rather than recomputed."""

    level: Literal["high", "medium", "low"]
    score: float = Field(ge=0.0, le=1.0)
    basis: tuple[str, ...]
    scope: str | None = None


class AlignmentConfidence(AdvisoryModel):
    """Alignment confidence states its basis as one sentence, not a list."""

    level: Literal["high", "medium", "low"]
    score: float = Field(ge=0.0, le=1.0)
    basis: str


class AnalyzerVersions(AdvisoryModel):
    true_peak: str | None = None
    integrated_loudness: str | None = None
    spectrum: str | None = None
    dynamics: str | None = None
    pitch: str | None = None
    alignment: str | None = None
    loudness_matching: str | None = None


class AudioFileAnalysis(AdvisoryModel):
    """Every measurement the engine produces for one rendered file."""

    schema_version: Literal["1.0"] = ADVISORY_SCHEMA_VERSION
    measured_at: datetime
    analyzer_version: str
    analyzer_versions: AnalyzerVersions
    file: AudioFileDescription
    loudness: LoudnessMeasurements
    spectrum: SpectrumMeasurements
    dynamics: DynamicsMeasurements
    stereo: StereoMeasurements
    pitch: PitchMeasurements | None = None
    confidence: MeasurementConfidence
    limitations: tuple[str, ...]
    interpretation: Literal["measurements_only"] = "measurements_only"


class BandDelta(AdvisoryModel):
    reference_share: float
    target_share: float
    difference_db: float


class CentroidComparison(AdvisoryModel):
    reference: float
    target: float


class AlignmentReport(AdvisoryModel):
    method: str
    lag_convention: str
    analysis_rate_hz: int
    lag_resolution_seconds: float
    max_lag_seconds: float
    target_lag_samples_at_reference_rate: int
    target_lag_seconds: float
    normalised_correlation: float
    absolute_correlation: float
    peak_to_next_distinct_peak_ratio: float
    reference_alignment_source: str
    target_alignment_source: str
    common_frames: int
    common_duration_sec: float
    common_coverage: float
    reference_common_start_sample: int
    reference_common_end_sample_exclusive: int
    target_common_start_sample_at_reference_rate: int
    target_common_end_sample_exclusive_at_reference_rate: int
    confidence: AlignmentConfidence


class LoudnessMatchReport(AdvisoryModel):
    method: str
    applied: bool
    in_memory_only: Literal[True]
    source_files_modified: Literal[False]
    reference_lufs: float | None = None
    target_lufs_before: float | None = None
    gain_db_applied: float | None = None
    target_lufs_after: float | None = None
    residual_lu: float | None = None
    target_sample_peak_db_before: float | None = None
    target_sample_peak_db_after: float | None = None
    clipped_channel_samples_after: int
    reason_not_applied: str | None = None


class RateConversion(AdvisoryModel):
    from_hz: int
    to_hz: int
    method: str


class ComparisonParameters(AdvisoryModel):
    max_seconds: float | None = None
    max_alignment_seconds: float
    alignment_rate_hz: int


class AudioComparison(AdvisoryModel):
    """A/B measurements over the common, loudness-matched overlap.

    ``target`` throughout is the candidate file supplied by the caller; the
    name is the measurement engine's, kept verbatim so the alignment lag
    convention reads consistently.
    """

    schema_version: Literal["1.0"] = ADVISORY_SCHEMA_VERSION
    measured_at: datetime
    analyzer_version: str
    analyzer_versions: AnalyzerVersions
    reference: InputProvenance
    target: InputProvenance
    target_rate_conversion: RateConversion | None = None
    parameters: ComparisonParameters
    band_deltas: dict[str, BandDelta]
    centroid_hz: CentroidComparison
    alignment: AlignmentReport
    loudness_matching: LoudnessMatchReport
    comparison_ready: bool
    confidence: MeasurementConfidence
    limitations: tuple[str, ...]
    interpretation: Literal["measurements_only"] = "measurements_only"


class VocalActivity(AdvisoryModel):
    active_frame_count: int
    total_frame_count: int
    active_share: float
    threshold_db_relative_to_stft_power: float


class MaskingBalance(AdvisoryModel):
    vocal_lufs_integrated: float | None = None
    instrumental_lufs_integrated: float | None = None
    vocal_minus_instrument_lu: float | None = None
    vocal_minus_instrument_active_median_db: float


class MaskingBand(AdvisoryModel):
    range_hz: tuple[int, int]
    vocal_minus_instrument_median_db: float
    vocal_minus_instrument_p10_db: float
    vocal_minus_instrument_p90_db: float
    instrument_within_6db_share: float
    spectral_overlap: float
    possible_masking_score: float


class MaskingSummary(AdvisoryModel):
    frequency_scope_hz: tuple[int, int]
    spectral_overlap: float
    instrument_within_6db_share: float
    possible_masking_index: float
    candidate_bands: tuple[str, ...]
    bands: dict[str, MaskingBand]


class MaskingParameters(AdvisoryModel):
    max_seconds: float | None = None
    stft_window_frames: int


class MaskingAnalysis(AdvisoryModel):
    """Overlap and level margins between a vocal and an instrumental render.

    ``context_ready`` is false, and the measurement blocks are absent, whenever
    the two files are not sample-synchronous renders of the same section.
    """

    schema_version: Literal["1.0"] = ADVISORY_SCHEMA_VERSION
    measured_at: datetime
    analyzer_version: str
    masking_analyzer_version: str
    parameters: MaskingParameters
    vocal: InputProvenance
    instrumental: InputProvenance
    context_ready: bool
    readiness_reasons: tuple[str, ...]
    vocal_activity: VocalActivity | None = None
    balance: MaskingBalance | None = None
    masking: MaskingSummary | None = None
    limitations: tuple[str, ...]
    interpretation: Literal["measurements_only"] = "measurements_only"


class DiscoveredAudioFile(AdvisoryModel):
    path: str
    root: str
    size_bytes: int
    modified_at: datetime
    suffix: str


class DiscoveryRootStatus(AdvisoryModel):
    path: str
    exists: bool
    matched_file_count: int
    scan_truncated: bool


class RecentAudioListing(AdvisoryModel):
    """Newest audio files in FL Studio's usual output and project folders."""

    schema_version: Literal["1.0"] = ADVISORY_SCHEMA_VERSION
    searched_at: datetime
    limit: int
    roots: tuple[DiscoveryRootStatus, ...]
    files: tuple[DiscoveredAudioFile, ...]
    matched_file_count: int
    returned_file_count: int
    scan_truncated: bool
    limitations: tuple[str, ...]


# ---------------------------------------------------------------------------
# path rules
# ---------------------------------------------------------------------------


def _within_allowed_roots(resolved: Path) -> bool:
    for root in ALLOWED_SYMLINK_ROOTS:
        real_root = Path(os.path.realpath(root))
        if resolved == real_root or resolved.is_relative_to(real_root):
            return True
    return False


def resolve_audio_path(path: str, *, label: str = "path") -> Path:
    """Apply every path rule and return the file the engine should read.

    The rules are deliberately narrow rather than exhaustive: this is a local
    connector reading the owner's own bounces, so an ordinary absolute path
    anywhere on disk is allowed, while the shapes that turn a read tool into a
    traversal primitive are not.
    """
    if not isinstance(path, str) or not path.strip():
        raise AdvisoryError(f"{label} must be a non-empty string")
    if "\x00" in path:
        raise AdvisoryError(f"{label} must not contain a null byte")

    candidate = Path(path)
    if not candidate.is_absolute():
        raise AdvisoryError(
            f"{label} must be an absolute path; {path!r} is relative and is not expanded"
        )
    if ".." in candidate.parts:
        raise AdvisoryError(f"{label} must not contain a '..' component")

    # Only the final component is symlink-gated.  Parent components are not,
    # because ordinary macOS locations (/var, /tmp) are themselves symlinks and
    # gating them would refuse legitimate absolute paths.
    if candidate.is_symlink():
        resolved = Path(os.path.realpath(candidate))
        if not _within_allowed_roots(resolved):
            raise AdvisoryError(
                f"{label} is a symlink resolving to {resolved} outside the allowed "
                "FL Studio and repository roots"
            )
    else:
        resolved = Path(os.path.realpath(candidate))

    if not os.path.lexists(candidate):
        raise AdvisoryError(f"{label} does not exist: {path}")
    if resolved.is_dir():
        raise AdvisoryError(f"{label} is a directory, not an audio file: {path}")
    if not resolved.is_file():
        raise AdvisoryError(f"{label} is not a regular file: {path}")

    suffix = candidate.suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        raise AdvisoryError(
            f"{label} does not have a readable audio extension "
            f"({suffix or 'no suffix'}); expected one of "
            f"{', '.join(sorted(AUDIO_SUFFIXES))}"
        )

    try:
        size_bytes = resolved.stat().st_size
    except OSError as exc:
        raise AdvisoryError(f"could not stat {label}: {exc}") from exc
    if size_bytes <= 0:
        raise AdvisoryError(f"{label} is empty: {path}")
    if size_bytes > MAX_AUDIO_FILE_BYTES:
        raise AdvisoryError(
            f"{label} is {size_bytes} bytes, over the {MAX_AUDIO_FILE_BYTES} byte "
            "analysis cap"
        )
    return resolved


def _max_seconds(value: float | None) -> float:
    if value is None:
        return DEFAULT_MAX_SECONDS
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise AdvisoryError("max_seconds must be a number")
    if not 1.0 <= float(value) <= DEFAULT_MAX_SECONDS:
        raise AdvisoryError(
            f"max_seconds must be between 1 and {int(DEFAULT_MAX_SECONDS)} seconds"
        )
    return float(value)


# ---------------------------------------------------------------------------
# engine adaptation
# ---------------------------------------------------------------------------


def _require_known_keys(
    payload: Mapping[str, object], expected: Iterable[str], label: str
) -> None:
    """Fail closed when the engine grows a section this layer would drop."""
    unexpected = sorted(set(payload) - set(expected))
    if unexpected:
        raise AdvisoryError(
            f"the audio engine returned unmodelled {label} sections: "
            f"{', '.join(unexpected)}"
        )


def _measure(callable_, *args, **kwargs):
    try:
        return callable_(*args, **kwargs)
    except audio.AudioError as exc:
        raise AdvisoryError(str(exc)) from exc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def analyze_audio_file(
    path: str,
    *,
    include_pitch: bool = False,
    max_seconds: float | None = None,
) -> AudioFileAnalysis:
    """Measure one rendered file: level, spectrum, dynamics, stereo, pitch."""
    resolved = resolve_audio_path(path, label="path")
    seconds = _max_seconds(max_seconds)
    payload = _measure(
        audio.analyze,
        os.fspath(resolved),
        include_pitch=bool(include_pitch),
        max_seconds=seconds,
    )
    _require_known_keys(
        payload,
        (
            "file", "loudness", "spectrum", "dynamics", "stereo", "pitch",
            "target_lufs", "provenance", "confidence", "limitations", "readings",
        ),
        "analysis",
    )
    provenance = payload["provenance"]
    pitch = payload.get("pitch")
    return AudioFileAnalysis(
        measured_at=_now(),
        analyzer_version=provenance["analyzer_version"],
        analyzer_versions=AnalyzerVersions(**provenance["analyzer_versions"]),
        file=AudioFileDescription(**payload["file"]),
        loudness=LoudnessMeasurements(**payload["loudness"]),
        spectrum=SpectrumMeasurements(**payload["spectrum"]),
        dynamics=DynamicsMeasurements(**payload["dynamics"]),
        stereo=StereoMeasurements(**payload["stereo"]),
        pitch=PitchMeasurements(**pitch) if pitch is not None else None,
        confidence=MeasurementConfidence(**payload["confidence"]),
        limitations=tuple(payload["limitations"]),
    )


def compare_audio_files(
    reference_path: str,
    candidate_path: str,
    *,
    max_seconds: float | None = None,
) -> AudioComparison:
    """Compare a candidate render against a reference over their overlap."""
    reference = resolve_audio_path(reference_path, label="reference_path")
    candidate = resolve_audio_path(candidate_path, label="candidate_path")
    seconds = _max_seconds(max_seconds)
    payload = _measure(
        audio.compare,
        os.fspath(reference),
        os.fspath(candidate),
        max_seconds=seconds,
    )
    _require_known_keys(
        payload,
        (
            "reference", "target", "band_deltas", "centroid_hz", "alignment",
            "loudness_matching", "comparison_ready", "confidence", "provenance",
            "limitations", "readings",
        ),
        "comparison",
    )
    provenance = payload["provenance"]
    conversion = provenance["target_rate_conversion"]
    return AudioComparison(
        measured_at=_now(),
        analyzer_version=provenance["analyzer_version"],
        analyzer_versions=AnalyzerVersions(**provenance["analyzer_versions"]),
        reference=InputProvenance(**provenance["reference"]),
        target=InputProvenance(**provenance["target"]),
        target_rate_conversion=(
            RateConversion(**conversion) if conversion is not None else None
        ),
        parameters=ComparisonParameters(**provenance["parameters"]),
        band_deltas={
            name: BandDelta(**values)
            for name, values in payload["band_deltas"].items()
        },
        centroid_hz=CentroidComparison(**payload["centroid_hz"]),
        alignment=AlignmentReport(**payload["alignment"]),
        loudness_matching=LoudnessMatchReport(**payload["loudness_matching"]),
        comparison_ready=bool(payload["comparison_ready"]),
        confidence=MeasurementConfidence(**payload["confidence"]),
        limitations=tuple(payload["limitations"]),
    )


def analyze_masking(
    vocal_path: str,
    instrument_path: str,
    *,
    max_seconds: float | None = None,
) -> MaskingAnalysis:
    """Measure spectral overlap and level margins between two synced renders."""
    vocal = resolve_audio_path(vocal_path, label="vocal_path")
    instrument = resolve_audio_path(instrument_path, label="instrument_path")
    seconds = _max_seconds(max_seconds)
    payload = _measure(
        audio.analyze_masking,
        os.fspath(vocal),
        os.fspath(instrument),
        max_seconds=seconds,
    )
    _require_known_keys(
        payload,
        (
            "vocal", "instrumental", "context_ready", "readiness_reasons",
            "vocal_activity", "balance", "masking", "provenance", "limitations",
            "readings",
        ),
        "masking",
    )
    provenance = payload["provenance"]
    activity = payload.get("vocal_activity")
    balance = payload.get("balance")
    masking = payload.get("masking")
    summary = None
    if masking is not None:
        summary = MaskingSummary(
            frequency_scope_hz=tuple(masking["frequency_scope_hz"]),
            spectral_overlap=masking["spectral_overlap"],
            instrument_within_6db_share=masking["instrument_within_6db_share"],
            possible_masking_index=masking["possible_masking_index"],
            candidate_bands=tuple(masking["candidate_bands"]),
            bands={
                name: MaskingBand(**values)
                for name, values in masking["bands"].items()
            },
        )
    return MaskingAnalysis(
        measured_at=_now(),
        analyzer_version=provenance["analyzer_version"],
        masking_analyzer_version=provenance["masking_analyzer_version"],
        parameters=MaskingParameters(**provenance["parameters"]),
        vocal=InputProvenance(**payload["vocal"]),
        instrumental=InputProvenance(**payload["instrumental"]),
        context_ready=bool(payload["context_ready"]),
        readiness_reasons=tuple(payload["readiness_reasons"]),
        vocal_activity=VocalActivity(**activity) if activity is not None else None,
        balance=MaskingBalance(**balance) if balance is not None else None,
        masking=summary,
        limitations=tuple(payload["limitations"]),
    )


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def _scan_root(root: Path, budget: int) -> tuple[list[tuple[Path, os.stat_result]], int, bool]:
    """Collect audio files under one root without following directory links."""
    found: list[tuple[Path, os.stat_result]] = []
    truncated = False
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if budget <= 0:
                truncated = True
                return found, budget, truncated
            budget -= 1
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if depth + 1 <= MAX_DISCOVERY_DEPTH:
                        stack.append((Path(entry.path), depth + 1))
                    else:
                        truncated = True
                    continue
                if not entry.is_file():
                    continue
                if os.path.splitext(entry.name)[1].lower() not in AUDIO_SUFFIXES:
                    continue
                found.append((Path(entry.path), entry.stat()))
            except OSError:
                continue
    return found, budget, truncated


def find_recent_audio_files(
    limit: int = 20,
    *,
    roots: Sequence[Path] | None = None,
) -> RecentAudioListing:
    """List the newest bounces under the fixed FL Studio roots.

    ``roots`` exists so deterministic tests can point the walk at a controlled
    tree.  It is not reachable from the MCP surface: an agent cannot choose
    where this connector reads.
    """
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise AdvisoryError("limit must be an integer")
    if not 1 <= limit <= 200:
        raise AdvisoryError("limit must be between 1 and 200")

    search_roots = tuple(roots) if roots is not None else DEFAULT_DISCOVERY_ROOTS
    budget = MAX_DISCOVERY_ENTRIES
    scan_truncated = False
    statuses: list[DiscoveryRootStatus] = []
    seen: set[str] = set()
    collected: list[tuple[float, str, DiscoveredAudioFile]] = []

    for root in search_roots:
        root = Path(root)
        exists = root.is_dir()
        matched = 0
        root_truncated = False
        if exists:
            found, budget, root_truncated = _scan_root(root, budget)
            for file_path, stat_result in found:
                key = os.path.realpath(file_path)
                if key in seen:
                    continue
                seen.add(key)
                matched += 1
                collected.append(
                    (
                        stat_result.st_mtime,
                        os.fspath(file_path),
                        DiscoveredAudioFile(
                            path=os.fspath(file_path),
                            root=os.fspath(root),
                            size_bytes=int(stat_result.st_size),
                            modified_at=datetime.fromtimestamp(
                                stat_result.st_mtime, tz=timezone.utc
                            ),
                            suffix=os.path.splitext(file_path.name)[1].lower(),
                        ),
                    )
                )
        scan_truncated = scan_truncated or root_truncated
        statuses.append(
            DiscoveryRootStatus(
                path=os.fspath(root),
                exists=exists,
                matched_file_count=matched,
                scan_truncated=root_truncated,
            )
        )

    # Newest first, with the path as a stable tiebreaker so two files written in
    # the same second do not swap places between calls.
    collected.sort(key=lambda item: (-item[0], item[1]))
    files = tuple(item[2] for item in collected[:limit])
    return RecentAudioListing(
        searched_at=_now(),
        limit=limit,
        roots=tuple(statuses),
        files=files,
        matched_file_count=len(collected),
        returned_file_count=len(files),
        scan_truncated=scan_truncated,
        limitations=DISCOVERY_LIMITATIONS,
    )
