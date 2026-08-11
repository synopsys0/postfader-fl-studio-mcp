import _state


def currentTime(snap=0): return _state.CURRENT_TIME
def currentTimeHint(mode, time, setRecPPB=None, isLength=0): return "%d raw" % time
def selectionStart(): return _state.SELECTION_START
def selectionEnd(): return _state.SELECTION_END
