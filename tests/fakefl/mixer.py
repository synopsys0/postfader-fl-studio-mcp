import _state


def _t(i):
    return _state.TRACKS[i]


def trackCount():
    return len(_state.TRACKS)


def getTrackName(index):
    return _t(index).name


def setTrackName(index, name):
    # An empty name is a request for the default, not for a blank label.
    track = _t(index)
    track.name = name if name else track.default_name


def getTrackVolume(index, mode=0):
    v = _t(index).volume
    if mode:
        import math

        return -200.0 if v <= 0 else round(20 * math.log10(v / 0.8), 2)
    return v


def setTrackVolume(index, volume, pickupMode=-1):
    if not 0.0 <= volume <= 1.0:
        raise ValueError("volume out of range")
    _t(index).volume = volume


def getTrackPan(index):
    return _t(index).pan


def setTrackPan(index, pan, pickupMode=-1):
    if not -1.0 <= pan <= 1.0:
        raise ValueError("pan out of range")
    _t(index).pan = pan


def getTrackStereoSep(index):
    return _t(index).stereo_sep


def setTrackStereoSep(index, pan, pickupMode=-1):
    _t(index).stereo_sep = pan


def isTrackMuted(index):
    return _t(index).muted


def muteTrack(index, value=-1):
    t = _t(index)
    t.muted = (not t.muted) if value == -1 else bool(value)


def isTrackSolo(index):
    return _t(index).solo


def soloTrack(index, value=-1, mode=-1):
    t = _t(index)
    t.solo = (not t.solo) if value == -1 else bool(value)


def isTrackArmed(index):
    return _t(index).armed


def armTrack(index):
    t = _t(index)
    t.armed = not t.armed


def isTrackSelected(index):
    return _t(index).selected


def selectTrack(index):
    _t(index).selected = not _t(index).selected


def isTrackEnabled(index):
    return _t(index).enabled


def enableTrack(index):
    _t(index).enabled = not _t(index).enabled


def isTrackSlotsEnabled(index):
    return _t(index).slots_enabled


def isTrackRevPolarity(index):
    return _t(index).rev_polarity


def isTrackSwapChannels(index):
    return _t(index).swap_channels


def getTrackColor(index):
    return _t(index).color


def setTrackColor(index, color):
    _t(index).color = color


def getTrackPeaks(index, mode):
    return 0.42 if _t(index).slots else 0.0


def isTrackPluginValid(index, plugIndex):
    return plugIndex in _t(index).slots


def getPluginMixLevel(index, plugIndex):
    # Answers for an empty slot too, exactly as FL does: neither this nor
    # getPluginMuteState can be used to find out whether a plug-in is there.
    return _t(index).slot_mix.get(plugIndex, 1.0)


def setPluginMixLevel(index, plugIndex, level):
    """Accepted and ignored, exactly as FL 2026 does.

    Verified live against every plug-in in a real project, native and VST3
    alike, across 0, 0.25, 0.5, 64 and 128: the getter never budged off 1.0.
    Together with the one-way mute above it means a script cannot bypass or
    blend an individual effect slot at all, so the fake refuses to certify a
    command the device would ignore.
    """
    return


def getPluginMuteState(index, plugIndex):
    # 1 is live and 0 is bypassed -- inverted from what the name suggests.
    return _t(index).slot_enabled.get(plugIndex, 1)


def setPluginMuteState(index, plugIndex, value):
    """One-way on the real host, and one-way here.

    FL 2026 accepts a mute and silently ignores every attempt to un-mute, so
    a bypass made this way can only be undone by hand. Reproducing the defect
    keeps the fake from certifying a command the device would not honour.
    There is no scripted way back; the slot has to be re-enabled by hand.
    """
    if not value:
        _t(index).slot_enabled[plugIndex] = 0


def getEqBandCount():
    return 3


def getEqGain(index, band, mode=0):
    g = _t(index).eq[band]["gain"]
    return round((g - 0.5) * 36.0, 2) if mode else g


def setEqGain(index, band, value):
    _t(index).eq[band]["gain"] = value


def getEqFrequency(index, band, mode=0):
    f = _t(index).eq[band]["freq"]
    return round(20 * (1000 ** f), 1) if mode else f


def setEqFrequency(index, band, value):
    _t(index).eq[band]["freq"] = value


def getEqBandwidth(index, band):
    return _t(index).eq[band]["bw"]


def setEqBandwidth(index, band, value):
    _t(index).eq[band]["bw"] = value


def getRouteSendActive(index, destIndex):
    return 1 if destIndex in _t(index).routes else 0


def setRouteTo(index, destIndex, value):
    """Create or tear down a send. Matches FL: a torn-down route keeps no level."""
    routes = _t(index).routes
    if value:
        routes.setdefault(destIndex, 0.8)
    else:
        routes.pop(destIndex, None)


def getRouteToLevel(index, destIndex):
    # FL raises here for an inactive route instead of answering zero. The fake
    # raises too, so a caller that forgets to create the route first fails the
    # same way against both.
    routes = _t(index).routes
    if destIndex not in routes:
        raise RuntimeError("Index out of range")
    return routes[destIndex]


def setRouteToLevel(index, destIndex, level):
    routes = _t(index).routes
    if destIndex not in routes:
        raise RuntimeError("Index out of range")
    routes[destIndex] = level


def afterRoutingChanged():
    pass


def getCurrentTempo(asInt=False):
    return int(_state.TEMPO) if asInt else _state.TEMPO


def trackNumber():
    return _state.ACTIVE_MIXER_TRACK


def setTrackNumber(trackNumber, flags=0):
    _state.ACTIVE_MIXER_TRACK = trackNumber


def setActiveTrack(trackNumber):
    _state.ACTIVE_MIXER_TRACK = trackNumber
    for index, track in enumerate(_state.TRACKS):
        track.selected = index == trackNumber


def getTrackPluginId(index, plugIndex):
    return int(index) * 1024 + int(plugIndex) * 64


def getTrackInfo(mode):
    import midi

    if mode == midi.TN_Master:
        return 0
    if mode == midi.TN_FirstIns:
        return 1
    if mode == midi.TN_LastIns:
        return len(_state.TRACKS) - 1
    if mode == midi.TN_Sel:
        return 0
    raise ValueError("unknown track-info mode")
