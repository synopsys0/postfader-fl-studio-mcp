import _state


def patternNumber():
    return _state.CURRENT_PATTERN


def patternCount():
    return len(_state.PATTERNS)


def patternMax():
    return 999


def getPatternName(index):
    return _state.PATTERNS[index].name


def setPatternName(index, name):
    pattern = _state.PATTERNS.setdefault(index, _state.Pattern(index))
    pattern.name = name
    pattern.default = False


def getPatternColor(index):
    return _state.PATTERNS[index].color


def setPatternColor(index, color):
    pattern = _state.PATTERNS.setdefault(index, _state.Pattern(index))
    pattern.color = color
    pattern.default = False


def getPatternLength(index):
    return _state.PATTERNS[index].length


def setPatternLength(index, length):
    pattern = _state.PATTERNS.setdefault(index, _state.Pattern(index))
    pattern.length = length
    pattern.default = False


def jumpToPattern(index):
    _state.CURRENT_PATTERN = index


def isPatternSelected(index):
    pattern = _state.PATTERNS.get(index)
    return False if pattern is None else pattern.selected


def isPatternDefault(index):
    pattern = _state.PATTERNS.get(index)
    return True if pattern is None else pattern.default
