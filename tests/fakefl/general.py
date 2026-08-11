import _state
def getVersion(): return 44
def getChangedFlag(): return 1
def getProjectTitle(): return "Synthetic Test Project"
def getProjectAuthor(): return "Test Author"
def getProjectGenre(): return "Test"
def getRecPPQ(): return _state.REC_PPQ
def getRecPPB(): return _state.REC_PPQ * 4
def saveUndo(name, flags, update=True):
    _state.UNDO.append(name)
    if flags & 4096:            # UF_Plugin
        _state.SWALLOW_NEXT_PARAM_WRITE[0] = True
def undo(): return len(_state.UNDO)
def undoUp(): return len(_state.UNDO)
def getUndoHistoryPos(): return len(_state.UNDO)
def getUndoHistoryCount(): return len(_state.UNDO)
def getUndoLevelHint(): return _state.UNDO[-1] if _state.UNDO else ""
def safeToEdit(): return True
