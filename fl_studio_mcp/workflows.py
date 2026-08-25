"""Outcome-level workflows built on PostFader's verified mutation kernel.

The batch executor deliberately does not add a generic bridge escape hatch.  It
accepts a closed discriminated union of existing absolute writes, performs one
live-session preflight, carries that session fingerprint internally, and then
returns every later-idle-tick receipt.  A batch is ordered and non-atomic: it
never retries an ambiguous mutation and never pretends a rollback occurred.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypedDict, cast

from pydantic import Field, TypeAdapter, model_validator

from .bridge_client import get_client
from .contracts import (
    SCHEMA_VERSION,
    ExpectedEqBandState,
    ExpectedMixerVolumeState,
    VerifiedMixerArmWrite,
    VerifiedMixerColorWrite,
    VerifiedMixerEqWrite,
    VerifiedMixerMuteWrite,
    VerifiedMixerNameWrite,
    VerifiedMixerPanWrite,
    VerifiedMixerSendLevelWrite,
    VerifiedMixerSendWrite,
    VerifiedMixerSoloWrite,
    VerifiedMixerStereoSeparationWrite,
    VerifiedMixerVolumeWrite,
    VerifiedMixerVolumeDbWrite,
)
from .performance import TrackBController, TrackBMutationGateway
from .readonly_inspector import IncompatibleFLStudio, connection_from_ping
from .track_b_contracts import (
    FL_COLOR_WORD_MAX,
    MAX_CHANNEL_NAME_LENGTH,
    MAX_PATTERN_LENGTH_BEATS,
    MAX_PATTERN_NAME_LENGTH,
    MAX_PATTERN_NUMBER,
    MAX_PLAYLIST_TRACK_NAME_LENGTH,
    SESSION_FINGERPRINT_PATTERN,
    ExpectedChannelIdentityState,
    ExpectedChannelMixState,
    ExpectedChannelPitchState,
    ExpectedChannelRouteState,
    ExpectedChannelSoloState,
    ExpectedPatternIdentityState,
    ExpectedPatternLengthState,
    ExpectedPlaylistTrackIdentityState,
    ExpectedPlaylistTrackState,
    ExpectedPluginParameterState,
    ExpectedTempoState,
    PluginTarget,
    TrackBContract,
    VerifiedChannelIdentityWrite,
    VerifiedChannelMixWrite,
    VerifiedChannelPitchWrite,
    VerifiedChannelRouteWrite,
    VerifiedChannelSoloWrite,
    VerifiedPatternIdentityWrite,
    VerifiedPatternLengthWrite,
    VerifiedPlaylistTrackIdentityWrite,
    VerifiedPlaylistTrackStateWrite,
    VerifiedTargetedPluginParameterWrite,
    VerifiedTempoWrite,
)
from .verified_writer import (
    PROVENANCE_REFUSAL,
    WRITES_DISABLED_HELP,
    VerifiedWriter,
    VerifiedWritesUnavailable,
    WriteGateway,
)


MAX_BATCH_OPERATIONS = 32
BatchOperationName = Literal[
    "mixer_volume",
    "mixer_volume_db",
    "mixer_pan",
    "mixer_mute",
    "mixer_solo",
    "mixer_arm",
    "mixer_color",
    "mixer_stereo_separation",
    "mixer_name",
    "mixer_send",
    "mixer_send_level",
    "mixer_eq",
    "plugin_parameter",
    "channel_mix",
    "channel_solo",
    "channel_pitch",
    "channel_identity",
    "channel_route",
    "pattern_identity",
    "pattern_length",
    "playlist_identity",
    "playlist_state",
    "tempo",
]


class BatchOperationBase(TrackBContract):
    operation_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    operation: BatchOperationName


class MixerBatchOperationBase(BatchOperationBase):
    track_index: int = Field(ge=0)
    allow_master: bool = False


class BatchMixerVolume(MixerBatchOperationBase):
    operation: Literal["mixer_volume"] = "mixer_volume"
    volume_normalized: float = Field(ge=0.0, le=1.0)
    expected_before: float | None = Field(default=None, ge=0.0, le=1.0)


class BatchMixerVolumeDb(MixerBatchOperationBase):
    operation: Literal["mixer_volume_db"] = "mixer_volume_db"
    volume_db: float = Field(ge=-60.0, le=6.0)
    tolerance_db: float = Field(default=0.1, ge=0.01, le=1.0)
    expected_before: ExpectedMixerVolumeState | None = None


class BatchMixerPan(MixerBatchOperationBase):
    operation: Literal["mixer_pan"] = "mixer_pan"
    pan: float = Field(ge=-1.0, le=1.0)
    expected_before: float | None = Field(default=None, ge=-1.0, le=1.0)


class BatchMixerMute(MixerBatchOperationBase):
    operation: Literal["mixer_mute"] = "mixer_mute"
    muted: bool
    expected_before: bool | None = None


class BatchMixerSolo(MixerBatchOperationBase):
    operation: Literal["mixer_solo"] = "mixer_solo"
    soloed: bool
    expected_before: bool | None = None


class BatchMixerArm(MixerBatchOperationBase):
    operation: Literal["mixer_arm"] = "mixer_arm"
    armed: bool
    expected_before: bool | None = None


class BatchMixerColor(MixerBatchOperationBase):
    operation: Literal["mixer_color"] = "mixer_color"
    color: int = Field(ge=0, le=FL_COLOR_WORD_MAX)
    expected_before: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)


class BatchMixerStereoSeparation(MixerBatchOperationBase):
    operation: Literal["mixer_stereo_separation"] = "mixer_stereo_separation"
    stereo_separation: float = Field(ge=-1.0, le=1.0)
    expected_before: float | None = Field(default=None, ge=-1.0, le=1.0)


class BatchMixerName(MixerBatchOperationBase):
    operation: Literal["mixer_name"] = "mixer_name"
    name: str = Field(max_length=64)
    expected_before: str | None = Field(default=None, max_length=64)


class BatchMixerSend(MixerBatchOperationBase):
    operation: Literal["mixer_send"] = "mixer_send"
    destination_track_index: int = Field(ge=0)
    enabled: bool
    expected_before: bool | None = None


class BatchMixerSendLevel(MixerBatchOperationBase):
    operation: Literal["mixer_send_level"] = "mixer_send_level"
    destination_track_index: int = Field(ge=0)
    level_normalized: float = Field(ge=0.0, le=1.0)
    expected_before: float | None = Field(default=None, ge=0.0, le=1.0)


class BatchMixerEq(MixerBatchOperationBase):
    operation: Literal["mixer_eq"] = "mixer_eq"
    band_index: int = Field(ge=0, le=2)
    gain_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    frequency_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    expected_before: ExpectedEqBandState | None = None

    @model_validator(mode="after")
    def require_field(self) -> "BatchMixerEq":
        if self.gain_normalized is None and self.frequency_normalized is None:
            raise ValueError("mixer_eq needs gain_normalized and/or frequency_normalized")
        return self


class BatchPluginParameter(BatchOperationBase):
    operation: Literal["plugin_parameter"] = "plugin_parameter"
    target: PluginTarget
    parameter_index: int = Field(ge=0)
    normalized_value: float = Field(ge=0.0, le=1.0)
    expected_before: ExpectedPluginParameterState | None = None


class BatchChannelMix(BatchOperationBase):
    operation: Literal["channel_mix"] = "channel_mix"
    channel_index: int = Field(ge=0)
    volume_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    pan: float | None = Field(default=None, ge=-1.0, le=1.0)
    muted: bool | None = None
    expected_before: ExpectedChannelMixState | None = None

    @model_validator(mode="after")
    def require_field(self) -> "BatchChannelMix":
        if self.volume_normalized is None and self.pan is None and self.muted is None:
            raise ValueError("channel_mix needs volume_normalized, pan, and/or muted")
        return self


class BatchChannelSolo(BatchOperationBase):
    operation: Literal["channel_solo"] = "channel_solo"
    channel_index: int = Field(ge=0)
    soloed: bool
    expected_before: ExpectedChannelSoloState | None = None


class BatchChannelPitch(BatchOperationBase):
    operation: Literal["channel_pitch"] = "channel_pitch"
    channel_index: int = Field(ge=0)
    pitch_normalized: float = Field(ge=-1.0, le=1.0)
    expected_before: ExpectedChannelPitchState | None = None


class BatchChannelIdentity(BatchOperationBase):
    operation: Literal["channel_identity"] = "channel_identity"
    channel_index: int = Field(ge=0)
    name: str | None = Field(default=None, max_length=MAX_CHANNEL_NAME_LENGTH)
    color: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)
    expected_before: ExpectedChannelIdentityState | None = None

    @model_validator(mode="after")
    def require_field(self) -> "BatchChannelIdentity":
        if self.name is None and self.color is None:
            raise ValueError("channel_identity needs name and/or color")
        return self


class BatchChannelRoute(BatchOperationBase):
    operation: Literal["channel_route"] = "channel_route"
    channel_index: int = Field(ge=0)
    mixer_destination: int = Field(ge=-1)
    expected_before: ExpectedChannelRouteState | None = None


class BatchPatternIdentity(BatchOperationBase):
    operation: Literal["pattern_identity"] = "pattern_identity"
    pattern_number: int = Field(ge=1, le=MAX_PATTERN_NUMBER)
    name: str | None = Field(default=None, max_length=MAX_PATTERN_NAME_LENGTH)
    color: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)
    expected_before: ExpectedPatternIdentityState | None = None

    @model_validator(mode="after")
    def require_field(self) -> "BatchPatternIdentity":
        if self.name is None and self.color is None:
            raise ValueError("pattern_identity needs name and/or color")
        return self


class BatchPatternLength(BatchOperationBase):
    operation: Literal["pattern_length"] = "pattern_length"
    pattern_number: int = Field(ge=1, le=MAX_PATTERN_NUMBER)
    length_beats: int = Field(ge=1, le=MAX_PATTERN_LENGTH_BEATS)
    expected_before: ExpectedPatternLengthState | None = None


class BatchPlaylistIdentity(BatchOperationBase):
    operation: Literal["playlist_identity"] = "playlist_identity"
    track_index: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=MAX_PLAYLIST_TRACK_NAME_LENGTH)
    color: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)
    expected_before: ExpectedPlaylistTrackIdentityState | None = None

    @model_validator(mode="after")
    def require_field(self) -> "BatchPlaylistIdentity":
        if self.name is None and self.color is None:
            raise ValueError("playlist_identity needs name and/or color")
        return self


class BatchPlaylistState(BatchOperationBase):
    operation: Literal["playlist_state"] = "playlist_state"
    track_index: int = Field(ge=1)
    muted: bool | None = None
    soloed: bool | None = None
    selected: bool | None = None
    expected_before: ExpectedPlaylistTrackState | None = None

    @model_validator(mode="after")
    def require_field(self) -> "BatchPlaylistState":
        if self.muted is None and self.soloed is None and self.selected is None:
            raise ValueError("playlist_state needs muted, soloed, and/or selected")
        return self


class BatchTempo(BatchOperationBase):
    operation: Literal["tempo"] = "tempo"
    tempo_bpm: float = Field(ge=10.0, le=522.0)
    expected_before: ExpectedTempoState | None = None


BatchOperation = Annotated[
    BatchMixerVolume
    | BatchMixerVolumeDb
    | BatchMixerPan
    | BatchMixerMute
    | BatchMixerSolo
    | BatchMixerArm
    | BatchMixerColor
    | BatchMixerStereoSeparation
    | BatchMixerName
    | BatchMixerSend
    | BatchMixerSendLevel
    | BatchMixerEq
    | BatchPluginParameter
    | BatchChannelMix
    | BatchChannelSolo
    | BatchChannelPitch
    | BatchChannelIdentity
    | BatchChannelRoute
    | BatchPatternIdentity
    | BatchPatternLength
    | BatchPlaylistIdentity
    | BatchPlaylistState
    | BatchTempo,
    Field(discriminator="operation"),
]


BatchReceipt = Annotated[
    VerifiedMixerVolumeWrite
    | VerifiedMixerVolumeDbWrite
    | VerifiedMixerPanWrite
    | VerifiedMixerMuteWrite
    | VerifiedMixerSoloWrite
    | VerifiedMixerArmWrite
    | VerifiedMixerColorWrite
    | VerifiedMixerStereoSeparationWrite
    | VerifiedMixerNameWrite
    | VerifiedMixerSendWrite
    | VerifiedMixerSendLevelWrite
    | VerifiedMixerEqWrite
    | VerifiedTargetedPluginParameterWrite
    | VerifiedChannelMixWrite
    | VerifiedChannelSoloWrite
    | VerifiedChannelPitchWrite
    | VerifiedChannelIdentityWrite
    | VerifiedChannelRouteWrite
    | VerifiedPatternIdentityWrite
    | VerifiedPatternLengthWrite
    | VerifiedPlaylistTrackIdentityWrite
    | VerifiedPlaylistTrackStateWrite
    | VerifiedTempoWrite,
    Field(discriminator="bridge_command"),
]


class BatchItemResult(TrackBContract):
    operation_index: int = Field(ge=0)
    operation_id: str = Field(min_length=1, max_length=64)
    operation: BatchOperationName
    status: Literal["verified", "unverified", "error_unknown"]
    outcome_known: bool
    verified: bool
    receipt: BatchReceipt | None = None
    error: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_status(self) -> "BatchItemResult":
        if self.status == "verified":
            if not self.outcome_known or not self.verified or self.receipt is None:
                raise ValueError("verified batch status needs a verified receipt")
        elif self.status == "unverified":
            if not self.outcome_known or self.verified or self.receipt is None:
                raise ValueError("unverified batch status needs a known receipt")
        elif self.outcome_known or self.verified or self.receipt is not None or not self.error:
            raise ValueError("error_unknown needs only an error and unknown outcome")
        return self


class VerifiedBatchResult(TrackBContract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    applied_at: datetime
    requested_count: int = Field(ge=1, le=MAX_BATCH_OPERATIONS)
    attempted_count: int = Field(ge=0, le=MAX_BATCH_OPERATIONS)
    skipped_count: int = Field(ge=0, le=MAX_BATCH_OPERATIONS)
    completed: bool
    verified: bool
    stop_on_unverified: bool
    stopped_reason: Literal["unverified_receipt", "unknown_outcome"] | None = None
    one_session_preflight_completed: Literal[True] = True
    session_fingerprint: str = Field(pattern=SESSION_FINGERPRINT_PATTERN)
    automatic_replay_attempted: Literal[False] = False
    rollback_attempted: Literal[False] = False
    project_saved: Literal[False] = False
    results: list[BatchItemResult]
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_aggregate(self) -> "VerifiedBatchResult":
        if self.attempted_count != len(self.results):
            raise ValueError("attempted_count must equal the result count")
        if self.skipped_count != self.requested_count - self.attempted_count:
            raise ValueError("batch requested/attempted/skipped counts disagree")
        if self.completed != (self.skipped_count == 0):
            raise ValueError("completed must mean every requested item was attempted")
        expected_verified = self.completed and all(item.verified for item in self.results)
        if self.verified != expected_verified:
            raise ValueError("batch verified must mean every requested item verified")
        return self


_BATCH_ADAPTER = TypeAdapter(list[BatchOperation])


def validate_batch_operations(
    operations: list[BatchOperation] | list[dict[str, Any]],
) -> list[BatchOperation]:
    """Validate the complete closed-union batch before any live handshake."""

    parsed = _BATCH_ADAPTER.validate_python(operations)
    if not 1 <= len(parsed) <= MAX_BATCH_OPERATIONS:
        raise ValueError(f"operations must contain 1..{MAX_BATCH_OPERATIONS} items")
    _preflight_operations(parsed)
    return parsed


class _CachedPingClient:
    """Delegate calls while serving the single preflight handshake from memory."""

    def __init__(self, client: Any, ping: dict[str, Any]):
        self._client = client
        self._ping = dict(ping)

    @property
    def transport(self) -> str:
        return cast(str, getattr(self._client, "transport", "unknown"))

    def ping(self) -> dict[str, Any]:
        return dict(self._ping)

    def call(self, cmd: str, **arguments: Any) -> dict[str, Any]:
        return cast(dict[str, Any], self._client.call(cmd, **arguments))


class _SessionKwargs(TypedDict):
    """Keyword arguments shared by every verified batch dispatch."""

    session_fingerprint: str


def _operation_keys(operation: BatchOperation) -> list[tuple[Any, ...]]:
    """Fields one operation owns; duplicates would make ordering ambiguous."""

    if isinstance(operation, BatchMixerEq):
        fields = []
        if operation.gain_normalized is not None:
            fields.append(("mixer", operation.track_index, "eq", operation.band_index, "gain"))
        if operation.frequency_normalized is not None:
            fields.append(("mixer", operation.track_index, "eq", operation.band_index, "frequency"))
        return fields
    if isinstance(operation, BatchMixerSend):
        return [("mixer", operation.track_index, "send", operation.destination_track_index, "enabled")]
    if isinstance(operation, BatchMixerSendLevel):
        return [("mixer", operation.track_index, "send", operation.destination_track_index, "level")]
    if isinstance(operation, BatchPluginParameter):
        target = operation.target
        if target.kind == "mixer_effect":
            address = (target.kind, target.track_index, target.slot_index)
        else:
            address = (target.kind, target.channel_index, -1)
        return [("plugin", *address, operation.parameter_index)]
    if isinstance(operation, BatchChannelMix):
        return [
            ("channel", operation.channel_index, field)
            for field, value in (
                ("volume", operation.volume_normalized),
                ("pan", operation.pan),
                ("muted", operation.muted),
            )
            if value is not None
        ]
    if isinstance(operation, BatchChannelIdentity):
        return [
            ("channel", operation.channel_index, field)
            for field, value in (("name", operation.name), ("color", operation.color))
            if value is not None
        ]
    if isinstance(operation, BatchPatternIdentity):
        return [
            ("pattern", operation.pattern_number, field)
            for field, value in (("name", operation.name), ("color", operation.color))
            if value is not None
        ]
    if isinstance(operation, BatchPlaylistIdentity):
        return [
            ("playlist", operation.track_index, field)
            for field, value in (("name", operation.name), ("color", operation.color))
            if value is not None
        ]
    if isinstance(operation, BatchPlaylistState):
        return [
            ("playlist", operation.track_index, field)
            for field, value in (
                ("muted", operation.muted),
                ("soloed", operation.soloed),
                ("selected", operation.selected),
            )
            if value is not None
        ]
    simple_fields: list[tuple[type[Any], str, str, str]] = [
        (BatchMixerVolume, "track_index", "mixer", "volume"),
        (BatchMixerVolumeDb, "track_index", "mixer", "volume"),
        (BatchMixerPan, "track_index", "mixer", "pan"),
        (BatchMixerMute, "track_index", "mixer", "muted"),
        (BatchMixerSolo, "track_index", "mixer", "soloed"),
        (BatchMixerArm, "track_index", "mixer", "armed"),
        (BatchMixerColor, "track_index", "mixer", "color"),
        (BatchMixerStereoSeparation, "track_index", "mixer", "stereo"),
        (BatchMixerName, "track_index", "mixer", "name"),
        (BatchChannelSolo, "channel_index", "channel", "soloed"),
        (BatchChannelPitch, "channel_index", "channel", "pitch"),
        (BatchChannelRoute, "channel_index", "channel", "route"),
        (BatchPatternLength, "pattern_number", "pattern", "length"),
    ]
    for kind, index_field, namespace, field in simple_fields:
        if isinstance(operation, kind):
            return [(namespace, getattr(operation, index_field), field)]
    if isinstance(operation, BatchTempo):
        return [("project", "tempo")]
    raise AssertionError("unhandled batch operation")


def _preflight_operations(operations: list[BatchOperation]) -> None:
    ids: set[str] = set()
    owners: dict[tuple[Any, ...], str] = {}
    for operation in operations:
        if isinstance(operation, MixerBatchOperationBase):
            if operation.track_index == 0 and not operation.allow_master:
                raise ValueError(
                    f"batch operation {operation.operation_id!r} targets Master; "
                    "that item must set allow_master=true explicitly"
                )
        if isinstance(operation, (BatchMixerSend, BatchMixerSendLevel)):
            if operation.track_index == operation.destination_track_index:
                raise ValueError(
                    f"batch operation {operation.operation_id!r} cannot route a "
                    "mixer track to itself"
                )
        if operation.operation_id in ids:
            raise ValueError(
                f"duplicate batch operation_id {operation.operation_id!r}"
            )
        ids.add(operation.operation_id)
        for key in _operation_keys(operation):
            prior = owners.get(key)
            if prior is not None:
                raise ValueError(
                    f"batch operations {prior!r} and {operation.operation_id!r} "
                    f"both write {key!r}; combine them into one absolute operation"
                )
            owners[key] = operation.operation_id


class VerifiedBatchExecutor:
    """Apply an ordered closed-union batch after one live-session handshake."""

    def apply(
        self,
        *,
        operations: list[BatchOperation] | list[dict[str, Any]],
        stop_on_unverified: bool = True,
        session_fingerprint: str | None = None,
    ) -> VerifiedBatchResult:
        parsed = validate_batch_operations(operations)
        if type(stop_on_unverified) is not bool:
            raise ValueError("stop_on_unverified must be true or false")
        client = get_client()
        ping = client.ping()
        if not isinstance(ping, dict):
            raise ValueError("FL bridge returned a malformed batch preflight handshake")
        transport = getattr(client, "transport", "unknown")
        connection = connection_from_ping(ping, transport)
        if not connection.connected or not connection.compatible:
            raise IncompatibleFLStudio(
                connection.error or connection.compatibility_reason
            )
        if not connection.verified_writes_enabled:
            raise VerifiedWritesUnavailable(
                WRITES_DISABLED_HELP.format(
                    mode=connection.bridge_mode,
                    enabled=connection.verified_writes_enabled,
                )
            )
        if not connection.bridge_provenance_verified:
            raise VerifiedWritesUnavailable(
                PROVENANCE_REFUSAL.format(status=connection.bridge_provenance)
            )
        session = connection.session_fingerprint
        if session is None:
            raise VerifiedWritesUnavailable(
                "batch preflight requires a valid bridge session fingerprint"
            )
        if session_fingerprint is not None and session_fingerprint != session:
            raise VerifiedWritesUnavailable(
                "batch session precondition failed before the first mutation"
            )

        cached = _CachedPingClient(client, ping)
        writer = VerifiedWriter(WriteGateway(cached))
        controller = TrackBController(TrackBMutationGateway(cached))
        results: list[BatchItemResult] = []
        stopped_reason: Literal["unverified_receipt", "unknown_outcome"] | None = None

        for index, operation in enumerate(parsed):
            try:
                receipt = _dispatch_batch_operation(
                    operation, writer=writer, controller=controller, session=session
                )
            except Exception as exc:
                results.append(
                    BatchItemResult(
                        operation_index=index,
                        operation_id=operation.operation_id,
                        operation=operation.operation,
                        status="error_unknown",
                        outcome_known=False,
                        verified=False,
                        error=(f"{type(exc).__name__}: {exc}")[:2048],
                    )
                )
                stopped_reason = "unknown_outcome"
                break
            verified = bool(receipt.verified)
            results.append(
                BatchItemResult(
                    operation_index=index,
                    operation_id=operation.operation_id,
                    operation=operation.operation,
                    status="verified" if verified else "unverified",
                    outcome_known=True,
                    verified=verified,
                    receipt=receipt,
                )
            )
            if not verified and stop_on_unverified:
                stopped_reason = "unverified_receipt"
                break

        attempted = len(results)
        completed = attempted == len(parsed)
        warnings = [
            "This ordered batch is non-atomic. Every attempted item has its own "
            "later-tick receipt; PostFader never retries an ambiguous mutation.",
            "No rollback or project save was attempted. If execution stopped, "
            "previous verified items remain applied and later items were skipped.",
        ]
        return VerifiedBatchResult(
            applied_at=datetime.now(timezone.utc),
            requested_count=len(parsed),
            attempted_count=attempted,
            skipped_count=len(parsed) - attempted,
            completed=completed,
            verified=completed and all(item.verified for item in results),
            stop_on_unverified=stop_on_unverified,
            stopped_reason=stopped_reason,
            session_fingerprint=session,
            results=results,
            warnings=warnings,
        )


def _dispatch_batch_operation(
    operation: BatchOperation,
    *,
    writer: VerifiedWriter,
    controller: TrackBController,
    session: str,
) -> BatchReceipt:
    common: _SessionKwargs = {"session_fingerprint": session}
    if isinstance(operation, BatchMixerVolume):
        return writer.set_mixer_volume(
            track_index=operation.track_index,
            volume_normalized=operation.volume_normalized,
            allow_master=operation.allow_master,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchMixerVolumeDb):
        return writer.set_mixer_volume_db(
            track_index=operation.track_index,
            volume_db=operation.volume_db,
            tolerance_db=operation.tolerance_db,
            allow_master=operation.allow_master,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchMixerPan):
        return writer.set_mixer_pan(
            track_index=operation.track_index,
            pan=operation.pan,
            allow_master=operation.allow_master,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchMixerMute):
        return writer.set_mixer_mute(
            track_index=operation.track_index,
            muted=operation.muted,
            allow_master=operation.allow_master,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchMixerSolo):
        return writer.set_mixer_solo(
            track_index=operation.track_index,
            soloed=operation.soloed,
            allow_master=operation.allow_master,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchMixerArm):
        return writer.set_mixer_arm(
            track_index=operation.track_index,
            armed=operation.armed,
            allow_master=operation.allow_master,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchMixerColor):
        return writer.set_mixer_color(
            track_index=operation.track_index,
            color=operation.color,
            allow_master=operation.allow_master,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchMixerStereoSeparation):
        return writer.set_mixer_stereo_separation(
            track_index=operation.track_index,
            stereo_separation=operation.stereo_separation,
            allow_master=operation.allow_master,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchMixerName):
        return writer.set_mixer_name(
            track_index=operation.track_index,
            name=operation.name,
            allow_master=operation.allow_master,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchMixerSend):
        return writer.set_mixer_send(
            track_index=operation.track_index,
            destination_track_index=operation.destination_track_index,
            enabled=operation.enabled,
            allow_master=operation.allow_master,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchMixerSendLevel):
        return writer.set_mixer_send_level(
            track_index=operation.track_index,
            destination_track_index=operation.destination_track_index,
            level_normalized=operation.level_normalized,
            allow_master=operation.allow_master,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchMixerEq):
        return writer.set_mixer_eq(
            track_index=operation.track_index,
            band_index=operation.band_index,
            gain_normalized=operation.gain_normalized,
            frequency_normalized=operation.frequency_normalized,
            allow_master=operation.allow_master,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchPluginParameter):
        return controller.set_plugin_parameter(
            target=operation.target,
            parameter_index=operation.parameter_index,
            normalized_value=operation.normalized_value,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchChannelMix):
        return controller.set_channel_mix(
            channel_index=operation.channel_index,
            volume_normalized=operation.volume_normalized,
            pan=operation.pan,
            muted=operation.muted,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchChannelSolo):
        return controller.set_channel_solo(
            channel_index=operation.channel_index,
            soloed=operation.soloed,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchChannelPitch):
        return controller.set_channel_pitch(
            channel_index=operation.channel_index,
            pitch_normalized=operation.pitch_normalized,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchChannelIdentity):
        return controller.set_channel_identity(
            channel_index=operation.channel_index,
            name=operation.name,
            color=operation.color,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchChannelRoute):
        return controller.route_channel_to_mixer(
            channel_index=operation.channel_index,
            mixer_destination=operation.mixer_destination,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchPatternIdentity):
        return controller.set_pattern_identity(
            pattern_number=operation.pattern_number,
            name=operation.name,
            color=operation.color,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchPatternLength):
        return controller.set_pattern_length(
            pattern_number=operation.pattern_number,
            length_beats=operation.length_beats,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchPlaylistIdentity):
        return controller.set_playlist_track_identity(
            track_index=operation.track_index,
            name=operation.name,
            color=operation.color,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchPlaylistState):
        return controller.set_playlist_track_state(
            track_index=operation.track_index,
            muted=operation.muted,
            soloed=operation.soloed,
            selected=operation.selected,
            expected_before=operation.expected_before,
            **common,
        )
    if isinstance(operation, BatchTempo):
        return controller.set_tempo(
            tempo_bpm=operation.tempo_bpm,
            expected_before=operation.expected_before,
            **common,
        )
    raise AssertionError("unhandled batch operation")
