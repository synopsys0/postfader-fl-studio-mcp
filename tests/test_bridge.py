"""Exercise the FL Studio bridge outside FL, against stub API modules.

Imports `device_UniversalBridge` with `tests/fakefl` shadowing the real FL API,
drives `OnIdle` the way FL would, and talks to it over a real TCP socket.
"""

import json
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "fakefl"))
sys.path.insert(0, os.path.join(ROOT, "bridge"))

# The lean verified write surface is a separate opt-in read once at module
# load. Load the bridge with it unset so the default really is the default;
# _load_bridge_with_writes() loads a second copy with it set.
os.environ.pop("FL_BRIDGE_ENABLE_WRITES", None)

import _state  # noqa: E402  (fake FL state)
import device_UniversalBridge as bridge  # noqa: E402

# Never bind the production loopback port from a deterministic test process.
# Port zero delegates one private ephemeral port to the kernel, preventing a
# concurrent safe-suite run (or a real local bridge) from receiving test
# traffic.
bridge.PORT = 0

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


class Client:
    """Talks to the bridge, pumping OnIdle between sends like FL would."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=2)
        self.sock.setblocking(False)
        self.buf = b""
        self.next_id = 0

    def call(self, cmd, **args):
        self.next_id += 1
        rid = self.next_id
        payload = json.dumps({"id": rid, "cmd": cmd, "args": args}) + "\n"
        self.sock.sendall(payload.encode())
        deadline = time.time() + 3.0
        while time.time() < deadline:
            bridge.OnIdle()
            try:
                chunk = self.sock.recv(65536)
                if chunk:
                    self.buf += chunk
            except BlockingIOError:
                pass
            except OSError:
                pass
            if b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                return json.loads(line.decode())
            time.sleep(0.005)
        raise AssertionError("timeout waiting for reply to %s" % cmd)


class StuckValues(list):
    """A parameter list that accepts every write and keeps its value.

    This is the failure the write surface exists to catch: FL's setter reports
    success and the control does not move. Swapping this in for a plugin's
    value list drives the real fake code path - setParamValue still runs, still
    returns 1, still updates its stale-readback bookkeeping - while the stuck
    index never changes, so the display string never changes either.
    """

    def __init__(self, values, stuck_index):
        list.__init__(self, values)
        self.stuck_index = stuck_index

    def __setitem__(self, index, value):
        if index == self.stuck_index:
            return                      # accepted, ignored, no complaint
        list.__setitem__(self, index, value)


def _load_bridge_with_writes():
    """Load a second copy of the bridge as FL would with writes enabled.

    FL_BRIDGE_ENABLE_WRITES is read once at module load, so testing both states
    honestly means loading the module twice rather than poking a constant
    afterwards. The copy exposes reads plus the ten verified writes and
    nothing else. It is never started; its commands run through `_dispatch`,
    the same allowlist gate the socket path goes through.
    """
    import importlib.util

    path = os.path.join(ROOT, "bridge", "device_UniversalBridge.py")
    saved = dict(os.environ)
    os.environ["FL_BRIDGE_ENABLE_WRITES"] = "1"
    try:
        spec = importlib.util.spec_from_file_location(
            "device_UniversalBridge_writes", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return module


def _load_bridge_read_only():
    """Load a copy of the bridge with no write flag set at all.

    That is what a real install runs: reads and nothing else. Loading it here
    is how a read command can be *shown* to need no write flag rather than
    asserted to, since the flag is read once at module load.
    """
    import importlib.util

    path = os.path.join(ROOT, "bridge", "device_UniversalBridge.py")
    saved = dict(os.environ)
    os.environ.pop("FL_BRIDGE_ENABLE_WRITES", None)
    try:
        spec = importlib.util.spec_from_file_location(
            "device_UniversalBridge_readonly", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return module


def _load_bridge_client():
    """Load fl_studio_mcp/bridge_client.py straight off disk.

    By path, not as a package import: this suite puts tests/fakefl on sys.path
    so the bridge finds stub FL modules, and nothing here should risk pulling
    the connector package in under that shadowing. The client module itself
    imports only the standard library.
    """
    import importlib.util

    path = os.path.join(ROOT, "fl_studio_mcp", "bridge_client.py")
    spec = importlib.util.spec_from_file_location(
        "fl_bridge_client_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dispatch(module, cmd, **args):
    """Run one command on a bridge copy without going over the wire.

    Write commands now span FL idle ticks: they write, hand FL's thread back,
    and only then read the control. That is required because FL's readback in
    the write's own tick returns the previous value, so drive the job to
    completion here the way the real idle loop would.
    """
    response = module._dispatch({"id": 1, "cmd": cmd, "args": args})
    if not isinstance(response, module._Job):
        return response
    while True:
        try:
            next(response.gen)
        except StopIteration as stopped:
            return {"id": response.rid, "ok": True, "result": stopped.value}
        except Exception as exc:  # mirror the bridge's error envelope
            return {"id": response.rid, "ok": False, "error": str(exc)}


def drive(module, cmd, **args):
    """Like `dispatch`, but also report how often FL's thread was handed back.

    Returns (response, yields). A long scan that comes back with zero yields
    did all of its FL API calls inside one OnIdle callback, which is the
    overrun the chunking exists to prevent - so the count is the thing worth
    asserting on, not just the answer.
    """
    response = module._dispatch({"id": 1, "cmd": cmd, "args": args})
    if not isinstance(response, module._Job):
        return response, 0
    yields = 0
    while True:
        try:
            next(response.gen)
            yields += 1
        except StopIteration as stopped:
            return ({"id": response.rid, "ok": True,
                     "result": stopped.value}, yields)
        except Exception as exc:
            return ({"id": response.rid, "ok": False,
                     "error": str(exc)}, yields)


def check_source_is_ascii():
    """The bridge file must contain no byte above 127.

    FL Studio's embedded interpreter reports locale.getpreferredencoding() as
    US-ASCII. If FL reads the script with that encoding, a single smart quote
    or em dash anywhere in the file - even inside a docstring - kills the load
    with UnicodeDecodeError before any code runs, and the only symptom is a
    bridge that never answers.
    """
    print("\n-- source encoding --")
    path = os.path.join(ROOT, "bridge", "device_UniversalBridge.py")
    raw = open(path, "rb").read()
    try:
        raw.decode("ascii")
        check("bridge source is pure ASCII", True)
    except UnicodeDecodeError as e:
        bad = raw[e.start:e.start + 1]
        line = raw[:e.start].count(b"\n") + 1
        check("bridge source is pure ASCII", False,
              "byte %r on line %d - FL cannot load this" % (bad, line))


def check_transport_selection():
    """TCP is preferred, and an unusable socket must fall through to files.

    Inside FL the socket type cannot construct at all, so the fallback is the
    only thing that makes the bridge reachable. Both paths are checked here.
    """
    print("\n-- transport selection --")
    import tempfile

    bridge._start_server()
    check("prefers TCP when sockets work", bridge._transport.name == "tcp",
          bridge._transport.name)
    bridge.OnDeInit()

    class Unusable:
        name = "tcp"

        def start(self):
            raise SystemError(
                "<slot wrapper '__init__' of '_socket.socket' objects> "
                "returned NULL without setting an exception")

    mailbox = tempfile.mkdtemp(prefix="flmcp-sel-")
    real_cls, real_box = bridge._SocketTransport, bridge.MAILBOX
    bridge._SocketTransport, bridge.MAILBOX = Unusable, mailbox
    try:
        bridge._start_server()
        check("falls back to files when sockets are unusable",
              bridge._transport.name == "files", bridge._transport.name)
        check("mailbox is usable",
              os.path.isfile(os.path.join(mailbox, bridge.ALIVE_NAME)),
              os.listdir(mailbox))
        bridge.OnDeInit()
    finally:
        bridge._SocketTransport, bridge.MAILBOX = real_cls, real_box
        import shutil
        shutil.rmtree(mailbox, ignore_errors=True)


def check_scan_params(c):
    """plugin.scan_params: de-pad a whole plug-in in one round trip.

    Addressing is the hard problem: answering "which index is the compressor's
    threshold?" means knowing the real parameter map, and FL reports one padded
    maximum for a VST3 -- thousands of slots -- with the real ones scattered
    sparsely inside that range. Paging it costs a client about a thousand round
    trips. These checks pin the alternative - the walk happening inside FL,
    across idle ticks, answering once - and pin it against a fixture whose
    padding really is interleaved with the real controls, so de-padding is
    exercised rather than assumed.
    """
    real_indices = sorted(_state.SPARSE_VST_REAL)
    total = _state.SPARSE_VST_COUNT

    print("\n-- the fixture really is a sparse padded map --")
    check("reported count is far larger than the real one",
          total == 1200 and len(real_indices) == 7, (total, real_indices))
    check("real parameters are scattered, not a prefix",
          real_indices[-1] == total - 1 and 517 in real_indices, real_indices)

    print("\n-- scanning a padded VST returns only the real parameters --")
    undo_at = len(_state.UNDO)
    before_values = list(_state.TRACKS[7].slots[2].values)
    r = c.call("plugin.scan_params", track=7, slot=2)
    check("scan ok", r["ok"], r)
    res = r["result"]
    check("only the real parameters come back",
          [p["index"] for p in res["params"]] == real_indices,
          [p["index"] for p in res["params"]])
    check("no returned row is padding by the bridge's own rule",
          not [p for p in res["params"]
               if bridge._is_padding(p["name"], p["display"])],
          [p for p in res["params"]
           if bridge._is_padding(p["name"], p["display"])])
    check("a parameter with no name but a real display is kept",
          res["params"][1]["index"] == 1
          and res["params"][1]["name"] == ""
          and res["params"][1]["display"] == "Auto mode", res["params"][1])
    check("a named parameter displaying a bare zero is kept",
          any(p["index"] == 61 and p["name"] == "Throat Length"
              for p in res["params"]),
          [p for p in res["params"] if p["index"] == 61])

    print("\n-- every real parameter carries the display that identifies it --")
    by_index = {p["index"]: p for p in res["params"]}
    check("display strings present on every row",
          all(p["display"] for p in res["params"]),
          [p for p in res["params"] if not p["display"]])
    check("the display says what the number cannot",
          by_index[4]["display"] == "20 ms"
          and abs(by_index[4]["value"] - 0.6855) < 1e-9, by_index[4])
    check("name, index, value and display all reported",
          set(by_index[0]) == {"index", "name", "value", "display"}
          and by_index[0]["name"] == "Input Gain"
          and by_index[0]["display"] == "-3.0 dB", by_index[0])

    print("\n-- the scan reports its own coverage --")
    check("FL's padded count is reported as reported",
          res["reported_count"] == total, res["reported_count"])
    check("the real count is reported separately",
          res["real"] == len(real_indices) == len(res["params"]),
          (res["real"], len(res["params"])))
    check("both counts are surfaced, and they disagree",
          res["reported_count"] > res["real"] * 100,
          (res["reported_count"], res["real"]))
    check("padding counted, not silently dropped",
          res["padding_skipped"] == total - len(real_indices),
          res["padding_skipped"])
    check("real plus padding accounts for every index examined",
          res["real"] + res["padding_skipped"] == res["examined"] == total,
          (res["real"], res["padding_skipped"], res["examined"]))
    check("highest index examined reported",
          res["highest_index_examined"] == total - 1,
          res["highest_index_examined"])
    check("a complete scan does not claim truncation",
          res["truncated"] is False and res["truncated_by"] is None, res)
    check("the plug-in is named", res["plugin"] == "Sparse Param VST3",
          res.get("plugin"))

    print("\n-- the scan is a read --")
    check("no undo point taken", len(_state.UNDO) == undo_at, _state.UNDO[-2:])
    check("nothing in the plug-in moved",
          _state.TRACKS[7].slots[2].values == before_values,
          _state.TRACKS[7].slots[2].values[:8])

    print("\n-- it shares one padding rule with plugin.params --")
    paged = []
    paged_padding = 0
    offset = 0
    while offset < total:
        page = c.call("plugin.params", track=7, slot=2, offset=offset,
                      limit=400)["result"]
        paged.extend(p["index"] for p in page["params"])
        paged_padding += page["padding_skipped"]
        offset += 400
    check("paging by hand finds exactly the same parameters",
          paged == real_indices, paged)
    check("and counts exactly the same padding",
          paged_padding == res["padding_skipped"],
          (paged_padding, res["padding_skipped"]))

    print("\n-- an honest native plug-in is not de-padded away --")
    r = c.call("plugin.scan_params", track=3, slot=1)
    res = r["result"]
    check("all six compressor parameters kept",
          r["ok"] and res["real"] == 6 and res["reported_count"] == 6, res)
    check("nothing mistaken for padding", res["padding_skipped"] == 0, res)
    check("named and complete",
          res["params"][0]["name"] == "Threshold"
          and res["truncated"] is False, res["params"][0])

    print("\n-- a bounded scan says it is bounded --")
    res = c.call("plugin.scan_params", track=7, slot=2,
                 max_indices=100)["result"]
    check("max_indices stops the walk",
          res["examined"] == 100 and res["highest_index_examined"] == 99, res)
    check("and says the map is incomplete",
          res["truncated"] is True and res["truncated_by"] == "max_indices",
          res)
    check("a truncated scan still reports FL's full count",
          res["reported_count"] == total, res["reported_count"])
    check("only the real parameters below the bound came back",
          [p["index"] for p in res["params"]]
          == [i for i in real_indices if i < 100],
          [p["index"] for p in res["params"]])

    res = c.call("plugin.scan_params", track=7, slot=2, end=64)["result"]
    check("an end bound is reported as truncation",
          res["truncated"] is True and res["truncated_by"] == "end"
          and res["examined"] == 64, res)
    res = c.call("plugin.scan_params", track=7, slot=2,
                 max_results=2)["result"]
    check("max_results stops on the result it was asked for",
          res["real"] == 2 and res["truncated_by"] == "max_results"
          and [p["index"] for p in res["params"]] == real_indices[:2], res)
    res = c.call("plugin.scan_params", track=7, slot=2, start=500)["result"]
    check("a start offset is reported as truncation too",
          res["truncated"] is True and res["truncated_by"] == "start"
          and res["scan_start"] == 500, res)
    check("the tail of the map still scanned correctly",
          [p["index"] for p in res["params"]]
          == [i for i in real_indices if i >= 500], res["params"])

    print("\n-- bad bounds are refused rather than guessed at --")
    for label, args in (
        ("negative start", {"track": 7, "slot": 2, "start": -1}),
        ("negative end", {"track": 7, "slot": 2, "end": -5}),
        ("zero max_indices", {"track": 7, "slot": 2, "max_indices": 0}),
        ("max_indices past the ceiling",
         {"track": 7, "slot": 2, "max_indices": 99999}),
        ("zero max_results", {"track": 7, "slot": 2, "max_results": 0}),
        ("empty slot", {"track": 7, "slot": 5}),
        ("track outside the mixer", {"track": 999, "slot": 0}),
    ):
        r = c.call("plugin.scan_params", **args)
        check("%s refused" % label, not r["ok"], r)

    print("\n-- the walk is spread over FL idle ticks --")
    full, yields = drive(bridge, "plugin.scan_params", track=7, slot=2)
    check("driven scan still complete",
          full["ok"] and full["result"]["real"] == len(real_indices), full)
    check("a 1200-index walk does not happen in one tick", yields > 1, yields)
    check("it yields once per chunk of indices examined",
          yields == total // bridge.PARAMS_PER_TICK, yields)
    small, small_yields = drive(bridge, "plugin.scan_params", track=7, slot=2,
                                max_indices=10)
    check("a short scan does not yield needlessly",
          small["ok"] and small_yields == 0, small_yields)
    _paged, paged_yields = drive(bridge, "plugin.params", track=7, slot=2,
                                 limit=400)
    check("paging a sparse map spans ticks as well",
          paged_yields >= 2, paged_yields)

    print("\n-- the scan needs no write flag --")
    ro = _load_bridge_read_only()
    check("the copy really is locked read-only",
          ro.LEAN_WRITES_ENABLED is False, ro.LEAN_WRITES_ENABLED)
    check("scan_params is on the read-only allowlist beside plugin.params",
          {"plugin.scan_params", "plugin.params"} <= ro.READ_ONLY_COMMANDS,
          sorted(ro.READ_ONLY_COMMANDS))
    r = dispatch(ro, "plugin.scan_params", track=7, slot=2)
    check("and it answers with no write flag set",
          r["ok"] and r["result"]["real"] == len(real_indices), r)
    r = dispatch(ro, "plugin.set_param", track=7, slot=2, index=0, value=0.1)
    check("while writes on that same copy stay prohibited",
          not r["ok"] and "read-only" in r["error"], r)
    check("the read-only copy left the plug-in alone",
          _state.TRACKS[7].slots[2].values == before_values,
          _state.TRACKS[7].slots[2].values[:8])

    print("\n-- BridgeClient.scan_plugin_params speaks the same argument names --")
    client_mod = _load_bridge_client()
    sent = []

    class _Wire:
        """Stands in for a live link: records the call, then really runs it.

        A convenience wrapper that mistypes an argument name is invisible to a
        mock, so the recorded call is dispatched against the same bridge the
        rest of this suite drives. If the client and the bridge ever disagree
        about what an argument is called, this fails here rather than on a
        live plug-in.
        """

        def call(self, cmd, **args):
            sent.append((cmd, dict(args)))
            reply = dispatch(bridge, cmd, **args)
            if not reply["ok"]:
                raise AssertionError(reply["error"])
            return reply["result"]

    scan = client_mod.BridgeClient.scan_plugin_params
    res = scan(_Wire(), 7, 2)
    check("the wrapper sends plugin.scan_params",
          sent[-1][0] == "plugin.scan_params", sent[-1])
    check("with no bounds unless it was given some",
          sent[-1][1] == {"track": 7, "slot": 2}, sent[-1])
    check("and the bridge answers it in full",
          res["real"] == len(real_indices) and res["truncated"] is False, res)
    res = scan(_Wire(), 7, 2, start=2, end=600, max_indices=500, max_results=3)
    check("every bound reaches the bridge under the name it reads",
          sent[-1][1] == {"track": 7, "slot": 2, "start": 2, "end": 600,
                          "max_indices": 500, "max_results": 3}, sent[-1])
    check("and the bounds actually took effect",
          res["scan_start"] == 2 and res["scan_end"] == 600
          and res["truncated"] is True, res)
    check("the long scan is replay-safe after a lost reply",
          "plugin.scan_params" in client_mod.IDEMPOTENT_READ_COMMANDS,
          sorted(client_mod.IDEMPOTENT_READ_COMMANDS))


def check_lean_writes(c):
    """The ten verified write commands: gate, master guard, and proof rules.

    `c` is the live client talking to the bridge copy this process loaded
    *without* FL_BRIDGE_ENABLE_WRITES, which is what proves the commands are
    unreachable by default.
    """
    import general as fake_general
    import mixer as fake_mixer
    import plugins as fake_plugins

    w = _load_bridge_with_writes()

    # Tripwire: nothing on this surface may ever save the project.
    saves = []
    fake_general.saveProject = lambda *args, **kwargs: saves.append(args)

    print("\n-- lean writes are gated behind FL_BRIDGE_ENABLE_WRITES --")
    check("flag off in the bridge loaded without it",
          bridge.LEAN_WRITES_ENABLED is False, bridge.LEAN_WRITES_ENABLED)
    check("flag on in the copy loaded with it",
          w.LEAN_WRITES_ENABLED is True, w.LEAN_WRITES_ENABLED)
    check("the write surface is exactly the published set",
          w.LEAN_WRITE_COMMANDS == frozenset({
              "mixer.set_volume", "mixer.set_pan", "mixer.set_mute",
              "mixer.set_eq", "mixer.set_name", "mixer.set_send",
              "mixer.set_send_level",
              "plugin.set_param", "plugin.set_param_display",
              "plugin.set_param_option"}),
          sorted(w.LEAN_WRITE_COMMANDS))
    check("the dispatcher contains exactly reads and verified writes",
          set(w.HANDLERS) == set(w.READ_ONLY_COMMANDS | w.LEAN_WRITE_COMMANDS),
          sorted(w.HANDLERS))

    vol_before = _state.TRACKS[3].volume
    gated = [
        ("mixer.set_volume", {"track": 3, "value": 0.5}),
        ("mixer.set_pan", {"track": 3, "value": 0.5}),
        ("mixer.set_mute", {"track": 3, "muted": True}),
        ("mixer.set_eq", {"track": 3, "band": 0, "gain": 0.7}),
        ("mixer.set_name", {"track": 3, "name": "gated"}),
        ("mixer.set_send", {"track": 3, "to": 5, "enabled": True}),
        ("mixer.set_send_level", {"track": 3, "to": 0, "value": 0.5}),
        ("plugin.set_param", {"track": 3, "slot": 1, "index": 1, "value": 0.5}),
        ("plugin.set_param_display",
         {"track": 3, "slot": 1, "param": "Threshold", "target": 40.0}),
        ("plugin.set_param_option",
         {"track": 9, "slot": 0, "param": "Key", "option": "A"}),
    ]
    for cmd, args in gated:
        r = c.call(cmd, **args)
        check("%s is not dispatchable without the flag" % cmd,
              (not r["ok"]) and cmd not in r.get("available", []), r)
    check("nothing moved while the surface was gated off",
          abs(_state.TRACKS[3].volume - vol_before) < 1e-9
          and _state.TRACKS[3].muted is False, _state.TRACKS[3].volume)
    check("the handlers exist regardless; the allowlist is the gate",
          all(cmd in bridge.HANDLERS for cmd in bridge.LEAN_WRITE_COMMANDS),
          sorted(bridge.HANDLERS))

    r = dispatch(w, "ping")
    check("a bridge that can write does not call itself read-only",
          r["result"]["bridge_mode"] == "write_test", r["result"]["bridge_mode"])
    check("ping advertises the surface",
          r["result"]["verified_writes_enabled"] is True, r["result"])
    check("reads still work with writes enabled", dispatch(w, "project.info")["ok"])

    print("\n-- master is refused unless it is asked for by name --")
    master_vol = _state.TRACKS[0].volume
    master_gain = _state.TRACKS[0].slots[0].values[0]
    for cmd, args in (
        ("mixer.set_volume", {"track": 0, "value": 0.5}),
        ("mixer.set_pan", {"track": 0, "value": 0.5}),
        ("mixer.set_mute", {"track": 0, "muted": True}),
        ("mixer.set_eq", {"track": 0, "band": 0, "gain": 0.7}),
        ("plugin.set_param", {"track": 0, "slot": 0, "index": 0, "value": 0.9}),
    ):
        r = dispatch(w, cmd, **args)
        check("%s refuses master by default" % cmd,
              (not r["ok"]) and "master" in r["error"], r)
    check("master really was left alone",
          abs(_state.TRACKS[0].volume - master_vol) < 1e-9
          and _state.TRACKS[0].muted is False
          and abs(_state.TRACKS[0].eq[0]["gain"] - 0.5) < 1e-9
          and abs(_state.TRACKS[0].slots[0].values[0] - master_gain) < 1e-9,
          (_state.TRACKS[0].volume, _state.TRACKS[0].muted))

    r = dispatch(w, "mixer.set_volume", track=0, value=0.55, allow_master=True)
    check("master fader accepted with allow_master",
          r["ok"] and r["result"]["verified"] is True, r)
    check("master fader actually moved",
          abs(_state.TRACKS[0].volume - 0.55) < 1e-9, _state.TRACKS[0].volume)
    dispatch(w, "mixer.set_volume", track=0, value=master_vol, allow_master=True)

    _state.LAST_WRITE[0] = None
    _state.TRACKS[0].slots[0].reported = {}
    r = dispatch(w, "plugin.set_param", track=0, slot=0, index=0, value=0.9,
                 allow_master=True)
    check("master plugin accepted with allow_master",
          r["ok"] and r["result"]["verified"] is True, r)
    check("master plugin parameter actually moved",
          abs(_state.TRACKS[0].slots[0].values[0] - 0.9) < 1e-9,
          _state.TRACKS[0].slots[0].values[0])
    _state.TRACKS[0].slots[0].values[0] = master_gain

    print("\n-- a verified write on each surface --")
    undo_at = len(_state.UNDO)
    was = _state.TRACKS[3].volume
    r = dispatch(w, "mixer.set_volume", track=3, value=0.55)
    res = r["result"]
    check("volume write verified", r["ok"] and res["verified"] is True, r)
    check("volume landed in FL", abs(_state.TRACKS[3].volume - 0.55) < 1e-9,
          _state.TRACKS[3].volume)
    check("volume reported requested/before/after",
          abs(res["requested"] - 0.55) < 1e-9
          and abs(res["before"] - was) < 1e-9
          and abs(res["after"] - 0.55) < 1e-9, res)
    check("volume carries the dB FL shows for it",
          res["before_db"] is not None and res["after_db"] is not None, res)
    check("volume made exactly one undo point",
          len(_state.UNDO) == undo_at + 1, _state.UNDO[-2:])

    undo_at = len(_state.UNDO)
    was = _state.TRACKS[3].pan
    r = dispatch(w, "mixer.set_pan", track=3, value=-0.25)
    res = r["result"]
    check("pan write verified", r["ok"] and res["verified"] is True, r)
    check("pan landed in FL", abs(_state.TRACKS[3].pan + 0.25) < 1e-9,
          _state.TRACKS[3].pan)
    check("pan reported before/after", abs(res["before"] - was) < 1e-9
          and abs(res["after"] + 0.25) < 1e-9, res)
    check("pan made exactly one undo point", len(_state.UNDO) == undo_at + 1,
          _state.UNDO[-2:])

    undo_at = len(_state.UNDO)
    r = dispatch(w, "mixer.set_mute", track=3, muted=True)
    res = r["result"]
    check("mute write verified", r["ok"] and res["verified"] is True, r)
    check("mute landed in FL", _state.TRACKS[3].muted is True,
          _state.TRACKS[3].muted)
    check("mute reported before/after",
          res["before"] is False and res["after"] is True, res)
    check("mute made exactly one undo point", len(_state.UNDO) == undo_at + 1,
          _state.UNDO[-2:])
    r = dispatch(w, "mixer.set_mute", track=3, muted=False)
    check("unmute verified and applied",
          r["result"]["verified"] is True and _state.TRACKS[3].muted is False,
          r["result"])

    undo_at = len(_state.UNDO)
    band_was = dict(_state.TRACKS[3].eq[1])
    r = dispatch(w, "mixer.set_eq", track=3, band=1, gain=0.62, freq=0.33)
    res = r["result"]
    check("eq write verified", r["ok"] and res["verified"] is True, r)
    check("both eq fields verified individually",
          res["verified_fields"] == {"gain": True, "freq": True}, res)
    check("eq landed in FL",
          abs(_state.TRACKS[3].eq[1]["gain"] - 0.62) < 1e-9
          and abs(_state.TRACKS[3].eq[1]["freq"] - 0.33) < 1e-9,
          _state.TRACKS[3].eq[1])
    check("eq captured the band as it was",
          abs(res["before"]["gain"] - band_was["gain"]) < 1e-9
          and abs(res["before"]["freq"] - band_was["freq"]) < 1e-9, res)
    check("eq reports dB and Hz alongside the normalised values",
          res["after"]["gain_db"] is not None
          and res["after"]["freq_hz"] is not None, res["after"])
    check("eq made exactly one undo point", len(_state.UNDO) == undo_at + 1,
          _state.UNDO[-2:])
    r = dispatch(w, "mixer.set_eq", track=3, band=2, freq=0.75)
    check("eq accepts one field on its own",
          r["ok"] and r["result"]["verified_fields"] == {"freq": True},
          r.get("result"))

    print("\n-- plugin parameters are proved by the display string --")
    plug = _state.TRACKS[3].slots[1]
    plug.values[1] = 0.20
    plug.reported = {}
    _state.LAST_WRITE[0] = None
    undo_at = len(_state.UNDO)
    calls = []
    real_set = fake_plugins.setParamValue

    def counting_set(*args, **kwargs):
        calls.append(args)
        return real_set(*args, **kwargs)

    fake_plugins.setParamValue = counting_set
    try:
        r = dispatch(w, "plugin.set_param", track=3, slot=1, index=1, value=0.66)
    finally:
        fake_plugins.setParamValue = real_set
    res = r["result"]
    check("plugin write verified", r["ok"] and res["verified"] is True, r)
    check("plugin value landed in FL", abs(plug.values[1] - 0.66) < 1e-9,
          plug.values[1])
    check("FL was written to twice, because it drops a lone write",
          len(calls) >= 2, len(calls))
    check("the display string is what proved it",
          res["display_changed"] is True
          and res["before"]["display"] == "20.0 %"
          and res["after"]["display"] == "66.0 %", res)
    check("the stale numeric readback did not decide it",
          abs(res["after"]["value"] - 0.66) > 1e-6, res["after"])
    check("plugin names the parameter it moved",
          res["name"] == "Ratio" and res["plugin"] == "Fruity Compressor", res)
    check("plugin made exactly one undo point", len(_state.UNDO) == undo_at + 1,
          _state.UNDO[-2:])

    # Nothing observable happens when the parameter is already there: the
    # display cannot change. The readback taken after the write is what says
    # the parameter is where it was asked to be. Note the *before* reading is
    # the stale 0.20 from the previous write, which is exactly why it is not
    # allowed to decide anything.
    _state.LAST_WRITE[0] = None
    r = dispatch(w, "plugin.set_param", track=3, slot=1, index=1, value=0.66)
    res = r["result"]
    check("rewriting the value it already holds stays verified",
          r["ok"] and res["verified"] is True, res)
    check("and says so through the readback, not a display change",
          res["display_changed"] is False and res["reads_at_value"] is True,
          res)

    print("\n-- FL accepting a write and moving nothing --")
    verb = _state.TRACKS[3].slots[3]
    verb.reported = {}
    stuck = StuckValues(verb.values, 0)
    verb.values = stuck
    _state.LAST_WRITE[0] = None
    try:
        first = fake_plugins.setParamValue(0.9, 0, 3, 3)
        second = fake_plugins.setParamValue(0.9, 0, 3, 3)
        check("the fake reports success on both writes",
              first == 1 and second == 1, (first, second))
        check("and the parameter genuinely did not move",
              abs(stuck[0] - 0.4) < 1e-9, stuck[0])
        _state.LAST_WRITE[0] = None
        verb.reported = {}
        r = dispatch(w, "plugin.set_param", track=3, slot=3, index=0, value=0.9)
    finally:
        verb.values = list(stuck)
    res = r["result"]
    check("an ignored plugin write is reported, not raised", r["ok"] is True, r)
    check("verified is false when the display never moved",
          res["verified"] is False, res)
    check("before and after agree because nothing happened",
          res["before"]["display"] == res["after"]["display"]
          and res["display_changed"] is False, res)
    check("the ignored write still requested what it was asked for",
          abs(res["requested"] - 0.9) < 1e-9, res)

    real_vol = fake_mixer.setTrackVolume
    fake_mixer.setTrackVolume = lambda index, volume, pickupMode=-1: 1
    try:
        r = dispatch(w, "mixer.set_volume", track=3, value=0.12)
    finally:
        fake_mixer.setTrackVolume = real_vol
    res = r["result"]
    check("an ignored fader write is reported, not raised", r["ok"] is True, r)
    check("fader verified false when the readback did not follow",
          res["verified"] is False, res)
    check("fader before and after match because nothing moved",
          abs(res["after"] - res["before"]) < 1e-9, res)

    print("\n-- naming, sends and slot mix --")
    res = dispatch(w, "mixer.set_name", track=3, name="Lead Verb")["result"]
    check("a track takes the name it was given",
          res["verified"] and res["after"] == "Lead Verb", res)
    res = dispatch(w, "mixer.set_name", track=3, name="")["result"]
    check("an empty name restores FL's default rather than blanking the track",
          res["verified"] and res["restored_default"] and res["after"], res)

    check("track 3 does not send to track 5 yet",
          _state.TRACKS[3].routes.get(5) is None, _state.TRACKS[3].routes)
    res = dispatch(w, "mixer.set_send", track=3, to=5, enabled=True)["result"]
    check("the send is created and verified",
          res["verified"] and res["after"] is True, res)
    res = dispatch(w, "mixer.set_send_level", track=3, to=5, value=0.42)["result"]
    check("the send level lands on the route",
          res["verified"] and abs(res["after"] - 0.42) < 1e-6, res)
    check("and the reply says the route is actually carrying it",
          res["send_active"] is True, res)

    # The device trap: FL raises "Index out of range" reading the level of a
    # route that is not active, so a level set on a route nobody created could
    # never read back. It has to be refused, not reported unverified.
    r = dispatch(w, "mixer.set_send_level", track=3, to=7, value=0.5)
    check("a level on a route that does not exist is refused, not attempted",
          not r["ok"] and "mixer.set_send" in r.get("error", ""), r)

    res = dispatch(w, "mixer.set_send", track=3, to=5, enabled=False)["result"]
    check("tearing the send down verifies too",
          res["verified"] and res["after"] is False, res)
    check("and its level reads as absent rather than as zero",
          res["level"] is None, res)

    r = dispatch(w, "mixer.set_send", track=3, to=3, enabled=True)
    check("a track may not send to itself", not r["ok"], r)

    print("\n-- setting a parameter in the units the plug-in displays --")
    # The fake's display is the value as a percentage, so a target of 40 is a
    # normalised 0.4 -- but nothing here is told that, and the bridge is never
    # given the curve. It searches on the readback exactly as it does on FL.
    res = dispatch(w, "plugin.set_param_display",
                   track=3, slot=1, param="Threshold", target=40.0)["result"]
    check("the display search lands on the requested number",
          res["verified"] and abs(res["landed_on"] - 40.0) <= res["tolerance"], res)
    check("and it resolved the parameter by name",
          res["matched_on"] == "name" and res["index"] == 0, res)
    check("the reply carries the display FL now shows",
          "%" in (res["after"]["display"] or ""), res)

    res = dispatch(w, "plugin.set_param_display",
                   track=3, slot=1, param="thresh", target=70.0)["result"]
    check("a substring resolves too, and says so",
          res["matched_on"] == "name_substring" and res["index"] == 0, res)

    res = dispatch(w, "plugin.set_param_display",
                   track=3, slot=1, param=1, target=25.0)["result"]
    check("a bare index still works and reports matched_on=index",
          res["matched_on"] == "index" and res["index"] == 1, res)

    r = dispatch(w, "plugin.set_param_display",
                 track=3, slot=1, param="no such control", target=1.0)
    check("an unresolvable name is refused and points at the scan",
          not r["ok"] and "scan_params" in r.get("error", ""), r)

    print("\n-- every parameter write turns FL's pickup off --")
    # The trap this guards: FL's default pickup can put a control into
    # "waiting for pickup" after repeated writes and then refuse everything,
    # including the write that would put it back. Live, that stranded a
    # third-party VST control at the wrong setting until an undo rescued it.
    import plugins as fake_plugins
    _state.reset()
    for i in range(6):
        fake_plugins.setParamValue(0.1 * i, 0, 3, 1)          # pickup left on
    check("a caller that forgets pickup gets latched by FL",
          _state.TRACKS[3].slots[1].values[0] < 0.2,
          _state.TRACKS[3].slots[1].values[0])
    _state.reset()
    for i in range(6):
        fake_plugins.setParamValue(0.1 * i, 0, 3, 1, w.PICKUP_NONE)
    check("and PIM_None keeps the control writable",
          abs(_state.TRACKS[3].slots[1].values[0] - 0.5) < 1e-9,
          _state.TRACKS[3].slots[1].values[0])

    _state.reset()
    res = dispatch(w, "plugin.set_param",
                   track=3, slot=1, index=0, value=0.25)["result"]
    check("so a long run of verified writes never strands a control", res["verified"], res)
    for value in (0.4, 0.55, 0.7, 0.85, 0.3):
        res = dispatch(w, "plugin.set_param",
                       track=3, slot=1, index=0, value=value)["result"]
    check("even after six of them in a row", res["verified"], res)

    print("\n-- enumerated controls, the ones with no number to search --")
    res = dispatch(w, "plugin.set_param_option",
                   track=9, slot=0, param="Key", option="A")["result"]
    check("an enumerated control lands on the named option",
          res["verified"] and res["selected"] == "A", res)
    check("and the sweep reports the whole enumeration it found",
          res["options"] == ["C", "C#", "D", "D#", "E", "F",
                             "F#", "G", "G#", "A", "A#", "B"], res["options"])

    moved = _state.TRACKS[9].slots[0].values[0]
    r = dispatch(w, "plugin.set_param_option",
                 track=9, slot=0, param="Key", option="H")
    check("a missing option is refused and names what was found",
          not r["ok"] and "'A#'" in r.get("error", ""), r)
    check("and the control the sweep moved is put back",
          abs(_state.TRACKS[9].slots[0].values[0] - moved) < 1e-9,
          _state.TRACKS[9].slots[0].values[0])

    print("\n-- the undo guarantee is observed, never asserted --")
    # Undo is the entire safety net for this surface. Telling a caller a change
    # is reversible when FL took no undo point hands them the one guarantee
    # they cannot check for themselves, so it is watched rather than claimed.
    res = dispatch(w, "mixer.set_volume", track=3, value=0.55)["result"]
    check("a normal write reports the undo point it created",
          res["undo_point_created"] is True, res)

    real_save = fake_general.saveUndo
    fake_general.saveUndo = lambda *a, **k: None      # FL silently declines
    try:
        res = dispatch(w, "mixer.set_volume", track=3, value=0.6)["result"]
    finally:
        fake_general.saveUndo = real_save
    check("a write FL took no undo point for says so",
          res["undo_point_created"] is False, res)
    check("and it still reports the fader move truthfully",
          res["verified"] is True, res)

    print("\n-- master is guarded at the bridge, not only at the MCP layer --")
    for cmd, args in (
        ("plugin.set_param_display",
         {"track": 0, "slot": 0, "param": "GAIN", "target": 50.0}),
        ("plugin.set_param_option",
         {"track": 0, "slot": 0, "param": "GAIN", "option": "On"}),
    ):
        r = dispatch(w, cmd, **args)
        check("%s refuses master without allow_master" % cmd,
              not r["ok"] and "allow_master" in r.get("error", ""), r)

    print("\n-- a failed option search never leaves the control adrift --")
    _state.reset()
    before = _state.TRACKS[9].slots[0].values[0]
    r = dispatch(w, "plugin.set_param_option",
                 track=9, slot=0, param="Key", option="H")
    check("the restore is verified and the error says so",
          not r["ok"] and "restored, verified" in r.get("error", ""), r)
    check("and the control really is back",
          abs(_state.TRACKS[9].slots[0].values[0] - before) < 1e-9)

    # Now block the restore the way FL does when it ignores a write. The
    # caller must be told the control was left moved, not merely that the
    # option was missing.
    _state.reset()
    original_value = _state.TRACKS[9].slots[0].values[0]
    real_set2 = fake_plugins.setParamValue

    def refuse_restore(value, paramIndex, index, slotIndex=-1, pickupMode=0,
                       useGlobalIndex=False):
        # FL accepts the sweep but ignores the write back to where it started.
        if paramIndex == 0 and abs(value - original_value) < 1e-9:
            return 1
        return real_set2(value, paramIndex, index, slotIndex, pickupMode,
                         useGlobalIndex)

    fake_plugins.setParamValue = refuse_restore
    try:
        r = dispatch(w, "plugin.set_param_option",
                     track=9, slot=0, param="Key", option="H")
    finally:
        fake_plugins.setParamValue = real_set2
    check("a restore FL ignored is reported as leaving the control moved",
          not r["ok"] and "LEFT AT" in r.get("error", ""), r)

    print("\n-- movement is not proof it moved to the requested value --")
    _state.reset()
    real_set = fake_plugins.setParamValue

    def wrong_landing(value, paramIndex, index, slotIndex=-1, pickupMode=0,
                      useGlobalIndex=False):
        # Accepted, and lands somewhere else entirely.
        return real_set(0.2, paramIndex, index, slotIndex, pickupMode,
                        useGlobalIndex)

    fake_plugins.setParamValue = wrong_landing
    try:
        res = dispatch(w, "plugin.set_param",
                       track=3, slot=1, index=0, value=0.9)["result"]
    finally:
        fake_plugins.setParamValue = real_set
    check("a write confirmed only by a moved display says exactly that",
          res["verification_basis"] == "display_change_only", res)
    check("the value readback is the stale previous one, so it proves nothing",
          abs(res["after"]["value"] - res["before"]["value"]) < 1e-9, res)
    check("and the display shows it did not land on the request",
          res["after"]["display"] == "20.0 %" and res["requested"] == 0.9, res)

    print("\n-- refusing is an error; ignoring is verified false --")
    for label, cmd, args in (
        ("volume above 1.0", "mixer.set_volume", {"track": 3, "value": 1.5}),
        ("pan beyond hard right", "mixer.set_pan", {"track": 3, "value": 2.0}),
        ("missing value", "mixer.set_volume", {"track": 3}),
        ("missing muted", "mixer.set_mute", {"track": 3}),
        ("eq band out of range", "mixer.set_eq", {"track": 3, "band": 9,
                                                  "gain": 0.5}),
        ("eq with nothing to set", "mixer.set_eq", {"track": 3, "band": 0}),
        ("track outside the mixer", "mixer.set_volume", {"track": 999,
                                                         "value": 0.5}),
        ("empty plugin slot", "plugin.set_param", {"track": 3, "slot": 9,
                                                   "index": 0, "value": 0.5}),
        ("parameter index past the end", "plugin.set_param",
         {"track": 3, "slot": 1, "index": 999, "value": 0.5}),
        ("plugin value above 1.0", "plugin.set_param",
         {"track": 3, "slot": 1, "index": 1, "value": 1.7}),
    ):
        r = dispatch(w, cmd, **args)
        check("%s is refused outright" % label, not r["ok"], r)

    check("no verified write saved the project", not saves, saves)


def main():
    _state.reset()
    check_source_is_ascii()
    check_transport_selection()
    bridge.OnInit()
    bridge.OnIdle()
    port = bridge._transport.server.getsockname()[1]
    c = Client(port)

    print("\n-- handshake --")
    r = c.call("ping")
    check("ping ok", r["ok"], r)
    check("reports FL version",
          r["result"]["fl_version"] ==
          "Producer Edition v26.1.3 [build 5336]", r)

    r = c.call("project.info")
    check("project.info ok", r["ok"], r)
    check("tempo scaled to BPM", r["result"]["tempo_bpm"] == 140.0,
          r["result"].get("tempo_bpm"))
    check("mixer count", r["result"]["mixer_track_count"] == 126, r)

    print("\n-- raw arrangement selection --")
    _state.SELECTION_START = 384
    _state.SELECTION_END = 768
    r = c.call("arrangement.selection")
    check("selection read ok", r["ok"], r)
    check(
        "raw endpoints preserved",
        r["result"]["first_raw_start"] == 384
        and r["result"]["second_raw_start"] == 384
        and r["result"]["first_raw_end"] == 768
        and r["result"]["second_raw_end"] == 768,
        r,
    )
    check(
        "PPQ repeated consistently",
        r["result"]["first_ppq"] == 96
        and r["result"]["second_ppq"] == 96,
        r,
    )
    r = c.call("arrangement.selection", unexpected=True)
    check("selection command rejects arguments", not r["ok"], r)

    print("\n-- mixer listing --")
    r = c.call("mixer.list")
    check("mixer.list ok", r["ok"], r)
    tracks = r["result"]["tracks"]
    names = {t["index"]: t["name"] for t in tracks}
    check("only_used filters empties", len(tracks) < 10, len(tracks))
    check("master present", names.get(0) == "Master", names)
    check("vocal track present", names.get(3) == "Lead Vox", names)
    vox = [t for t in tracks if t["index"] == 3][0]
    check("vocal plugins found", len(vox["plugins"]) == 3, vox["plugins"])
    check("plugin slots correct", [p["slot"] for p in vox["plugins"]] == [0, 1, 3],
          vox["plugins"])
    check("EQ2 named", vox["plugins"][0]["name"] == "Fruity Parametric EQ 2", vox)
    check("volume in dB present", vox["volume_db"] is not None, vox)

    r = c.call("mixer.list", only_used=False)
    check("only_used=False returns all", len(r["result"]["tracks"]) == 126,
          len(r["result"]["tracks"]))

    print("\n-- only_used ignores FL's default track names --")
    # FL names every empty mixer track "Insert N", so treating any name as a
    # sign of use returned all 127 tracks on the real project.
    r = c.call("mixer.list")
    kept = {t["index"] for t in r["result"]["tracks"]}
    check("default-named empty tracks filtered out",
          10 not in kept and 99 not in kept, sorted(kept)[:12])
    check("master always kept", 0 in kept, sorted(kept)[:12])
    check("user-named tracks kept", {3, 4} <= kept, sorted(kept)[:12])
    check("track carrying plugins kept", 5 in kept, sorted(kept)[:12])
    check("result is a short list, not the whole mixer",
          len(kept) < 12, len(kept))
    check("_has_custom_name rejects FL defaults",
          not bridge._has_custom_name("Insert 42")
          and not bridge._has_custom_name("Master")
          and not bridge._has_custom_name("  "),
          "default names leaking through")
    check("_has_custom_name accepts real names",
          bridge._has_custom_name("Custom Track")
          and bridge._has_custom_name("Insert Coin"),
          "real names rejected")

    print("\n-- VST padding parameters --")
    # FL reports a fixed padded count for VST plugins; the real ones carry
    # a name or a meaningful display, the rest are blank filler.
    r = c.call("plugin.params", track=5, slot=0, limit=240)
    res = r["result"]
    check("padding skipped by default", res["returned"] == 4, res["returned"])
    check("padding counted", res["padding_skipped"] == 236,
          res["padding_skipped"])
    check("real param count still reported", res["param_count"] == 240,
          res["param_count"])
    names = [p["name"] for p in res["params"]]
    check("unnamed but meaningful param kept",
          res["params"][0]["display"] == "Auto mode", res["params"][0])
    check("named params kept", names[1:] == ["Scale", "Key", "Tune Speed"],
          names)

    r = c.call("plugin.params", track=5, slot=0, limit=240, skip_padding=False)
    check("padding included on request",
          r["result"]["returned"] == 240, r["result"]["returned"])

    check_scan_params(c)

    print("\n-- track detail --")
    r = c.call("mixer.track", track=3)
    check("mixer.track ok", r["ok"], r)
    check("eq bands returned", len(r["result"]["eq"]["bands"]) == 3, r["result"]["eq"])
    check("routes returned", r["result"]["routes"][0]["to"] == 0, r["result"]["routes"])

    print("\n-- plugin params --")
    r = c.call("plugin.params", track=3, slot=1)
    check("plugin.params ok", r["ok"], r)
    check("compressor param count", r["result"]["param_count"] == 6, r["result"])
    check("param names present",
          r["result"]["params"][0]["name"] == "Threshold", r["result"]["params"][0])
    check("display string present",
          "%" in r["result"]["params"][0]["display"], r["result"]["params"][0])

    r = c.call("plugin.params", track=3, slot=1, filter="rat")
    check("param filter works", len(r["result"]["params"]) == 1, r["result"]["params"])

    check_lean_writes(c)

    print("\n-- channels and dispatch errors --")
    r = c.call("channels.list")
    check("channels.list ok", r["ok"] and r["result"]["channel_count"] == 3, r)
    r = c.call("call", module="mixer", function="getTrackName", args=[3])
    check("generic calls are rejected", not r["ok"], r)
    r = c.call("bogus.command")
    check("unknown command lists only read options",
          not r["ok"] and set(r["available"]) == set(bridge.READ_ONLY_COMMANDS),
          r)

    print("\n-- large payload across ticks --")
    r = c.call("mixer.list", only_used=False, peaks=True)
    check("large reply fully drained", r["ok"] and len(r["result"]["tracks"]) == 126,
          len(r["result"].get("tracks", [])))

    print("\n-- malformed input --")
    c.sock.sendall(b"this is not json\n")
    deadline = time.time() + 2
    got = None
    while time.time() < deadline and got is None:
        bridge.OnIdle()
        try:
            chunk = c.sock.recv(65536)
            if chunk:
                c.buf += chunk
        except (BlockingIOError, OSError):
            pass
        if b"\n" in c.buf:
            line, c.buf = c.buf.split(b"\n", 1)
            got = json.loads(line.decode())
        time.sleep(0.005)
    check("bad JSON handled without crash", got is not None and not got["ok"], got)
    check("bridge still alive after bad input", c.call("ping")["ok"])

    print("\n-- disconnect handling --")
    c.sock.close()
    # Reaping needs the kernel to surface EOF on the server side, which is not
    # guaranteed to have happened by the time close() returns on the client.
    # A fixed spin of ticks makes this a race that a loaded machine loses, so
    # poll to a deadline the way the malformed-input check above does.
    deadline = time.time() + 2
    while time.time() < deadline and bridge._transport.clients:
        bridge.OnIdle()
        time.sleep(0.005)
    check("client reaped on disconnect",
          len(bridge._transport.clients) == 0,
          len(bridge._transport.clients))
    c2 = Client(port)
    check("reconnect works", c2.call("ping")["ok"])
    c2.sock.close()
    for _ in range(5):
        bridge.OnIdle()

    bridge.OnDeInit()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
