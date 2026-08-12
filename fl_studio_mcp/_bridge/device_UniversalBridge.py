# name=Universal Bridge
# supportedDevices=Universal Bridge
# receiveFrom=
"""
Universal Bridge - FL Studio MIDI Controller Script.

Exposes a narrow FL Studio scripting surface to an external MCP server. The
bridge is locked read-only by default. A small, readback-verified write surface
can be turned on with FL_BRIDGE_ENABLE_WRITES=1.

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
PARAMS_PER_TICK = 64
CHANNELS_PER_TICK = 8
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
SYSEX_CHUNK = 1024
MAX_SYSEX_PER_TICK = 8

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
MAX_SYSEX_RESPONSE_PARTS = (
    MAX_SYSEX_RESPONSE_BYTES + SYSEX_CHUNK - 1) // SYSEX_CHUNK
MAX_SYSEX_FRAME_BYTES = 10 + SYSEX_CHUNK
MAX_SYSEX_PARTIAL_MESSAGES = 8
MAX_SYSEX_PARTIAL_BYTES = MAX_SYSEX_REQUEST_BYTES
MAX_SYSEX_READY_MESSAGES = 16
MAX_SYSEX_OUTBOX_FRAMES = MAX_SYSEX_RESPONSE_PARTS + MAX_SYSEX_PER_TICK
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
# It is off unless FL is launched with this flag. Setting it never enables
# anything outside the twenty explicit command names below.
LEAN_WRITES_ENABLED = (
    os.environ.get("FL_BRIDGE_ENABLE_WRITES", "").strip() == "1"
)
READ_ONLY_COMMANDS = frozenset({
    "ping",
    "project.info",
    "arrangement.selection",
    "mixer.list",
    "mixer.track",
    "plugin.params",
    # A read, like plugin.params: it walks the same indices with the same
    # padding rule and writes nothing.
    "plugin.scan_params",
    "channels.list",
    "sequencer.get",
})
LEAN_WRITE_COMMANDS = frozenset({
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
    "transport.set_playing",
    "transport.stop",
    "transport.set_song_position",
    "transport.set_loop_mode",
    "transport.set_tempo",
    "channel.set_mix",
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


def _mixer_track_index(value):
    """Return a valid live mixer index, never a fabricated empty track."""
    index = _strict_integer(value, "mixer track index")
    count = int(mixer.trackCount())
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
        "verified_writes_enabled": bool(LEAN_WRITES_ENABLED),
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
        "mixer_track_count": _safe(lambda: mixer.trackCount(), None),
        "channel_count": _safe(lambda: channels.channelCount(), None),
        "pattern_count": _safe(lambda: patterns.patternCount(), None),
        "playlist_track_count": _safe(lambda: playlist.trackCount(), None),
        "unsaved_changes": _safe(lambda: general.getChangedFlag(), None),
        "undo_history_position": _safe(lambda: general.getUndoHistoryPos(), None),
        "undo_history_count": _safe(lambda: general.getUndoHistoryCount(), None),
        "metronome": _safe(lambda: ui.isMetronomeEnabled(), None),
    }


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
    n = mixer.trackCount()
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


def cmd_mixer_track(a):
    i = _mixer_track_index(a["track"])
    t = _track_summary(i, with_slots=True, with_peaks=True)
    t["eq"] = cmd_mixer_eq_get({"track": i})
    routes = []
    for d in range(mixer.trackCount()):
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


# ---------------------------------------------------------------------------
# lean verified write surface (FL_BRIDGE_ENABLE_WRITES=1)
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

# The mixer setters store the float they are handed, so this only has to absorb
# float round-tripping - it is not slack for a curve.
MIXER_READBACK_TOLERANCE = 1e-4
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
        option        the option text to land on, matched case-insensitively
                      and then as a substring
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
        for display, value in options:
            if display and low in display.lower():
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
    if key in ("playing", "muted") and type(wanted) is not bool:
        raise ValueError("expected_before.%s must be true or false" % key)
    if key in ("color", "mixer_destination") and (
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
    last_insert = int(mixer.getTrackInfo(midi.TN_LastIns))
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


HANDLERS = {
    "ping": cmd_ping,
    "project.info": cmd_project_info,
    "arrangement.selection": cmd_arrangement_selection,
    "mixer.list": cmd_mixer_list,
    "mixer.track": cmd_mixer_track,
    "transport.set_playing": cmd_transport_set_playing,
    "transport.stop": cmd_transport_stop,
    "transport.set_song_position": cmd_transport_set_song_position,
    "transport.set_loop_mode": cmd_transport_set_loop_mode,
    "transport.set_tempo": cmd_transport_set_tempo,
    # The lean verified write surface; reachable only with
    # FL_BRIDGE_ENABLE_WRITES=1, see LEAN_WRITE_COMMANDS in _dispatch.
    "mixer.set_volume": cmd_mixer_set_volume,
    "mixer.set_pan": cmd_mixer_set_pan,
    "mixer.set_mute": cmd_mixer_set_mute,
    "mixer.set_eq": cmd_mixer_set_eq,
    "mixer.set_name": cmd_mixer_set_name,
    "mixer.set_send": cmd_mixer_set_send,
    "mixer.set_send_level": cmd_mixer_set_send_level,
    "plugin.set_param": cmd_plugin_set_param,
    "plugin.set_param_display": cmd_plugin_set_param_display,
    "plugin.set_param_option": cmd_plugin_set_param_option,
    "plugin.params": cmd_plugin_params,
    "plugin.scan_params": cmd_plugin_scan_params,
    "channels.list": cmd_channels_list,
    "channel.set_mix": cmd_channel_set_mix,
    "channel.set_identity": cmd_channel_set_identity,
    "channel.route_to_mixer": cmd_channel_route_to_mixer,
    "sequencer.get": cmd_sequencer_get,
    "sequencer.set": cmd_sequencer_set,
    "channel.trigger_note": cmd_channel_trigger_note,
}


class _Job:
    """A command that spreads its work over several idle ticks."""

    def __init__(self, handle, rid, gen, cmd):
        self.handle = handle
        self.rid = rid
        self.gen = gen
        self.cmd = cmd
        self.chunks = 0


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
    cmd = req.get("cmd", "")
    args = req.get("args", {})
    if not isinstance(args, dict):
        return {
            "id": rid,
            "ok": False,
            "error": "bridge request args must be a JSON object",
        }
    allowed = READ_ONLY_COMMANDS
    lock_reason = "bridge is locked read-only"
    if LEAN_WRITES_ENABLED:
        allowed = allowed | LEAN_WRITE_COMMANDS
        lock_reason = "bridge exposes only read commands plus verified writes"
    available = sorted(allowed)
    if cmd not in allowed:
        return {
            "id": rid,
            "ok": False,
            "error": "%s; command %r is prohibited" % (lock_reason, cmd),
            "available": available,
        }
    handler = HANDLERS.get(cmd)
    if handler is None:
        return {"id": rid, "ok": False, "error": "unknown command %r" % cmd,
                "available": available}
    try:
        result = handler(args)
        if isinstance(result, types.GeneratorType):
            return _Job(None, rid, result, cmd)
        return {"id": rid, "ok": True, "result": result}
    except Exception as e:
        return {"id": rid, "ok": False, "error": "%s: %s" % (type(e).__name__, e),
                "trace": traceback.format_exc(limit=6)}


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
        _queue(job.handle, {"id": job.rid, "ok": True, "result": value})
    except Exception as e:
        _queue(job.handle, {
            "id": job.rid, "ok": False,
            "error": "%s: %s" % (type(e).__name__, e),
            "trace": traceback.format_exc(limit=6)})


def _queue(handle, resp):
    try:
        json.dumps(resp, default=str)
    except Exception as e:
        resp = {"id": resp.get("id"), "ok": False,
                "error": "unserialisable result: %s" % e}
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
        return json.dumps({"hello": True, "protocol": PROTOCOL_VERSION,
                           "transport": self.name})

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
        if (len(data) < 10 or len(data) > MAX_SYSEX_FRAME_BYTES
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
            self.respond(mid, {"id": request.get("id"), "ok": False,
                               "error": "bridge MIDI request queue is full"})
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
        if len(text) > MAX_SYSEX_RESPONSE_BYTES:
            rid = resp.get("id") if isinstance(resp, dict) else None
            text = json.dumps({
                "id": rid,
                "ok": False,
                "error": "bridge response exceeds the MIDI size limit",
            })
        chunks = [text[i:i + SYSEX_CHUNK]
                  for i in range(0, len(text), SYSEX_CHUNK)] or [""]
        self._send(TAG_RESPONSE, handle, chunks)

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
        resp = _dispatch(req)
        if isinstance(resp, _Job):
            if len(_jobs) >= MAX_PENDING_JOBS:
                try:
                    resp.gen.close()
                except Exception:
                    pass
                _queue(handle, {
                    "id": resp.rid,
                    "ok": False,
                    "error": "bridge command queue is full",
                })
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
