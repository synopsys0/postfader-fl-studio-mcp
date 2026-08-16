# name=Universal Bridge
# supportedDevices=Universal Bridge
# receiveFrom=
"""
Universal Bridge - FL Studio MIDI Controller Script.

Exposes a narrow FL Studio scripting surface to an external MCP server. The
bridge is locked read-only by default. A small, readback-verified write surface
can be enabled for the current bridge session by an explicit, user-confirmed
control request. FL_BRIDGE_ENABLE_WRITES=1 remains a startup compatibility path.

Design constraints this file works within:

* FL sandboxes the script interpreter. Sockets and every filesystem write
  fail by returning NULL with no exception set, so the transport that works
  in practice is MIDI SysEx through FL's own `device` module. TCP and a file
  mailbox are tried first, for hosts where they do work.
* Every FL API call must happen on FL Studio's own thread. FL calls `OnIdle`
  roughly every 20 ms, so all I/O is polled there and nothing touches the API
  from a background thread.
* `OnIdle` must stay fast or the script lags MIDI handling, so work is capped
  per tick and long scans resume across ticks.
* The file must be pure ASCII: FL reads it with the locale encoding, which is
  US-ASCII, so a single non-ASCII byte stops the script loading.
* Nothing may raise out of a callback, or FL disables the script.

Protocol: JSON request/response.
    -> {"id": 1, "cmd": "mixer.list", "args": {}}
    <- {"id": 1, "ok": true, "result": {...}}
    <- {"id": 1, "ok": false, "error": "...", "trace": "..."}
"""

import json
import hashlib
import math
import socket
import os
import select
import time
import traceback
import types

import midi
import general
import transport
import mixer
import channels
import plugins
import patterns
import playlist
import arrangement
import ui
import device

HOST = "127.0.0.1"
PORT = 20202

# Cap work per idle tick so FL's MIDI handling never starves.
MAX_COMMANDS_PER_TICK = 8
MAX_SEND_PER_TICK = 262144
MAX_CLIENTS = 4

# Scanning the whole mixer costs roughly 24 API calls per track (10 of them
# probing effect slots), so a full 126-track listing is ~3000 calls. Run in one
# callback that overruns OnIdle's ~20 ms budget on FL's main thread, which is
# also the thread driving the UI and MIDI. Long scans are therefore split into
# chunks of this many tracks and resumed on later ticks.
TRACKS_PER_TICK = 12
MIXER_SLOTS = 10
# FL scripting API 45 reports mixer.trackCount() == 127, but index 126 is the
# non-addressable Current pseudo-track. API 44 and the documented mixer surface
# report the 126 real targets directly (Master plus inserts 1..125). Never let
# the host's sentinel inflate a public count or reach a track-addressed API.
MAX_ADDRESSABLE_MIXER_TRACKS = 126
PARAMS_PER_TICK = 64
CHANNELS_PER_TICK = 8
PATTERNS_PER_TICK = 64
PLAYLIST_TRACKS_PER_TICK = 32
MAX_PATTERN_NUMBER = 999
MAX_PATTERN_NAME_LENGTH = 64
MAX_PATTERN_LENGTH_BEATS = 4096
MAX_PLAYLIST_TRACK_NAME_LENGTH = 64
MAX_ARRANGEMENT_MARKER_NAME_LENGTH = 64
MAX_ARRANGEMENT_MARKERS_PER_WRITE = 32
MAX_ARRANGEMENT_MARKERS_SCANNED = 512
# Every plug-in parameter write passes this. FL's default pickup behaviour can
# put a control into "waiting for pickup", after which it silently refuses
# further scripted writes. Disabling pickup makes repeated write/readback
# round trips land reliably. This is midi.PIM_None; it is written as a literal
# so the value cannot drift with an import.
PICKUP_NONE = 0

# How finely an enumerated control is walked to discover its options. FL has no
# way to report them, so they are found by moving the control and reading what
# it shows. 64 was sized against a 12-option musical Key selector, which it
# resolves without costing much. A control with more options than there are
# steps returns a partial list, so a caller facing a large enumeration -- a
# wavetable or preset selector, say -- should raise `steps` toward the ceiling
# and check `truncated` in the reply. See docs/plugin-support.md.
OPTION_SWEEP_STEPS = 64
MAX_OPTION_SWEEP_STEPS = 256
# Probes a display-value search may make before giving up. Each probe is two
# writes and a read, spread one probe per FL idle tick.
SOLVE_ITERATIONS = 24
MAX_PARAM_SCAN = 512

# Ceiling on how many indices one plugin.scan_params walk may examine. FL
# reports a padded maximum rather than a real count for VST plug-ins -- often
# in the thousands -- so a whole-plugin de-padding walk is thousands of
# indices; this bounds the total work even if some plug-in reports an absurd
# count, and the scan says it was truncated rather than pretending it saw
# everything.
MAX_PARAM_INDEX_SCAN = 8192

# MIDI SysEx framing. 0x7D is the manufacturer id reserved for non-commercial
# use. Chunks stay well under what CoreMIDI and FL will carry in one message.
SYSEX_ID = 0x7D
TAG_REQUEST = 0x01
TAG_RESPONSE = 0x02
# Command/result semantics remain bridge protocol 2. MIDI has a separate wire
# version because v0.13 added correlated responses and changed the frame size;
# v0.12 is the implicit wire protocol 1.
LEGACY_MIDI_WIRE_PROTOCOL_VERSION = 1
MIDI_WIRE_PROTOCOL_VERSION = 2
MIDI_WIRE_PROTOCOL_FIELD = "midi_wire_protocol_version"
# RtMidi's Windows MM input backend uses 1,024-byte SysEx buffers by
# default.  Account for the nine-byte wire header and F7 terminator so a full
# frame reaches the client intact; 1,000 also leaves modest driver headroom.
WINMM_SYSEX_BUFFER_BYTES = 1024
SYSEX_FRAME_OVERHEAD_BYTES = 10
MIDI_WIRE_SYSEX_CHUNKS = {
    LEGACY_MIDI_WIRE_PROTOCOL_VERSION: 1024,
    MIDI_WIRE_PROTOCOL_VERSION: 1000,
}
MIDI_WIRE_FRAME_BYTES = {
    version: SYSEX_FRAME_OVERHEAD_BYTES + chunk
    for version, chunk in MIDI_WIRE_SYSEX_CHUNKS.items()
}
SYSEX_CHUNK = MIDI_WIRE_SYSEX_CHUNKS[MIDI_WIRE_PROTOCOL_VERSION]
# A v0.12 client ignores the new heartbeat field. Accept its larger incoming
# request frames so old client -> new bridge remains an upgrade-safe direction;
# all new bridge responses still use the WinMM-safe current chunk size.
MAX_SYSEX_INPUT_CHUNK = MIDI_WIRE_SYSEX_CHUNKS[
    LEGACY_MIDI_WIRE_PROTOCOL_VERSION]
# WinMM/virtualMIDI can discard a burst of long SysEx messages even though
# each individual message is valid. Pace Windows responses one frame per FL
# idle callback; CoreMIDI retains the already-verified eight-frame window.
MAX_SYSEX_PER_TICK = 1 if os.name == "nt" else 8

# A request contains only one command and its bounded arguments. Responses can
# be much larger (a full mixer or de-padded parameter scan), but both directions
# need a hard ceiling before any chunks are retained. The bridge is normally
# driven by one serialized MCP client; the extra partial/ready capacity absorbs
# harmless delivery overlap without allowing a shared IAC sender to grow FL's
# embedded interpreter without bound.
# A request is one command and its bounded arguments on every transport, so
# the socket and file paths use the same ceiling the SysEx reassembler does.
# Neither is reachable inside FL Studio's sandbox, but both listen wherever the
# bridge runs outside it, and an unbounded accumulator is an unbounded
# accumulator regardless of who is expected to connect to it.
MAX_TRANSPORT_REQUEST_BYTES = 256 * 1024
MAX_SYSEX_REQUEST_BYTES = 256 * 1024
MAX_SYSEX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_SYSEX_REQUEST_PARTS = (
    MAX_SYSEX_REQUEST_BYTES + SYSEX_CHUNK - 1) // SYSEX_CHUNK
_CURRENT_MIDI_RESPONSE_PARTS = (
    MAX_SYSEX_RESPONSE_BYTES + SYSEX_CHUNK - 1) // SYSEX_CHUNK
# A v0.12 receiver permits at most 4,096 response parts. Reducing the current
# payload from 1,024 to 1,000 bytes must not let a new bridge advertise a total
# that an old client rejects before reassembly. Keep the rolling-upgrade
# direction within that published receiver ceiling.
LEGACY_MIDI_RESPONSE_PART_CEILING = (
    MAX_SYSEX_RESPONSE_BYTES
    + MIDI_WIRE_SYSEX_CHUNKS[LEGACY_MIDI_WIRE_PROTOCOL_VERSION]
    - 1
) // MIDI_WIRE_SYSEX_CHUNKS[LEGACY_MIDI_WIRE_PROTOCOL_VERSION]
MAX_SYSEX_RESPONSE_PARTS = min(
    _CURRENT_MIDI_RESPONSE_PARTS,
    LEGACY_MIDI_RESPONSE_PART_CEILING,
)
MAX_SYSEX_RESPONSE_WIRE_BYTES = min(
    MAX_SYSEX_RESPONSE_BYTES,
    MAX_SYSEX_RESPONSE_PARTS * SYSEX_CHUNK,
)
MAX_SYSEX_FRAME_BYTES = MIDI_WIRE_FRAME_BYTES[MIDI_WIRE_PROTOCOL_VERSION]
MAX_SYSEX_INPUT_FRAME_BYTES = MIDI_WIRE_FRAME_BYTES[
    LEGACY_MIDI_WIRE_PROTOCOL_VERSION]
MAX_SYSEX_PARTIAL_MESSAGES = 8
MAX_SYSEX_PARTIAL_BYTES = MAX_SYSEX_REQUEST_BYTES
MAX_SYSEX_READY_MESSAGES = 16
MAX_SYSEX_OUTBOX_FRAMES = MAX_SYSEX_RESPONSE_PARTS + MAX_SYSEX_PER_TICK
if MAX_SYSEX_FRAME_BYTES > WINMM_SYSEX_BUFFER_BYTES:
    raise RuntimeError("SysEx framing exceeds the Windows MM input buffer")
# OnIdle is normally about 50 Hz, making this roughly a ten-second deadline.
# Tick time is used instead of wall time so the FL callback never depends on a
# clock call and deterministic tests can advance it directly.
SYSEX_PARTIAL_TTL_TICKS = 500

PROTOCOL_VERSION = 2


# FL loads the copy installed under its own Hardware folder, which silently
# falls behind the repository whenever this file is edited without re-running
# scripts/install.sh. Reporting a source hash lets a client say the bridge is
# outdated instead of failing on whichever new field it happens to be missing.
#
# The value cannot be computed here: FL 2026's embedded Python cannot open
# files at all (every _io.FileIO call returns NULL without setting an
# exception, which is also why the file transport is unavailable). So the
# installer substitutes the repository hash into the deployed copy, and this
# repository copy keeps the empty placeholder that the hash is taken over.
# Do not edit the marker text; scripts/install.sh matches it exactly.
BRIDGE_SOURCE_SHA256 = ""  # injected-by-install

# Stable for exactly one loaded bridge lifetime and intentionally unrelated to
# project names or host identity. A caller can pass it back with a write to
# refuse if FL reloaded the script between observation and mutation.
SESSION_FINGERPRINT = "%016x%016x" % (
    os.getpid() & 0xFFFFFFFFFFFFFFFF,
    time.time_ns() & 0xFFFFFFFFFFFFFFFF,
)

# The bounded mutation surface: persistent commands read FL back after a later
# idle tick, while live-note dispatch reports only note-on/note-off delivery.
# It is off unless FL is launched with this flag or a user-confirmed runtime
# control request enables it. Neither path enables anything outside the twenty
# explicit command names below.
STARTUP_WRITES_ENABLED = (
    os.environ.get("FL_BRIDGE_ENABLE_WRITES", "").strip() == "1"
)
LEAN_WRITES_ENABLED = STARTUP_WRITES_ENABLED
WRITE_MODE_ORIGIN = (
    "startup_environment" if STARTUP_WRITES_ENABLED else "disabled"
)
READ_ONLY_COMMANDS = frozenset({
    "ping",
    "project.info",
    "project.history",
    "arrangement.selection",
    "mixer.list",
    "mixer.peaks",
    "mixer.track",
    "plugin.params",
    "plugin.preset_count",
    # A read, like plugin.params: it walks the same indices with the same
    # padding rule and writes nothing.
    "plugin.scan_params",
    "channels.list",
    "sequencer.get",
    "patterns.list",
    "patterns.find_empty",
    "playlist.list",
})
SESSION_CONTROL_COMMANDS = frozenset({
    "session.set_write_mode",
})
LEAN_WRITE_COMMANDS = frozenset({
    "mixer.set_volume",
    "mixer.set_volume_db",
    "mixer.set_pan",
    "mixer.set_mute",
    "mixer.set_solo",
    "mixer.set_arm",
    "mixer.set_color",
    "mixer.set_stereo_separation",
    "mixer.select_track",
    "mixer.set_eq",
    "mixer.set_name",
    "mixer.set_send",
    "mixer.set_send_level",
    "plugin.set_param",
    "plugin.set_param_display",
    "plugin.set_param_option",
    "transport.set_playing",
    "transport.stop",
    "transport.set_song_position",
    "transport.set_loop_mode",
    "transport.set_tempo",
    "transport.set_recording",
    "transport.set_metronome",
    "transport.set_precount",
    "project.set_time_signature_numerator",
    "project.undo",
    "project.redo",
    "channel.set_mix",
    "channel.set_solo",
    "channel.set_pitch",
    "channel.select",
    "pattern.select",
    "pattern.set_identity",
    "pattern.set_length",
    "playlist.set_identity",
    "playlist.set_state",
    "creative.prepare_piano_roll",
    "arrangement.add_markers",
    "automation.record_value",
    "channel.set_identity",
    "channel.route_to_mixer",
    "sequencer.set",
    "channel.trigger_note",
})

MAX_PENDING_JOBS = 32
_jobs = []     # list of _Job, chunked commands still running
NOTE_TICK_MS = 20
_idle_tick = 0
_active_notes = []  # notes whose note-on reached FL and still need note-off

# Where the file transport exchanges messages. Both sides derive it the
# same way; TMPDIR is per-application on macOS so it cannot be used here.
# The mailbox lives in a directory that already exists: FL's sandbox blocks
# creating one. Both sides search the same list in the same order.
PREFIX = "clbr-"
REQ_PREFIX = PREFIX + "req-"
RESP_PREFIX = PREFIX + "resp-"
ALIVE_NAME = PREFIX + "alive.json"


def _mailbox_candidates():
    """Existing directories that might accept writes, best first."""
    out = []
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here:
            out.append(here)          # the script's own folder
    except Exception:
        pass
    out.append(os.path.expanduser("~/Documents/Image-Line/FL Studio/Settings"))
    out.append(os.path.expanduser("~"))
    out.append("/tmp")
    seen = set()
    return [d for d in out if d and not (d in seen or seen.add(d))]


MAILBOX = None


class _Client:
    """A connected MCP server, with buffers drained across idle ticks."""

    def __init__(self, sock):
        self.sock = sock
        self.sock.setblocking(False)
        self.inbox = b""
        self.outbox = b""

    def fileno(self):
        return self.sock.fileno()

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _log(msg):
    try:
        print("[UniversalBridge] " + str(msg))
    except Exception:
        pass


def _safe(fn, default=None):
    """Call an FL API function, swallowing failures.

    Several API calls raise on tracks/plugins that are empty or on FL versions
    that lack the call, and a single failure should not sink a whole listing.
    """
    try:
        return fn()
    except Exception:
        return default


def _fl_bool(value):
    """Normalize FL's integer boolean values at the wire boundary."""
    if value in (0, 1):
        return bool(value)
    return None


def _bpm():
    """Current tempo in BPM.

    FL reports tempo scaled by 1000 (140 BPM -> 140000). Scale defensively so
    this stays correct if a future build reports plain BPM.
    """
    raw = _safe(lambda: mixer.getCurrentTempo(False), None)
    if raw is None:
        return None
    return round(raw / 1000.0, 4) if raw > 1000 else round(raw, 4)


def _strict_integer(value, label):
    """Return a JSON integer without accepting booleans, floats, or strings."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be an integer" % label)
    return value


def _fl_color_word(value):
    """Canonical unsigned spelling of FL's signed/unsigned 32-bit color."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < -(1 << 31) or value > 0xFFFFFFFF:
        return None
    return value & 0xFFFFFFFF


def _fl_color_argument(value):
    """Convert a public unsigned color word to FL's signed Python integer."""
    word = _fl_color_word(value)
    if word is None or value < 0:
        raise ValueError("color must be an integer within 0..4294967295")
    return word - (1 << 32) if word >= (1 << 31) else word


def _fl_colors_equivalent(left, right):
    """Compare the controllable 0x00BBGGRR bits; FL owns the high byte."""
    left_word = _fl_color_word(left)
    right_word = _fl_color_word(right)
    return (
        left_word is not None
        and right_word is not None
        and (left_word & 0x00FFFFFF) == (right_word & 0x00FFFFFF)
    )


def _mixer_track_count():
    """Return the count of indices that track-addressed mixer APIs accept."""
    try:
        count = int(mixer.trackCount())
    except Exception as exc:
        raise ValueError("FL did not report a valid mixer track count") from exc
    if count < 0:
        raise ValueError("FL reported a negative mixer track count")
    return min(count, MAX_ADDRESSABLE_MIXER_TRACKS)


def _mixer_track_index(value):
    """Return a valid live mixer index, never a fabricated empty track."""
    index = _strict_integer(value, "mixer track index")
    count = _mixer_track_count()
    if index < 0 or index >= count:
        raise ValueError(
            "mixer track index %d is outside the live range 0..%d"
            % (index, max(0, count - 1))
        )
    return index


def _effect_slot_index(value):
    """Return a valid zero-based mixer effect slot index."""
    slot = _strict_integer(value, "effect slot index")
    if slot < 0 or slot >= MIXER_SLOTS:
        raise ValueError("effect slot index must be 0..%d" % (MIXER_SLOTS - 1))
    return slot


def _plugin_summary(track, slot, use_global_index=False):
    if not _safe(lambda: plugins.isValid(track, slot, use_global_index), False):
        return None
    return {
        "slot": slot,
        "name": _safe(
            lambda: plugins.getPluginName(track, slot, False, use_global_index), ""
        ),
        "user_name": _safe(
            lambda: plugins.getPluginName(track, slot, True, use_global_index), ""
        ),
        "param_count": _safe(
            lambda: plugins.getParamCount(track, slot, use_global_index), 0
        ),
        "mix_level": None if use_global_index else _safe(
            lambda: mixer.getPluginMixLevel(track, slot), None
        ),
        "use_global_index": bool(use_global_index),
    }


def _track_summary(i, with_slots=True, with_peaks=False):
    out = {
        "index": i,
        "name": _safe(lambda: mixer.getTrackName(i), ""),
        "volume": _safe(lambda: mixer.getTrackVolume(i), None),
        "volume_db": _safe(lambda: mixer.getTrackVolume(i, 1), None),
        "pan": _safe(lambda: mixer.getTrackPan(i), None),
        "stereo_sep": _safe(lambda: mixer.getTrackStereoSep(i), None),
        "muted": _safe(lambda: mixer.isTrackMuted(i), None),
        "solo": _safe(lambda: mixer.isTrackSolo(i), None),
        "armed": _safe(lambda: mixer.isTrackArmed(i), None),
        "selected": _safe(lambda: mixer.isTrackSelected(i), None),
        "enabled": _safe(lambda: mixer.isTrackEnabled(i), None),
        "slots_enabled": _safe(lambda: mixer.isTrackSlotsEnabled(i), None),
        "polarity_reversed": _safe(lambda: mixer.isTrackRevPolarity(i), None),
        "channels_swapped": _safe(lambda: mixer.isTrackSwapChannels(i), None),
        "color": _safe(lambda: mixer.getTrackColor(i), None),
    }
    if with_peaks:
        out["peak_l"] = _safe(lambda: mixer.getTrackPeaks(i, midi.PEAK_L), None)
        out["peak_r"] = _safe(lambda: mixer.getTrackPeaks(i, midi.PEAK_R), None)
    if with_slots:
        slots = []
        for s in range(MIXER_SLOTS):
            p = _plugin_summary(i, s)
            if p:
                slots.append(p)
        out["plugins"] = slots
    return out


def _has_custom_name(name):
    """True when the user actually named this track.

    FL reports a default name for every mixer track ("Insert 7", "Master"),
    never an empty string, so treating any name as a sign of use marks all 127
    tracks interesting and makes only_used pointless.
    """
    name = (name or "").strip()
    if not name or name == "Master":
        return False
    if name.startswith("Insert ") and name[7:].strip().isdigit():
        return False
    return True


def _is_padding(name, display):
    """True for the empty parameter slots VST plugins pad their list with.

    FL reports a fixed padded parameter count for VST plugins regardless of how
    many the plugin really has, so a plug-in with a few dozen controls can look
    like it has thousands of knobs. The real ones carry a name or a meaningful
    display string; the padding is blank or a bare zero.
    """
    if (name or "").strip():
        return False
    return (display or "").strip() in ("", "0", "0.0000000", "0.000000")


def _param_state(track, slot, idx, use_global_index=False):
    return (
        _safe(
            lambda: plugins.getParamValue(
                idx, track, slot, use_global_index
            ), None
        ),
        _safe(
            lambda: plugins.getParamValueString(
                idx, track, slot, use_global_index
            ), ""
        ) or "",
    )


def _set_param_verified(
    track, slot, idx, value, attempts=4, use_global_index=False
):
    """Write a plugin parameter and confirm it actually landed.

    Two observed FL behaviours make a naive write unreliable:

    * A single setParamValue in an idle tick is ignored outright. Repeating
      the write within the same tick makes it take, which is why the
      search-based helpers worked while a lone write silently did nothing.
    * getParamValue often keeps reporting the previous number afterwards,
      while getParamValueString reflects the new setting. The display string
      is therefore the authority on whether a write landed.

    Returns (value_now, display_now, verified).
    """
    # Never skip the write because getParamValue claims the value is already
    # correct: that readback is precisely what cannot be trusted. Writing an
    # unchanged value is harmless, so always write and judge by what moves.
    before_v, before_d = _param_state(track, slot, idx, use_global_index)

    cur_v, cur_d = before_v, before_d
    for _ in range(attempts):
        # Always issue two writes: FL drops a lone one, and the second is
        # guaranteed to be a repeat of the same parameter.
        plugins.setParamValue(
            value, idx, track, slot, PICKUP_NONE, use_global_index
        )
        plugins.setParamValue(
            value, idx, track, slot, PICKUP_NONE, use_global_index
        )
        cur_v, cur_d = _param_state(track, slot, idx, use_global_index)
        # Check the display first. getParamValue can report a stale number
        # that happens to equal the target, which would look like success on
        # a write that never happened.
        if cur_d != before_d:
            return cur_v, cur_d, True
        if cur_v is not None and abs(cur_v - value) <= 1e-4:
            return cur_v, cur_d, True      # unchanged because already correct
    return cur_v, cur_d, False


def _save_undo(name):
    """Ask FL for an undo point and report whether one actually appeared.

    `general.saveUndo` returns nothing useful and raises nothing when it does
    not take, so the only honest way to know is to watch FL's undo history
    count across the call. That matters more than it looks: undo is the entire
    safety net for this write surface, and a caller told a change is
    reversible when no undo point exists has been given the one guarantee it
    cannot check for itself.

    Returns True only when the history demonstrably grew, False when it
    demonstrably did not, and None when FL would not say -- which is not the
    same as success and must not be reported as one.
    """
    def state():
        return (_safe(lambda: general.getUndoHistoryCount(), None),
                _safe(lambda: general.getUndoHistoryPos(), None))

    before = state()
    _safe(lambda: general.saveUndo(name, midi.UF_Plugin, True))
    after = state()
    if None in before or None in after:
        return None
    # FL caps its history, so on a full stack a new point moves the position
    # rather than growing the count. Either changing is proof one was taken;
    # neither changing is proof one was not.
    return after != before


# ---------------------------------------------------------------------------
# command handlers
# ---------------------------------------------------------------------------


def cmd_ping(a):
    api_version = _safe(lambda: general.getVersion(), 0)
    if LEAN_WRITES_ENABLED:
        # The name read-only tooling already looks for. A bridge that can write
        # must never present itself as read-only, even though the surface is
        # the explicit, readback-oriented mutation commands.
        bridge_mode = "write_test"
    else:
        bridge_mode = "read_only"
    return {
        "pong": True,
        "protocol": PROTOCOL_VERSION,
        MIDI_WIRE_PROTOCOL_FIELD: MIDI_WIRE_PROTOCOL_VERSION,
        "verified_writes_enabled": bool(LEAN_WRITES_ENABLED),
        "runtime_write_mode_control": True,
        "write_mode_origin": WRITE_MODE_ORIGIN,
        "startup_write_mode_enabled": bool(STARTUP_WRITES_ENABLED),
        "fl_version": _safe(lambda: ui.getVersion(), ""),
        # Keep the old key for protocol-1 clients, but name the value accurately
        # for new clients: this is the MIDI scripting API version, not the FL
        # application version.
        "fl_version_int": api_version,
        "midi_scripting_api_version": api_version,
        "bridge_mode": bridge_mode,
        # Lets a client detect that FL is running an outdated installed copy.
        "bridge_source_sha256": BRIDGE_SOURCE_SHA256,
        "session_fingerprint": SESSION_FINGERPRINT,
        "program_title": _safe(lambda: ui.getProgTitle(), ""),
    }


def cmd_session_set_write_mode(a):
    """Set the bounded write surface for this loaded bridge session only."""
    global LEAN_WRITES_ENABLED, WRITE_MODE_ORIGIN

    allowed = {
        "enabled",
        "confirm_user_present",
        "session_fingerprint",
    }
    unknown = set(a) - allowed
    if unknown:
        raise ValueError(
            "session.set_write_mode received unknown arguments: %s"
            % ", ".join(sorted(unknown))
        )

    enabled = _lean_bool(a, "enabled")
    confirmed = _lean_bool(a, "confirm_user_present")
    session = a.get("session_fingerprint")
    if not isinstance(session, str) or session != SESSION_FINGERPRINT:
        raise ValueError(
            "session precondition failed: the bridge was reloaded or the "
            "fingerprint is invalid; read the connection state again"
        )
    if enabled and not confirmed:
        raise ValueError(
            "enabling write mode requires confirm_user_present=true from an "
            "explicit user request"
        )

    # A long-running write may already have touched FL and still be waiting for
    # its later-idle-tick readback. Do not claim the gate is closed until that
    # command has completed and reported its outcome.
    pending_mutations = sorted({
        job.cmd for job in _jobs if job.cmd in LEAN_WRITE_COMMANDS
    })
    if not enabled and pending_mutations:
        raise ValueError(
            "cannot disable write mode while mutation commands are still in "
            "flight: %s; wait for them to finish and try again"
            % ", ".join(pending_mutations)
        )

    before = bool(LEAN_WRITES_ENABLED)
    LEAN_WRITES_ENABLED = enabled
    WRITE_MODE_ORIGIN = "runtime_request" if enabled else "disabled"
    return {
        "command": "session.set_write_mode",
        "requested_enabled": enabled,
        "before_enabled": before,
        "after_enabled": bool(LEAN_WRITES_ENABLED),
        "changed": before != bool(LEAN_WRITES_ENABLED),
        "bridge_mode": "write_test" if LEAN_WRITES_ENABLED else "read_only",
        "write_mode_origin": WRITE_MODE_ORIGIN,
        "runtime_write_mode_control": True,
        "confirmation_required": enabled,
        "confirmation_applied": enabled and confirmed,
        "session_fingerprint": SESSION_FINGERPRINT,
        "session_precondition_applied": True,
        "session_only": True,
        "startup_default_enabled": bool(STARTUP_WRITES_ENABLED),
        "project_saved": False,
    }


def _metronome_state():
    value = _safe(lambda: ui.isMetronomeEnabled(), None)
    return None if value is None else bool(value)


def _precount_state():
    value = _safe(lambda: ui.isPrecountEnabled(), None)
    return None if value is None else bool(value)


def _time_signature_snapshot():
    ppq = _strict_integer(general.getRecPPQ(), "project PPQ")
    pulses_per_bar = _strict_integer(general.getRecPPB(), "pulses per bar")
    if ppq < 1 or pulses_per_bar < 1 or pulses_per_bar % ppq:
        raise ValueError(
            "FL reported a time-signature numerator that cannot be proven from "
            "getRecPPB/getRecPPQ"
        )
    numerator = pulses_per_bar // ppq
    if numerator < 1 or numerator > 32:
        raise ValueError("FL reported a time-signature numerator outside 1..32")
    return {
        "numerator": numerator,
        "ppq": ppq,
        "pulses_per_bar": pulses_per_bar,
        "denominator_available": False,
    }


def _history_snapshot():
    position = _strict_integer(
        general.getUndoHistoryPos(), "undo history position"
    )
    count = _strict_integer(general.getUndoHistoryCount(), "undo history count")
    last = _strict_integer(
        general.getUndoHistoryLast(), "last undo history position"
    )
    # FL's public history position is one-based while getUndoHistoryLast() is
    # an independent cursor value.  In particular, a fresh real project may
    # report position=1, count=1, last=0.  Do not invent an ordering between
    # position and last that Image-Line's API does not provide.
    if position < 0 or count < 0 or last < 0 or position > count or last > count:
        raise ValueError("FL reported inconsistent undo-history bounds")
    hint = _safe(lambda: general.getUndoLevelHint(), "") or ""
    if not isinstance(hint, str):
        hint = str(hint)
    return {
        "position": position,
        "count": count,
        "last_position": last,
        "level_hint": hint[:512],
        "project_dirty_flag": _safe(lambda: general.getChangedFlag(), None),
        "can_undo": position > 1,
        "can_redo": position < count,
    }


def cmd_project_info(a):
    return {
        "project_title": _safe(lambda: general.getProjectTitle(), ""),
        "project_author": _safe(lambda: general.getProjectAuthor(), ""),
        "project_genre": _safe(lambda: general.getProjectGenre(), ""),
        "safe_to_edit": _safe(lambda: general.safeToEdit(), None),
        "tempo_bpm": _bpm(),
        "playing": _safe(lambda: transport.isPlaying(), None),
        "recording": _safe(lambda: transport.isRecording(), None),
        "song_pos": _safe(lambda: transport.getSongPos(), None),
        "song_pos_hint": _safe(lambda: transport.getSongPosHint(), ""),
        "song_length_ms": _safe(lambda: transport.getSongLength(midi.SONGLENGTH_MS), None),
        "loop_mode": _safe(lambda: transport.getLoopMode(), None),
        "ppq": _safe(lambda: general.getRecPPQ(), None),
        "pulses_per_bar": _safe(lambda: general.getRecPPB(), None),
        "time_signature_numerator": _safe(
            lambda: _time_signature_snapshot()["numerator"], None
        ),
        "mixer_track_count": _safe(lambda: _mixer_track_count(), None),
        "channel_count": _safe(lambda: channels.channelCount(), None),
        "pattern_count": _safe(lambda: patterns.patternCount(), None),
        "playlist_track_count": _safe(lambda: playlist.trackCount(), None),
        "unsaved_changes": _safe(lambda: general.getChangedFlag(), None),
        "undo_history_position": _safe(lambda: general.getUndoHistoryPos(), None),
        "undo_history_count": _safe(lambda: general.getUndoHistoryCount(), None),
        "metronome": _metronome_state(),
        "precount": _precount_state(),
    }


def cmd_project_history(a):
    if a:
        raise ValueError("project.history accepts no arguments")
    return {"command": "project.history", **_history_snapshot()}


def cmd_arrangement_selection(a):
    """Return raw selection endpoints without guessing units or presence.

    Image-Line documents selectionStart/selectionEnd as integer times but does
    not document their coordinate system or the no-selection sentinel. Read
    both endpoints twice so the caller can reject a pair that changed during
    this small, non-atomic observation. Do not retry or spin on FL's UI thread.
    """
    if a:
        raise ValueError("arrangement.selection accepts no arguments")
    first_start = _safe(lambda: arrangement.selectionStart(), None)
    first_end = _safe(lambda: arrangement.selectionEnd(), None)
    first_ppq = _safe(lambda: general.getRecPPQ(), None)

    start_hint = None
    end_hint = None
    if first_start is not None:
        start_hint = _safe(
            lambda: arrangement.currentTimeHint(1, int(first_start)), None
        )
    if first_end is not None:
        end_hint = _safe(
            lambda: arrangement.currentTimeHint(1, int(first_end)), None
        )

    second_start = _safe(lambda: arrangement.selectionStart(), None)
    second_end = _safe(lambda: arrangement.selectionEnd(), None)
    second_ppq = _safe(lambda: general.getRecPPQ(), None)

    return {
        "first_raw_start": first_start,
        "first_raw_end": first_end,
        "first_ppq": first_ppq,
        "second_raw_start": second_start,
        "second_raw_end": second_end,
        "second_ppq": second_ppq,
        "start_hint": start_hint,
        "end_hint": end_hint,
    }


def cmd_mixer_list(a):
    """Walk the mixer, yielding between chunks so no single tick runs long.

    This is a generator: the dispatcher registers it as a job and resumes it
    on later idle ticks, which keeps each callback well inside its budget even
    on a full 126-track project.
    """
    n = _mixer_track_count()
    only_used = a.get("only_used", True)
    peaks = a.get("peaks", False)
    limit = a.get("max_tracks")
    tracks = []
    scanned = 0

    for i in range(n):
        t = _track_summary(i, with_slots=True, with_peaks=peaks)
        scanned += 1
        keep = True
        if only_used:
            keep = (
                i == 0
                or t.get("plugins")
                or _has_custom_name(t.get("name"))
                or t.get("armed")
                or t.get("solo")
                or t.get("muted")
                or t.get("selected")
                or t.get("enabled") is False
                or t.get("slots_enabled") is False
                or t.get("polarity_reversed")
                or t.get("channels_swapped")
                or (t.get("volume") is not None and abs(t["volume"] - 0.8) > 1e-4)
                or (t.get("pan") is not None and abs(t["pan"]) > 1e-4)
            )
        if keep:
            tracks.append(t)
            if limit and len(tracks) >= int(limit):
                break
        if scanned % TRACKS_PER_TICK == 0:
            yield  # hand FL's thread back before starting the next chunk

    return {"track_count": n, "scanned": scanned, "tracks": tracks}


def cmd_mixer_peaks(a):
    """Return a lightweight mixer peak frame for persistent host metering."""
    unknown = set(a) - {"only_used", "max_tracks"}
    if unknown:
        raise ValueError(
            "mixer.peaks received unsupported arguments: %s"
            % ", ".join(sorted(unknown))
        )
    only_used = a.get("only_used", True)
    if type(only_used) is not bool:
        raise ValueError("only_used must be true or false")
    total = _mixer_track_count()
    maximum = _strict_integer(a.get("max_tracks", total), "max_tracks")
    if maximum < 1 or maximum > total:
        raise ValueError("max_tracks must be within 1..%d" % total)
    rows = []
    for i in range(maximum):
        name = _safe(lambda: mixer.getTrackName(i), "") or ""
        left = _safe(lambda: mixer.getTrackPeaks(i, midi.PEAK_L), None)
        right = _safe(lambda: mixer.getTrackPeaks(i, midi.PEAK_R), None)
        has_peak = (
            isinstance(left, (int, float)) and not isinstance(left, bool) and left > 0
        ) or (
            isinstance(right, (int, float)) and not isinstance(right, bool) and right > 0
        )
        if not only_used or i == 0 or _has_custom_name(name) or has_peak:
            rows.append({
                "track": i,
                "name": name,
                "volume": _safe(lambda: mixer.getTrackVolume(i), None),
                "volume_db": _safe(lambda: mixer.getTrackVolume(i, 1), None),
                "muted": _safe(lambda: mixer.isTrackMuted(i), None),
                "peak_l": left,
                "peak_r": right,
            })
        if (i + 1) % 16 == 0 and i + 1 < maximum:
            yield
    return {
        "command": "mixer.peaks",
        "session_fingerprint": SESSION_FINGERPRINT,
        "observed_idle_tick": _idle_tick,
        "playing": _safe(lambda: bool(transport.isPlaying()), None),
        "song_position": _safe(lambda: transport.getSongPos(), None),
        "track_count": total,
        "scanned_track_count": maximum,
        "partial": maximum < total,
        "only_used": only_used,
        "tracks": rows,
    }


def cmd_mixer_track(a):
    i = _mixer_track_index(a["track"])
    t = _track_summary(i, with_slots=True, with_peaks=True)
    t["eq"] = cmd_mixer_eq_get({"track": i})
    routes = []
    for d in range(_mixer_track_count()):
        if d == i:
            continue
        if _safe(lambda: mixer.getRouteSendActive(i, d), False):
            routes.append(
                {"to": d, "name": _safe(lambda: mixer.getTrackName(d), ""),
                 "level": _safe(lambda: mixer.getRouteToLevel(i, d), None)}
            )
    t["routes"] = routes
    return t


def _first_float(s):
    """Pull the first signed number out of a display string like '-18.0 dB'."""
    num = ""
    seen_digit = False
    for ch in s:
        if ch.isdigit():
            num += ch
            seen_digit = True
        elif ch in "-+" and not num:
            num += ch
        elif ch == "." and seen_digit and "." not in num:
            num += ch
        elif seen_digit:
            break
        else:
            num = ""
    try:
        return float(num)
    except ValueError:
        return None


def cmd_mixer_eq_get(a):
    i = int(a["track"])
    bands = []
    n = _safe(lambda: mixer.getEqBandCount(), 3) or 3
    for b in range(n):
        bands.append(
            {
                "band": b,
                "gain": _safe(lambda: mixer.getEqGain(i, b), None),
                "gain_db": _safe(lambda: mixer.getEqGain(i, b, 1), None),
                "freq": _safe(lambda: mixer.getEqFrequency(i, b), None),
                "freq_hz": _safe(lambda: mixer.getEqFrequency(i, b, 1), None),
                "bandwidth": _safe(lambda: mixer.getEqBandwidth(i, b), None),
            }
        )
    return {"track": i, "band_count": n, "bands": bands}


def _plugin_target(a, writing=False):
    kind = a.get("target_kind", "mixer_effect")
    if kind == "legacy":
        kind = "mixer_effect"
    if kind == "mixer_effect":
        if a.get("use_global_index", False) is not False:
            raise ValueError("mixer_effect targets require use_global_index=false")
        if "channel" in a:
            raise ValueError(
                "mixer_effect targets use track and slot, never channel"
            )
        track = _lean_track(a) if writing else _mixer_track_index(a["track"])
        slot = _effect_slot_index(a.get("slot", -1))
        return {
            "target_kind": "mixer_effect",
            "index": track,
            "track": track,
            "slot": slot,
            "use_global_index": False,
        }
    if kind == "channel_generator":
        if a.get("use_global_index") is not True:
            raise ValueError(
                "channel_generator targets require use_global_index=true"
            )
        if "track" in a:
            raise ValueError(
                "channel_generator targets use channel, never mixer track"
        )
        _require_global_scope(a)
        channel = _channel_index(a.get("channel"))
        if "slot" in a:
            generator_slot = a["slot"]
            if (
                isinstance(generator_slot, bool)
                or not isinstance(generator_slot, int)
                or generator_slot != -1
            ):
                raise ValueError("channel_generator targets require integer slot=-1")
        return {
            "target_kind": "channel_generator",
            "index": channel,
            "channel": channel,
            "slot": -1,
            "use_global_index": True,
            "index_scope": "global",
        }
    raise ValueError(
        "target_kind must be 'mixer_effect' or 'channel_generator'"
    )


def _plugin_target_report(target):
    report = {
        "target_kind": target["target_kind"],
        "slot": target["slot"],
        "use_global_index": target["use_global_index"],
    }
    if target["target_kind"] == "channel_generator":
        report["channel"] = target["channel"]
        report["index_scope"] = "global"
    else:
        report["track"] = target["track"]
    return report


def cmd_plugin_params(a):
    target = _plugin_target(a)
    track = target["index"]
    slot = target["slot"]
    use_global = target["use_global_index"]
    if not _safe(lambda: plugins.isValid(track, slot, use_global), False):
        raise ValueError("no plugin at requested %s target" % target["target_kind"])
    count = plugins.getParamCount(track, slot, use_global)
    limit = int(a.get("limit", 128))
    offset = int(a.get("offset", 0))
    if limit < 1 or limit > MAX_PARAM_SCAN:
        raise ValueError("parameter scan limit must be 1..%d" % MAX_PARAM_SCAN)
    if offset < 0:
        raise ValueError("parameter scan offset must be non-negative")
    name_filter = (a.get("filter") or "").strip().lower()
    skip_padding = a.get("skip_padding", True)
    params = []
    padding = 0
    for p in range(offset, min(count, offset + limit)):
        pname = _safe(
            lambda: plugins.getParamName(p, track, slot, use_global), ""
        ) or ""
        pdisp = _safe(
            lambda: plugins.getParamValueString(p, track, slot, use_global), ""
        ) or ""
        if skip_padding and _is_padding(pname, pdisp):
            padding += 1
        elif name_filter and name_filter not in pname.lower():
            pass
        else:
            params.append(
                {
                    "index": p,
                    "name": pname,
                    "value": _safe(
                        lambda: plugins.getParamValue(p, track, slot, use_global), None
                    ),
                    "display": pdisp,
                }
            )
        # Yield on indices examined rather than on rows kept. A padded VST map
        # is mostly filler, and counting only kept rows meant a page of a
        # sparse plug-in read every one of its indices inside a single idle
        # tick, which is the overrun the chunking exists to prevent.
        if (p - offset + 1) % PARAMS_PER_TICK == 0:
            yield
    return {
        "command": "plugin.params",
        **_plugin_target_report(target),
        "plugin": _safe(
            lambda: plugins.getPluginName(track, slot, False, use_global), ""
        ),
        "param_count": count,
        "returned": len(params),
        "padding_skipped": padding,
        "params": params,
    }


def cmd_plugin_preset_count(a):
    target = _plugin_target(a)
    index = target["index"]
    slot = target["slot"]
    use_global = target["use_global_index"]
    if not _safe(lambda: plugins.isValid(index, slot, use_global), False):
        raise ValueError("no plugin at requested %s target" % target["target_kind"])
    count = _strict_integer(
        plugins.getPresetCount(index, slot, use_global), "plug-in preset count"
    )
    if count < 0 or count > 1000000:
        raise ValueError("FL reported a plug-in preset count outside 0..1000000")
    return {
        "command": "plugin.preset_count",
        **_plugin_target_report(target),
        "plugin": _safe(
            lambda: plugins.getPluginName(index, slot, False, use_global), ""
        ),
        "plugin_user_name": _safe(
            lambda: plugins.getPluginName(index, slot, True, use_global), ""
        ),
        "param_count": _safe(
            lambda: plugins.getParamCount(index, slot, use_global), 0
        ),
        "mix_level": None if use_global else _safe(
            lambda: mixer.getPluginMixLevel(index, slot), None
        ),
        "preset_count": count,
        "unsaved_changes": _safe(lambda: general.getChangedFlag(), None),
    }


def cmd_plugin_scan_params(a):
    """De-pad a whole plug-in inside FL and answer in one round trip.

    `plugin.params` pages, so a client that wants the real parameter map of a
    VST has to ask for page after page. FL reports a padded maximum for a VST3
    - not a count - which can run to thousands of slots, with the real controls
    sparse inside it, clustered at low indices and in scattered pockets. Paging
    that at 128 per request is roughly a thousand SysEx round trips and over a
    minute of latency the first time the plug-in is touched. This walks the
    same range here, on FL's own thread, drops the padding before it ever goes
    on the wire, and replies once.

    Reality is decided by `_is_padding`, the same helper whose verdict
    `plugin.params` counts into `padding_skipped`. The two commands share that
    one rule so they can never disagree about which indices are real.

    Each real parameter comes back with its display string as well as its
    normalised value, because the display is what identifies a control: 0.6855
    says nothing, "20 ms" says which knob this is.

    Arguments, all bounds optional:
        track         mixer track index
        slot          effect slot index, 0..9
        start         first index to examine (default 0)
        end           exclusive last index to examine (default: FL's count)
        max_indices   stop after examining this many indices
        max_results   stop after collecting this many real parameters

    A scan that did not examine every index FL reports comes back with
    truncated=true and truncated_by naming the bound that stopped it, so a
    partial map is never mistaken for a complete one.

    A read. It calls nothing that changes FL, which is why it sits in
    READ_ONLY_COMMANDS next to plugin.params and needs no write flag.
    """
    target = _plugin_target(a)
    track = target["index"]
    slot = target["slot"]
    use_global = target["use_global_index"]
    if not _safe(lambda: plugins.isValid(track, slot, use_global), False):
        raise ValueError("no plugin at requested %s target" % target["target_kind"])
    # What FL claims. For a VST this is a padded maximum and never a count of
    # the parameters that exist, which is the whole reason this command exists.
    reported_count = int(
        _safe(lambda: plugins.getParamCount(track, slot, use_global), 0) or 0
    )

    start = int(a.get("start", 0))
    if start < 0:
        raise ValueError("parameter scan start must be non-negative")
    end = a.get("end")
    end = reported_count if end is None else int(end)
    if end < 0:
        raise ValueError("parameter scan end must be non-negative")
    end = min(end, reported_count)

    max_indices = a.get("max_indices")
    if max_indices is None:
        max_indices = MAX_PARAM_INDEX_SCAN
    max_indices = int(max_indices)
    if max_indices < 1 or max_indices > MAX_PARAM_INDEX_SCAN:
        raise ValueError(
            "max_indices must be 1..%d" % MAX_PARAM_INDEX_SCAN
        )
    max_results = a.get("max_results")
    if max_results is not None:
        max_results = int(max_results)
        if max_results < 1:
            raise ValueError("max_results must be at least 1")

    params = []
    padding = 0
    examined = 0
    highest = None
    stopped_by = None

    for p in range(start, end):
        if examined >= max_indices:
            stopped_by = "max_indices"
            break
        pname = _safe(
            lambda: plugins.getParamName(p, track, slot, use_global), ""
        ) or ""
        pdisp = _safe(
            lambda: plugins.getParamValueString(p, track, slot, use_global), ""
        ) or ""
        examined += 1
        highest = p
        if _is_padding(pname, pdisp):
            padding += 1
        else:
            params.append(
                {
                    "index": p,
                    "name": pname,
                    "value": _safe(
                        lambda: plugins.getParamValue(p, track, slot, use_global), None
                    ),
                    "display": pdisp,
                }
            )
            if max_results is not None and len(params) >= max_results:
                stopped_by = "max_results"
                break
        # Hand FL's thread back on indices examined, not on results kept. A
        # padded map is mostly empty, so counting kept results would run
        # thousands of indices in one callback before the count ever lined up.
        # Two or three API calls per index keeps a chunk well inside the tick.
        if examined % PARAMS_PER_TICK == 0:
            yield

    if stopped_by is None:
        # The walk finished on its own; it is still incomplete if a bound kept
        # it away from either end of the range FL reports.
        if end < reported_count:
            stopped_by = "end"
        elif start > 0:
            stopped_by = "start"

    return {
        "command": "plugin.scan_params",
        **_plugin_target_report(target),
        "plugin": _safe(
            lambda: plugins.getPluginName(track, slot, False, use_global), ""
        ),
        "reported_count": reported_count,
        "scan_start": start,
        "scan_end": end,
        "examined": examined,
        "highest_index_examined": highest,
        "real": len(params),
        "padding_skipped": padding,
        "truncated": stopped_by is not None,
        "truncated_by": stopped_by,
        "params": params,
    }


CHANNEL_TYPE_NAMES = {
    0: "sampler",
    1: "hybrid",
    2: "generator_plugin",
    3: "layer",
    4: "audio_clip",
    5: "automation_clip",
}


def _sha256_json(value):
    encoded = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _channel_index(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("channel must be an integer global index")
    index = value
    count = int(channels.channelCount(True))
    if index < 0 or index >= count:
        raise ValueError(
            "global channel index %d is outside the live range 0..%d"
            % (index, max(0, count - 1))
        )
    return index


def _require_global_scope(a):
    if a.get("index_scope") != "global":
        raise ValueError(
            "channel commands require explicit index_scope='global'; grouped "
            "or implicit indices are not accepted"
        )


def _channel_summary(i):
    name = _safe(lambda: channels.getChannelName(i, True), "") or ""
    type_code = _safe(lambda: channels.getChannelType(i, True), None)
    color = _fl_color_word(
        _safe(lambda: channels.getChannelColor(i, True), None)
    )
    destination = _safe(lambda: channels.getTargetFxTrack(i, True), None)
    plugin_name = _safe(lambda: plugins.getPluginName(i, -1, False, True), "") or ""
    param_count = _safe(lambda: plugins.getParamCount(i, -1, True), None)
    material = {
        "channel_index": i,
        "channel_type_code": type_code,
        "color": color,
        "generator_name": plugin_name or None,
        "mixer_destination": destination,
        "name": name,
        "scope": "global",
    }
    muted = _safe(lambda: channels.isChannelMuted(i, True), None)
    solo = _safe(lambda: channels.isChannelSolo(i, True), None)
    selected = _safe(lambda: channels.isChannelSelected(i, True), None)
    return {
        "index": i,
        "index_scope": "global",
        "name": name,
        "volume": _safe(lambda: channels.getChannelVolume(i, 0, True), None),
        "pan": _safe(lambda: channels.getChannelPan(i, True), None),
        "pitch": _safe(lambda: channels.getChannelPitch(i, 0, True), None),
        "pitch_semitones": _safe(
            lambda: channels.getChannelPitch(i, 1, True), None
        ),
        "pitch_range": _safe(lambda: channels.getChannelPitch(i, 2, True), None),
        "muted": None if muted is None else bool(muted),
        "solo": None if solo is None else bool(solo),
        "selected": None if selected is None else bool(selected),
        "type": type_code,
        "type_name": CHANNEL_TYPE_NAMES.get(type_code, "unknown"),
        "mixer_track": destination,
        "color": color,
        "plugin": plugin_name,
        "reported_parameter_count": param_count,
        "channel_fingerprint": _sha256_json(material),
    }


def cmd_channels_list(a):
    if a and set(a) - {"global_count"}:
        raise ValueError("channels.list accepts only global_count=true")
    if a.get("global_count", True) is not True:
        raise ValueError("channels.list exposes global channel indices only")
    n = channels.channelCount(True)
    out = []
    for i in range(n):
        out.append(_channel_summary(i))
        # A channel summary makes roughly ten FL API calls. Large racks must
        # not perform all of those synchronously inside one UI-thread callback.
        if (i + 1) % CHANNELS_PER_TICK == 0 and i + 1 < n:
            yield
    return {
        "command": "channels.list",
        "channel_count": n,
        "partial": False,
        "unsaved_changes": _safe(lambda: general.getChangedFlag(), None),
        "channels": out,
    }


def _pattern_bounds():
    count = _strict_integer(
        _safe(lambda: patterns.patternCount(), None), "pattern count"
    )
    maximum = _strict_integer(
        _safe(lambda: patterns.patternMax(), None), "maximum pattern number"
    )
    if count < 0 or count > MAX_PATTERN_NUMBER:
        raise ValueError("FL reported pattern count outside 0..999")
    if maximum < 1 or maximum > MAX_PATTERN_NUMBER:
        raise ValueError("FL reported maximum pattern number outside 1..999")
    return count, maximum


def _pattern_index(value):
    index = _strict_integer(value, "pattern number")
    _count, maximum = _pattern_bounds()
    if index < 1 or index > maximum:
        raise ValueError(
            "pattern number must be within 1..%d (got %r)" % (maximum, index)
        )
    return index


def _pattern_summary(index):
    current = _safe(lambda: patterns.patternNumber(), None)
    return {
        "pattern": index,
        "name": _safe(lambda: patterns.getPatternName(index), "") or "",
        "color": _fl_color_word(
            _safe(lambda: patterns.getPatternColor(index), None)
        ),
        "length": _safe(lambda: patterns.getPatternLength(index), None),
        "current": current == index,
        "selected": _safe(lambda: patterns.isPatternSelected(index), None),
        "default": _safe(lambda: patterns.isPatternDefault(index), None),
    }


def cmd_patterns_list(a):
    if a:
        raise ValueError("patterns.list accepts no arguments")
    count, maximum = _pattern_bounds()
    current = _strict_integer(
        _safe(lambda: patterns.patternNumber(), None), "current pattern number"
    )
    rows = []
    for index in range(1, count + 1):
        rows.append(_pattern_summary(index))
        if index % PATTERNS_PER_TICK == 0 and index < count:
            yield
    return {
        "command": "patterns.list",
        "current_pattern": current,
        "pattern_count": count,
        "pattern_max": maximum,
        "patterns": rows,
        "unsaved_changes": _safe(lambda: general.getChangedFlag(), None),
    }


def cmd_patterns_find_empty(a):
    allowed = {"start"}
    unknown = set(a) - allowed
    if unknown:
        raise ValueError(
            "patterns.find_empty received unsupported arguments: %s"
            % ", ".join(sorted(unknown))
        )
    _count, maximum = _pattern_bounds()
    start = _strict_integer(a.get("start", 1), "start pattern number")
    if start < 1 or start > maximum:
        raise ValueError("start pattern number must be within 1..%d" % maximum)
    before_current = _strict_integer(
        _safe(lambda: patterns.patternNumber(), None), "current pattern number"
    )
    found = None
    scanned = 0
    for index in range(start, maximum + 1):
        scanned += 1
        if bool(_safe(lambda index=index: patterns.isPatternDefault(index), False)):
            found = index
            break
        if scanned % PATTERNS_PER_TICK == 0 and index < maximum:
            yield
    after_current = _safe(lambda: patterns.patternNumber(), None)
    return {
        "command": "patterns.find_empty",
        "start": start,
        "empty_pattern": found,
        "scanned": scanned,
        "current_pattern_before": before_current,
        "current_pattern_after": after_current,
        "current_pattern_unchanged": after_current == before_current,
        "unsaved_changes": _safe(lambda: general.getChangedFlag(), None),
    }


def _playlist_track_count():
    count = _strict_integer(
        _safe(lambda: playlist.trackCount(), None), "Playlist track count"
    )
    if count < 0 or count > 10000:
        raise ValueError("FL reported an invalid Playlist track count")
    return count


def _playlist_track_index(value):
    index = _strict_integer(value, "Playlist track index")
    count = _playlist_track_count()
    if index < 1 or index > count:
        raise ValueError(
            "Playlist track index must be within 1..%d (got %r)" % (count, index)
        )
    return index


def _playlist_track_summary(index):
    return {
        "track": index,
        "name": _safe(lambda: playlist.getTrackName(index), "") or "",
        "color": _fl_color_word(
            _safe(lambda: playlist.getTrackColor(index), None)
        ),
        "muted": _fl_bool(_safe(lambda: playlist.isTrackMuted(index), None)),
        "solo": _fl_bool(_safe(lambda: playlist.isTrackSolo(index), None)),
        "selected": _fl_bool(
            _safe(lambda: playlist.isTrackSelected(index), None)
        ),
        "activity": _safe(lambda: playlist.getTrackActivityLevel(index), None),
    }


def cmd_playlist_list(a):
    if a:
        raise ValueError("playlist.list accepts no arguments")
    count = _playlist_track_count()
    rows = []
    for index in range(1, count + 1):
        rows.append(_playlist_track_summary(index))
        if index % PLAYLIST_TRACKS_PER_TICK == 0 and index < count:
            yield
    return {
        "command": "playlist.list",
        "track_count": count,
        "tracks": rows,
        "unsaved_changes": _safe(lambda: general.getChangedFlag(), None),
    }


# ---------------------------------------------------------------------------
# lean verified write surface (current bridge session write gate)
#
# Bounded commands, each changing one narrowly scoped state or dispatching one
# live note. Persistent changes read FL back to say what actually happened.
# Shared rules:
#
# * Track 0 is the master bus and is refused unless the caller passes
#   allow_master=True. Nothing here decides on its own that the master is what
#   the user meant.
# * Every reply is {requested, before, after, verified} plus enough context to
#   identify what was touched. `verified` is always an observation - a numeric
#   readback for the mixer controls, the parameter's display string for a
#   plug-in parameter. A setter's return code is never evidence: FL's setters
#   report success whether or not anything moved.
# * FL accepting a write and changing nothing is a real outcome, so it comes
#   back as verified=false. An exception means FL refused the call outright,
#   which is a different thing and stays an error.
# * One undo point is requested per call and the reply reports whether FL's
#   undo history demonstrably changed. The project is never saved.
# ---------------------------------------------------------------------------

# Most mixer setters store the float they are handed, so this only has to absorb
# float round-tripping - it is not slack for a curve.
MIXER_READBACK_TOLERANCE = 1e-4
# FL Studio quantizes the mixer stereo-separation knob to 1/64-unit
# increments even though the scripting API accepts a float.  Half a step
# therefore distinguishes the nearest representable setting from a missed
# write while still rejecting a neighbouring control position.
STEREO_SEPARATION_READBACK_TOLERANCE = (1.0 / 128.0) + 1e-6
# How close a plug-in parameter's readback has to be to count as sitting on the
# requested value when its display text never changed.
PARAM_NOOP_TOLERANCE = 1e-4
# One retry: on live FL a single mixer write lands, but a retry costs one API
# call and turns a dropped write into a landed one instead of a report.
WRITE_ATTEMPTS = 2


def _lean_track(a):
    """Resolve the target mixer track, refusing the master bus by default."""
    index = _mixer_track_index(a["track"])
    allow_master = a.get("allow_master", False)
    if type(allow_master) is not bool:
        raise ValueError("allow_master must be true or false")
    if index == 0 and not allow_master:
        raise ValueError(
            "refusing to write to mixer track 0 (master); pass "
            "allow_master=true to target the master bus deliberately"
        )
    return index


def _lean_value(a, key, low, high):
    value = a.get(key)
    if value is None:
        raise ValueError("%s is required" % key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a number" % key)
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("%s must be a finite number" % key)
    if value < low or value > high:
        raise ValueError(
            "%s must be within %g..%g (got %r)" % (key, low, high, value)
        )
    return value


def _lean_bool(a, key):
    value = a.get(key)
    if type(value) is not bool:
        raise ValueError("%s is required and must be true or false" % key)
    return value


def _lean_finite_number(a, key, low=None, high=None):
    value = a.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s is required and must be a number" % key)
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("%s must be a finite number" % key)
    if low is not None and number < low:
        raise ValueError("%s must be at least %g" % (key, low))
    if high is not None and number > high:
        raise ValueError("%s must be at most %g" % (key, high))
    return number


def _lean_parameter_selector(a):
    value = a.get("param")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("param is required and must be an integer index or text")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("parameter index must be 0 or greater")
    elif not value.strip():
        raise ValueError("parameter name to resolve must not be empty")
    return value


def _near(value, target, tol):
    if value is None:
        return False
    try:
        return abs(float(value) - float(target)) <= tol
    except (TypeError, ValueError):
        return False


def _check_session_precondition(a):
    """Refuse a write observed against a different loaded bridge session."""
    if "session_fingerprint" not in a or a.get("session_fingerprint") is None:
        return
    expected = a.get("session_fingerprint")
    if not isinstance(expected, str) or expected != SESSION_FINGERPRINT:
        raise ValueError(
            "session precondition failed: the bridge was reloaded or the "
            "fingerprint is invalid; re-read the project before writing"
        )


def _expected_before(a):
    """Return (present, value) without confusing an omitted guard with null."""
    return "expected_before" in a and a.get("expected_before") is not None, a.get(
        "expected_before"
    )


def _expect_number(a, actual, label, tol=MIXER_READBACK_TOLERANCE):
    present, expected = _expected_before(a)
    if not present:
        return
    if isinstance(expected, bool) or not isinstance(expected, (int, float)):
        raise ValueError("expected_before for %s must be a number" % label)
    if not math.isfinite(float(expected)):
        raise ValueError("expected_before for %s must be finite" % label)
    if not _near(actual, expected, tol):
        raise ValueError(
            "expected_before precondition failed for %s: expected %r, found %r; "
            "nothing was changed" % (label, expected, actual)
        )


def _expect_bool(a, actual, label):
    present, expected = _expected_before(a)
    if not present:
        return
    if type(expected) is not bool:
        raise ValueError("expected_before for %s must be true or false" % label)
    if actual is None or bool(actual) != expected:
        raise ValueError(
            "expected_before precondition failed for %s: expected %r, found %r; "
            "nothing was changed" % (label, expected, actual)
        )


def _expect_text(a, actual, label):
    present, expected = _expected_before(a)
    if not present:
        return
    if not isinstance(expected, str):
        raise ValueError("expected_before for %s must be text" % label)
    if actual != expected:
        raise ValueError(
            "expected_before precondition failed for %s: expected %r, found %r; "
            "nothing was changed" % (label, expected, actual)
        )


def _expect_color(a, actual, label):
    present, expected = _expected_before(a)
    if not present:
        return
    if _fl_color_word(expected) is None or expected < 0:
        raise ValueError(
            "expected_before for %s must be an integer within 0..4294967295"
            % label
        )
    if not _fl_colors_equivalent(actual, expected):
        raise ValueError(
            "expected_before precondition failed for %s: expected %r, found %r; "
            "nothing was changed" % (label, expected, actual)
        )


def _expect_eq(a, actual):
    present, expected = _expected_before(a)
    if not present:
        return
    if not isinstance(expected, dict):
        raise ValueError("expected_before for an EQ band must be an object")
    allowed = {"gain_normalized", "frequency_normalized"}
    unknown = set(expected) - allowed
    supplied = [key for key in allowed if expected.get(key) is not None]
    if unknown or not supplied:
        raise ValueError(
            "expected_before for an EQ band accepts gain_normalized and/or "
            "frequency_normalized only"
        )
    checks = (
        ("gain_normalized", "gain"),
        ("frequency_normalized", "freq"),
    )
    for public, raw in checks:
        if public not in expected or expected[public] is None:
            continue
        value = expected[public]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("expected_before.%s must be a number" % public)
        if not math.isfinite(float(value)):
            raise ValueError("expected_before.%s must be finite" % public)
        if not _near(actual.get(raw), value, MIXER_READBACK_TOLERANCE):
            raise ValueError(
                "expected_before precondition failed for EQ %s: expected %r, "
                "found %r; nothing was changed"
                % (public, value, actual.get(raw))
            )


def _expect_plugin(a, value, display):
    present, expected = _expected_before(a)
    if not present:
        return
    if not isinstance(expected, dict):
        raise ValueError("expected_before for a plug-in parameter must be an object")
    allowed = {"normalized_value", "display_text"}
    unknown = set(expected) - allowed
    supplied = [key for key in allowed if expected.get(key) is not None]
    if unknown or not supplied:
        raise ValueError(
            "expected_before for a plug-in parameter accepts normalized_value "
            "and/or display_text only"
        )
    if expected.get("normalized_value") is not None:
        wanted = expected["normalized_value"]
        if isinstance(wanted, bool) or not isinstance(wanted, (int, float)):
            raise ValueError("expected_before.normalized_value must be a number")
        if not math.isfinite(float(wanted)):
            raise ValueError("expected_before.normalized_value must be finite")
        if not _near(value, wanted, PARAM_NOOP_TOLERANCE):
            raise ValueError(
                "expected_before precondition failed for plug-in normalized_value: "
                "expected %r, found %r; nothing was changed" % (wanted, value)
            )
    if expected.get("display_text") is not None:
        wanted = expected["display_text"]
        if not isinstance(wanted, str):
            raise ValueError("expected_before.display_text must be text")
        if display != wanted:
            raise ValueError(
                "expected_before precondition failed for plug-in display_text: "
                "expected %r, found %r; nothing was changed" % (wanted, display)
            )


def _precondition_report(a):
    return {
        "session_fingerprint": SESSION_FINGERPRINT,
        "session_precondition_applied": a.get("session_fingerprint") is not None,
        "expected_before_applied": a.get("expected_before") is not None,
    }


def _write_and_read_back(write, read, matches, attempts=WRITE_ATTEMPTS):
    """Write, let FL settle one idle tick, then read back. A generator.

    Returns (value_now, verified) via ``yield from``. Nothing here trusts what
    the setter returned; only the readback decides.

    The yield is load-bearing, not politeness. Reading in the same idle tick
    as the write returns FL's *previous* value: observed live, a mixer volume
    set to 0.65 read back as the old 0.80 and reported unverified, while the
    next command's read showed 0.65 -- the write had landed all along. Plug-in
    display strings lag the same way, by a whole operation. Handing FL's
    thread back before reading is what makes the readback mean anything.
    """
    current = None
    for _ in range(max(1, attempts)):
        write()
        yield  # let FL run an idle tick; an immediate read is stale
        current = read()
        if matches(current):
            return current, True
    return current, False


def _eq_band_state(track, band):
    return {
        "gain": _safe(lambda: mixer.getEqGain(track, band), None),
        "gain_db": _safe(lambda: mixer.getEqGain(track, band, 1), None),
        "freq": _safe(lambda: mixer.getEqFrequency(track, band), None),
        "freq_hz": _safe(lambda: mixer.getEqFrequency(track, band, 1), None),
    }


def cmd_mixer_set_volume(a):
    """Set one mixer fader, normalised 0..1 (0.8 is FL's 0 dB default)."""
    i = _lean_track(a)
    value = _lean_value(a, "value", 0.0, 1.0)
    _check_session_precondition(a)
    before = _safe(lambda: mixer.getTrackVolume(i), None)
    before_db = _safe(lambda: mixer.getTrackVolume(i, 1), None)
    _expect_number(a, before, "mixer volume")
    undone = _save_undo("Universal Bridge: volume track %d" % i)
    after, verified = yield from _write_and_read_back(
        lambda: mixer.setTrackVolume(i, value),
        lambda: _safe(lambda: mixer.getTrackVolume(i), None),
        lambda got: _near(got, value, MIXER_READBACK_TOLERANCE),
    )
    return {
        "command": "mixer.set_volume",
        "undo_point_created": undone,
        "track": i,
        "requested": value,
        "before": before,
        "after": after,
        "before_db": before_db,
        "after_db": _safe(lambda: mixer.getTrackVolume(i, 1), None),
        "verified": verified,
        **_precondition_report(a),
    }


def cmd_mixer_set_volume_db(a):
    """Set a fader by its authoritative dB readback, not a guessed curve."""
    i = _lean_track(a)
    wanted = _finite_number(a.get("volume_db"), "volume_db", -60.0, 6.0)
    tolerance = _finite_number(a.get("tolerance_db", 0.1), "tolerance_db", 0.01, 1.0)
    _check_session_precondition(a)
    before = _safe(lambda: mixer.getTrackVolume(i), None)
    before_db = _safe(lambda: mixer.getTrackVolume(i, 1), None)

    present, expected = _expected_before(a)
    if present:
        if not isinstance(expected, dict):
            raise ValueError("expected_before for mixer volume dB must be an object")
        unknown = set(expected) - {"volume_normalized", "volume_db"}
        supplied = [key for key in expected if expected[key] is not None]
        if unknown or not supplied:
            raise ValueError(
                "expected_before for mixer volume dB accepts volume_normalized "
                "and/or volume_db"
            )
        if expected.get("volume_normalized") is not None:
            value = _finite_number(
                expected["volume_normalized"], "expected_before.volume_normalized",
                0.0, 1.0
            )
            if before is None or not _near(float(before), value, MIXER_READBACK_TOLERANCE):
                _precondition_failure("mixer volume", value, before)
        if expected.get("volume_db") is not None:
            value = _finite_number(
                expected["volume_db"], "expected_before.volume_db", -200.0, 12.0
            )
            if before_db is None or not _near(float(before_db), value, tolerance):
                _precondition_failure("mixer volume dB", value, before_db)

    iterations = 0
    undone = False
    after = before
    after_db = before_db
    if before_db is None or not _near(float(before_db), wanted, tolerance):
        undone = _save_undo(
            "Universal Bridge: volume track %d to %.2f dB" % (i, wanted)
        )
        low = 0.0
        high = 1.0
        for _step in range(16):
            candidate = (low + high) / 2.0
            mixer.setTrackVolume(i, candidate)
            iterations += 1
            yield
            observed_db = _safe(lambda: mixer.getTrackVolume(i, 1), None)
            observed = _safe(lambda: mixer.getTrackVolume(i), None)
            after = observed
            after_db = observed_db
            if observed_db is None:
                break
            if _near(float(observed_db), wanted, tolerance):
                break
            if float(observed_db) < wanted:
                low = candidate
            else:
                high = candidate
    else:
        # Preserve the later-idle-tick proof even for a no-op request.
        yield
        after = _safe(lambda: mixer.getTrackVolume(i), None)
        after_db = _safe(lambda: mixer.getTrackVolume(i, 1), None)

    verified = after_db is not None and _near(float(after_db), wanted, tolerance)
    return {
        "command": "mixer.set_volume_db",
        "undo_point_created": undone,
        "track": i,
        "requested_db": wanted,
        "tolerance_db": tolerance,
        "before": before,
        "after": after,
        "before_db": before_db,
        "after_db": after_db,
        "search_iterations": iterations,
        "verified": verified,
        **_precondition_report(a),
    }


def cmd_mixer_set_pan(a):
    """Set one mixer pan, -1.0 hard left to 1.0 hard right."""
    i = _lean_track(a)
    value = _lean_value(a, "value", -1.0, 1.0)
    _check_session_precondition(a)
    before = _safe(lambda: mixer.getTrackPan(i), None)
    _expect_number(a, before, "mixer pan")
    undone = _save_undo("Universal Bridge: pan track %d" % i)
    after, verified = yield from _write_and_read_back(
        lambda: mixer.setTrackPan(i, value),
        lambda: _safe(lambda: mixer.getTrackPan(i), None),
        lambda got: _near(got, value, MIXER_READBACK_TOLERANCE),
    )
    return {
        "command": "mixer.set_pan",
        "undo_point_created": undone,
        "track": i,
        "requested": value,
        "before": before,
        "after": after,
        "verified": verified,
        **_precondition_report(a),
    }


def cmd_mixer_set_mute(a):
    """Mute or unmute one mixer track. Never a toggle: state is stated."""
    i = _lean_track(a)
    want = _lean_bool(a, "muted")
    _check_session_precondition(a)
    before = _safe(lambda: mixer.isTrackMuted(i), None)
    _expect_bool(a, before, "mixer mute state")
    undone = _save_undo("Universal Bridge: mute track %d" % i)
    after, verified = yield from _write_and_read_back(
        # muteTrack toggles when its value argument is left at -1, so the
        # wanted state is always passed explicitly.
        lambda: mixer.muteTrack(i, 1 if want else 0),
        lambda: _safe(lambda: mixer.isTrackMuted(i), None),
        lambda got: got is not None and bool(got) == want,
    )
    return {
        "command": "mixer.set_mute",
        "undo_point_created": undone,
        "track": i,
        "requested": want,
        "before": None if before is None else bool(before),
        "after": None if after is None else bool(after),
        "verified": verified,
        **_precondition_report(a),
    }


def cmd_mixer_set_solo(a):
    """Solo or unsolo one track using FL's explicit state argument."""
    i = _lean_track(a)
    want = _lean_bool(a, "soloed")
    _check_session_precondition(a)
    before = _safe(lambda: mixer.isTrackSolo(i), None)
    _expect_bool(a, before, "mixer solo state")
    undone = _save_undo("Universal Bridge: solo track %d" % i)
    after, verified = yield from _write_and_read_back(
        lambda: mixer.soloTrack(i, 1 if want else 0),
        lambda: _safe(lambda: mixer.isTrackSolo(i), None),
        lambda got: got is not None and bool(got) == want,
    )
    return {
        "command": "mixer.set_solo",
        "undo_point_created": undone,
        "track": i,
        "requested": want,
        "before": None if before is None else bool(before),
        "after": None if after is None else bool(after),
        "verified": verified,
        **_precondition_report(a),
    }


def cmd_mixer_set_arm(a):
    """Arm or disarm one track without ever replaying FL's toggle-only call."""
    i = _lean_track(a)
    want = _lean_bool(a, "armed")
    _check_session_precondition(a)
    before = _safe(lambda: mixer.isTrackArmed(i), None)
    _expect_bool(a, before, "mixer recording-arm state")
    if before is None:
        raise ValueError("FL did not report the mixer recording-arm state")
    changing = bool(before) != want
    undone = (
        _save_undo("Universal Bridge: record arm track %d" % i)
        if changing
        else False
    )
    if changing:
        # armTrack has no explicit value argument. One call is the entire
        # mutation budget: a retry could undo a successful first call.
        mixer.armTrack(i)
    yield
    after = _safe(lambda: mixer.isTrackArmed(i), None)
    verified = after is not None and bool(after) == want
    return {
        "command": "mixer.set_arm",
        "undo_point_created": undone,
        "track": i,
        "requested": want,
        "before": bool(before),
        "after": None if after is None else bool(after),
        "verified": verified,
        "toggle_dispatched": changing,
        **_precondition_report(a),
    }


def cmd_mixer_set_color(a):
    """Set one track color and compare only FL's controllable RGB bits."""
    i = _lean_track(a)
    requested = a.get("color")
    color_arg = _fl_color_argument(requested)
    want = _fl_color_word(requested)
    _check_session_precondition(a)
    before = _fl_color_word(_safe(lambda: mixer.getTrackColor(i), None))
    _expect_color(a, before, "mixer color")
    undone = _save_undo("Universal Bridge: color track %d" % i)
    after, verified = yield from _write_and_read_back(
        lambda: mixer.setTrackColor(i, color_arg),
        lambda: _fl_color_word(_safe(lambda: mixer.getTrackColor(i), None)),
        lambda got: _fl_colors_equivalent(got, want),
    )
    return {
        "command": "mixer.set_color",
        "undo_point_created": undone,
        "track": i,
        "requested": want,
        "before": before,
        "after": after,
        "verified": verified,
        **_precondition_report(a),
    }


def cmd_mixer_set_stereo_separation(a):
    """Set one track's stereo-separation control in FL's -1..1 range."""
    i = _lean_track(a)
    want = _lean_value(a, "stereo_separation", -1.0, 1.0)
    _check_session_precondition(a)
    before = _safe(lambda: mixer.getTrackStereoSep(i), None)
    _expect_number(a, before, "mixer stereo separation")
    undone = _save_undo("Universal Bridge: stereo separation track %d" % i)
    after, verified = yield from _write_and_read_back(
        lambda: mixer.setTrackStereoSep(i, want),
        lambda: _safe(lambda: mixer.getTrackStereoSep(i), None),
        lambda got: _near(got, want, STEREO_SEPARATION_READBACK_TOLERANCE),
    )
    return {
        "command": "mixer.set_stereo_separation",
        "undo_point_created": undone,
        "track": i,
        "requested": want,
        "before": before,
        "after": after,
        "verified": verified,
        **_precondition_report(a),
    }


def cmd_mixer_select_track(a):
    """Make one mixer track active and verify the active-track getter."""
    i = _lean_track(a)
    _check_session_precondition(a)
    before = _safe(lambda: mixer.trackNumber(), None)
    _expect_number(a, before, "active mixer track", tol=0.0)
    after, verified = yield from _write_and_read_back(
        lambda: mixer.setActiveTrack(i),
        lambda: _safe(lambda: mixer.trackNumber(), None),
        lambda got: got == i and type(got) is int,
    )
    return {
        "command": "mixer.select_track",
        # Active selection is transient UI state, not an undoable project edit.
        "undo_point_created": None,
        "track": i,
        "requested": i,
        "before": before,
        "after": after,
        "verified": verified,
        **_precondition_report(a),
    }


def cmd_mixer_set_eq(a):
    """Set gain and/or frequency on one band of a track's built-in EQ.

    Both are normalised 0..1; the reply carries the dB and Hz FL shows for
    them so the caller can see what those normalised numbers mean.
    """
    i = _lean_track(a)
    band = _strict_integer(a.get("band"), "eq band")
    band_count = _safe(lambda: mixer.getEqBandCount(), 3) or 3
    if band < 0 or band >= band_count:
        raise ValueError("eq band must be 0..%d" % (band_count - 1))
    requested = {}
    if a.get("gain") is not None:
        requested["gain"] = _lean_value(a, "gain", 0.0, 1.0)
    if a.get("freq") is not None:
        requested["freq"] = _lean_value(a, "freq", 0.0, 1.0)
    if not requested:
        raise ValueError("mixer.set_eq needs gain, freq, or both")

    _check_session_precondition(a)
    before = _eq_band_state(i, band)
    _expect_eq(a, before)
    undone = _save_undo("Universal Bridge: EQ band %d track %d" % (band, i))
    fields = {}
    if "gain" in requested:
        want = requested["gain"]
        _unused, fields["gain"] = yield from _write_and_read_back(
            lambda: mixer.setEqGain(i, band, want),
            lambda: _safe(lambda: mixer.getEqGain(i, band), None),
            lambda got: _near(got, want, MIXER_READBACK_TOLERANCE),
        )
    if "freq" in requested:
        want = requested["freq"]
        _unused, fields["freq"] = yield from _write_and_read_back(
            lambda: mixer.setEqFrequency(i, band, want),
            lambda: _safe(lambda: mixer.getEqFrequency(i, band), None),
            lambda got: _near(got, want, MIXER_READBACK_TOLERANCE),
        )
    return {
        "command": "mixer.set_eq",
        "undo_point_created": undone,
        "track": i,
        "band": band,
        "requested": requested,
        "before": before,
        "after": _eq_band_state(i, band),
        "verified": all(fields.values()),
        "verified_fields": fields,
        **_precondition_report(a),
    }


def cmd_plugin_set_param(a):
    """Set one plug-in parameter by index, normalised 0..1.

    The write itself goes through `_set_param_verified`, which repeats the
    parameter write and checks FL's readback. Whether it landed is decided
    here from what was observed rather than from a setter return value:

    * the display string changing is proof the parameter moved, and it is the
      only proof of movement available, because getParamValue can keep
      reporting the old number for a tick after a write that did work;
    * if the display did not change, the parameter still counts as verified
      when it now *reads* as the requested value, which is the case where it
      was already there and there was nothing to observe. FL's stale readback
      lags behind a write, so it can only show the requested number when the
      parameter already held it;
    * anything else is verified=false, including FL accepting the write and
      leaving the parameter where it was.

    The before reading is reported but deliberately does not decide anything:
    it is the reading most likely to be a tick stale. A change too small to
    alter the displayed text and too small to show in the readback therefore
    reports verified=false, which is the right direction to be wrong in.
    """
    target = _plugin_target(a, writing=True)
    i = target["index"]
    slot = target["slot"]
    use_global = target["use_global_index"]
    if not _safe(lambda: plugins.isValid(i, slot, use_global), False):
        raise ValueError("no plugin at requested %s target" % target["target_kind"])
    index = _strict_integer(a.get("index"), "parameter index")
    # FL pads VST parameter lists out to a fixed size, so getParamCount is an
    # upper bound and never a count of the parameters that really exist. It is
    # used here only to reject an index FL could not address at all.
    reported_count = _safe(
        lambda: plugins.getParamCount(i, slot, use_global), 0
    ) or 0
    if index < 0 or (reported_count and index >= reported_count):
        raise ValueError(
            "parameter index %d is outside the 0..%d FL reports for this "
            "%s target" % (index, max(0, reported_count - 1), target["target_kind"])
        )
    value = _lean_value(a, "value", 0.0, 1.0)

    _check_session_precondition(a)
    if _expected_before(a)[0]:
        # Some plug-ins lag getParamValue/getParamValueString by one idle
        # callback. Settle before treating a read as a concurrency guard.
        yield
    before_value, before_display = _param_state(i, slot, index, use_global)
    _expect_plugin(a, before_value, before_display)
    undone = _save_undo(
        "Universal Bridge: %s param %d"
        % (_safe(
            lambda: plugins.getPluginName(i, slot, False, use_global), "plugin"
        ), index)
    )
    # The helper's own verdict is not taken: it decides from the numbers it
    # happened to read last. This handler judges from the before and after it
    # captured itself.
    after_value, after_display, _helper_verdict = _set_param_verified(
        i, slot, index, value, use_global_index=use_global
    )
    # Same staleness as the mixer controls: the display string read in the
    # write's own tick lags by a whole operation. Observed live -- restoring a
    # parameter to 0.6855 ("20 ms") reported "78 ms", the display belonging to
    # the value set just before it. Give FL a tick, then re-read, so
    # display_changed describes this write rather than the previous one.
    yield
    after_value, after_display = _param_state(i, slot, index, use_global)
    display_changed = after_display != before_display
    reads_at_value = _near(after_value, value, PARAM_NOOP_TOLERANCE)
    # There are three outcomes here, not two, and collapsing them into one
    # boolean overclaims. When the value readback agrees with the request the
    # destination is confirmed. When it does not, a changed display still
    # proves the control *moved* -- but not that it moved *here*, because
    # getParamValue keeps returning the previous number even across ticks and
    # so cannot be used to cross-check the destination. Callers that need the
    # destination guaranteed should use plugin.set_param_display, which
    # searches on the display and therefore knows where it landed.
    basis = ("value_readback" if reads_at_value
             else "display_change_only" if display_changed
             else "none")
    return {
        "command": "plugin.set_param",
        "undo_point_created": undone,
        **_plugin_target_report(target),
        "index": index,
        "plugin": _safe(
            lambda: plugins.getPluginName(i, slot, False, use_global), ""
        ),
        "name": _safe(
            lambda: plugins.getParamName(index, i, slot, use_global), ""
        ),
        "requested": value,
        "before": {"value": before_value, "display": before_display},
        "after": {"value": after_value, "display": after_display},
        "verified": reads_at_value or display_changed,
        "verification_basis": basis,
        "display_changed": display_changed,
        "reads_at_value": reads_at_value,
        **_precondition_report(a),
    }


def cmd_mixer_set_name(a):
    """Name one mixer track.

    An empty name is not an empty label: FL restores the track's default
    ("Insert 8" for track 8), so the readback compares against what FL
    reports rather than against the empty string it was handed.
    """
    i = _lean_track(a)
    if a.get("name") is None:
        raise ValueError("name is required (pass \"\" to restore the default)")
    if not isinstance(a.get("name"), str):
        raise ValueError("name must be text")
    want = a["name"]
    _check_session_precondition(a)
    before = _safe(lambda: mixer.getTrackName(i), None)
    _expect_text(a, before, "mixer track name")
    undone = _save_undo("Universal Bridge: name track %d" % i)
    after, verified = yield from _write_and_read_back(
        lambda: mixer.setTrackName(i, want),
        lambda: _safe(lambda: mixer.getTrackName(i), None),
        # An empty request asks for whatever default FL chooses, so any
        # non-empty answer is the write landing. A named request must match.
        lambda got: bool(got) if want == "" else got == want,
    )
    return {
        "command": "mixer.set_name",
        "undo_point_created": undone,
        "track": i,
        "requested": want,
        "before": before,
        "after": after,
        "restored_default": want == "",
        "verified": verified,
        **_precondition_report(a),
    }


def _send_destination(a, source):
    """Resolve the destination of a send, refusing a track that feeds itself."""
    dest = _mixer_track_index(a["to"])
    if dest == source:
        raise ValueError("a mixer track cannot send to itself (track %d)" % source)
    return dest


def cmd_mixer_set_send(a):
    """Route one mixer track to another, or stop routing it there.

    A state, never a toggle. Master is a legitimate destination -- every track
    routes there by default -- so only the *source* is refused by _lean_track.
    """
    i = _lean_track(a)
    dest = _send_destination(a, i)
    want = _lean_bool(a, "enabled")
    _check_session_precondition(a)
    before = _safe(lambda: mixer.getRouteSendActive(i, dest), None)
    _expect_bool(a, before, "mixer send state")
    undone = _save_undo("Universal Bridge: route track %d to %d" % (i, dest))

    def write():
        mixer.setRouteTo(i, dest, 1 if want else 0)
        # FL does not rebuild its routing graph until it is told the routing
        # changed; without this the send is set but not audible.
        _safe(lambda: mixer.afterRoutingChanged(), None)

    after, verified = yield from _write_and_read_back(
        write,
        lambda: _safe(lambda: mixer.getRouteSendActive(i, dest), None),
        lambda got: got is not None and bool(got) == want,
    )
    return {
        "command": "mixer.set_send",
        "undo_point_created": undone,
        "track": i,
        "to": dest,
        "requested": want,
        "before": None if before is None else bool(before),
        "after": None if after is None else bool(after),
        # Only readable while the route is active; FL raises otherwise, which
        # is why this is a _safe read that reports None for a route just torn
        # down rather than a fabricated zero.
        "level": _safe(lambda: mixer.getRouteToLevel(i, dest), None),
        "verified": verified,
        **_precondition_report(a),
    }


def cmd_mixer_set_send_level(a):
    """Set how much of one track is sent to another, normalised 0..1.

    0.8 is unity, the same convention as the fader, so a send at 0.484 is
    below unity rather than near half.

    The route has to exist first. FL's `getRouteToLevel` raises "Index out of
    range" for a route that is not active rather than reporting zero, so a
    level set on a route that was never created could not be read back and
    would be reported unverified forever. Refusing up front, and naming the
    command that creates the route, is the honest answer.
    """
    i = _lean_track(a)
    dest = _send_destination(a, i)
    value = _lean_value(a, "value", 0.0, 1.0)
    _check_session_precondition(a)
    if not _safe(lambda: mixer.getRouteSendActive(i, dest), False):
        raise ValueError(
            "track %d does not send to track %d, so its level cannot be set; "
            "create the route first with mixer.set_send" % (i, dest)
        )
    before = _safe(lambda: mixer.getRouteToLevel(i, dest), None)
    _expect_number(a, before, "mixer send level")
    undone = _save_undo("Universal Bridge: send level track %d to %d" % (i, dest))
    after, verified = yield from _write_and_read_back(
        lambda: mixer.setRouteToLevel(i, dest, value),
        lambda: _safe(lambda: mixer.getRouteToLevel(i, dest), None),
        lambda got: _near(got, value, MIXER_READBACK_TOLERANCE),
    )
    active = _safe(lambda: mixer.getRouteSendActive(i, dest), None)
    return {
        "command": "mixer.set_send_level",
        "undo_point_created": undone,
        "track": i,
        "to": dest,
        "requested": value,
        "before": before,
        "after": after,
        "send_active": None if active is None else bool(active),
        "verified": verified,
        **_precondition_report(a),
    }


# How far past the last real-looking parameter a name search keeps walking
# before concluding the rest is padding. The plug-ins measured so far cluster
# every real control inside the first hundred or so indices with scattered
# gaps, and 256 tolerates those gaps without walking thousands of empty slots.
# That sample is narrow: a plug-in that leaves a wider gap loses whatever sits
# past it. See docs/plugin-support.md before trusting this on an untested VST.
PARAM_SEARCH_RUN = 256
PARAM_AMBIGUITY_LIMIT = 8


def _one_parameter_match(query, matches, matched_on):
    """Return one match or fail with a bounded, index-addressable list."""
    unique = []
    seen = set()
    for index, label in matches:
        if index not in seen:
            seen.add(index)
            unique.append((index, label))
    if not unique:
        return None
    if len(unique) == 1:
        index, text = unique[0]
        return index, matched_on, text
    shown = unique[:PARAM_AMBIGUITY_LIMIT]
    candidates = ", ".join(
        "%d (%r)" % (index, str(label)[:80]) for index, label in shown
    )
    if len(unique) > len(shown):
        candidates += ", ... and %d more" % (len(unique) - len(shown))
    raise ValueError(
        "ambiguous parameter %r matched %d controls by %s: %s. Pass a "
        "parameter index to disambiguate."
        % (str(query)[:80], len(unique), matched_on, candidates)
    )


def _resolve_named_param(track, slot, query, use_global_index=False):
    """Find a parameter by name or by displayed value. A generator.

    A naive name-only search walks every index FL reports, which for a VST can
    be thousands of mostly-empty slots in one idle callback. Two things make
    that unusable here. Some controls have an empty name and carry their
    identity in the display string instead -- a control can be called nothing
    at all and identify itself only by displaying "Auto mode" -- and the walk
    has to hand FL's thread back.

    Matching runs in four passes over what was collected, most specific
    first: exact name, exact display, name substring, display substring. An
    exact hit on either wins over a substring hit on the other, so asking for
    "Key" cannot be captured by "Key Mapper" while "vibrato rate" and
    "auto mode" both still resolve.

    Returns (index, matched_on, matched_text). Raises when nothing matches,
    naming what was searched so a caller can scan and look for itself.
    """
    if isinstance(query, int) and not isinstance(query, bool):
        return query, "index", None
    wanted = str(query).strip().lower()
    if not wanted:
        raise ValueError("parameter name to resolve must not be empty")

    reported = int(
        _safe(
            lambda: plugins.getParamCount(track, slot, use_global_index), 0
        ) or 0
    )
    limit = min(reported, MAX_PARAM_INDEX_SCAN)
    found = []          # (index, lowered name, lowered display)
    since_real = 0
    examined = 0

    for p in range(limit):
        pname = _safe(
            lambda: plugins.getParamName(p, track, slot, use_global_index), ""
        ) or ""
        pdisp = _safe(
            lambda: plugins.getParamValueString(p, track, slot, use_global_index), ""
        ) or ""
        examined += 1
        if _is_padding(pname, pdisp):
            since_real += 1
            # A long unbroken run of padding means the real list ended. Without
            # this the search walks every padded index for every lookup.
            if since_real >= PARAM_SEARCH_RUN:
                break
        else:
            since_real = 0
            found.append((p, pname.strip().lower(), pdisp.strip().lower()))
        if examined % PARAMS_PER_TICK == 0:
            yield

    passes = (
        ("name", [(i, name) for i, name, _display in found if name == wanted]),
        (
            "display",
            [(i, display) for i, _name, display in found if display == wanted],
        ),
        (
            "name_substring",
            [(i, name) for i, name, _display in found if name and wanted in name],
        ),
        (
            "display_substring",
            [
                (i, display)
                for i, _name, display in found
                if display and wanted in display
            ],
        ),
    )
    for matched_on, matches in passes:
        match = _one_parameter_match(query, matches, matched_on)
        if match is not None:
            return match

    raise ValueError(
        "no parameter matching %r on track %d slot %d; searched %d indices "
        "and found %d real controls. Use plugin.scan_params to see them."
        % (query, track, slot, examined, len(found))
    )


def _solve_across_ticks(read, write, target, tol, iters=SOLVE_ITERATIONS):
    """Search a normalized control while yielding between probes.

    The write-write-read grouping inside one probe is deliberate and stays
    intact: FL ignores a lone parameter write, so each probe point is written
    twice and only then read. What changes is that the twenty-odd probes no
    longer all land in a single OnIdle callback.
    """
    lo, hi = 0.0, 1.0

    def probe(value):
        """Write a probe point and read it back on a *later* tick.

        Both halves are load-bearing. FL ignores a lone parameter write, so
        the value is written twice; and FL's readback in the write's own tick
        is a whole operation behind, so reading without yielding first returns
        the previous probe's number and sends the search the wrong way. A
        generator, so the yield reaches the caller's caller.
        """
        write(value)
        write(value)
        yield
        return read()

    v_lo = yield from probe(0.4)
    v_hi = yield from probe(0.6)
    if v_hi == v_lo:
        # Nothing moved between the middle probes; widen once before assuming
        # the usual ascending mapping.
        v_lo = yield from probe(0.15)
        v_hi = yield from probe(0.85)
    ascending = v_hi >= v_lo

    best, best_err = None, None
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        current = yield from probe(mid)
        err = abs(current - target)
        if best_err is None or err < best_err:
            best, best_err = mid, err
        if err <= tol:
            return mid, current, True
        if (current < target) == ascending:
            lo = mid
        else:
            hi = mid

    final = yield from probe(best)
    return best, final, best_err is not None and best_err <= tol


def _sweep_options(track, slot, index, steps, use_global_index=False):
    """Walk a control across 0..1 and record what it displays. A generator.

    An enumerated control -- a musical Key or Scale, an input-type selector --
    shows text rather than a number, so there is nothing to binary-search on.
    Its options
    do occupy contiguous stretches of the normalised range, though, so walking
    the range at a fixed resolution finds all of them and shows where each one
    lives.

    Returns [(value, display)] in ascending order. The control is left wherever
    the last probe put it; restoring it is the caller's job.
    """
    seen = []
    for step in range(steps):
        value = step / float(steps - 1) if steps > 1 else 0.0
        plugins.setParamValue(
            value, index, track, slot, PICKUP_NONE, use_global_index
        )
        plugins.setParamValue(
            value, index, track, slot, PICKUP_NONE, use_global_index
        )
        yield
        display = _safe(
            lambda: plugins.getParamValueString(
                index, track, slot, use_global_index
            ), "") or ""
        seen.append((value, display.strip()))
    return seen


def _option_runs(seen):
    """Collapse a sweep into one entry per option, keeping a central value.

    Boundaries between options are where rounding bites, so each option is
    represented by the middle of the contiguous run that produced it rather
    than by the first value that happened to show it.
    """
    runs = []
    for value, display in seen:
        if runs and runs[-1][0] == display:
            runs[-1][1].append(value)
        else:
            runs.append((display, [value]))
    return [(display, values[len(values) // 2]) for display, values in runs]


def cmd_plugin_set_param_option(a):
    """Set an enumerated plug-in parameter to a named option.

    `plugin.set_param_display` searches on the number a control shows, which
    is exactly what an enumeration does not have: a Key control can display
    "A", a Scale control "Chromatic", and an Input Type control "Low Male".
    Text-valued controls therefore need their own route.

    There is no way to ask FL what options a control has. This finds out the
    only way available -- by walking the control across its range and reading
    what it displays -- and so it necessarily moves the control while looking.
    If the wanted option is not there, the original value is put back before
    raising, and the error names every option that was found.

    The reply carries `options`, the whole enumeration in order, so one call
    is enough to learn what a control can be set to.

    Arguments:
        track, slot   where the plug-in is
        param         index, or a name/display string
        option        the exact option text to land on, matched
                      case-insensitively
        steps         sweep resolution, 2..256 (default 64)
    """
    target_info = _plugin_target(a, writing=True)
    track = target_info["index"]
    slot = target_info["slot"]
    use_global = target_info["use_global_index"]
    if not _safe(lambda: plugins.isValid(track, slot, use_global), False):
        raise ValueError(
            "no plugin at requested %s target" % target_info["target_kind"]
        )
    parameter = _lean_parameter_selector(a)
    wanted = a.get("option")
    if not isinstance(wanted, str) or not wanted.strip():
        raise ValueError("option is required (the display text to land on)")
    wanted = wanted.strip()
    steps = _strict_integer(a.get("steps", OPTION_SWEEP_STEPS), "steps")
    if steps < 2 or steps > MAX_OPTION_SWEEP_STEPS:
        raise ValueError("steps must be 2..%d" % MAX_OPTION_SWEEP_STEPS)

    _check_session_precondition(a)
    index, matched_on, matched_text = yield from _resolve_named_param(
        track, slot, parameter, use_global
    )
    reported_count = int(
        _safe(lambda: plugins.getParamCount(track, slot, use_global), 0) or 0
    )
    if index < 0 or index >= reported_count:
        raise ValueError(
            "parameter index %d is outside the range this plug-in reports" % index
        )
    if _expected_before(a)[0]:
        yield
    original = _safe(
        lambda: plugins.getParamValue(index, track, slot, use_global), None
    )
    before_display = _safe(
        lambda: plugins.getParamValueString(index, track, slot, use_global), ""
    ) or ""
    _expect_plugin(a, original, before_display)

    undone = _save_undo(
        "Universal Bridge: %s param %d"
        % (_safe(
            lambda: plugins.getPluginName(track, slot, False, use_global), "plugin"
        ), index)
    )
    seen = yield from _sweep_options(
        track, slot, index, steps, use_global
    )
    options = _option_runs(seen)

    low = wanted.lower()
    target = None
    for display, value in options:
        if display.lower() == low:
            target = (display, value)
            break

    if target is None:
        # The sweep moved the control to look. Putting it back is not enough:
        # an unverified restore is the same unfounded claim this whole surface
        # exists to avoid, and a caller that gets only an exception has no way
        # to learn the control was left somewhere it never asked for. So the
        # restore is read back on a later tick like any other write, and the
        # error says which of the two happened.
        offered = ", ".join(repr(d) for d, _ in options) or "nothing"
        if original is None:
            raise ValueError(
                "no option matching %r on parameter %d; it offers %s. The "
                "sweep moved this control and its original value could not be "
                "read, so it is NOT restored." % (wanted, index, offered)
            )
        _, restored = yield from _write_and_read_back(
            lambda: [
                plugins.setParamValue(
                    original, index, track, slot, PICKUP_NONE, use_global
                ),
                plugins.setParamValue(
                    original, index, track, slot, PICKUP_NONE, use_global
                ),
            ],
            lambda: _safe(
                lambda: plugins.getParamValue(index, track, slot, use_global), None
            ),
            lambda got: _near(got, original, PARAM_NOOP_TOLERANCE),
        )
        if not restored:
            now = _safe(
                lambda: plugins.getParamValueString(
                    index, track, slot, use_global
                ), "") or ""
            raise ValueError(
                "no option matching %r on parameter %d; it offers %s. The "
                "sweep moved this control and FL did not accept the restore, "
                "so it is LEFT AT %r instead of its original value %.6f."
                % (wanted, index, offered, now.strip(), original)
            )
        raise ValueError(
            "no option matching %r on parameter %d; it offers %s. The control "
            "was moved to look and has been restored, verified."
            % (wanted, index, offered)
        )

    chosen_display, chosen_value = target
    after, verified = yield from _write_and_read_back(
        lambda: [
            plugins.setParamValue(
                chosen_value, index, track, slot, PICKUP_NONE, use_global
            ),
            plugins.setParamValue(
                chosen_value, index, track, slot, PICKUP_NONE, use_global
            ),
        ],
        lambda: (_safe(
            lambda: plugins.getParamValueString(
                index, track, slot, use_global
            ), "") or "").strip(),
        lambda got: got.lower() == chosen_display.lower(),
    )
    return {
        "command": "plugin.set_param_option",
        "undo_point_created": undone,
        **_plugin_target_report(target_info),
        "index": index,
        "plugin": _safe(
            lambda: plugins.getPluginName(track, slot, False, use_global), ""
        ),
        "name": _safe(
            lambda: plugins.getParamName(index, track, slot, use_global), ""
        ),
        "matched_on": matched_on,
        "matched_text": matched_text,
        "requested": wanted,
        "selected": chosen_display,
        "normalised": chosen_value,
        "steps": steps,
        "options": [d for d, _ in options],
        "before": {"value": original, "display": before_display.strip()},
        "after": {
            "value": _safe(
                lambda: plugins.getParamValue(index, track, slot, use_global), None
            ),
            "display": after,
        },
        "verified": verified,
        **_precondition_report(a),
    }


def cmd_plugin_set_param_display(a):
    """Set a plug-in parameter to a value in the units the plug-in displays.

    `plugin.set_param` takes a normalised 0..1 with no published mapping to
    anything a musician would say. This takes the number the plug-in itself
    shows -- 20 for "20 ms", -18 for "-18.0 dB", 4000 for "4.0kHz" -- and
    searches the control until its own readback says that is where it is.
    The curve is never assumed; FL's readback is the authority.

    The parameter may be given as an index or as text, and the text is matched
    against names *and* display strings, because plenty of real third-party
    controls have no name at all.

    Arguments:
        track, slot   where the plug-in is
        param         index, or a name/display string
        target        the number to land on, in the plug-in's own units
        tolerance     optional; defaults to 2% of the target, floor 0.01

    Only the numeric part of a display is matched, so this cannot set a
    control whose display is pure text ("Chromatic", "Low Male"). Those are
    enumerations; set them with plugin.set_param_option.
    """
    target_info = _plugin_target(a, writing=True)
    track = target_info["index"]
    slot = target_info["slot"]
    use_global = target_info["use_global_index"]
    if not _safe(lambda: plugins.isValid(track, slot, use_global), False):
        raise ValueError(
            "no plugin at requested %s target" % target_info["target_kind"]
        )
    parameter = _lean_parameter_selector(a)
    target = _lean_finite_number(a, "target", -1e6, 1e6)
    tol = a.get("tolerance")
    tol = (
        max(0.01, abs(target) * 0.02)
        if tol is None
        else _lean_finite_number(a, "tolerance", 0.0, 1e6)
    )

    _check_session_precondition(a)
    index, matched_on, matched_text = yield from _resolve_named_param(
        track, slot, parameter, use_global
    )
    if _expected_before(a)[0]:
        yield
    if index < 0 or index >= int(
        _safe(lambda: plugins.getParamCount(track, slot, use_global), 0) or 0
    ):
        raise ValueError(
            "parameter index %d is outside the range this plug-in reports" % index
        )

    def read():
        text = _safe(
            lambda: plugins.getParamValueString(
                index, track, slot, use_global
            ), ""
        ) or ""
        number = _first_float(text)
        if number is None:
            raise ValueError(
                "parameter %d displays %r, which has no number to search on; "
                "use plugin.set_param_option for an enumerated control"
                % (index, text)
            )
        return number

    before_value = _safe(
        lambda: plugins.getParamValue(index, track, slot, use_global), None
    )
    before_display = _safe(
        lambda: plugins.getParamValueString(index, track, slot, use_global), ""
    ) or ""
    read()  # fail before touching anything if this control has no number
    _expect_plugin(a, before_value, before_display)

    undone = _save_undo(
        "Universal Bridge: %s param %d"
        % (_safe(
            lambda: plugins.getPluginName(track, slot, False, use_global), "plugin"
        ), index)
    )
    normalised, landed, within = yield from _solve_across_ticks(
        read,
        lambda v: plugins.setParamValue(
            max(0.0, min(1.0, v)), index, track, slot, PICKUP_NONE, use_global
        ),
        target,
        tol,
    )
    yield
    after_display = _safe(
        lambda: plugins.getParamValueString(index, track, slot, use_global), ""
    ) or ""
    return {
        "command": "plugin.set_param_display",
        "undo_point_created": undone,
        **_plugin_target_report(target_info),
        "index": index,
        "plugin": _safe(
            lambda: plugins.getPluginName(track, slot, False, use_global), ""
        ),
        "name": _safe(
            lambda: plugins.getParamName(index, track, slot, use_global), ""
        ),
        "matched_on": matched_on,
        "matched_text": matched_text,
        "requested": target,
        "tolerance": tol,
        "landed_on": landed,
        "normalised": normalised,
        "before": {"value": before_value, "display": before_display},
        "after": {
            "value": _safe(
                lambda: plugins.getParamValue(index, track, slot, use_global), None
            ),
            "display": after_display,
        },
        "verified": bool(within),
        **_precondition_report(a),
    }


# ---------------------------------------------------------------------------
# Track B performance and Channel Rack surface
# ---------------------------------------------------------------------------

MAX_CHANNEL_NAME_LENGTH = 64
MAX_STEP_COUNT = 512
MAX_VERIFIED_STEP_COUNT = 256
STEP_GRID_RESOLUTION = "sixteenth_note"
STEP_DIGEST_ALGORITHM = "sha256-canonical-json-v1"
# A guarded step write has to re-read the complete grid without yielding, then
# create its undo point and apply the whole batch without yielding. Keep that
# atomic section below the repository's 400-call ceiling with explicit
# headroom for the request pump that runs earlier in the same OnIdle callback.
SEQUENCER_WRITE_CALL_BUDGET = 320
SEQUENCER_WRITE_FIXED_CALLS = 8
TEMPO_READBACK_TOLERANCE = 1e-3


def _finite_number(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s must be a number" % label)
    number = float(value)
    if not math.isfinite(number) or number < low or number > high:
        raise ValueError("%s must be within %g..%g" % (label, low, high))
    return number


def _strict_bool_arg(a, key):
    value = a.get(key)
    if type(value) is not bool:
        raise ValueError("%s must be true or false" % key)
    return value


def _track_b_expected(a, allowed, label):
    present, expected = _expected_before(a)
    if not present:
        return None
    if not isinstance(expected, dict):
        raise ValueError("expected_before for %s must be an object" % label)
    unknown = set(expected) - set(allowed)
    if unknown:
        raise ValueError(
            "expected_before for %s has unsupported fields: %s"
            % (label, ", ".join(sorted(unknown)))
        )
    if not expected:
        raise ValueError("expected_before for %s must not be empty" % label)
    return expected


def _precondition_failure(label, expected, actual):
    raise ValueError(
        "expected_before precondition failed for %s: expected %r, found %r; "
        "nothing was changed" % (label, expected, actual)
    )


def _expect_track_b_value(expected, key, actual, label, tolerance=None):
    if expected is None or key not in expected:
        return
    wanted = expected[key]
    if key in ("playing", "recording", "enabled", "muted") and type(wanted) is not bool:
        raise ValueError("expected_before.%s must be true or false" % key)
    if key in (
        "color", "mixer_destination", "numerator", "position", "count",
        "project_dirty_flag"
    ) and (
        isinstance(wanted, bool) or not isinstance(wanted, int)
    ):
        raise ValueError("expected_before.%s must be an integer" % key)
    if key in ("name", "loop_mode", "channel_fingerprint") and not isinstance(
        wanted, str
    ):
        raise ValueError("expected_before.%s must be text" % key)
    if tolerance is not None:
        if isinstance(wanted, bool) or not isinstance(wanted, (int, float)):
            raise ValueError("expected_before.%s must be a number" % key)
        if actual is None or not _near(float(actual), float(wanted), tolerance):
            _precondition_failure(label, wanted, actual)
    elif wanted != actual:
        _precondition_failure(label, wanted, actual)


def _transport_loop_mode():
    raw = _safe(lambda: transport.getLoopMode(), None)
    return "pattern" if raw == 0 else "song" if raw == 1 else None


def cmd_transport_set_playing(a):
    wanted = _strict_bool_arg(a, "playing")
    _check_session_precondition(a)
    before = _safe(lambda: bool(transport.isPlaying()), None)
    expected = _track_b_expected(a, {"playing"}, "transport playing")
    _expect_track_b_value(expected, "playing", before, "transport playing")

    if before != wanted:
        if wanted:
            transport.start()
        else:
            transport.stop()
    yield
    after = _safe(lambda: bool(transport.isPlaying()), None)
    return {
        "command": "transport.set_playing",
        "requested": wanted,
        "before": before,
        "after": after,
        "verified": after is wanted,
        "undo_point_created": None,
        **_precondition_report(a),
    }


def cmd_transport_stop(a):
    allowed = {"playing", "position", "session_fingerprint", "expected_before"}
    unknown = set(a) - allowed
    if unknown:
        raise ValueError(
            "transport.stop received unsupported arguments: %s"
            % ", ".join(sorted(unknown))
        )
    position = _finite_number(a.get("position"), "position", 0.0, 0.0)
    if a.get("playing") is not False or position != 0.0:
        raise ValueError(
            "transport.stop is absolute and requires playing=false and position=0.0"
        )
    _check_session_precondition(a)
    before = {
        "playing": _safe(lambda: bool(transport.isPlaying()), None),
        "position": _safe(lambda: transport.getSongPos(), None),
    }
    expected = _track_b_expected(
        a, {"playing", "song_position_normalized"}, "transport stop state"
    )
    _expect_track_b_value(expected, "playing", before["playing"], "playing")
    _expect_track_b_value(
        expected, "song_position_normalized", before["position"],
        "song position", 1e-4
    )

    transport.stop()
    transport.setSongPos(0.0)
    yield
    after = {
        "playing": _safe(lambda: bool(transport.isPlaying()), None),
        "position": _safe(lambda: transport.getSongPos(), None),
    }
    fields = {
        "playing": after["playing"] is False,
        "position": after["position"] is not None
        and _near(float(after["position"]), 0.0, 1e-4),
    }
    return {
        "command": "transport.stop",
        "requested": {"playing": False, "position": 0.0},
        "before": before,
        "after": after,
        "verified_fields": fields,
        "verified": all(fields.values()),
        "undo_point_created": None,
        **_precondition_report(a),
    }


def cmd_transport_set_song_position(a):
    position = _finite_number(a.get("position"), "position", 0.0, 1.0)
    tolerance = _finite_number(a.get("tolerance", 1e-4), "tolerance", 0.0, 0.05)
    _check_session_precondition(a)
    if _safe(lambda: bool(transport.isPlaying()), False):
        raise ValueError(
            "transport.set_song_position refuses while playback is running; "
            "stop first so the readback has one stable meaning"
        )
    before = _safe(lambda: transport.getSongPos(), None)
    expected = _track_b_expected(
        a, {"song_position_normalized"}, "song position"
    )
    _expect_track_b_value(
        expected, "song_position_normalized", before, "song position", tolerance
    )
    transport.setSongPos(position)
    yield
    after = _safe(lambda: transport.getSongPos(), None)
    return {
        "command": "transport.set_song_position",
        "requested": position,
        "tolerance": tolerance,
        "before": before,
        "after": after,
        "verified": after is not None and _near(float(after), position, tolerance),
        "undo_point_created": None,
        **_precondition_report(a),
    }


def cmd_transport_set_loop_mode(a):
    wanted = a.get("loop_mode")
    if wanted not in ("pattern", "song"):
        raise ValueError("loop_mode must be 'pattern' or 'song'")
    _check_session_precondition(a)
    before = _transport_loop_mode()
    expected = _track_b_expected(a, {"loop_mode"}, "transport loop mode")
    _expect_track_b_value(expected, "loop_mode", before, "transport loop mode")
    if before != wanted:
        transport.setLoopMode()
    yield
    after = _transport_loop_mode()
    return {
        "command": "transport.set_loop_mode",
        "requested": wanted,
        "before": before,
        "after": after,
        "verified": after == wanted,
        "undo_point_created": None,
        **_precondition_report(a),
    }


def cmd_transport_set_tempo(a):
    wanted = _finite_number(a.get("tempo_bpm"), "tempo_bpm", 10.0, 522.0)
    _check_session_precondition(a)
    if _safe(lambda: bool(transport.isRecording()), False):
        raise ValueError("transport.set_tempo refuses while recording is active")
    if _safe(lambda: bool(transport.isPlaying()), False):
        raise ValueError(
            "transport.set_tempo refuses while playback is running; stop first "
            "so the later-tick BPM readback has one stable meaning"
        )
    before = _bpm()
    expected = _track_b_expected(a, {"tempo_bpm"}, "tempo")
    _expect_track_b_value(
        expected, "tempo_bpm", before, "tempo", TEMPO_READBACK_TOLERANCE
    )
    undone = _save_undo("Universal Bridge: tempo %.4g BPM" % wanted)
    # Image-Line's bundled MCP tool passes ordinary BPM with the explicit mode
    # flag. That flag avoids the legacy raw BPM*1000 form; current FL releases
    # also accept a float here, which preserves this contract's fractional BPM.
    mixer.setCurrentTempo(wanted, True)
    yield
    after = _bpm()
    return {
        "command": "transport.set_tempo",
        "requested": wanted,
        "before": before,
        "after": after,
        "verified": after is not None and _near(
            float(after), wanted, TEMPO_READBACK_TOLERANCE
        ),
        "undo_point_created": undone,
        **_precondition_report(a),
    }


def cmd_transport_set_recording(a):
    wanted = _strict_bool_arg(a, "recording")
    _check_session_precondition(a)
    before = _safe(lambda: bool(transport.isRecording()), None)
    expected = _track_b_expected(a, {"recording"}, "transport recording")
    _expect_track_b_value(expected, "recording", before, "transport recording")
    if before != wanted:
        # FL exposes record as a toggle. Dispatch it at most once and never
        # retry it after an ambiguous transport outcome.
        transport.record()
    yield
    after = _safe(lambda: bool(transport.isRecording()), None)
    return {
        "command": "transport.set_recording",
        "requested": wanted,
        "before": before,
        "after": after,
        "verified": after is wanted,
        "undo_point_created": None,
        **_precondition_report(a),
    }


def _set_transport_toggle(a, command, argument, getter, transport_command):
    wanted = _strict_bool_arg(a, "enabled")
    _check_session_precondition(a)
    before = getter()
    expected = _track_b_expected(a, {"enabled"}, argument)
    _expect_track_b_value(expected, "enabled", before, argument)
    if before != wanted:
        # These are button commands, not setters. One edge is safe only after
        # reading the absolute state and must never be replayed.
        # Image-Line's third argument is pmeflags, not the global-transport
        # routing flags.  Pass both explicitly: PME_System authorizes ordinary
        # transport buttons and GT_Global sends the command to FL itself.
        transport.globalTransport(
            transport_command, 1, midi.PME_System, midi.GT_Global
        )
    yield
    after = getter()
    return {
        "command": command,
        "requested": wanted,
        "before": before,
        "after": after,
        "verified": after is wanted,
        "undo_point_created": None,
        **_precondition_report(a),
    }


def cmd_transport_set_metronome(a):
    return (yield from _set_transport_toggle(
        a,
        "transport.set_metronome",
        "transport metronome",
        _metronome_state,
        midi.FPT_Metronome,
    ))


def cmd_transport_set_precount(a):
    return (yield from _set_transport_toggle(
        a,
        "transport.set_precount",
        "transport recording precount",
        _precount_state,
        midi.FPT_CountDown,
    ))


def cmd_project_set_time_signature_numerator(a):
    wanted = _strict_integer(a.get("numerator"), "time-signature numerator")
    if wanted < 1 or wanted > 32:
        raise ValueError("time-signature numerator must be within 1..32")
    _check_session_precondition(a)
    if _safe(lambda: bool(transport.isPlaying()), False):
        raise ValueError("time-signature changes require stopped playback")
    if _safe(lambda: bool(transport.isRecording()), False):
        raise ValueError("time-signature changes require recording to be off")
    before = _time_signature_snapshot()
    expected = _track_b_expected(a, {"numerator"}, "time signature")
    _expect_track_b_value(
        expected, "numerator", before["numerator"], "time-signature numerator"
    )
    undone = False
    if before["numerator"] != wanted:
        undone = _save_undo(
            "Universal Bridge: time signature numerator %d" % wanted
        )
        general.setNumerator(wanted)
    yield
    after = _time_signature_snapshot()
    return {
        "command": "project.set_time_signature_numerator",
        "requested": wanted,
        "before": before,
        "after": after,
        "verified": after["numerator"] == wanted,
        "undo_point_created": undone,
        **_precondition_report(a),
    }


def _move_project_history(a, direction):
    _check_session_precondition(a)
    before = _history_snapshot()
    expected = _track_b_expected(
        a, {"position", "count", "project_dirty_flag"}, "project history"
    )
    for key in ("position", "count", "project_dirty_flag"):
        _expect_track_b_value(expected, key, before[key], "history " + key)
    if direction == "undo":
        if not before["can_undo"]:
            raise ValueError("project undo refused because no earlier history level exists")
        target = before["position"] - 1
    else:
        if not before["can_redo"]:
            raise ValueError("project redo refused because no later history level exists")
        target = before["position"] + 1
    # This absolute API avoids FL's alternate Ctrl+Z behavior and makes the
    # requested position independently verifiable.
    general.setUndoHistoryPos(target)
    yield
    after = _history_snapshot()
    return {
        "command": "project." + direction,
        "direction": direction,
        "requested_position": target,
        "before": before,
        "after": after,
        "verified": after["position"] == target,
        "undo_point_created": None,
        **_precondition_report(a),
    }


def cmd_project_undo(a):
    return (yield from _move_project_history(a, "undo"))


def cmd_project_redo(a):
    return (yield from _move_project_history(a, "redo"))


def _channel_mix_snapshot(index):
    summary = _channel_summary(index)
    return {
        "volume": summary["volume"],
        "pan": summary["pan"],
        "muted": summary["muted"],
        "channel_fingerprint": summary["channel_fingerprint"],
    }


def _channel_identity_snapshot(index):
    summary = _channel_summary(index)
    return {
        "name": summary["name"],
        "color": summary["color"],
        "channel_fingerprint": summary["channel_fingerprint"],
    }


def _channel_route_snapshot(index):
    summary = _channel_summary(index)
    return {
        "mixer_destination": summary["mixer_track"],
        "channel_fingerprint": summary["channel_fingerprint"],
    }


def _channel_solo_snapshot(index):
    summary = _channel_summary(index)
    return {
        "soloed": summary["solo"],
        "channel_fingerprint": summary["channel_fingerprint"],
    }


def _channel_pitch_snapshot(index):
    summary = _channel_summary(index)
    return {
        "pitch": summary["pitch"],
        "pitch_semitones": summary["pitch_semitones"],
        "pitch_range": summary["pitch_range"],
        "channel_fingerprint": summary["channel_fingerprint"],
    }


def _selected_channels():
    count = int(channels.channelCount(True))
    return [
        index for index in range(count)
        if bool(_safe(lambda index=index: channels.isChannelSelected(index, True), False))
    ]


def _expect_channel(a, before, fields, label):
    expected = _track_b_expected(
        a, {field[0] for field in fields} | {"channel_fingerprint"}, label
    )
    if expected is None:
        return
    _expect_track_b_value(
        expected, "channel_fingerprint", before.get("channel_fingerprint"),
        "channel fingerprint"
    )
    for public, actual, tolerance in fields:
        if public == "color" and public in expected:
            wanted = expected[public]
            if (
                isinstance(wanted, bool)
                or not isinstance(wanted, int)
                or wanted < 0
                or wanted > 0xFFFFFFFF
            ):
                raise ValueError(
                    "expected_before.color must be an integer within "
                    "0..4294967295"
                )
            observed = before.get(actual)
            if not _fl_colors_equivalent(wanted, observed):
                _precondition_failure(
                    "%s %s" % (label, public), wanted, observed
                )
            continue
        _expect_track_b_value(
            expected, public, before.get(actual), "%s %s" % (label, public),
            tolerance
        )


def _channel_write_target(a):
    _require_global_scope(a)
    return _channel_index(a.get("channel"))


def cmd_channel_set_mix(a):
    index = _channel_write_target(a)
    requested = {}
    if "volume" in a:
        requested["volume"] = _finite_number(a["volume"], "volume", 0.0, 1.0)
    if "pan" in a:
        requested["pan"] = _finite_number(a["pan"], "pan", -1.0, 1.0)
    if "muted" in a:
        requested["muted"] = _strict_bool_arg(a, "muted")
    if not requested:
        raise ValueError("channel.set_mix requires volume, pan, muted, or a combination")
    _check_session_precondition(a)
    before = _channel_mix_snapshot(index)
    _expect_channel(
        a, before,
        (("volume_normalized", "volume", 1e-4),
         ("pan", "pan", 1e-4), ("muted", "muted", None)),
        "channel mix"
    )
    undone = _save_undo("Universal Bridge: channel %d mix" % index)
    if "volume" in requested:
        channels.setChannelVolume(index, requested["volume"], PICKUP_NONE, True)
    if "pan" in requested:
        channels.setChannelPan(index, requested["pan"], PICKUP_NONE, True)
    if "muted" in requested:
        channels.muteChannel(index, int(requested["muted"]), True)
    yield
    after = _channel_mix_snapshot(index)
    fields = {}
    if "volume" in requested:
        fields["volume"] = after["volume"] is not None and _near(
            float(after["volume"]), requested["volume"], 1e-4
        )
    if "pan" in requested:
        fields["pan"] = after["pan"] is not None and _near(
            float(after["pan"]), requested["pan"], 1e-4
        )
    if "muted" in requested:
        fields["muted"] = after["muted"] is requested["muted"]
    return {
        "command": "channel.set_mix",
        "channel": index,
        "index_scope": "global",
        "requested": requested,
        "before": before,
        "after": after,
        "verified_fields": fields,
        "verified": all(fields.values()),
        "undo_point_created": undone,
        **_precondition_report(a),
    }


def cmd_channel_set_solo(a):
    index = _channel_write_target(a)
    wanted = _strict_bool_arg(a, "soloed")
    _check_session_precondition(a)
    before = _channel_solo_snapshot(index)
    _expect_channel(
        a, before,
        (("soloed", "soloed", None),),
        "channel solo"
    )
    undone = _save_undo("Universal Bridge: channel %d solo" % index)
    channels.soloChannel(index, int(wanted), True)
    yield
    after = _channel_solo_snapshot(index)
    return {
        "command": "channel.set_solo",
        "channel": index,
        "index_scope": "global",
        "requested": wanted,
        "before": before,
        "after": after,
        "verified": after["soloed"] is wanted,
        "undo_point_created": undone,
        **_precondition_report(a),
    }


def cmd_channel_set_pitch(a):
    index = _channel_write_target(a)
    wanted = _finite_number(a.get("pitch"), "pitch", -1.0, 1.0)
    _check_session_precondition(a)
    before = _channel_pitch_snapshot(index)
    _expect_channel(
        a, before,
        (("pitch_normalized", "pitch", 1e-4),),
        "channel pitch"
    )
    undone = _save_undo("Universal Bridge: channel %d pitch" % index)
    channels.setChannelPitch(index, wanted, 0, PICKUP_NONE, True)
    yield
    after = _channel_pitch_snapshot(index)
    return {
        "command": "channel.set_pitch",
        "channel": index,
        "index_scope": "global",
        "requested": wanted,
        "before": before,
        "after": after,
        "verified": after["pitch"] is not None and _near(
            float(after["pitch"]), wanted, 1e-4
        ),
        "undo_point_created": undone,
        **_precondition_report(a),
    }


def cmd_channel_select(a):
    index = _channel_write_target(a)
    if a.get("exclusive") is not True:
        raise ValueError("channel.select requires exclusive=true")
    _check_session_precondition(a)
    before = _selected_channels()
    expected = _track_b_expected(
        a, {"selected_channel_indices"}, "channel selection"
    )
    if expected is not None:
        wanted_before = expected.get("selected_channel_indices")
        if (
            not isinstance(wanted_before, list)
            or any(type(value) is not int or value < 0 for value in wanted_before)
            or wanted_before != sorted(set(wanted_before))
        ):
            raise ValueError(
                "expected_before.selected_channel_indices must be sorted, unique "
                "non-negative integers"
            )
        if wanted_before != before:
            _precondition_failure(
                "channel selection", wanted_before, before
            )
    channels.selectOneChannel(index, True)
    yield
    after = _selected_channels()
    return {
        "command": "channel.select",
        "channel": index,
        "index_scope": "global",
        "exclusive": True,
        "requested": index,
        "before": before,
        "after": after,
        "verified": after == [index],
        "undo_point_created": None,
        **_precondition_report(a),
    }


def cmd_channel_set_identity(a):
    index = _channel_write_target(a)
    requested = {}
    if "name" in a:
        name = a["name"]
        if not isinstance(name, str):
            raise ValueError("name must be text")
        if len(name) > MAX_CHANNEL_NAME_LENGTH:
            raise ValueError("name must be at most %d characters" % MAX_CHANNEL_NAME_LENGTH)
        requested["name"] = name
    if "color" in a:
        color = a["color"]
        # Validate the public unsigned spelling now. Conversion back to FL's
        # signed Python integer happens only at the API call boundary below.
        _fl_color_argument(color)
        requested["color"] = color
    if not requested:
        raise ValueError("channel.set_identity requires name, color, or both")
    _check_session_precondition(a)
    before = _channel_identity_snapshot(index)
    _expect_channel(
        a, before, (("name", "name", None), ("color", "color", None)),
        "channel identity"
    )
    undone = _save_undo("Universal Bridge: channel %d identity" % index)
    if "name" in requested:
        channels.setChannelName(index, requested["name"], True)
    if "color" in requested:
        channels.setChannelColor(
            index, _fl_color_argument(requested["color"]), True
        )
    yield
    after = _channel_identity_snapshot(index)
    fields = {}
    if "name" in requested:
        fields["name"] = after["name"] == requested["name"]
    if "color" in requested:
        fields["color"] = _fl_colors_equivalent(
            after["color"], requested["color"]
        )
    return {
        "command": "channel.set_identity",
        "channel": index,
        "index_scope": "global",
        "requested": requested,
        "before": before,
        "after": after,
        "verified_fields": fields,
        "verified": all(fields.values()),
        "undo_point_created": undone,
        **_precondition_report(a),
    }


def cmd_channel_route_to_mixer(a):
    index = _channel_write_target(a)
    destination = a.get("destination")
    if isinstance(destination, bool) or not isinstance(destination, int):
        raise ValueError("destination must be an integer")
    last_insert = min(
        int(mixer.getTrackInfo(midi.TN_LastIns)),
        _mixer_track_count() - 1,
    )
    if last_insert < 0:
        raise ValueError("FL did not report a valid last mixer insert")
    if destination < -1 or destination > last_insert:
        raise ValueError(
            "destination must be -1 or a live mixer track within 0..%d"
            % last_insert
        )
    _check_session_precondition(a)
    before = _channel_route_snapshot(index)
    _expect_channel(
        a, before, (("mixer_destination", "mixer_destination", None),),
        "channel route"
    )
    undone = _save_undo("Universal Bridge: channel %d route" % index)
    channels.setTargetFxTrack(index, destination, True)
    yield
    after = _channel_route_snapshot(index)
    return {
        "command": "channel.route_to_mixer",
        "channel": index,
        "index_scope": "global",
        "requested": destination,
        "before": before,
        "after": after,
        "verified": after["mixer_destination"] == destination,
        "undo_point_created": undone,
        **_precondition_report(a),
    }


def _pattern_identity_snapshot(index):
    summary = _pattern_summary(index)
    return {"name": summary["name"], "color": summary["color"]}


def cmd_pattern_select(a):
    index = _pattern_index(a.get("pattern"))
    _check_session_precondition(a)
    before = _safe(lambda: patterns.patternNumber(), None)
    expected = _track_b_expected(
        a, {"current_pattern_number"}, "current pattern"
    )
    if expected is not None:
        _expect_track_b_value(
            expected, "current_pattern_number", before, "current pattern"
        )
    patterns.jumpToPattern(index)
    yield
    after = _safe(lambda: patterns.patternNumber(), None)
    return {
        "command": "pattern.select",
        "requested": index,
        "before": before,
        "after": after,
        "verified": after == index,
        "undo_point_created": None,
        **_precondition_report(a),
    }


def cmd_pattern_set_identity(a):
    index = _pattern_index(a.get("pattern"))
    requested = {}
    if "name" in a:
        name = a["name"]
        if not isinstance(name, str):
            raise ValueError("pattern name must be text")
        if len(name) > MAX_PATTERN_NAME_LENGTH:
            raise ValueError("pattern name must be at most 64 characters")
        requested["name"] = name
    if "color" in a:
        _fl_color_argument(a["color"])
        requested["color"] = a["color"]
    if not requested:
        raise ValueError("pattern.set_identity requires name, color, or both")
    _check_session_precondition(a)
    before = _pattern_identity_snapshot(index)
    expected = _track_b_expected(a, {"name", "color"}, "pattern identity")
    if expected is not None:
        if "name" in expected and expected["name"] != before["name"]:
            _precondition_failure("pattern name", expected["name"], before["name"])
        if "color" in expected:
            wanted_color = expected["color"]
            _fl_color_argument(wanted_color)
            if not _fl_colors_equivalent(wanted_color, before["color"]):
                _precondition_failure(
                    "pattern color", wanted_color, before["color"]
                )
    undone = _save_undo("Universal Bridge: pattern %d identity" % index)
    if "name" in requested:
        patterns.setPatternName(index, requested["name"])
    if "color" in requested:
        patterns.setPatternColor(index, _fl_color_argument(requested["color"]))
    yield
    after = _pattern_identity_snapshot(index)
    fields = {}
    if "name" in requested:
        fields["name"] = after["name"] == requested["name"]
    if "color" in requested:
        fields["color"] = _fl_colors_equivalent(
            after["color"], requested["color"]
        )
    return {
        "command": "pattern.set_identity",
        "pattern": index,
        "requested": requested,
        "before": before,
        "after": after,
        "verified_fields": fields,
        "verified": all(fields.values()),
        "undo_point_created": undone,
        **_precondition_report(a),
    }


def cmd_pattern_set_length(a):
    index = _pattern_index(a.get("pattern"))
    length = _strict_integer(a.get("length"), "pattern length")
    if length < 1 or length > MAX_PATTERN_LENGTH_BEATS:
        raise ValueError("pattern length must be within 1..4096 beats")
    _check_session_precondition(a)
    before = _safe(lambda: patterns.getPatternLength(index), None)
    expected = _track_b_expected(a, {"length_beats"}, "pattern length")
    if expected is not None:
        _expect_track_b_value(
            expected, "length_beats", before, "pattern length"
        )
    undone = _save_undo("Universal Bridge: pattern %d length" % index)
    patterns.setPatternLength(index, length)
    yield
    after = _safe(lambda: patterns.getPatternLength(index), None)
    return {
        "command": "pattern.set_length",
        "pattern": index,
        "requested": length,
        "before": before,
        "after": after,
        "verified": after == length,
        "undo_point_created": undone,
        **_precondition_report(a),
    }


def _playlist_identity_snapshot(index):
    summary = _playlist_track_summary(index)
    return {"name": summary["name"], "color": summary["color"]}


def _playlist_state_snapshot(index):
    summary = _playlist_track_summary(index)
    return {
        "muted": None if summary["muted"] is None else bool(summary["muted"]),
        "soloed": None if summary["solo"] is None else bool(summary["solo"]),
        "selected": None
        if summary["selected"] is None else bool(summary["selected"]),
    }


def cmd_playlist_set_identity(a):
    index = _playlist_track_index(a.get("track"))
    requested = {}
    if "name" in a:
        name = a["name"]
        if not isinstance(name, str):
            raise ValueError("Playlist track name must be text")
        if len(name) > MAX_PLAYLIST_TRACK_NAME_LENGTH:
            raise ValueError("Playlist track name must be at most 64 characters")
        requested["name"] = name
    if "color" in a:
        _fl_color_argument(a["color"])
        requested["color"] = a["color"]
    if not requested:
        raise ValueError("playlist.set_identity requires name, color, or both")
    _check_session_precondition(a)
    before = _playlist_identity_snapshot(index)
    expected = _track_b_expected(a, {"name", "color"}, "Playlist identity")
    if expected is not None:
        if "name" in expected and expected["name"] != before["name"]:
            _precondition_failure(
                "Playlist track name", expected["name"], before["name"]
            )
        if "color" in expected:
            _fl_color_argument(expected["color"])
            if not _fl_colors_equivalent(expected["color"], before["color"]):
                _precondition_failure(
                    "Playlist track color", expected["color"], before["color"]
                )
    undone = _save_undo("Universal Bridge: Playlist track %d identity" % index)
    if "name" in requested:
        playlist.setTrackName(index, requested["name"])
    if "color" in requested:
        playlist.setTrackColor(index, _fl_color_argument(requested["color"]))
    yield
    after = _playlist_identity_snapshot(index)
    fields = {}
    if "name" in requested:
        fields["name"] = after["name"] == requested["name"]
    if "color" in requested:
        fields["color"] = _fl_colors_equivalent(
            after["color"], requested["color"]
        )
    return {
        "command": "playlist.set_identity",
        "track": index,
        "requested": requested,
        "before": before,
        "after": after,
        "verified_fields": fields,
        "verified": all(fields.values()),
        "undo_point_created": undone,
        **_precondition_report(a),
    }


def cmd_playlist_set_state(a):
    index = _playlist_track_index(a.get("track"))
    requested = {}
    for public in ("muted", "soloed", "selected"):
        if public in a:
            requested[public] = _strict_bool_arg(a, public)
    if not requested:
        raise ValueError(
            "playlist.set_state requires muted, soloed, selected, or a combination"
        )
    _check_session_precondition(a)
    before = _playlist_state_snapshot(index)
    expected = _track_b_expected(
        a, {"muted", "soloed", "selected"}, "Playlist track state"
    )
    if expected is not None:
        for field in ("muted", "soloed", "selected"):
            if field in expected and expected[field] != before[field]:
                _precondition_failure(
                    "Playlist track %s" % field, expected[field], before[field]
                )
    undone = (
        _save_undo("Universal Bridge: Playlist track %d state" % index)
        if "muted" in requested or "soloed" in requested
        else None
    )
    if "muted" in requested:
        playlist.muteTrack(index, int(requested["muted"]), 0)
    if "soloed" in requested:
        playlist.soloTrack(index, int(requested["soloed"]), 0)
    if "selected" in requested and before["selected"] is not requested["selected"]:
        # Playlist selection exposes only a toggle. It is issued at most once
        # and never enters the generic retry helper.
        playlist.selectTrack(index)
    yield
    after = _playlist_state_snapshot(index)
    fields = {
        field: after[field] is wanted for field, wanted in requested.items()
    }
    return {
        "command": "playlist.set_state",
        "track": index,
        "requested": requested,
        "before": before,
        "after": after,
        "verified_fields": fields,
        "verified": all(fields.values()),
        "undo_point_created": undone,
        **_precondition_report(a),
    }


def _step_count(pattern):
    count = _safe(lambda: patterns.getPatternLength(pattern), None)
    if isinstance(count, bool) or not isinstance(count, int):
        raise ValueError("FL did not report an integer step count for pattern %d" % pattern)
    if count < 1 or count > MAX_STEP_COUNT:
        raise ValueError(
            "pattern %d step count must be within 1..%d (got %r)"
            % (pattern, MAX_STEP_COUNT, count)
        )
    return count


def _step_digest(pattern, channel, cells):
    return _sha256_json({
        "cells": [1 if value else 0 for value in cells],
        "channel_index": channel,
        "grid_resolution": STEP_GRID_RESOLUTION,
        "pattern_number": pattern,
        "step_count": len(cells),
    })


def _step_snapshot(pattern, channel):
    current = _safe(lambda: patterns.patternNumber(), None)
    if current != pattern:
        raise ValueError(
            "step-grid APIs are current-pattern-only: requested pattern %d, "
            "current pattern %r; nothing was changed" % (pattern, current)
        )
    count = _step_count(pattern)
    cells = []
    for position in range(count):
        cells.append(bool(channels.getGridBit(channel, position, True)))
        if (position + 1) % PARAMS_PER_TICK == 0:
            yield
    after_current = _safe(lambda: patterns.patternNumber(), None)
    if after_current != pattern:
        raise ValueError(
            "current pattern changed during the step-grid observation; "
            "discard the result and retry the read"
        )
    return {
        "pattern": pattern,
        "current_pattern": after_current,
        "channel": channel,
        "index_scope": "global",
        "step_count": count,
        "grid_resolution": STEP_GRID_RESOLUTION,
        "cells": cells,
        "digest_algorithm": STEP_DIGEST_ALGORITHM,
        "digest": _step_digest(pattern, channel, cells),
    }


def _step_snapshot_immediate(pattern, channel):
    """Capture the whole current grid without yielding before a mutation.

    The ordinary snapshot is deliberately chunked so a read does not monopolize
    FL's UI thread. A write precondition has a different requirement: after the
    last yield, re-read every cell in this one callback so a change to an early
    chunk cannot race the digest and then be overwritten by the batch.
    """
    current = _safe(lambda: patterns.patternNumber(), None)
    if current != pattern:
        raise ValueError("current pattern changed immediately before mutation")
    count = _step_count(pattern)
    cells = [
        bool(channels.getGridBit(channel, position, True))
        for position in range(count)
    ]
    if _safe(lambda: patterns.patternNumber(), None) != pattern:
        raise ValueError("current pattern changed immediately before mutation")
    return {
        "pattern": pattern,
        "current_pattern": pattern,
        "channel": channel,
        "index_scope": "global",
        "step_count": count,
        "grid_resolution": STEP_GRID_RESOLUTION,
        "cells": cells,
        "digest_algorithm": STEP_DIGEST_ALGORITHM,
        "digest": _step_digest(pattern, channel, cells),
    }


def _sequence_target(a):
    _require_global_scope(a)
    pattern = a.get("pattern")
    if isinstance(pattern, bool) or not isinstance(pattern, int) or pattern < 1:
        raise ValueError("pattern must be an integer of 1 or greater")
    channel = _channel_index(a.get("channel"))
    return pattern, channel


def cmd_sequencer_get(a):
    if set(a) - {"pattern", "channel", "index_scope"}:
        raise ValueError("sequencer.get received unsupported arguments")
    pattern, channel = _sequence_target(a)
    result = yield from _step_snapshot(pattern, channel)
    result["command"] = "sequencer.get"
    result["unsaved_changes"] = _safe(lambda: general.getChangedFlag(), None)
    result["warnings"] = [
        "FL's grid-bit API addresses the current pattern only; this read "
        "refused to switch patterns implicitly."
    ]
    return result


def cmd_sequencer_set(a):
    if "expected_before" in a:
        raise ValueError(
            "sequencer.set does not accept generic expected_before; its required "
            "expected_digest is the one authoritative concurrency guard"
        )
    pattern, channel = _sequence_target(a)
    _check_session_precondition(a)
    expected_digest = a.get("expected_digest")
    if (
        not isinstance(expected_digest, str)
        or len(expected_digest) != 64
        or any(ch not in "0123456789abcdef" for ch in expected_digest)
    ):
        raise ValueError("expected_digest must be 64 lowercase hex characters")
    updates = a.get("updates")
    if (
        not isinstance(updates, list)
        or not updates
        or len(updates) > MAX_VERIFIED_STEP_COUNT
    ):
        raise ValueError(
            "updates must contain 1..%d cells" % MAX_VERIFIED_STEP_COUNT
        )
    parsed = []
    seen = set()
    for item in updates:
        if not isinstance(item, dict) or set(item) != {"step_index", "enabled"}:
            raise ValueError("each update must contain only step_index and enabled")
        step = item["step_index"]
        enabled = item["enabled"]
        if isinstance(step, bool) or not isinstance(step, int) or step < 0:
            raise ValueError("step_index must be a non-negative integer")
        if type(enabled) is not bool:
            raise ValueError("enabled must be true or false")
        if step in seen:
            raise ValueError("updates must not contain duplicate step indices")
        seen.add(step)
        parsed.append({"step_index": step, "enabled": enabled})

    before = yield from _step_snapshot(pattern, channel)
    if any(item["step_index"] >= before["step_count"] for item in parsed):
        raise ValueError("step_index is outside the pattern's absolute step grid")
    if before["digest"] != expected_digest:
        raise ValueError(
            "expected_digest conflict: the current step grid changed; nothing "
            "was written and the batch was not retried"
        )
    step_count = before["step_count"]
    if step_count > MAX_VERIFIED_STEP_COUNT:
        raise ValueError(
            "verified step writes support at most %d cells so the atomic "
            "digest check stays within FL's idle-tick budget (got %d); "
            "nothing was changed"
            % (MAX_VERIFIED_STEP_COUNT, step_count)
        )
    atomic_calls = step_count + len(parsed) + SEQUENCER_WRITE_FIXED_CALLS
    if atomic_calls > SEQUENCER_WRITE_CALL_BUDGET:
        max_updates = max(
            0,
            SEQUENCER_WRITE_CALL_BUDGET
            - SEQUENCER_WRITE_FIXED_CALLS
            - step_count,
        )
        raise ValueError(
            "step batch is too large for one atomic FL idle tick: pattern "
            "length %d permits at most %d updates (got %d); split the batch "
            "and refresh the digest between calls"
            % (step_count, max_updates, len(parsed))
        )
    # The chunked observation above yielded between cell ranges. Re-read the
    # full grid without yielding immediately before mutation so an edit to an
    # already-read cell cannot slip between the digest guard and this batch.
    # This unconditional boundary also prevents the final read chunk from
    # sharing a callback with the atomic recheck/write section.
    yield
    immediate_before = _step_snapshot_immediate(pattern, channel)
    if immediate_before["digest"] != expected_digest:
        raise ValueError(
            "expected_digest conflict immediately before mutation: the current "
            "step grid changed; nothing was written and the batch was not retried"
        )
    before = immediate_before
    undone = _save_undo(
        "Universal Bridge: pattern %d channel %d steps" % (pattern, channel)
    )
    for item in parsed:
        channels.setGridBit(
            channel, item["step_index"], int(item["enabled"]), True
        )
    yield
    after = yield from _step_snapshot(pattern, channel)
    verified_cells = []
    for item in parsed:
        actual = after["cells"][item["step_index"]]
        verified_cells.append({
            "step_index": item["step_index"],
            "requested_enabled": item["enabled"],
            "after_enabled": actual,
            "verified": actual is item["enabled"],
        })
    fields_ok = all(item["verified"] for item in verified_cells)
    report = _precondition_report(a)
    report["expected_before_applied"] = True
    return {
        "command": "sequencer.set",
        "pattern": pattern,
        "channel": channel,
        "index_scope": "global",
        "expected_digest": expected_digest,
        "requested_updates": parsed,
        "before": before,
        "after": after,
        "verified_cells": verified_cells,
        "verified": fields_ok,
        "undo_point_created": undone,
        **report,
    }


def _release_active_note(active):
    """Send one registered note-off, retaining failures for deinit cleanup."""
    try:
        channels.midiNoteOn(
            active["channel"], active["note"], 0, active["midi_channel"]
        )
    except Exception:
        active["release_pending"] = True
        return False
    try:
        _active_notes.remove(active)
    except ValueError:
        pass
    active["note_off_sent"] = True
    active["release_pending"] = False
    return True


def _cleanup_active_notes(force_all=False):
    """Retry due note-offs, or release every note while the script unloads."""
    for active in list(_active_notes):
        if (
            force_all
            or active.get("release_pending")
            or active.get("due_tick", _idle_tick + 1) <= _idle_tick
        ):
            _release_active_note(active)


def cmd_channel_trigger_note(a):
    index = _channel_write_target(a)
    note = a.get("note")
    velocity = a.get("velocity")
    duration = a.get("duration_ms", 250)
    midi_channel = a.get("midi_channel", -1)
    for value, label, low, high in (
        (note, "note", 0, 127),
        (velocity, "velocity", 1, 127),
        (duration, "duration_ms", 20, 5000),
        (midi_channel, "midi_channel", -1, 15),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError("%s must be an integer within %d..%d" % (label, low, high))
    _check_session_precondition(a)
    before = _channel_summary(index)
    expected = _track_b_expected(a, {"channel_fingerprint"}, "channel note target")
    _expect_track_b_value(
        expected, "channel_fingerprint", before["channel_fingerprint"],
        "channel fingerprint"
    )

    # MIDI note-off has no voice identifier. A second overlapping audition of
    # the same target/note/channel would be cut short when the earlier job sent
    # its off, so refuse that ambiguous overlap before dispatching another on.
    for pending in _active_notes:
        if (
            not pending.get("note_off_sent")
            and pending.get("channel") == index
            and pending.get("note") == note
            and pending.get("midi_channel") == midi_channel
        ):
            raise ValueError(
                "the same channel/note/MIDI-channel audition is already active; "
                "wait for its note-off before retrying"
            )

    active = {
        "channel": index,
        "note": note,
        "midi_channel": midi_channel,
        "due_tick": None,
        "note_off_sent": False,
        "release_pending": False,
    }
    sent_on = False
    note_off_sent = False
    try:
        channels.midiNoteOn(index, note, velocity, midi_channel)
        sent_on = True
        active["due_tick"] = _idle_tick + max(
            1, int(math.ceil(float(duration) / NOTE_TICK_MS))
        )
        _active_notes.append(active)
        # Every active note is checked once per global OnIdle callback. This
        # job can therefore be resumed round-robin with scans and other notes
        # without multiplying its audible duration by the number of jobs.
        while not active["note_off_sent"] and _idle_tick < active["due_tick"]:
            yield
    finally:
        if sent_on and not active["note_off_sent"] and not active["release_pending"]:
            active["release_pending"] = True
            note_off_sent = _release_active_note(active)
        else:
            note_off_sent = active["note_off_sent"]
    return {
        "command": "channel.trigger_note",
        "channel": index,
        "index_scope": "global",
        "note": note,
        "velocity": velocity,
        "duration_ms": duration,
        "midi_channel": midi_channel,
        "dispatched": sent_on,
        "note_off_sent": note_off_sent,
        "undo_point_created": None,
        **_precondition_report(a),
    }


# ---------------------------------------------------------------------------
# Creative targeting, section markers, and public REC-event automation
# ---------------------------------------------------------------------------


def _arrangement_marker_names():
    """Read the contiguous public marker-name index without inventing times."""
    names = []
    for index in range(MAX_ARRANGEMENT_MARKERS_SCANNED):
        name = _safe(lambda index=index: arrangement.getMarkerName(index), "")
        if not isinstance(name, str) or not name:
            break
        names.append(name)
    return names


def cmd_creative_prepare_piano_roll(a):
    """Select one global channel/pattern and focus the Piano Roll.

    This changes transient UI targeting only. The host-side creative subsystem
    writes and triggers the separate Piano Roll script after this receipt.
    """
    _require_global_scope(a)
    channel = _channel_index(a.get("channel"))
    pattern = _strict_integer(a.get("pattern"), "pattern")
    maximum = _strict_integer(patterns.patternMax(), "maximum pattern number")
    if pattern < 1 or pattern > maximum:
        raise ValueError("pattern must be within the live range 1..%d" % maximum)
    _check_session_precondition(a)
    before_channels = _selected_channels()
    before_pattern = _strict_integer(patterns.patternNumber(), "current pattern")
    window_id = getattr(midi, "widPianoRoll", None)
    if window_id is None or not hasattr(ui, "showWindow"):
        raise ValueError("FL does not expose ui.showWindow(midi.widPianoRoll)")
    before_visible = _safe(lambda: bool(ui.getVisible(window_id)), None)
    patterns.jumpToPattern(pattern)
    channels.selectOneChannel(channel, True)
    ui.showWindow(window_id)
    yield
    after_channels = _selected_channels()
    after_pattern = _strict_integer(patterns.patternNumber(), "current pattern")
    after_visible = _safe(lambda: bool(ui.getVisible(window_id)), None)
    return {
        "command": "creative.prepare_piano_roll",
        "channel_index": channel,
        "pattern_number": pattern,
        "before_channel_indices": before_channels,
        "after_channel_indices": after_channels,
        "before_pattern_number": before_pattern,
        "after_pattern_number": after_pattern,
        "piano_roll_visible_before": before_visible,
        "piano_roll_visible_after": after_visible,
        "selected_target_verified": (
            after_channels == [channel] and after_pattern == pattern
        ),
        "piano_roll_visibility_verified": (
            None if after_visible is None else bool(after_visible)
        ),
        "session_fingerprint": SESSION_FINGERPRINT,
        "session_precondition_applied": a.get("session_fingerprint") is not None,
        "project_saved": False,
    }


def cmd_arrangement_add_markers(a):
    """Add bounded absolute timeline markers and verify their names appeared.

    Image-Line exposes marker names by index but no marker-time getter. The
    response therefore proves name-count growth after a later idle tick and
    reports requested time plus its FL hint, while explicitly leaving time
    verification unavailable.
    """
    values = a.get("markers")
    if not isinstance(values, list) or not (
            1 <= len(values) <= MAX_ARRANGEMENT_MARKERS_PER_WRITE):
        raise ValueError(
            "markers must contain 1..%d entries"
            % MAX_ARRANGEMENT_MARKERS_PER_WRITE
        )
    parsed = []
    for index, value in enumerate(values):
        if not isinstance(value, dict) or set(value) != {"time_ticks", "name"}:
            raise ValueError(
                "marker %d must contain exactly time_ticks and name" % index
            )
        ticks = _strict_integer(value.get("time_ticks"), "marker time_ticks")
        name = value.get("name")
        if ticks < 0 or ticks > 0x7FFFFFFF:
            raise ValueError("marker time_ticks must be within 0..2147483647")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("marker name must be non-empty text")
        if len(name) > MAX_ARRANGEMENT_MARKER_NAME_LENGTH:
            raise ValueError(
                "marker name must be at most %d characters"
                % MAX_ARRANGEMENT_MARKER_NAME_LENGTH
            )
        parsed.append((ticks, name))
    _check_session_precondition(a)
    if _safe(lambda: bool(transport.isPlaying()), False):
        raise ValueError("arrangement marker writes require stopped playback")
    if _safe(lambda: bool(transport.isRecording()), False):
        raise ValueError("arrangement marker writes require recording to be off")
    before = _arrangement_marker_names()
    before_counts = {name: before.count(name) for _ticks, name in parsed}
    undone = _save_undo("Universal Bridge: add arrangement markers")
    requested = []
    for ticks, name in parsed:
        arrangement.addAutoTimeMarker(ticks, name)
        requested.append({
            "time_ticks": ticks,
            "name": name,
            "time_hint": _safe(
                lambda ticks=ticks: arrangement.currentTimeHint(1, ticks), ""
            ) or "",
        })
    yield
    after = _arrangement_marker_names()
    names_verified = all(
        after.count(name) >= before_counts[name] + sum(
            1 for _ticks, requested_name in parsed if requested_name == name
        )
        for name in before_counts
    )
    return {
        "command": "arrangement.add_markers",
        "requested": requested,
        "before_marker_names": before,
        "after_marker_names": after,
        "names_verified": names_verified,
        "times_verified": False,
        "verification_status": (
            "name_count_verified_time_unobservable"
            if names_verified else "marker_name_readback_unverified"
        ),
        # The public API has no marker-time readback. Never roll a partial
        # observation into the kernel's fully verified meaning.
        "verified": False,
        "undo_point_created": undone,
        "session_fingerprint": SESSION_FINGERPRINT,
        "session_precondition_applied": a.get("session_fingerprint") is not None,
        "project_saved": False,
    }


def _automation_target(a):
    kind = a.get("target_kind")
    prop = a.get("property")
    index = a.get("target_index")
    if kind not in ("mixer", "channel"):
        raise ValueError("target_kind must be mixer or channel")
    if kind == "mixer":
        track_args = {
            "track": index,
            "allow_master": a.get("allow_master", False),
        }
        index = _lean_track(track_args)
        definitions = {
            "volume": (
                getattr(midi, "REC_Mixer_Vol"),
                lambda: mixer.getTrackVolume(index),
                lambda value: value,
            ),
            "pan": (
                getattr(midi, "REC_Mixer_Pan"),
                lambda: mixer.getTrackPan(index),
                lambda value: (float(value) + 1.0) / 2.0,
            ),
            "stereo_separation": (
                getattr(midi, "REC_Mixer_SS"),
                lambda: mixer.getTrackStereoSep(index),
                lambda value: (float(value) + 1.0) / 2.0,
            ),
        }
        if prop not in definitions:
            raise ValueError(
                "mixer automation property must be volume, pan, or stereo_separation"
            )
        base, getter, normalize = definitions[prop]
        event_id = int(base) + int(mixer.getTrackPluginId(index, 0))
    else:
        _require_global_scope(a)
        index = _channel_index(index)
        definitions = {
            "volume": (
                getattr(midi, "REC_Chan_Vol"),
                lambda: channels.getChannelVolume(index, 0, True),
                lambda value: value,
            ),
            "pan": (
                getattr(midi, "REC_Chan_Pan"),
                lambda: channels.getChannelPan(index, True),
                lambda value: (float(value) + 1.0) / 2.0,
            ),
        }
        if prop not in definitions:
            raise ValueError("channel automation property must be volume or pan")
        base, getter, normalize = definitions[prop]
        event_id = int(base) + int(channels.getRecEventId(index, True))
    return kind, index, prop, event_id, getter, normalize


def cmd_automation_record_value(a):
    """Dispatch one documented REC event while FL is actively recording.

    The controlled value is read back later. FL exposes no automation-point
    getter, so event capture itself remains explicitly unobservable.
    """
    normalized = _lean_value(a, "value_normalized", 0.0, 1.0)
    _check_session_precondition(a)
    kind, index, prop, event_id, getter, normalize = _automation_target(a)
    if not _safe(lambda: bool(transport.isPlaying()), False):
        raise ValueError("automation capture requires active playback")
    if not _safe(lambda: bool(transport.isRecording()), False):
        raise ValueError("automation capture requires FL recording to be enabled")
    before_native = _safe(getter, None)
    before = None if before_native is None else normalize(before_native)
    expected = a.get("expected_before")
    if expected is not None:
        if isinstance(expected, bool) or not isinstance(expected, (int, float)):
            raise ValueError("expected_before must be a normalized number")
        if not math.isfinite(float(expected)) or not 0.0 <= float(expected) <= 1.0:
            raise ValueError("expected_before must be within 0..1")
        if not _near(before, float(expected), 1e-4):
            _precondition_failure("automation target", expected, before)
    before_position = _safe(
        lambda: transport.getSongPos(midi.SONGLENGTH_ABSTICKS), None
    )
    undone = _save_undo(
        "Universal Bridge: record %s %d %s automation" % (kind, index, prop)
    )
    maximum = int(getattr(midi, "FromMIDI_Max", 65536))
    controller_value = int(round(normalized * maximum))
    flags = int(getattr(midi, "REC_MIDIController"))
    event_result = general.processRECEvent(event_id, controller_value, flags)
    yield
    after_native = _safe(getter, None)
    after = None if after_native is None else normalize(after_native)
    after_position = _safe(
        lambda: transport.getSongPos(midi.SONGLENGTH_ABSTICKS), None
    )
    value_verified = _near(after, normalized, 2e-3)
    capture_conditions_held = bool(
        _safe(lambda: bool(transport.isPlaying()), False)
        and _safe(lambda: bool(transport.isRecording()), False)
    )
    return {
        "command": "automation.record_value",
        "target_kind": kind,
        "target_index": index,
        "property": prop,
        "event_id": event_id,
        "controller_value": controller_value,
        "process_rec_event_result": event_result,
        "requested_normalized": normalized,
        "before_normalized": before,
        "after_normalized": after,
        "control_value_verified": value_verified,
        "capture_conditions_held": capture_conditions_held,
        "song_position_before_ticks": before_position,
        "song_position_after_ticks": after_position,
        "automation_event_recorded": None,
        "automation_event_verification": "unavailable_no_public_point_getter",
        "verified": False,
        "undo_point_created": undone,
        "session_fingerprint": SESSION_FINGERPRINT,
        "session_precondition_applied": a.get("session_fingerprint") is not None,
        "expected_before_applied": expected is not None,
        "project_saved": False,
    }


HANDLERS = {
    "ping": cmd_ping,
    "session.set_write_mode": cmd_session_set_write_mode,
    "project.info": cmd_project_info,
    "project.history": cmd_project_history,
    "arrangement.selection": cmd_arrangement_selection,
    "mixer.list": cmd_mixer_list,
    "mixer.peaks": cmd_mixer_peaks,
    "mixer.track": cmd_mixer_track,
    "transport.set_playing": cmd_transport_set_playing,
    "transport.stop": cmd_transport_stop,
    "transport.set_song_position": cmd_transport_set_song_position,
    "transport.set_loop_mode": cmd_transport_set_loop_mode,
    "transport.set_tempo": cmd_transport_set_tempo,
    "transport.set_recording": cmd_transport_set_recording,
    "transport.set_metronome": cmd_transport_set_metronome,
    "transport.set_precount": cmd_transport_set_precount,
    "project.set_time_signature_numerator": cmd_project_set_time_signature_numerator,
    "project.undo": cmd_project_undo,
    "project.redo": cmd_project_redo,
    # The lean verified write surface; reachable only while the bridge session
    # reports write_test mode, see LEAN_WRITE_COMMANDS in _dispatch.
    "mixer.set_volume": cmd_mixer_set_volume,
    "mixer.set_volume_db": cmd_mixer_set_volume_db,
    "mixer.set_pan": cmd_mixer_set_pan,
    "mixer.set_mute": cmd_mixer_set_mute,
    "mixer.set_solo": cmd_mixer_set_solo,
    "mixer.set_arm": cmd_mixer_set_arm,
    "mixer.set_color": cmd_mixer_set_color,
    "mixer.set_stereo_separation": cmd_mixer_set_stereo_separation,
    "mixer.select_track": cmd_mixer_select_track,
    "mixer.set_eq": cmd_mixer_set_eq,
    "mixer.set_name": cmd_mixer_set_name,
    "mixer.set_send": cmd_mixer_set_send,
    "mixer.set_send_level": cmd_mixer_set_send_level,
    "plugin.set_param": cmd_plugin_set_param,
    "plugin.set_param_display": cmd_plugin_set_param_display,
    "plugin.set_param_option": cmd_plugin_set_param_option,
    "plugin.params": cmd_plugin_params,
    "plugin.preset_count": cmd_plugin_preset_count,
    "plugin.scan_params": cmd_plugin_scan_params,
    "channels.list": cmd_channels_list,
    "patterns.list": cmd_patterns_list,
    "patterns.find_empty": cmd_patterns_find_empty,
    "playlist.list": cmd_playlist_list,
    "channel.set_mix": cmd_channel_set_mix,
    "channel.set_solo": cmd_channel_set_solo,
    "channel.set_pitch": cmd_channel_set_pitch,
    "channel.select": cmd_channel_select,
    "pattern.select": cmd_pattern_select,
    "pattern.set_identity": cmd_pattern_set_identity,
    "pattern.set_length": cmd_pattern_set_length,
    "playlist.set_identity": cmd_playlist_set_identity,
    "playlist.set_state": cmd_playlist_set_state,
    "channel.set_identity": cmd_channel_set_identity,
    "channel.route_to_mixer": cmd_channel_route_to_mixer,
    "sequencer.get": cmd_sequencer_get,
    "sequencer.set": cmd_sequencer_set,
    "channel.trigger_note": cmd_channel_trigger_note,
    "creative.prepare_piano_roll": cmd_creative_prepare_piano_roll,
    "arrangement.add_markers": cmd_arrangement_add_markers,
    "automation.record_value": cmd_automation_record_value,
}


class _Job:
    """A command that spreads its work over several idle ticks."""

    def __init__(self, handle, rid, gen, cmd, client_session=None,
                 request_token=None):
        self.handle = handle
        self.rid = rid
        self.gen = gen
        self.cmd = cmd
        self.client_session = client_session
        self.request_token = request_token
        self.chunks = 0


def _correlated(response, client_session=None, request_token=None):
    """Echo transport correlation without changing legacy command results."""
    if client_session is not None:
        response["client_session"] = client_session
    if request_token is not None:
        response["request_token"] = request_token
    return response


def _dispatch(req):
    """Run a command.

    Returns a response dict, or a `_Job` when the handler is a generator that
    wants to be resumed across ticks.
    """
    if not isinstance(req, dict):
        return {
            "id": None,
            "ok": False,
            "error": "bridge request must be a JSON object",
        }
    rid = req.get("id")
    client_session = req.get("client_session")
    request_token = req.get("request_token")

    if (client_session is None) != (request_token is None):
        return {
            "id": rid,
            "ok": False,
            "error": "client_session and request_token must be supplied together",
        }
    if client_session is not None and (
            not isinstance(client_session, str)
            or len(client_session) != 32
            or any(c not in "0123456789abcdef" for c in client_session)):
        return {"id": rid, "ok": False,
                "error": "client_session must be 32 lowercase hex characters"}
    if request_token is not None and (
            not isinstance(request_token, str)
            or len(request_token) < 34
            or len(request_token) > 64
            or not request_token.startswith(client_session + "-")):
        return _correlated(
            {"id": rid, "ok": False,
             "error": "request_token is invalid for client_session"},
            client_session,
        )

    def response(value):
        return _correlated(value, client_session, request_token)

    cmd = req.get("cmd", "")
    args = req.get("args", {})
    if not isinstance(args, dict):
        return response({
            "id": rid,
            "ok": False,
            "error": "bridge request args must be a JSON object",
        })
    allowed = READ_ONLY_COMMANDS | SESSION_CONTROL_COMMANDS
    lock_reason = "bridge is locked read-only"
    if LEAN_WRITES_ENABLED:
        allowed = allowed | LEAN_WRITE_COMMANDS
        lock_reason = (
            "bridge exposes only read commands, session control, and verified "
            "writes"
        )
    available = sorted(allowed)
    if cmd not in allowed:
        return response({
            "id": rid,
            "ok": False,
            "error": "%s; command %r is prohibited" % (lock_reason, cmd),
            "available": available,
        })
    handler = HANDLERS.get(cmd)
    if handler is None:
        return response(
            {"id": rid, "ok": False, "error": "unknown command %r" % cmd,
             "available": available}
        )
    try:
        result = handler(args)
        if isinstance(result, types.GeneratorType):
            return _Job(
                None, rid, result, cmd, client_session, request_token
            )
        return response({"id": rid, "ok": True, "result": result})
    except Exception as e:
        return response(
            {"id": rid, "ok": False,
             "error": "%s: %s" % (type(e).__name__, e),
             "trace": traceback.format_exc(limit=6)}
        )


def _advance_jobs():
    """Run one long-command chunk per idle tick, round-robin."""
    if not _jobs:
        return
    job = _jobs.pop(0)
    if not _transport.alive(job.handle):
        try:
            job.gen.close()
        except Exception:
            pass
        return                         # requester vanished mid-scan
    try:
        next(job.gen)
        job.chunks += 1
        _jobs.append(job)
    except StopIteration as e:
        value = e.value if e.value is not None else {}
        _queue(job.handle, _correlated(
            {"id": job.rid, "ok": True, "result": value},
            job.client_session,
            job.request_token,
        ))
    except Exception as e:
        _queue(job.handle, _correlated({
            "id": job.rid, "ok": False,
            "error": "%s: %s" % (type(e).__name__, e),
            "trace": traceback.format_exc(limit=6)},
            job.client_session,
            job.request_token,
        ))


def _queue(handle, resp):
    try:
        json.dumps(resp, default=str)
    except Exception as e:
        source = resp if isinstance(resp, dict) else {}
        resp = _correlated(
            {"id": source.get("id"), "ok": False,
             "error": "unserialisable result: %s" % e},
            source.get("client_session"),
            source.get("request_token"),
        )
    _transport.respond(handle, resp)


# ---------------------------------------------------------------------------
# transports
# ---------------------------------------------------------------------------
#
# TCP is preferred, but FL Studio runs device scripts in a sub-interpreter
# where `_socket` is unusable: it is a single-phase-init C extension, so the
# process-wide extension cache hands this interpreter a socket type owned by
# the host, and constructing one fails with
#
#     slot wrapper '__init__' of '_socket.socket' objects
#     returned NULL without setting an exception
#
# Re-importing does not help, because the cache is keyed per process rather
# than per interpreter. In the validated FL host, plain file writes are also
# blocked, so MIDI SysEx is the production transport. TCP and the file mailbox
# remain useful fallbacks for hosts and deterministic tests where they work.


class _SocketTransport:
    """Loopback TCP. Fast, but unavailable inside FL Studio."""

    name = "tcp"

    def __init__(self):
        self.server = None
        self.clients = []

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen(MAX_CLIENTS)
        s.setblocking(False)
        self.server = s
        return "listening on %s:%d" % (HOST, PORT)

    def alive(self, handle):
        return handle in self.clients

    def poll(self):
        self._accept()
        out = []
        for c in list(self.clients):
            try:
                r, _w, _x = select.select([c.sock], [], [], 0)
            except Exception:
                self._drop(c)
                continue
            if r:
                try:
                    chunk = c.sock.recv(65536)
                except BlockingIOError:
                    chunk = b""
                except Exception:
                    self._drop(c)
                    continue
                if chunk == b"":
                    self._drop(c)
                    continue
                if len(c.inbox) + len(chunk) > MAX_TRANSPORT_REQUEST_BYTES:
                    # A sender that never terminates a line would otherwise
                    # grow this buffer until the interpreter died. There is no
                    # partial request worth keeping, so drop the connection.
                    self.respond(c, {"id": None, "ok": False,
                                     "error": "request exceeds the transport "
                                              "size limit"})
                    self._drop(c)
                    continue
                c.inbox += chunk
            n = 0
            while n < MAX_COMMANDS_PER_TICK and b"\n" in c.inbox:
                line, c.inbox = c.inbox.split(b"\n", 1)
                n += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append((c, json.loads(line.decode("utf-8"))))
                except Exception as e:
                    self.respond(c, {"id": None, "ok": False,
                                     "error": "bad JSON: %s" % e})
        return out

    def respond(self, handle, resp):
        handle.outbox += (json.dumps(resp, default=str) + "\n").encode("utf-8")

    def flush(self):
        for c in list(self.clients):
            if not c.outbox:
                continue
            try:
                sent = c.sock.send(c.outbox[:MAX_SEND_PER_TICK])
                c.outbox = c.outbox[sent:]
            except BlockingIOError:
                pass
            except Exception:
                self._drop(c)

    def _accept(self):
        if self.server is None:
            return
        while True:
            try:
                conn, _addr = self.server.accept()
            except (BlockingIOError, OSError):
                return
            if len(self.clients) >= MAX_CLIENTS:
                try:
                    conn.close()
                except Exception:
                    pass
                continue
            self.clients.append(_Client(conn))
            _log("client connected (%d total)" % len(self.clients))

    def _drop(self, c):
        c.close()
        if c in self.clients:
            self.clients.remove(c)
            _log("client disconnected (%d left)" % len(self.clients))

    def close(self):
        for c in list(self.clients):
            c.close()
        self.clients = []
        if self.server is not None:
            try:
                self.server.close()
            except Exception:
                pass
            self.server = None


class _FileTransport:
    """A mailbox directory, using nothing but `os` and builtins.

    The MCP server drops `req-<token>.json` in; this picks them up on idle
    ticks and writes `resp-<token>.json` back. Both sides write to a temporary
    name and rename into place, so a reader never sees a half-written file.
    """

    name = "files"

    def __init__(self, root=None):
        # MAILBOX pins the directory when set; otherwise search the candidates.
        self.root = root or MAILBOX
        self.ticks = 0

    def start(self):
        # FL sandboxes the script interpreter: calls that alter the machine
        # fail by returning NULL with no exception set, which surfaces as
        # "<built-in function mkdir> returned NULL without setting an
        # exception". So never create a directory - only ever use one that
        # already exists, and prove it is writable before relying on it.
        candidates = [self.root] if self.root else _mailbox_candidates()
        problems = []
        for directory in candidates:
            if not directory or not os.path.isdir(directory):
                problems.append("%s (does not exist)" % directory)
                continue
            try:
                self._verify_writable(directory)
            except Exception as e:
                problems.append("%s (%s)" % (directory, e))
                continue
            self.root = directory
            self._clear_stale()
            self._touch_alive()
            return "mailbox in %s" % directory

        for p in problems:
            _log("  mailbox candidate unusable: %s" % p)
        raise IOError("no writable directory among %d candidates"
                      % len(candidates))

    def _verify_writable(self, directory):
        probe = os.path.join(directory, PREFIX + "probe.tmp")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        with open(probe, encoding="utf-8") as fh:
            if fh.read() != "ok":
                raise IOError("read back wrong data")
        os.remove(probe)

    def _clear_stale(self):
        try:
            names = os.listdir(self.root)
        except OSError:
            return
        for n in names:
            if n.startswith(PREFIX):
                try:
                    os.remove(os.path.join(self.root, n))
                except OSError:
                    pass

    def alive(self, handle):
        return True          # a request file has no connection to lose

    def poll(self):
        out = []
        try:
            names = sorted(n for n in os.listdir(self.root)
                           if n.startswith(REQ_PREFIX) and n.endswith(".json"))
        except OSError:
            return out
        for name in names[:MAX_COMMANDS_PER_TICK]:
            path = os.path.join(self.root, name)
            token = name[len(REQ_PREFIX):-5]
            try:
                # Check the size before reading: os.path.getsize is a stat, so
                # an oversized request never reaches memory at all.
                if os.path.getsize(path) > MAX_TRANSPORT_REQUEST_BYTES:
                    os.remove(path)
                    self.respond(token, {
                        "id": None, "ok": False,
                        "error": "request exceeds the transport size limit"})
                    continue
                with open(path, encoding="utf-8") as fh:
                    body = fh.read(MAX_TRANSPORT_REQUEST_BYTES + 1)
                os.remove(path)
            except OSError:
                continue
            try:
                out.append((token, json.loads(body)))
            except Exception as e:
                self.respond(token, {"id": None, "ok": False,
                                     "error": "bad JSON: %s" % e})
        return out

    def respond(self, handle, resp):
        self._write_atomic(RESP_PREFIX + "%s.json" % handle,
                           json.dumps(resp, default=str))

    def flush(self):
        # Refresh the liveness marker about once a second; the MCP server
        # reads its mtime to tell "FL is not running" from "FL is busy".
        self.ticks += 1
        if self.ticks % 50 == 0:
            self._touch_alive()

    def _touch_alive(self):
        self._write_atomic(ALIVE_NAME,
                           json.dumps({"protocol": PROTOCOL_VERSION,
                                       "transport": self.name,
                                       "ticks": self.ticks}))

    def _write_atomic(self, name, text):
        tmp = os.path.join(self.root, PREFIX + "tmp-" + name)
        final = os.path.join(self.root, name)
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, final)
        except OSError as e:
            _log("could not write %s - %s" % (name, e))

    def close(self):
        for n in (ALIVE_NAME,):
            try:
                os.remove(os.path.join(self.root, n))
            except OSError:
                pass


class _MidiTransport:
    """Carry the protocol over MIDI SysEx, using only FL's own device module.

    FL sandboxes the script interpreter: sockets and every filesystem write
    fail by returning NULL with no exception set. MIDI is the one channel
    left, because it goes through FL's API rather than the OS.

    Frame layout, every byte 7-bit as SysEx requires:

        F0 7D TAG  ID_HI ID_LO  SEQ_HI SEQ_LO  TOT_HI TOT_LO  <payload> F7

    TAG separates the two directions. The IAC bus echoes to every subscriber,
    including the sender, so each side must ignore its own traffic. Payload is
    JSON with ensure_ascii, which is 7-bit clean by construction.
    """

    name = "midi"

    def __init__(self):
        # msg id -> {total, parts, bytes, updated_tick}. Both the number of
        # slots and their aggregate retained payload are independently capped.
        self.partial = {}
        self.partial_bytes = 0
        self.ready = []         # assembled (handle, request) pairs
        self.outbox = []        # frames still to go out
        self.ticks = 0
        self.out_assigned = None

    # -- lifecycle --------------------------------------------------------

    def start(self):
        if not _safe(lambda: device.isAssigned(), False):
            raise IOError("no MIDI input is assigned to this script")
        self.out_assigned = _safe(lambda: device.isMidiOutAssigned(), None)
        if self.out_assigned is False:
            raise IOError(
                "MIDI output is not assigned - in Options > MIDI settings give "
                "the output device the same Port number as the input")
        self._send(TAG_RESPONSE, 0, [self._hello()])
        return "MIDI SysEx"

    def _hello(self):
        return json.dumps({
            "hello": True,
            "protocol": PROTOCOL_VERSION,
            MIDI_WIRE_PROTOCOL_FIELD: MIDI_WIRE_PROTOCOL_VERSION,
            "transport": self.name,
        })

    def close(self):
        self.partial = {}
        self.partial_bytes = 0
        self.ready = []
        self.outbox = []

    # -- receiving --------------------------------------------------------

    def _drop_partial(self, mid):
        slot = self.partial.pop(mid, None)
        if slot is not None:
            self.partial_bytes = max(
                0, self.partial_bytes - int(slot.get("bytes", 0)))
        return slot

    def _expire_partial(self):
        expired = [
            mid for mid, slot in self.partial.items()
            if self.ticks - int(slot.get("updated_tick", self.ticks))
            > SYSEX_PARTIAL_TTL_TICKS
        ]
        for mid in expired:
            self._drop_partial(mid)

    def feed(self, data):
        """Called from OnSysEx with the raw bytes of one message."""
        self._expire_partial()
        if (len(data) < 10 or len(data) > MAX_SYSEX_INPUT_FRAME_BYTES
                or data[0] != 0xF0 or data[1] != SYSEX_ID):
            return
        if data[2] != TAG_REQUEST:
            return                      # our own echo, or someone else's
        try:
            header = [int(value) for value in data[1:9]]
        except Exception:
            return
        if any(value < 0 or value > 0x7F for value in header):
            return
        end = len(data) - 1 if data[-1] == 0xF7 else len(data)
        mid = (header[2] << 7) | header[3]
        seq = (header[4] << 7) | header[5]
        total = (header[6] << 7) | header[7]
        if (mid < 1 or total < 1 or total > MAX_SYSEX_REQUEST_PARTS
                or seq < 0 or seq >= total):
            return
        try:
            text = bytes(data[9:end]).decode("ascii")
        except Exception:
            return

        slot = self.partial.get(mid)
        if slot is None:
            if len(self.partial) >= MAX_SYSEX_PARTIAL_MESSAGES:
                return
            slot = {"total": total, "parts": {}, "bytes": 0,
                    "updated_tick": self.ticks}
            self.partial[mid] = slot
        elif slot["total"] != total:
            self._drop_partial(mid)
            return

        previous = slot["parts"].get(seq)
        if previous is not None:
            if previous != text:
                self._drop_partial(mid)
            else:
                slot["updated_tick"] = self.ticks
            return

        added = len(text)
        if (slot["bytes"] + added > MAX_SYSEX_REQUEST_BYTES
                or self.partial_bytes + added > MAX_SYSEX_PARTIAL_BYTES):
            self._drop_partial(mid)
            return
        slot["parts"][seq] = text
        slot["bytes"] += added
        slot["updated_tick"] = self.ticks
        self.partial_bytes += added
        if len(slot["parts"]) < total:
            return

        if not all(i in slot["parts"] for i in range(total)):
            self._drop_partial(mid)
            return
        slot = self._drop_partial(mid)
        body = "".join(slot["parts"][i] for i in range(total))
        try:
            request = json.loads(body)
        except Exception as e:
            self.respond(mid, {"id": None, "ok": False,
                               "error": "bad JSON over sysex: %s" % e})
            return
        if not isinstance(request, dict):
            self.respond(mid, {"id": None, "ok": False,
                               "error": "SysEx request must be a JSON object"})
            return
        if len(self.ready) >= MAX_SYSEX_READY_MESSAGES:
            self.respond(mid, _correlated(
                {"id": request.get("id"), "ok": False,
                 "error": "bridge MIDI request queue is full"},
                request.get("client_session"),
                request.get("request_token"),
            ))
            return
        self.ready.append((mid, request))

    def poll(self):
        out = self.ready[:MAX_COMMANDS_PER_TICK]
        del self.ready[:len(out)]
        return out

    # -- sending ----------------------------------------------------------

    def alive(self, handle):
        return True

    def respond(self, handle, resp):
        text = json.dumps(resp, default=str)
        if len(text) > MAX_SYSEX_RESPONSE_WIRE_BYTES:
            source = resp if isinstance(resp, dict) else {}
            text = json.dumps(_correlated(
                {
                    "id": source.get("id"),
                    "ok": False,
                    "error": (
                        "bridge response exceeds the MIDI size limit "
                        "compatible with v0.12 receivers"
                    ),
                },
                source.get("client_session"),
                source.get("request_token"),
            ))
        chunks = [text[i:i + SYSEX_CHUNK]
                  for i in range(0, len(text), SYSEX_CHUNK)] or [""]
        self._send(TAG_RESPONSE, handle, chunks)

    def abandon_pending_responses(self):
        """Drop frames for a requester that no longer owns the MIDI bus."""
        self.outbox = []

    def _send(self, tag, mid, chunks):
        total = len(chunks)
        if (total < 1 or total > MAX_SYSEX_RESPONSE_PARTS
                or any(len(chunk) > SYSEX_CHUNK for chunk in chunks)
                or len(self.outbox) + total > MAX_SYSEX_OUTBOX_FRAMES):
            return False
        for seq, chunk in enumerate(chunks):
            frame = [0xF0, SYSEX_ID, tag,
                     (mid >> 7) & 0x7F, mid & 0x7F,
                     (seq >> 7) & 0x7F, seq & 0x7F,
                     (total >> 7) & 0x7F, total & 0x7F]
            frame.extend(ord(c) & 0x7F for c in chunk)
            frame.append(0xF7)
            self.outbox.append(bytes(frame))
        return True

    def flush(self):
        sent = 0
        while self.outbox and sent < MAX_SYSEX_PER_TICK:
            frame = self.outbox.pop(0)
            try:
                device.midiOutSysex(frame)
            except Exception as e:
                _log("sysex send failed - %s" % e)
                self.outbox = []
                return
            sent += 1

        # Heartbeat, so the MCP server can tell a live bridge from a dead one.
        self.ticks += 1
        self._expire_partial()
        if self.ticks % 50 == 0 and not self.outbox:
            self._send(TAG_RESPONSE, 0, [self._hello()])


def _start_server():
    """Bring up the best transport that works in this interpreter."""
    global _transport
    for transport in (_SocketTransport(), _FileTransport(),
                      _MidiTransport()):
        try:
            detail = transport.start()
        except Exception as e:
            _log("%s transport unavailable - %s" % (transport.name, e))
            continue
        _transport = transport
        _log("ready: %s (%s transport)" % (detail, transport.name))
        return
    _transport = _NullTransport()
    _log("FAILED: no usable transport. The MCP server cannot reach FL Studio.")


class _NullTransport:
    name = "none"

    def alive(self, handle):
        return False

    def poll(self):
        return []

    def respond(self, handle, resp):
        pass

    def flush(self):
        pass

    def close(self):
        pass


_transport = _NullTransport()


def _pump():
    for handle, req in _transport.poll():
        if (
            getattr(_transport, "name", None) == "midi"
            and isinstance(req, dict)
            and req.get("client_session") is not None
            and req.get("request_token") is not None
        ):
            # The native endpoint ownership lock serializes MIDI clients. A
            # new correlated request means any older job/reply was abandoned
            # by a timed-out or exited process. Do not let that old response
            # collide with the successor's 14-bit wire ID.
            for abandoned in list(_jobs):
                try:
                    abandoned.gen.close()
                except Exception:
                    pass
            del _jobs[:]
            _transport.abandon_pending_responses()
        resp = _dispatch(req)
        if isinstance(resp, _Job):
            if len(_jobs) >= MAX_PENDING_JOBS:
                try:
                    resp.gen.close()
                except Exception:
                    pass
                _queue(handle, _correlated(
                    {
                        "id": resp.rid,
                        "ok": False,
                        "error": "bridge command queue is full",
                    },
                    resp.client_session,
                    resp.request_token,
                ))
            else:
                resp.handle = handle
                _jobs.append(resp)  # first chunk runs on the next tick
        else:
            _queue(handle, resp)
    _transport.flush()


# ---------------------------------------------------------------------------
# FL Studio callbacks
# ---------------------------------------------------------------------------


def OnInit():
    _start_server()
    try:
        ui.setHintMsg("Universal Bridge ready (%s)" % _transport.name)
    except Exception:
        pass


def OnDeInit():
    for job in list(_jobs):
        try:
            job.gen.close()
        except Exception:
            pass
    del _jobs[:]
    # Closing live-note generators normally sends their note-offs. The
    # registry catches any job that was cancelled elsewhere, plus a note-off
    # API call that failed and needs one final best-effort retry on unload.
    _cleanup_active_notes(force_all=True)
    try:
        _transport.close()
    except Exception:
        pass
    _log("stopped")


def OnIdle():
    global _idle_tick
    try:
        _idle_tick += 1
        _pump()
        # Only records whose scheduled/finally note-off failed are retried;
        # notes whose duration has not elapsed remain registered but untouched.
        _cleanup_active_notes()
        _advance_jobs()
    except Exception:
        # Never let an exception escape a callback; FL disables the script.
        _log(traceback.format_exc(limit=4))


def OnMidiMsg(event):
    # The bridge does not consume ordinary MIDI; let it through to FL.
    event.handled = False


def OnSysEx(event):
    """Feed SysEx to the transport when that is how we are talking."""
    try:
        feed = getattr(_transport, "feed", None)
        if feed is None:
            return
        data = getattr(event, "sysex", None)
        if data:
            feed(data)
            event.handled = True
    except Exception:
        _log(traceback.format_exc(limit=4))
