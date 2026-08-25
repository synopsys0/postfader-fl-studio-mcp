import _state
def getVersion(): return 44
def getChangedFlag(): return 1
def getProjectTitle(): return "Synthetic Test Project"
def getProjectAuthor(): return "Test Author"
def getProjectGenre(): return "Test"
def getRecPPQ(): return _state.REC_PPQ
def getRecPPB(): return _state.REC_PPQ * _state.TIME_SIGNATURE_NUMERATOR
def setNumerator(value):
    _state.TIME_SIGNATURE_NUMERATOR = value
def saveUndo(name, flags, update=True):
    if _state.UNDO_POSITION < len(_state.UNDO):
        del _state.UNDO[_state.UNDO_POSITION:]
    _state.UNDO.append(name)
    _state.UNDO_POSITION = len(_state.UNDO)
    if flags & 4096:            # UF_Plugin
        _state.SWALLOW_NEXT_PARAM_WRITE[0] = True
def undo():
    if _state.UNDO_POSITION == len(_state.UNDO):
        _state.UNDO_POSITION = max(0, _state.UNDO_POSITION - 1)
    else:
        _state.UNDO_POSITION = min(len(_state.UNDO), _state.UNDO_POSITION + 1)
    return _state.UNDO_POSITION
def undoUp():
    _state.UNDO_POSITION = max(0, _state.UNDO_POSITION - 1)
    return _state.UNDO_POSITION
def undoDown():
    _state.UNDO_POSITION = min(len(_state.UNDO), _state.UNDO_POSITION + 1)
    return _state.UNDO_POSITION
def setUndoHistoryPos(value):
    _state.UNDO_POSITION = max(0, min(len(_state.UNDO), value))
def getUndoHistoryPos(): return _state.UNDO_POSITION
def getUndoHistoryCount(): return len(_state.UNDO)
def getUndoHistoryLast(): return len(_state.UNDO)
def getUndoLevelHint():
    if not _state.UNDO or _state.UNDO_POSITION == 0:
        return ""
    return _state.UNDO[_state.UNDO_POSITION - 1]
def getUseMetronome(): return _state.METRONOME
def getPrecount(): return int(_state.PRECOUNT)
def safeToEdit(): return True


def processRECEvent(event_id, value, flags):
    import midi

    normalized = max(0.0, min(1.0, float(value) / midi.FromMIDI_Max))
    for index, track in enumerate(_state.TRACKS):
        plugin_id = index * 1024
        if event_id == midi.REC_Mixer_Vol + plugin_id:
            track.volume = normalized
            target = ("mixer", index, "volume")
            break
        if event_id == midi.REC_Mixer_Pan + plugin_id:
            track.pan = normalized * 2.0 - 1.0
            target = ("mixer", index, "pan")
            break
        if event_id == midi.REC_Mixer_SS + plugin_id:
            track.stereo_sep = normalized * 2.0 - 1.0
            target = ("mixer", index, "stereo_separation")
            break
    else:
        target = None
        for index, channel in enumerate(_state.CHANNELS):
            rec_id = index * 1024
            if event_id == midi.REC_Chan_Vol + rec_id:
                channel.volume = normalized
                target = ("channel", index, "volume")
                break
            if event_id == midi.REC_Chan_Pan + rec_id:
                channel.pan = normalized * 2.0 - 1.0
                target = ("channel", index, "pan")
                break
    if target is None:
        raise ValueError("unknown fake REC event")
    _state.RECORDED_AUTOMATION_EVENTS.append(
        {
            "target": target,
            "normalized": normalized,
            "flags": flags,
            "song_position_ticks": _state.SONG_POS_TICKS,
        }
    )
    return int(value)
