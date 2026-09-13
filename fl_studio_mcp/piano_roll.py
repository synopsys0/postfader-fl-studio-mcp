"""Structured note inspection through FL's existing Piano Roll script channel."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict

from pydantic import Field, computed_field, model_validator

from .bridge_client import get_client
from .creative import (
    PIANO_ROLL,
    PIANO_ROLL_RECEIPT_WAIT_SECONDS,
    PIANO_ROLL_SCRIPT_NAME,
    _PIANO_ROLL_DISPATCH_LOCK,
    CreativeModel,
    HotkeyDispatch,
    PianoRollTargetReceipt,
    _atomic_text,
    _now,
    _piano_roll_receipt_path,
    _target_piano_roll,
    _trigger_piano_roll_shortcut,
    piano_roll_scripts_directory,
)


MAX_NOTE_PAGE = 2048
MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_SCORE_NOTES = 1_000_000


class PianoRollObservedNote(CreativeModel):
    """One existing note; raw ticks preserve timing without composition limits."""

    note_index: int = Field(ge=0, lt=MAX_SCORE_NOTES)
    pitch: int = Field(ge=0, le=131)
    start_ticks: int = Field(ge=-(2**31), le=2**31 - 1)
    length_ticks: int = Field(ge=0, le=2**31 - 1)
    ppq: int = Field(ge=1, le=1_000_000)
    velocity: float = Field(ge=0.0, le=1.0)
    pan: float = Field(ge=0.0, le=1.0)
    release: float = Field(ge=0.0, le=1.0)
    color: int = Field(ge=0, le=15)
    pitch_offset_tenths: int = Field(ge=-120, le=120)
    slide: bool
    portamento: bool
    muted: bool
    selected: bool

    @computed_field
    @property
    def start_beats(self) -> float:
        return self.start_ticks / self.ppq

    @computed_field
    @property
    def duration_beats(self) -> float:
        return self.length_ticks / self.ppq


class PianoRollNoteSnapshot(CreativeModel):
    schema_version: Literal["1.0"] = "1.0"
    observed_at: datetime
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    channel_index: int = Field(ge=0)
    pattern_number: int = Field(ge=1, le=999)
    offset: int = Field(ge=0, le=MAX_SCORE_NOTES)
    limit: int = Field(ge=1, le=MAX_NOTE_PAGE)
    selected_only: bool
    status: Literal["observed", "not_dispatched", "receipt_unavailable", "target_changed"]
    total_note_count: int | None = Field(default=None, ge=0, le=MAX_SCORE_NOTES)
    ppq: int | None = Field(default=None, ge=1, le=1_000_000)
    notes: tuple[PianoRollObservedNote, ...] = Field(default=(), max_length=MAX_NOTE_PAGE)
    next_offset: int | None = Field(default=None, ge=1, le=MAX_SCORE_NOTES)
    target: PianoRollTargetReceipt
    trigger: HotkeyDispatch
    source: Literal["fl_piano_roll_script_runtime"] = "fl_piano_roll_script_runtime"
    notes_modified: Literal[False] = False
    project_saved: Literal[False] = False
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_observation(self) -> PianoRollNoteSnapshot:
        if self.status == "observed":
            if self.total_note_count is None or self.ppq is None:
                raise ValueError("observed notes need a score count and PPQ")
        elif self.notes or self.total_note_count is not None or self.ppq is not None:
            raise ValueError("unverified snapshots cannot expose target-attributed notes")
        return self


class _SnapshotArgs(TypedDict):
    observed_at: datetime
    request_id: str
    channel_index: int
    pattern_number: int
    offset: int
    limit: int
    selected_only: bool
    target: PianoRollTargetReceipt
    trigger: HotkeyDispatch


class _SnapshotPayload(CreativeModel):
    request_id: str
    operation: Literal["read_notes"]
    offset: int = Field(ge=0, le=MAX_SCORE_NOTES)
    limit: int = Field(ge=1, le=MAX_NOTE_PAGE)
    selected_only: bool
    total_note_count: int = Field(ge=0, le=MAX_SCORE_NOTES)
    ppq: int = Field(ge=1, le=1_000_000)
    notes: tuple[PianoRollObservedNote, ...] = Field(max_length=MAX_NOTE_PAGE)
    script_completed: bool
    error: str | None = Field(default=None, max_length=512)


class _TargetObservation(CreativeModel):
    channel_indices: list[int]
    pattern_number: int
    piano_roll_visible: bool | None
    session_fingerprint: str


def _snapshot_script(
    *, request_id: str, receipt_path: Path, receipt_secret: str,
    offset: int, limit: int, selected_only: bool,
) -> str:
    return f'''# Script.Name = "Postfader Apply"
# Script.Category = "Postfader"
# Read-only note snapshot; no score or selection writes.
import hashlib
import hmac
import json
import os
import flpianoroll as flp
REQUEST_ID = {request_id!r}
RECEIPT_PATH = {ascii(os.fspath(receipt_path))}
RECEIPT_SECRET = {receipt_secret!r}
OFFSET = {offset}
LIMIT = {limit}
SELECTED_ONLY = {selected_only!r}

def _emit(payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    signature = hmac.new(bytes.fromhex(RECEIPT_SECRET), canonical, hashlib.sha256).hexdigest()
    encoded = json.dumps({{"payload": payload, "hmac_sha256": signature}}, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    with open(RECEIPT_PATH, "xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())

try:
    score = flp.score
    ppq = int(score.PPQ)
    count = int(score.noteCount)
    if not 1 <= ppq <= 1000000 or not 0 <= count <= {MAX_SCORE_NOTES}:
        raise ValueError("Piano Roll reported an unsupported PPQ or score size")
    notes = []
    for index in range(min(OFFSET, count), min(OFFSET + LIMIT, count)):
        note = score.getNote(index)
        if SELECTED_ONLY and not note.selected:
            continue
        notes.append({{
            "note_index": index, "pitch": int(note.number),
            "start_ticks": int(note.time), "length_ticks": int(note.length), "ppq": ppq,
            "velocity": float(note.velocity), "pan": float(note.pan), "release": float(note.release),
            "color": int(note.color), "pitch_offset_tenths": int(note.pitchofs),
            "slide": bool(note.slide), "portamento": bool(note.porta),
            "muted": bool(note.muted), "selected": bool(note.selected),
        }})
    payload = {{"request_id": REQUEST_ID, "operation": "read_notes", "offset": OFFSET,
        "limit": LIMIT, "selected_only": SELECTED_ONLY, "total_note_count": count,
        "ppq": ppq, "notes": notes, "script_completed": True, "error": None}}
except Exception as error:
    payload = {{"request_id": REQUEST_ID, "operation": "read_notes", "offset": OFFSET,
        "limit": LIMIT, "selected_only": SELECTED_ONLY, "total_note_count": 0,
        "ppq": 1, "notes": [], "script_completed": False,
        "error": (type(error).__name__ + ": " + str(error))[:512]}}
_emit(payload)
'''


def _read_snapshot_receipt(
    path: Path, *, request_id: str, receipt_secret: str,
    offset: int, limit: int, selected_only: bool,
) -> _SnapshotPayload:
    with path.open("rb") as handle:
        encoded = handle.read(MAX_SNAPSHOT_BYTES + 1)
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Piano Roll snapshot exceeded its response limit")
    envelope = json.loads(encoded.decode("ascii"))
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "hmac_sha256"}:
        raise ValueError("invalid Piano Roll snapshot envelope")
    canonical = json.dumps(envelope["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    signature = hmac.new(bytes.fromhex(receipt_secret), canonical, hashlib.sha256).hexdigest()
    reported = envelope["hmac_sha256"]
    if (
        not isinstance(reported, str)
        or not reported.isascii()
        or not hmac.compare_digest(signature, reported)
    ):
        raise ValueError("Piano Roll snapshot does not belong to this request")
    # JSON mode retains strict scalar types while accepting JSON arrays as tuples.
    payload = _SnapshotPayload.model_validate_json(canonical, strict=True)
    if (payload.request_id, payload.offset, payload.limit, payload.selected_only) != (
        request_id, offset, limit, selected_only,
    ):
        raise ValueError("Piano Roll snapshot request fields changed")
    if not payload.script_completed:
        raise ValueError(payload.error or "Piano Roll snapshot script failed")
    stop = min(offset + limit, payload.total_note_count)
    indices = [note.note_index for note in payload.notes]
    if indices != sorted(set(indices)) or any(not offset <= index < stop for index in indices):
        raise ValueError("Piano Roll snapshot contains invalid note indices")
    if any(note.ppq != payload.ppq or (selected_only and not note.selected) for note in payload.notes):
        raise ValueError("Piano Roll snapshot note metadata is inconsistent")
    if not selected_only and len(indices) != max(0, stop - offset):
        raise ValueError("Piano Roll snapshot is missing notes from its requested page")
    return payload


def read_piano_roll_notes(
    *, channel_index: int, pattern_number: int, offset: int = 0, limit: int = 512,
    selected_only: bool = False, session_fingerprint: str | None = None,
) -> PianoRollNoteSnapshot:
    """Focus the requested score and return a bounded page of existing notes.

    Editor navigation is transient and never enables the bridge's musical-write
    mode. It uses the same one-time Piano Roll script setup as note writing.
    """
    if type(offset) is not int or not 0 <= offset <= MAX_SCORE_NOTES:
        raise ValueError(f"offset must be within 0..{MAX_SCORE_NOTES}")
    if type(limit) is not int or not 1 <= limit <= MAX_NOTE_PAGE:
        raise ValueError(f"limit must be within 1..{MAX_NOTE_PAGE}")
    if type(selected_only) is not bool:
        raise ValueError("selected_only must be a boolean")
    with _PIANO_ROLL_DISPATCH_LOCK:
        PIANO_ROLL.require_armed()
        target = _target_piano_roll(channel_index, pattern_number,
            session_fingerprint=session_fingerprint, navigation_only=True)
        if not target.selected_target_verified or target.piano_roll_visibility_verified is not True:
            raise ValueError("FL could not open the requested Piano Roll target")
        request_id = os.urandom(16).hex()
        secret = os.urandom(32).hex()
        path = piano_roll_scripts_directory() / PIANO_ROLL_SCRIPT_NAME
        receipt_path = _piano_roll_receipt_path(path, request_id, phase="inspect")
        _atomic_text(path, _snapshot_script(request_id=request_id, receipt_path=receipt_path,
            receipt_secret=secret, offset=offset, limit=limit, selected_only=selected_only))
        PIANO_ROLL.record(request_id=request_id, operation="read_notes", count=None, digest=None)
        trigger = _trigger_piano_roll_shortcut()
        base: _SnapshotArgs = {
            "observed_at": _now(), "request_id": request_id, "channel_index": channel_index,
            "pattern_number": pattern_number, "offset": offset, "limit": limit,
            "selected_only": selected_only, "target": target, "trigger": trigger,
        }
        if not trigger.hotkey_dispatched:
            return PianoRollNoteSnapshot(**base, status="not_dispatched",
                warnings=(trigger.error or "FL did not receive the Piano Roll shortcut.",))
        deadline = time.monotonic() + PIANO_ROLL_RECEIPT_WAIT_SECONDS
        payload = None
        last_error = "FL has not returned the note snapshot."
        while payload is None:
            try:
                payload = _read_snapshot_receipt(receipt_path, request_id=request_id,
                    receipt_secret=secret, offset=offset, limit=limit, selected_only=selected_only)
            except (OSError, ValueError) as error:
                last_error = str(error)
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        if payload is None:
            return PianoRollNoteSnapshot(**base, status="receipt_unavailable", warnings=(last_error,))
        try:
            after = _TargetObservation.model_validate(
                get_client().call("creative.piano_roll_target"), strict=True)
            unchanged = (after.channel_indices == [channel_index]
                and after.pattern_number == pattern_number
                and after.session_fingerprint == target.session_fingerprint
                and after.piano_roll_visible is True)
        except (OSError, RuntimeError, ValueError) as error:
            return PianoRollNoteSnapshot(**base, status="target_changed", warnings=(str(error),))
        if not unchanged:
            return PianoRollNoteSnapshot(**base, status="target_changed",
                warnings=("The editor target changed during inspection; request a fresh snapshot.",))
        next_offset = min(offset + limit, payload.total_note_count)
        return PianoRollNoteSnapshot(**base, status="observed", total_note_count=payload.total_note_count,
            ppq=payload.ppq, notes=payload.notes,
            next_offset=next_offset if next_offset < payload.total_note_count else None)
