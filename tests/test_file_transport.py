"""Exercise the mailbox fallback used by deterministic transport tests.

Production FL Studio uses the MIDI SysEx transport. This suite forces TCP off
and drives the file-mailbox fallback through the same BridgeClient the MCP
server uses, preserving coverage for its request/reply framing and cleanup.
"""

import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "fakefl"))
sys.path.insert(0, os.path.join(ROOT, "bridge"))
sys.path.insert(0, ROOT)

# Exercise one narrow verified write after the read-only round trips.
os.environ["FL_BRIDGE_ENABLE_WRITES"] = "1"

import _state  # noqa: E402
import device_UniversalBridge as bridge  # noqa: E402

PASS = 0
FAIL = 0
MAILBOX = tempfile.mkdtemp(prefix="flmcp-mailbox-")


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s  %s" % (label, detail))


def force_file_transport():
    """Start the bridge with TCP made to fail, as it does inside FL."""
    bridge.MAILBOX = MAILBOX  # pin the bridge to one directory

    class Unusable:
        name = "tcp"

        def start(self):
            raise SystemError(
                "<slot wrapper '__init__' of '_socket.socket' objects> "
                "returned NULL without setting an exception")

    real_socket_transport = bridge._SocketTransport
    bridge._SocketTransport = Unusable
    try:
        bridge.OnInit()
    finally:
        bridge._SocketTransport = real_socket_transport


def main():
    global FAIL
    _state.reset()

    print("\n-- transport selection --")
    force_file_transport()
    check("fell back to the file transport",
          bridge._transport.name == "files", bridge._transport.name)
    check("mailbox directory used as-is", os.path.isdir(MAILBOX), MAILBOX)
    check("liveness marker written",
          os.path.isfile(os.path.join(MAILBOX, bridge.ALIVE_NAME)),
          os.listdir(MAILBOX))

    stop = threading.Event()

    def pump():
        while not stop.is_set():
            bridge.OnIdle()
            time.sleep(0.004)

    t = threading.Thread(target=pump, daemon=True)
    t.start()

    from fl_studio_mcp.bridge_client import BridgeClient, BridgeError

    client = BridgeClient(port=1, mailbox=MAILBOX, timeout=15,
                           midi_port="no-such-midi-port")

    print("\n-- client picks the mailbox when TCP is dead --")
    info = client.ping()
    check("ping answered", info.get("pong") is True, info)
    check("client reports the file transport", client.transport == "files",
          client.transport)

    print("\n-- commands round trip --")
    proj = client.call("project.info")
    check("project.info", proj["tempo_bpm"] == 140.0, proj)
    mix = client.call("mixer.list")
    check("mixer.list", mix["track_count"] == 126, mix.get("track_count"))
    vox = [t for t in mix["tracks"] if t["index"] == 3][0]
    check("plugins visible", len(vox["plugins"]) == 3, vox["plugins"])

    print("\n-- chunked job over files --")
    t0 = time.time()
    full = client.call("mixer.list", only_used=False)
    elapsed = time.time() - t0
    check("full scan completed", len(full["tracks"]) == 126, len(full["tracks"]))
    check("scan finished promptly (%.2fs)" % elapsed, elapsed < 10, elapsed)

    print("\n-- large payload survives the file round trip --")
    big = client.call("mixer.list", only_used=False, peaks=True)
    check("peaks included", big["tracks"][0].get("peak_l") is not None,
          big["tracks"][0])
    check("payload intact", all("plugins" in t for t in big["tracks"]))

    print("\n-- writes --")
    changed = client.call("mixer.set_volume", track=3, value=0.6)
    check("verified write applied",
          changed["verified"] is True
          and abs(_state.TRACKS[3].volume - 0.6) < 1e-9,
          changed)

    print("\n-- errors propagate --")
    try:
        client.call("plugin.set_param", track=3, slot=1,
                    index=999, value=0.5)
        check("bad parameter raises", False, "no exception")
    except BridgeError as e:
        check("bad parameter raises", "outside" in str(e), str(e)[:80])
    try:
        client.call("no.such.command")
        check("unknown command raises", False, "no exception")
    except BridgeError as e:
        check("unknown command lists options", "available commands" in str(e),
              str(e)[:80])

    print("\n-- mailbox hygiene --")
    # The liveness marker is meant to persist; nothing else should.
    #
    # The pump thread is still running and still refreshing that marker, and
    # _write_atomic legitimately creates PREFIX + "tmp-" + name before renaming
    # it into place. A single listdir can catch that rename window and see a
    # file that is transient by design, which fails here on a loaded machine
    # for no real reason. Poll instead: anything genuinely stranded is still
    # present a moment later, while the temp file is not.
    deadline = time.time() + 2
    while True:
        leftovers = [n for n in os.listdir(MAILBOX)
                     if n.startswith(bridge.PREFIX) and n != bridge.ALIVE_NAME]
        if not leftovers or time.time() >= deadline:
            break
        time.sleep(0.01)
    check("no request or reply files left behind", not leftovers, leftovers)

    print("\n-- concurrent clients --")
    other = BridgeClient(port=1, mailbox=MAILBOX, timeout=15,
                           midi_port="no-such-midi-port")
    results = {}

    def worker(name, cl, cmd, kwargs):
        try:
            results[name] = cl.call(cmd, **kwargs)
        except Exception as e:  # noqa: BLE001
            results[name] = e

    threads = [
        threading.Thread(target=worker,
                         args=("scan", client, "mixer.list", {"only_used": False})),
        threading.Thread(target=worker,
                         args=("ping", other, "ping", {})),
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=20)
    check("long scan answered", isinstance(results.get("scan"), dict)
          and len(results["scan"]["tracks"]) == 126, results.get("scan"))
    check("second client answered concurrently",
          isinstance(results.get("ping"), dict)
          and results["ping"].get("pong"), results.get("ping"))

    print("\n-- staleness detection --")
    stop.set()
    t.join(timeout=2)
    bridge.OnDeInit()
    check("liveness marker removed on shutdown",
          not os.path.isfile(os.path.join(MAILBOX, bridge.ALIVE_NAME)),
          os.listdir(MAILBOX))
    dead = BridgeClient(port=1, mailbox=MAILBOX, timeout=2,
                         midi_port="no-such-midi-port")
    try:
        dead.ping()
        check("dead bridge reports clearly", False, "ping unexpectedly succeeded")
    except BridgeError as e:
        check("dead bridge reports clearly", "Could not reach" in str(e), str(e)[:60])

    shutil.rmtree(MAILBOX, ignore_errors=True)
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
