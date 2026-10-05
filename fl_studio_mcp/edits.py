"""Per-target setters: one call changes several fields of one FL target.

The MCP surface has one setter per target -- a mixer track, a channel, a
pattern, a Playlist track, and the transport -- instead of one tool per field.
Each setter turns its arguments into the existing verified writes for that
target, in a fixed documented order, performs the batch executor's single
live-session preflight, and dispatches the writes with that session pinned.
Every attempted write keeps its own later-idle-tick receipt. Like a batch, the
sequence is non-atomic and never retried: earlier verified fields remain if a
later write fails, and an ambiguous outcome stops the sequence.

Guards fail closed. ``expected_before`` names the fields it guards, and a guard
that no write in the call would check is refused rather than silently ignored.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

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
    VerifiedMixerSelectionWrite,
    VerifiedMixerSendLevelWrite,
    VerifiedMixerSendWrite,
    VerifiedMixerSoloWrite,
    VerifiedMixerStereoSeparationWrite,
    VerifiedMixerVolumeDbWrite,
    VerifiedMixerVolumeWrite,
)
from .performance import TrackBController, TrackBMutationGateway
from .track_b_contracts import (
    FL_COLOR_WORD_MAX,
    MAX_CHANNEL_NAME_LENGTH,
    MAX_PATTERN_LENGTH_BEATS,
    MAX_PATTERN_NAME_LENGTH,
    MAX_PATTERN_NUMBER,
    MAX_PLAYLIST_TRACK_NAME_LENGTH,
    SESSION_FINGERPRINT_PATTERN,
    SHA256_PATTERN,
    ExpectedChannelIdentityState,
    ExpectedChannelMixState,
    ExpectedChannelPitchState,
    ExpectedChannelRouteState,
    ExpectedChannelSelectionState,
    ExpectedChannelSoloState,
    ExpectedLoopModeState,
    ExpectedMetronomeState,
    ExpectedPatternIdentityState,
    ExpectedPatternLengthState,
    ExpectedPatternSelectionState,
    ExpectedPlayingState,
    ExpectedPlaylistTrackIdentityState,
    ExpectedPlaylistTrackState,
    ExpectedPrecountState,
    ExpectedRecordingState,
    ExpectedSongPositionState,
    ExpectedStopState,
    ExpectedTempoState,
    ExpectedTimeSignatureState,
    LoopMode,
    TrackBContract,
    VerifiedChannelIdentityWrite,
    VerifiedChannelMixWrite,
    VerifiedChannelPitchWrite,
    VerifiedChannelRouteWrite,
    VerifiedChannelSelectionWrite,
    VerifiedChannelSoloWrite,
    VerifiedLoopModeWrite,
    VerifiedMetronomeWrite,
    VerifiedPatternIdentityWrite,
    VerifiedPatternLengthWrite,
    VerifiedPatternSelectionWrite,
    VerifiedPlayingWrite,
    VerifiedPlaylistTrackIdentityWrite,
    VerifiedPlaylistTrackStateWrite,
    VerifiedPrecountWrite,
    VerifiedRecordingWrite,
    VerifiedSongPositionWrite,
    VerifiedStopWrite,
    VerifiedTempoWrite,
    VerifiedTimeSignatureNumeratorWrite,
)
from .verified_writer import VerifiedWriter, WriteGateway
from .workflows import open_verified_session


MAX_EDIT_STEPS = 32
MAX_MIXER_SENDS = 8

EditStatus = Literal["verified", "unverified", "error_unknown"]
StoppedReason = Literal["unverified_receipt", "unknown_outcome"]


# ---------------------------------------------------------------------------
# Inputs: list items and flat before-state guards
# ---------------------------------------------------------------------------


def _require_any(model: TrackBContract, label: str) -> None:
    if all(value is None for _name, value in model):
        raise ValueError(f"{label} needs at least one field")


class MixerEqBandChange(TrackBContract):
    """One built-in EQ band change; supply gain, frequency, or both."""

    band_index: int = Field(ge=0, le=2, description="Built-in EQ band: 0 low, 1 mid, 2 high.")
    gain_normalized: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Band gain 0..1; 0.5 is flat."
    )
    frequency_normalized: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Band centre frequency, normalized 0..1."
    )
    expected_before: ExpectedEqBandState | None = Field(
        default=None,
        description="Optional current gain and/or frequency; refuse if either changed.",
    )

    @model_validator(mode="after")
    def require_change(self) -> "MixerEqBandChange":
        if self.gain_normalized is None and self.frequency_normalized is None:
            raise ValueError("an EQ band change needs gain_normalized and/or frequency_normalized")
        return self


class ExpectedMixerSendState(TrackBContract):
    """Optional before-state guard for one send."""

    enabled: bool | None = Field(default=None, description="Expected route state.")
    level_normalized: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Expected send amount."
    )

    @model_validator(mode="after")
    def require_field(self) -> "ExpectedMixerSendState":
        _require_any(self, "a send guard")
        return self


class MixerSendChange(TrackBContract):
    """Create, remove, or level one send from this track to another."""

    destination_track_index: int = Field(
        ge=0, description="Receiving mixer track; sending to Master needs no flag."
    )
    enabled: bool | None = Field(
        default=None, description="True creates the route, false removes it."
    )
    level_normalized: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Send amount 0..1; 0.8 is unity. The send must exist or be enabled in this change.",
    )
    expected_before: ExpectedMixerSendState | None = Field(
        default=None, description="Optional current route state and/or amount."
    )

    @model_validator(mode="after")
    def validate_change(self) -> "MixerSendChange":
        if self.enabled is None and self.level_normalized is None:
            raise ValueError("a send change needs enabled and/or level_normalized")
        if self.enabled is False and self.level_normalized is not None:
            raise ValueError("a send that is being removed cannot also take a level")
        guard = self.expected_before
        if guard is not None:
            if guard.enabled is not None and self.enabled is None:
                raise ValueError(
                    "expected_before.enabled guards the route state; this send change does not set enabled"
                )
            if guard.level_normalized is not None and self.level_normalized is None:
                raise ValueError(
                    "expected_before.level_normalized guards the amount; this send change does not set a level"
                )
        return self


class ExpectedMixerTrackFields(TrackBContract):
    """Optional current values for the fields one mixer_set_track call changes."""

    volume_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    volume_db: float | None = Field(
        default=None, ge=-200.0, le=12.0, description="Guards only a volume_db change."
    )
    pan: float | None = Field(default=None, ge=-1.0, le=1.0)
    stereo_separation: float | None = Field(default=None, ge=-1.0, le=1.0)
    muted: bool | None = None
    soloed: bool | None = None
    armed: bool | None = None
    name: str | None = Field(default=None, max_length=64)
    color: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)
    active_track_index: int | None = Field(
        default=None, ge=0, description="Guards select: the currently active track."
    )

    @model_validator(mode="after")
    def require_field(self) -> "ExpectedMixerTrackFields":
        _require_any(self, "expected_before")
        return self


class ExpectedChannelFields(TrackBContract):
    """Optional current values for the fields one channel_set call changes."""

    channel_fingerprint: str | None = Field(
        default=None,
        pattern=SHA256_PATTERN,
        description=(
            "Channel identity from channel_list; checked up to the first change to "
            "the name, color, or routing, and never by select."
        ),
    )
    volume_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    pan: float | None = Field(default=None, ge=-1.0, le=1.0)
    muted: bool | None = None
    soloed: bool | None = None
    pitch_normalized: float | None = Field(default=None, ge=-1.0, le=1.0)
    name: str | None = Field(default=None, max_length=MAX_CHANNEL_NAME_LENGTH)
    color: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)
    mixer_destination: int | None = Field(default=None, ge=-1)
    selected_channel_indices: list[int] | None = Field(
        default=None, description="Guards select: the sorted current selection."
    )

    @model_validator(mode="after")
    def require_field(self) -> "ExpectedChannelFields":
        _require_any(self, "expected_before")
        return self


class ExpectedPatternFields(TrackBContract):
    """Optional current values for the fields one pattern_set call changes."""

    name: str | None = Field(default=None, max_length=MAX_PATTERN_NAME_LENGTH)
    color: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)
    length_beats: int | None = Field(default=None, ge=1, le=MAX_PATTERN_LENGTH_BEATS)
    current_pattern_number: int | None = Field(
        default=None,
        ge=1,
        le=MAX_PATTERN_NUMBER,
        description="Guards select: the currently selected pattern.",
    )

    @model_validator(mode="after")
    def require_field(self) -> "ExpectedPatternFields":
        _require_any(self, "expected_before")
        return self


class ExpectedPlaylistTrackFields(TrackBContract):
    """Optional current values for the fields one playlist_set_track call changes."""

    name: str | None = Field(default=None, max_length=MAX_PLAYLIST_TRACK_NAME_LENGTH)
    color: int | None = Field(default=None, ge=0, le=FL_COLOR_WORD_MAX)
    muted: bool | None = None
    soloed: bool | None = None
    selected: bool | None = None

    @model_validator(mode="after")
    def require_field(self) -> "ExpectedPlaylistTrackFields":
        _require_any(self, "expected_before")
        return self


class ExpectedTransportFields(TrackBContract):
    """Optional current values for the settings one transport_set call changes."""

    playing: bool | None = None
    song_position_normalized: float | None = Field(default=None, ge=0.0, le=1.0)
    loop_mode: LoopMode | None = None
    tempo_bpm: float | None = Field(default=None, ge=10.0, le=522.0)
    recording: bool | None = None
    metronome: bool | None = None
    precount: bool | None = None
    time_signature_numerator: int | None = Field(default=None, ge=1, le=32)

    @model_validator(mode="after")
    def require_field(self) -> "ExpectedTransportFields":
        _require_any(self, "expected_before")
        return self


# ---------------------------------------------------------------------------
# Results: one receipt per attempted write
# ---------------------------------------------------------------------------


MixerTrackReceipt = Annotated[
    VerifiedMixerNameWrite
    | VerifiedMixerColorWrite
    | VerifiedMixerVolumeWrite
    | VerifiedMixerVolumeDbWrite
    | VerifiedMixerPanWrite
    | VerifiedMixerStereoSeparationWrite
    | VerifiedMixerMuteWrite
    | VerifiedMixerSoloWrite
    | VerifiedMixerArmWrite
    | VerifiedMixerEqWrite
    | VerifiedMixerSendWrite
    | VerifiedMixerSendLevelWrite
    | VerifiedMixerSelectionWrite,
    Field(discriminator="bridge_command"),
]

ChannelReceipt = Annotated[
    VerifiedChannelIdentityWrite
    | VerifiedChannelRouteWrite
    | VerifiedChannelMixWrite
    | VerifiedChannelSoloWrite
    | VerifiedChannelPitchWrite
    | VerifiedChannelSelectionWrite,
    Field(discriminator="bridge_command"),
]

PatternReceipt = Annotated[
    VerifiedPatternIdentityWrite
    | VerifiedPatternLengthWrite
    | VerifiedPatternSelectionWrite,
    Field(discriminator="bridge_command"),
]

PlaylistTrackReceipt = Annotated[
    VerifiedPlaylistTrackIdentityWrite | VerifiedPlaylistTrackStateWrite,
    Field(discriminator="bridge_command"),
]

TransportReceipt = Annotated[
    VerifiedStopWrite
    | VerifiedPlayingWrite
    | VerifiedTempoWrite
    | VerifiedTimeSignatureNumeratorWrite
    | VerifiedLoopModeWrite
    | VerifiedMetronomeWrite
    | VerifiedPrecountWrite
    | VerifiedRecordingWrite
    | VerifiedSongPositionWrite,
    Field(discriminator="bridge_command"),
]


class EditItemBase(TrackBContract):
    step_index: int = Field(ge=0)
    step: str = Field(min_length=1, max_length=64)
    status: EditStatus
    outcome_known: bool
    verified: bool
    error: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_status(self) -> "EditItemBase":
        receipt = getattr(self, "receipt", None)
        if self.status == "verified":
            if not self.outcome_known or not self.verified or receipt is None:
                raise ValueError("verified edit status needs a verified receipt")
        elif self.status == "unverified":
            if not self.outcome_known or self.verified or receipt is None:
                raise ValueError("unverified edit status needs a known receipt")
        elif self.outcome_known or self.verified or receipt is not None or not self.error:
            raise ValueError("error_unknown needs only an error and unknown outcome")
        return self


class MixerTrackEditItem(EditItemBase):
    receipt: MixerTrackReceipt | None = None


class ChannelEditItem(EditItemBase):
    receipt: ChannelReceipt | None = None


class PatternEditItem(EditItemBase):
    receipt: PatternReceipt | None = None


class PlaylistTrackEditItem(EditItemBase):
    receipt: PlaylistTrackReceipt | None = None


class TransportEditItem(EditItemBase):
    receipt: TransportReceipt | None = None


class EditResultBase(TrackBContract):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    applied_at: datetime
    requested_count: int = Field(ge=1, le=MAX_EDIT_STEPS)
    attempted_count: int = Field(ge=0, le=MAX_EDIT_STEPS)
    skipped_count: int = Field(ge=0, le=MAX_EDIT_STEPS)
    completed: bool
    verified: bool
    stop_on_unverified: bool
    stopped_reason: StoppedReason | None = None
    skipped_steps: list[str] = Field(default_factory=list)
    one_session_preflight_completed: Literal[True] = True
    session_fingerprint: str = Field(pattern=SESSION_FINGERPRINT_PATTERN)
    automatic_replay_attempted: Literal[False] = False
    rollback_attempted: Literal[False] = False
    project_saved: Literal[False] = False
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_aggregate(self) -> "EditResultBase":
        results: list[EditItemBase] = getattr(self, "results")
        if self.attempted_count != len(results):
            raise ValueError("attempted_count must equal the result count")
        if self.skipped_count != self.requested_count - self.attempted_count:
            raise ValueError("requested/attempted/skipped counts disagree")
        if self.skipped_count != len(self.skipped_steps):
            raise ValueError("skipped_steps must name every skipped write")
        if self.completed != (self.skipped_count == 0):
            raise ValueError("completed must mean every requested write was attempted")
        expected_verified = self.completed and all(item.verified for item in results)
        if self.verified != expected_verified:
            raise ValueError("verified must mean every requested write verified")
        return self


class MixerTrackEditResult(EditResultBase):
    track_index: int = Field(ge=0)
    results: list[MixerTrackEditItem]


class ChannelEditResult(EditResultBase):
    channel_index: int = Field(ge=0)
    results: list[ChannelEditItem]


class PatternEditResult(EditResultBase):
    pattern_number: int = Field(ge=1, le=MAX_PATTERN_NUMBER)
    results: list[PatternEditItem]


class PlaylistTrackEditResult(EditResultBase):
    track_index: int = Field(ge=1)
    results: list[PlaylistTrackEditItem]


class TransportEditResult(EditResultBase):
    results: list[TransportEditItem]


# ---------------------------------------------------------------------------
# Planning and execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Step:
    label: str
    write: Callable[[VerifiedWriter, TrackBController, str], Any]


def _writer_step(label: str, method: str, **arguments: Any) -> _Step:
    def write(writer: VerifiedWriter, _controller: TrackBController, session: str) -> Any:
        return getattr(writer, method)(session_fingerprint=session, **arguments)

    return _Step(label, write)


def _controller_step(label: str, method: str, **arguments: Any) -> _Step:
    def write(_writer: VerifiedWriter, controller: TrackBController, session: str) -> Any:
        return getattr(controller, method)(session_fingerprint=session, **arguments)

    return _Step(label, write)


class EditRefusal(ValueError):
    """An explicit edit preflight refusal whose guidance is safe for the caller."""


class _Guards:
    """Hand each write the guard fields it checks; refuse any left over."""

    def __init__(self, expected: TrackBContract | None):
        self._values: dict[str, Any] = (
            {}
            if expected is None
            else {name: value for name, value in expected if value is not None}
        )
        self._used: set[str] = set()

    def take(self, *names: str) -> dict[str, Any]:
        found = {name: self._values[name] for name in names if name in self._values}
        self._used.update(found)
        return found

    def one(self, name: str) -> Any:
        return self.take(name).get(name)

    def model(self, model: type[BaseModel], **renamed: str) -> Any:
        """Build ``model`` from guard fields; ``renamed`` maps model field to guard field."""

        names = {field: renamed.get(field, field) for field in model.model_fields}
        found = self.take(*names.values())
        values = {field: found[source] for field, source in names.items() if source in found}
        return model(**values) if values else None

    def refuse_unused(self, tool: str) -> None:
        unused = sorted(set(self._values) - self._used)
        if unused:
            raise EditRefusal(
                f"{tool} expected_before guards {', '.join(unused)}, but no write in "
                "this call checks those fields; remove them or change those fields"
            )


def _execute(
    steps: list[_Step],
    *,
    tool: str,
    stop_on_unverified: bool,
    session_fingerprint: str | None,
) -> dict[str, Any]:
    if not steps:
        raise EditRefusal(f"{tool} needs at least one field to change")
    if len(steps) > MAX_EDIT_STEPS:
        raise EditRefusal(f"{tool} can make at most {MAX_EDIT_STEPS} writes in one call")
    if type(stop_on_unverified) is not bool:
        raise EditRefusal("stop_on_unverified must be true or false")
    cached, session = open_verified_session(session_fingerprint, label=tool)
    writer = VerifiedWriter(WriteGateway(cached))
    controller = TrackBController(TrackBMutationGateway(cached))
    items: list[dict[str, Any]] = []
    stopped_reason: StoppedReason | None = None
    for index, step in enumerate(steps):
        try:
            receipt = step.write(writer, controller, session)
        except Exception as exc:
            items.append(
                {
                    "step_index": index,
                    "step": step.label,
                    "status": "error_unknown",
                    "outcome_known": False,
                    "verified": False,
                    "error": (f"{type(exc).__name__}: {exc}")[:2048],
                }
            )
            stopped_reason = "unknown_outcome"
            break
        verified = bool(receipt.verified)
        items.append(
            {
                "step_index": index,
                "step": step.label,
                "status": "verified" if verified else "unverified",
                "outcome_known": True,
                "verified": verified,
                "receipt": receipt,
            }
        )
        if not verified and stop_on_unverified:
            stopped_reason = "unverified_receipt"
            break
    attempted = len(items)
    completed = attempted == len(steps)
    return {
        "applied_at": datetime.now(timezone.utc),
        "requested_count": len(steps),
        "attempted_count": attempted,
        "skipped_count": len(steps) - attempted,
        "skipped_steps": [step.label for step in steps[attempted:]],
        "completed": completed,
        "verified": completed and all(item["verified"] for item in items),
        "stop_on_unverified": stop_on_unverified,
        "stopped_reason": stopped_reason,
        "session_fingerprint": session,
        "items": items,
        "warnings": [
            f"{tool} applies its writes in order and is non-atomic. Every attempted "
            "write has its own later-tick receipt; PostFader never retries an "
            "ambiguous mutation.",
            "No rollback or project save was attempted. If execution stopped, "
            "earlier verified writes remain applied and later ones were skipped.",
        ],
    }


def _result(
    result_type: type[EditResultBase],
    item_type: type[EditItemBase],
    execution: dict[str, Any],
    **target: Any,
) -> Any:
    items = [item_type(**item) for item in execution.pop("items")]
    return result_type.model_validate({"results": items, **execution, **target})


def _unique(values: Iterable[int], label: str) -> None:
    seen: set[int] = set()
    for value in values:
        if value in seen:
            raise EditRefusal(f"{label} {value} appears more than once")
        seen.add(value)


def set_mixer_track(
    *,
    track_index: int,
    allow_master: bool = False,
    name: str | None = None,
    color: int | None = None,
    volume_normalized: float | None = None,
    volume_db: float | None = None,
    tolerance_db: float | None = None,
    pan: float | None = None,
    stereo_separation: float | None = None,
    muted: bool | None = None,
    soloed: bool | None = None,
    armed: bool | None = None,
    eq: tuple[MixerEqBandChange, ...] = (),
    sends: tuple[MixerSendChange, ...] = (),
    select: bool = False,
    expected_before: ExpectedMixerTrackFields | None = None,
    session_fingerprint: str | None = None,
    stop_on_unverified: bool = True,
) -> MixerTrackEditResult:
    """Change several fields of one mixer track in a fixed order."""

    tool = "mixer_set_track"
    if type(allow_master) is not bool or type(select) is not bool:
        raise EditRefusal("allow_master and select must be true or false")
    if track_index == 0 and not allow_master:
        raise EditRefusal("mixer track 0 is Master; set allow_master=true to change it")
    if volume_normalized is not None and volume_db is not None:
        raise EditRefusal("set volume_normalized or volume_db, not both")
    if tolerance_db is not None and volume_db is None:
        raise EditRefusal("tolerance_db applies only to a volume_db change")
    _unique((band.band_index for band in eq), "EQ band")
    _unique((send.destination_track_index for send in sends), "send destination")
    if len(sends) > MAX_MIXER_SENDS:
        raise EditRefusal(f"{tool} changes at most {MAX_MIXER_SENDS} sends in one call")
    if any(send.destination_track_index == track_index for send in sends):
        raise EditRefusal("a mixer track cannot send to itself")

    guards = _Guards(expected_before)
    common = {"track_index": track_index, "allow_master": allow_master}
    steps: list[_Step] = []
    if name is not None:
        steps.append(_writer_step(
            "name", "set_mixer_name", name=name, expected_before=guards.one("name"), **common
        ))
    if color is not None:
        steps.append(_writer_step(
            "color", "set_mixer_color", color=color, expected_before=guards.one("color"), **common
        ))
    if volume_normalized is not None:
        steps.append(_writer_step(
            "volume",
            "set_mixer_volume",
            volume_normalized=volume_normalized,
            expected_before=guards.one("volume_normalized"),
            **common,
        ))
    if volume_db is not None:
        steps.append(_writer_step(
            "volume",
            "set_mixer_volume_db",
            volume_db=volume_db,
            tolerance_db=0.1 if tolerance_db is None else tolerance_db,
            expected_before=guards.model(ExpectedMixerVolumeState),
            **common,
        ))
    for label, method, value in (
        ("pan", "set_mixer_pan", pan),
        ("stereo_separation", "set_mixer_stereo_separation", stereo_separation),
        ("muted", "set_mixer_mute", muted),
        ("soloed", "set_mixer_solo", soloed),
        ("armed", "set_mixer_arm", armed),
    ):
        if value is not None:
            steps.append(_writer_step(
                label, method, **{label: value}, expected_before=guards.one(label), **common
            ))
    for band in eq:
        steps.append(_writer_step(
            f"eq_band_{band.band_index}",
            "set_mixer_eq",
            band_index=band.band_index,
            gain_normalized=band.gain_normalized,
            frequency_normalized=band.frequency_normalized,
            expected_before=band.expected_before,
            **common,
        ))
    for send in sends:
        destination = send.destination_track_index
        guard = send.expected_before
        if send.enabled is not None:
            steps.append(_writer_step(
                f"send_{destination}",
                "set_mixer_send",
                destination_track_index=destination,
                enabled=send.enabled,
                expected_before=None if guard is None else guard.enabled,
                **common,
            ))
        if send.level_normalized is not None:
            steps.append(_writer_step(
                f"send_{destination}_level",
                "set_mixer_send_level",
                destination_track_index=destination,
                level_normalized=send.level_normalized,
                expected_before=None if guard is None else guard.level_normalized,
                **common,
            ))
    if select:
        steps.append(_writer_step(
            "select",
            "select_mixer_track",
            expected_before=guards.one("active_track_index"),
            **common,
        ))
    guards.refuse_unused(tool)
    execution = _execute(
        steps,
        tool=tool,
        stop_on_unverified=stop_on_unverified,
        session_fingerprint=session_fingerprint,
    )
    return _result(MixerTrackEditResult, MixerTrackEditItem, execution, track_index=track_index)


def set_channel(
    *,
    channel_index: int,
    name: str | None = None,
    color: int | None = None,
    mixer_destination: int | None = None,
    volume_normalized: float | None = None,
    pan: float | None = None,
    muted: bool | None = None,
    soloed: bool | None = None,
    pitch_normalized: float | None = None,
    select: bool = False,
    expected_before: ExpectedChannelFields | None = None,
    session_fingerprint: str | None = None,
    stop_on_unverified: bool = True,
) -> ChannelEditResult:
    """Change several fields of one global Channel Rack channel in a fixed order.

    The channel fingerprint covers the name, color, and mixer routing, so the
    first write that changes one of them makes it stale. Writes that leave it
    intact run first, and the fingerprint guards every write up to and
    including the first one that changes it; the session pin covers the rest.
    """

    tool = "channel_set"
    if type(select) is not bool:
        raise EditRefusal("select must be true or false")
    guards = _Guards(expected_before)
    fingerprint = guards.take("channel_fingerprint")
    fingerprint_current = True
    steps: list[_Step] = []

    def guard(
        model: type[TrackBContract], *fields: str, changes_fingerprint: bool = False
    ) -> Any:
        nonlocal fingerprint_current
        values = guards.take(*fields)
        if fingerprint_current:
            values = {**fingerprint, **values}
        if changes_fingerprint:
            fingerprint_current = False
        return model(**values) if values else None

    if volume_normalized is not None or pan is not None or muted is not None:
        steps.append(_controller_step(
            "mix",
            "set_channel_mix",
            channel_index=channel_index,
            volume_normalized=volume_normalized,
            pan=pan,
            muted=muted,
            expected_before=guard(ExpectedChannelMixState, "volume_normalized", "pan", "muted"),
        ))
    if soloed is not None:
        steps.append(_controller_step(
            "soloed",
            "set_channel_solo",
            channel_index=channel_index,
            soloed=soloed,
            expected_before=guard(ExpectedChannelSoloState, "soloed"),
        ))
    if pitch_normalized is not None:
        steps.append(_controller_step(
            "pitch",
            "set_channel_pitch",
            channel_index=channel_index,
            pitch_normalized=pitch_normalized,
            expected_before=guard(ExpectedChannelPitchState, "pitch_normalized"),
        ))
    if name is not None or color is not None:
        steps.append(_controller_step(
            "identity",
            "set_channel_identity",
            channel_index=channel_index,
            name=name,
            color=color,
            expected_before=guard(
                ExpectedChannelIdentityState, "name", "color", changes_fingerprint=True
            ),
        ))
    if mixer_destination is not None:
        steps.append(_controller_step(
            "route",
            "route_channel_to_mixer",
            channel_index=channel_index,
            mixer_destination=mixer_destination,
            expected_before=guard(
                ExpectedChannelRouteState, "mixer_destination", changes_fingerprint=True
            ),
        ))
    if select:
        selection = guards.take("selected_channel_indices")
        steps.append(_controller_step(
            "select",
            "select_channel",
            channel_index=channel_index,
            expected_before=ExpectedChannelSelectionState(**selection) if selection else None,
        ))
    if fingerprint and not any(step.label != "select" for step in steps):
        raise EditRefusal(
            "channel_set expected_before.channel_fingerprint is checked only by "
            "writes other than select; this call has none"
        )
    guards.refuse_unused(tool)
    execution = _execute(
        steps,
        tool=tool,
        stop_on_unverified=stop_on_unverified,
        session_fingerprint=session_fingerprint,
    )
    return _result(ChannelEditResult, ChannelEditItem, execution, channel_index=channel_index)


def set_pattern(
    *,
    pattern_number: int,
    name: str | None = None,
    color: int | None = None,
    length_beats: int | None = None,
    select: bool = False,
    expected_before: ExpectedPatternFields | None = None,
    session_fingerprint: str | None = None,
    stop_on_unverified: bool = True,
) -> PatternEditResult:
    """Change one pattern's name, color, and length, then optionally select it."""

    tool = "pattern_set"
    if type(select) is not bool:
        raise EditRefusal("select must be true or false")
    guards = _Guards(expected_before)
    steps: list[_Step] = []
    if name is not None or color is not None:
        steps.append(_controller_step(
            "identity",
            "set_pattern_identity",
            pattern_number=pattern_number,
            name=name,
            color=color,
            expected_before=guards.model(ExpectedPatternIdentityState),
        ))
    if length_beats is not None:
        steps.append(_controller_step(
            "length",
            "set_pattern_length",
            pattern_number=pattern_number,
            length_beats=length_beats,
            expected_before=guards.model(ExpectedPatternLengthState),
        ))
    if select:
        steps.append(_controller_step(
            "select",
            "select_pattern",
            pattern_number=pattern_number,
            expected_before=guards.model(ExpectedPatternSelectionState),
        ))
    guards.refuse_unused(tool)
    execution = _execute(
        steps,
        tool=tool,
        stop_on_unverified=stop_on_unverified,
        session_fingerprint=session_fingerprint,
    )
    return _result(PatternEditResult, PatternEditItem, execution, pattern_number=pattern_number)


def set_playlist_track(
    *,
    track_index: int,
    name: str | None = None,
    color: int | None = None,
    muted: bool | None = None,
    soloed: bool | None = None,
    selected: bool | None = None,
    expected_before: ExpectedPlaylistTrackFields | None = None,
    session_fingerprint: str | None = None,
    stop_on_unverified: bool = True,
) -> PlaylistTrackEditResult:
    """Change one Playlist track's name, color, and mute/solo/selection states."""

    tool = "playlist_set_track"
    guards = _Guards(expected_before)
    steps: list[_Step] = []
    if name is not None or color is not None:
        steps.append(_controller_step(
            "identity",
            "set_playlist_track_identity",
            track_index=track_index,
            name=name,
            color=color,
            expected_before=guards.model(ExpectedPlaylistTrackIdentityState),
        ))
    if muted is not None or soloed is not None or selected is not None:
        steps.append(_controller_step(
            "state",
            "set_playlist_track_state",
            track_index=track_index,
            muted=muted,
            soloed=soloed,
            selected=selected,
            expected_before=guards.model(ExpectedPlaylistTrackState),
        ))
    guards.refuse_unused(tool)
    execution = _execute(
        steps,
        tool=tool,
        stop_on_unverified=stop_on_unverified,
        session_fingerprint=session_fingerprint,
    )
    return _result(
        PlaylistTrackEditResult, PlaylistTrackEditItem, execution, track_index=track_index
    )


def set_transport(
    *,
    stop: bool = False,
    playing: bool | None = None,
    tempo_bpm: float | None = None,
    time_signature_numerator: int | None = None,
    loop_mode: LoopMode | None = None,
    metronome: bool | None = None,
    precount: bool | None = None,
    recording: bool | None = None,
    position_normalized: float | None = None,
    position_tolerance: float | None = None,
    expected_before: ExpectedTransportFields | None = None,
    session_fingerprint: str | None = None,
    stop_on_unverified: bool = True,
) -> TransportEditResult:
    """Change transport and project timing settings in a fixed order.

    Stopping, pausing, and turning recording off happen first; turning
    recording on and starting playback happen last. FL refuses tempo and
    position changes while playing (and tempo while recording), so this order
    lets one call stop, adjust, and restart.
    """

    tool = "transport_set"
    if type(stop) is not bool:
        raise EditRefusal("stop must be true or false")
    if stop and (playing is not None or position_normalized is not None):
        raise EditRefusal(
            "stop already sets playing=false and rewinds to position 0; "
            "do not combine it with playing or position_normalized"
        )
    if position_tolerance is not None and position_normalized is None:
        raise EditRefusal("position_tolerance applies only to a position_normalized change")
    guards = _Guards(expected_before)
    # Writes that release the transport, those that need it released, and
    # those that engage it again: recording is armed before playback starts.
    release: list[_Step] = []
    steps: list[_Step] = []
    engage: list[_Step] = []
    start: list[_Step] = []
    if stop:
        release.append(_controller_step(
            "stop",
            "stop",
            expected_before=guards.model(ExpectedStopState),
        ))
    if playing is not None:
        (start if playing else release).append(_controller_step(
            "playing",
            "set_playing",
            playing=playing,
            expected_before=guards.model(ExpectedPlayingState),
        ))
    if tempo_bpm is not None:
        steps.append(_controller_step(
            "tempo",
            "set_tempo",
            tempo_bpm=tempo_bpm,
            expected_before=guards.model(ExpectedTempoState),
        ))
    if time_signature_numerator is not None:
        steps.append(_controller_step(
            "time_signature_numerator",
            "set_time_signature_numerator",
            numerator=time_signature_numerator,
            expected_before=guards.model(
                ExpectedTimeSignatureState, numerator="time_signature_numerator"
            ),
        ))
    if loop_mode is not None:
        steps.append(_controller_step(
            "loop_mode",
            "set_loop_mode",
            loop_mode=loop_mode,
            expected_before=guards.model(ExpectedLoopModeState),
        ))
    if metronome is not None:
        steps.append(_controller_step(
            "metronome",
            "set_metronome",
            enabled=metronome,
            expected_before=guards.model(ExpectedMetronomeState, enabled="metronome"),
        ))
    if precount is not None:
        steps.append(_controller_step(
            "precount",
            "set_precount",
            enabled=precount,
            expected_before=guards.model(ExpectedPrecountState, enabled="precount"),
        ))
    if recording is not None:
        (engage if recording else release).append(_controller_step(
            "recording",
            "set_recording",
            recording=recording,
            expected_before=guards.model(ExpectedRecordingState),
        ))
    if position_normalized is not None:
        steps.append(_controller_step(
            "position",
            "set_song_position",
            position_normalized=position_normalized,
            tolerance=0.0001 if position_tolerance is None else position_tolerance,
            expected_before=guards.model(ExpectedSongPositionState),
        ))
    guards.refuse_unused(tool)
    execution = _execute(
        release + steps + engage + start,
        tool=tool,
        stop_on_unverified=stop_on_unverified,
        session_fingerprint=session_fingerprint,
    )
    return _result(TransportEditResult, TransportEditItem, execution)
