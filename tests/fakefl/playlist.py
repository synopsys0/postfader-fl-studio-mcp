import _state


def _t(index):
    return _state.PLAYLIST_TRACKS[index]


def trackCount():
    return len(_state.PLAYLIST_TRACKS)


def getTrackName(index):
    return _t(index).name


def setTrackName(index, name):
    _t(index).name = name


def getTrackColor(index):
    return _t(index).color


def setTrackColor(index, color):
    _t(index).color = color


def isTrackMuted(index):
    return _t(index).muted


def muteTrack(index, value=-1, inGroup=0):
    track = _t(index)
    track.muted = (not track.muted) if value == -1 else bool(value)


def isTrackSolo(index):
    return _t(index).solo


def soloTrack(index, value=-1, inGroup=0):
    track = _t(index)
    track.solo = (not track.solo) if value == -1 else bool(value)


def isTrackSelected(index):
    return _t(index).selected


def selectTrack(index):
    _t(index).selected = not _t(index).selected


def getTrackActivityLevel(index):
    return _t(index).activity
