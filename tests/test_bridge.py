"""Exercise the FL Studio bridge outside FL, against stub API modules.

Imports `device_UniversalBridge` with `tests/fakefl` shadowing the real FL API,
drives `OnIdle` the way FL would, and talks to it over a real TCP socket.
"""

import json
import hashlib
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "fakefl"))
sys.path.insert(0, os.path.join(ROOT, "fl_studio_mcp", "_bridge"))

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

    path = os.path.join(ROOT, "fl_studio_mcp", "_bridge", "device_UniversalBridge.py")
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

    path = os.path.join(ROOT, "fl_studio_mcp", "_bridge", "device_UniversalBridge.py")
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

    By path, not as a normal connector import: this suite puts tests/fakefl on
    sys.path so the bridge finds stub FL modules. The client now shares the
    standard-library-only host_config module, so expose the repository root
    only while that dependency is resolved.
    """
    import importlib.util

    path = os.path.join(ROOT, "fl_studio_mcp", "bridge_client.py")
    spec = importlib.util.spec_from_file_location(
        "fl_bridge_client_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, ROOT)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(ROOT)
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
            module._idle_tick += 1
            module._cleanup_active_notes()
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
            module._idle_tick += 1
            module._cleanup_active_notes()
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
    path = os.path.join(ROOT, "fl_studio_mcp", "_bridge", "device_UniversalBridge.py")
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
    published_writes = frozenset({
              "mixer.set_volume", "mixer.set_pan", "mixer.set_mute",
              "mixer.set_eq", "mixer.set_name", "mixer.set_send",
              "mixer.set_send_level",
              "plugin.set_param", "plugin.set_param_display",
              "plugin.set_param_option",
              "transport.set_playing", "transport.stop",
              "transport.set_song_position", "transport.set_loop_mode",
              "transport.set_tempo", "channel.set_mix",
              "channel.set_identity", "channel.route_to_mixer",
              "sequencer.set", "channel.trigger_note",
          })
    check("the write surface is exactly the published set",
          w.LEAN_WRITE_COMMANDS == published_writes,
          sorted(w.LEAN_WRITE_COMMANDS))
    check("the dispatcher contains exactly reads and verified writes",
          set(w.HANDLERS) == set(w.READ_ONLY_COMMANDS | w.LEAN_WRITE_COMMANDS),
          sorted(w.HANDLERS))
    malformed_requests = [
        w._dispatch([]),
        w._dispatch({"id": 44, "cmd": "ping", "args": []}),
    ]
    check("dispatcher rejects non-object requests and argument envelopes",
          all(not response["ok"] and "JSON object" in response["error"]
              for response in malformed_requests), malformed_requests)
    check("dispatcher remains healthy after malformed envelopes",
          dispatch(w, "ping")["ok"])

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
        ("transport.set_playing", {"playing": True}),
        ("transport.stop", {"playing": False, "position": 0.0}),
        ("transport.set_song_position", {"position": 0.5}),
        ("transport.set_loop_mode", {"loop_mode": "song"}),
        ("transport.set_tempo", {"tempo_bpm": 120.0}),
        ("channel.set_mix", {"channel": 0, "volume": 0.5}),
        ("channel.set_identity", {"channel": 0, "name": "gated"}),
        ("channel.route_to_mixer", {"channel": 0, "destination": 5}),
        ("sequencer.set", {"pattern": 1, "channel": 0,
                           "expected_digest": "0" * 64,
                           "updates": [{"step_index": 0, "enabled": True}]}),
        ("channel.trigger_note", {"channel": 0, "note": 60,
                                  "velocity": 100}),
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
    session_fingerprint = r["result"].get("session_fingerprint")
    check("ping carries one bounded lowercase-hex session fingerprint",
          isinstance(session_fingerprint, str)
          and len(session_fingerprint) == 32
          and all(ch in "0123456789abcdef" for ch in session_fingerprint),
          session_fingerprint)
    check("the session fingerprint is stable for one loaded bridge",
          dispatch(w, "ping")["result"].get("session_fingerprint")
          == session_fingerprint)
    another = _load_bridge_with_writes()
    check("a separately loaded bridge gets a different session fingerprint",
          dispatch(another, "ping")["result"].get("session_fingerprint")
          != session_fingerprint)
    check("reads still work with writes enabled", dispatch(w, "project.info")["ok"])

    print("\n-- write preconditions are enforced inside the bridge --")
    _state.reset()
    before_volume = _state.TRACKS[3].volume
    before_undo = list(_state.UNDO)
    stale_session = "f" * 32
    if stale_session == session_fingerprint:
        stale_session = "e" * 32
    rejected = dispatch(w, "mixer.set_volume", track=3, value=0.5,
                        session_fingerprint=stale_session)
    check("a stale session is refused before mutation",
          not rejected["ok"] and "session" in rejected.get("error", "").lower(),
          rejected)
    check("a stale session creates no undo point and changes nothing",
          _state.TRACKS[3].volume == before_volume and _state.UNDO == before_undo,
          (_state.TRACKS[3].volume, _state.UNDO))

    rejected = dispatch(w, "mixer.set_volume", track=3, value=0.5,
                        session_fingerprint=session_fingerprint,
                        expected_before=0.1)
    check("a stale before-state is refused before mutation",
          not rejected["ok"] and "expected_before" in rejected.get("error", ""),
          rejected)
    check("a stale before-state creates no undo point and changes nothing",
          _state.TRACKS[3].volume == before_volume and _state.UNDO == before_undo,
          (_state.TRACKS[3].volume, _state.UNDO))

    accepted = dispatch(w, "mixer.set_volume", track=3, value=0.5,
                        session_fingerprint=session_fingerprint,
                        expected_before=before_volume)
    check("matching preconditions are reported explicitly",
          accepted["ok"]
          and accepted["result"]["session_fingerprint"] == session_fingerprint
          and accepted["result"]["session_precondition_applied"] is True
          and accepted["result"]["expected_before_applied"] is True,
          accepted)
    _state.reset()

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

    _state.reset()
    compressor = _state.TRACKS[3].slots[1]
    compressor.param_names[2] = "Attack Time"
    compressor.param_names[3] = "Attack Curve"
    before_values = list(compressor.values)
    before_undo = list(_state.UNDO)
    r = dispatch(w, "plugin.set_param_display",
                 track=3, slot=1, param="Attack", target=30.0)
    error = r.get("error", "").lower()
    check("an ambiguous substring is refused and names both candidates",
          not r["ok"] and "ambiguous" in error
          and "attack time" in error and "attack curve" in error,
          r)
    check("an ambiguous match directs the caller to an index",
          "index" in error, r)
    check("ambiguity is detected before writes or undo points",
          compressor.values == before_values and _state.UNDO == before_undo,
          (compressor.values, _state.UNDO))
    r = dispatch(w, "plugin.set_param_display",
                 track=3, slot=1, param=2, target=30.0)
    check("a parameter index disambiguates the same controls",
          r["ok"] and r["result"]["index"] == 2
          and r["result"]["matched_on"] == "index", r)

    try:
        w._one_parameter_match(
            "attack", [(i, "attack %d" % i) for i in range(10)],
            "name_substring")
        bounded_error = ""
    except ValueError as exc:
        bounded_error = str(exc)
    check("ambiguity diagnostics bound the candidate list",
          "and 2 more" in bounded_error and "attack 7" in bounded_error
          and "attack 8" not in bounded_error,
          bounded_error)

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

    print("\n-- malformed raw writes fail closed before undo or mutation --")

    def track_a_state():
        """Snapshot every field reachable by the original ten write commands."""
        return repr([
            (
                track.name,
                track.volume,
                track.pan,
                track.muted,
                tuple(sorted(track.routes.items())),
                tuple(
                    (band["gain"], band["freq"], band["bw"])
                    for band in track.eq
                ),
                tuple(
                    (slot, tuple(plugin.values))
                    for slot, plugin in sorted(track.slots.items())
                ),
            )
            for track in _state.TRACKS
        ])

    malformed_writes = (
        (
            "a string false cannot authorize Master",
            "mixer.set_volume",
            {"track": 0, "value": 0.51, "allow_master": "false"},
        ),
        (
            "a boolean is not a pan track index",
            "mixer.set_pan",
            {"track": True, "value": 0.25},
        ),
        (
            "a string false is not an unmuted state",
            "mixer.set_mute",
            {"track": 3, "muted": "false"},
        ),
        (
            "a boolean is not an EQ band index",
            "mixer.set_eq",
            {"track": 3, "band": True, "gain": 0.6},
        ),
        (
            "a numeric track name is not text",
            "mixer.set_name",
            {"track": 3, "name": 123},
        ),
        (
            "a string false is not a disabled send state",
            "mixer.set_send",
            {"track": 3, "to": 5, "enabled": "false"},
        ),
        (
            "a boolean is not a send destination index",
            "mixer.set_send_level",
            {"track": 3, "to": True, "value": 0.4},
        ),
        (
            "a boolean is not a plug-in parameter index",
            "plugin.set_param",
            {"track": 3, "slot": 1, "index": True, "value": 0.4},
        ),
        (
            "NaN is not a displayed parameter target",
            "plugin.set_param_display",
            {"track": 3, "slot": 1, "param": 0, "target": float("nan")},
        ),
        (
            "a boolean is not an option sweep step count",
            "plugin.set_param_option",
            {"track": 9, "slot": 0, "param": "Key", "option": "A", "steps": True},
        ),
        (
            "NaN is not a normalized mixer value",
            "mixer.set_volume",
            {"track": 3, "value": float("nan")},
        ),
        (
            "infinity is not a normalized mixer value",
            "mixer.set_pan",
            {"track": 3, "value": float("inf")},
        ),
        (
            "negative infinity is not a normalized EQ value",
            "mixer.set_eq",
            {"track": 3, "band": 1, "gain": float("-inf")},
        ),
        (
            "infinity is not a normalized send level",
            "mixer.set_send_level",
            {"track": 3, "to": 0, "value": float("inf")},
        ),
        (
            "infinity is not a normalized plug-in value",
            "plugin.set_param",
            {"track": 3, "slot": 1, "index": 0, "value": float("inf")},
        ),
        (
            "infinity is not a display-search tolerance",
            "plugin.set_param_display",
            {
                "track": 3,
                "slot": 1,
                "param": 0,
                "target": 20.0,
                "tolerance": float("inf"),
            },
        ),
        (
            "NaN is not a numeric expected-before guard",
            "mixer.set_volume",
            {"track": 3, "value": 0.5, "expected_before": float("nan")},
        ),
    )
    for label, cmd, args in malformed_writes:
        before_state = track_a_state()
        before_undo = list(_state.UNDO)
        r = dispatch(w, cmd, **args)
        check(label + " is refused", not r["ok"], r)
        check(
            label + " leaves state and undo untouched",
            track_a_state() == before_state and _state.UNDO == before_undo,
            r,
        )

    check("no verified write saved the project", not saves, saves)


def check_track_b():
    """Track B against stateful fake FL APIs, including failure paths."""
    import channels as fake_channels
    import mixer as fake_mixer
    import patterns as fake_patterns
    import plugins as fake_plugins
    import transport as fake_transport

    _state.reset()
    w = _load_bridge_with_writes()
    session = dispatch(w, "ping")["result"]["session_fingerprint"]
    channel_scopes = []
    color_writes = []
    plugin_scopes = []
    grid_writes = []
    notes = []
    timed_notes = []
    current_pattern = [2]
    pattern_length = [16]
    grids = {(2, i): [False] * 16 for i in range(len(_state.CHANNELS))}
    grids[(2, 1)][0] = True
    grids[(2, 1)][4] = True
    generator = _state.Plugin(
        "Persistent Generator",
        [("Cutoff", 0.2), ("Mode", 0.0), ("Drive", 0.4)],
    )
    generator.enums = {1: ["Clean", "Warm", "Drive"]}
    generator.reported = {}

    channel_names = (
        "channelCount", "getChannelName", "getChannelVolume",
        "setChannelVolume", "getChannelPan", "setChannelPan",
        "isChannelMuted", "muteChannel", "isChannelSolo",
        "isChannelSelected", "getChannelType", "getChannelColor",
        "setChannelColor", "setChannelName", "getTargetFxTrack",
        "setTargetFxTrack",
    )
    saved_channels = {name: getattr(fake_channels, name) for name in channel_names}
    saved_channel_optional = {
        name: getattr(fake_channels, name, None)
        for name in ("getGridBit", "setGridBit", "midiNoteOn")
    }
    saved_patterns = {
        name: getattr(fake_patterns, name, None)
        for name in ("patternNumber", "getPatternLength")
    }
    saved_transport_loop = getattr(fake_transport, "setLoopMode", None)
    saved_tempo = getattr(fake_mixer, "setCurrentTempo", None)
    plugin_names = (
        "_get", "isValid", "getPluginName", "getParamCount",
        "getParamName", "getParamValue", "getParamValueString",
        "setParamValue",
    )
    saved_plugins = {name: getattr(fake_plugins, name) for name in plugin_names}

    def channel_count(global_count=False):
        channel_scopes.append(("channelCount", bool(global_count)))
        return saved_channels["channelCount"](global_count)

    def one_scope(name, index, use_global=False):
        channel_scopes.append((name, bool(use_global)))
        return saved_channels[name](index, use_global)

    def get_volume(index, mode=False, use_global=False):
        channel_scopes.append(("getChannelVolume", bool(use_global)))
        return saved_channels["getChannelVolume"](index, mode, use_global)

    def set_volume(index, value, pickup=-1, use_global=False):
        channel_scopes.append(("setChannelVolume", bool(use_global)))
        return saved_channels["setChannelVolume"](index, value, pickup, use_global)

    def set_pan(index, value, pickup=-1, use_global=False):
        channel_scopes.append(("setChannelPan", bool(use_global)))
        return saved_channels["setChannelPan"](index, value, pickup, use_global)

    def mute(index, value=-1, use_global=False):
        channel_scopes.append(("muteChannel", bool(use_global)))
        return saved_channels["muteChannel"](index, value, use_global)

    def set_name(index, value, use_global=False):
        channel_scopes.append(("setChannelName", bool(use_global)))
        return saved_channels["setChannelName"](index, value, use_global)

    def set_color(index, value, use_global=False):
        channel_scopes.append(("setChannelColor", bool(use_global)))
        color_writes.append(value)
        return saved_channels["setChannelColor"](index, value, use_global)

    def set_route(index, value, use_global=False):
        channel_scopes.append(("setTargetFxTrack", bool(use_global)))
        return saved_channels["setTargetFxTrack"](index, value, use_global)

    def get_grid(index, position, use_global=False):
        channel_scopes.append(("getGridBit", bool(use_global)))
        return int(grids[(current_pattern[0], index)][position])

    def set_grid(index, position, value, use_global=False):
        channel_scopes.append(("setGridBit", bool(use_global)))
        grid_writes.append(position)
        grids[(current_pattern[0], index)][position] = bool(value)

    def midi_note(index, note, velocity, midi_channel=-1):
        notes.append((index, note, velocity, midi_channel))
        timed_notes.append(
            (w._idle_tick, index, note, velocity, midi_channel)
        )

    fake_channels.channelCount = channel_count
    fake_channels.getChannelVolume = get_volume
    fake_channels.setChannelVolume = set_volume
    fake_channels.setChannelPan = set_pan
    fake_channels.muteChannel = mute
    fake_channels.setChannelName = set_name
    fake_channels.setChannelColor = set_color
    fake_channels.setTargetFxTrack = set_route
    for name in (
        "getChannelName", "getChannelPan", "isChannelMuted",
        "isChannelSolo", "isChannelSelected", "getChannelType",
        "getChannelColor", "getTargetFxTrack",
    ):
        setattr(
            fake_channels, name,
            (lambda method: lambda index, use_global=False:
             one_scope(method, index, use_global))(name),
        )
    fake_channels.getGridBit = get_grid
    fake_channels.setGridBit = set_grid
    fake_channels.midiNoteOn = midi_note
    fake_patterns.patternNumber = lambda: current_pattern[0]
    fake_patterns.getPatternLength = lambda pattern: pattern_length[0]
    fake_transport.setLoopMode = lambda: setattr(
        _state, "LOOP_MODE", 1 - _state.LOOP_MODE
    )
    tempo_calls = []

    def set_tempo(value, as_int=False):
        tempo_calls.append((value, as_int))
        _state.TEMPO = float(value)

    fake_mixer.setCurrentTempo = set_tempo

    def plugin_get(index, slot=-1):
        if index == 2 and slot == -1:
            return generator
        return saved_plugins["_get"](index, slot)

    def plugin_valid(index, slot=-1, use_global=False):
        plugin_scopes.append(("isValid", index, slot, bool(use_global)))
        return plugin_get(index, slot) is not None

    def plugin_name(index, slot=-1, user_name=False, use_global=False):
        plugin_scopes.append(("getPluginName", index, slot, bool(use_global)))
        plug = plugin_get(index, slot)
        if plug is None:
            raise TypeError("no plugin")
        return plug.name

    def plugin_count(index, slot=-1, use_global=False):
        plugin_scopes.append(("getParamCount", index, slot, bool(use_global)))
        return len(plugin_get(index, slot).param_names)

    def plugin_param_name(param, index, slot=-1, use_global=False):
        plugin_scopes.append(("getParamName", index, slot, bool(use_global)))
        return plugin_get(index, slot).param_names[param]

    def plugin_value(param, index, slot=-1, use_global=False):
        plugin_scopes.append(("getParamValue", index, slot, bool(use_global)))
        return saved_plugins["getParamValue"](param, index, slot, use_global)

    def plugin_display(param, index, slot=-1, use_global=False):
        plugin_scopes.append(("getParamValueString", index, slot, bool(use_global)))
        return saved_plugins["getParamValueString"](
            param, index, slot, -1, use_global
        )

    def plugin_set(value, param, index, slot=-1, pickup=1, use_global=False):
        plugin_scopes.append(("setParamValue", index, slot, bool(use_global)))
        return saved_plugins["setParamValue"](
            value, param, index, slot, pickup, use_global
        )

    fake_plugins._get = plugin_get
    fake_plugins.isValid = plugin_valid
    fake_plugins.getPluginName = plugin_name
    fake_plugins.getParamCount = plugin_count
    fake_plugins.getParamName = plugin_param_name
    fake_plugins.getParamValue = plugin_value
    fake_plugins.getParamValueString = plugin_display
    fake_plugins.setParamValue = plugin_set

    try:
        print("\n-- Track B uses only the explicit published surface --")
        check("playback speed remains omitted",
              "transport.set_playback_speed" not in w.LEAN_WRITE_COMMANDS
              and "transport.set_playback_speed" not in w.HANDLERS)
        ro = _load_bridge_read_only()
        sequence_read = dispatch(
            ro, "sequencer.get", pattern=2, channel=1, index_scope="global"
        )
        check("sequencer.get remains available with writes disabled",
              sequence_read["ok"], sequence_read)

        print("\n-- Track B transport is absolute and later-tick verified --")
        r, yields = drive(
            w, "transport.set_playing", playing=True,
            session_fingerprint=session, expected_before={"playing": False}
        )
        check("absolute play starts and verifies on a later tick",
              r["ok"] and yields == 1 and r["result"]["verified"]
              and r["result"]["after"] is True, (r, yields))
        before_undo = list(_state.UNDO)
        stale = dispatch(
            w, "transport.set_playing", playing=False,
            expected_before={"playing": False}
        )
        check("stale transport guard refuses without undo or mutation",
              not stale["ok"] and _state.PLAYING is True
              and _state.UNDO == before_undo, stale)
        refused = dispatch(w, "transport.set_song_position", position=0.5)
        check("song position refuses while playing",
              not refused["ok"] and _state.SONG_POS == 0.0, refused)
        dispatch(w, "transport.set_playing", playing=False)
        r, yields = drive(
            w, "transport.set_song_position", position=0.375, tolerance=0.0001,
            expected_before={"song_position_normalized": 0.0}
        )
        check("absolute song position verifies after a yield",
              r["ok"] and yields == 1 and r["result"]["verified"]
              and r["result"]["after"] == 0.375, (r, yields))
        # FL stores the playhead on its absolute-tick grid even when
        # setSongPos receives a normalized float.  A 959-tick fixture makes
        # normalized 0.5 land exactly half-way between ticks; FL chooses tick
        # 480, whose normalized readback is 480/959.  Keep the API's caller-
        # supplied tolerance honest and prove that this short reviewed fixture
        # needs an explicit grid-sized tolerance rather than a looser global
        # default.
        real_set_song_position = fake_transport.setSongPos
        real_song_length_ticks = _state.SONG_LENGTH_TICKS

        def quantized_song_position(position, mode=-1):
            length = _state.SONG_LENGTH_TICKS
            if mode == 2:
                tick = int(float(position))
            else:
                tick = int(float(position) * length + 0.5)
            _state.SONG_POS = tick / float(length)

        fake_transport.setSongPos = quantized_song_position
        _state.SONG_LENGTH_TICKS = 959
        representable_before = 288.0 / 959.0
        quantized_half = 480.0 / 959.0
        try:
            _state.SONG_POS = representable_before
            grid_aware, yields = drive(
                w, "transport.set_song_position", position=0.5,
                tolerance=0.001,
                expected_before={
                    "song_position_normalized": representable_before
                },
            )
            check("explicit short-song tolerance accepts nearest FL tick",
                  grid_aware["ok"] and yields == 1
                  and grid_aware["result"]["verified"]
                  and grid_aware["result"]["after"] == quantized_half
                  and grid_aware["result"]["after"] - 0.5 > 0.0001,
                  (grid_aware, yields))

            _state.SONG_POS = representable_before
            too_strict, yields = drive(
                w, "transport.set_song_position", position=0.5,
                tolerance=0.0001,
            )
            check("default tolerance still reports half-tick miss honestly",
                  too_strict["ok"] and yields == 1
                  and not too_strict["result"]["verified"]
                  and too_strict["result"]["after"] == quantized_half,
                  (too_strict, yields))

            restored, yields = drive(
                w, "transport.set_song_position",
                position=representable_before, tolerance=0.0001,
            )
            check("captured representable position still restores exactly",
                  restored["ok"] and yields == 1
                  and restored["result"]["verified"]
                  and restored["result"]["after"] == representable_before,
                  (restored, yields))
        finally:
            fake_transport.setSongPos = real_set_song_position
            _state.SONG_LENGTH_TICKS = real_song_length_ticks
            _state.SONG_POS = 0.375
        r, yields = drive(
            w, "transport.set_loop_mode", loop_mode="song",
            expected_before={"loop_mode": "pattern"}
        )
        check("toggle-only loop API is exposed as an absolute state",
              r["ok"] and yields == 1 and r["result"]["verified"]
              and r["result"]["before"] == "pattern"
              and r["result"]["after"] == "song", (r, yields))
        _state.PLAYING = True
        tempo_before = _state.TEMPO
        undo_before = list(_state.UNDO)
        refused = dispatch(w, "transport.set_tempo", tempo_bpm=128.5)
        check("tempo refuses while playing before undo or mutation",
              not refused["ok"] and _state.TEMPO == tempo_before
              and _state.UNDO == undo_before, refused)
        _state.PLAYING = False
        r, yields = drive(
            w, "transport.set_tempo", tempo_bpm=128.5,
            expected_before={"tempo_bpm": 140.0}
        )
        check("tempo uses Image-Line's explicit BPM-unit setter mode",
              r["ok"] and yields == 1 and r["result"]["verified"]
              and _state.TEMPO == 128.5
              and tempo_calls[-1] == (128.5, True),
              (r, yields, tempo_calls))
        _state.RECORDING = True
        tempo_before = _state.TEMPO
        undo_before = list(_state.UNDO)
        refused = dispatch(w, "transport.set_tempo", tempo_bpm=100.0)
        check("tempo refuses while recording before undo or mutation",
              not refused["ok"] and _state.TEMPO == tempo_before
              and _state.UNDO == undo_before, refused)
        _state.RECORDING = False
        _state.PLAYING = True
        _state.SONG_POS = 0.8
        r, yields = drive(w, "transport.stop", playing=False, position=0.0)
        check("stop requests and verifies both absolute fields",
              r["ok"] and yields == 1 and r["result"]["verified"]
              and r["result"]["verified_fields"]
              == {"playing": True, "position": True}, (r, yields))
        real_song_position = fake_transport.setSongPos
        fake_transport.setSongPos = lambda _position: None
        _state.PLAYING = True
        _state.SONG_POS = 0.6
        try:
            partial_stop = dispatch(
                w, "transport.stop", playing=False, position=0.0
            )
        finally:
            fake_transport.setSongPos = real_song_position
        check("stop aggregate is the AND of play and position proof",
              partial_stop["ok"] and not partial_stop["result"]["verified"]
              and partial_stop["result"]["verified_fields"]
              == {"playing": True, "position": False}, partial_stop)
        _state.SONG_POS = 0.0

        stop_rejections = []
        for invalid in (False, "0.0", float("nan"), float("inf"), 0.25):
            _state.PLAYING = True
            _state.SONG_POS = 0.7
            state_before = (_state.PLAYING, _state.SONG_POS, list(_state.UNDO))
            rejected = dispatch(
                w, "transport.stop", playing=False, position=invalid
            )
            state_after = (_state.PLAYING, _state.SONG_POS, list(_state.UNDO))
            stop_rejections.append((invalid, rejected, state_before == state_after))
        extra = dispatch(
            w, "transport.stop", playing=False, position=0.0, toggle=True
        )
        check("stop rejects coerced, non-finite, nonzero, and unknown inputs",
              all(not row[1]["ok"] and row[2] for row in stop_rejections)
              and not extra["ok"], (stop_rejections, extra))
        _state.PLAYING = False
        _state.SONG_POS = 0.0

        print("\n-- global Channel Rack identity and guarded writes --")
        listing = dispatch(w, "channels.list", global_count=True)["result"]
        first = listing["channels"][0]
        check("channel list is globally addressed with strong identity",
              listing["command"] == "channels.list"
              and all(row["index_scope"] == "global" for row in listing["channels"])
              and len(first["channel_fingerprint"]) == 64, listing)
        check("channel fingerprint is stable for an unchanged observation",
              dispatch(w, "channels.list", global_count=True)["result"]
              ["channels"][0]["channel_fingerprint"]
              == first["channel_fingerprint"])

        # FL can expose the 0x--BBGGRR word through Python's signed integer
        # spelling. The public bridge contract is unsigned so JSON, guards,
        # fingerprints, and restore payloads have one canonical form.
        saved_color = _state.CHANNELS[1].color
        live_signed_color = -13880523       # same bits as 0xFF2C3335
        baseline_color = 0xFF2C3335
        alternate_color = 0xFE102030
        alternate_signed = alternate_color - (1 << 32)
        _state.CHANNELS[1].color = live_signed_color
        signed_listing = dispatch(
            w, "channels.list", global_count=True
        )["result"]["channels"][1]
        canonical_material = {
            "channel_index": 1,
            "channel_type_code": signed_listing["type"],
            "color": baseline_color,
            "generator_name": signed_listing["plugin"] or None,
            "mixer_destination": signed_listing["mixer_track"],
            "name": signed_listing["name"],
            "scope": "global",
        }
        canonical_fingerprint = hashlib.sha256(json.dumps(
            canonical_material, ensure_ascii=True,
            separators=(",", ":"), sort_keys=True
        ).encode("ascii")).hexdigest()
        check("signed FL channel colors list as canonical unsigned words",
              signed_listing["color"] == baseline_color
              and signed_listing["channel_fingerprint"] == canonical_fingerprint,
              signed_listing)

        undo_at = len(_state.UNDO)
        changed = dispatch(
            w, "channel.set_identity", channel=1, index_scope="global",
            color=alternate_color,
            expected_before={
                "channel_fingerprint": canonical_fingerprint,
                "color": baseline_color,
            },
        )
        changed_row = dispatch(
            w, "channels.list", global_count=True
        )["result"]["channels"][1]
        restored = dispatch(
            w, "channel.set_identity", channel=1, index_scope="global",
            color=baseline_color,
            expected_before={
                "channel_fingerprint": changed_row["channel_fingerprint"],
                "color": alternate_color,
            },
        )
        final_row = dispatch(
            w, "channels.list", global_count=True
        )["result"]["channels"][1]
        check("high-bit channel color writes verify through signed FL readback",
              changed["ok"] and changed["result"]["verified"]
              and changed["result"]["before"]["color"] == baseline_color
              and changed["result"]["after"]["color"] == alternate_color
              and color_writes[-2] == alternate_signed,
              (changed, color_writes[-2:]))
        check("canonical color baseline restores exactly with a guarded write",
              restored["ok"] and restored["result"]["verified"]
              and restored["result"]["after"]["color"] == baseline_color
              and color_writes[-1] == live_signed_color
              and final_row["color"] == baseline_color
              and final_row["channel_fingerprint"] == canonical_fingerprint
              and _state.CHANNELS[1].color == live_signed_color
              and len(_state.UNDO) == undo_at + 2,
              (restored, final_row, color_writes[-2:]))

        # FL owns the high byte of its documented 0x--BBGGRR color word. A
        # low-24-bit request can therefore read back with 0xFF in that byte;
        # guards and proof must compare controllable color bits, while the
        # observation still preserves the exact returned 32-bit word.
        def set_opaque_high_byte(index, value, use_global=False):
            channel_scopes.append(("setChannelColor", bool(use_global)))
            color_writes.append(value)
            word = value & 0xFFFFFFFF
            observed = 0xFF000000 | (word & 0x00FFFFFF)
            _state.CHANNELS[index].color = observed - (1 << 32)

        fake_channels.setChannelColor = set_opaque_high_byte
        opaque_undo_at = len(_state.UNDO)
        opaque_changed = dispatch(
            w, "channel.set_identity", channel=1, index_scope="global",
            color=0x0055AA,
            expected_before={"color": baseline_color & 0x00FFFFFF},
        )
        opaque_restored = dispatch(
            w, "channel.set_identity", channel=1, index_scope="global",
            color=baseline_color & 0x00FFFFFF,
            expected_before={"color": 0x0055AA},
        )
        check("FL-owned color high byte is ignored by guards and later proof",
              opaque_changed["ok"] and opaque_changed["result"]["verified"]
              and opaque_changed["result"]["requested"]["color"] == 0x0055AA
              and opaque_changed["result"]["after"]["color"] == 0xFF0055AA
              and opaque_restored["ok"]
              and opaque_restored["result"]["verified"]
              and opaque_restored["result"]["after"]["color"] == baseline_color
              and len(_state.UNDO) == opaque_undo_at + 2,
              (opaque_changed, opaque_restored))
        fake_channels.setChannelColor = set_color

        for invalid_color in (-1, 1 << 32, True):
            before_state = (_state.CHANNELS[1].color, list(_state.UNDO),
                            list(color_writes))
            refused = dispatch(
                w, "channel.set_identity", channel=1, index_scope="global",
                color=invalid_color,
            )
            check("non-canonical public color %r is refused before mutation"
                  % (invalid_color,),
                  not refused["ok"]
                  and (_state.CHANNELS[1].color, list(_state.UNDO),
                       list(color_writes)) == before_state,
                  refused)
        # Leave the shared fixture exactly as the older Channel Rack checks
        # expect; the signed baseline itself was already restored and proved.
        _state.CHANNELS[1].color = saved_color

        real_channel_count = fake_channels.channelCount
        fake_channels.channelCount = lambda global_count=False: 20
        try:
            large_listing, listing_yields = drive(
                w, "channels.list", global_count=True
            )
        finally:
            fake_channels.channelCount = real_channel_count
        check("large channel lists yield in bounded UI-thread chunks",
              large_listing["ok"] and listing_yields == 2
              and large_listing["result"]["channel_count"] == 20,
              (large_listing, listing_yields))
        ambiguous_scope = dispatch(
            w, "channel.set_mix", channel=0, volume=0.5
        )
        noninteger_channel = dispatch(
            w, "channel.set_mix", channel="0", index_scope="global", volume=0.5
        )
        check("channel writes refuse implicit scope and coerced indices",
              not ambiguous_scope["ok"] and not noninteger_channel["ok"],
              (ambiguous_scope, noninteger_channel))
        undo_at = len(_state.UNDO)
        r, yields = drive(
            w, "channel.set_mix", channel=0, index_scope="global",
            volume=0.44, pan=-0.25, muted=True,
            session_fingerprint=session,
            expected_before={
                "channel_fingerprint": first["channel_fingerprint"],
                "volume_normalized": 0.78, "pan": 0.0, "muted": False,
            },
        )
        check("multi-field channel mix verifies every requested field",
              r["ok"] and yields == 1 and r["result"]["verified"]
              and r["result"]["verified_fields"]
              == {"volume": True, "pan": True, "muted": True}, (r, yields))
        first_mix_setters = [name for name, _global in channel_scopes
                             if name.startswith("setChannel") or name == "muteChannel"]
        check("channel mix issues each requested absolute setter exactly once",
              first_mix_setters[-3:] == [
                  "setChannelVolume", "setChannelPan", "muteChannel"
              ], first_mix_setters)
        check("channel mix creates one undo point",
              len(_state.UNDO) == undo_at + 1, _state.UNDO[-2:])
        scoped_pan = fake_channels.setChannelPan
        fake_channels.setChannelPan = lambda *args, **kwargs: None
        try:
            r = dispatch(
                w, "channel.set_mix", channel=0, index_scope="global",
                volume=0.5, pan=0.5
            )
        finally:
            fake_channels.setChannelPan = scoped_pan
        check("channel mix aggregate is AND, never OR",
              r["ok"] and r["result"]["verified"] is False
              and r["result"]["verified_fields"]
              == {"volume": True, "pan": False}, r)

        fingerprint = dispatch(w, "channels.list", global_count=True)["result"]
        fingerprint = fingerprint["channels"][1]["channel_fingerprint"]
        r = dispatch(
            w, "channel.set_identity", channel=1, index_scope="global",
            name="Kick Tight", color=0x123456,
            expected_before={"channel_fingerprint": fingerprint,
                             "name": "Kick", "color": 0x445566}
        )
        check("channel identity verifies name and color independently",
              r["ok"] and r["result"]["verified"]
              and r["result"]["verified_fields"]
              == {"name": True, "color": True}, r)
        scoped_color = fake_channels.setChannelColor
        fake_channels.setChannelColor = lambda *args, **kwargs: None
        try:
            r = dispatch(
                w, "channel.set_identity", channel=1, index_scope="global",
                name="Kick Two", color=0xABCDEF
            )
        finally:
            fake_channels.setChannelColor = scoped_color
        check("channel identity aggregate is AND, never OR",
              r["ok"] and not r["result"]["verified"]
              and r["result"]["verified_fields"]
              == {"name": True, "color": False}, r)
        r, yields = drive(
            w, "channel.route_to_mixer", channel=2, index_scope="global",
            destination=8
        )
        check("channel routing is isolated and readback verified",
              r["ok"] and yields == 1 and r["result"]["verified"]
              and r["result"]["after"]["mixer_destination"] == 8,
              (r, yields))
        real_track_info = fake_mixer.getTrackInfo
        fake_mixer.getTrackInfo = (
            lambda mode: len(_state.TRACKS) - 2
            if mode == w.midi.TN_LastIns else real_track_info(mode)
        )
        route_before = (_state.CHANNELS[2].target_fx, list(_state.UNDO))
        try:
            current_track = dispatch(
                w, "channel.route_to_mixer", channel=2,
                index_scope="global", destination=len(_state.TRACKS) - 1
            )
        finally:
            fake_mixer.getTrackInfo = real_track_info
        check("channel routing refuses FL's special Current utility track",
              not current_track["ok"]
              and (_state.CHANNELS[2].target_fx, list(_state.UNDO)) == route_before,
              current_track)
        state_before = (_state.CHANNELS[2].target_fx, list(_state.UNDO))
        r = dispatch(
            w, "channel.route_to_mixer", channel=2, index_scope="global",
            destination=9, expected_before={"channel_fingerprint": "0" * 64}
        )
        check("stale channel identity refuses before route mutation",
              not r["ok"]
              and (_state.CHANNELS[2].target_fx, list(_state.UNDO)) == state_before,
              r)

        print("\n-- current-pattern sequencer uses one canonical digest --")
        before = dispatch(
            w, "sequencer.get", pattern=2, channel=1, index_scope="global"
        )["result"]
        canonical = {
            "cells": [1 if value else 0 for value in before["cells"]],
            "channel_index": 1,
            "grid_resolution": "sixteenth_note",
            "pattern_number": 2,
            "step_count": 16,
        }
        digest = hashlib.sha256(json.dumps(
            canonical, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("ascii")).hexdigest()
        check("sequencer digest matches the public canonical JSON exactly",
              before["digest"] == digest, (before["digest"], digest))
        undo_at = len(_state.UNDO)
        grid_writes[:] = []
        r, yields = drive(
            w, "sequencer.set", pattern=2, channel=1, index_scope="global",
            expected_digest=digest,
            updates=[{"step_index": 0, "enabled": False},
                     {"step_index": 3, "enabled": True}]
        )
        check("step batch writes once and verifies absolute cell states",
              r["ok"] and yields >= 1 and r["result"]["verified"]
              and [cell["verified"] for cell in r["result"]["verified_cells"]]
              == [True, True]
              and grid_writes == [0, 3]
              and r["result"]["expected_before_applied"] is True, (r, yields))
        check("one step batch creates one undo point",
              len(_state.UNDO) == undo_at + 1, _state.UNDO[-2:])
        grid_before = list(grids[(2, 1)])
        undo_before = list(_state.UNDO)
        stale = dispatch(
            w, "sequencer.set", pattern=2, channel=1, index_scope="global",
            expected_digest=digest,
            updates=[{"step_index": 2, "enabled": True}]
        )
        check("concurrent grid edit conflicts without mutation or undo",
              not stale["ok"] and grids[(2, 1)] == grid_before
              and _state.UNDO == undo_before, stale)
        generic = dispatch(
            w, "sequencer.set", pattern=2, channel=1, index_scope="global",
            expected_digest=r["result"]["after"]["digest"],
            expected_before=None,
            updates=[{"step_index": 2, "enabled": True}]
        )
        check("sequencer rejects ambiguous generic expected_before",
              not generic["ok"] and grids[(2, 1)] == grid_before, generic)

        short_grid = list(grids[(2, 1)])
        pattern_length[0] = 128
        grids[(2, 1)][:] = short_grid + [False] * (128 - len(short_grid))
        long_before = dispatch(
            w, "sequencer.get", pattern=2, channel=1, index_scope="global"
        )["result"]
        undo_before = list(_state.UNDO)
        grid_writes[:] = []
        raced_job = w._dispatch({
            "id": 8, "cmd": "sequencer.set",
            "args": {
                "pattern": 2, "channel": 1, "index_scope": "global",
                "expected_digest": long_before["digest"],
                "updates": [{"step_index": 100, "enabled": True}],
            },
        })
        next(raced_job.gen)  # cells 0..63 have been observed; then it yields
        grids[(2, 1)][0] = not grids[(2, 1)][0]  # concurrent edit to an old chunk
        try:
            while True:
                next(raced_job.gen)
        except StopIteration as stopped:
            raced = {"ok": True, "result": stopped.value}
        except Exception as exc:
            raced = {"ok": False, "error": str(exc)}
        check("an early-cell race is rechecked immediately before the batch",
              not raced["ok"] and "immediately before" in raced["error"]
              and grids[(2, 1)][100] is False and grid_writes == []
              and _state.UNDO == undo_before, (raced, grid_writes, _state.UNDO))
        pattern_length[0] = 16
        grids[(2, 1)][:] = short_grid

        fresh = dispatch(
            w, "sequencer.get", pattern=2, channel=1, index_scope="global"
        )["result"]
        real_set_grid = fake_channels.setGridBit
        attempted_steps = []

        def fail_second_grid_write(index, position, value, use_global=False):
            attempted_steps.append(position)
            if len(attempted_steps) == 2:
                raise RuntimeError("simulated second-cell refusal")
            return real_set_grid(index, position, value, use_global)

        fake_channels.setGridBit = fail_second_grid_write
        partial_before = list(grids[(2, 1)])
        try:
            partial = dispatch(
                w, "sequencer.set", pattern=2, channel=1,
                index_scope="global", expected_digest=fresh["digest"],
                updates=[{"step_index": 5, "enabled": True},
                         {"step_index": 6, "enabled": True}]
            )
        finally:
            fake_channels.setGridBit = real_set_grid
        check("a partially refused batch is never retried",
              not partial["ok"] and attempted_steps == [5, 6]
              and grids[(2, 1)][5] is True and grids[(2, 1)][6] is False,
              (partial, attempted_steps, grids[(2, 1)]))
        grids[(2, 1)][:] = partial_before

        fresh = dispatch(
            w, "sequencer.get", pattern=2, channel=1, index_scope="global"
        )["result"]

        def ignore_grid_write(index, position, value, use_global=False):
            channel_scopes.append(("setGridBit", bool(use_global)))

        fake_channels.setGridBit = ignore_grid_write
        try:
            unverified = dispatch(
                w, "sequencer.set", pattern=2, channel=1,
                index_scope="global", expected_digest=fresh["digest"],
                updates=[{"step_index": 7, "enabled": True}]
            )
        finally:
            fake_channels.setGridBit = real_set_grid
        check("an ignored step write reports per-cell unverified",
              unverified["ok"] and not unverified["result"]["verified"]
              and unverified["result"]["verified_cells"] == [{
                  "step_index": 7, "requested_enabled": True,
                  "after_enabled": False, "verified": False,
              }], unverified)
        current_pattern[0] = 1
        refused = dispatch(w, "sequencer.get", pattern=2, channel=1,
                           index_scope="global")
        check("sequencer never switches the current pattern implicitly",
              not refused["ok"] and current_pattern[0] == 1, refused)
        current_pattern[0] = 2

        print("\n-- live note is dispatch-only with guaranteed cleanup --")
        target_fingerprint = dispatch(
            w, "channels.list", global_count=True
        )["result"]["channels"][2]["channel_fingerprint"]
        notes[:] = []
        r, yields = drive(
            w, "channel.trigger_note", channel=2, index_scope="global",
            note=64, velocity=100, duration_ms=40, midi_channel=3,
            expected_before={"channel_fingerprint": target_fingerprint}
        )
        check("note receipt claims dispatch only, never verification",
              r["ok"] and yields == 2 and r["result"]["dispatched"]
              and r["result"]["note_off_sent"]
              and "verified" not in r["result"], (r, yields))
        check("note-off is paired with the exact note-on target",
              notes == [(2, 64, 100, 3), (2, 64, 0, 3)], notes)
        check("completed notes leave no active-note cleanup debt",
              w._active_notes == [], w._active_notes)
        notes[:] = []
        job = w._dispatch({
            "id": 7, "cmd": "channel.trigger_note",
            "args": {"channel": 2, "index_scope": "global", "note": 67,
                     "velocity": 90, "duration_ms": 5000},
        })
        next(job.gen)
        job.gen.close()
        check("cancelled note jobs send note-off during cleanup",
              notes == [(2, 67, 90, -1), (2, 67, 0, -1)], notes)

        notes[:] = []
        first_same = w._dispatch({
            "id": 70, "cmd": "channel.trigger_note",
            "args": {"channel": 2, "index_scope": "global", "note": 70,
                     "velocity": 90, "duration_ms": 5000,
                     "midi_channel": 1},
        })
        next(first_same.gen)
        duplicate = dispatch(
            w, "channel.trigger_note", channel=2, index_scope="global",
            note=70, velocity=91, duration_ms=40, midi_channel=1
        )
        first_same.gen.close()
        check("overlapping identical auditions are refused before a second note-on",
              not duplicate["ok"]
              and notes == [(2, 70, 90, 1), (2, 70, 0, 1)]
              and w._active_notes == [], (duplicate, notes, w._active_notes))

        notes[:] = []
        timed_notes[:] = []
        concurrent = []

        class _LiveJobs:
            def alive(self, _handle):
                return True

            def respond(self, _handle, response):
                concurrent.append(response)

        saved_job_transport = w._transport
        first_job = w._dispatch({
            "id": 9, "cmd": "channel.trigger_note",
            "args": {"channel": 2, "index_scope": "global", "note": 70,
                     "velocity": 80, "duration_ms": 40},
        })
        second_job = w._dispatch({
            "id": 10, "cmd": "channel.trigger_note",
            "args": {"channel": 2, "index_scope": "global", "note": 71,
                     "velocity": 81, "duration_ms": 40},
        })
        w._transport = _LiveJobs()
        w._jobs[:] = [first_job, second_job]
        try:
            for _unused in range(4):
                w._idle_tick += 1
                w._cleanup_active_notes()
                w._advance_jobs()
        finally:
            for pending in list(w._jobs):
                pending.gen.close()
            w._jobs[:] = []
            w._transport = saved_job_transport
        note_ticks = {
            (note, velocity): tick
            for tick, _channel, note, velocity, _midi_channel in timed_notes
        }
        check("concurrent jobs do not multiply live-note duration",
              note_ticks[(70, 0)] - note_ticks[(70, 80)] == 2
              and note_ticks[(71, 0)] - note_ticks[(71, 81)] == 2
              and len(concurrent) == 2 and w._active_notes == [],
              (timed_notes, concurrent, w._active_notes))

        real_midi_note = fake_channels.midiNoteOn
        failed_note_offs = [0]

        def fail_first_note_off(index, note, velocity, midi_channel=-1):
            if velocity == 0 and failed_note_offs[0] == 0:
                failed_note_offs[0] += 1
                raise RuntimeError("simulated transient note-off refusal")
            return real_midi_note(index, note, velocity, midi_channel)

        fake_channels.midiNoteOn = fail_first_note_off
        failed_release = dispatch(
            w, "channel.trigger_note", channel=2, index_scope="global",
            note=69, velocity=80, duration_ms=20
        )
        fake_channels.midiNoteOn = real_midi_note
        check("a refused note-off remains registered for cleanup",
              failed_release["ok"]
              and failed_release["result"]["note_off_sent"] is False
              and len(w._active_notes) == 1, (failed_release, w._active_notes))
        w.OnDeInit()
        check("deinit retries and clears a previously refused note-off",
              w._active_notes == [] and notes[-1] == (2, 69, 0, -1),
              (notes, w._active_notes))

        print("\n-- generator targets preserve full plug-in guarantees --")
        plugin_scopes[:] = []
        ambiguous_generator = dispatch(
            w, "plugin.params", target_kind="channel_generator", channel=2,
            slot=-1, use_global_index=True
        )
        wrong_generator_slot = dispatch(
            w, "plugin.params", target_kind="channel_generator", channel=2,
            slot="-1", use_global_index=True, index_scope="global"
        )
        check("generator targets require explicit global scope and integer slot -1",
              not ambiguous_generator["ok"] and not wrong_generator_slot["ok"],
              (ambiguous_generator, wrong_generator_slot))
        params = dispatch(
            w, "plugin.params", target_kind="channel_generator", channel=2,
            slot=-1, use_global_index=True, index_scope="global"
        )["result"]
        scan = dispatch(
            w, "plugin.scan_params", target_kind="channel_generator", channel=2,
            slot=-1, use_global_index=True, index_scope="global"
        )["result"]
        check("generator parameter reads echo the explicit global target",
              params["target_kind"] == scan["target_kind"] == "channel_generator"
              and params["channel"] == scan["channel"] == 2
              and params["slot"] == scan["slot"] == -1
              and params["use_global_index"] is scan["use_global_index"] is True,
              (params, scan))
        _state.LAST_WRITE[0] = None
        generator.reported = {}
        normal = dispatch(
            w, "plugin.set_param", target_kind="channel_generator", channel=2,
            slot=-1, use_global_index=True, index_scope="global",
            index=0, value=0.35
        )
        display = dispatch(
            w, "plugin.set_param_display", target_kind="channel_generator",
            channel=2, slot=-1, use_global_index=True, index_scope="global",
            param="Drive",
            target=60.0
        )
        option = dispatch(
            w, "plugin.set_param_option", target_kind="channel_generator",
            channel=2, slot=-1, use_global_index=True, index_scope="global",
            param="Mode",
            option="Drive", steps=24
        )
        check("all three generator mutation modes read back and verify",
              normal["ok"] and normal["result"]["verified"]
              and display["ok"] and display["result"]["verified"]
              and option["ok"] and option["result"]["verified"],
              (normal, display, option))
        check("generator values persist across reads and writes",
              abs(generator.values[0] - 0.35) < 1e-9
              and option["result"]["selected"] == "Drive", generator.values)
        values_before = list(generator.values)
        undo_before = list(_state.UNDO)
        invalid_option_index = dispatch(
            w, "plugin.set_param_option", target_kind="channel_generator",
            channel=2, slot=-1, use_global_index=True, index_scope="global",
            param=999, option="Drive", steps=24
        )
        check("option index is bounded before undo or parameter sweep",
              not invalid_option_index["ok"]
              and "outside" in invalid_option_index["error"]
              and generator.values == values_before
              and _state.UNDO == undo_before,
              (invalid_option_index, generator.values, _state.UNDO))
        generator_calls = [row for row in plugin_scopes
                           if row[1] == 2 and row[2] == -1]
        check("every generator plug-in API call uses a global channel index",
              bool(generator_calls) and all(row[3] is True for row in generator_calls),
              [row for row in generator_calls if not row[3]])
        check("every Channel Rack state API call used global addressing",
              bool(channel_scopes)
              and all(global_scope for _name, global_scope in channel_scopes),
              [row for row in channel_scopes if not row[1]])
    finally:
        for name, function in saved_channels.items():
            setattr(fake_channels, name, function)
        for name, function in saved_channel_optional.items():
            if function is None:
                try:
                    delattr(fake_channels, name)
                except AttributeError:
                    pass
            else:
                setattr(fake_channels, name, function)
        for name, function in saved_patterns.items():
            if function is None:
                try:
                    delattr(fake_patterns, name)
                except AttributeError:
                    pass
            else:
                setattr(fake_patterns, name, function)
        if saved_transport_loop is None:
            delattr(fake_transport, "setLoopMode")
        else:
            fake_transport.setLoopMode = saved_transport_loop
        if saved_tempo is None:
            delattr(fake_mixer, "setCurrentTempo")
        else:
            fake_mixer.setCurrentTempo = saved_tempo
        for name, function in saved_plugins.items():
            setattr(fake_plugins, name, function)


def check_mixer_count_sentinel(c):
    """API 45's 127th pseudo-track must never reach track-addressed calls."""
    import channels as fake_channels
    import mixer as fake_mixer
    import plugins as fake_plugins

    saved = {
        "track_count": fake_mixer.trackCount,
        "track_summary": bridge._track_summary,
        "route_active": fake_mixer.getRouteSendActive,
        "track_info": fake_mixer.getTrackInfo,
        "set_volume": fake_mixer.setTrackVolume,
        "set_route": fake_mixer.setRouteTo,
        "plugin_valid": fake_plugins.isValid,
        "set_target": fake_channels.setTargetFxTrack,
    }
    summary_indices = []
    route_indices = []
    write_indices = []
    plugin_indices = []
    channel_destinations = []

    def track_summary(index, *args, **kwargs):
        summary_indices.append(index)
        if index == 126:
            raise AssertionError("non-addressable mixer pseudo-track was scanned")
        return saved["track_summary"](index, *args, **kwargs)

    def route_active(source, destination):
        route_indices.append((source, destination))
        if source == 126 or destination == 126:
            raise AssertionError("non-addressable mixer pseudo-track was routed")
        return saved["route_active"](source, destination)

    def set_volume(index, *args, **kwargs):
        write_indices.append(("volume", index))
        return saved["set_volume"](index, *args, **kwargs)

    def set_route(source, destination, *args, **kwargs):
        write_indices.append(("route_source", source))
        write_indices.append(("route_destination", destination))
        return saved["set_route"](source, destination, *args, **kwargs)

    def plugin_valid(index, *args, **kwargs):
        plugin_indices.append(index)
        return saved["plugin_valid"](index, *args, **kwargs)

    def set_target(channel, destination, *args, **kwargs):
        channel_destinations.append(destination)
        return saved["set_target"](channel, destination, *args, **kwargs)

    fake_mixer.trackCount = lambda: 127
    fake_mixer.getTrackInfo = lambda _mode: 126
    bridge._track_summary = track_summary
    fake_mixer.getRouteSendActive = route_active
    fake_mixer.setTrackVolume = set_volume
    fake_mixer.setRouteTo = set_route
    fake_plugins.isValid = plugin_valid
    fake_channels.setTargetFxTrack = set_target
    try:
        print("\n-- API 45 mixer count sentinel is never addressable --")
        info = c.call("project.info")
        listing = c.call("mixer.list", only_used=False)
        detail = c.call("mixer.track", track=3)
        rejected_read = c.call("mixer.track", track=126)

        check("API 45 public mixer count is normalized to 126",
              info["ok"] and info["result"]["mixer_track_count"] == 126,
              info)
        check("API 45 full mixer listing stops at index 125",
              listing["ok"]
              and listing["result"]["track_count"] == 126
              and listing["result"]["scanned"] == 126
              and len(listing["result"]["tracks"]) == 126
              and listing["result"]["tracks"][-1]["index"] == 125,
              listing.get("result"))
        check("API 45 detail route scan stops at index 125",
              detail["ok"]
              and route_indices
              and max(destination for _source, destination in route_indices) == 125,
              route_indices[-4:])
        check("pseudo-track 126 is rejected before track summary APIs",
              not rejected_read["ok"] and 126 not in summary_indices,
              (rejected_read, summary_indices[-4:]))

        w = _load_bridge_with_writes()
        undo_before = list(_state.UNDO)
        invalid_writes = (
            dispatch(w, "mixer.set_volume", track=126, value=0.5),
            dispatch(
                w, "plugin.set_param", track=126, slot=0,
                index=0, value=0.5
            ),
            dispatch(
                w, "mixer.set_send", track=3, to=126, enabled=True
            ),
            dispatch(
                w, "channel.route_to_mixer", channel=1,
                index_scope="global", destination=126
            ),
        )
        check("every write path rejects pseudo-track 126 before FL APIs",
              all(not response["ok"] for response in invalid_writes)
              and all(index != 126 for _kind, index in write_indices)
              and 126 not in plugin_indices
              and 126 not in channel_destinations
              and _state.UNDO == undo_before,
              (invalid_writes, write_indices, plugin_indices,
               channel_destinations, _state.UNDO))

        # The normalizer is a ceiling, not a hard-coded public count. Smaller
        # projects/older hosts retain the exact lower count they report.
        fake_mixer.trackCount = lambda: 12
        lower_info = c.call("project.info")
        lower_list = c.call("mixer.list", only_used=False)
        check("lower mixer counts remain unchanged",
              lower_info["result"]["mixer_track_count"] == 12
              and lower_list["result"]["track_count"] == 12
              and lower_list["result"]["scanned"] == 12
              and len(lower_list["result"]["tracks"]) == 12,
              (lower_info, lower_list))
    finally:
        fake_mixer.trackCount = saved["track_count"]
        bridge._track_summary = saved["track_summary"]
        fake_mixer.getRouteSendActive = saved["route_active"]
        fake_mixer.getTrackInfo = saved["track_info"]
        fake_mixer.setTrackVolume = saved["set_volume"]
        fake_mixer.setRouteTo = saved["set_route"]
        fake_plugins.isValid = saved["plugin_valid"]
        fake_channels.setTargetFxTrack = saved["set_target"]


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

    check_mixer_count_sentinel(c)

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
    check_track_b()

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

    def send_non_object(body):
        c.sock.sendall((json.dumps(body) + "\n").encode("utf-8"))
        deadline = time.time() + 2
        while time.time() < deadline:
            bridge.OnIdle()
            try:
                chunk = c.sock.recv(65536)
                if chunk:
                    c.buf += chunk
            except (BlockingIOError, OSError):
                pass
            if b"\n" in c.buf:
                line, c.buf = c.buf.split(b"\n", 1)
                return json.loads(line.decode())
            time.sleep(0.005)
        return None

    non_object = send_non_object([])
    bad_args = send_non_object({"id": 77, "cmd": "ping", "args": []})
    check("non-object requests and args fail closed",
          non_object is not None and not non_object["ok"]
          and bad_args is not None and not bad_args["ok"],
          (non_object, bad_args))
    check("bridge remains alive after non-object input", c.call("ping")["ok"])

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
