"""Drive the SysEx transport over the real IAC bus.

FL Studio sandboxes its script interpreter: sockets fail and every filesystem
write fails, both returning NULL with no exception set. MIDI is the only
channel left, so this is the transport that actually carries traffic in
practice and it needs the same scrutiny as the others.

The bridge runs here with its socket and file transports forced to fail, its
`device.midiOutSysex` wired to a real MIDI port, and its `OnSysEx` fed from a
real MIDI input - the same shape as FL, minus FL.
"""

import sys
import os
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "fakefl"))
sys.path.insert(0, os.path.join(ROOT, "fl_studio_mcp", "_bridge"))
sys.path.insert(0, ROOT)

# Exercise the verified write framing as well as read-only traffic.
os.environ["FL_BRIDGE_ENABLE_WRITES"] = "1"

import _state  # noqa: E402
import device as fake_device  # noqa: E402
import device_UniversalBridge as bridge  # noqa: E402

PASS = 0
FAIL = 0
PORT_HINT = "IAC Driver"


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s  %s" % (label, detail))


class Msg:
    """Stand-in for FlMidiMsg, which only needs .sysex and .handled here."""

    def __init__(self, data):
        self.sysex = bytes(data)
        self.handled = False


def find_port(collection):
    ports = collection.get_ports()
    for i, name in enumerate(ports):
        if PORT_HINT.lower() in name.lower():
            return i, ports
    return None, ports


def main():
    global FAIL
    try:
        import rtmidi
    except ImportError:
        print("python-rtmidi not installed; skipping")
        return 0

    fl_out, fl_in = rtmidi.MidiOut(), rtmidi.MidiIn()
    oi, out_names = find_port(fl_out)
    ii, in_names = find_port(fl_in)
    if oi is None or ii is None:
        print("No IAC Driver port found (out=%s in=%s); skipping."
              % (out_names, in_names))
        print("Enable it in Audio MIDI Setup to run this suite.")
        return 0

    fl_out.open_port(oi)
    fl_in.open_port(ii)
    fl_in.ignore_types(sysex=False, timing=True, active_sense=True)
    fake_device.set_sink(lambda payload: fl_out.send_message(list(payload)))

    _state.reset()

    print("\n-- transport selection --")

    class Dead:
        def __init__(self, name):
            self.name = name

        def start(self):
            raise IOError("blocked by FL's sandbox")

    real_sock, real_file = bridge._SocketTransport, bridge._FileTransport
    bridge._SocketTransport = lambda: Dead("tcp")
    bridge._FileTransport = lambda: Dead("files")
    try:
        bridge.OnInit()
    finally:
        bridge._SocketTransport, bridge._FileTransport = real_sock, real_file

    check("falls through to MIDI when sockets and files are blocked",
          bridge._transport.name == "midi", bridge._transport.name)

    stop = threading.Event()

    def pump():
        """Stand in for FL: deliver SysEx and run idle ticks."""
        while not stop.is_set():
            while True:
                m = fl_in.get_message()
                if not m:
                    break
                bridge.OnSysEx(Msg(m[0]))
            bridge.OnIdle()
            time.sleep(0.002)

    t = threading.Thread(target=pump, daemon=True)
    t.start()

    from fl_studio_mcp.bridge_client import BridgeClient, BridgeError

    client = BridgeClient(port=1, mailbox="/nonexistent-flmcp", timeout=25)

    print("\n-- handshake over SysEx --")
    info = client.ping()
    check("ping answered", info.get("pong") is True, info)
    check("client selected the MIDI transport", client.transport == "midi",
          client.transport)

    print("\n-- commands round trip --")
    proj = client.call("project.info")
    check("project.info", proj["tempo_bpm"] == 140.0, proj)
    mix = client.call("mixer.list")
    check("mixer.list", mix["track_count"] == 126, mix.get("track_count"))
    vox = [t for t in mix["tracks"] if t["index"] == 3][0]
    check("plugin detail intact", len(vox["plugins"]) == 3, vox["plugins"])
    check("names survived the 7-bit encoding", vox["name"] == "Lead Vox",
          vox["name"])

    print("\n-- multi-frame payload (chunked reassembly) --")
    t0 = time.time()
    full = client.call("mixer.list", only_used=False)
    elapsed = time.time() - t0
    check("full 126-track scan returned", len(full["tracks"]) == 126,
          len(full["tracks"]))
    check("every track has its plugin list",
          all("plugins" in t for t in full["tracks"]))
    payload_kb = len(str(full)) / 1024.0
    print("     %.0f KB across ~%d SysEx frames in %.2fs"
          % (payload_kb, payload_kb * 1024 / bridge.SYSEX_CHUNK + 1, elapsed))
    check("large transfer completed in reasonable time", elapsed < 25, elapsed)

    print("\n-- writes --")
    r = client.call("mixer.set_volume", track=3, value=0.55)
    check("verified mixer write applied",
          r["verified"] is True
          and abs(_state.TRACKS[3].volume - 0.55) < 1e-9,
          r)
    r = client.call("plugin.set_param", track=3, slot=1,
                    index=1, value=0.3)
    check("verified plugin write applied",
          r["verified"] is True and r["name"] == "Ratio", r)

    print("\n-- errors propagate --")
    try:
        client.call("plugin.set_param", track=3, slot=1,
                    index=999, value=0.5)
        check("bad parameter raises", False, "no exception")
    except BridgeError as e:
        check("bad parameter raises", "outside" in str(e),
              str(e)[:70])

    print("\n-- request ordering under load --")
    ids = []
    for i in range(6):
        got = client.call("mixer.track", track=3)
        ids.append(got["index"])
    check("six sequential replies all matched their request",
          ids == [3] * 6, ids)

    print("\n-- echo rejection --")
    # Both sides subscribe to the same bus, so each must ignore its own tag.
    check("bridge ignored its own responses",
          bridge._transport.partial == {}, bridge._transport.partial)
    check("client left no half-assembled messages",
          client._active.partial == {}, client._active.partial)

    stop.set()
    t.join(timeout=3)
    bridge.OnDeInit()
    fake_device.set_sink(None)
    fl_in.close_port()
    fl_out.close_port()

    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
