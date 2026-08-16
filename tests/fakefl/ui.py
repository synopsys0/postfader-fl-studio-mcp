def getVersion(mode=4): return "Producer Edition v26.1.3 [build 5336]"
def getProgTitle(): return "FL Studio 2026"
def setHintMsg(msg): pass
import _state
def isMetronomeEnabled(): return _state.METRONOME
def isPrecountEnabled(): return _state.PRECOUNT
def getHintMsg(): return ""


def showWindow(window):
    _state.VISIBLE_WINDOWS.add(window)


def getVisible(window):
    return window in _state.VISIBLE_WINDOWS
