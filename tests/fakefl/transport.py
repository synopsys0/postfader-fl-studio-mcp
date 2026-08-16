import _state
import midi
def isPlaying(): return _state.PLAYING
def isRecording(): return _state.RECORDING
def start():
    _state.PLAYING = not _state.PLAYING
def stop():
    _state.PLAYING = False
def record():
    _state.RECORDING = not _state.RECORDING
def globalTransport(command, value, flags=0):
    if command == midi.FPT_Metronome:
        _state.METRONOME = not _state.METRONOME
    elif command == midi.FPT_CountDown:
        _state.PRECOUNT = not _state.PRECOUNT
    return midi.GT_Global
def getSongPos(mode=-1):
    return _state.SONG_POS_TICKS if mode == 2 else _state.SONG_POS
def setSongPos(position, mode=-1):
    _state.SONG_POS = position
def getSongPosHint(): return "1:1:00"
def getSongLength(mode):
    return _state.SONG_LENGTH_TICKS if mode == 2 else 185000
def getLoopMode(): return _state.LOOP_MODE
