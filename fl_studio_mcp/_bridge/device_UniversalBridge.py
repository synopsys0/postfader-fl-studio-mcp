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
import socket
import os
import select
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

# The lean write surface: commands that each change exactly one thing and
# then read FL back to say whether it actually moved. Off unless FL is launched
# with this flag. Setting it never enables anything outside the ten verified
# command names below.
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
})

MAX_PENDING_JOBS = 32
_jobs = []     # list of _Job, chunked commands still running

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
    raw = _safe(lambda: mixer.getCurrentTempo(), None)
    if raw is None:
        return None
    return round(raw / 1000.0, 4) if raw > 1000 else round(raw, 4)


def _mixer_track_index(value):
    """Return a valid live mixer index, never a fabricated empty track."""
    index = int(value)
    count = int(mixer.trackCount())
    if index < 0 or index >= count:
        raise ValueError(
            "mixer track index %d is outside the live range 0..%d"
            % (index, max(0, count - 1))
        )
    return index


def _effect_slot_index(value):
    """Return a valid zero-based mixer effect slot index."""
    slot = int(value)
    if slot < 0 or slot >= MIXER_SLOTS:
        raise ValueError("effect slot index must be 0..%d" % (MIXER_SLOTS - 1))
    return slot


def _plugin_summary(track, slot):
    if not _safe(lambda: plugins.isValid(track, slot), False):
        return None
    return {
        "slot": slot,
        "name": _safe(lambda: plugins.getPluginName(track, slot), ""),
        "user_name": _safe(lambda: plugins.getPluginName(track, slot, True), ""),
        "param_count": _safe(lambda: plugins.getParamCount(track, slot), 0),
        "mix_level": _safe(lambda: mixer.getPluginMixLevel(track, slot), None),
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


def _param_state(track, slot, idx):
    return (_safe(lambda: plugins.getParamValue(idx, track, slot), None),
            _safe(lambda: plugins.getParamValueString(idx, track, slot), "") or "")


def _set_param_verified(track, slot, idx, value, attempts=4):
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
    before_v, before_d = _param_state(track, slot, idx)

    cur_v, cur_d = before_v, before_d
    for _ in range(attempts):
        # Always issue two writes: FL drops a lone one, and the second is
        # guaranteed to be a repeat of the same parameter.
        plugins.setParamValue(value, idx, track, slot, PICKUP_NONE)
        plugins.setParamValue(value, idx, track, slot, PICKUP_NONE)
        cur_v, cur_d = _param_state(track, slot, idx)
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
        # ten verified commands.
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


def cmd_plugin_params(a):
    track = _mixer_track_index(a["track"])
    slot = _effect_slot_index(a.get("slot", -1))
    if not _safe(lambda: plugins.isValid(track, slot), False):
        raise ValueError("no plugin at track %d slot %d" % (track, slot))
    count = plugins.getParamCount(track, slot)
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
        pname = _safe(lambda: plugins.getParamName(p, track, slot), "") or ""
        pdisp = _safe(lambda: plugins.getParamValueString(p, track, slot), "") or ""
        if skip_padding and _is_padding(pname, pdisp):
            padding += 1
        elif name_filter and name_filter not in pname.lower():
            pass
        else:
            params.append(
                {
                    "index": p,
                    "name": pname,
                    "value": _safe(lambda: plugins.getParamValue(p, track, slot), None),
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
        "track": track,
        "slot": slot,
        "plugin": _safe(lambda: plugins.getPluginName(track, slot), ""),
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
    track = _mixer_track_index(a["track"])
    slot = _effect_slot_index(a.get("slot", -1))
    if not _safe(lambda: plugins.isValid(track, slot), False):
        raise ValueError("no plugin at track %d slot %d" % (track, slot))
    # What FL claims. For a VST this is a padded maximum and never a count of
    # the parameters that exist, which is the whole reason this command exists.
    reported_count = int(_safe(lambda: plugins.getParamCount(track, slot), 0) or 0)

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
        pname = _safe(lambda: plugins.getParamName(p, track, slot), "") or ""
        pdisp = _safe(
            lambda: plugins.getParamValueString(p, track, slot), "") or ""
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
                        lambda: plugins.getParamValue(p, track, slot), None),
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
        "track": track,
        "slot": slot,
        "plugin": _safe(lambda: plugins.getPluginName(track, slot), ""),
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


def cmd_channels_list(a):
    n = channels.channelCount()
    out = []
    for i in range(n):
        out.append(
            {
                "index": i,
                "name": _safe(lambda: channels.getChannelName(i), ""),
                "volume": _safe(lambda: channels.getChannelVolume(i), None),
                "pan": _safe(lambda: channels.getChannelPan(i), None),
                "muted": _safe(lambda: channels.isChannelMuted(i), None),
                "solo": _safe(lambda: channels.isChannelSolo(i), None),
                "selected": _safe(lambda: channels.isChannelSelected(i), None),
                "type": _safe(lambda: channels.getChannelType(i), None),
                "mixer_track": _safe(lambda: channels.getTargetFxTrack(i), None),
                "color": _safe(lambda: channels.getChannelColor(i), None),
                "plugin": _safe(lambda: plugins.getPluginName(i), ""),
            }
        )
    return {"channel_count": n, "channels": out}


# ---------------------------------------------------------------------------
# lean verified write surface (FL_BRIDGE_ENABLE_WRITES=1)
#
# Ten commands, each changing one narrowly scoped property and then reading
# FL back to say what actually happened. Shared rules:
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
    if index == 0 and not bool(a.get("allow_master", False)):
        raise ValueError(
            "refusing to write to mixer track 0 (master); pass "
            "allow_master=true to target the master bus deliberately"
        )
    return index


def _lean_value(a, key, low, high):
    value = a.get(key)
    if value is None:
        raise ValueError("%s is required" % key)
    value = float(value)
    if value < low or value > high:
        raise ValueError(
            "%s must be within %g..%g (got %r)" % (key, low, high, value)
        )
    return value


def _near(value, target, tol):
    if value is None:
        return False
    try:
        return abs(float(value) - float(target)) <= tol
    except (TypeError, ValueError):
        return False


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
    before = _safe(lambda: mixer.getTrackVolume(i), None)
    before_db = _safe(lambda: mixer.getTrackVolume(i, 1), None)
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
    }


def cmd_mixer_set_pan(a):
    """Set one mixer pan, -1.0 hard left to 1.0 hard right."""
    i = _lean_track(a)
    value = _lean_value(a, "value", -1.0, 1.0)
    before = _safe(lambda: mixer.getTrackPan(i), None)
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
    }


def cmd_mixer_set_mute(a):
    """Mute or unmute one mixer track. Never a toggle: state is stated."""
    i = _lean_track(a)
    if a.get("muted") is None:
        raise ValueError("muted is required (true or false)")
    want = bool(a["muted"])
    before = _safe(lambda: mixer.isTrackMuted(i), None)
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
    }


def cmd_mixer_set_eq(a):
    """Set gain and/or frequency on one band of a track's built-in EQ.

    Both are normalised 0..1; the reply carries the dB and Hz FL shows for
    them so the caller can see what those normalised numbers mean.
    """
    i = _lean_track(a)
    band = int(a["band"])
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

    before = _eq_band_state(i, band)
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
    i = _lean_track(a)
    slot = _effect_slot_index(a.get("slot", -1))
    if not _safe(lambda: plugins.isValid(i, slot), False):
        raise ValueError("no plugin at track %d slot %d" % (i, slot))
    index = int(a["index"])
    # FL pads VST parameter lists out to a fixed size, so getParamCount is an
    # upper bound and never a count of the parameters that really exist. It is
    # used here only to reject an index FL could not address at all.
    reported_count = _safe(lambda: plugins.getParamCount(i, slot), 0) or 0
    if index < 0 or (reported_count and index >= reported_count):
        raise ValueError(
            "parameter index %d is outside the 0..%d FL reports for track %d "
            "slot %d" % (index, max(0, reported_count - 1), i, slot)
        )
    value = _lean_value(a, "value", 0.0, 1.0)

    before_value, before_display = _param_state(i, slot, index)
    undone = _save_undo(
        "Universal Bridge: %s param %d"
        % (_safe(lambda: plugins.getPluginName(i, slot), "plugin"), index)
    )
    # The helper's own verdict is not taken: it decides from the numbers it
    # happened to read last. This handler judges from the before and after it
    # captured itself.
    after_value, after_display, _helper_verdict = _set_param_verified(
        i, slot, index, value
    )
    # Same staleness as the mixer controls: the display string read in the
    # write's own tick lags by a whole operation. Observed live -- restoring a
    # parameter to 0.6855 ("20 ms") reported "78 ms", the display belonging to
    # the value set just before it. Give FL a tick, then re-read, so
    # display_changed describes this write rather than the previous one.
    yield
    after_value, after_display = _param_state(i, slot, index)
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
        "track": i,
        "slot": slot,
        "index": index,
        "plugin": _safe(lambda: plugins.getPluginName(i, slot), ""),
        "name": _safe(lambda: plugins.getParamName(index, i, slot), ""),
        "requested": value,
        "before": {"value": before_value, "display": before_display},
        "after": {"value": after_value, "display": after_display},
        "verified": reads_at_value or display_changed,
        "verification_basis": basis,
        "display_changed": display_changed,
        "reads_at_value": reads_at_value,
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
    want = str(a["name"])
    before = _safe(lambda: mixer.getTrackName(i), None)
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
    if a.get("enabled") is None:
        raise ValueError("enabled is required (true or false)")
    want = bool(a["enabled"])
    before = _safe(lambda: mixer.getRouteSendActive(i, dest), None)
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
    if not _safe(lambda: mixer.getRouteSendActive(i, dest), False):
        raise ValueError(
            "track %d does not send to track %d, so its level cannot be set; "
            "create the route first with mixer.set_send" % (i, dest)
        )
    before = _safe(lambda: mixer.getRouteToLevel(i, dest), None)
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
    }


# How far past the last real-looking parameter a name search keeps walking
# before concluding the rest is padding. The plug-ins measured so far cluster
# every real control inside the first hundred or so indices with scattered
# gaps, and 256 tolerates those gaps without walking thousands of empty slots.
# That sample is narrow: a plug-in that leaves a wider gap loses whatever sits
# past it. See docs/plugin-support.md before trusting this on an untested VST.
PARAM_SEARCH_RUN = 256


def _resolve_named_param(track, slot, query):
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

    reported = int(_safe(lambda: plugins.getParamCount(track, slot), 0) or 0)
    limit = min(reported, MAX_PARAM_INDEX_SCAN)
    found = []          # (index, lowered name, lowered display)
    since_real = 0
    examined = 0

    for p in range(limit):
        pname = _safe(lambda: plugins.getParamName(p, track, slot), "") or ""
        pdisp = _safe(lambda: plugins.getParamValueString(p, track, slot), "") or ""
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

    for index, pname, pdisp in found:
        if pname == wanted:
            return index, "name", pname
    for index, pname, pdisp in found:
        if pdisp == wanted:
            return index, "display", pdisp
    for index, pname, pdisp in found:
        if pname and wanted in pname:
            return index, "name_substring", pname
    for index, pname, pdisp in found:
        if pdisp and wanted in pdisp:
            return index, "display_substring", pdisp

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


def _sweep_options(track, slot, index, steps):
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
        plugins.setParamValue(value, index, track, slot, PICKUP_NONE)
        plugins.setParamValue(value, index, track, slot, PICKUP_NONE)
        yield
        display = _safe(
            lambda: plugins.getParamValueString(index, track, slot), "") or ""
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
    track = _lean_track(a)
    slot = _effect_slot_index(a.get("slot", -1))
    if not _safe(lambda: plugins.isValid(track, slot), False):
        raise ValueError("no plugin at track %d slot %d" % (track, slot))
    if a.get("param") is None:
        raise ValueError("param is required (an index, a name, or a display)")
    wanted = a.get("option")
    if wanted is None or not str(wanted).strip():
        raise ValueError("option is required (the display text to land on)")
    wanted = str(wanted).strip()
    steps = int(a.get("steps", OPTION_SWEEP_STEPS))
    if steps < 2 or steps > MAX_OPTION_SWEEP_STEPS:
        raise ValueError("steps must be 2..%d" % MAX_OPTION_SWEEP_STEPS)

    index, matched_on, matched_text = yield from _resolve_named_param(
        track, slot, a["param"]
    )
    original = _safe(lambda: plugins.getParamValue(index, track, slot), None)
    before_display = _safe(
        lambda: plugins.getParamValueString(index, track, slot), "") or ""

    undone = _save_undo(
        "Universal Bridge: %s param %d"
        % (_safe(lambda: plugins.getPluginName(track, slot), "plugin"), index)
    )
    seen = yield from _sweep_options(track, slot, index, steps)
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
                plugins.setParamValue(original, index, track, slot, PICKUP_NONE),
                plugins.setParamValue(original, index, track, slot, PICKUP_NONE),
            ],
            lambda: _safe(lambda: plugins.getParamValue(index, track, slot), None),
            lambda got: _near(got, original, PARAM_NOOP_TOLERANCE),
        )
        if not restored:
            now = _safe(
                lambda: plugins.getParamValueString(index, track, slot), "") or ""
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
            plugins.setParamValue(chosen_value, index, track, slot, PICKUP_NONE),
            plugins.setParamValue(chosen_value, index, track, slot, PICKUP_NONE),
        ],
        lambda: (_safe(
            lambda: plugins.getParamValueString(index, track, slot), "") or "").strip(),
        lambda got: got.lower() == chosen_display.lower(),
    )
    return {
        "command": "plugin.set_param_option",
        "undo_point_created": undone,
        "track": track,
        "slot": slot,
        "index": index,
        "plugin": _safe(lambda: plugins.getPluginName(track, slot), ""),
        "name": _safe(lambda: plugins.getParamName(index, track, slot), ""),
        "matched_on": matched_on,
        "matched_text": matched_text,
        "requested": wanted,
        "selected": chosen_display,
        "normalised": chosen_value,
        "steps": steps,
        "options": [d for d, _ in options],
        "before": {"value": original, "display": before_display.strip()},
        "after": {
            "value": _safe(lambda: plugins.getParamValue(index, track, slot), None),
            "display": after,
        },
        "verified": verified,
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
    track = _lean_track(a)
    slot = _effect_slot_index(a.get("slot", -1))
    if not _safe(lambda: plugins.isValid(track, slot), False):
        raise ValueError("no plugin at track %d slot %d" % (track, slot))
    if a.get("param") is None:
        raise ValueError("param is required (an index, a name, or a display)")
    if a.get("target") is None:
        raise ValueError("target is required (a number in the plug-in's units)")
    target = float(a["target"])
    tol = a.get("tolerance")
    tol = max(0.01, abs(target) * 0.02) if tol is None else abs(float(tol))

    index, matched_on, matched_text = yield from _resolve_named_param(
        track, slot, a["param"]
    )
    if index < 0 or index >= int(
        _safe(lambda: plugins.getParamCount(track, slot), 0) or 0
    ):
        raise ValueError(
            "parameter index %d is outside the range this plug-in reports" % index
        )

    def read():
        text = _safe(lambda: plugins.getParamValueString(index, track, slot), "") or ""
        number = _first_float(text)
        if number is None:
            raise ValueError(
                "parameter %d displays %r, which has no number to search on; "
                "use plugin.set_param_option for an enumerated control"
                % (index, text)
            )
        return number

    before_value = _safe(lambda: plugins.getParamValue(index, track, slot), None)
    before_display = _safe(
        lambda: plugins.getParamValueString(index, track, slot), "") or ""
    read()  # fail before touching anything if this control has no number

    undone = _save_undo(
        "Universal Bridge: %s param %d"
        % (_safe(lambda: plugins.getPluginName(track, slot), "plugin"), index)
    )
    normalised, landed, within = yield from _solve_across_ticks(
        read,
        lambda v: plugins.setParamValue(
            max(0.0, min(1.0, v)), index, track, slot, PICKUP_NONE),
        target,
        tol,
    )
    yield
    after_display = _safe(
        lambda: plugins.getParamValueString(index, track, slot), "") or ""
    return {
        "command": "plugin.set_param_display",
        "undo_point_created": undone,
        "track": track,
        "slot": slot,
        "index": index,
        "plugin": _safe(lambda: plugins.getPluginName(track, slot), ""),
        "name": _safe(lambda: plugins.getParamName(index, track, slot), ""),
        "matched_on": matched_on,
        "matched_text": matched_text,
        "requested": target,
        "tolerance": tol,
        "landed_on": landed,
        "normalised": normalised,
        "before": {"value": before_value, "display": before_display},
        "after": {
            "value": _safe(lambda: plugins.getParamValue(index, track, slot), None),
            "display": after_display,
        },
        "verified": bool(within),
    }


HANDLERS = {
    "ping": cmd_ping,
    "project.info": cmd_project_info,
    "arrangement.selection": cmd_arrangement_selection,
    "mixer.list": cmd_mixer_list,
    "mixer.track": cmd_mixer_track,
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
    rid = req.get("id")
    cmd = req.get("cmd", "")
    args = req.get("args") or {}
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
    del _jobs[:]
    try:
        _transport.close()
    except Exception:
        pass
    _log("stopped")


def OnIdle():
    try:
        _pump()
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
