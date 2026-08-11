"""Guard the per-tick cost of bridge commands.

FL calls OnIdle on its main thread, the same thread running the UI and MIDI,
roughly every 20 ms. A command that makes thousands of API calls inside one
callback overruns that budget and shows up as UI stutter or MIDI lag. These
tests count real FL API calls per tick and hold the worst case down.
"""

import collections
import json
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "fakefl"))
sys.path.insert(0, os.path.join(ROOT, "fl_studio_mcp", "_bridge"))

import _state  # noqa: E402
import mixer  # noqa: E402
import plugins  # noqa: E402
import channels  # noqa: E402
import arrangement  # noqa: E402
import general  # noqa: E402
import device_UniversalBridge as bridge  # noqa: E402

# Keep bounded-load tests off the production loopback port.  Every client in
# this process uses the exact ephemeral listener selected below.
bridge.PORT = 0

PASS = 0
FAIL = 0
CALLS = collections.Counter()

# A single OnIdle callback should stay well inside FL's ~20 ms budget. This
# ceiling is deliberately far below the ~3000 calls an unchunked full-mixer
# scan used to make in one go.
MAX_CALLS_PER_TICK = 400


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s  %s" % (label, detail))


def instrument():
    """Count every call into the fake FL API modules."""
    for mod, modname in (
        (mixer, "mixer"),
        (plugins, "plugins"),
        (channels, "channels"),
        (arrangement, "arrangement"),
        (general, "general"),
    ):
        for name in dir(mod):
            if name.startswith("_"):
                continue
            fn = getattr(mod, name)
            if not callable(fn):
                continue

            def make(fn=fn, key=f"{modname}.{name}"):
                def inner(*a, **k):
                    CALLS[key] += 1
                    return fn(*a, **k)
                return inner
            setattr(mod, name, make())


class Client:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        self.sock.setblocking(False)
        self.buf = b""
        self.n = 0

    def call(self, cmd, **args):
        """Send a command, pumping OnIdle and recording per-tick call counts."""
        self.n += 1
        self.sock.sendall(
            (json.dumps({"id": self.n, "cmd": cmd, "args": args}) + "\n").encode())
        per_tick = []
        deadline = time.time() + 5
        while time.time() < deadline:
            CALLS.clear()
            bridge.OnIdle()
            per_tick.append(sum(CALLS.values()))
            try:
                chunk = self.sock.recv(1 << 20)
                if chunk:
                    self.buf += chunk
            except (BlockingIOError, OSError):
                pass
            if b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                return json.loads(line.decode()), per_tick
            time.sleep(0.001)
        raise AssertionError("timeout on %s" % cmd)


def main():
    _state.reset()
    instrument()
    bridge.OnInit()
    bridge.OnIdle()
    port = bridge._transport.server.getsockname()[1]
    c = Client(port)

    print("\n-- full mixer scan is chunked --")
    resp, ticks = c.call("mixer.list", only_used=False)
    worst = max(ticks)
    busy = [t for t in ticks if t > 0]
    check("scan completed", resp["ok"], resp)
    check("all 126 tracks scanned", resp["result"]["scanned"] == 126,
          resp["result"].get("scanned"))
    check("all tracks returned", len(resp["result"]["tracks"]) == 126,
          len(resp["result"]["tracks"]))
    check("spread over multiple ticks", len(busy) > 5, len(busy))
    check("worst tick under budget (%d calls, limit %d)" % (worst, MAX_CALLS_PER_TICK),
          worst <= MAX_CALLS_PER_TICK, worst)
    print("     %d busy ticks, worst %d calls, total %d"
          % (len(busy), worst, sum(ticks)))

    print("\n-- default listing is chunked too --")
    resp, ticks = c.call("mixer.list")
    check("default scan completed", resp["ok"], resp)
    check("worst tick under budget (%d calls)" % max(ticks),
          max(ticks) <= MAX_CALLS_PER_TICK, max(ticks))
    check("only interesting tracks returned",
          len(resp["result"]["tracks"]) < 10, len(resp["result"]["tracks"]))

    print("\n-- peaks variant stays bounded --")
    resp, ticks = c.call("mixer.list", only_used=False, peaks=True)
    check("peak scan completed", resp["ok"], resp)
    check("worst tick under budget (%d calls)" % max(ticks),
          max(ticks) <= MAX_CALLS_PER_TICK, max(ticks))

    print("\n-- max_tracks stops the walk early --")
    resp, ticks = c.call("mixer.list", only_used=False, max_tracks=5)
    check("respects max_tracks", len(resp["result"]["tracks"]) == 5,
          len(resp["result"]["tracks"]))
    check("stopped scanning early", resp["result"]["scanned"] < 20,
          resp["result"]["scanned"])
    check("cheaper than a full scan", sum(ticks) < 400, sum(ticks))

    print("\n-- single-track and param reads stay cheap --")
    resp, ticks = c.call("mixer.track", track=3)
    check("track detail under budget (%d calls)" % max(ticks),
          max(ticks) <= MAX_CALLS_PER_TICK, max(ticks))
    resp, ticks = c.call("plugin.params", track=3, slot=1)
    check("param listing under budget (%d calls)" % max(ticks),
          max(ticks) <= MAX_CALLS_PER_TICK, max(ticks))

    print("\n-- raw selection reads stay bounded --")
    resp, ticks = c.call("arrangement.selection")
    check("raw selection read completed", resp["ok"], resp)
    check("raw selection under budget (%d calls)" % max(ticks),
          max(ticks) <= MAX_CALLS_PER_TICK, max(ticks))

    requests = "".join(
        json.dumps({"id": 100 + i, "cmd": "arrangement.selection", "args": {}})
        + "\n"
        for i in range(8)
    )
    c.sock.sendall(requests.encode())
    selection_batch_worst = 0
    batch_replies = []
    deadline = time.time() + 2
    while time.time() < deadline and len(batch_replies) < 8:
        CALLS.clear()
        bridge.OnIdle()
        selection_batch_worst = max(
            selection_batch_worst, sum(CALLS.values())
        )
        try:
            chunk = c.sock.recv(1 << 20)
            if chunk:
                c.buf += chunk
        except (BlockingIOError, OSError):
            pass
        while b"\n" in c.buf:
            line, c.buf = c.buf.split(b"\n", 1)
            batch_replies.append(json.loads(line.decode()))
        if len(batch_replies) < 8:
            time.sleep(0.001)
    check("eight queued selection reads answered",
          len(batch_replies) == 8 and all(r["ok"] for r in batch_replies),
          batch_replies)
    check("queued selection worst tick under budget (%d)" % selection_batch_worst,
          selection_batch_worst <= MAX_CALLS_PER_TICK,
          selection_batch_worst)

    print("\n-- maximum agent parameter page is chunked --")
    resp, ticks = c.call(
        "plugin.params", track=5, slot=0, limit=128, skip_padding=False)
    busy = [tick for tick in ticks if tick > 0]
    check("128 parameters returned", resp["ok"] and
          resp["result"]["returned"] == 128, resp)
    check("parameter page spans multiple ticks", len(busy) >= 2, busy)
    check("maximum page stays under budget (%d calls)" % max(ticks),
          max(ticks) <= MAX_CALLS_PER_TICK, max(ticks))

    print("\n-- concurrent parameter pages share the global tick budget --")
    readers = [Client(port) for _ in range(3)]
    for reader in readers:
        reader.sock.sendall((json.dumps({
            "id": 1,
            "cmd": "plugin.params",
            "args": {"track": 5, "slot": 0, "limit": 128,
                     "skip_padding": False},
        }) + "\n").encode())
    replies = [None] * len(readers)
    concurrent_param_worst = 0
    deadline = time.time() + 5
    while time.time() < deadline and any(value is None for value in replies):
        CALLS.clear()
        bridge.OnIdle()
        concurrent_param_worst = max(
            concurrent_param_worst, sum(CALLS.values()))
        for index, reader in enumerate(readers):
            try:
                chunk = reader.sock.recv(1 << 20)
                if chunk:
                    reader.buf += chunk
            except (BlockingIOError, OSError):
                pass
            if replies[index] is None and b"\n" in reader.buf:
                line, reader.buf = reader.buf.split(b"\n", 1)
                replies[index] = json.loads(line.decode())
        time.sleep(0.001)
    check("all concurrent parameter pages answered",
          all(reply and reply["ok"] for reply in replies), replies)
    check("concurrent parameter worst tick under budget (%d)" %
          concurrent_param_worst,
          concurrent_param_worst <= MAX_CALLS_PER_TICK,
          concurrent_param_worst)
    for reader in readers:
        reader.sock.close()
    for _ in range(5):
        bridge.OnIdle()

    print("\n-- chunked scans stay correct and concurrent --")
    a, b = Client(port), Client(port)
    a.sock.sendall((json.dumps(
        {"id": 1, "cmd": "mixer.list", "args": {"only_used": False}}) + "\n").encode())
    b.sock.sendall((json.dumps({"id": 1, "cmd": "ping", "args": {}}) + "\n").encode())
    got_a = got_b = None
    worst_concurrent = 0
    deadline = time.time() + 5
    while time.time() < deadline and (got_a is None or got_b is None):
        CALLS.clear()
        bridge.OnIdle()
        worst_concurrent = max(worst_concurrent, sum(CALLS.values()))
        for cl, slot in ((a, "a"), (b, "b")):
            try:
                chunk = cl.sock.recv(1 << 20)
                if chunk:
                    cl.buf += chunk
            except (BlockingIOError, OSError):
                pass
            if b"\n" in cl.buf:
                line, cl.buf = cl.buf.split(b"\n", 1)
                if slot == "a" and got_a is None:
                    got_a = json.loads(line.decode())
                elif slot == "b" and got_b is None:
                    got_b = json.loads(line.decode())
        time.sleep(0.001)
    check("long scan still answered", got_a and got_a["ok"], got_a)
    check("second client answered during the scan", got_b and got_b["ok"], got_b)
    check("concurrent worst tick under budget (%d)" % worst_concurrent,
          worst_concurrent <= MAX_CALLS_PER_TICK, worst_concurrent)

    print("\n-- a client that leaves mid-scan is cleaned up --")
    d = Client(port)
    d.sock.sendall((json.dumps(
        {"id": 1, "cmd": "mixer.list", "args": {"only_used": False}}) + "\n").encode())
    bridge.OnIdle()
    bridge.OnIdle()
    check("job registered", len(bridge._jobs) >= 1, len(bridge._jobs))
    d.sock.close()
    for _ in range(20):
        bridge.OnIdle()
    check("abandoned job dropped", len(bridge._jobs) == 0, len(bridge._jobs))
    check("bridge still healthy", c.call("ping")[0]["ok"])

    for cl in (a, b, c):
        cl.sock.close()
    for _ in range(5):
        bridge.OnIdle()
    bridge.OnDeInit()

    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
