"""Reusable, process-local context snapshots for one creation run.

The context is a cache boundary, not a permanent project identity.  A caller
may reuse it across phases only while the session and the relevant target
fingerprints still match.  Refreshing a target returns a new snapshot; the
previous snapshot and any receipts derived from it remain unchanged.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import AliasChoices, Field, model_validator

from ..contracts import ProjectSummary
from ..sound_selection.models import SoundInventory
from ..track_b_contracts import SESSION_FINGERPRINT_PATTERN, PluginTarget
from .models import (
    CreationPipelineModel,
    Digest,
    Identifier,
    PatternIdentity,
)


MAX_CONTEXT_TARGETS = 256
MAX_CONTEXT_TEXT = 256
MAX_CONTEXT_RECEIPTS = 128
MAX_TEMPO_CHANGES = 64


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TempoCheckpoint(CreationPipelineModel):
    """One documented one-based tempo change retained by a run snapshot."""

    start_bar: float = Field(ge=1.0, le=1_000_000.0)
    tempo_bpm: float = Field(gt=0.0, le=522.0)


class TransportCheckpoint(CreationPipelineModel):
    """Bounded musical transport facts needed for later section mapping."""

    tempo_bpm: float | None = Field(default=None, gt=0.0, le=522.0)
    time_signature_numerator: int | None = Field(default=None, ge=1, le=32)
    time_signature_denominator: Literal[1, 2, 4, 8, 16, 32] | None = None
    tempo_changes: tuple[TempoCheckpoint, ...] = Field(
        default=(), max_length=MAX_TEMPO_CHANGES
    )

    @model_validator(mode="after")
    def validate_tempo_changes(self) -> "TransportCheckpoint":
        starts = [item.start_bar for item in self.tempo_changes]
        if starts != sorted(starts) or len(starts) != len(set(starts)):
            raise ValueError("transport tempo changes must have unique ordered start bars")
        return self


class ProjectCheckpoint(CreationPipelineModel):
    """The small project checkpoint needed for phase-boundary validation."""

    digest: Digest | None = None
    dirty_state: Literal["clean", "dirty", "autosave_dirty", "unknown"] = "unknown"
    undo_history_position: int | None = Field(default=None, ge=0)
    undo_history_count: int | None = Field(default=None, ge=0)
    transport: TransportCheckpoint | None = None


class ContextTargetIdentity(CreationPipelineModel):
    """An observation-scoped target identity held by the run cache."""

    target_id: Identifier
    kind: str = Field(min_length=1, max_length=MAX_CONTEXT_TEXT)
    fingerprint: Digest
    target: PluginTarget | None = None


class PianoRollArmingReceipt(CreationPipelineModel):
    """Minimal authenticated arming proof retained in the run context."""

    receipt_id: Identifier
    process_identity: str | None = Field(default=None, max_length=MAX_CONTEXT_TEXT)
    authenticated: bool = False
    script_present: bool = False
    captured_at: datetime | None = None


class CreationRunContextSnapshot(CreationPipelineModel):
    """Immutable cache of observations shared by all run phases."""

    schema_version: Literal["1.0"] = "1.0"
    captured_at: datetime = Field(default_factory=_now)
    mcp_process_identity: str | None = Field(default=None, max_length=MAX_CONTEXT_TEXT)
    session_fingerprint: str | None = Field(
        default=None, pattern=SESSION_FINGERPRINT_PATTERN
    )
    package_source_revision: str | None = Field(default=None, max_length=MAX_CONTEXT_TEXT)
    deployed_bridge_revision: str | None = Field(default=None, max_length=MAX_CONTEXT_TEXT)
    running_bridge_revision: str | None = Field(default=None, max_length=MAX_CONTEXT_TEXT)
    midi_endpoint: str | None = Field(default=None, max_length=MAX_CONTEXT_TEXT)
    project_checkpoint: ProjectCheckpoint = Field(default_factory=ProjectCheckpoint)
    target_fingerprints: tuple[ContextTargetIdentity, ...] = Field(
        default=(),
        validation_alias=AliasChoices(
            "target_fingerprints", "relevant_target_fingerprints"
        ),
        max_length=MAX_CONTEXT_TARGETS,
    )
    pattern_identities: tuple[PatternIdentity, ...] = Field(default=(), max_length=512)
    palette_inventory_digest: Digest | None = None
    preset_inventory_digest: Digest | None = None
    drum_map_digest: Digest | None = None
    effect_coverage_digest: Digest | None = None
    piano_roll_arming_receipt: PianoRollArmingReceipt | None = None
    # A bounded immutable inventory is useful to integrations that already
    # captured one.  It is optional so lightweight callers can cache digests
    # only and avoid retaining a large observation graph.
    sound_inventory: SoundInventory | None = None
    completed_phase_ids: tuple[Identifier, ...] = Field(
        default=(), max_length=MAX_CONTEXT_RECEIPTS
    )
    receipt_ids: tuple[Identifier, ...] = Field(default=(), max_length=MAX_CONTEXT_RECEIPTS)

    @model_validator(mode="before")
    @classmethod
    def normalize_compact_inputs(cls, value: object) -> object:
        """Accept compact digest-only forms from existing run registries."""

        if not isinstance(value, dict):
            return value
        data = dict(value)
        raw_targets = data.get(
            "target_fingerprints", data.get("relevant_target_fingerprints", ())
        )
        if isinstance(raw_targets, dict):
            raw_targets = tuple(
                {"target_id": key, "kind": "unknown", "fingerprint": fingerprint}
                for key, fingerprint in raw_targets.items()
            )
        elif isinstance(raw_targets, (list, tuple)):
            raw_targets = tuple(
                (
                    {"target_id": f"target-{index}", "kind": "unknown", "fingerprint": item}
                    if isinstance(item, str)
                    else item
                )
                for index, item in enumerate(raw_targets)
            )
        data["target_fingerprints"] = raw_targets
        checkpoint = data.get("project_checkpoint")
        if isinstance(checkpoint, str):
            data["project_checkpoint"] = {"digest": checkpoint}
        receipt = data.get("piano_roll_arming_receipt")
        if isinstance(receipt, str):
            data["piano_roll_arming_receipt"] = {
                "receipt_id": receipt,
                "authenticated": True,
            }
        return data

    @model_validator(mode="after")
    def validate_snapshot(self) -> "CreationRunContextSnapshot":
        target_ids = [item.target_id.casefold() for item in self.target_fingerprints]
        if len(set(target_ids)) != len(target_ids):
            raise ValueError("context target identities must be unique")
        pattern_ids = [item.pattern_number for item in self.pattern_identities]
        if len(set(pattern_ids)) != len(pattern_ids):
            raise ValueError("context pattern identities must be unique")
        for label, values in (
            ("completed_phase_ids", self.completed_phase_ids),
            ("receipt_ids", self.receipt_ids),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{label} must be unique")
        return self

    @property
    def relevant_target_fingerprints(self) -> tuple[str, ...]:
        """Compatibility view containing only target fingerprint strings."""

        return tuple(item.fingerprint for item in self.target_fingerprints)

    @property
    def context_digest(self) -> str:
        """Stable digest of observations, excluding capture time."""

        return _canonical_digest(
            self.model_dump(mode="json", exclude={"captured_at"}, exclude_none=False)
        )

    @property
    def snapshot_digest(self) -> str:
        return self.context_digest

    @property
    def digest(self) -> str:
        return self.context_digest

    @property
    def project_state_digest(self) -> str | None:
        return self.project_checkpoint.digest

    @property
    def target_identities(self) -> tuple[ContextTargetIdentity, ...]:
        return self.target_fingerprints

    @property
    def arming_receipt(self) -> PianoRollArmingReceipt | None:
        return self.piano_roll_arming_receipt

    @property
    def reusable(self) -> bool:
        """Whether the snapshot has the minimum session proof for reuse."""

        return self.session_fingerprint is not None

    def matches_session(self, session_fingerprint: str | None) -> bool:
        """Return whether a later observation is from the same FL session."""

        return (
            session_fingerprint is not None
            and self.session_fingerprint is not None
            and session_fingerprint == self.session_fingerprint
        )

    def target_matches(self, target_id: str, fingerprint: str) -> bool:
        """Return whether one cached target still has its captured identity."""

        return any(
            item.target_id == target_id and item.fingerprint == fingerprint
            for item in self.target_fingerprints
        )

    def target_fingerprint_for(self, target_id: str) -> str | None:
        for item in self.target_fingerprints:
            if item.target_id == target_id:
                return item.fingerprint
        return None

    def with_target_refresh(self, target: ContextTargetIdentity) -> "CreationRunContextSnapshot":
        """Return a snapshot with one target replaced, preserving immutability."""

        retained = tuple(
            item for item in self.target_fingerprints if item.target_id != target.target_id
        )
        refreshed = tuple(sorted((*retained, target), key=lambda item: item.target_id))
        return self.model_copy(update={"target_fingerprints": refreshed})

    def with_phase_completion(
        self,
        phase_id: str,
        *,
        receipt_ids: tuple[str, ...] = (),
    ) -> "CreationRunContextSnapshot":
        """Return a new snapshot with append-only phase/receipt references."""

        phases = tuple(dict.fromkeys((*self.completed_phase_ids, phase_id)))
        receipts = tuple(dict.fromkeys((*self.receipt_ids, *receipt_ids)))
        return self.model_copy(
            update={"completed_phase_ids": phases, "receipt_ids": receipts}
        )


# Short aliases are intentionally public: integration modules can migrate to
# the more explicit name without duplicating a second context contract.
RunContextSnapshot = CreationRunContextSnapshot
CreationRunContext = CreationRunContextSnapshot
ContextSnapshot = CreationRunContextSnapshot


def _canonical_digest(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=False)
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def build_context_snapshot(
    *,
    session_fingerprint: str | None,
    project: ProjectSummary | None = None,
    target_fingerprints: tuple[ContextTargetIdentity, ...] = (),
    pattern_identities: tuple[PatternIdentity, ...] = (),
    palette_inventory_digest: str | None = None,
    preset_inventory_digest: str | None = None,
    drum_map_digest: str | None = None,
    effect_coverage_digest: str | None = None,
    piano_roll_arming_receipt: PianoRollArmingReceipt | None = None,
    sound_inventory: SoundInventory | None = None,
    mcp_process_identity: str | None = None,
    package_source_revision: str | None = None,
    deployed_bridge_revision: str | None = None,
    running_bridge_revision: str | None = None,
    midi_endpoint: str | None = None,
    captured_at: datetime | None = None,
    project_checkpoint: ProjectCheckpoint | None = None,
    project_checkpoint_digest: str | None = None,
) -> CreationRunContextSnapshot:
    """Build one immutable snapshot from already-captured observations.

    ``project`` is never retained.  If a checkpoint is not supplied, only its
    digest and small dirty/history fields are copied from the summary.  This
    keeps the run context bounded and makes it clear that a snapshot is not a
    second project inventory.
    """

    checkpoint = project_checkpoint
    if checkpoint is None:
        checkpoint_digest = project_checkpoint_digest
        if checkpoint_digest is None and project is not None:
            checkpoint_digest = _project_checkpoint_digest(project)
        checkpoint = ProjectCheckpoint(
            digest=checkpoint_digest,
            dirty_state="unknown" if project is None else project.dirty_state,
            undo_history_position=(
                None if project is None else project.undo_history_position
            ),
            undo_history_count=None if project is None else project.undo_history_count,
            transport=(
                None
                if project is None
                else TransportCheckpoint(
                    tempo_bpm=project.transport.tempo_bpm or project.tempo_bpm,
                    time_signature_numerator=(
                        project.transport.time_signature_numerator
                    ),
                )
            ),
        )
    return CreationRunContextSnapshot(
        captured_at=_now() if captured_at is None else captured_at,
        mcp_process_identity=mcp_process_identity,
        session_fingerprint=session_fingerprint,
        package_source_revision=package_source_revision,
        deployed_bridge_revision=deployed_bridge_revision,
        running_bridge_revision=running_bridge_revision,
        midi_endpoint=midi_endpoint,
        project_checkpoint=checkpoint,
        target_fingerprints=target_fingerprints,
        pattern_identities=pattern_identities,
        palette_inventory_digest=palette_inventory_digest,
        preset_inventory_digest=preset_inventory_digest,
        drum_map_digest=drum_map_digest,
        effect_coverage_digest=effect_coverage_digest,
        piano_roll_arming_receipt=piano_roll_arming_receipt,
        sound_inventory=sound_inventory,
    )


def context_snapshot_digest(snapshot: CreationRunContextSnapshot) -> str:
    """Return the stable digest used for cache identity and diagnostics."""

    return snapshot.context_digest


def _project_checkpoint_digest(project: ProjectSummary) -> str:
    """Digest stable summary facts, excluding observation timestamp/warnings."""

    payload = {
        "project_title": project.project_title,
        "project_author": project.project_author,
        "project_genre": project.project_genre,
        "tempo_bpm": project.tempo_bpm,
        "ppq": project.ppq,
        "mixer_track_count": project.mixer_track_count,
        "channel_count": project.channel_count,
        "pattern_count": project.pattern_count,
        "playlist_track_count": project.playlist_track_count,
        "dirty_flag": project.dirty_flag,
        "undo_history_position": project.undo_history_position,
        "undo_history_count": project.undo_history_count,
        "transport": {
            "playing": project.transport.playing,
            "recording": project.transport.recording,
            "metronome_enabled": project.transport.metronome_enabled,
            "precount_enabled": project.transport.precount_enabled,
            "time_signature_numerator": project.transport.time_signature_numerator,
            "tempo_bpm": project.transport.tempo_bpm,
            "song_length_ms": project.transport.song_length_ms,
            "loop_mode": project.transport.loop_mode,
        },
    }
    return _canonical_digest(payload)


__all__ = [
    "ContextSnapshot",
    "ContextTargetIdentity",
    "CreationRunContext",
    "CreationRunContextSnapshot",
    "PianoRollArmingReceipt",
    "ProjectCheckpoint",
    "TempoCheckpoint",
    "TransportCheckpoint",
    "RunContextSnapshot",
    "build_context_snapshot",
    "context_snapshot_digest",
]
