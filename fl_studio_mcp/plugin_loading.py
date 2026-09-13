"""Named macOS Add-menu loading, verified against FL's live inventory.

The MIDI API cannot insert a plugin. This adapter uses the observed native
menu and then checks the actual new channel/slot through the existing bridge.
It never uses screen coordinates or retries a dispatched menu action.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import time
from datetime import datetime, timezone
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .bridge_client import get_client
from .creative import _PIANO_ROLL_DISPATCH_LOCK
from .performance import TrackBInspector
from .plugin_atlas.registry import normalize_search_text
from .readonly_inspector import connection_from_ping
from .verified_writer import VerifiedWriter, WriteModeManager


PluginKind = Literal["instrument", "effect"]
MAX_MENU_ENTRIES = 512


class PluginLoadingError(RuntimeError):
    pass


class LoadingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class PluginMenuEntry(LoadingModel):
    name: str = Field(min_length=1, max_length=256)
    kind: PluginKind
    menu_path: tuple[str, ...] = Field(min_length=2, max_length=12)


class PluginMenuInventory(LoadingModel):
    observed_at: datetime
    platform: str
    supported: bool
    entries: tuple[PluginMenuEntry, ...] = ()
    error: str | None = None
    source: Literal["native_add_menu"] = "native_add_menu"


class PluginLoadRequest(LoadingModel):
    name: str = Field(min_length=1, max_length=256)
    kind: PluginKind
    track_index: int | None = Field(default=None, ge=0)
    allow_master: bool = False
    session_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    timeout_seconds: float = Field(default=20.0, ge=1.0, le=60.0)

    @model_validator(mode="after")
    def validate_destination(self) -> PluginLoadRequest:
        if not self.name.strip():
            raise ValueError("plugin name must not be blank")
        if (self.kind == "effect") != (self.track_index is not None):
            raise ValueError("effects require track_index; instruments use a new channel")
        if self.track_index == 0 and not self.allow_master:
            raise ValueError("Master must be explicitly targeted with allow_master=true")
        return self


class LoadedPlugin(LoadingModel):
    index: int = Field(ge=0)
    name: str


class PluginLoadResult(LoadingModel):
    observed_at: datetime
    request: PluginLoadRequest
    status: Literal["loaded", "not_dispatched", "unknown_outcome"]
    verified: bool = False
    dispatched: bool = False
    loaded_plugin: LoadedPlugin | None = None
    session_fingerprint: str | None = None
    menu_path: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    project_saved: Literal[False] = False
    undo_point_created: None = None

    @model_validator(mode="after")
    def validate_result(self) -> PluginLoadResult:
        if self.verified != (self.status == "loaded"):
            raise ValueError("only an observed addition can be verified")
        if self.verified and (not self.dispatched or self.loaded_plugin is None):
            raise ValueError("a loaded result requires dispatch and new-instance evidence")
        return self


class MenuDispatch(LoadingModel):
    status: Literal["dispatched", "not_dispatched", "unknown_outcome"]
    error: str | None = None


class MenuBackend(Protocol):
    def entries(self) -> tuple[PluginMenuEntry, ...]: ...
    def load(self, entry: PluginMenuEntry) -> MenuDispatch: ...


# JXA talks to named Accessibility menu objects. Arguments are JSON carried
# through argv, never interpolated into executable source. Both the catalogue
# and final click resolve the current menu afresh. The path was observed in FL
# 26.1.3 on macOS; unsupported/localized menu structures return a clear error.
_MENU_SCRIPT = r'''
function run(argv) {
    var command = JSON.parse(argv[0]);
    var se = Application('System Events');
    var attempted = false;
    var opened = false;
    var denied = ['More plugins...', 'Categories', 'Simple', 'Tree', 'Plugin picker',
        'View plugin picker', 'Plugin database', 'Browse plugin database', 'Browse all installed plugins',
        'Browse presets', 'Refresh plugin list (fast scan)', 'Manage plugins',
        'Manage FL Cloud plugins...', 'Automation for last tweaked parameter', 'Pattern'];
    function unique(items, name) {
        var matches = items.filter(function (item) { return item.name() === name; });
        if (matches.length !== 1) throw Error('Menu entry is absent or ambiguous: ' + name);
        return matches[0];
    }
    try {
        var processes = se.applicationProcesses.whose({bundleIdentifier:'com.image-line.flstudio'})();
        if (processes.length !== 1) throw Error('One running FL Studio instance is required');
        var process = processes[0];
        process.frontmost = true;
        var add = unique(process.menuBars()[0].menuBarItems(), 'Add');
        add.click(); opened = true;
        var root = add.menus()[0];
        if (command.action === 'list') {
            var rows = [];
            function walk(menu, path, kind) {
                if (path.length > 11) throw Error('Plugin menu nesting is too deep');
                menu.menuItems().forEach(function (item) {
                    var name = item.name();
                    if (!name || !item.enabled() || denied.indexOf(name) !== -1) return;
                    var nextKind = path.length === 1 && name === 'Effect' ? 'effect' : kind;
                    var nextPath = path.concat([name]);
                    var submenus = item.menus();
                    if (submenus.length) walk(submenus[0], nextPath, nextKind);
                    else rows.push({name:name, kind:nextKind, menu_path:nextPath});
                    if (rows.length > 512) throw Error('Plugin menu has more than 512 entries');
                });
            }
            walk(root, ['Add'], 'instrument');
            se.keyCode(53); opened = false;
            return JSON.stringify({entries:rows});
        }
        if (command.action !== 'load' || command.path[0] !== 'Add') throw Error('Unknown menu operation');
        var menu = root;
        var leaf = null;
        for (var i=1; i<command.path.length; i++) {
            leaf = unique(menu.menuItems(), command.path[i]);
            if (!leaf.enabled()) throw Error('Plugin menu entry is disabled');
            if (i < command.path.length-1) menu = leaf.menus()[0];
        }
        if (!leaf || denied.indexOf(leaf.name()) !== -1 || leaf.menus().length) throw Error('Not a plugin entry');
        attempted = true;
        leaf.click(); opened = false;
        return JSON.stringify({status:'dispatched'});
    } catch (error) {
        if (opened && !attempted) { try { se.keyCode(53); } catch (_) {} }
        return JSON.stringify({status:attempted ? 'unknown_outcome' : 'not_dispatched', error:String(error)});
    }
}
'''


class MacOSPluginMenu:
    def _run(self, action: str, path: tuple[str, ...] = ()) -> dict[str, object]:
        if os.environ.get("FL_BRIDGE_SANDBOXED") == "1":
            raise PluginLoadingError("Desktop plugin loading is disabled in the offline test environment")
        if platform.system() != "Darwin":
            raise PluginLoadingError("Named plugin menu loading currently supports macOS only")
        completed = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", _MENU_SCRIPT,
             json.dumps({"action": action, "path": path}, ensure_ascii=True)],
            check=False, capture_output=True, text=True, timeout=25,
        )
        if completed.returncode:
            raise PluginLoadingError((completed.stderr or "FL menu automation failed")[:1024])
        if len(completed.stdout) > 256 * 1024:
            raise PluginLoadingError("FL menu response exceeded its bound")
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise PluginLoadingError("FL menu returned a malformed response")
        return payload

    def entries(self) -> tuple[PluginMenuEntry, ...]:
        payload = self._run("list")
        if "entries" not in payload:
            raise PluginLoadingError(str(payload.get("error", "FL Add menu is unavailable")))
        rows = TypeAdapter(tuple[PluginMenuEntry, ...]).validate_json(json.dumps(payload["entries"]))
        if len(rows) > MAX_MENU_ENTRIES:
            raise PluginLoadingError("FL menu exceeded 512 entries")
        return rows

    def load(self, entry: PluginMenuEntry) -> MenuDispatch:
        try:
            return MenuDispatch.model_validate(self._run("load", entry.menu_path))
        except Exception as exc:
            # A timeout/error may occur after the menu click. Never retry it.
            return MenuDispatch(status="unknown_outcome", error=str(exc)[:1024])


def list_available_plugins(*, backend: MenuBackend | None = None) -> PluginMenuInventory:
    with _PIANO_ROLL_DISPATCH_LOCK:
        try:
            entries = (backend or MacOSPluginMenu()).entries()
            return PluginMenuInventory(observed_at=_now(), platform=platform.system(), supported=True, entries=entries)
        except Exception as exc:
            return PluginMenuInventory(observed_at=_now(), platform=platform.system(), supported=False, error=str(exc)[:1024])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _observe(request: PluginLoadRequest) -> tuple[LoadedPlugin, ...]:
    if request.kind == "instrument":
        observed = TrackBInspector().list_channels()
        if observed.partial or observed.scanned_channel_count != observed.total_channel_count:
            raise PluginLoadingError("A complete Channel Rack observation is required to identify the added instrument")
        return tuple(LoadedPlugin(index=row.channel_index, name=row.generator.name if row.generator else row.name)
                     for row in observed.channels)
    raw = get_client().call("mixer.track", track=request.track_index)
    if raw.get("index") != request.track_index:
        raise PluginLoadingError("FL returned a different mixer destination")
    plugins = raw.get("plugins")
    if not isinstance(plugins, list):
        raise PluginLoadingError("FL returned no mixer slot inventory")
    return tuple(LoadedPlugin.model_validate({"index": row["slot"], "name": row["name"]}, strict=True)
                 for row in plugins)


def _new_instance(before: tuple[LoadedPlugin, ...], after: tuple[LoadedPlugin, ...], name: str) -> LoadedPlugin | None:
    previous = {row.index: row.name for row in before}
    current = {row.index: row.name for row in after}
    if len(previous) != len(before) or len(current) != len(after):
        return None
    if len(current) != len(previous) + 1 or any(current.get(index) != value for index, value in previous.items()):
        return None
    added = [row for row in after if row.index not in previous]
    return added[0] if len(added) == 1 and _name_key(added[0].name) == _name_key(name) else None


def _name_key(name: str) -> str:
    # FL's native menu says "3x Osc" while its generator API says "3xOsc".
    return normalize_search_text(name).replace(" ", "")


def load_plugin(request: PluginLoadRequest, *, backend: MenuBackend | None = None) -> PluginLoadResult:
    """Add one exact named plugin and identify one new channel or effect slot."""
    request = PluginLoadRequest.model_validate(request.model_dump())
    menu = backend or MacOSPluginMenu()
    with _PIANO_ROLL_DISPATCH_LOCK:
        client = get_client()
        connection = connection_from_ping(client.ping(), getattr(client, "transport", "unknown"))
        session = connection.session_fingerprint
        if not connection.connected or not connection.compatible or session is None:
            raise PluginLoadingError(connection.error or connection.compatibility_reason)
        if request.session_fingerprint is not None and request.session_fingerprint != session:
            raise PluginLoadingError("The requested FL session changed")
        matches = [row for row in menu.entries() if row.kind == request.kind and _name_key(row.name) == _name_key(request.name)]
        if len(matches) != 1:
            return PluginLoadResult(observed_at=_now(), request=request, status="not_dispatched",
                session_fingerprint=session, warnings=("The requested plugin is missing or ambiguous in FL's Add menu. Inspect plugins_list_available for exact names.",))
        entry = matches[0]
        before = _observe(request)
        if request.kind == "effect" and len(before) >= 10:
            raise PluginLoadingError("The selected mixer track has no free effect slot")
        owned_mode = False
        warning: list[str] = []
        dispatch = MenuDispatch(status="not_dispatched")
        added = None
        try:
            if request.kind == "effect":
                assert request.track_index is not None
                if not connection.verified_writes_enabled:
                    owned_mode = True
                    WriteModeManager().set_write_mode(enabled=True, confirm_user_present=True, session_fingerprint=session)
                receipt = VerifiedWriter().select_mixer_track(track_index=request.track_index, allow_master=request.allow_master, session_fingerprint=session)
                if not receipt.verified:
                    raise PluginLoadingError("FL did not select the requested mixer track")
            if client.ping().get("session_fingerprint") != session:
                raise PluginLoadingError("FL changed projects before plugin loading")
            # A backend exception can follow a successful click. Mark the
            # attempt before entering it, so callers never infer a safe retry.
            dispatch = MenuDispatch(status="unknown_outcome")
            dispatch = menu.load(entry)
            if dispatch.status == "dispatched":
                deadline = time.monotonic() + request.timeout_seconds
                while True:
                    if client.ping().get("session_fingerprint") != session:
                        warning.append("FL changed projects during plugin loading")
                        break
                    after = _observe(request)
                    added = _new_instance(before, after, entry.name)
                    if added is not None:
                        break
                    if time.monotonic() >= deadline:
                        warning.append("FL did not report one unambiguous matching addition before the observation deadline; inspect its inventory before another load")
                        break
                    time.sleep(0.2)
        except Exception as exc:
            warning.append(str(exc)[:1024])
        finally:
            if owned_mode:
                try:
                    WriteModeManager().set_write_mode(enabled=False, session_fingerprint=session)
                except Exception as exc:
                    warning.append("Could not close the temporary write mode: " + str(exc)[:512])
        if dispatch.error:
            warning.append(dispatch.error)
        status = "loaded" if added else "not_dispatched" if dispatch.status == "not_dispatched" else "unknown_outcome"
        return PluginLoadResult(observed_at=_now(), request=request, status=status,
            verified=added is not None, dispatched=dispatch.status != "not_dispatched", loaded_plugin=added,
            session_fingerprint=session, menu_path=entry.menu_path, warnings=tuple(warning))
