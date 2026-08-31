"""Bounded local history for deterministic Sound Selection ranking.

This store is intentionally local and boring: it writes a small schema-versioned
JSON document with an atomic replace under a process-local re-entrant lock.  It
never stores prompts, audio, project files, credentials, transcripts, or vendor
manuals.  A malformed file is isolated in memory and left untouched until the
caller explicitly resets or repairs it, so a read can never silently destroy
the user's history.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal, Mapping

from pydantic import Field, ValidationError, model_validator

from ..host_config import fl_studio_user_data_dir
from .models import (
    SOUND_SELECTION_SCHEMA_VERSION,
    DescriptorIdentifier,
    Digest,
    HistoryVerdict,
    RoleIdentifier,
    SoundSelectionModel,
    canonical_digest,
)


HISTORY_SCHEMA_VERSION = SOUND_SELECTION_SCHEMA_VERSION
HISTORY_PATH_ENV = "POSTFADER_SOUND_SELECTION_HISTORY_PATH"
HISTORY_PATH_ENV_ALIASES = (HISTORY_PATH_ENV, "POSTFADER_SOUND_SELECTION_HISTORY")
DEFAULT_HISTORY_FILENAME = "sound-selection-history-v1.json"
DEFAULT_MAX_RECORDS = 256
DEFAULT_MAX_FEEDBACK = 512
MAX_HISTORY_RECORDS = 4096
MAX_HISTORY_FEEDBACK = 8192
MAX_HISTORY_NOTE = 512
# Keep the durable document bounded on both sides of the read/write boundary.
# The counter limit is deliberately finite so malformed JSON cannot introduce
# arbitrarily large Python integers, while remaining far beyond any practical
# local usage lifetime.  Updates saturate at this value instead of eventually
# producing an invalid model after enough successful calls.
MAX_HISTORY_SERIALIZED_BYTES = 8 * 1024 * 1024
MAX_HISTORY_COUNTER = 2**31 - 1
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class SoundHistoryRecord(SoundSelectionModel):
    """Aggregated usage for one product/preset/role tuple."""

    record_id: str = Field(min_length=1, max_length=128)
    product_id: str = Field(min_length=1, max_length=128)
    preset_identity_digest: Digest
    preset_name: str | None = Field(default=None, max_length=256)
    role_id: RoleIdentifier
    style_tags: tuple[str, ...] = Field(default=(), max_length=32)
    first_used_at: datetime | None = None
    last_used_at: datetime
    usage_count: int = Field(default=0, ge=0, le=MAX_HISTORY_COUNTER)
    consecutive_use_count: int = Field(
        default=0, ge=0, le=MAX_HISTORY_COUNTER
    )
    accepted_count: int = Field(default=0, ge=0, le=MAX_HISTORY_COUNTER)
    rejected_count: int = Field(default=0, ge=0, le=MAX_HISTORY_COUNTER)
    last_feedback_at: datetime | None = None
    last_palette_digest: Digest | None = None

    @model_validator(mode="after")
    def validate_counts(self) -> "SoundHistoryRecord":
        if self.usage_count == 0 and self.consecutive_use_count != 0:
            raise ValueError("consecutive_use_count requires usage_count")
        if self.consecutive_use_count > self.usage_count:
            raise ValueError("consecutive_use_count cannot exceed usage_count")
        if self.first_used_at is not None and self.first_used_at > self.last_used_at:
            raise ValueError("first_used_at cannot be after last_used_at")
        return self


class SoundHistoryFeedback(SoundSelectionModel):
    """One explicit user verdict; no verdict is inferred from silence."""

    feedback_id: str = Field(min_length=1, max_length=128)
    palette_id: str = Field(min_length=1, max_length=128)
    role_id: RoleIdentifier | None = None
    product_id: str | None = Field(default=None, max_length=128)
    preset_identity_digest: Digest | None = None
    verdict: HistoryVerdict
    descriptors: tuple[DescriptorIdentifier, ...] = Field(default=(), max_length=64)
    note: str | None = Field(default=None, max_length=MAX_HISTORY_NOTE)
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_identity_pair(self) -> "SoundHistoryFeedback":
        if (self.product_id is None) != (self.preset_identity_digest is None):
            raise ValueError(
                "feedback product_id and preset_identity_digest must be supplied together"
            )
        if self.note is not None and not self.note.strip():
            raise ValueError("feedback note must contain text when supplied")
        return self


class SoundHistoryDocument(SoundSelectionModel):
    """On-disk schema; arrays are represented as tuples after validation."""

    schema_version: Literal["1.0"] = HISTORY_SCHEMA_VERSION
    created_at: datetime
    updated_at: datetime
    records: tuple[SoundHistoryRecord, ...] = Field(default=(), max_length=MAX_HISTORY_RECORDS)
    feedback: tuple[SoundHistoryFeedback, ...] = Field(default=(), max_length=MAX_HISTORY_FEEDBACK)

    @model_validator(mode="after")
    def validate_document(self) -> "SoundHistoryDocument":
        record_ids = [item.record_id for item in self.records]
        if len(set(record_ids)) != len(record_ids):
            raise ValueError("history record IDs must be unique")
        record_keys = [
            (item.product_id.casefold(), item.preset_identity_digest, item.role_id.casefold())
            for item in self.records
        ]
        if len(set(record_keys)) != len(record_keys):
            raise ValueError("history usage identities must be unique")
        feedback_ids = [item.feedback_id for item in self.feedback]
        if len(set(feedback_ids)) != len(feedback_ids):
            raise ValueError("history feedback IDs must be unique")
        if self.updated_at < self.created_at:
            raise ValueError("history updated_at cannot be before created_at")
        return self


class SoundHistoryStatus(SoundSelectionModel):
    """Explicit health/size information for the local history store."""

    path: str
    exists: bool
    healthy: bool
    corrupt: bool
    schema_version: str | None = None
    record_count: int = Field(default=0, ge=0)
    feedback_count: int = Field(default=0, ge=0)
    max_records: int = Field(ge=1, le=4096)
    max_feedback: int = Field(ge=1, le=8192)
    warnings: tuple[str, ...] = Field(default=(), max_length=16)
    error: str | None = Field(default=None, max_length=1024)


class SoundHistoryResetResult(SoundSelectionModel):
    """Receipt for an explicit history reset request."""

    path: str
    existed: bool
    removed: bool
    recoverable: bool = False
    warnings: tuple[str, ...] = Field(default=(), max_length=16)


class HistoryCorruptionError(RuntimeError):
    """Raised by explicit callers that require writes to a healthy store."""


class HistoryWriteError(RuntimeError):
    """Raised when an atomic local history write cannot be completed."""


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    key = os.fspath(path)
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def resolve_history_path(
    path: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    user_data_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve an explicit absolute path, env override, or PostFader data path."""

    if path is not None and os.fspath(path).strip():
        raw = os.fspath(path).strip()
    else:
        environment = os.environ if environ is None else environ
        raw = ""
        for key in HISTORY_PATH_ENV_ALIASES:
            value = environment.get(key, "").strip()
            if value:
                raw = value
                break
        if not raw:
            root = (
                Path(user_data_dir)
                if user_data_dir is not None
                else fl_studio_user_data_dir()
            )
            return (root / "Settings" / "PostFader" / DEFAULT_HISTORY_FILENAME).resolve()
    resolved = Path(raw)
    if not resolved.is_absolute():
        raise ValueError("Sound Selection history path must be absolute")
    return resolved.resolve()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime | None) -> datetime:
    result = _utc_now() if value is None else value
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _increment_counter(value: int) -> int:
    """Increment a validated history counter without exceeding its ceiling."""

    return min(value + 1, MAX_HISTORY_COUNTER)


class LocalSoundSelectionHistory:
    """Thread-safe bounded history with corruption isolation and atomic writes."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        max_feedback: int = DEFAULT_MAX_FEEDBACK,
        environ: Mapping[str, str] | None = None,
        user_data_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        if type(max_records) is not int or not (1 <= max_records <= 4096):
            raise ValueError("max_records is outside history bounds")
        if type(max_feedback) is not int or not (1 <= max_feedback <= 8192):
            raise ValueError("max_feedback is outside history bounds")
        self.path = resolve_history_path(
            path, environ=environ, user_data_dir=user_data_dir
        )
        self.max_records = max_records
        self.max_feedback = max_feedback
        self._lock = _path_lock(self.path)
        self._document: SoundHistoryDocument | None = None
        self._corrupt = False
        self._error: str | None = None
        self._warnings: tuple[str, ...] = ()
        with self._lock:
            self._load_locked()

    @property
    def history_path(self) -> Path:
        return self.path

    def _empty_document(self, *, now: datetime | None = None) -> SoundHistoryDocument:
        stamp = _ensure_utc(now)
        return SoundHistoryDocument(created_at=stamp, updated_at=stamp)

    def _load_locked(self) -> None:
        if not self.path.exists():
            self._document = self._empty_document()
            self._corrupt = False
            self._error = None
            self._warnings = ()
            return
        try:
            # Open in binary mode so the bound is measured in serialized
            # bytes rather than decoded characters.  The stat check rejects
            # an oversized regular file before allocating its contents; the
            # bounded read also closes the race where the file grows after
            # stat (and keeps special files bounded as well).
            with self.path.open("rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                if size > MAX_HISTORY_SERIALIZED_BYTES:
                    raise ValueError("history file exceeds the 8 MiB safety bound")
                encoded = handle.read(MAX_HISTORY_SERIALIZED_BYTES + 1)
            if len(encoded) > MAX_HISTORY_SERIALIZED_BYTES:
                raise ValueError("history file exceeds the 8 MiB safety bound")
            raw = encoded.decode("utf-8")
            # JSON transport necessarily represents datetimes as ISO strings;
            # the in-memory model remains strict for Python callers while the
            # document decoder uses Pydantic's safe JSON coercion.
            document = SoundHistoryDocument.model_validate_json(raw, strict=False)
            if len(document.records) > self.max_records:
                raise ValueError("history record count exceeds configured bound")
            if len(document.feedback) > self.max_feedback:
                raise ValueError("history feedback count exceeds configured bound")
        except (OSError, UnicodeError, ValueError, ValidationError) as exc:
            self._document = None
            self._corrupt = True
            self._error = str(exc)[:1024]
            self._warnings = (
                "Sound Selection history is corrupt or unreadable; the file was left untouched.",
            )
            return
        self._document = document
        self._corrupt = False
        self._error = None
        self._warnings = ()

    def _refresh_locked(self) -> None:
        # Re-read before each mutation so separate store instances sharing one
        # path do not lose each other's valid updates.
        self._load_locked()

    def _require_writable_locked(self) -> SoundHistoryDocument | None:
        self._refresh_locked()
        if self._corrupt or self._document is None:
            return None
        return self._document

    def status(self) -> SoundHistoryStatus:
        with self._lock:
            self._load_locked()
            document = self._document
            return SoundHistoryStatus(
                path=os.fspath(self.path),
                exists=self.path.exists(),
                healthy=not self._corrupt,
                corrupt=self._corrupt,
                schema_version=None if document is None else document.schema_version,
                record_count=0 if document is None else len(document.records),
                feedback_count=0 if document is None else len(document.feedback),
                max_records=self.max_records,
                max_feedback=self.max_feedback,
                warnings=self._warnings,
                error=self._error,
            )

    history_status = status

    def snapshot(self) -> SoundHistoryDocument:
        with self._lock:
            self._load_locked()
            return self._empty_document() if self._document is None else self._document

    def records(self) -> tuple[SoundHistoryRecord, ...]:
        return self.snapshot().records

    def feedback(self) -> tuple[SoundHistoryFeedback, ...]:
        return self.snapshot().feedback

    def lookup(
        self,
        *,
        product_id: str,
        preset_identity_digest: str,
        role_id: str,
    ) -> SoundHistoryRecord | None:
        product_key = product_id.casefold()
        digest_key = preset_identity_digest.casefold()
        role_key = role_id.casefold()
        return next(
            (
                item
                for item in self.records()
                if item.product_id.casefold() == product_key
                and item.preset_identity_digest.casefold() == digest_key
                and item.role_id.casefold() == role_key
            ),
            None,
        )

    usage_for = lookup

    def _write_locked(self, document: SoundHistoryDocument) -> None:
        payload = document.model_dump_json(indent=2).encode("utf-8")
        if len(payload) > MAX_HISTORY_SERIALIZED_BYTES:
            raise HistoryWriteError(
                "serialized Sound Selection history exceeds the 8 MiB safety bound"
            )
        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=os.fspath(parent)
            )
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = -1
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                temporary = ""
                try:
                    directory_fd = os.open(os.fspath(parent), os.O_DIRECTORY)
                except (AttributeError, OSError):
                    directory_fd = -1
                if directory_fd >= 0:
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                if temporary:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass
        except OSError as exc:
            raise HistoryWriteError(f"cannot atomically write Sound Selection history: {exc}") from exc
        self._document = document
        self._corrupt = False
        self._error = None
        self._warnings = ()

    @staticmethod
    def _record_key(product_id: str, digest: str, role_id: str) -> tuple[str, str, str]:
        return product_id.casefold(), digest.casefold(), role_id.casefold()

    def record_usage(
        self,
        product_id: str,
        preset_identity_digest: str,
        role_id: str,
        *,
        preset_name: str | None = None,
        style_tags: Iterable[str] = (),
        palette_digest: str | None = None,
        now: datetime | None = None,
        persist: bool = True,
    ) -> bool:
        """Record one successfully applied assignment.

        Planning must not call this method.  ``persist=False`` is a true
        no-op, including no directory creation and no read/write of history.
        Corrupt history is also a no-op with an explicit unhealthy status.  It
        raises ``HistoryCorruptionError`` so integration callers can report
        the skipped write truthfully; the malformed source remains recoverable
        until reset/repair is requested.
        """

        if not persist:
            return False
        if not isinstance(product_id, str) or not product_id.strip():
            raise ValueError("product_id must contain text")
        if not isinstance(preset_identity_digest, str) or not _DIGEST_RE.fullmatch(
            preset_identity_digest
        ):
            raise ValueError("preset_identity_digest must be a SHA-256 digest")
        if not isinstance(role_id, str) or not role_id.strip():
            raise ValueError("role_id must contain text")
        product_id = product_id.strip().casefold()
        preset_identity_digest = preset_identity_digest.lower()
        role_id = role_id.strip()
        stamp = _ensure_utc(now)
        tags = tuple(sorted({item.strip() for item in style_tags if item.strip()}))
        if len(tags) > 32:
            raise ValueError("style_tags exceed history bounds")
        if palette_digest is not None and not _DIGEST_RE.fullmatch(palette_digest):
            raise ValueError("palette_digest must be a SHA-256 digest")
        if palette_digest is not None:
            palette_digest = palette_digest.lower()
        with self._lock:
            document = self._require_writable_locked()
            if document is None:
                raise HistoryCorruptionError(
                    "Sound Selection history is corrupt or unreadable; "
                    "the source was left untouched and no usage was recorded"
                )
            key = self._record_key(product_id, preset_identity_digest, role_id)
            previous = next(
                (
                    item
                    for item in document.records
                    if self._record_key(
                        item.product_id, item.preset_identity_digest, item.role_id
                    )
                    == key
                ),
                None,
            )
            latest_for_role = max(
                (
                    item
                    for item in document.records
                    if item.role_id.casefold() == role_id.casefold()
                ),
                key=lambda item: (item.last_used_at, item.record_id),
                default=None,
            )
            record_id = "usage-" + canonical_digest(
                {
                    "product_id": key[0],
                    "preset_digest": key[1],
                    "role_id": key[2],
                }
            )[:24]
            if previous is None:
                record = SoundHistoryRecord(
                    record_id=record_id,
                    product_id=product_id,
                    preset_identity_digest=preset_identity_digest,
                    preset_name=preset_name,
                    role_id=role_id,
                    style_tags=tags,
                    first_used_at=stamp,
                    last_used_at=stamp,
                    usage_count=1,
                    consecutive_use_count=(
                        _increment_counter(latest_for_role.consecutive_use_count)
                        if latest_for_role is not None
                        and self._record_key(
                            latest_for_role.product_id,
                            latest_for_role.preset_identity_digest,
                            latest_for_role.role_id,
                        )
                        == key
                        else 1
                    ),
                    last_palette_digest=palette_digest,
                )
            else:
                record = previous.model_copy(
                    update={
                        "preset_name": preset_name or previous.preset_name,
                        "style_tags": tuple(sorted(set((*previous.style_tags, *tags)))),
                        "last_used_at": stamp,
                        "usage_count": _increment_counter(previous.usage_count),
                        "consecutive_use_count": (
                            _increment_counter(previous.consecutive_use_count)
                            if latest_for_role is not None
                            and self._record_key(
                                latest_for_role.product_id,
                                latest_for_role.preset_identity_digest,
                                latest_for_role.role_id,
                            )
                            == key
                            else 1
                        ),
                        "last_palette_digest": palette_digest or previous.last_palette_digest,
                    }
                )
            # Remove every matching logical key, including a legacy file that
            # used case-sensitive record IDs.  This prevents duplicate usage
            # rows from surviving a case-only update.
            records = [
                item
                for item in document.records
                if self._record_key(
                    item.product_id, item.preset_identity_digest, item.role_id
                )
                != key
            ]
            records.append(record)
            records.sort(key=lambda item: (-item.last_used_at.timestamp(), item.record_id))
            records = records[: self.max_records]
            updated = document.model_copy(
                update={
                    "created_at": min(document.created_at, stamp),
                    "updated_at": stamp,
                    "records": tuple(records),
                }
            )
            self._write_locked(updated)
            return True

    def record_feedback(
        self,
        *,
        palette_id: str,
        verdict: HistoryVerdict,
        role_id: str | None = None,
        product_id: str | None = None,
        preset_identity_digest: str | None = None,
        descriptors: Iterable[str] = (),
        note: str | None = None,
        now: datetime | None = None,
        persist: bool = True,
    ) -> bool:
        """Store explicit feedback and update matching aggregate counters."""

        if not persist:
            return False
        if not isinstance(palette_id, str) or not palette_id.strip():
            raise ValueError("palette_id must contain text")
        palette_id = palette_id.strip()
        if role_id is not None:
            if not isinstance(role_id, str) or not role_id.strip():
                raise ValueError("role_id must contain text when supplied")
            role_id = role_id.strip()
        if product_id is not None:
            if not isinstance(product_id, str) or not product_id.strip():
                raise ValueError("product_id must contain text when supplied")
            product_id = product_id.strip().casefold()
        if (product_id is None) != (preset_identity_digest is None):
            raise ValueError("product_id and preset_identity_digest must be supplied together")
        if preset_identity_digest is not None and not _DIGEST_RE.fullmatch(
            preset_identity_digest
        ):
            raise ValueError("preset_identity_digest must be a SHA-256 digest")
        if preset_identity_digest is not None:
            preset_identity_digest = preset_identity_digest.lower()
        stamp = _ensure_utc(now)
        tags = tuple(sorted({item.strip() for item in descriptors if item.strip()}))
        if len(tags) > 64:
            raise ValueError("feedback descriptors exceed history bounds")
        clean_note = None if note is None else note.strip()[:MAX_HISTORY_NOTE]
        with self._lock:
            document = self._require_writable_locked()
            if document is None:
                raise HistoryCorruptionError(
                    "Sound Selection history is corrupt or unreadable; "
                    "the source was left untouched and no feedback was recorded"
                )
            feedback_id = "feedback-" + canonical_digest(
                {
                    "palette_id": palette_id,
                    "role_id": role_id,
                    "product_id": product_id,
                    "preset_identity_digest": preset_identity_digest,
                    "verdict": verdict,
                    "recorded_at": stamp.isoformat(),
                }
            )[:24]
            feedback = SoundHistoryFeedback(
                feedback_id=feedback_id,
                palette_id=palette_id,
                role_id=role_id,
                product_id=product_id,
                preset_identity_digest=preset_identity_digest,
                verdict=verdict,
                descriptors=tags,
                note=clean_note or None,
                recorded_at=stamp,
            )
            feedback_rows = [item for item in document.feedback if item.feedback_id != feedback_id]
            feedback_rows.append(feedback)
            feedback_rows.sort(key=lambda item: (-item.recorded_at.timestamp(), item.feedback_id))
            feedback_rows = feedback_rows[: self.max_feedback]
            records = list(document.records)
            if product_id is not None and preset_identity_digest is not None and role_id is not None:
                key = self._record_key(product_id, preset_identity_digest, role_id)
                for index, record in enumerate(records):
                    if self._record_key(
                        record.product_id, record.preset_identity_digest, record.role_id
                    ) != key:
                        continue
                    records[index] = record.model_copy(
                        update={
                            "accepted_count": (
                                _increment_counter(record.accepted_count)
                                if verdict == "accepted"
                                else record.accepted_count
                            ),
                            "rejected_count": (
                                _increment_counter(record.rejected_count)
                                if verdict == "rejected"
                                else record.rejected_count
                            ),
                            "last_feedback_at": stamp,
                        }
                    )
                    break
            updated = document.model_copy(
                update={
                    "created_at": min(document.created_at, stamp),
                    "updated_at": stamp,
                    "records": tuple(records),
                    "feedback": tuple(feedback_rows),
                }
            )
            self._write_locked(updated)
            return True

    def reset(self) -> SoundHistoryResetResult:
        """Explicitly remove the history file; unlike reads, reset is destructive."""

        with self._lock:
            existed = self.path.exists()
            removed = False
            warnings: list[str] = []
            if existed:
                try:
                    self.path.unlink()
                    removed = True
                except OSError as exc:
                    warnings.append(f"history reset could not remove file: {exc}")
            self._document = self._empty_document()
            self._corrupt = False
            self._error = None
            self._warnings = tuple(warnings)
            return SoundHistoryResetResult(
                path=os.fspath(self.path),
                existed=existed,
                removed=removed,
                recoverable=False,
                warnings=tuple(warnings),
            )

    history_reset = reset

    def repair_corrupt(self) -> SoundHistoryResetResult:
        """Explicitly discard a corrupt source after the caller has chosen repair."""

        return self.reset()


SoundSelectionHistory = LocalSoundSelectionHistory
SelectionHistoryStore = LocalSoundSelectionHistory
BoundedSoundSelectionHistory = LocalSoundSelectionHistory


__all__ = [
    "BoundedSoundSelectionHistory",
    "DEFAULT_HISTORY_FILENAME",
    "DEFAULT_MAX_FEEDBACK",
    "DEFAULT_MAX_RECORDS",
    "MAX_HISTORY_FEEDBACK",
    "MAX_HISTORY_COUNTER",
    "MAX_HISTORY_RECORDS",
    "MAX_HISTORY_SERIALIZED_BYTES",
    "HISTORY_PATH_ENV",
    "HISTORY_SCHEMA_VERSION",
    "HistoryCorruptionError",
    "HistoryWriteError",
    "LocalSoundSelectionHistory",
    "SelectionHistoryStore",
    "SoundHistoryDocument",
    "SoundHistoryFeedback",
    "SoundHistoryRecord",
    "SoundHistoryResetResult",
    "SoundHistoryStatus",
    "SoundSelectionHistory",
    "resolve_history_path",
]
