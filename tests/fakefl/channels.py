import _state


def _c(i):
    return _state.CHANNELS[i]


def channelCount(globalCount=False):
    return len(_state.CHANNELS)


def rerollLoopStarterLoop(index, useGlobalIndex=True):
    if not useGlobalIndex:
        raise ValueError("Loop Starter reroll requires global indexing")
    if index < 0 or index >= len(_state.CHANNELS):
        raise IndexError(index)
    # The public fake cannot model FL Cloud's selected loop identity. Record
    # the dispatch only so callers must preserve dispatch-only semantics.
    _state.CHANNELS[index].loop_starter_rerolls = (
        getattr(_state.CHANNELS[index], "loop_starter_rerolls", 0) + 1
    )


def getChannelName(index, useGlobalIndex=False):
    return _c(index).name


def setChannelName(index, name, useGlobalIndex=False):
    _c(index).name = name


def getChannelVolume(index, mode=False, useGlobalIndex=False):
    return _c(index).volume


def setChannelVolume(index, volume, pickupMode=-1, useGlobalIndex=False):
    _c(index).volume = volume


def getChannelPan(index, useGlobalIndex=False):
    return _c(index).pan


def setChannelPan(index, pan, pickupMode=-1, useGlobalIndex=False):
    _c(index).pan = pan


def getChannelPitch(index, mode=0, useGlobalIndex=False):
    channel = _c(index)
    if mode == 1:
        return channel.pitch * channel.pitch_range
    if mode == 2:
        return channel.pitch_range
    return channel.pitch


def setChannelPitch(index, value, pitchUnit=0, pickupMode=-1, useGlobalIndex=False):
    channel = _c(index)
    if pitchUnit == 2:
        channel.pitch_range = float(value)
    elif pitchUnit == 1:
        channel.pitch = float(value) / channel.pitch_range
    else:
        channel.pitch = float(value)


def isChannelMuted(index, useGlobalIndex=False):
    return _c(index).muted


def muteChannel(index, value=-1, useGlobalIndex=False):
    c = _c(index)
    c.muted = (not c.muted) if value == -1 else bool(value)


def isChannelSolo(index, useGlobalIndex=False):
    return _c(index).solo


def soloChannel(index, value=-1, useGlobalIndex=False):
    channel = _c(index)
    channel.solo = (not channel.solo) if value == -1 else bool(value)


def isChannelSelected(index, useGlobalIndex=False):
    return _c(index).selected


def getChannelType(index, useGlobalIndex=False):
    return 2


def getChannelColor(index, useGlobalIndex=False):
    return _c(index).color


def setChannelColor(index, color, useGlobalIndex=False):
    _c(index).color = color


def getTargetFxTrack(index, useGlobalIndex=False):
    return _c(index).target_fx


def setTargetFxTrack(channelIndex, mixerIndex, useGlobalIndex=False):
    _c(channelIndex).target_fx = mixerIndex


def getActivityLevel(index, useGlobalIndex=False):
    return 0.0


def selectOneChannel(index, useGlobalIndex=False):
    for c in _state.CHANNELS:
        c.selected = False
    _c(index).selected = True


def getRecEventId(index, useGlobalIndex=False):
    return int(index) * 1024
