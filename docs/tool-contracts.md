# Tool and command reference

PostFader exposes 90 MCP tools and 8 MCP resources. The MCP layer is the supported
public interface; the bridge commands are its local implementation protocol.
There is no generic command-dispatch tool.

Every MCP response uses a strict Pydantic model that rejects unknown fields
and non-finite numbers. The surface contains 36 read-only tools, 39 directly
guarded FL setters, 8 specialized mutating workflows, 6 non-destructive
workflow/dispatch tools, and one session-mode control. `fl_set_write_mode` is a
destructive capability change but an idempotent, session-only absolute state.

## Inspection tools

| Tool | Purpose |
| --- | --- |
| `fl_get_capabilities` | Report direct, partial, unavailable, and unvalidated integration paths. Call this before relying on a feature. |
| `fl_get_project_summary` | Read project metadata, counts, PPQ, dirty state, undo position, version, and a transport observation. |
| `fl_get_transport_state` | Read playback, recording, metronome, precount, time-signature numerator, loop mode, position, and song length. |
| `fl_get_selected_range` | Return repeated raw Playlist endpoints and project PPQ without interpreting render semantics. |
| `fl_list_mixer_tracks` | List mixer tracks, levels, selection state, routing, and loaded effects. `only_used=false` is authoritative; peaks and a page limit are optional. |
| `fl_inspect_mixer_track` | Read one track's state, effects, built-in EQ, and outgoing routes. Track 0 is Master. |
| `plugins_scan_loaded_plugins` | Inventory effects loaded on observed mixer tracks; optionally include Channel Rack generators with explicit target kinds. |
| `plugins_inspect_parameter_map` | Read a bounded page of one mixer effect or Channel Rack generator's exposed parameters. |
| `plugins_scan_parameters` | Walk a bounded parameter range for either plug-in target and return named or display-bearing controls without VST padding. |
| `copilot_capture_readonly_inspection` | Capture project, mixer, and bounded plug-in previews under one observation ID. |
| `fl_list_channels` | List globally indexed Channel Rack targets, mix/identity/routing state, generator identity, and an observation-scoped fingerprint. |
| `fl_get_step_sequence` | Read one globally indexed channel's bounded sixteenth-note grid on the explicitly named current pattern and return a conflict digest. |
| `fl_list_patterns` | List bounded pattern identity, color, length, current state, and default-empty evidence. |
| `fl_find_empty_pattern` | Find a default/empty pattern without changing the current pattern. |
| `fl_list_playlist_tracks` | List every one-based Playlist track and its controllable identity/state. |
| `fl_get_project_history` | Read undo/redo bounds, current position, history hint, and dirty state. |
| `fl_get_plugin_preset_count` | Read FL's authoritative preset count for one explicit effect or generator target. |

## Live resources

Resources are read through the same contracts and compatibility gates as the
corresponding tools. They do not bypass read-only mode.

| URI | Content |
| --- | --- |
| `fl://capabilities` | Compatibility, provenance, current write availability, and capability statuses. |
| `fl://status` | Compact project, transport, write-mode, and session context. |
| `fl://project` | Typed project summary. |
| `fl://transport` | Current transport and recording-option state. |
| `fl://mixer` | Complete bounded mixer inventory without peak sampling. |
| `fl://channels` | Global Channel Rack inventory. |
| `fl://plugins` | Loaded mixer effects and channel generators. |
| `fl://patterns` | Current bounded pattern inventory. |

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

## Session write-mode control

`fl_set_write_mode` exposes or locks the bounded project-write tools without
restarting FL Studio. Its arguments are:

| Argument | Meaning |
| --- | --- |
| `enabled` | Absolute session state: `true` enables the bounded write surface; `false` locks it. |
| `confirm_user_present` | Must be literal `true` when enabling, after the present user explicitly requested the capability change. It defaults to `false` and is not required when disabling. |

Before enabling, the host requires a compatible running bridge, a matching
stamped source hash, runtime-control support, and a valid live session
fingerprint. The bridge checks that fingerprint and confirmation again. The
host then performs a second handshake and reports success only if that new
handshake confirms `bridge_mode="write_test"`,
`verified_writes_enabled=true`, and `write_mode_origin="runtime_request"`.

The result records before/after state, whether it changed, the session
fingerprint, confirmation proof, startup default, `session_only: true`,
`project_saved: false`, and
`verification_basis="post_transition_bridge_handshake"`. Disabling reports
the corresponding read-only handshake. A normal FL Studio process or bridge
reload starts read-only again unless the legacy startup environment opt-in was
used for that FL process.

## Write tools

State writes are available only when the live bridge reports
`verified_writes_enabled: true` and its stamped source SHA-256 matches the
bridge packaged with the server. Each tool changes one target, yields to a
later FL Studio idle tick, reads the target back, and returns a verdict. There
is no per-write MCP confirmation round-trip and no automatic rollback. Ask the
client to enable the session first with `fl_set_write_mode`.

Every state tool accepts an optional bridge-lifetime `session_fingerprint` and
a typed `expected_before`, except `fl_set_step_sequence`, whose required
`expected_digest` is its stronger state guard. The bridge checks supplied
guards immediately before undo and mutation. Omitting them preserves the 0.11
call shape; supplying them makes stale decisions fail closed.

| Tool | Required target and value | Bridge command |
| --- | --- | --- |
| `fl_set_mixer_volume` | `track_index`; `volume_normalized` 0–1; optional `allow_master` | `mixer.set_volume` |
| `fl_set_mixer_volume_db` | `track_index`; displayed fader target -60 to +6 dB; optional tolerance and `allow_master` | `mixer.set_volume_db` |
| `fl_set_mixer_pan` | `track_index`; `pan` -1–1; optional `allow_master` | `mixer.set_pan` |
| `fl_set_mixer_mute` | `track_index`; absolute `muted` state; optional `allow_master` | `mixer.set_mute` |
| `fl_set_mixer_solo` | `track_index`; absolute `soloed` state; optional `allow_master` | `mixer.set_solo` |
| `fl_set_mixer_arm` | `track_index`; absolute recording-arm state; optional `allow_master` | `mixer.set_arm` |
| `fl_set_mixer_color` | `track_index`; unsigned FL color word; optional `allow_master` | `mixer.set_color` |
| `fl_set_mixer_stereo_separation` | `track_index`; absolute -1–1 stereo separation; optional `allow_master` | `mixer.set_stereo_separation` |
| `fl_select_mixer_track` | Absolute active mixer `track_index`; optional `allow_master` | `mixer.select_track` |
| `fl_set_track_eq` | `track_index`; `band_index` 0–2; gain and/or frequency normalized 0–1; optional `allow_master` | `mixer.set_eq` |
| `fl_set_mixer_name` | `track_index`; `name`; optional `allow_master` | `mixer.set_name` |
| `fl_set_mixer_send` | source and destination track indices; absolute `enabled` state; optional `allow_master` for a Master source | `mixer.set_send` |
| `fl_set_mixer_send_level` | source and destination track indices; level normalized 0–1; optional `allow_master` for a Master source | `mixer.set_send_level` |
| `fl_set_plugin_param` | track and slot; parameter index; normalized value 0–1; optional `allow_master` | `plugin.set_param` |
| `fl_set_plugin_param_display` | track and slot; parameter index or text; numeric target in displayed units; optional tolerance and `allow_master` | `plugin.set_param_display` |
| `fl_set_plugin_param_option` | track and slot; parameter index or text; option text; optional sweep resolution and `allow_master` | `plugin.set_param_option` |

The three plug-in reads and three plug-in setters also accept an explicit
discriminated target. `mixer_effect` names `track_index`, slot 0–9, and optional
Master authorization. `channel_generator` names a global `channel_index`; the
bridge uses FL's separate `slotIndex=-1` form internally. A call may use that
target or the legacy mixer track/slot pair, never both.

### Transport, Channel Rack, and sequencer state

| Tool | Required target and value | Bridge command |
| --- | --- | --- |
| `fl_set_playing` | absolute `playing` boolean | `transport.set_playing` |
| `fl_stop` | no value; absolute stopped state plus normalized position 0 | `transport.stop` |
| `fl_set_song_position` | normalized position 0–1; transport must be stopped | `transport.set_song_position` |
| `fl_set_loop_mode` | absolute `pattern` or `song` mode | `transport.set_loop_mode` |
| `fl_set_tempo` | absolute 10–522 BPM; playback and recording must be stopped | `transport.set_tempo` |
| `fl_set_recording` | absolute recording-arm boolean | `transport.set_recording` |
| `fl_set_metronome` | absolute metronome boolean | `transport.set_metronome` |
| `fl_set_precount` | absolute recording-precount boolean | `transport.set_precount` |
| `fl_set_time_signature_numerator` | beats per bar 1–32; FL exposes no denominator getter | `project.set_time_signature_numerator` |
| `fl_undo` | move one position backward when undo is available | `project.undo` |
| `fl_redo` | move one position forward when redo is available | `project.redo` |
| `fl_set_channel_mix` | global channel; volume, pan, mute, or any combination | `channel.set_mix` |
| `fl_set_channel_solo` | global channel and absolute solo state | `channel.set_solo` |
| `fl_set_channel_pitch` | global channel and normalized pitch -1–1 | `channel.set_pitch` |
| `fl_select_channel` | select one global channel exclusively | `channel.select` |
| `fl_set_channel_identity` | global channel; name, color, or both. Color observations preserve FL's unsigned 32-bit `0x--BBGGRR` word; guards and proof compare the controllable low 24 bits because FL owns the high byte. | `channel.set_identity` |
| `fl_route_channel_to_mixer` | global channel and absolute mixer destination; `-1` is unassigned | `channel.route_to_mixer` |
| `fl_set_step_sequence` | explicit current pattern, global channel, required prior digest, and unique absolute cell updates | `sequencer.set` |

### Pattern and Playlist state

| Tool | Required target and value | Bridge command |
| --- | --- | --- |
| `fl_select_pattern` | absolute current pattern number | `pattern.select` |
| `fl_set_pattern_identity` | pattern number plus name, color, or both | `pattern.set_identity` |
| `fl_set_pattern_length` | pattern number plus absolute beat length | `pattern.set_length` |
| `fl_set_playlist_track_identity` | one-based Playlist track plus name, color, or both | `playlist.set_identity` |
| `fl_set_playlist_track_state` | one-based Playlist track plus mute, solo, selection, or a combination | `playlist.set_state` |

Multi-field stop, channel, EQ, and step responses include a proof flag for each
requested field or cell. Aggregate `verified` is the logical AND of those
flags. Step reads and writes refuse when `pattern_number` differs from FL's
current pattern; the connector never changes patterns implicitly.

Step reads can observe grids of up to 512 cells. A verified step write refuses
a grid longer than 256 cells because its final digest recheck and complete
batch must remain atomic within one FL idle tick. For an eligible grid, the
batch must satisfy:

```text
step_count + update_count + 8 <= 320
```

Thus a 256-cell pattern permits at most 56 updates in one call. Split a larger
edit into multiple batches and call `fl_get_step_sequence` again after every
successful batch; each subsequent call must use the newly returned digest.
These limits preserve headroom beneath the repository's fewer-than-400 FL API
calls per idle-tick gate.

### Bounded note audition

`fl_trigger_note` dispatches one note-on/note-off pair to a global Channel Rack
target with bounded note, velocity, MIDI channel, and duration. It can use the
same session and channel-fingerprint guards, but returns
`verification_basis="dispatch_only_no_state_readback"`. `dispatched: true`
means the bounded event pair was sent, not that sound was produced or heard.
While an audition is active, another audition for the same channel, note, and
MIDI-channel tuple is refused so the first note-off cannot release a later note.

The volume and send scales follow FL Studio's normalized controls: `0.8` is
unity/0 dB. Built-in EQ gain uses `0.5` as flat. An empty mixer name restores
FL Studio's default track label.

Sending *to* Master does not require `allow_master`; sending *from* mixer track
0 does. A send route must exist before its level can be read or changed.

### Write response contract

Every readback-verified state-write response identifies the bridge command and
target and includes:

- the requested value;
- `before` and `after` observations;
- `verified` and a plain-language `verification_summary`;
- `verification_basis: "readback_on_a_later_fl_idle_tick"`;
- `undo_point_created: true`, `false`, or `null`;
- `project_saved: false`; and
- warnings, with an `UNVERIFIED:` warning first when applicable.

State-write results additionally report the echoed session fingerprint plus
`session_precondition_applied` and `expected_before_applied`. These fields are
strictly validated; a malformed or contradictory bridge reply is an error, not
a plausible-looking success.

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

Mixer and plug-in parameter handlers may repeat an FL-facing setter inside one
dispatched bridge command because FL drops a lone `setParamValue`: a mixer
write makes up to `WRITE_ATTEMPTS` (2) calls and a plug-in parameter write up to
four attempts of two calls each, each judged by readback. Transport, direct
Channel Rack state, routing, and step setters are issued once. What never
happens is the unsafe part — a mutating bridge command is not dispatched again
after an ambiguous transport outcome, an unverified result is not retried for
the caller, and a write that landed is not rolled back. The one exception is a
failed option search, which moved the control to look and therefore attempts a
*verified* restore and tells you whether the restore itself was proven.

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
an error. The requested text must exactly match FL's actual parameter readback
label, ignoring case; substring matches are refused. That label can differ
from the conceptual term used by a plug-in's UI or manual (for example, 3x Osc
exposes `pulse` for its square-shaped LFO mode).
Do not run this tool during recording.

Native Image-Line effects and third-party VST3 effects can both expose
addressable parameters. A reported VST count can contain thousands of padding
and MIDI CC entries, so scan before selecting a parameter and never treat the
reported count as proof that an index is a meaningful control.

## Verified batch and production workflows

`fl_apply_verified_batch` accepts 1–32 operations from a closed discriminated
union. Supported operation kinds cover the direct mixer, plug-in, transport,
project-history, channel, pattern, Playlist, and step-sequencer setters. The
executor pins one compatible session and adds the session guard internally.
Each item keeps its original typed receipt.

A batch is ordered but not transactional. With `stop_on_unverified=true`, an
unverified result skips later items; a bridge or validation error stops the
batch. Earlier successful mutations remain applied. `completed`, `verified`,
`stopped_early`, counts, and ordered item outcomes make partial success
explicit. No automatic replay, rollback, or save occurs.

### Mix analysis and recommendations

| Tool | Purpose |
| --- | --- |
| `mix_doctor` | Diagnose a real candidate bounce against versioned thresholds, with optional real reference and synchronized masking inputs. |
| `mix_reference_recommendations` | Turn measured, aligned candidate/reference band deltas into bounded review ranges. |
| `mix_masking_recommendations` | Turn synchronized vocal/instrument masking evidence into bounded dynamic-remediation suggestions. |
| `mix_finish_assessment` | Run the complete read-only finish assessment and stop at the user-export boundary. |
| `mix_list_plugin_profiles` | List bundled parameter-role adapters and processing recipes. |
| `mix_inspect_plugin_compatibility` | Match currently loaded effects against those profiles without claiming an exact plug-in version. |
| `mix_resolve_processing_intent` | Resolve an outcome such as `reduce_mud`, `control_dynamics`, or `add_depth` to loaded, profiled controls without applying it. |

### Persistent metering and plans

| Tool | Purpose |
| --- | --- |
| `mix_start_peak_watch` | Start a process-local peak sampler for 1–3,600 seconds. |
| `mix_get_peak_watch` | Read cumulative peaks and sampling coverage for one watch. |
| `mix_stop_peak_watch` | Stop the watch and return its final aggregate. |
| `mix_create_gain_stage_plan` | Convert a watch into bounded dB-fader operations without applying them. |
| `mix_create_plan` | Store 1–32 reviewed batch operations against the current bridge session. |
| `mix_get_plan` | Read a stored plan and its lifecycle state. |
| `mix_apply_plan` | Apply a stored plan once through the verified batch executor. |

Peak watches and plans are in-memory process state. IDs cease to exist when
the MCP process exits. Creating a plan is not approval to apply it; plan
application is a distinct destructive, non-idempotent tool. Plan state is
`draft` before an attempt, `applied` only when the complete batch verifies,
`partial` when a batch receipt reports an incomplete or unverified result, and
`failed` when no batch receipt can be returned. Both terminal failure states
require a fresh plan rather than an automatic retry.

## Creative, MIDI, arrangement, and automation tools

### Deterministic composition and offline analysis

| Tool | Purpose |
| --- | --- |
| `compose_chord_progression` | Voice Roman-numeral triads/sevenths using a scale, mode, raga, or custom pitch collection. |
| `compose_melody` | Generate a seed-reproducible bounded melody with register, contour, and density controls. |
| `compose_bassline` | Generate roots, eighths, octaves, or walking bass against Roman harmony. |
| `compose_drums` | Generate GM-mapped house, hip-hop, trap, pop, or drum-and-bass notes. |
| `midi_export_type1` | Atomically write a standard Type-1 MIDI file, reopen it, parse it, and verify header, digest, tracks, and note events. Existing files require `overwrite=true`. |
| `audio_estimate_tempo_and_key` | Estimate periodic tempo with half/double-time candidates and rank global major/minor keys. |
| `audio_transcribe_melody` | Extract a reviewable, optionally beat-quantized note sequence from one isolated monophonic source. |

Composition returns a deterministic SHA-256 note digest and does not touch FL.
Tempo/key and transcription results include confidence and limitations; they
are estimates, not project metadata.

### Piano Roll bridge

| Tool | Purpose |
| --- | --- |
| `piano_roll_bridge` | Inspect setup, atomically prepare the bootstrap script, or confirm the user's one manual run for this process. |
| `piano_roll_write_notes` | Prepare append/replace note content and optionally target FL plus dispatch the run-last-script shortcut. |
| `piano_roll_transform` | Quantize, transpose, humanize, duplicate, delete, or clear selected/all live score notes. |

Automatic Piano Roll use requires the one-time prepare/manual-run/confirm
handshake. The normal bridge proves channel selection, pattern selection, and
Piano Roll visibility before dispatch. FL exposes no controller-side note
readback, so `application_verified=false` even when
`hotkey_dispatched=true`. `auto_trigger=false` writes the generated script for
manual execution without touching the live project.

### Arrangement and automation

| Tool | Purpose and evidence boundary |
| --- | --- |
| `arrangement_prepare_pattern` | Find an FL-reported empty pattern, then select, name/color, and size it through ordered direct verified writes. The workflow is non-atomic and returns `selection`, optional `identity`/`length`, plus an exact outcome if it stops after an unverified step. |
| `arrangement_add_section_markers` | Convert one-based bars/beat offsets through live PPQ and add up to 32 markers. Names are later-tick observed; times remain unverified because FL has no getter. |
| `automation_record_value` | Dispatch one public REC controller value while playing and recording. The controlled value and capture conditions are checked; automation-point existence remains unknown. |

The last two receipts deliberately keep aggregate `verified=false` where FL
cannot expose the fact needed to prove the whole requested outcome.

### Refusals before dispatch

| Refusal | Meaning |
| --- | --- |
| `WriteModeConfirmationRequired` | Enabling was requested without literal `confirm_user_present=true` from an explicit present-user request. |
| `WriteModeUnavailable` | Provenance, runtime-control support, session identity, command metadata, or the post-transition handshake did not safely prove the requested capability state. |
| `VerifiedWritesUnavailable` | The live bridge does not report the verified write surface. Ask the client to call `fl_set_write_mode(enabled=true, confirm_user_present=true)`. |
| `TrackBMutationsUnavailable` | A Track B mutation is disabled, the bridge provenance does not match, or a supplied session changed before dispatch. |
| `IncompatibleFLStudio` | The live handshake failed the FL Studio version, program-title, MIDI API, or bridge-protocol gate. |
| `ValueError` | A value is out of range, a multi-field call names no field to change, a route/current-pattern/digest/precondition is invalid, a plug-in selector is ambiguous, or mixer track 0 lacks explicit authorization. |
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

### Available in every mode

| Command | Arguments | Returns |
| --- | --- | --- |
| `ping` | none | Protocol, FL Studio and MIDI API versions, bridge mode, runtime-control support, write availability and origin, source hash, session fingerprint, and program title. |
| `session.set_write_mode` | absolute `enabled`, literal user-present confirmation, and current session fingerprint | Session-only before/after capability state; no project value is changed. |
| `project.info` | none | Project metadata, counts, transport, dirty state, and undo tokens. |
| `project.history` | none | Absolute undo/redo bounds, position, hint, and dirty state. |
| `arrangement.selection` | none | Raw endpoints read twice with PPQ and time hints. |
| `mixer.list` | `only_used`, `include_peaks`, optional `max_tracks` | Mixer tracks with levels, routes, and effect slots. |
| `mixer.peaks` | bounded track selection | One lightweight instantaneous peak frame. |
| `mixer.track` | `track` | One track's complete inspection. |
| `plugin.params` | track, slot, page and filter arguments | One bounded parameter page. |
| `plugin.scan_params` | track, slot, and bounded scan arguments | De-padded controls and scan progress. |
| `plugin.preset_count` | explicit effect/generator target | FL's reported preset count. |
| `channels.list` | none | Channel Rack contents. |
| `patterns.list`, `patterns.find_empty` | optional bounded starting pattern | Pattern inventory or first FL-reported empty pattern. |
| `playlist.list` | none | One-based Playlist track identity and state. |
| `sequencer.get` | explicit current pattern and global channel | Absolute bounded cells and canonical digest. |

### Verified writes

The direct state commands use the same names shown in the write-tool tables.
Mixer sources and mixer-effect targets refuse Master unless `allow_master` is
true. Every direct state command reports an undo observation, later-tick
readback, and `project_saved: false`. `channel.trigger_note` is dispatch-only.
`creative.prepare_piano_roll`, `arrangement.add_markers`, and
`automation.record_value` publish their narrower evidence rather than
borrowing the direct-write guarantee. A command disabled by the active mode is
absent from `available`; it is not merely rejected inside its handler.
