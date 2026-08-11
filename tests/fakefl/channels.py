import _state


def _c(i):
    return _state.CHANNELS[i]


def channelCount(globalCount=False):
    return len(_state.CHANNELS)


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


def isChannelMuted(index, useGlobalIndex=False):
    return _c(index).muted


def muteChannel(index, value=-1, useGlobalIndex=False):
    c = _c(index)
    c.muted = (not c.muted) if value == -1 else bool(value)


def isChannelSolo(index, useGlobalIndex=False):
    return _c(index).solo


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
