"""Shared mutable state for the fake FL Studio API used in tests."""


class Plugin:
    def __init__(self, name, params):
        self.name = name
        self.param_names = [p for p, _ in params]
        self.values = [v for _, v in params]


class Track:
    def __init__(self, name, volume=0.8, pan=0.0):
        self.name = name
        # What FL falls back to when a track is named the empty string. It
        # restores this rather than showing a blank label, so the fake keeps
        # the original around to restore too.
        self.default_name = name
        self.volume = volume
        self.pan = pan
        self.stereo_sep = 0.0
        self.muted = False
        self.solo = False
        self.armed = False
        self.selected = False
        self.enabled = True
        self.slots_enabled = True
        self.rev_polarity = False
        self.swap_channels = False
        self.color = 0x565148
        self.slots = {}  # slot index -> Plugin
        self.slot_mix = {}  # slot index -> normalized wet/dry mix
        self.eq = [
            {"gain": 0.5, "freq": 0.1, "bw": 0.5},
            {"gain": 0.5, "freq": 0.5, "bw": 0.5},
            {"gain": 0.5, "freq": 0.9, "bw": 0.5},
        ]
        # FL keeps these apart, and so does this fake. A route has to be made
        # active before it carries a level: getRouteToLevel raises "Index out
        # of range" on an inactive route rather than reporting zero.
        self.routes = {}  # dest index -> level, active routes only
        self.slot_enabled = {}  # slot index -> 1 live, 0 bypassed


class Channel:
    def __init__(self, name, target_fx=0):
        self.name = name
        self.volume = 0.78
        self.pan = 0.0
        self.pitch = 0.0
        self.pitch_range = 2.0
        self.muted = False
        self.solo = False
        self.selected = False
        self.color = 0x445566
        self.target_fx = target_fx


class Pattern:
    def __init__(self, number):
        self.number = number
        self.name = "Pattern %d" % number
        self.color = 0x334455
        self.length = 16
        self.selected = number == 1
        self.default = number >= 3


class PlaylistTrack:
    def __init__(self, index):
        self.index = index
        self.name = "Track %d" % index
        self.color = 0x223344
        self.muted = False
        self.solo = False
        self.selected = index == 1
        self.activity = 0.0


# FL drops the first plugin-parameter write after saveUndo(UF_Plugin).
SWALLOW_NEXT_PARAM_WRITE = [False]
LAST_WRITE = [None]
STALE_SENTINEL = -1.0

# Track 7 slot 2 carries a padded VST parameter map: a big reported count with
# real parameters only at these scattered indices, everything else blank
# filler. The last real one sits on the final index so a walk that stops early
# cannot silently look complete.
SPARSE_VST_COUNT = 1200
SPARSE_VST_REAL = {
    0: ("Input Gain", 0.5, "-3.0 dB"),
    # No name, but a display that means something: real, and only the display
    # says so.
    1: ("", 0.0, "Auto mode"),
    4: ("Attack", 0.6855, "20 ms"),
    60: ("Formant", 0.25, "25 %"),
    # Named, but its display is the same bare zero the padding shows: real,
    # and only the name says so.
    61: ("Throat Length", 0.0, "0.0000000"),
    517: ("Humanize", 0.1, "10 %"),
    1199: ("Bypass", 0.0, "Off"),
}

TRACKS = []
CHANNELS = []
PATTERNS = {}
PLAYLIST_TRACKS = {}
CURRENT_PATTERN = 1
UNDO = []
UNDO_POSITION = 0
PLAYING = False
RECORDING = False
METRONOME = False
PRECOUNT = False
SONG_POS = 0.0
SONG_POS_TICKS = 0
SONG_LENGTH_TICKS = 3072
CURRENT_TIME = 0
SELECTION_START = -1
SELECTION_END = -1
LOOP_MODE = 0
REC_PPQ = 96
TIME_SIGNATURE_NUMERATOR = 4
TEMPO = 140000.0
ACTIVE_MIXER_TRACK = 0
ARRANGEMENT_MARKERS = []
VISIBLE_WINDOWS = set()
RECORDED_AUTOMATION_EVENTS = []


# Parameters FL has dropped into "waiting for pickup"; see plugins.setParamValue.
PICKUP_LATCHED = {}


def reset():
    """Build a small but realistic project: a vocal chain on track 3."""
    global TRACKS, CHANNELS, PATTERNS, PLAYLIST_TRACKS, CURRENT_PATTERN
    global UNDO, UNDO_POSITION, PLAYING, RECORDING, METRONOME, PRECOUNT, SONG_POS
    global SONG_POS_TICKS, CURRENT_TIME, SELECTION_START, SELECTION_END, LOOP_MODE
    global REC_PPQ, TIME_SIGNATURE_NUMERATOR, ACTIVE_MIXER_TRACK
    global ARRANGEMENT_MARKERS, VISIBLE_WINDOWS, RECORDED_AUTOMATION_EVENTS
    UNDO = []
    UNDO_POSITION = 0
    SWALLOW_NEXT_PARAM_WRITE[0] = False
    LAST_WRITE[0] = None
    PICKUP_LATCHED.clear()
    PLAYING = False
    RECORDING = False
    METRONOME = False
    PRECOUNT = False
    SONG_POS = 0.0
    SONG_POS_TICKS = 0
    CURRENT_TIME = 0
    SELECTION_START = -1
    SELECTION_END = -1
    LOOP_MODE = 0
    REC_PPQ = 96
    TIME_SIGNATURE_NUMERATOR = 4
    ACTIVE_MIXER_TRACK = 0
    ARRANGEMENT_MARKERS = []
    VISIBLE_WINDOWS = set()
    RECORDED_AUTOMATION_EVENTS = []
    CURRENT_PATTERN = 1

    # FL never returns an empty mixer track name; empties are "Insert N".
    TRACKS = [Track("Master")] + [Track("Insert %d" % i)
                                  for i in range(1, 126)]
    TRACKS[0].slots[0] = Plugin(
        "Fruity Limiter",
        [("GAIN", 0.5), ("SAT", 0.0), ("CEIL", 0.97), ("ATT", 0.2), ("REL", 0.35)],
    )
    TRACKS[3].name = "Lead Vox"
    TRACKS[3].volume = 0.72
    TRACKS[3].slots[0] = Plugin(
        "Fruity Parametric EQ 2",
        [("Band 1 level", 0.5), ("Band 2 level", 0.5), ("Band 3 level", 0.5),
         ("Band 1 freq", 0.05), ("Band 2 freq", 0.4), ("Band 3 freq", 0.8),
         ("Band 1 width", 0.5), ("Band 2 width", 0.5), ("Band 3 width", 0.5)],
    )
    TRACKS[3].slots[1] = Plugin(
        "Fruity Compressor",
        [("Threshold", 0.6), ("Ratio", 0.3), ("Attack", 0.2),
         ("Release", 0.4), ("Gain", 0.5), ("Type", 0.0)],
    )
    TRACKS[3].slots[3] = Plugin(
        "Fruity Reeverb 2",
        [("Decay", 0.4), ("Wet", 0.15), ("Dry", 1.0), ("HighCut", 0.7)],
    )
    TRACKS[3].routes[0] = 0.8
    TRACKS[4].name = "Drums"
    # A VST reporting far more parameter slots than it has controls, most of
    # them padding, and whose first real control has no name at all -- only a
    # display string. Both shapes are common in third-party VSTs.
    vst = Plugin("Vocal Tuner VST",
                 [("", 0.0), ("Scale", 0.07), ("Key", 0.45), ("Tune Speed", 0.87)]
                 + [("", 0.0)] * 236)
    vst.displays = ["Auto mode", "Chromatic", "F", "5 ms"] + ["0.0000000"] * 236
    TRACKS[5].slots[0] = vst
    TRACKS[5].name = "Insert 5"

    # A VST3 whose padding is not all at the end. FL reports one padded maximum
    # and the real controls sit sparsely inside it: a cluster at the low
    # indices, a pocket around 60, and stragglers much further up. Anything
    # that walks this map has to judge each index on its own rather than
    # assume the first N are the real ones.
    sparse = Plugin("Sparse Param VST3", [("", 0.0)] * SPARSE_VST_COUNT)
    sparse.displays = ["0.0000000"] * SPARSE_VST_COUNT
    for idx, (pname, value, display) in SPARSE_VST_REAL.items():
        sparse.param_names[idx] = pname
        sparse.values[idx] = value
        sparse.displays[idx] = display
    TRACKS[7].name = "Harmonies"
    TRACKS[7].slots[2] = sparse

    inverted = Plugin("Fruity Delay 3", [("Feedback", 0.5), ("Time", 0.5)])
    inverted.descending = True     # display falls as the value rises
    TRACKS[6].slots[0] = inverted
    TRACKS[6].name = "Delay Bus"

    # An enumerated control, the shape a musical Key or Scale selector has:
    # the display is text, so there is no number to search on and no way to ask
    # FL what the options are. They can only be found by moving the control.
    tuner = Plugin("Pitch Tuner", [("Key", 0.0), ("Scale", 0.0), ("Mix", 0.5)])
    tuner.enums = {
        0: ["C", "C#", "D", "D#", "E", "F",
            "F#", "G", "G#", "A", "A#", "B"],
        1: ["Chromatic", "Major", "Minor"],
    }
    TRACKS[9].slots[0] = tuner
    TRACKS[9].name = "Tuner Bus"

    CHANNELS = [Channel("Vox Take 1", 3), Channel("Kick", 4), Channel("Sytrus Lead", 5)]
    CHANNELS[0].selected = True
    PATTERNS = {number: Pattern(number) for number in range(1, 5)}
    PLAYLIST_TRACKS = {index: PlaylistTrack(index) for index in range(1, 17)}


reset()
