import _state


def _get(index, slotIndex):
    if slotIndex is None or slotIndex < 0:
        # channel rack generator
        if index >= len(_state.CHANNELS):
            return None
        name = _state.CHANNELS[index].name
        return _state.Plugin(name, [("Volume", 0.8), ("Pan", 0.5)])
    if index >= len(_state.TRACKS):
        return None
    return _state.TRACKS[index].slots.get(slotIndex)


def isValid(index, slotIndex=-1, useGlobalIndex=False):
    return _get(index, slotIndex) is not None


def getPluginName(index, slotIndex=-1, userName=False, useGlobalIndex=False):
    p = _get(index, slotIndex)
    if p is None:
        raise TypeError("no plugin")
    return p.name


def getParamCount(index, slotIndex=-1, useGlobalIndex=False):
    p = _get(index, slotIndex)
    if p is None:
        raise TypeError("no plugin")
    return len(p.param_names)


def getParamName(paramIndex, index, slotIndex=-1, useGlobalIndex=False):
    p = _get(index, slotIndex)
    if p is None:
        raise TypeError("no plugin")
    return p.param_names[paramIndex]


def getParamValue(paramIndex, index, slotIndex=-1, useGlobalIndex=False):
    p = _get(index, slotIndex)
    if p is None:
        raise TypeError("no plugin")
    reported = getattr(p, "reported", {})
    if paramIndex in reported:
        return reported[paramIndex]       # one step behind, as FL is
    return p.values[paramIndex]


# midi.PIM_None. Anything else leaves FL free to engage pickup.
_PIM_NONE = 0


def setParamValue(value, paramIndex, index, slotIndex=-1, pickupMode=1,
                  useGlobalIndex=False):
    """Write a parameter, reproducing FL's pickup trap.

    FL's default pickup behaviour can put a control into "waiting for pickup"
    after repeated writes, and from then on it silently refuses everything --
    including the write that would put the control back. Verified live against
    a third-party VST: a display-value search left one control latched at the
    wrong setting until the same round trip was repeated with pickup off.

    So a caller that omits pickupMode gets exactly two writes before this
    parameter latches, and only PIM_None keeps it writable. That makes the
    trap fail a test here instead of stranding a control in a real project.
    """
    p = _get(index, slotIndex)
    if p is None:
        raise TypeError("no plugin")
    if not 0.0 <= value <= 1.0:
        raise ValueError("value out of range")
    latch = _state.PICKUP_LATCHED
    pin = (index, slotIndex, paramIndex)
    if pickupMode == _PIM_NONE:
        latch.pop(pin, None)              # pickup off; never latches
    else:
        latch[pin] = latch.get(pin, 0) + 1
        if latch[pin] > 2:
            return 1                      # accepted, ignored, no complaint
    # Real FL ignores a lone write in an idle tick; repeating it makes it
    # take. Track consecutive writes to the same parameter to mimic that.
    key = (index, slotIndex, paramIndex)
    if _state.LAST_WRITE[0] != key:
        _state.LAST_WRITE[0] = key
        return 1
    # getParamValue reports the value from before this write, as FL does.
    p.reported = dict(getattr(p, "reported", {}))
    p.reported[paramIndex] = p.values[paramIndex]
    p.values[paramIndex] = value
    return 1


def getParamValueString(paramIndex, index, slotIndex=-1, pickupMode=-1,
                        useGlobalIndex=False):
    p = _get(index, slotIndex)
    if p is None:
        raise TypeError("no plugin")
    enums = getattr(p, "enums", None)
    if enums is not None and paramIndex in enums:
        # An enumerated control: contiguous stretches of 0..1 each show one
        # option's text, which is the only way FL exposes the option list.
        choices = enums[paramIndex]
        slot_index = int(p.values[paramIndex] * len(choices))
        return choices[min(slot_index, len(choices) - 1)]
    displays = getattr(p, "displays", None)
    if displays is not None:
        return displays[paramIndex]
    value = p.values[paramIndex]
    if getattr(p, "descending", False):
        return "%.1f ms" % (1000.0 - value * 1000.0)
    return "%.1f %%" % (value * 100)


def getPresetCount(index, slotIndex=-1, useGlobalIndex=False):
    return 12


def nextPreset(index, slotIndex=-1, useGlobalIndex=False):
    pass


def prevPreset(index, slotIndex=-1, useGlobalIndex=False):
    pass


def getColor(index, slotIndex=-1, flag=0, useGlobalIndex=False):
    return 0x112233


def getName(index, slotIndex=-1, flag=0, paramIndex=0, useGlobalIndex=False):
    return getPluginName(index, slotIndex)
