"""Bounded audio assets and decode caching for Creation Review.

The review workflow deliberately accepts only caller-selected files (or an
exact path returned by the existing bounded audio discovery surface).  This
module owns the small amount of metadata needed to identify those files and a
process-local cache which lets the evaluators share one decoded copy.  It
never stores audio bytes in a review session and never writes to FL Studio.

The review contracts are kept in :mod:`creation_review.models`.  The helpers
below use a tiny compatibility constructor so the module remains importable
while an installation is upgrading from an older package; once the contracts
are present all public results are instances of those frozen models.
"""

# pyright: reportMissingImports=false

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import soundfile as sf

from .. import audio
from ..advisory import AUDIO_SUFFIXES, MAX_AUDIO_FILE_BYTES, resolve_audio_path


ASSET_ANALYZER_VERSION = "creation-review-assets-1"
MAX_REVIEW_ASSETS = 32
MAX_STEM_ASSETS = 16
MAX_SECTION_BOUNCES = 32
MAX_DECODE_SECONDS = 600.0
# A review should be useful on ordinary masters but must not allow a caller to
# retain an unbounded collection of decoded arrays in a long-lived MCP process.
DEFAULT_DECODE_CACHE_BYTES = 768 * 1024 * 1024
DEFAULT_DECODE_CACHE_ENTRIES = 16

ASSET_KINDS = frozenset(
    {
        "candidate_full_mix",
        "before_full_mix",
        "after_full_mix",
        "reference_full_mix",
        "instrumental_stem",
        "vocal_stem",
        "drum_stem",
        "bass_stem",
        "chord_stem",
        "lead_stem",
        "role_stem",
        "section_bounce",
    }
)
FULL_MIX_KINDS = frozenset(
    {"candidate_full_mix", "before_full_mix", "after_full_mix"}
)
STEM_KINDS = frozenset(ASSET_KINDS - FULL_MIX_KINDS - {"reference_full_mix", "section_bounce"})


class ReviewAssetError(ValueError):
    """A caller-selected review asset failed validation."""


def _model(name: str, payload: Mapping[str, Any]) -> Any:
    """Construct a review contract when available, otherwise return a dict.

    This fallback is intentionally private and only helps mixed-version
    callers import the low-level analyzer during an upgrade.  The normal
    package ships ``creation_review.models`` and therefore returns strict
    Pydantic contracts.
    """

    try:
        from . import models

        model_type = getattr(models, name, None)
    except (ImportError, AttributeError):
        model_type = None
    if model_type is None:
        return dict(payload)
    return model_type(**dict(payload))


def _value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _canonical_path(path: str | os.PathLike[str], *, label: str = "path") -> Path:
    try:
        return resolve_audio_path(os.fspath(path), label=label)
    except Exception as exc:
        # Preserve the existing advisory boundary's useful wording while
        # exposing one review-specific exception type to callers.
        raise ReviewAssetError(str(exc)) from None


def sha256_file(
    path: str | os.PathLike[str],
    *,
    max_bytes: int = MAX_AUDIO_FILE_BYTES,
    expected_signature: tuple[int, int] | None = None,
) -> str:
    """Hash one stable file without reading beyond the configured byte cap."""

    if type(max_bytes) is not int or max_bytes <= 0:
        raise ReviewAssetError("max_bytes must be a positive integer")
    digest = hashlib.sha256()
    total = 0
    try:
        with open(path, "rb") as handle:
            before = os.fstat(handle.fileno())
            before_signature = (int(before.st_size), int(before.st_mtime_ns))
            if expected_signature is not None and before_signature != expected_signature:
                raise ReviewAssetError("audio asset changed before hashing")
            if before.st_size > max_bytes:
                raise ReviewAssetError(
                    f"audio asset is {before.st_size} bytes, over the {max_bytes} byte cap"
                )
            while True:
                block = handle.read(min(1024 * 1024, max_bytes - total + 1))
                if not block:
                    break
                total += len(block)
                if total > max_bytes:
                    raise ReviewAssetError(
                        f"audio asset exceeds the {max_bytes} byte cap while hashing"
                    )
                digest.update(block)
            after = os.fstat(handle.fileno())
            after_signature = (int(after.st_size), int(after.st_mtime_ns))
            if before_signature != after_signature or total != after.st_size:
                raise ReviewAssetError("audio asset changed while it was being hashed")
    except ReviewAssetError:
        raise
    except OSError as exc:
        raise ReviewAssetError(f"could not hash audio asset: {exc}") from exc
    return digest.hexdigest()


def _asset_id(
    digest: str,
    *,
    asset_kind: str,
    role_id: str | None,
    section_id: str | None,
    revision_pass_id: str | None,
) -> str:
    material = {
        "sha256": digest,
        "asset_kind": asset_kind,
        "role_id": role_id,
        "section_id": section_id,
        "revision_pass_id": revision_pass_id,
    }
    return "asset-" + hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()[:24]


def _finite_number(value: Any, label: str, *, minimum: float | None = None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReviewAssetError(f"{label} must be a number")
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")):
        raise ReviewAssetError(f"{label} must be finite")
    if minimum is not None and result < minimum:
        raise ReviewAssetError(f"{label} must be at least {minimum}")
    return result


def _stat_signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise ReviewAssetError(f"could not stat audio asset: {exc}") from exc
    return int(stat.st_size), int(stat.st_mtime_ns)


def validate_audio_asset(
    path: str | os.PathLike[str],
    *,
    asset_kind: str = "candidate_full_mix",
    display_label: str | None = None,
    role_id: str | None = None,
    section_id: str | None = None,
    source_run_id: str | None = None,
    revision_pass_id: str | None = None,
    expected_start_seconds: float | None = None,
    declared_offset_seconds: float | None = None,
    max_file_bytes: int = MAX_AUDIO_FILE_BYTES,
    persist_asset_path: bool = True,
    asset_id: str | None = None,
) -> Any:
    """Validate one explicit audio path and return immutable asset metadata.

    The file is header-inspected but not decoded here.  Its digest is computed
    through a size-bounded descriptor and its signature is checked again after
    the header read, so a changing file is refused before a review can retain
    stale metadata.  Decoding is performed by
    :class:`DecodedAudioCache` exactly once per digest and bound.
    """

    if asset_kind not in ASSET_KINDS:
        raise ReviewAssetError(
            f"unsupported review asset kind {asset_kind!r}; expected one of "
            + ", ".join(sorted(ASSET_KINDS))
        )
    if max_file_bytes <= 0:
        raise ReviewAssetError("max_file_bytes must be positive")
    if role_id is not None and (not isinstance(role_id, str) or not role_id.strip()):
        raise ReviewAssetError("role_id must be non-empty when supplied")
    if section_id is not None and (not isinstance(section_id, str) or not section_id.strip()):
        raise ReviewAssetError("section_id must be non-empty when supplied")
    expected_start_seconds = _finite_number(
        expected_start_seconds, "expected_start_seconds"
    )
    if expected_start_seconds is not None and expected_start_seconds < 0:
        raise ReviewAssetError("expected_start_seconds must not be negative")
    declared_offset_seconds = _finite_number(
        declared_offset_seconds, "declared_offset_seconds"
    )

    try:
        path_text = os.fspath(path)
    except TypeError as exc:
        raise ReviewAssetError("audio asset path must be a string or path-like value") from exc
    resolved = _canonical_path(path_text)
    suffix = resolved.suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        raise ReviewAssetError(
            f"audio asset extension {suffix or 'none'!r} is unsupported"
        )
    size_before, mtime_before = _stat_signature(resolved)
    if size_before > max_file_bytes:
        raise ReviewAssetError(
            f"audio asset is {size_before} bytes, over the {max_file_bytes} byte cap"
        )

    digest = sha256_file(
        resolved,
        max_bytes=max_file_bytes,
        expected_signature=(size_before, mtime_before),
    )
    try:
        info = sf.info(os.fspath(resolved))
    except Exception as exc:
        raise ReviewAssetError(f"could not read audio header: {exc}") from None
    size_after, mtime_after = _stat_signature(resolved)
    if (size_before, mtime_before) != (size_after, mtime_after):
        raise ReviewAssetError("audio asset changed while it was being validated")
    if int(info.frames) <= 0:
        raise ReviewAssetError("audio asset contains no frames")
    rate = int(info.samplerate)
    channels = int(info.channels)
    if rate <= 0 or channels <= 0:
        raise ReviewAssetError("audio asset has invalid sample-rate or channel count")
    duration = float(info.frames) / rate
    if duration <= 0:
        raise ReviewAssetError("audio asset has no measurable duration")

    final_asset_id = asset_id or _asset_id(
        digest,
        asset_kind=asset_kind,
        role_id=role_id,
        section_id=section_id,
        revision_pass_id=revision_pass_id,
    )
    warnings: list[str] = []
    if channels > 2:
        warnings.append(
            "multichannel audio is retained, but some existing analyzers inspect the first channel pair"
        )
    if duration > MAX_DECODE_SECONDS:
        warnings.append(
            f"duration exceeds the default {MAX_DECODE_SECONDS:g}-second decode bound"
        )
    # The model deliberately stores a portable container spelling rather than
    # libsndfile's longer format description (for example, ``MPEG-1/2 Audio``
    # for an MP3).  Use the caller-selected extension and normalize aliases to
    # the finite model vocabulary.
    format_by_suffix = {
        ".wav": "wav",
        ".wave": "wave",
        ".w64": "wav",
        ".rf64": "wav",
        ".aif": "aif",
        ".aiff": "aiff",
        ".aifc": "aiff",
        ".flac": "flac",
        ".ogg": "ogg",
        ".oga": "oga",
        ".caf": "wav",
        ".mp3": "mp3",
    }
    portable_format = format_by_suffix.get(suffix)
    if portable_format is None:
        raise ReviewAssetError(f"audio asset format {suffix!r} cannot be represented")
    payload = {
        "asset_id": final_asset_id,
        "asset_kind": asset_kind,
        # Runtime results retain the explicit path.  Persistence code should
        # call asset_for_persistence() when the caller opted out of paths.
        "path": os.fspath(resolved),
        "display_label": display_label or resolved.name,
        "role_id": role_id,
        "section_id": section_id,
        "source_run_id": source_run_id,
        "revision_pass_id": revision_pass_id,
        "expected_start_seconds": expected_start_seconds,
        "declared_offset_seconds": declared_offset_seconds,
        "sha256": digest,
        "sample_rate_hz": rate,
        "channels": channels,
        "duration_seconds": round(duration, 6),
        "file_size_bytes": size_after,
        "format": portable_format,
        "validation_state": "valid",
        "warnings": tuple(warnings),
    }
    return _model("ReviewAudioAsset", payload)


# Short aliases used by integrations migrating from advisory terminology.
validate_review_audio_asset = validate_audio_asset
validate_asset = validate_audio_asset


def asset_for_persistence(asset: Any, *, persist_asset_paths: bool) -> dict[str, Any]:
    """Return metadata safe to put in the local review store.

    Audio bytes are never present.  When path persistence is disabled the
    absolute path is removed while the digest and display label remain.
    """

    if hasattr(asset, "model_dump"):
        payload = asset.model_dump(mode="json", exclude_none=False)
    elif isinstance(asset, Mapping):
        payload = dict(asset)
    else:
        payload = {
            key: getattr(asset, key)
            for key in (
                "asset_id", "asset_kind", "path", "display_label", "role_id",
                "section_id", "source_run_id", "revision_pass_id",
                "expected_start_seconds", "declared_offset_seconds", "sha256",
                "sample_rate_hz", "channels", "duration_seconds", "file_size_bytes",
                "format", "validation_state", "warnings",
            )
            if hasattr(asset, key)
        }
    if not persist_asset_paths:
        payload.pop("path", None)
    # Never accidentally retain arbitrary caller data if a permissive model
    # was supplied by an older installation.
    allowed = {
        "asset_id", "asset_kind", "path", "display_label", "role_id",
        "section_id", "source_run_id", "revision_pass_id", "expected_start_seconds",
        "declared_offset_seconds", "sha256", "sample_rate_hz", "channels",
        "duration_seconds", "file_size_bytes", "format", "validation_state", "warnings",
    }
    return {key: value for key, value in payload.items() if key in allowed}


def _asset_digest_payload(asset: Any) -> dict[str, Any]:
    payload = asset_for_persistence(asset, persist_asset_paths=False)
    # Paths and user labels do not affect identity of an asset set.  Labels are
    # retained in the model but omitted from the digest for deterministic
    # comparison when a caller selects the same file with a different label.
    payload.pop("display_label", None)
    payload.pop("warnings", None)
    return payload


def asset_set_digest(assets: Iterable[Any], *, alignment_state: str | None = None) -> str:
    values = sorted(
        (_asset_digest_payload(asset) for asset in assets),
        key=lambda item: (str(item.get("asset_kind", "")), str(item.get("asset_id", ""))),
    )
    material = {"analyzer_version": ASSET_ANALYZER_VERSION, "assets": values}
    if alignment_state is not None:
        material["alignment_state"] = alignment_state
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def build_review_asset_set(
    assets: Sequence[Any],
    *,
    source_run_id: str | None = None,
    max_assets: int = MAX_REVIEW_ASSETS,
    persist_asset_paths: bool = True,
) -> Any:
    """Validate relationships between a candidate, reference, and stems."""

    if not assets:
        raise ReviewAssetError("a review asset set needs at least one audio asset")
    if len(assets) > max_assets:
        raise ReviewAssetError(f"a review asset set may contain at most {max_assets} assets")
    records = list(assets)
    # Re-check persisted/constructed records against the current file.  This
    # catches stale session metadata before analysis starts.
    for record in records:
        kind = _value(record, "asset_kind")
        path = _value(record, "path")
        expected_digest = _value(record, "sha256")
        if kind not in ASSET_KINDS:
            raise ReviewAssetError(f"unsupported review asset kind {kind!r}")
        if path:
            current = _canonical_path(str(path), label="asset.path")
            current_signature = _stat_signature(current)
            actual = sha256_file(
                current,
                expected_signature=current_signature,
            )
            recorded_size = _value(record, "file_size_bytes")
            if recorded_size is not None and int(recorded_size) != current_signature[0]:
                raise ReviewAssetError(
                    f"asset {kind!r} size no longer matches the supplied metadata"
                )
            if expected_digest and actual != expected_digest:
                raise ReviewAssetError(
                    f"asset {kind!r} digest no longer matches the supplied metadata"
                )
    full_mix = [record for record in records if _value(record, "asset_kind") in FULL_MIX_KINDS]
    if not full_mix or len(full_mix) > 3:
        raise ReviewAssetError(
            "a review asset set needs a candidate, before, or after full-mix asset"
        )
    full_mix_kinds = [_value(record, "asset_kind") for record in full_mix]
    if len(set(full_mix_kinds)) != len(full_mix_kinds):
        raise ReviewAssetError("a review asset set may contain at most one asset for each full-mix kind")
    by_digest: dict[str, list[Any]] = {}
    for record in records:
        by_digest.setdefault(str(_value(record, "sha256")), []).append(record)
    duplicate_ids = [digest for digest, values in by_digest.items() if len(values) > 1]
    # The same file can be useful as a section bounce and a stem only if its
    # role/kind differs; before and after must never share identity.
    before_after = [
        record
        for record in records
        if _value(record, "asset_kind") in {"before_full_mix", "after_full_mix"}
    ]
    if len(before_after) == 2 and _value(before_after[0], "sha256") == _value(before_after[1], "sha256"):
        raise ReviewAssetError("before and after full-mix assets must have different SHA-256 identities")

    stems = [record for record in records if _value(record, "asset_kind") in STEM_KINDS]
    if len(stems) > MAX_STEM_ASSETS:
        raise ReviewAssetError(f"a review asset set may contain at most {MAX_STEM_ASSETS} stems")
    section_bounces = [
        record for record in records if _value(record, "asset_kind") == "section_bounce"
    ]
    if len(section_bounces) > MAX_SECTION_BOUNCES:
        raise ReviewAssetError(
            f"a review asset set may contain at most {MAX_SECTION_BOUNCES} section bounces"
        )
    reference = next(
        (record for record in records if _value(record, "asset_kind") == "reference_full_mix"),
        None,
    )
    if sum(_value(record, "asset_kind") == "reference_full_mix" for record in records) > 1:
        raise ReviewAssetError("a review asset set may contain at most one reference full mix")

    # Metadata synchronization is conservative: similar durations do not
    # imply a common timeline. Explicit offsets are required to agree when
    # supplied; otherwise the set remains usable but low-confidence.
    timed = [record for record in (*full_mix, *stems) if _value(record, "asset_kind") != "reference_full_mix"]
    rates = {int(_value(record, "sample_rate_hz")) for record in timed if _value(record, "sample_rate_hz") is not None}
    full_durations = [
        float(_value(record, "duration_seconds"))
        for record in full_mix
        if _value(record, "asset_kind") != "reference_full_mix"
        and _value(record, "duration_seconds") is not None
    ]
    timed_durations = [
        float(_value(record, "duration_seconds"))
        for record in timed
        if _value(record, "duration_seconds") is not None
    ]
    offsets = [
        float(_value(record, "declared_offset_seconds"))
        for record in timed
        if _value(record, "declared_offset_seconds") is not None
    ]
    explicit_starts = [
        float(_value(record, "expected_start_seconds"))
        for record in timed
        if _value(record, "expected_start_seconds") is not None
    ]
    limitations: list[str] = []
    alignment_state = "unknown"
    common_start_confidence = 0.0
    full_duration_compatible = (
        len(full_durations) <= 1
        or max(full_durations) - min(full_durations) <= 1.0 / max(rates or {1})
    )
    timed_duration_compatible = (
        len(timed_durations) <= 1
        or max(timed_durations) - min(timed_durations)
        <= 1.0 / max(rates or {1})
    )
    duration_compatible = full_duration_compatible and timed_duration_compatible
    sample_rate_compatible = len(rates) <= 1
    if stems:
        offsets_compatible = not offsets or max(offsets) - min(offsets) <= 1e-6
        starts_compatible = (
            not explicit_starts
            or max(explicit_starts) - min(explicit_starts) <= 1e-6
        )
        offset_evidence = len(offsets) == len(timed) and offsets_compatible
        start_evidence = (
            len(explicit_starts) == len(timed) and starts_compatible
        )
        explicit_common_start = offset_evidence or start_evidence
        contradictory_start = (
            (len(offsets) == len(timed) and not offsets_compatible)
            or (len(explicit_starts) == len(timed) and not starts_compatible)
        )
        if (
            sample_rate_compatible
            and timed_duration_compatible
            and explicit_common_start
            and not contradictory_start
        ):
            alignment_state = "aligned"
            common_start_confidence = 0.95
        elif (
            sample_rate_compatible
            and timed_duration_compatible
            and not contradictory_start
        ):
            alignment_state = "unknown"
            common_start_confidence = 0.0
            limitations.append(
                "supplied stems do not declare a common export start; masking attribution is withheld"
            )
        else:
            alignment_state = "unsynchronized"
            common_start_confidence = 0.2
            if not sample_rate_compatible:
                limitations.append("supplied stems use different sample rates")
            if not timed_duration_compatible:
                limitations.append("supplied stems and full mix have incompatible durations")
            if contradictory_start:
                limitations.append("supplied stems do not declare a common export start")
    else:
        limitations.append("no synchronized stems supplied; role attribution remains unavailable")
        if len(full_mix) > 1 and not full_duration_compatible:
            alignment_state = "unsynchronized"
            common_start_confidence = 0.2
            limitations.append("full-mix before/after assets have incompatible durations")
    if duplicate_ids:
        limitations.append("some asset identities are reused across distinct asset roles")
    digest = asset_set_digest(records, alignment_state=alignment_state)
    candidate = next(
        (record for record in full_mix if _value(record, "asset_kind") == "candidate_full_mix"),
        None,
    )
    before = next(
        (record for record in full_mix if _value(record, "asset_kind") == "before_full_mix"),
        None,
    )
    after = next(
        (record for record in full_mix if _value(record, "asset_kind") == "after_full_mix"),
        None,
    )
    asset_set_id = "asset-set-" + hashlib.sha256(
        json.dumps(
            sorted(_value(record, "asset_id") for record in records),
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()[:24]
    payload = {
        "asset_set_id": asset_set_id,
        "candidate_full_mix": candidate,
        "before_full_mix": before,
        "after_full_mix": after,
        "reference": reference,
        "synchronized_stems": tuple(stems),
        "section_bounces": tuple(section_bounces),
        "alignment_state": alignment_state,
        "common_start_confidence": common_start_confidence,
        "duration_compatible": duration_compatible,
        "sample_rate_compatible": sample_rate_compatible,
        "asset_set_digest": digest,
        "limitations": tuple(limitations),
    }
    return _model("ReviewAssetSet", payload)


validate_asset_set = build_review_asset_set


def _asset_path(asset: Any) -> str:
    path = _value(asset, "path")
    if not path:
        raise ReviewAssetError(
            "the audio path is unavailable; reattach the asset because persisted metadata omitted paths"
        )
    return str(path)


@dataclass(frozen=True)
class DecodedCacheStats:
    entries: int
    decoded_bytes: int
    decode_count: int
    feature_entries: int
    cache_hits: int
    feature_bytes: int = 0


class DecodedAudioCache:
    """Thread-safe bounded decoded-audio and derived-feature cache.

    The decoded and derived stores use separate LRU bounds.  Derived values
    are keyed by the ordered digest(s) of their source files plus analyzer,
    section-map, and policy digests.  The multi-source path is useful for
    alignment/comparison work where a result depends on both bounces (or both
    sides of a masking pair).
    """

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_DECODE_CACHE_ENTRIES,
        max_bytes: int = DEFAULT_DECODE_CACHE_BYTES,
    ) -> None:
        if max_entries <= 0 or max_bytes <= 0:
            raise ValueError("decode cache bounds must be positive")
        self.max_entries = int(max_entries)
        self.max_bytes = int(max_bytes)
        self._decoded: OrderedDict[tuple[str, float | None], tuple[Any, int]] = OrderedDict()
        self._features: OrderedDict[tuple[str, str, str, str], Any] = OrderedDict()
        self._feature_sizes: dict[tuple[str, str, str, str], int] = {}
        self._decoded_bytes = 0
        self._feature_bytes = 0
        self._decode_count = 0
        self._cache_hits = 0
        self._lock = threading.RLock()
        self._decode_inflight: dict[tuple[str, float | None], threading.Event] = {}
        self._feature_inflight: dict[
            tuple[str, str, str, str], threading.Event
        ] = {}

    @property
    def decode_count(self) -> int:
        with self._lock:
            return self._decode_count

    def stats(self) -> DecodedCacheStats:
        with self._lock:
            return DecodedCacheStats(
                entries=len(self._decoded),
                decoded_bytes=self._decoded_bytes,
                decode_count=self._decode_count,
                feature_entries=len(self._features),
                cache_hits=self._cache_hits,
                feature_bytes=self._feature_bytes,
            )

    def clear(self) -> None:
        with self._lock:
            self._decoded.clear()
            self._features.clear()
            self._feature_sizes.clear()
            self._decoded_bytes = 0
            self._feature_bytes = 0
            self._cache_hits = 0

    def _bounded_seconds(self, max_seconds: float | None) -> float | None:
        if max_seconds is None:
            # The review cache always has a duration ceiling, even when a
            # caller omits an explicit per-call value.  This keeps a direct
            # cache use bounded just like the high-level evaluator.
            return MAX_DECODE_SECONDS
        if isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float)):
            raise ReviewAssetError("max_seconds must be a number")
        value = float(max_seconds)
        if value <= 0 or value > MAX_DECODE_SECONDS:
            raise ReviewAssetError(
                f"max_seconds must be greater than zero and at most {MAX_DECODE_SECONDS:g}"
            )
        return value

    def get_or_decode(
        self,
        asset: Any,
        *,
        max_seconds: float | None = MAX_DECODE_SECONDS,
    ) -> Any:
        """Return one decoded :class:`audio.Loaded` for an unchanged asset."""

        seconds = self._bounded_seconds(max_seconds)
        path = _asset_path(asset)
        resolved = _canonical_path(path, label="asset.path")
        expected = _value(asset, "sha256")
        source_signature = _stat_signature(resolved)
        current = sha256_file(
            resolved,
            expected_signature=source_signature,
        )
        if expected and current != expected:
            raise ReviewAssetError("audio asset digest changed before decoding")
        key = (current, seconds)
        while True:
            with self._lock:
                cached = self._decoded.get(key)
                if cached is not None:
                    self._decoded.move_to_end(key)
                    self._cache_hits += 1
                    return cached[0]
                inflight = self._decode_inflight.get(key)
                if inflight is None:
                    inflight = threading.Event()
                    self._decode_inflight[key] = inflight
                    break
            # Decode happens outside the cache lock, but only the owner of the
            # per-key event may perform it.  A failed owner wakes waiters; one
            # may then retry through the normal explicit caller path.
            inflight.wait()
        try:
            info = sf.info(os.fspath(resolved))
            requested_frames = int(info.frames)
            if seconds is not None:
                requested_frames = min(requested_frames, int(seconds * int(info.samplerate)))
            # ``audio.load`` has a larger global safety ceiling; this review
            # cache adds a stricter per-cache bound before allocating samples.
            estimated = requested_frames * max(1, int(info.channels)) * 8
            if estimated > self.max_bytes:
                raise ReviewAssetError(
                    f"decoded audio would use about {estimated} bytes, over the {self.max_bytes} byte review cache cap"
                )
            started = perf_counter()
            loaded = audio.load(os.fspath(resolved), max_seconds=seconds)
            if _stat_signature(resolved) != source_signature:
                raise ReviewAssetError("audio asset changed while it was being decoded")
            # Keep timing available to evaluation reports without changing
            # the Loaded contract.
            loaded.meta.setdefault("review_decode_seconds", round(perf_counter() - started, 6))
        except ReviewAssetError:
            with self._lock:
                self._decode_inflight.pop(key, None)
                inflight.set()
            raise
        except audio.AudioError as exc:
            with self._lock:
                self._decode_inflight.pop(key, None)
                inflight.set()
            raise ReviewAssetError(str(exc)) from None
        except Exception as exc:
            with self._lock:
                self._decode_inflight.pop(key, None)
                inflight.set()
            raise ReviewAssetError(f"could not decode audio asset: {exc}") from None
        decoded_bytes = int(loaded.samples.nbytes)
        if loaded.channel_samples is not None:
            decoded_bytes += int(loaded.channel_samples.nbytes)
        if loaded.stereo is not None and loaded.channel_samples is None:
            decoded_bytes += int(loaded.stereo.nbytes)
        with self._lock:
            self._decode_count += 1
            self._decoded[key] = (loaded, decoded_bytes)
            self._decoded_bytes += decoded_bytes
            self._decoded.move_to_end(key)
            while (
                len(self._decoded) > self.max_entries
                or self._decoded_bytes > self.max_bytes
            ):
                _old_key, (_old_loaded, old_size) = self._decoded.popitem(last=False)
                self._decoded_bytes -= old_size
            self._decode_inflight.pop(key, None)
            inflight.set()
            return loaded

    # Friendly names used by evaluators and tests.
    load = get_or_decode
    get = get_or_decode

    @staticmethod
    def _normalise_assets(assets: Any | Sequence[Any]) -> tuple[Any, ...]:
        if isinstance(assets, (str, bytes, os.PathLike)):
            return (assets,)
        if isinstance(assets, Sequence) and not isinstance(assets, (str, bytes, bytearray)):
            return tuple(assets)
        return (assets,)

    @staticmethod
    def _loaded_digest(loaded: audio.Loaded) -> str:
        """Derive a stable digest for a caller-supplied in-memory Loaded.

        Normal review calls pass validated asset metadata and therefore hash
        the source file.  This fallback keeps the low-level comparison helper
        cacheable when integrations pass a manually-created ``Loaded`` value
        whose path is only a descriptive label.
        """

        data = audio._channel_data(loaded)
        digest = hashlib.sha256()
        digest.update(np.asarray(data, dtype=np.float64).tobytes())
        digest.update(str(int(loaded.rate)).encode("ascii"))
        digest.update(str(int(loaded.channels)).encode("ascii"))
        return digest.hexdigest()

    @classmethod
    def _source_digest(cls, asset: Any) -> str:
        if isinstance(asset, audio.Loaded):
            source_path = str(getattr(asset, "path", ""))
            if source_path and os.path.isfile(source_path):
                return sha256_file(_canonical_path(source_path, label="loaded.path"))
            return cls._loaded_digest(asset)
        path = _value(asset, "path")
        if path:
            resolved = _canonical_path(str(path), label="asset.path")
            digest = sha256_file(resolved)
            expected = _value(asset, "sha256")
            if expected and digest != expected:
                raise ReviewAssetError("audio asset digest changed before feature lookup")
            return digest
        raise ReviewAssetError("the audio path is unavailable; reattach the asset before feature lookup")

    @staticmethod
    def _cache_value_size(value: Any, seen: set[int] | None = None) -> int:
        """Estimate retained bytes for a derived cache value.

        Feature results are normally small mappings, but alignment helpers may
        return NumPy arrays.  Counting arrays recursively prevents those
        results from bypassing the cache's memory bound.
        """

        seen = seen if seen is not None else set()
        marker = id(value)
        if marker in seen:
            return 0
        seen.add(marker)
        if isinstance(value, np.ndarray):
            return int(value.nbytes)
        if isinstance(value, (str, bytes, bytearray)):
            return len(value)
        if isinstance(value, Mapping):
            return int(sys.getsizeof(value)) + sum(
                DecodedAudioCache._cache_value_size(key, seen)
                + DecodedAudioCache._cache_value_size(item, seen)
                for key, item in value.items()
            )
        if isinstance(value, (tuple, list, set, frozenset)):
            return int(sys.getsizeof(value)) + sum(
                DecodedAudioCache._cache_value_size(item, seen) for item in value
            )
        if hasattr(value, "model_dump"):
            try:
                return DecodedAudioCache._cache_value_size(
                    value.model_dump(mode="json", exclude_none=False), seen
                )
            except Exception:
                pass
        if hasattr(value, "__dict__"):
            return int(sys.getsizeof(value)) + DecodedAudioCache._cache_value_size(
                vars(value), seen
            )
        return int(sys.getsizeof(value))

    @classmethod
    def _feature_key(
        cls,
        assets: Sequence[Any],
        *,
        analyzer_version: str,
        section_map_digest: str,
        analysis_policy_digest: str,
    ) -> tuple[str, str, str, str]:
        digests = tuple(cls._source_digest(asset) for asset in assets)
        if len(digests) == 1:
            source_digest = digests[0]
        else:
            # Preserve source ordering: before/after and vocal/instrumental
            # pairs are directional even when their file sets are identical.
            source_digest = hashlib.sha256(
                json.dumps(digests, separators=(",", ":")).encode("ascii")
            ).hexdigest()
        return (
            source_digest,
            str(analyzer_version),
            str(section_map_digest),
            str(analysis_policy_digest),
        )

    def _decode_record(
        self,
        asset: Any,
        *,
        max_seconds: float | None,
    ) -> Any:
        if isinstance(asset, audio.Loaded):
            return asset
        return self.get_or_decode(asset, max_seconds=max_seconds)

    def get_or_compute(
        self,
        assets: Any | Sequence[Any],
        *,
        analyzer_version: str,
        section_map_digest: str = "",
        analysis_policy_digest: str = "",
        compute,
        max_seconds: float | None = MAX_DECODE_SECONDS,
    ) -> Any:
        """Compute/cache a value derived from one or more audio assets.

        ``compute`` receives one decoded ``Loaded`` for a single asset and a
        tuple of decoded values for multiple assets.  The value is retained
        only when its estimated size fits the derived-value bound; oversized
        results remain correct but are deliberately not cached.
        """

        records = self._normalise_assets(assets)
        if not records:
            raise ReviewAssetError("at least one asset is required for feature lookup")
        seconds = self._bounded_seconds(max_seconds)
        effective_policy_digest = hashlib.sha256(
            json.dumps(
                {
                    "analysis_policy_digest": analysis_policy_digest,
                    "max_seconds": seconds,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        key = self._feature_key(
            records,
            analyzer_version=analyzer_version,
            section_map_digest=section_map_digest,
            analysis_policy_digest=effective_policy_digest,
        )
        while True:
            with self._lock:
                if key in self._features:
                    self._features.move_to_end(key)
                    self._cache_hits += 1
                    return self._features[key]
                inflight = self._feature_inflight.get(key)
                if inflight is None:
                    inflight = threading.Event()
                    self._feature_inflight[key] = inflight
                    break
            inflight.wait()
        try:
            loaded_values = tuple(
                self._decode_record(asset, max_seconds=seconds) for asset in records
            )
            value = compute(loaded_values[0] if len(loaded_values) == 1 else loaded_values)
            value_size = self._cache_value_size(value)
        except Exception:
            with self._lock:
                self._feature_inflight.pop(key, None)
                inflight.set()
            raise
        with self._lock:
            # A result too large for the derived store is returned to the
            # caller but not retained, keeping the cache bounded even for
            # alignment code that produces large NumPy arrays.
            if value_size <= self.max_bytes:
                old_size = self._feature_sizes.pop(key, 0)
                self._feature_bytes -= old_size
                self._features[key] = value
                self._feature_sizes[key] = value_size
                self._features.move_to_end(key)
                self._feature_bytes += value_size
                while (
                    len(self._features) > self.max_entries * 4
                    or self._feature_bytes > self.max_bytes
                ):
                    old_key, _old_value = self._features.popitem(last=False)
                    self._feature_bytes -= self._feature_sizes.pop(old_key, 0)
            self._feature_inflight.pop(key, None)
            inflight.set()
        return value

    def get_or_compute_features(
        self,
        asset: Any,
        *,
        analyzer_version: str,
        section_map_digest: str,
        analysis_policy_digest: str,
        compute,
        max_seconds: float | None = MAX_DECODE_SECONDS,
    ) -> Any:
        return self.get_or_compute(
            asset,
            analyzer_version=analyzer_version,
            section_map_digest=section_map_digest,
            analysis_policy_digest=analysis_policy_digest,
            compute=compute,
            max_seconds=max_seconds,
        )

    # Explicit alias for callers that describe these values as derived
    # features rather than generic cached computations.
    get_or_compute_derived = get_or_compute


_GLOBAL_DECODE_CACHE = DecodedAudioCache()


def get_decode_cache() -> DecodedAudioCache:
    return _GLOBAL_DECODE_CACHE


def clear_decode_cache() -> None:
    _GLOBAL_DECODE_CACHE.clear()


__all__ = [
    "ASSET_ANALYZER_VERSION",
    "ASSET_KINDS",
    "FULL_MIX_KINDS",
    "STEM_KINDS",
    "ReviewAssetError",
    "DecodedAudioCache",
    "DecodedCacheStats",
    "asset_for_persistence",
    "asset_set_digest",
    "build_review_asset_set",
    "clear_decode_cache",
    "get_decode_cache",
    "sha256_file",
    "validate_asset",
    "validate_asset_set",
    "validate_audio_asset",
    "validate_review_audio_asset",
]
