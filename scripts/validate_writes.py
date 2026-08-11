#!/usr/bin/env python3
"""Prove the five core normalized writes work against your live project.

Everything is captured first, changed, and put back, and every step is judged
by reading FL back rather than by what a setter returned. Nothing is left
changed, and the project is never saved.

By default this only touches an unused mixer track: one with no plug-ins, at
unity gain and centre pan. Nothing in your mix is written to unless you name a
plug-in parameter explicitly:

    ./.venv/bin/python scripts/validate_writes.py
    ./.venv/bin/python scripts/validate_writes.py --track 3 --slot 3 --index 2

FL Studio must have been launched with FL_BRIDGE_ENABLE_WRITES=1, since the
bridge reads that flag once at script load:

    FL_BRIDGE_ENABLE_WRITES=1 open -a "FL Studio 2026"
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from fl_studio_mcp.bridge_client import BridgeClient, BridgeError  # noqa: E402

PASS = 0
FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s  %s" % (label, detail))
    return bool(cond)


def rule(title):
    print("\n" + "=" * 66 + "\n" + title + "\n" + "=" * 66)


def find_scratch_track(client) -> int | None:
    """An untouched track: no plug-ins, unity gain, centred."""
    full = client.call("mixer.list", only_used=False)
    for track in reversed(full["tracks"]):
        if (
            track["index"] > 4
            and not track.get("plugins")
            and abs((track.get("volume") or 0) - 0.8) < 1e-6
            and abs(track.get("pan") or 0) < 1e-6
        ):
            return track["index"]
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exercise the verified write commands and restore everything."
    )
    parser.add_argument(
        "--track",
        type=int,
        help="Mixer track carrying the plug-in to test. Requires --slot and --index.",
    )
    parser.add_argument("--slot", type=int, help="Effect slot on that track.")
    parser.add_argument(
        "--index", type=int, help="Parameter index on that plug-in to move and restore."
    )
    args = parser.parse_args()
    named = [value is not None for value in (args.track, args.slot, args.index)]
    if any(named) and not all(named):
        parser.error("--track, --slot and --index must be given together")
    if args.track == 0:
        parser.error("refusing to target the master bus")
    return args


def main() -> int:
    args = parse_args()
    client = BridgeClient(timeout=60)
    try:
        pong = client.ping()
    except BridgeError as error:
        print(error)
        return 1
    print("connected over %s" % client.transport)

    if not pong.get("verified_writes_enabled"):
        print(
            "\nThe bridge reports bridge_mode=%r and no verified write surface.\n"
            "Relaunch FL Studio with FL_BRIDGE_ENABLE_WRITES=1 and try again."
            % pong.get("bridge_mode")
        )
        return 1

    undo_before = client.call("project.info").get("undo_history_position")

    rule("1. an unused mixer track: fader, pan, mute")
    track = find_scratch_track(client)
    if track is None:
        print("  No untouched track found; stopping before touching anything real.")
        return 1
    print("  using mixer track %d (no plug-ins, unity gain, centred)" % track)

    before = client.call("mixer.track", track=track)
    original_volume, original_pan = before["volume"], before["pan"]

    moved = client.call("mixer.set_volume", track=track, value=0.5)
    landed = check(
        "fader write verified by readback",
        moved["verified"] and abs(moved["after"] - 0.5) < 1e-4,
        moved,
    )
    back = client.call("mixer.set_volume", track=track, value=original_volume)
    check(
        "fader restored exactly",
        back["verified"] and abs(back["after"] - original_volume) < 1e-6,
        (original_volume, back["after"]),
    )
    if not landed:
        print("\n  Writes are not landing. Stopping before touching anything else.")
        return 1

    moved = client.call("mixer.set_pan", track=track, value=-0.35)
    check(
        "pan write verified by readback",
        moved["verified"] and abs(moved["after"] + 0.35) < 1e-3,
        moved,
    )
    back = client.call("mixer.set_pan", track=track, value=original_pan)
    check(
        "pan restored exactly",
        back["verified"] and abs(back["after"] - original_pan) < 1e-6,
        (original_pan, back["after"]),
    )

    original_mute = bool(before.get("muted"))
    moved = client.call("mixer.set_mute", track=track, muted=not original_mute)
    check(
        "mute write verified by readback",
        moved["verified"] and moved["after"] is (not original_mute),
        moved,
    )
    back = client.call("mixer.set_mute", track=track, muted=original_mute)
    check(
        "mute restored exactly",
        back["verified"] and back["after"] is original_mute,
        (original_mute, back["after"]),
    )

    rule("2. built-in EQ on the same unused track")
    eq_before = client.call("mixer.set_eq", track=track, band=1, gain=0.6)
    check(
        "EQ gain write verified by readback",
        eq_before["verified"],
        eq_before,
    )
    print(
        "       band 1 gain now %s (%s dB)"
        % (eq_before["after"]["gain"], eq_before["after"]["gain_db"])
    )
    original_gain = eq_before["before"]["gain"]
    eq_back = client.call("mixer.set_eq", track=track, band=1, gain=original_gain)
    check(
        "EQ gain restored exactly",
        eq_back["verified"]
        and abs((eq_back["after"]["gain"] or 0) - (original_gain or 0)) < 1e-6,
        (original_gain, eq_back["after"]["gain"]),
    )

    rule("3. master is refused unless it is asked for by name")
    try:
        client.call("mixer.set_volume", track=0, value=0.5)
        check("master refused without allow_master", False, "the write was accepted")
    except BridgeError as error:
        check("master refused without allow_master", "master" in str(error), error)

    if args.track is None:
        rule("4. one plug-in parameter - skipped")
        print(
            "  Pass --track/--slot/--index to move and restore one real parameter.\n"
            "  Nothing in your mix has been written to."
        )
    else:
        rule("4. one plug-in parameter")
        page = client.call(
            "plugin.params", track=args.track, slot=args.slot, limit=1,
            offset=args.index,
        )
        params = page.get("params") or []
        if not params:
            print(
                "  No parameter at track %d slot %d index %d on %r; skipping."
                % (args.track, args.slot, args.index, page.get("plugin"))
            )
            return 1 if FAIL else 0
        param = params[0]
        original_value, original_display = param["value"], param["display"]
        print(
            "  %s parameter %d (%s) currently %.4f  (%s)"
            % (
                page.get("plugin"),
                args.index,
                param.get("name"),
                original_value,
                original_display,
            )
        )

        target = 0.5 if abs(original_value - 0.5) > 0.05 else 0.2
        moved = client.call(
            "plugin.set_param",
            track=args.track,
            slot=args.slot,
            index=args.index,
            value=target,
        )
        check("plug-in parameter write verified", moved["verified"], moved)
        print(
            "       moved to %.4f  (%s)"
            % (moved["after"]["value"], moved["after"]["display"])
        )

        back = client.call(
            "plugin.set_param",
            track=args.track,
            slot=args.slot,
            index=args.index,
            value=original_value,
        )
        check("parameter restore verified", back["verified"], back)
        now = client.call(
            "plugin.params", track=args.track, slot=args.slot, limit=1,
            offset=args.index,
        )["params"][0]
        check(
            "parameter restored - display identical to before",
            now["display"] == original_display,
            (original_display, now["display"]),
        )

    rule("undo trail")
    undo_after = client.call("project.info")
    print(
        "  undo history position moved %s -> %s"
        % (undo_before, undo_after.get("undo_history_position"))
    )
    check(
        "writes recorded undo points",
        (undo_after.get("undo_history_position") or 0) != (undo_before or 0),
        (undo_before, undo_after.get("undo_history_position")),
    )

    print("\n%d passed, %d failed" % (PASS, FAIL))
    if FAIL:
        print(
            "SOME CHECKS FAILED - a value may have been left changed. Read the "
            "restore lines above before trusting the project."
        )
    else:
        print(
            "Nothing was left changed: every restore was verified by reading the "
            "value back. The project was not saved."
        )
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
