import _state


def currentTime(snap=0): return _state.CURRENT_TIME
def currentTimeHint(mode, time, setRecPPB=None, isLength=0): return "%d raw" % time
def selectionStart(): return _state.SELECTION_START
def selectionEnd(): return _state.SELECTION_END


def getMarkerName(index):
    if 0 <= index < len(_state.ARRANGEMENT_MARKERS):
        return _state.ARRANGEMENT_MARKERS[index][1]
    return ""


def addAutoTimeMarker(time, name):
    _state.ARRANGEMENT_MARKERS.append((int(time), str(name)))
    _state.ARRANGEMENT_MARKERS.sort(key=lambda item: item[0])
