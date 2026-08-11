# Tool and command reference

Postfader exposes 24 MCP tools. The MCP layer is the supported
public interface; the bridge commands are its local implementation protocol.
There is no generic command-dispatch tool.

Every MCP response uses a strict Pydantic model that rejects unknown fields
and non-finite numbers. Inspection and audio tools are annotated read-only.
The ten `fl_set_*` tools are annotated mutating and destructive because they
change the open FL Studio project immediately.

## Inspection tools

| Tool | Purpose |
| --- | --- |
| `fl_get_capabilities` | Report direct, partial, unavailable, and unvalidated integration paths. Call this before relying on a feature. |
| `fl_get_project_summary` | Read project metadata, counts, PPQ, dirty state, undo position, version, and a transport observation. |
| `fl_get_transport_state` | Read playback, recording, loop mode, position, and song length. |
| `fl_get_selected_range` | Return repeated raw Playlist endpoints and project PPQ without interpreting render semantics. |
| `fl_list_mixer_tracks` | List mixer tracks, levels, selection state, routing, and loaded effects. `only_used=false` is authoritative; peaks and a page limit are optional. |
| `fl_inspect_mixer_track` | Read one track's state, effects, built-in EQ, and outgoing routes. Track 0 is Master. |
| `plugins_scan_loaded_plugins` | Inventory the effects loaded on the observed mixer tracks. |
| `plugins_inspect_parameter_map` | Read a bounded page of one plug-in's exposed parameters by track and slot. |
| `plugins_scan_parameters` | Walk a bounded parameter range inside FL Studio and return named or display-bearing controls without VST padding. |
| `copilot_capture_readonly_inspection` | Capture project, mixer, and bounded plug-in previews under one observation ID. |

### Unprofiled parameters

Every unprofiled parameter is returned with
`profile_status="unprofiled_read_only"` and `safe_to_modify=false`. A caller
may inspect the normalized value, name, and optional display string; the
connector does not infer the control's meaning or promote it to safe.

### Playlist selection

`fl_get_selected_range` reads both raw endpoints twice so a caller can reject a
pair that moved during observation. Image-Line does not document enough public
semantics to infer selection presence, units, normalized ticks, or render
inclusivity. Those fields remain unknown or null, and every ordinary result is
marked unsafe for automated rendering.

### Parameter scans

`plugins_inspect_parameter_map` reads a page of 1–128 raw indices. It is useful
for a small native parameter map or a known index.

`plugins_scan_parameters` is preferable for a padded VST parameter space. It
accepts optional `start`, exclusive `end`, `max_indices` up to 8,192, and
`max_results`. Check its `truncated` field before treating the response as a
complete control map.

## Write tools

Writes are available only when the live bridge reports
`verified_writes_enabled: true`. Each tool changes one target, yields to a
later FL Studio idle tick, reads the target back, and returns a verdict. There
is no MCP-level confirmation round-trip and no automatic rollback.

| Tool | Required target and value | Bridge command |
| --- | --- | --- |
| `fl_set_mixer_volume` | `track_index`; `volume_normalized` 0–1; optional `allow_master` | `mixer.set_volume` |
| `fl_set_mixer_pan` | `track_index`; `pan` -1–1; optional `allow_master` | `mixer.set_pan` |
| `fl_set_mixer_mute` | `track_index`; absolute `muted` state; optional `allow_master` | `mixer.set_mute` |
| `fl_set_track_eq` | `track_index`; `band_index` 0–2; gain and/or frequency normalized 0–1; optional `allow_master` | `mixer.set_eq` |
| `fl_set_mixer_name` | `track_index`; `name`; optional `allow_master` | `mixer.set_name` |
| `fl_set_mixer_send` | source and destination track indices; absolute `enabled` state; optional `allow_master` for a Master source | `mixer.set_send` |
| `fl_set_mixer_send_level` | source and destination track indices; level normalized 0–1; optional `allow_master` for a Master source | `mixer.set_send_level` |
| `fl_set_plugin_param` | track and slot; parameter index; normalized value 0–1; optional `allow_master` | `plugin.set_param` |
| `fl_set_plugin_param_display` | track and slot; parameter index or text; numeric target in displayed units; optional tolerance and `allow_master` | `plugin.set_param_display` |
| `fl_set_plugin_param_option` | track and slot; parameter index or text; option text; optional sweep resolution and `allow_master` | `plugin.set_param_option` |

The volume and send scales follow FL Studio's normalized controls: `0.8` is
unity/0 dB. Built-in EQ gain uses `0.5` as flat. An empty mixer name restores
FL Studio's default track label.

Sending *to* Master does not require `allow_master`; sending *from* mixer track
0 does. A send route must exist before its level can be read or changed.

### Write response contract

Every write response identifies the bridge command and target and includes:

- the requested value;
- `before` and `after` observations;
- `verified` and a plain-language `verification_summary`;
- `verification_basis: "readback_on_a_later_fl_idle_tick"`;
- `undo_point_created: true`, `false`, or `null`;
- `project_saved: false`; and
- warnings, with an `UNVERIFIED:` warning first when applicable.

`undo_point_created: true` means the exposed undo-history count or position
changed around the request. False means it demonstrably did not, and null
means FL Studio did not expose enough state to decide. False and null must not
be treated as reversible.

`fl_set_track_eq` reports gain and frequency verification separately when both
are requested. `fl_set_plugin_param` additionally reports the plug-in and
parameter names, display change, numeric destination check, and one of these
proof strengths:

- `value_readback`: the numeric destination was observed;
- `display_change_only`: movement was observed, but the numeric accessor
  remained stale; or
- `none`: neither observation proved the write.

`verified: false` is a result, not a transport error. FL Studio accepted the
call but the later observation did not prove the requested outcome.

The setter itself is repeated inside a single write, because FL drops a lone
`setParamValue`: a mixer write makes up to `WRITE_ATTEMPTS` (2) attempts and a
plug-in parameter write up to four attempts of two calls each, each judged by
readback. What never happens is the part that would be unsafe — an unproven or
ambiguous write is not replayed afterwards, and a write that landed is not
rolled back. The one exception is a failed option search, which moved the
control to look and therefore attempts a *verified* restore and tells you
whether the restore itself was proven.

### Setting plug-in parameters

`fl_set_plugin_param` is appropriate when the caller already knows the exact
normalized value.

`fl_set_plugin_param_display` searches for a numeric value in the units the
plug-in displays, such as milliseconds, decibels, or hertz. The normalized
curve is not assumed. The parameter can be addressed by index or by text
matched against its name and display string.

`fl_set_plugin_param_option` handles controls that display words, such as a
key, scale, mode, or input type. FL Studio cannot list an enumeration, so the
tool sweeps normalized values while recording the displayed options. **This
moves the control through intermediate values.** If the requested option is
not found, the bridge attempts to restore the original value before returning
an error. Do not run this tool during recording.

Native Image-Line effects and third-party VST3 effects can both expose
addressable parameters. A reported VST count can contain thousands of padding
and MIDI CC entries, so scan before selecting a parameter and never treat the
reported count as proof that an index is a meaningful control.

### Refusals before dispatch

| Refusal | Meaning |
| --- | --- |
| `VerifiedWritesUnavailable` | The live bridge does not report the verified write surface. Relaunch FL Studio with `FL_BRIDGE_ENABLE_WRITES=1`. |
| `IncompatibleFLStudio` | The live handshake failed the FL Studio version, program-title, MIDI API, or bridge-protocol gate. |
| `ValueError` | A value is out of range, an EQ call names no field to change, a route is invalid, or mixer track 0 lacks explicit authorization. |
| Argument validation | An unknown, misspelled, or incorrectly typed MCP argument was rejected by the strict schema. |

## Audio tools

The FL Studio scripting API provides no live audio. These tools read audio
files from disk and return measurements, provenance, confidence, and explicit
limitations. They do not rank a mix, choose a candidate, or prescribe a
processing move.

| Tool | Purpose |
| --- | --- |
| `audio_analyze_file` | Measure level, spectrum, dynamics, stereo, and optionally monophonic pitch. Pitch is useful for a lead vocal or other monophonic stem and unreliable on a full mix. |
| `audio_compare_files` | Measure per-band deltas over the time-aligned, loudness-matched common overlap of a reference and candidate. |
| `audio_analyze_masking` | Measure per-band vocal/instrument spectral overlap and level margins. Inputs must cover the same section sample-synchronously. |
| `audio_find_recent_bounces` | List recent audio files under the fixed, bounded FL Studio output and project roots. |

The three measurement tools accept `max_seconds` from 1 to 600; the default is
600 seconds. Direct inputs must be absolute paths to regular files, may not
contain a `..` component, must use an allowed audio extension, and must not
exceed 512 MiB. A direct path can be outside FL Studio's normal folders.

The supported extension allowlist is `.wav`, `.wave`, `.w64`, `.rf64`, `.aif`,
`.aiff`, `.aifc`, `.flac`, `.ogg`, `.caf`, and `.mp3`, subject to the codecs
available through the installed audio library.

Results include canonical paths and SHA-256 hashes. They do not contain audio
samples. `audio_find_recent_bounces` reads directory metadata only, skips
hidden entries and directory symlinks, and bounds both depth and work.

## Bridge commands

The MCP server maps its tools to these local protocol commands.

### Always available

| Command | Arguments | Returns |
| --- | --- | --- |
| `ping` | none | Protocol, FL Studio and MIDI API versions, bridge mode, write availability, source hash, and program title. |
| `project.info` | none | Project metadata, counts, transport, dirty state, and undo tokens. |
| `arrangement.selection` | none | Raw endpoints read twice with PPQ and time hints. |
| `mixer.list` | `only_used`, `include_peaks`, optional `max_tracks` | Mixer tracks with levels, routes, and effect slots. |
| `mixer.track` | `track` | One track's complete inspection. |
| `plugin.params` | track, slot, page and filter arguments | One bounded parameter page. |
| `plugin.scan_params` | track, slot, and bounded scan arguments | De-padded controls and scan progress. |
| `channels.list` | none | Channel Rack contents. |

### Verified writes

The verified bridge commands use the same names shown in the write-tool table.
All refuse a Master source unless `allow_master` is true, request one undo
point, report readback, and never save the project. A command disabled by the
active mode is absent from `available`; it is not merely rejected inside its
handler.
