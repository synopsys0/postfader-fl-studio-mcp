"""Bounded, local persistence for Creation Review Sessions.

The store is intentionally small and boring.  It uses one versioned JSON
document, a per-path re-entrant lock, and an atomic ``os.replace``.  Reads of
an invalid document isolate the corruption in memory and leave the original
file untouched; callers must explicitly reset it before a replacement can be
written.  Audio samples, prompts, transcripts, credentials, and network
identifiers are never part of the durable contract.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel, ValidationError

from ..host_config import fl_studio_user_data_dir
from .models import (
    CREATION_REVIEW_SCHEMA_VERSION,
    MAX_REVIEW_ASSETS,
    MAX_REVIEW_ASSET_SETS,
    MAX_REVIEW_COMPARISONS,
    MAX_REVIEW_EVALUATIONS,
    MAX_REVIEW_FEEDBACK,
    MAX_REVIEW_FINDINGS,
    MAX_REVIEW_MANIFESTS,
    MAX_REVIEW_PASSES,
    MAX_REVIEW_SERIALIZED_BYTES,
    MAX_REVIEW_SESSIONS,
    MAX_REVIEW_TEXT,
    ReviewSession,
    ReviewSessionDocument,
    ReviewStoreResetResult,
    ReviewStoreStatus,
)


REVIEW_SESSION_PATH_ENV = "POSTFADER_CREATION_REVIEW_PATH"
REVIEW_SESSION_PATH_ENV_ALIASES = (
    REVIEW_SESSION_PATH_ENV,
    "POSTFADER_CREATION_REVIEW_SESSIONS_PATH",
)
DEFAULT_REVIEW_SESSION_FILENAME = "creation-review-sessions-v1.json"
REVIEW_SESSION_SCHEMA_VERSION = CREATION_REVIEW_SCHEMA_VERSION
DEFAULT_MAX_REVIEW_SESSIONS = MAX_REVIEW_SESSIONS
DEFAULT_MAX_REVIEW_PASSES = MAX_REVIEW_PASSES
DEFAULT_MAX_REVIEW_ASSETS = MAX_REVIEW_ASSETS
DEFAULT_MAX_REVIEW_FINDINGS = MAX_REVIEW_FINDINGS
DEFAULT_MAX_REVIEW_FEEDBACK = MAX_REVIEW_FEEDBACK
DEFAULT_MAX_REVIEW_ASSET_SETS = MAX_REVIEW_ASSET_SETS
DEFAULT_MAX_REVIEW_EVALUATIONS = MAX_REVIEW_EVALUATIONS
DEFAULT_MAX_REVIEW_COMPARISONS = MAX_REVIEW_COMPARISONS
DEFAULT_MAX_REVIEW_MANIFESTS = MAX_REVIEW_MANIFESTS


class ReviewSessionCorruptionError(RuntimeError):
    """The source document is malformed and needs explicit repair/reset."""


class ReviewSessionWriteError(RuntimeError):
    """An atomic local write could not be completed."""


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


@contextmanager
def _exclusive_store_lock(path: Path) -> Iterator[None]:
    """Serialize writers across MCP processes with a private sidecar lock."""

    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    lock_path = parent / f".{path.name}.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    except (AttributeError, OSError):
        pass
    try:
        if os.name == "nt":  # pragma: no cover - exercised on Windows hosts
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def resolve_review_session_path(
    path: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    user_data_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Resolve an absolute override or the normal FL Studio user-data path."""

    if path is not None and os.fspath(path).strip():
        raw = os.fspath(path).strip()
    else:
        environment = os.environ if environ is None else environ
        raw = ""
        for key in REVIEW_SESSION_PATH_ENV_ALIASES:
            configured = environment.get(key, "").strip()
            if configured:
                raw = configured
                break
        if not raw:
            root = (
                Path(user_data_dir)
                if user_data_dir is not None
                else fl_studio_user_data_dir()
            )
            return (root / "Settings" / "PostFader" / DEFAULT_REVIEW_SESSION_FILENAME).resolve()
    resolved = Path(raw)
    if not resolved.is_absolute():
        raise ValueError("Creation Review session path must be absolute")
    return resolved.resolve()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime | None) -> datetime:
    stamp = _utc_now() if value is None else value
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


_DROP = object()

# These are field names, not broad substring checks: ``source_run_id`` and
# ``session_fingerprint`` are functional local review state and must survive,
# while provider credentials and model transcripts must not.
_SENSITIVE_FIELDS = frozenset(
    {
        "api_key",
        "apikey",
        "api_secret",
        "auth",
        "authorization",
        "bearer",
        "client_secret",
        "credential",
        "credentials",
        "cookie",
        "password",
        "passphrase",
        "private_key",
        "refresh_token",
        "secret",
        "secret_key",
        "session_token",
        "token",
        "tokens",
        "access_key",
        "access_token",
        "id_token",
        "oauth_token",
        "prompt",
        "prompts",
        "raw_prompt",
        "system_prompt",
        "user_prompt",
        "prompt_text",
        "transcript",
        "transcripts",
        "raw_transcript",
        "tool_transcript",
        "conversation",
        "conversation_id",
        "conversation_history",
        "chat_history",
        "messages",
        "cloud_id",
        "cloud_identifier",
        "cloud_conversation_id",
        "cloud_thread_id",
        "cloud_project_id",
        "cloud_run_id",
        "cloud_session_id",
        "cloud_task_id",
        "remote_id",
        "remote_thread_id",
        "external_id",
        "account_id",
        "organization_id",
        "workspace_id",
        "tenant_id",
        "thread_id",
        "chat_id",
    }
)
_AUDIO_PAYLOAD_FIELDS = frozenset(
    {
        "audio_bytes",
        "audio_data",
        "encoded_audio",
        "pcm",
        "raw_audio",
        "raw_samples",
        "samples",
        "sample_bytes",
        "waveform",
        "wav_blob",
        "audio_blob",
        "encoded",
        "binary",
        "blob",
    }
)
_INLINE_SECRET = re.compile(
    r"(?i)(\b(?:api[_ -]?(?:key|token|secret)|token|secret|password|authorization|bearer)\b\s*[:=]?\s*)(?:bearer\s+)?[^\s,;]+"
)
_KNOWN_SECRET_VALUE = re.compile(
    r"(?i)\b(?:sk|pk|tok|pw|ghp|glpat|xox[baprs]-)[A-Za-z0-9_\-]{8,}\b"
)
_ENCODED_AUDIO_VALUE = re.compile(
    r"(?i)^(?:data:audio/|UklGR|Rk9STQ|SUQz|T2dnUw|ZkxhQ|AAAAIGZ0eXB)"
)
_ABSOLUTE_PATH_IN_TEXT = re.compile(
    r"(?<![A-Za-z0-9:])(?:/(?:[^/\s]+/)+[^\s,;)}\]]+|[A-Za-z]:[\\/](?:[^\\/\s]+[\\/])+[^\s,;)}\]]+)"
)
_ASSET_CONTAINER_FIELDS = frozenset({"assets", "review_assets"})


def _normalized_field_name(value: object) -> str:
    # Split camelCase before punctuation normalization so API integrations
    # cannot bypass the field-name policy with spellings such as ``apiKey``
    # or ``cloudThreadId``.
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value))
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")


def _sensitive_field(key: str) -> bool:
    normalized = _normalized_field_name(key)
    if normalized in {"authorization_count"}:
        return False
    if normalized in _SENSITIVE_FIELDS:
        return True
    if normalized in {"cloud", "remote", "external"}:
        return True
    if normalized.startswith("cloud_") or normalized.endswith("_cloud_id"):
        return True
    if "cloud" in normalized and (
        "id" in normalized or "identifier" in normalized
    ):
        return True
    parts = frozenset(normalized.split("_"))
    if parts.intersection(
        {
            "authorization",
            "bearer",
            "blob",
            "cookie",
            "credential",
            "credentials",
            "encoded",
            "environment",
            "password",
            "passphrase",
            "prompt",
            "secret",
            "token",
            "transcript",
        }
    ):
        return True
    if normalized.startswith(
        ("api_key_", "apikey_", "secret_", "credential_", "token_")
    ):
        return True
    if normalized.endswith(
        ("_api_key", "_apikey", "_secret", "_token", "_password")
    ) or any(
        marker in normalized
        for marker in ("_access_key", "_client_secret", "_authorization")
    ):
        return True
    return normalized in _AUDIO_PAYLOAD_FIELDS


def _looks_like_absolute_path(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return (
        value.startswith("/")
        or value.startswith("\\\\")
        or bool(re.match(r"^[A-Za-z]:[\\/]", value))
    )


def _redact_value(
    value: object,
    *,
    persist_asset_paths: bool,
    field_path: tuple[str, ...] = (),
    root_asset: bool = False,
    trusted_model: bool = False,
    depth: int = 0,
) -> object:
    """Return JSON-safe durable state with secrets and raw media removed."""

    if depth > 12:
        return _DROP
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in {float("inf"), float("-inf")} else _DROP
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _DROP
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if isinstance(value, str):
        is_asset_path = (
            persist_asset_paths
            and bool(field_path)
            and field_path[-1] == "path"
            and (
                root_asset
                or (
                    len(field_path) == 2
                    and field_path[-2] in _ASSET_CONTAINER_FIELDS
                )
            )
        )
        if _looks_like_absolute_path(value) and not is_asset_path:
            return "[PATH REDACTED]"
        if _ENCODED_AUDIO_VALUE.match(value.strip()):
            return "[AUDIO PAYLOAD REDACTED]"
        if len(value) > MAX_REVIEW_TEXT:
            value = value[:MAX_REVIEW_TEXT]
        if not is_asset_path:
            value = _ABSOLUTE_PATH_IN_TEXT.sub("[PATH REDACTED]", value)
        value = _INLINE_SECRET.sub(r"\1[REDACTED]", value)
        return _KNOWN_SECRET_VALUE.sub("[REDACTED]", value)
    if isinstance(value, BaseModel):
        is_root_asset = value.__class__.__name__ == "ReviewAudioAsset"
        try:
            value = value.model_dump(mode="python", exclude_none=False)
        except (TypeError, ValueError):
            return _DROP
        root_asset = root_asset or is_root_asset
        trusted_model = True
    if isinstance(value, Mapping):
        mapping_value: Mapping[str, object] = {
            key: item for key, item in value.items() if isinstance(key, str)
        }
        keys = tuple(mapping_value)
        mapping_result: dict[str, object] = {}
        selected_keys = keys if trusted_model else keys[:MAX_REVIEW_ASSETS]
        for key in selected_keys:
            normalized = _normalized_field_name(key)
            if _sensitive_field(key):
                continue
            if (
                normalized == "path" or normalized.endswith("_path")
            ) and _looks_like_absolute_path(mapping_value[key]) and not (
                persist_asset_paths
                and normalized == "path"
                and (
                    root_asset
                    or (
                        len(field_path) == 1
                        and field_path[-1] in _ASSET_CONTAINER_FIELDS
                    )
                )
            ):
                continue
            item = _redact_value(
                mapping_value[key],
                persist_asset_paths=persist_asset_paths,
                field_path=(*field_path, normalized),
                root_asset=root_asset,
                trusted_model=trusted_model,
                depth=depth + 1,
            )
            if item is not _DROP:
                mapping_result[key] = item
        return mapping_result
    if isinstance(value, (list, tuple)):
        sequence_result: list[object] = []
        selected_values = value if trusted_model else value[:MAX_REVIEW_ASSETS]
        for item in selected_values:
            redacted = _redact_value(
                item,
                persist_asset_paths=persist_asset_paths,
                field_path=field_path,
                root_asset=root_asset,
                trusted_model=trusted_model,
                depth=depth + 1,
            )
            if redacted is not _DROP:
                sequence_result.append(redacted)
        return sequence_result
    # Arbitrary object fields are a compatibility escape hatch in old
    # feedback records.  Never stringify them: reprs can expose credentials
    # or retain opaque audio buffers.
    return _DROP


def _asset_path_redaction(value: object) -> object:
    """Compatibility helper retaining the historical path-only entry point."""

    redacted = _redact_value(value, persist_asset_paths=False)
    return {} if redacted is _DROP else redacted


def sanitize_review_payload(
    value: object,
    *,
    persist_asset_paths: bool = False,
) -> object:
    """Return bounded JSON-safe review data without private payload material.

    Absolute paths are retained only for canonical ``ReviewAudioAsset.path``
    fields when the caller opts in.  Arbitrary metadata cannot turn path
    retention into a general filesystem-data escape hatch.
    """

    redacted = _redact_value(
        value,
        persist_asset_paths=persist_asset_paths,
    )
    if redacted is _DROP:
        raise ReviewSessionWriteError(
            "Creation Review payload contains no durable JSON-safe data"
        )
    return redacted


def _redacted_payload(session: ReviewSession) -> dict[str, object]:
    redacted = sanitize_review_payload(
        session,
        persist_asset_paths=session.request.persist_asset_paths,
    )
    if not isinstance(redacted, dict):
        raise ReviewSessionWriteError("Creation Review persistence contains no JSON-safe session payload")
    return redacted


class LocalReviewSessionStore:
    """Thread-safe bounded JSON store for Review Sessions."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        max_sessions: int = DEFAULT_MAX_REVIEW_SESSIONS,
        max_revision_passes: int = DEFAULT_MAX_REVIEW_PASSES,
        max_assets: int = DEFAULT_MAX_REVIEW_ASSETS,
        max_findings: int = DEFAULT_MAX_REVIEW_FINDINGS,
        max_feedback: int = DEFAULT_MAX_REVIEW_FEEDBACK,
        max_asset_sets: int = DEFAULT_MAX_REVIEW_ASSET_SETS,
        max_evaluations: int = DEFAULT_MAX_REVIEW_EVALUATIONS,
        max_comparisons: int = DEFAULT_MAX_REVIEW_COMPARISONS,
        max_manifests: int = DEFAULT_MAX_REVIEW_MANIFESTS,
        environ: Mapping[str, str] | None = None,
        user_data_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        if type(max_sessions) is not int or not 1 <= max_sessions <= MAX_REVIEW_SESSIONS:
            raise ValueError("max_sessions is outside Creation Review bounds")
        if type(max_revision_passes) is not int or not 0 <= max_revision_passes <= MAX_REVIEW_PASSES:
            raise ValueError("max_revision_passes is outside Creation Review bounds")
        if type(max_assets) is not int or not 1 <= max_assets <= MAX_REVIEW_ASSETS:
            raise ValueError("max_assets is outside Creation Review bounds")
        if type(max_findings) is not int or not 1 <= max_findings <= MAX_REVIEW_FINDINGS:
            raise ValueError("max_findings is outside Creation Review bounds")
        if type(max_feedback) is not int or not 1 <= max_feedback <= MAX_REVIEW_FEEDBACK:
            raise ValueError("max_feedback is outside Creation Review bounds")
        if type(max_asset_sets) is not int or not 1 <= max_asset_sets <= MAX_REVIEW_ASSET_SETS:
            raise ValueError("max_asset_sets is outside Creation Review bounds")
        if type(max_evaluations) is not int or not 1 <= max_evaluations <= MAX_REVIEW_EVALUATIONS:
            raise ValueError("max_evaluations is outside Creation Review bounds")
        if type(max_comparisons) is not int or not 1 <= max_comparisons <= MAX_REVIEW_COMPARISONS:
            raise ValueError("max_comparisons is outside Creation Review bounds")
        if type(max_manifests) is not int or not 1 <= max_manifests <= MAX_REVIEW_MANIFESTS:
            raise ValueError("max_manifests is outside Creation Review bounds")
        self.path = resolve_review_session_path(
            path, environ=environ, user_data_dir=user_data_dir
        )
        self.max_sessions = max_sessions
        self.max_revision_passes = max_revision_passes
        self.max_assets = max_assets
        self.max_findings = max_findings
        self.max_feedback = max_feedback
        self.max_asset_sets = max_asset_sets
        self.max_evaluations = max_evaluations
        self.max_comparisons = max_comparisons
        self.max_manifests = max_manifests
        self._lock = _path_lock(self.path)
        self._document: ReviewSessionDocument | None = None
        self._corrupt = False
        self._error: str | None = None
        self._warnings: tuple[str, ...] = ()
        with self._lock:
            self._load_locked()

    @property
    def session_path(self) -> Path:
        return self.path

    def _empty_document(self, *, now: datetime | None = None) -> ReviewSessionDocument:
        stamp = _ensure_utc(now)
        return ReviewSessionDocument(created_at=stamp, updated_at=stamp)

    def _load_locked(self) -> None:
        if not self.path.exists():
            self._document = self._empty_document()
            self._corrupt = False
            self._error = None
            self._warnings = ()
            return
        try:
            with self.path.open("rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                if size > MAX_REVIEW_SERIALIZED_BYTES:
                    raise ValueError("Creation Review store exceeds the 16 MiB safety bound")
                encoded = handle.read(MAX_REVIEW_SERIALIZED_BYTES + 1)
            if len(encoded) > MAX_REVIEW_SERIALIZED_BYTES:
                raise ValueError("Creation Review store exceeds the 16 MiB safety bound")
            document = ReviewSessionDocument.model_validate_json(
                encoded.decode("utf-8"), strict=False
            )
            bounded_sessions = [self._bound_session(item) for item in document.sessions]
            bounded_sessions = list(self._prune_sessions(bounded_sessions, self.max_sessions))
            if tuple(bounded_sessions) != document.sessions:
                document = ReviewSessionDocument(
                    schema_version=document.schema_version,
                    created_at=document.created_at,
                    updated_at=document.updated_at,
                    sessions=tuple(bounded_sessions),
                )
        except (OSError, UnicodeError, ValueError, ValidationError) as exc:
            self._document = None
            self._corrupt = True
            self._error = str(exc)[:4096]
            self._warnings = (
                "Creation Review persistence is corrupt or unreadable; the file was left untouched.",
            )
            return
        self._document = document
        self._corrupt = False
        self._error = None
        self._warnings = ()

    def _refresh_locked(self) -> None:
        self._load_locked()

    def _require_writable_locked(self) -> ReviewSessionDocument:
        self._refresh_locked()
        if self._corrupt or self._document is None:
            raise ReviewSessionCorruptionError(
                "Creation Review persistence is corrupt or unreadable; reset it explicitly before writing"
            )
        return self._document

    def status(self) -> ReviewStoreStatus:
        with self._lock:
            self._load_locked()
            document = self._document
            return ReviewStoreStatus(
                path=os.fspath(self.path),
                exists=self.path.exists(),
                healthy=not self._corrupt,
                corrupt=self._corrupt,
                schema_version=None if document is None else document.schema_version,
                session_count=0 if document is None else len(document.sessions),
                max_sessions=self.max_sessions,
                max_revision_passes=self.max_revision_passes,
                max_assets=self.max_assets,
                max_findings=self.max_findings,
                max_feedback=self.max_feedback,
                max_asset_sets=self.max_asset_sets,
                max_evaluations=self.max_evaluations,
                max_comparisons=self.max_comparisons,
                max_manifests=self.max_manifests,
                warnings=self._warnings,
                error=self._error,
            )

    review_session_status = status

    def snapshot(self) -> tuple[ReviewSession, ...]:
        with self._lock:
            self._load_locked()
            return () if self._document is None else self._document.sessions

    sessions = snapshot

    def get(self, review_session_id: str) -> ReviewSession | None:
        return next(
            (item for item in self.snapshot() if item.review_session_id == review_session_id),
            None,
        )

    lookup = get

    def _bound_session(self, session: ReviewSession) -> ReviewSession:
        """Apply deterministic per-session limits before a durable write."""

        updates: dict[str, object] = {}
        retained_asset_ids = {item.asset_id for item in session.assets}
        if len(session.assets) > self.max_assets:
            retained_assets = tuple(session.assets[-self.max_assets :])
            retained_asset_ids = {item.asset_id for item in retained_assets}
            updates["assets"] = retained_assets
        if len(session.revision_passes) > self.max_revision_passes:
            updates["revision_passes"] = tuple(
                session.revision_passes[-self.max_revision_passes :]
            ) if self.max_revision_passes else ()
        if len(session.revision_plans) > self.max_revision_passes:
            updates["revision_plans"] = tuple(
                session.revision_plans[-self.max_revision_passes :]
            ) if self.max_revision_passes else ()
        if len(session.feedback) > self.max_feedback:
            updates["feedback"] = tuple(session.feedback[-self.max_feedback :])
        bounded_asset_sets = (
            tuple(session.asset_sets[-self.max_asset_sets :])
            if len(session.asset_sets) > self.max_asset_sets
            else session.asset_sets
        )
        if len(session.evaluations) > self.max_evaluations:
            updates["evaluations"] = tuple(session.evaluations[-self.max_evaluations :])
        bounded_comparisons = (
            tuple(session.comparisons[-self.max_comparisons :])
            if len(session.comparisons) > self.max_comparisons
            else session.comparisons
        )
        if len(session.delivery_manifests) > self.max_manifests:
            updates["delivery_manifests"] = tuple(
                session.delivery_manifests[-self.max_manifests :]
            )
        # Top-level assets are the authoritative lookup surface after a
        # restart.  Never persist an asset set or comparison that points to an
        # asset removed by a tighter configured bound.
        bounded_asset_sets = tuple(
            item
            for item in bounded_asset_sets
            if all(asset.asset_id in retained_asset_ids for asset in item.assets)
        )
        if bounded_asset_sets != session.asset_sets:
            updates["asset_sets"] = bounded_asset_sets
        bounded_comparisons = tuple(
            item
            for item in bounded_comparisons
            if item.before_asset.asset_id in retained_asset_ids
            and item.after_asset.asset_id in retained_asset_ids
        )
        if bounded_comparisons != session.comparisons:
            updates["comparisons"] = bounded_comparisons
        # Evaluations are retained as records; trim findings on every report
        # (not only when the number of reports exceeds the findings bound) so
        # a large individual report cannot bypass the store limit.
        evaluation_rows = (
            session.evaluations[-self.max_evaluations :]
            if len(session.evaluations) > self.max_evaluations
            else session.evaluations
        )
        reports = []
        reports_changed = False
        referenced_finding_ids = {
            finding_id
            for plan in session.revision_plans
            for finding_id in (
                *plan.targeted_findings,
                *(value for operation in plan.operations for value in operation.finding_ids),
            )
        }
        referenced_finding_ids.update(
            finding_id
            for revision_pass in session.revision_passes
            for receipt in revision_pass.operation_receipts
            for finding_id in receipt.finding_ids
        )
        for report in evaluation_rows:
            if len(report.findings) <= self.max_findings:
                reports.append(report)
                continue
            reports_changed = True
            # Evaluation findings are already ranked with explicit feedback
            # and critical technical failures first.  Pin any finding cited
            # by a retained plan/pass, then fill the remaining bounded slots
            # in that authoritative order.  This avoids dangling traceability
            # without reverting to the least-important tail of the report.
            cited = tuple(
                item
                for item in report.findings
                if item.finding_id in referenced_finding_ids
            )
            uncited = tuple(
                item
                for item in report.findings
                if item.finding_id not in referenced_finding_ids
            )
            findings = (*cited, *uncited)[: self.max_findings]
            ids = {item.finding_id for item in findings}
            reports.append(
                report.model_copy(
                    update={
                        "findings": findings,
                        "top_priorities": tuple(
                            item for item in report.top_priorities if item in ids
                        ),
                    }
                )
            )
        if reports_changed or len(evaluation_rows) != len(session.evaluations):
            updates["evaluations"] = tuple(reports)
        return session if not updates else session.model_copy(update=updates)

    @staticmethod
    def _prune_sessions(
        sessions: list[ReviewSession], max_sessions: int
    ) -> tuple[ReviewSession, ...]:
        if len(sessions) <= max_sessions:
            return tuple(sessions)
        terminal = {"accepted", "completed", "rejected", "stopped", "blocked"}
        # Old terminal sessions are removed before active sessions.  Every tie
        # is resolved by the stable session ID, making pruning reproducible.
        removal_order = sorted(
            sessions,
            key=lambda item: (
                0 if item.status in terminal else 1,
                item.updated_at,
                item.review_session_id,
            ),
        )
        remove = {item.review_session_id for item in removal_order[: len(sessions) - max_sessions]}
        kept = [item for item in sessions if item.review_session_id not in remove]
        kept.sort(key=lambda item: (item.updated_at, item.review_session_id))
        return tuple(kept)

    def _write_locked(self, document: ReviewSessionDocument) -> None:
        # Serialize each session independently so path retention is controlled
        # by that session's request.  This also keeps the durable envelope free
        # of any accidental future audio payload field.
        payload_dict = {
            "schema_version": document.schema_version,
            "created_at": document.created_at.isoformat(),
            "updated_at": document.updated_at.isoformat(),
            "sessions": [_redacted_payload(item) for item in document.sessions],
        }
        payload = json.dumps(payload_dict, indent=2, ensure_ascii=True).encode("utf-8")
        if len(payload) > MAX_REVIEW_SERIALIZED_BYTES:
            raise ReviewSessionWriteError(
                "serialized Creation Review persistence exceeds the 16 MiB safety bound"
            )
        parent = self.path.parent
        descriptor = -1
        temporary = ""
        try:
            parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=os.fspath(parent)
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = ""
        except OSError as exc:
            raise ReviewSessionWriteError(
                f"cannot atomically write Creation Review persistence: {exc}"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        self._document = document
        self._corrupt = False
        self._error = None
        durability_warning: tuple[str, ...] = ()
        try:
            directory_fd = os.open(os.fspath(parent), os.O_DIRECTORY)
        except (AttributeError, OSError):
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            except OSError as exc:
                # The atomic replace already committed.  Reporting a write
                # failure here would invite an unsafe replay by the caller.
                durability_warning = (
                    f"Creation Review data was committed, but directory durability could not be confirmed: {exc}",
                )
            finally:
                os.close(directory_fd)
        self._warnings = durability_warning

    def save(self, session: ReviewSession) -> ReviewSession:
        """Create or replace one session, pruning deterministically if needed."""

        if not isinstance(session, ReviewSession):
            raise TypeError("save expects a ReviewSession")
        bounded = self._bound_session(session)
        with self._lock:
            with _exclusive_store_lock(self.path):
                document = self._require_writable_locked()
                rows = [item for item in document.sessions if item.review_session_id != bounded.review_session_id]
                rows.append(bounded)
                rows = list(self._prune_sessions(rows, self.max_sessions))
                stamp = max(document.updated_at, bounded.updated_at, _utc_now())
                updated = ReviewSessionDocument(
                    created_at=min(document.created_at, bounded.created_at),
                    updated_at=stamp,
                    sessions=tuple(rows),
                )
                self._write_locked(updated)
                return bounded

    upsert = save

    def delete(self, review_session_id: str, *, explicit: bool = False) -> bool:
        """Delete one session only when the caller explicitly opts in."""

        if not explicit:
            raise ValueError("deleting a Review Session requires explicit=True")
        with self._lock:
            with _exclusive_store_lock(self.path):
                document = self._require_writable_locked()
                rows = tuple(item for item in document.sessions if item.review_session_id != review_session_id)
                if len(rows) == len(document.sessions):
                    return False
                updated = ReviewSessionDocument(
                    created_at=document.created_at,
                    updated_at=max(document.updated_at, _utc_now()),
                    sessions=rows,
                )
                self._write_locked(updated)
                return True

    def reset(self, *, explicit: bool = False) -> ReviewStoreResetResult:
        """Explicitly remove the source file; malformed data remains recoverable until this call."""

        if not explicit:
            raise ValueError("resetting Review Session persistence requires explicit=True")
        with self._lock:
            with _exclusive_store_lock(self.path):
                existed = self.path.exists()
                removed = False
                warnings: list[str] = []
                if existed:
                    try:
                        self.path.unlink()
                        removed = True
                    except OSError as exc:
                        warnings.append(f"Creation Review reset could not remove file: {exc}")
                self._document = self._empty_document()
                self._corrupt = False
                self._error = None
                self._warnings = tuple(warnings)
                return ReviewStoreResetResult(
                    path=os.fspath(self.path),
                    existed=existed,
                    removed=removed,
                    warnings=tuple(warnings),
                )

    review_session_reset = reset
    repair_corrupt = reset


ReviewSessionStore = LocalReviewSessionStore
CreationReviewSessionStore = LocalReviewSessionStore
BoundedReviewSessionStore = LocalReviewSessionStore


__all__ = [
    "BoundedReviewSessionStore",
    "CreationReviewSessionStore",
    "DEFAULT_MAX_REVIEW_ASSETS",
    "DEFAULT_MAX_REVIEW_ASSET_SETS",
    "DEFAULT_MAX_REVIEW_COMPARISONS",
    "DEFAULT_MAX_REVIEW_EVALUATIONS",
    "DEFAULT_MAX_REVIEW_FEEDBACK",
    "DEFAULT_MAX_REVIEW_FINDINGS",
    "DEFAULT_MAX_REVIEW_MANIFESTS",
    "DEFAULT_MAX_REVIEW_PASSES",
    "DEFAULT_MAX_REVIEW_SESSIONS",
    "DEFAULT_REVIEW_SESSION_FILENAME",
    "LocalReviewSessionStore",
    "REVIEW_SESSION_PATH_ENV",
    "REVIEW_SESSION_PATH_ENV_ALIASES",
    "REVIEW_SESSION_SCHEMA_VERSION",
    "ReviewSessionCorruptionError",
    "ReviewSessionStore",
    "ReviewSessionWriteError",
    "resolve_review_session_path",
    "sanitize_review_payload",
]
