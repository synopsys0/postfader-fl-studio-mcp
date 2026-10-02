"""Reporting shared by the script-style test files.

Those files drive one long fake-FL scenario and print an ``ok`` or ``FAIL``
line for every assertion. ``section()`` starts a named scenario and
``check()`` records an assertion inside it. ``summary()`` reports one result
per scenario, so ``scripts/run_safe_tests.py`` counts these files on the same
footing as unittest files instead of counting every assertion as a test.
"""

_sections = []


def section(name):
    print("\n-- %s --" % name)
    _sections.append([name, 0])


def check(label, cond, detail=""):
    if not _sections:
        _sections.append(["setup", 0])
    if cond:
        print("  ok   %s" % label)
    else:
        _sections[-1][1] += 1
        print("  FAIL %s  %s" % (label, detail))


def summary():
    failed = [name for name, failures in _sections if failures]
    for name in failed:
        print("FAILED SCENARIO: %s" % name)
    print("\n%d passed, %d failed" % (len(_sections) - len(failed), len(failed)))
    return 1 if failed else 0
