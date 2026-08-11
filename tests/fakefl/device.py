"""Fake FL device module.

`midiOutSysex` can be pointed at a real MIDI port so tests can drive the
SysEx transport over the actual IAC bus, exactly as FL Studio would.
"""

_sink = None
_out_assigned = True


def set_sink(fn):
    global _sink
    _sink = fn


def set_midi_out_assigned(value):
    global _out_assigned
    _out_assigned = value


def isAssigned():
    return True


def isMidiOutAssigned():
    return _out_assigned


def getName():
    return "Universal Bridge"


def midiOutMsg(message, channel=-1, data1=-1, data2=-1):
    pass


def midiOutSysex(message):
    if _sink is None:
        return
    _sink(message)


def dispatch(ctrlIndex, message, sysex=None):
    pass


def dispatchReceiverCount():
    return 0
