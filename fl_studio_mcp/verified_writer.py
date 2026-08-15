"""Readback-verified write surface for FL Studio 2026.

Ten commands, each changing exactly one thing on one mixer track and then
reading FL back to say what actually happened.  This module is the typed,
fail-closed boundary between an MCP tool and ``BridgeClient``; it is the write
counterpart of :mod:`fl_studio_mcp.readonly_inspector` and shares that module's
handshake gate so the FL Studio version rules are stated exactly once.

Three things are worth reading before changing anything here:

* **The bridge is the authority on whether writes are possible.**  FL Studio
  must have been launched with ``FL_BRIDGE_ENABLE_WRITES=1``; the flag is read
  once when FL loads the script, so it cannot be turned on from out here.  When
  the running bridge does not report that surface, every write refuses locally
  with :class:`VerifiedWritesUnavailable`, which names the flag.  There is
  deliberately no second, client-side enable: a flag here could only ever
  disagree with the bridge.

* **``verified`` is never inferred.**  It is the bridge's own observation, made
  by reading FL back on a *later* idle tick, and it is passed through
  untouched.  A write FL accepted and then ignored comes back as
  ``verified=false`` with a leading warning, not as an exception and never as
  success.  A missing ``verified`` field is a protocol error, not a default.

* **These tools apply the change.**  There is no candidate, approval, or
  rollback ceremony; FL Studio's own undo is the safety net.  Whether an undo
  point actually appeared is *observed* and reported as
  ``undo_point_created`` -- FL's ``saveUndo`` neither returns a result nor
  raises when it declines, so the bridge watches the undo history across the
  call rather than asserting the guarantee.  ``None`` means FL would not say,
  which is not the same as success.  The two things refused without an
  explicit opt-in are the master bus and out-of-range values.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, cast

from .bridge_client import BridgeError, get_client
from .contracts import (
    ConnectionInfo,
    EqBandObservation,
    ExpectedEqBandState,
    ExpectedPluginParameterState,
    PluginParameterObservation,
    PluginVerificationBasis,
    VerifiedMixerEqWrite,
    VerifiedMixerMuteWrite,
    VerifiedMixerNameWrite,
    VerifiedMixerPanWrite,
    VerifiedMixerSendLevelWrite,
    VerifiedMixerSendWrite,
    VerifiedMixerVolumeWrite,
    VerifiedPluginDisplayWrite,
    VerifiedPluginOptionWrite,
    VerifiedPluginParameterWrite,
)
from .readonly_inspector import (
    BridgeLike,
    IncompatibleFLStudio,
    connection_from_ping,
)


# Exactly the commands the bridge locks behind FL_BRIDGE_ENABLE_WRITES=1.
WRITE_COMMANDS = frozenset(
    {
        "mixer.set_volume",
        "mixer.set_pan",
        "mixer.set_mute",
        "mixer.set_eq",
        "mixer.set_name",
        "mixer.set_send",
        "mixer.set_send_level",
        "plugin.set_param",
        "plugin.set_param_display",
        "plugin.set_param_option",
    }
)

# FL truncates a mixer track name well before this, but a bound here keeps a
# runaway string off the SysEx wire in the first place.
MAX_TRACK_NAME_LENGTH = 64

MAX_EFFECT_SLOT_INDEX = 9

WRITES_DISABLED_HELP = (
    "This FL Studio bridge cannot apply writes: it reports bridge_mode={mode!r} "
    "and verified_writes_enabled={enabled!r}. Quit FL Studio and relaunch it "
    "with FL_BRIDGE_ENABLE_WRITES=1 in its environment, then use Reload script "
    "in View > Script output. The flag is read once when FL loads the bridge, "
    "so it cannot be enabled from this connector; a bridge that can write "
    "reports bridge_mode='write_test' and verified_writes_enabled=true. Reading "
    "the project works either way."
)

UNVERIFIED_WARNING = (
    "UNVERIFIED: FL Studio accepted this write, but reading it back on a later "
    "idle tick did not show the requested value, so the control may not have "
    "moved. Nothing was retried and nothing was rolled back. Re-read the track "
    "before deciding what to do next."
)

MASTER_REFUSAL = (
    "refusing to write to mixer track 0 (Master) unless allow_master is true; "
    "nothing here decides on its own that the master bus is what was meant"
)

PROVENANCE_REFUSAL = (
    "refusing to write through an unverified FL bridge: provenance is {status!r}. "
    "Install this package's bridge with postfader-install-bridge, reload the "
    "script in FL Studio, and read the connection state again"
)

SESSION_FINGERPRINT_RE = re.compile(r"[0-9a-f]{32}")


class VerifiedWritesUnavailable(RuntimeError):
    """The running bridge will not dispatch the verified write commands."""


class WriteBoundaryViolation(RuntimeError):
    """Raised when code attempts a command outside the verified write surface."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _optional_float(value: Any) -> float | None:
    """Coerce an observed readback, keeping an unusable one as unknown."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _strict_bool(payload: dict[str, Any], field: str) -> bool:
    """Read a flag the bridge must state; never invent one."""
    value = payload.get(field)
    if not isinstance(value, bool):
        raise ValueError(
            f"FL bridge did not report {field!r} as a boolean, so this write's "
            "outcome is unknown"
        )
    return value


def _session_precondition(value: Any) -> str | None:
    """Validate an optional bridge-lifetime guard before any dispatch."""
    if value is None:
        return None
    if not isinstance(value, str) or SESSION_FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError(
            "session_fingerprint must be exactly 32 lowercase hexadecimal characters"
        )
    return value


def _precondition_arguments(
    session_fingerprint: str | None, expected_before: Any
) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    if session_fingerprint is not None:
        arguments["session_fingerprint"] = session_fingerprint
    if expected_before is not None:
        arguments["expected_before"] = (
            expected_before.model_dump(exclude_none=True)
            if hasattr(expected_before, "model_dump")
            else expected_before
        )
    return arguments


def _precondition_result(
    payload: dict[str, Any],
    connection: ConnectionInfo,
    *,
    session_requested: bool,
    expected_requested: bool,
) -> dict[str, Any]:
    """Validate bridge proof metadata instead of coercing it into success."""
    echoed = payload.get("session_fingerprint")
    if (
        not isinstance(echoed, str)
        or SESSION_FINGERPRINT_RE.fullmatch(echoed) is None
    ):
        raise ValueError(
            "FL bridge did not echo a valid session fingerprint, so this write's "
            "session is unknown"
        )
    if connection.session_fingerprint is None or echoed != connection.session_fingerprint:
        raise ValueError(
            "FL bridge session changed between the pre-write handshake and the "
            "write reply; re-read project state before deciding what happened"
        )
    session_applied = _strict_bool(payload, "session_precondition_applied")
    expected_applied = _strict_bool(payload, "expected_before_applied")
    if session_applied != session_requested or expected_applied != expected_requested:
        raise ValueError(
            "FL bridge reported precondition metadata that contradicts the request, "
            "so this write's safety checks are unknown"
        )
    return {
        "session_fingerprint": echoed,
        "session_precondition_applied": session_applied,
        "expected_before_applied": expected_applied,
    }


def _plugin_verification_basis(
    payload: dict[str, Any], expected: PluginVerificationBasis
) -> PluginVerificationBasis:
    """Preserve current proof detail while accepting older protocol-2 replies."""
    value = payload.get("verification_basis")
    # The first protocol-2 write bridge already reported the two observations
    # this enum summarizes, but did not carry the enum itself. Derive it from
    # those observations so a stale installed bridge cannot mutate FL and then
    # turn a successful call into a retry-prone protocol error.
    if value is None:
        return expected
    allowed = {"value_readback", "display_change_only", "none"}
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(
            "FL bridge did not report 'verification_basis' as one of "
            "'value_readback', 'display_change_only', or 'none', so this "
            "write's proof strength is unknown"
        )
    reported = cast(PluginVerificationBasis, value)
    if reported != expected:
        raise ValueError(
            "FL bridge reported verification fields that contradict each other, "
            "so this write's proof strength is unknown"
        )
    return reported


def _echoed_track(payload: dict[str, Any], requested: int) -> int:
    """Use the index the bridge resolved, and refuse a mismatch."""
    value = payload.get("track")
    if not isinstance(value, int) or isinstance(value, bool) or value != requested:
        raise ValueError(
            f"FL bridge reported writing mixer track {value!r} when track "
            f"{requested} was requested"
        )
    return value


def _index(value: Any, label: str, *, low: int, high: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if value < low or (high is not None and value > high):
        bound = f"{low}..{high}" if high is not None else f"{low} or greater"
        raise ValueError(f"{label} must be {bound} (got {value})")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be true or false")
    return value


def _normalized(value: Any, label: str, *, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{label} must be a finite number")
    if number < low or number > high:
        raise ValueError(f"{label} must be within {low:g}..{high:g} (got {number:g})")
    return number


def _show(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.4f}"


def _verification(verified: bool, landed: str, missed: str) -> tuple[str, list[str]]:
    """Return the summary sentence and the warnings for one outcome."""
    if verified:
        return landed, []
    return missed, [UNVERIFIED_WARNING]


def _eq_observation(raw: Any) -> EqBandObservation:
    payload = raw if isinstance(raw, dict) else {}
    return EqBandObservation(
        gain_normalized=_optional_float(payload.get("gain")),
        gain_db=_optional_float(payload.get("gain_db")),
        frequency_normalized=_optional_float(payload.get("freq")),
        frequency_hz=_optional_float(payload.get("freq_hz")),
    )


def _parameter_observation(raw: Any) -> PluginParameterObservation:
    payload = raw if isinstance(raw, dict) else {}
    display = payload.get("display")
    text = str(display) if display not in (None, "") else None
    return PluginParameterObservation(
        normalized_value=_optional_float(payload.get("value")),
        display_text=text,
        display_text_available=text is not None,
    )


class WriteGateway:
    """Narrow bridge adapter that can dispatch nothing but the verified writes.

    The mirror image of :class:`~fl_studio_mcp.readonly_inspector.ReadOnlyGateway`:
    an allowlist, so a caller cannot reach reads, transport control, or anything
    outside the ten verified writes by inventing a bridge command name here.
    """

    ALLOWED_COMMANDS = WRITE_COMMANDS

    def __init__(self, client: BridgeLike | None = None):
        self._client = client or get_client()

    @property
    def transport(self) -> str:
        value = getattr(self._client, "transport", "unknown")
        return value if value in {"tcp", "files", "midi", "none"} else "unknown"

    def ping(self) -> dict[str, Any]:
        return self._client.ping()

    def call(self, command: str, **arguments: Any) -> dict[str, Any]:
        if command not in self.ALLOWED_COMMANDS:
            raise WriteBoundaryViolation(
                f"bridge operation {command!r} is not in the verified write allowlist"
            )
        result = self._client.call(command, **arguments)
        if not isinstance(result, dict):
            raise ValueError(f"FL bridge returned a malformed reply to {command!r}")
        return result


class VerifiedWriter:
    """High-level typed writer used by the MCP tools and validation scripts."""

    def __init__(self, gateway: WriteGateway | None = None):
        self.gateway = gateway or WriteGateway()

    # -- availability ----------------------------------------------------

    def connection_info(self) -> ConnectionInfo:
        try:
            ping = self.gateway.ping()
        except BridgeError as exc:
            return ConnectionInfo(
                connected=False,
                compatible=False,
                compatibility_reason="no live FL Studio 2026 handshake was available",
                bridge_transport="none",
                error=str(exc),
            )
        return connection_from_ping(ping, self.gateway.transport)

    def _require_writable(
        self, session_fingerprint: str | None = None
    ) -> ConnectionInfo:
        """Refuse before touching the project, with the actionable reason."""
        session_fingerprint = _session_precondition(session_fingerprint)
        connection = self.connection_info()
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
        if session_fingerprint is not None:
            if connection.session_fingerprint is None:
                raise VerifiedWritesUnavailable(
                    "refusing the session-guarded write because the running bridge "
                    "did not report a valid session fingerprint; reload the packaged "
                    "bridge and re-read project state"
                )
            if session_fingerprint != connection.session_fingerprint:
                raise VerifiedWritesUnavailable(
                    "session precondition failed before dispatch: FL Studio reloaded "
                    "the bridge since the observed state; re-read before writing"
                )
        return connection

    def _target(self, track_index: Any, allow_master: bool) -> int:
        """Validate the target locally; the bridge refuses master again itself."""
        index = _index(track_index, "track_index", low=0)
        if index == 0 and not allow_master:
            raise ValueError(MASTER_REFUSAL)
        return index

    def _call_guarded(
        self,
        command: str,
        arguments: dict[str, Any],
        *,
        session_fingerprint: str | None,
        expected_before: Any,
    ) -> tuple[dict[str, Any], ConnectionInfo, dict[str, Any]]:
        """Handshake, dispatch once, and validate the bridge's guard report."""
        session = _session_precondition(session_fingerprint)
        connection = self._require_writable(session)
        guarded = dict(arguments)
        guarded.update(_precondition_arguments(session, expected_before))
        raw = self.gateway.call(command, **guarded)
        metadata = _precondition_result(
            raw,
            connection,
            session_requested=session is not None,
            expected_requested=expected_before is not None,
        )
        return raw, connection, metadata

    # -- the verified writes ---------------------------------------------

    def set_mixer_volume(
        self,
        *,
        track_index: int,
        volume_normalized: float,
        allow_master: bool = False,
        session_fingerprint: str | None = None,
        expected_before: float | None = None,
    ) -> VerifiedMixerVolumeWrite:
        allow_master = _boolean(allow_master, "allow_master")
        index = self._target(track_index, allow_master)
        value = _normalized(volume_normalized, "volume_normalized", low=0.0, high=1.0)
        expected = (
            None
            if expected_before is None
            else _normalized(expected_before, "expected_before", low=0.0, high=1.0)
        )
        raw, connection, metadata = self._call_guarded(
            "mixer.set_volume",
            {"track": index, "value": value, "allow_master": allow_master},
            session_fingerprint=session_fingerprint,
            expected_before=expected,
        )
        verified = _strict_bool(raw, "verified")
        after = _optional_float(raw.get("after"))
        summary, warnings = _verification(
            verified,
            f"FL read the fader back at {_show(after)} on a later idle tick, "
            f"matching the requested {_show(value)}.",
            f"FL accepted the write but read the fader back at {_show(after)}, "
            f"not the requested {_show(value)}.",
        )
        return VerifiedMixerVolumeWrite(
            applied_at=_now(),
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            track_index=_echoed_track(raw, index),
            targeted_master=index == 0,
            verified=verified,
            verification_summary=summary,
            requested_volume_normalized=value,
            before_volume_normalized=_optional_float(raw.get("before")),
            after_volume_normalized=after,
            before_volume_db=_optional_float(raw.get("before_db")),
            after_volume_db=_optional_float(raw.get("after_db")),
            warnings=list(connection.warnings) + warnings,
            **metadata,
        )

    def set_mixer_pan(
        self,
        *,
        track_index: int,
        pan: float,
        allow_master: bool = False,
        session_fingerprint: str | None = None,
        expected_before: float | None = None,
    ) -> VerifiedMixerPanWrite:
        allow_master = _boolean(allow_master, "allow_master")
        index = self._target(track_index, allow_master)
        value = _normalized(pan, "pan", low=-1.0, high=1.0)
        expected = (
            None
            if expected_before is None
            else _normalized(expected_before, "expected_before", low=-1.0, high=1.0)
        )
        raw, connection, metadata = self._call_guarded(
            "mixer.set_pan",
            {"track": index, "value": value, "allow_master": allow_master},
            session_fingerprint=session_fingerprint,
            expected_before=expected,
        )
        verified = _strict_bool(raw, "verified")
        after = _optional_float(raw.get("after"))
        summary, warnings = _verification(
            verified,
            f"FL read the pan back at {_show(after)} on a later idle tick, "
            f"matching the requested {_show(value)}.",
            f"FL accepted the write but read the pan back at {_show(after)}, "
            f"not the requested {_show(value)}.",
        )
        return VerifiedMixerPanWrite(
            applied_at=_now(),
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            track_index=_echoed_track(raw, index),
            targeted_master=index == 0,
            verified=verified,
            verification_summary=summary,
            requested_pan=value,
            before_pan=_optional_float(raw.get("before")),
            after_pan=after,
            warnings=list(connection.warnings) + warnings,
            **metadata,
        )

    def set_mixer_name(
        self,
        *,
        track_index: int,
        name: str,
        allow_master: bool = False,
        session_fingerprint: str | None = None,
        expected_before: str | None = None,
    ) -> VerifiedMixerNameWrite:
        """Name one mixer track. The empty string restores FL's default."""
        allow_master = _boolean(allow_master, "allow_master")
        index = self._target(track_index, allow_master)
        if not isinstance(name, str):
            raise ValueError("name must be a string")
        if len(name) > MAX_TRACK_NAME_LENGTH:
            raise ValueError(
                f"name must be at most {MAX_TRACK_NAME_LENGTH} characters"
            )
        if expected_before is not None:
            if not isinstance(expected_before, str):
                raise ValueError("expected_before must be a string")
            if len(expected_before) > MAX_TRACK_NAME_LENGTH:
                raise ValueError(
                    f"expected_before must be at most {MAX_TRACK_NAME_LENGTH} characters"
                )
        raw, connection, metadata = self._call_guarded(
            "mixer.set_name",
            {"track": index, "name": name, "allow_master": allow_master},
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        verified = _strict_bool(raw, "verified")
        after = raw.get("after")
        after_name = None if after is None else str(after)
        restored = bool(raw.get("restored_default"))
        if restored:
            landed = (
                f"FL restored the track's default name, {after_name!r}, on a "
                "later idle tick."
            )
        else:
            landed = f"FL read the name back as {after_name!r} on a later idle tick."
        summary, warnings = _verification(
            verified,
            landed,
            f"FL accepted the write but read the name back as {after_name!r}, "
            f"not the requested {name!r}.",
        )
        return VerifiedMixerNameWrite(
            applied_at=_now(),
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            track_index=_echoed_track(raw, index),
            targeted_master=index == 0,
            verified=verified,
            verification_summary=summary,
            requested_name=name,
            before_name=None if raw.get("before") is None else str(raw["before"]),
            after_name=after_name,
            restored_default=restored,
            warnings=list(connection.warnings) + warnings,
            **metadata,
        )

    def _send_pair(
        self, track_index: Any, destination_track_index: Any, allow_master: bool
    ) -> tuple[int, int]:
        """Resolve a send's two ends.

        Only the source is guarded by ``allow_master``. Sending *to* Master is
        what almost every track already does, so refusing it by default would
        refuse the ordinary case.
        """
        source = self._target(track_index, allow_master)
        destination = _index(destination_track_index, "destination_track_index", low=0)
        if destination == source:
            raise ValueError(
                f"a mixer track cannot send to itself (track {source})"
            )
        return source, destination

    def set_mixer_send(
        self,
        *,
        track_index: int,
        destination_track_index: int,
        enabled: bool,
        allow_master: bool = False,
        session_fingerprint: str | None = None,
        expected_before: bool | None = None,
    ) -> VerifiedMixerSendWrite:
        """Create or tear down one send. A stated state, never a toggle."""
        allow_master = _boolean(allow_master, "allow_master")
        source, destination = self._send_pair(
            track_index, destination_track_index, allow_master
        )
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        if expected_before is not None and not isinstance(expected_before, bool):
            raise ValueError("expected_before must be true or false")
        raw, connection, metadata = self._call_guarded(
            "mixer.set_send",
            {
                "track": source,
                "to": destination,
                "enabled": enabled,
                "allow_master": allow_master,
            },
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        verified = _strict_bool(raw, "verified")
        after = _optional_bool(raw.get("after"))
        wanted = "on" if enabled else "off"
        summary, warnings = _verification(
            verified,
            f"FL read the send from track {source} to track {destination} back "
            f"as {wanted} on a later idle tick.",
            f"FL accepted the write but the send from track {source} to track "
            f"{destination} did not read back as {wanted}.",
        )
        return VerifiedMixerSendWrite(
            applied_at=_now(),
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            track_index=_echoed_track(raw, source),
            targeted_master=source == 0,
            destination_track_index=destination,
            verified=verified,
            verification_summary=summary,
            requested_enabled=enabled,
            before_enabled=_optional_bool(raw.get("before")),
            after_enabled=after,
            level_normalized=_optional_float(raw.get("level")),
            warnings=list(connection.warnings) + warnings,
            **metadata,
        )

    def set_mixer_send_level(
        self,
        *,
        track_index: int,
        destination_track_index: int,
        level_normalized: float,
        allow_master: bool = False,
        session_fingerprint: str | None = None,
        expected_before: float | None = None,
    ) -> VerifiedMixerSendLevelWrite:
        """Set how much of one track reaches another. 0.8 is unity.

        The send has to exist first. FL raises rather than reporting a level
        for an inactive route, so the bridge refuses this outright with a
        message naming ``fl_set_mixer_send`` instead of writing something it
        could never read back.
        """
        allow_master = _boolean(allow_master, "allow_master")
        source, destination = self._send_pair(
            track_index, destination_track_index, allow_master
        )
        value = _normalized(level_normalized, "level_normalized", low=0.0, high=1.0)
        expected = (
            None
            if expected_before is None
            else _normalized(expected_before, "expected_before", low=0.0, high=1.0)
        )
        raw, connection, metadata = self._call_guarded(
            "mixer.set_send_level",
            {
                "track": source,
                "to": destination,
                "value": value,
                "allow_master": allow_master,
            },
            session_fingerprint=session_fingerprint,
            expected_before=expected,
        )
        verified = _strict_bool(raw, "verified")
        after = _optional_float(raw.get("after"))
        summary, warnings = _verification(
            verified,
            f"FL read the send level back at {_show(after)} on a later idle "
            f"tick, matching the requested {_show(value)}.",
            f"FL accepted the write but read the send level back at "
            f"{_show(after)}, not the requested {_show(value)}.",
        )
        return VerifiedMixerSendLevelWrite(
            applied_at=_now(),
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            track_index=_echoed_track(raw, source),
            targeted_master=source == 0,
            destination_track_index=destination,
            verified=verified,
            verification_summary=summary,
            requested_level_normalized=value,
            before_level_normalized=_optional_float(raw.get("before")),
            after_level_normalized=after,
            send_active=_optional_bool(raw.get("send_active")),
            warnings=list(connection.warnings) + warnings,
            **metadata,
        )

    def set_plugin_parameter_display(
        self,
        *,
        track_index: int,
        slot_index: int,
        parameter: int | str,
        target_value: float,
        tolerance: float | None = None,
        allow_master: bool = False,
        session_fingerprint: str | None = None,
        expected_before: ExpectedPluginParameterState | None = None,
    ) -> VerifiedPluginDisplayWrite:
        """Set one plug-in parameter in the units the plug-in itself displays.

        ``parameter`` may be an index or text.  Text is matched against
        parameter names *and* display strings, because plenty of real
        third-party controls have no name at all and identify themselves only
        by what they display, such as "Auto mode".

        ``target_value`` is the number the plug-in shows -- 20 for "20 ms",
        -18 for "-18.0 dB".  The bridge searches the control until its own
        readback reports that number; no unit curve is assumed anywhere.
        Controls whose display is pure text are refused, with a message saying
        to use the normalized setter instead.
        """
        allow_master = _boolean(allow_master, "allow_master")
        index = self._target(track_index, allow_master)
        slot = _index(slot_index, "slot_index", low=0, high=MAX_EFFECT_SLOT_INDEX)
        if isinstance(parameter, bool) or not isinstance(parameter, (int, str)):
            raise ValueError("parameter must be an index or a name")
        if isinstance(parameter, int):
            _index(parameter, "parameter", low=0)
        elif not parameter.strip():
            raise ValueError("parameter name must not be empty")
        target = _normalized(
            target_value, "target_value", low=-1e6, high=1e6
        )
        if tolerance is not None:
            tolerance = _normalized(tolerance, "tolerance", low=0.0, high=1e6)
        if expected_before is not None:
            expected_before = ExpectedPluginParameterState.model_validate(
                expected_before
            )
        arguments: dict[str, Any] = {
            "track": index,
            "slot": slot,
            "param": parameter,
            "target": target,
            "allow_master": allow_master,
        }
        if tolerance is not None:
            arguments["tolerance"] = tolerance
        raw, connection, metadata = self._call_guarded(
            "plugin.set_param_display",
            arguments,
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        verified = _strict_bool(raw, "verified")
        landed = _optional_float(raw.get("landed_on"))
        after = _parameter_observation(raw.get("after"))
        summary, warnings = _verification(
            verified,
            f"FL searched the control and its own readback now reports "
            f"{_show(landed)} against the requested {_show(target)}; it "
            f"displays {after.display_text!r}.",
            f"FL could not land this control on {_show(target)}; the closest "
            f"its readback reached was {_show(landed)}, displaying "
            f"{after.display_text!r}.",
        )
        return VerifiedPluginDisplayWrite(
            applied_at=_now(),
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            track_index=_echoed_track(raw, index),
            targeted_master=index == 0,
            slot_index=slot,
            parameter_index=_index(raw.get("index", -1), "index", low=0),
            plugin_name=None if raw.get("plugin") is None else str(raw["plugin"]),
            parameter_name=None if raw.get("name") is None else str(raw["name"]),
            matched_on=str(raw.get("matched_on") or "index"),
            matched_text=(
                None if raw.get("matched_text") is None else str(raw["matched_text"])
            ),
            verified=verified,
            verification_summary=summary,
            requested_value=target,
            tolerance=_optional_float(raw.get("tolerance")) or 0.0,
            landed_value=landed,
            normalized_value=_optional_float(raw.get("normalised")),
            before=_parameter_observation(raw.get("before")),
            after=after,
            warnings=list(connection.warnings) + warnings,
            **metadata,
        )

    def set_plugin_parameter_option(
        self,
        *,
        track_index: int,
        slot_index: int,
        parameter: int | str,
        option: str,
        sweep_steps: int = 64,
        allow_master: bool = False,
        session_fingerprint: str | None = None,
        expected_before: ExpectedPluginParameterState | None = None,
    ) -> VerifiedPluginOptionWrite:
        """Set an enumerated parameter -- Key, Scale, Input Type -- by its text.

        These controls display words rather than numbers, so there is nothing
        for :meth:`set_plugin_parameter_display` to search on.

        FL cannot report a control's options, so the bridge finds them the only
        way available: by moving the control across its range and reading what
        it shows.  That means this call **moves the parameter while looking**.
        The requested text must exactly match one displayed option, ignoring
        case. If it does not, the original value is put back before the error
        is raised, and the error names everything that was found.

        The result carries ``options``, the whole enumeration in order, so one
        call is enough to learn what a control accepts.
        """
        allow_master = _boolean(allow_master, "allow_master")
        index = self._target(track_index, allow_master)
        slot = _index(slot_index, "slot_index", low=0, high=MAX_EFFECT_SLOT_INDEX)
        if isinstance(parameter, bool) or not isinstance(parameter, (int, str)):
            raise ValueError("parameter must be an index or a name")
        if isinstance(parameter, int):
            _index(parameter, "parameter", low=0)
        elif not parameter.strip():
            raise ValueError("parameter name must not be empty")
        if not isinstance(option, str) or not option.strip():
            raise ValueError("option must be a non-empty string")
        requested_option = option.strip()
        if len(requested_option) > 256:
            raise ValueError("option must be at most 256 characters")
        steps = _index(sweep_steps, "sweep_steps", low=2, high=256)
        if expected_before is not None:
            expected_before = ExpectedPluginParameterState.model_validate(
                expected_before
            )
        raw, connection, metadata = self._call_guarded(
            "plugin.set_param_option",
            {
                "track": index,
                "slot": slot,
                "param": parameter,
                "option": requested_option,
                "steps": steps,
                "allow_master": allow_master,
            },
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        verified = _strict_bool(raw, "verified")
        selected_raw = raw.get("selected")
        if (
            not isinstance(selected_raw, str)
            or not selected_raw
            or len(selected_raw) > 256
        ):
            raise ValueError("FL bridge returned a malformed selected option")
        selected = selected_raw
        if selected.casefold() != requested_option.casefold():
            raise ValueError(
                "FL bridge selected an option that does not exactly match the request"
            )
        options_raw = raw.get("options")
        if (
            not isinstance(options_raw, list)
            or any(not isinstance(item, str) for item in options_raw)
        ):
            raise ValueError("FL bridge returned malformed enumerated options")
        options = cast(list[str], options_raw)
        if selected not in options:
            raise ValueError(
                "FL bridge selected an option absent from its enumerated options"
            )
        after = _parameter_observation(raw.get("after"))
        later_display_matches = (
            after.display_text is not None
            and after.display_text.casefold() == selected.casefold()
        )
        if verified != later_display_matches:
            raise ValueError(
                "FL bridge returned contradictory plug-in option verification"
            )
        summary, warnings = _verification(
            verified,
            f"FL now shows {selected!r} for this control, matching the "
            f"requested {requested_option!r}.",
            f"FL was set to the value that showed {selected!r} during the "
            f"sweep, but it now reads {after.display_text!r}.",
        )
        return VerifiedPluginOptionWrite(
            applied_at=_now(),
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            track_index=_echoed_track(raw, index),
            targeted_master=index == 0,
            slot_index=slot,
            parameter_index=_index(raw.get("index", -1), "index", low=0),
            plugin_name=None if raw.get("plugin") is None else str(raw["plugin"]),
            parameter_name=None if raw.get("name") is None else str(raw["name"]),
            matched_on=str(raw.get("matched_on") or "index"),
            matched_text=(
                None if raw.get("matched_text") is None else str(raw["matched_text"])
            ),
            verified=verified,
            verification_summary=summary,
            requested_option=requested_option,
            selected_option=selected,
            normalized_value=_optional_float(raw.get("normalised")),
            sweep_steps=_index(raw.get("steps", steps), "steps", low=2),
            options=options,
            before=_parameter_observation(raw.get("before")),
            after=after,
            warnings=list(connection.warnings) + warnings,
            **metadata,
        )

    def set_mixer_mute(
        self,
        *,
        track_index: int,
        muted: bool,
        allow_master: bool = False,
        session_fingerprint: str | None = None,
        expected_before: bool | None = None,
    ) -> VerifiedMixerMuteWrite:
        allow_master = _boolean(allow_master, "allow_master")
        index = self._target(track_index, allow_master)
        if not isinstance(muted, bool):
            raise ValueError("muted must be true or false; this is a state, not a toggle")
        if expected_before is not None and not isinstance(expected_before, bool):
            raise ValueError("expected_before must be true or false")
        raw, connection, metadata = self._call_guarded(
            "mixer.set_mute",
            {"track": index, "muted": muted, "allow_master": allow_master},
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        verified = _strict_bool(raw, "verified")
        after = _optional_bool(raw.get("after"))
        wanted = "muted" if muted else "unmuted"
        observed = "unknown" if after is None else ("muted" if after else "unmuted")
        summary, warnings = _verification(
            verified,
            f"FL read the track back as {observed} on a later idle tick, "
            f"matching the requested {wanted} state.",
            f"FL accepted the write but read the track back as {observed}, "
            f"not the requested {wanted} state.",
        )
        return VerifiedMixerMuteWrite(
            applied_at=_now(),
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            track_index=_echoed_track(raw, index),
            targeted_master=index == 0,
            verified=verified,
            verification_summary=summary,
            requested_muted=muted,
            before_muted=_optional_bool(raw.get("before")),
            after_muted=after,
            warnings=list(connection.warnings) + warnings,
            **metadata,
        )

    def set_mixer_eq(
        self,
        *,
        track_index: int,
        band_index: int,
        gain_normalized: float | None = None,
        frequency_normalized: float | None = None,
        allow_master: bool = False,
        session_fingerprint: str | None = None,
        expected_before: ExpectedEqBandState | None = None,
    ) -> VerifiedMixerEqWrite:
        allow_master = _boolean(allow_master, "allow_master")
        index = self._target(track_index, allow_master)
        # FL Studio's built-in mixer EQ has three bands; the bridge re-checks
        # this against the live band count it can actually see.
        band = _index(band_index, "band_index", low=0, high=2)
        gain = (
            None
            if gain_normalized is None
            else _normalized(gain_normalized, "gain_normalized", low=0.0, high=1.0)
        )
        frequency = (
            None
            if frequency_normalized is None
            else _normalized(
                frequency_normalized, "frequency_normalized", low=0.0, high=1.0
            )
        )
        if gain is None and frequency is None:
            raise ValueError(
                "gain_normalized, frequency_normalized, or both must be given"
            )
        if expected_before is not None:
            expected_before = ExpectedEqBandState.model_validate(expected_before)
        arguments: dict[str, Any] = {
            "track": index,
            "band": band,
            "allow_master": allow_master,
        }
        if gain is not None:
            arguments["gain"] = gain
        if frequency is not None:
            arguments["freq"] = frequency
        raw, connection, metadata = self._call_guarded(
            "mixer.set_eq",
            arguments,
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        verified = _strict_bool(raw, "verified")
        fields = raw.get("verified_fields")
        fields = fields if isinstance(fields, dict) else {}
        gain_verified = None if gain is None else bool(fields.get("gain"))
        frequency_verified = None if frequency is None else bool(fields.get("freq"))
        after = _eq_observation(raw.get("after"))
        moved = ", ".join(
            part
            for part in (
                None if gain is None else f"gain {_show(after.gain_normalized)}",
                None
                if frequency is None
                else f"frequency {_show(after.frequency_normalized)}",
            )
            if part
        )
        summary, warnings = _verification(
            verified,
            f"FL read EQ band {band} back on a later idle tick at {moved}, "
            "matching what was requested.",
            f"FL accepted the write but read EQ band {band} back at {moved}, "
            "which does not match what was requested.",
        )
        return VerifiedMixerEqWrite(
            applied_at=_now(),
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            track_index=_echoed_track(raw, index),
            targeted_master=index == 0,
            verified=verified,
            verification_summary=summary,
            band_index=band,
            requested_gain_normalized=gain,
            requested_frequency_normalized=frequency,
            before=_eq_observation(raw.get("before")),
            after=after,
            gain_verified=gain_verified,
            frequency_verified=frequency_verified,
            warnings=list(connection.warnings) + warnings,
            **metadata,
        )

    def set_plugin_parameter(
        self,
        *,
        track_index: int,
        slot_index: int,
        parameter_index: int,
        normalized_value: float,
        allow_master: bool = False,
        session_fingerprint: str | None = None,
        expected_before: ExpectedPluginParameterState | None = None,
    ) -> VerifiedPluginParameterWrite:
        allow_master = _boolean(allow_master, "allow_master")
        index = self._target(track_index, allow_master)
        slot = _index(slot_index, "slot_index", low=0, high=MAX_EFFECT_SLOT_INDEX)
        parameter = _index(parameter_index, "parameter_index", low=0)
        value = _normalized(normalized_value, "normalized_value", low=0.0, high=1.0)
        if expected_before is not None:
            expected_before = ExpectedPluginParameterState.model_validate(
                expected_before
            )
        raw, connection, metadata = self._call_guarded(
            "plugin.set_param",
            {
                "track": index,
                "slot": slot,
                "index": parameter,
                "value": value,
                "allow_master": allow_master,
            },
            session_fingerprint=session_fingerprint,
            expected_before=expected_before,
        )
        verified = _strict_bool(raw, "verified")
        display_changed = _strict_bool(raw, "display_changed")
        reads_at_value = _strict_bool(raw, "reads_at_value")
        expected_basis = cast(
            PluginVerificationBasis,
            "value_readback"
            if reads_at_value
            else "display_change_only"
            if display_changed
            else "none",
        )
        verification_basis = _plugin_verification_basis(raw, expected_basis)
        if verification_basis != expected_basis or verified != (
            expected_basis != "none"
        ):
            raise ValueError(
                "FL bridge reported inconsistent plug-in verification fields, so "
                "this write's outcome is unknown"
            )
        after = _parameter_observation(raw.get("after"))
        if display_changed:
            landed = (
                "FL's displayed value for this parameter changed to "
                f"{after.display_text!r} on a later idle tick, which is proof it moved."
            )
        else:
            landed = (
                "FL's displayed value did not change, but the parameter now reads at "
                f"the requested {_show(value)}, so it was already there."
            )
        summary, warnings = _verification(
            verified,
            landed,
            "FL accepted the write, but on a later idle tick the displayed value "
            f"was unchanged at {after.display_text!r} and the parameter read back "
            f"at {_show(after.normalized_value)} rather than the requested "
            f"{_show(value)}. A change too small to alter the displayed text also "
            "reports unverified.",
        )
        return VerifiedPluginParameterWrite(
            applied_at=_now(),
            undo_point_created=_optional_bool(raw.get("undo_point_created")),
            track_index=_echoed_track(raw, index),
            targeted_master=index == 0,
            verified=verified,
            verification_summary=summary,
            verification_basis_detail=verification_basis,
            slot_index=slot,
            parameter_index=parameter,
            plugin_name=str(raw.get("plugin") or ""),
            parameter_name=str(raw.get("name") or ""),
            requested_normalized_value=value,
            before=_parameter_observation(raw.get("before")),
            after=after,
            display_changed=display_changed,
            reads_at_requested_value=reads_at_value,
            warnings=list(connection.warnings) + warnings,
            **metadata,
        )
