"""Client for the Universal Bridge script running inside FL Studio.

Three transports, tried in order:

* TCP on loopback - fastest, used wherever it works.
* A file mailbox - for hosts that allow writes but not sockets.
* MIDI SysEx - what actually works with FL Studio, which sandboxes its script
  interpreter so that both sockets and filesystem writes fail.

All three speak the same JSON request/response protocol, so nothing above this
module needs to know which one is carrying the traffic.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - FL Studio 2026 on macOS has fcntl.
    fcntl = None

HOST = os.environ.get("FL_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("FL_BRIDGE_PORT", "20202"))
TIMEOUT = float(os.environ.get("FL_BRIDGE_TIMEOUT", "30"))
PREFIX = "clbr-"
REQ_PREFIX = PREFIX + "req-"
RESP_PREFIX = PREFIX + "resp-"
ALIVE_NAME = PREFIX + "alive.json"

# The bridge cannot create directories - FL's sandbox blocks mkdir - so it
# settles into whichever existing directory accepts writes. Search the same
# list, in the same order, and use the one holding a fresh liveness marker.
_SCRIPT_DIR = os.path.expanduser(
    "~/Documents/Image-Line/FL Studio/Settings/Hardware/Universal Bridge")
MAILBOX_CANDIDATES = [
    d for d in (
        os.environ.get("FL_BRIDGE_MAILBOX"),
        _SCRIPT_DIR,
        os.path.expanduser("~/Documents/Image-Line/FL Studio/Settings"),
        os.path.expanduser("~"),
        "/tmp",
    ) if d
]

# How stale the bridge's liveness marker may be before we call FL dead. The
# script refreshes it about once a second.
ALIVE_MAX_AGE = 15.0

# SysEx framing, mirroring the bridge. 0x7D is the non-commercial
# manufacturer id; the tag byte keeps each side from reading its own echo.
SYSEX_ID = 0x7D
TAG_REQUEST = 0x01
TAG_RESPONSE = 0x02
SYSEX_CHUNK = 1024
MIDI_PORT = os.environ.get("FL_BRIDGE_MIDI_PORT", "IAC Driver")
MIDI_ENABLED = os.environ.get("FL_BRIDGE_ENABLE_MIDI", "").strip() == "1"
MAX_WIRE_ID = (1 << 14) - 1
MAX_SYSEX_REQUEST_BYTES = 256 * 1024
MAX_SYSEX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_SYSEX_HELLO_BYTES = 4 * 1024
MAX_SYSEX_REQUEST_PARTS = (
    MAX_SYSEX_REQUEST_BYTES + SYSEX_CHUNK - 1
) // SYSEX_CHUNK
MAX_SYSEX_RESPONSE_PARTS = (
    MAX_SYSEX_RESPONSE_BYTES + SYSEX_CHUNK - 1
) // SYSEX_CHUNK
MAX_SYSEX_HELLO_PARTS = (
    MAX_SYSEX_HELLO_BYTES + SYSEX_CHUNK - 1
) // SYSEX_CHUNK
MAX_SYSEX_FRAME_BYTES = 10 + SYSEX_CHUNK
MAX_SYSEX_PARTIAL_MESSAGES = 2  # one heartbeat plus one serialized request
MAX_SYSEX_PARTIAL_BYTES = MAX_SYSEX_RESPONSE_BYTES
SYSEX_PARTIAL_TTL_SECONDS = 10.0
_MIDI_PREFLIGHT: dict[str, tuple[float, bool]] = {}

# Only commands independently locked read-only by bridge protocol 2 may be
# replayed after an ambiguous transport failure. A lost response to any other
# command has an unknown outcome and must never be retried automatically.
IDEMPOTENT_READ_COMMANDS = frozenset({
    "ping",
    "project.info",
    "arrangement.selection",
    "mixer.list",
    "mixer.track",
    "plugin.params",
    "plugin.scan_params",
    "channels.list",
})
MAX_IDEMPOTENT_READ_RECONNECTS = 1
RECONNECT_DELAY_SECONDS = 0.05

NOT_CONNECTED_HELP = (
    "Could not reach the Universal Bridge inside FL Studio.\n"
    "Tried TCP {host}:{port}, mailboxes under {mailbox}.\n"
    "{midi_line}\n"
    "Check, in order:\n"
    "  1. FL Studio is running.\n"
    "  2. Options > MIDI settings > Input: the port carrying the bridge is\n"
    "     enabled and its Controller type is 'Universal Bridge'.\n"
    "  3. View > Script output shows '[UniversalBridge] ready: ...'.\n"
    "     Use the 'Reload script' button there after changing the script."
)


class BridgeError(RuntimeError):
    """A command reached FL Studio but failed there, or the link is down."""


class BridgeUnavailableError(BridgeError):
    """No bridge transport is currently reachable."""


class MidiTransportOwnedError(BridgeError):
    """Another live connector instance owns the physical MIDI transport."""

    def __init__(
        self,
        message: str,
        *,
        port_identity: str | None = None,
        lock_path: str | None = None,
        owner_pid: int | None = None,
    ) -> None:
        super().__init__(message)
        self.port_identity = port_identity
        self.lock_path = lock_path
        self.owner_pid = owner_pid


class _MidiPortOwnership:
    """Process-scoped advisory ownership for one resolved CoreMIDI port pair.

    CoreMIDI permits multiple subscribers to an IAC bus. That is unsafe for
    this request/response protocol because two MCP processes can consume or
    duplicate each other's replies. A per-user advisory file lock makes all
    cooperating connector processes fail closed before opening the ports.

    The lock file is intentionally retained after release. Unlinking an
    advisory-lock inode creates a race where a waiter can lock a new inode
    while the previous owner still holds the old one.
    """

    def __init__(self, port_identity: str, lock_dir: str | None = None):
        self.port_identity = port_identity
        # Do not use tempfile.gettempdir(): TMPDIR differs between macOS
        # applications, so an MCP launched by a GUI client and one launched
        # from a shell would each believe they own the same IAC bus. /tmp is
        # the stable system namespace; the uid keeps local users separate.
        root = lock_dir or "/tmp"
        uid = os.getuid() if hasattr(os, "getuid") else "user"
        digest = hashlib.sha256(port_identity.encode("utf-8")).hexdigest()[:24]
        self.path = os.path.join(
            root, "fl-studio-mcp-iac-%s-%s.lock" % (uid, digest)
        )
        self._fd: int | None = None

    def _owner_metadata(self, fd: int) -> dict:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 4096).decode("utf-8", errors="replace")
            value = json.loads(raw) if raw else {}
        except (OSError, ValueError, TypeError):
            value = {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _owner_description(value: dict) -> str:
        pid = value.get("pid")
        if type(pid) is int and pid > 0:
            return "owner pid %d" % pid
        return "owner pid unavailable"

    def evidence(self) -> dict:
        """Return bounded local ownership facts without transferring the lock."""
        return {
            "port_identity": self.port_identity,
            "lock_path": self.path,
            "owner_pid": os.getpid(),
            "acquired": self._fd is not None,
        }

    def acquire(self) -> None:
        if self._fd is not None:
            return
        if fcntl is None:
            raise BridgeError(
                "MIDI IAC transport ownership requires POSIX advisory locks; "
                "refusing to open the physical port without them"
            )
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise BridgeError(
                "could not create the MIDI IAC ownership lock %r: %s"
                % (self.path, exc)
            ) from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            owner_metadata = self._owner_metadata(fd)
            owner = self._owner_description(owner_metadata)
            owner_pid = owner_metadata.get("pid")
            if type(owner_pid) is not int or owner_pid <= 0:
                owner_pid = None
            try:
                os.close(fd)
            except OSError:
                pass
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise MidiTransportOwnedError(
                    "MIDI IAC transport %r is already owned by another live "
                    "fl-studio-mcp connector instance (%s). Stop the duplicate "
                    "MCP/doctor process or let it close before retrying."
                    % (self.port_identity, owner),
                    port_identity=self.port_identity,
                    lock_path=self.path,
                    owner_pid=owner_pid,
                ) from exc
            raise BridgeError(
                "could not lock MIDI IAC transport %r: %s"
                % (self.port_identity, exc)
            ) from exc

        metadata = json.dumps(
            {
                "pid": os.getpid(),
                "port": self.port_identity,
                "acquired_unix": int(time.time()),
            },
            sort_keys=True,
        ).encode("utf-8")
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            if os.write(fd, metadata) != len(metadata):
                raise OSError("short ownership metadata write")
        except OSError as exc:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise BridgeError(
                "could not record MIDI IAC ownership for %r: %s"
                % (self.port_identity, exc)
            ) from exc
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            # Closing the descriptor releases the lock even if an explicit
            # unlock reports a teardown race.
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def __del__(self):
        try:
            self.release()
        except Exception:
            # Process exit releases advisory locks regardless; finalizers must
            # never turn interpreter shutdown into a user-visible error.
            pass


def _native_midi_probe_blocked() -> bool:
    """Avoid native CoreMIDI startup when the caller says it is sandboxed.

    RtMidi can terminate the interpreter from C++ while constructing a MIDI
    client instead of raising a Python exception. Isolating that startup in a
    child protects this process, but on macOS the operating system still shows
    a "Python quit unexpectedly" report for the child. Automation harnesses,
    CI runners, and any process without CoreMIDI entitlements hit this, so
    such a caller sets FL_BRIDGE_SANDBOXED=1 to fail closed instead: the MIDI
    transport reports itself unavailable and no probe child is created.

    This is an explicit opt-in. Nothing is inferred from the environment,
    because guessing wrong in either direction is worse than being told.
    """
    marker = os.environ.get("FL_BRIDGE_SANDBOXED", "").strip().lower()
    return bool(marker) and marker not in {"0", "false", "no", "none", "off"}


def _midi_preflight(port_name: str) -> bool:
    """Probe RtMidi in a child so a native CoreMIDI abort cannot kill MCP."""
    if not MIDI_ENABLED or _native_midi_probe_blocked():
        return False
    now = time.monotonic()
    cached = _MIDI_PREFLIGHT.get(port_name)
    if cached and now - cached[0] < 5.0:
        return cached[1]
    source = (
        "import json,rtmidi; "
        "mi=rtmidi.MidiIn(); mo=rtmidi.MidiOut(); "
        "print(json.dumps({'in':mi.get_ports(),'out':mo.get_ports()})); "
        "mi.close_port(); mo.close_port()"
    )
    try:
        probe = subprocess.run(
            [sys.executable, "-c", source],
            capture_output=True,
            text=True,
            timeout=8,
        )
        parsed = json.loads(probe.stdout) if probe.returncode == 0 else {}
        ports = parsed if isinstance(parsed, dict) else {}
        found = (
            any(port_name.lower() in str(name).lower() for name in ports.get("in", []))
            and any(port_name.lower() in str(name).lower() for name in ports.get("out", []))
        )
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, TypeError):
        found = False
    _MIDI_PREFLIGHT[port_name] = (now, found)
    return found


# ---------------------------------------------------------------------------
# transports
# ---------------------------------------------------------------------------


class _TcpTransport:
    name = "tcp"

    def __init__(self, host, port, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock = None
        self.buf = b""

    def available(self) -> bool:
        try:
            s = socket.create_connection((self.host, self.port), timeout=1.0)
        except OSError:
            return False
        s.settimeout(self.timeout)
        self.sock = s
        self.buf = b""
        return True

    def request(self, rid: int, payload: dict) -> dict:
        if self.sock is None and not self.available():
            raise OSError("no connection")
        self.sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        while True:
            if b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                if not line.strip():
                    continue
                resp = json.loads(line.decode("utf-8"))
                if resp.get("id") != rid:
                    continue          # reply to an abandoned request
                return resp
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                self.close()
                raise TimeoutError("no reply within %ss" % self.timeout)
            if not chunk:
                self.close()
                raise OSError("bridge closed the connection")
            self.buf += chunk

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
            self.buf = b""


class _FileTransport:
    """Mailbox transport: write a request file, wait for its reply file."""

    name = "files"

    def __init__(self, roots, timeout):
        self.roots = [roots] if isinstance(roots, str) else list(roots)
        self.root = None
        self.timeout = timeout
        self.session = uuid.uuid4().hex[:8]

    def available(self) -> bool:
        """Find the directory the bridge settled on, by its liveness marker."""
        for directory in self.roots:
            try:
                age = time.time() - os.path.getmtime(
                    os.path.join(directory, ALIVE_NAME))
            except OSError:
                continue
            if age <= ALIVE_MAX_AGE:
                self.root = directory
                return True
        return False

    def request(self, rid: int, payload: dict) -> dict:
        if self.root is None and not self.available():
            raise OSError("no live mailbox")
        token = "%s-%d" % (self.session, rid)
        req = os.path.join(self.root, REQ_PREFIX + "%s.json" % token)
        resp = os.path.join(self.root, RESP_PREFIX + "%s.json" % token)
        tmp = os.path.join(self.root, PREFIX + "tmp-req-%s.json" % token)

        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload))
        os.replace(tmp, req)

        deadline = time.monotonic() + self.timeout
        delay = 0.002
        while time.monotonic() < deadline:
            try:
                with open(resp, encoding="utf-8") as fh:
                    body = fh.read()
                os.remove(resp)
            except OSError:
                # Poll quickly at first, then ease off; the bridge answers on
                # FL's idle tick, roughly every 20 ms.
                time.sleep(delay)
                delay = min(delay * 1.5, 0.02)
                continue
            if not body:
                continue
            return json.loads(body)

        for leftover in (req, tmp):
            try:
                os.remove(leftover)
            except OSError:
                pass
        raise TimeoutError("no reply within %ss" % self.timeout)

    def close(self):
        pass


class _MidiTransport:
    """SysEx over the IAC bus - the only channel FL's sandbox leaves open.

    FL blocks sockets and every filesystem write inside its script
    interpreter, but `device.midiOutSysex` goes through FL's own API. The bus
    echoes to all subscribers including the sender, so a direction tag keeps
    each side from reading its own traffic back.
    """

    name = "midi"

    def __init__(self, port_name, timeout):
        self.port_name = port_name
        self.timeout = timeout
        self.midi_in = None
        self.midi_out = None
        self._ownership = None
        self.resolved_input_name = None
        self.resolved_output_name = None
        self.resolved_port_identity = None
        self.partial = {}
        self.partial_bytes = 0
        self.replies = {}
        self.hello_seen = False
        self._active_request_id = None

    # -- ports ------------------------------------------------------------

    def _open(self) -> bool:
        if self.midi_in is not None:
            return True
        if not _midi_preflight(self.port_name):
            return False
        try:
            import rtmidi
        except ImportError:
            return False
        mo = mi = None
        ownership = None
        try:
            mo, mi = rtmidi.MidiOut(), rtmidi.MidiIn()
            names = mi.get_ports()
            match = next((i for i, n in enumerate(names)
                          if self.port_name.lower() in n.lower()), None)
            out_names = mo.get_ports()
            out_match = next((i for i, n in enumerate(out_names)
                              if self.port_name.lower() in n.lower()), None)
            if match is None or out_match is None:
                raise LookupError("configured MIDI port is no longer present")
            port_identity = "input=%s; output=%s" % (
                names[match], out_names[out_match]
            )
            ownership = _MidiPortOwnership(port_identity)
            ownership.acquire()
            mi.open_port(match)
            mo.open_port(out_match)
            mi.ignore_types(sysex=False, timing=True, active_sense=True)
        except BridgeError:
            for handle in (mi, mo):
                try:
                    if handle is not None:
                        handle.close_port()
                except Exception:
                    pass
            if ownership is not None:
                ownership.release()
            raise
        except Exception:
            for handle in (mi, mo):
                try:
                    if handle is not None:
                        handle.close_port()
                except Exception:
                    pass
            if ownership is not None:
                ownership.release()
            return False
        self.midi_in, self.midi_out = mi, mo
        self._ownership = ownership
        self.resolved_input_name = names[match]
        self.resolved_output_name = out_names[out_match]
        self.resolved_port_identity = port_identity
        return True

    def ownership_evidence(self) -> dict:
        """Describe the exact currently held IAC endpoints and advisory lock."""
        if (
            self.midi_in is None
            or self.midi_out is None
            or self._ownership is None
            or self.resolved_input_name is None
            or self.resolved_output_name is None
            or self.resolved_port_identity is None
        ):
            raise BridgeError("MIDI IAC ownership is not currently held")
        ownership = self._ownership.evidence()
        if (
            ownership.get("port_identity") != self.resolved_port_identity
            or ownership.get("acquired") is not True
        ):
            raise BridgeError("MIDI IAC ownership evidence is internally inconsistent")
        return {
            "configured_port_query": self.port_name,
            "resolved_input_name": self.resolved_input_name,
            "resolved_output_name": self.resolved_output_name,
            "resolved_port_identity": self.resolved_port_identity,
            "lock_path": ownership.get("lock_path"),
            "owner_pid": ownership.get("owner_pid"),
            "acquired": True,
        }

    def available(self) -> bool:
        """Wait briefly for the bridge's heartbeat."""
        if not self._open():
            return False
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            self._drain()
            if self.hello_seen:
                return True
            time.sleep(0.01)
        self.close()
        return False

    # -- framing ----------------------------------------------------------

    def _drop_partial(self, mid: int) -> dict | None:
        slot = self.partial.pop(mid, None)
        if slot is not None:
            self.partial_bytes = max(
                0, self.partial_bytes - int(slot.get("bytes", 0))
            )
        return slot

    def _expire_partial(self, now: float) -> None:
        expired = [
            mid
            for mid, slot in self.partial.items()
            if now - float(slot.get("updated", now)) > SYSEX_PARTIAL_TTL_SECONDS
        ]
        for mid in expired:
            self._drop_partial(mid)

    def _message_limit(self, mid: int) -> tuple[int, int] | None:
        if mid == 0:
            return MAX_SYSEX_HELLO_PARTS, MAX_SYSEX_HELLO_BYTES
        if mid == self._active_request_id:
            return MAX_SYSEX_RESPONSE_PARTS, MAX_SYSEX_RESPONSE_BYTES
        return None

    def _drain(self):
        now = time.monotonic()
        self._expire_partial(now)
        while True:
            msg = self.midi_in.get_message()
            if not msg:
                return
            data = msg[0]
            if (
                len(data) < 10
                or len(data) > MAX_SYSEX_FRAME_BYTES
                or data[0] != 0xF0
                or data[1] != SYSEX_ID
            ):
                continue
            if data[2] != TAG_RESPONSE:
                continue                      # our own request echoing back
            try:
                header = [int(value) for value in data[1:9]]
            except (TypeError, ValueError):
                continue
            if any(value < 0 or value > 0x7F for value in header):
                continue
            end = len(data) - 1 if data[-1] == 0xF7 else len(data)
            mid = (header[2] << 7) | header[3]
            seq = (header[4] << 7) | header[5]
            total = (header[6] << 7) | header[7]
            limits = self._message_limit(mid)
            if limits is None:
                continue
            max_parts, max_bytes = limits
            if total < 1 or total > max_parts or seq < 0 or seq >= total:
                continue
            try:
                text = bytes(data[9:end]).decode("ascii")
            except Exception:
                continue

            slot = self.partial.get(mid)
            if slot is None:
                if len(self.partial) >= MAX_SYSEX_PARTIAL_MESSAGES:
                    continue
                slot = {
                    "total": total,
                    "parts": {},
                    "bytes": 0,
                    "updated": now,
                }
                self.partial[mid] = slot
            elif slot["total"] != total:
                self._drop_partial(mid)
                continue

            previous = slot["parts"].get(seq)
            if previous is not None:
                if previous != text:
                    self._drop_partial(mid)
                else:
                    slot["updated"] = now
                continue

            added = len(text)
            if (
                slot["bytes"] + added > max_bytes
                or self.partial_bytes + added > MAX_SYSEX_PARTIAL_BYTES
            ):
                self._drop_partial(mid)
                continue
            slot["parts"][seq] = text
            slot["bytes"] += added
            slot["updated"] = now
            self.partial_bytes += added
            if len(slot["parts"]) < total:
                continue

            if not all(i in slot["parts"] for i in range(total)):
                self._drop_partial(mid)
                continue
            slot = self._drop_partial(mid)
            body = "".join(slot["parts"][i] for i in range(total))
            try:
                payload = json.loads(body)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if mid == 0 and payload.get("hello") is True:
                self.hello_seen = True
            elif mid == self._active_request_id:
                self.replies[mid] = payload

    def request(self, rid: int, payload: dict) -> dict:
        if not self._open():
            raise OSError("MIDI port %r unavailable" % self.port_name)
        text = json.dumps(payload)
        if len(text) > MAX_SYSEX_REQUEST_BYTES:
            raise ValueError(
                "MIDI request exceeds the %d-byte size limit"
                % MAX_SYSEX_REQUEST_BYTES
            )
        chunks = [text[i:i + SYSEX_CHUNK]
                  for i in range(0, len(text), SYSEX_CHUNK)] or [""]
        total = len(chunks)
        if total > MAX_SYSEX_REQUEST_PARTS:
            raise ValueError("MIDI request has too many SysEx chunks")

        self._drop_partial(rid)
        self.replies.pop(rid, None)
        self._active_request_id = rid
        try:
            for seq, chunk in enumerate(chunks):
                frame = [0xF0, SYSEX_ID, TAG_REQUEST,
                         (rid >> 7) & 0x7F, rid & 0x7F,
                         (seq >> 7) & 0x7F, seq & 0x7F,
                         (total >> 7) & 0x7F, total & 0x7F]
                frame.extend(ord(c) & 0x7F for c in chunk)
                frame.append(0xF7)
                self.midi_out.send_message(frame)

            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                self._drain()
                if rid in self.replies:
                    return self.replies.pop(rid)
                time.sleep(0.002)
            raise TimeoutError("no reply within %ss" % self.timeout)
        finally:
            self._drop_partial(rid)
            self.replies.pop(rid, None)
            if self._active_request_id == rid:
                self._active_request_id = None

    def close(self):
        for port in (self.midi_in, self.midi_out):
            try:
                if port is not None:
                    port.close_port()
            except Exception:
                pass
        self.midi_in = self.midi_out = None
        self.partial.clear()
        self.partial_bytes = 0
        self.replies.clear()
        self.hello_seen = False
        self._active_request_id = None
        ownership, self._ownership = self._ownership, None
        if ownership is not None:
            ownership.release()
        self.resolved_input_name = None
        self.resolved_output_name = None
        self.resolved_port_identity = None


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------


class BridgeClient:
    """Serialised access to the bridge; picks whichever transport is live."""

    def __init__(self, host=HOST, port=PORT, timeout=TIMEOUT,
                 mailbox=None, midi_port=MIDI_PORT):
        self.timeout = timeout
        roots = [mailbox] if mailbox else MAILBOX_CANDIDATES
        self._transports = [
            _TcpTransport(host, port, timeout),
            _FileTransport(roots, timeout),
        ]
        # FL_BRIDGE_ENABLE_MIDI gates the transport itself, not merely the
        # preflight probe. Opening CoreMIDI is the one transport that can take
        # a shared system resource -- and abort the interpreter from native
        # code doing it -- so a caller that has not asked for MIDI must never
        # have it attempted behind their back. Every supported entry point
        # (the MCP server config, doctor, and the inspect/validate scripts)
        # sets this; omitting it is what "I do not want this process touching
        # the IAC bus" looks like.
        if MIDI_ENABLED:
            self._transports.append(_MidiTransport(midi_port, timeout))
        self._active = None
        self._id = 0
        self._lock = threading.Lock()
        self.host, self.port = host, port
        self.mailbox = mailbox or ", ".join(roots)
        self.midi_port = midi_port

    @property
    def transport(self) -> str:
        return self._active.name if self._active else "none"

    def _select(self):
        if self._active is not None:
            return self._active
        for t in self._transports:
            if t.available():
                self._active = t
                return t
        if MIDI_ENABLED:
            midi_line = "Tried MIDI SysEx on %s." % self.midi_port
        else:
            # Saying "tried MIDI" when the transport was never built sends the
            # reader off to check FL's MIDI settings for a problem that is
            # actually in their own configuration.
            midi_line = (
                "MIDI SysEx was not tried: FL_BRIDGE_ENABLE_MIDI is not set "
                "to 1 for this process. That is the transport FL Studio "
                "actually uses, so set it (see .mcp.json.example)."
            )
        raise BridgeUnavailableError(NOT_CONNECTED_HELP.format(
            host=self.host, port=self.port, mailbox=self.mailbox,
            midi_line=midi_line))

    def call(self, cmd: str, **args) -> dict:
        with self._lock:
            retry_safe = cmd in IDEMPOTENT_READ_COMMANDS
            attempts = 1 + (
                MAX_IDEMPOTENT_READ_RECONNECTS if retry_safe else 0
            )
            for attempt in range(attempts):
                try:
                    return self._call_once(cmd, args)
                except MidiTransportOwnedError:
                    raise
                except BridgeUnavailableError as exc:
                    link_error = exc
                except BridgeError:
                    # FL returned a command-level rejection. The transport is
                    # still coherent, and replay cannot make that safer.
                    raise
                except (OSError, TimeoutError, ValueError) as exc:
                    link_error = exc

                # A transport-level failure can mean FL restarted, its script
                # reloaded, or CoreMIDI dropped the endpoint. Close it fully so
                # the next bounded attempt must reselect/reopen from scratch.
                self._drop()
                if attempt + 1 < attempts:
                    time.sleep(RECONNECT_DELAY_SECONDS)
                    continue

                if isinstance(link_error, BridgeUnavailableError):
                    raise link_error
                if retry_safe:
                    raise BridgeUnavailableError(
                        "FL Studio bridge read %r failed after %d bounded "
                        "attempts; the transport was dropped before retry: %s: %s"
                        % (
                            cmd,
                            attempts,
                            type(link_error).__name__,
                            link_error,
                        )
                    ) from link_error
                raise BridgeUnavailableError(
                    "FL Studio bridge call %r failed after its first transport "
                    "attempt and was not replayed because it is not an "
                    "allowlisted idempotent read; its outcome may be unknown: %s: %s"
                    % (cmd, type(link_error).__name__, link_error)
                ) from link_error

            raise AssertionError("unreachable bridge retry state")

    def _call_once(self, cmd: str, args: dict) -> dict:
        transport = self._select()
        # SysEx frames carry a 14-bit id. Calls are serialized, so wrapping is
        # safe and avoids a permanent timeout after request 16,383.
        self._id = (self._id % MAX_WIRE_ID) + 1
        rid = self._id
        resp = transport.request(rid, {"id": rid, "cmd": cmd, "args": args})
        if not isinstance(resp, dict) or resp.get("id") != rid:
            raise ValueError(
                "bridge returned a malformed or mismatched response envelope"
            )
        if not resp.get("ok"):
            detail = resp.get("error", "unknown error")
            if resp.get("available"):
                detail += "\navailable commands: " + ", ".join(resp["available"])
            raise BridgeError("FL Studio rejected '%s': %s" % (cmd, detail))
        return resp.get("result", {})

    def _drop(self):
        if self._active is not None:
            self._active.close()
            self._active = None

    def close(self):
        with self._lock:
            self._drop()

    def scan_plugin_params(self, track: int, slot: int, *,
                           start: int | None = None,
                           end: int | None = None,
                           max_indices: int | None = None,
                           max_results: int | None = None) -> dict:
        """Return a plug-in's real parameter map in one round trip.

        FL reports a padded maximum rather than a count for VST plug-ins -
        often thousands of slots for a VST3 - with the real controls sparse
        inside that range. Paging it with `plugin.params` is about a thousand SysEx
        round trips. This walks the range inside FL instead, drops the
        padding there, and answers once.

        The reply carries `params` (index, name, normalised value, and the
        display string that actually identifies a control), plus its own
        coverage: `reported_count`, `examined`, `highest_index_examined`,
        `real`, `padding_skipped`, and `truncated` / `truncated_by`. Check
        `truncated` before treating the map as the whole plug-in: a bounded
        scan says so rather than looking complete.

        A read. It needs no write flag, and is replay-safe on a dropped
        reply, which matters most here because it is the long call.
        """
        args: dict = {"track": track, "slot": slot}
        for key, value in (("start", start), ("end", end),
                           ("max_indices", max_indices),
                           ("max_results", max_results)):
            if value is not None:
                args[key] = value
        return self.call("plugin.scan_params", **args)

    # -- lean verified writes -------------------------------------------
    #
    # These reach the bridge only when FL was launched with
    # FL_BRIDGE_ENABLE_WRITES=1; otherwise the bridge rejects them and `call`
    # raises BridgeError. Each returns the bridge's verification dict
    # unchanged - {requested, before, after, verified} plus context - and a
    # verified=False result is a real answer, not a failure: FL accepted the
    # write and the control did not move. None of them are in
    # IDEMPOTENT_READ_COMMANDS, so a dropped reply is never replayed.

    def set_mixer_volume(self, track: int, value: float,
                         allow_master: bool = False) -> dict:
        """Set a mixer fader, normalised 0..1 (0.8 is FL's 0 dB default)."""
        return self.call("mixer.set_volume", track=track, value=value,
                         allow_master=allow_master)

    def set_mixer_pan(self, track: int, value: float,
                      allow_master: bool = False) -> dict:
        """Set a mixer pan, -1.0 hard left to 1.0 hard right."""
        return self.call("mixer.set_pan", track=track, value=value,
                         allow_master=allow_master)

    def set_mixer_mute(self, track: int, muted: bool,
                       allow_master: bool = False) -> dict:
        """Mute or unmute a mixer track. States the wanted value, never toggles."""
        return self.call("mixer.set_mute", track=track, muted=muted,
                         allow_master=allow_master)

    def set_mixer_eq(self, track: int, band: int, gain: float | None = None,
                     freq: float | None = None,
                     allow_master: bool = False) -> dict:
        """Set gain and/or frequency (normalised 0..1) on one built-in EQ band."""
        args: dict = {"track": track, "band": band,
                      "allow_master": allow_master}
        if gain is not None:
            args["gain"] = gain
        if freq is not None:
            args["freq"] = freq
        return self.call("mixer.set_eq", **args)

    def set_plugin_param(self, track: int, slot: int, index: int, value: float,
                         allow_master: bool = False) -> dict:
        """Set one plug-in parameter by index, normalised 0..1.

        Verification is the parameter's display string: FL's numeric readback
        can lag a tick behind a write that did land.
        """
        return self.call("plugin.set_param", track=track, slot=slot,
                         index=index, value=value, allow_master=allow_master)

    def ping(self) -> dict:
        return self.call("ping")

    def is_up(self) -> bool:
        try:
            self.ping()
            return True
        except BridgeError:
            return False


_client: BridgeClient | None = None


def get_client() -> BridgeClient:
    global _client
    if _client is None:
        _client = BridgeClient()
    return _client
