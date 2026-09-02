# Tool and command reference

PostFader exposes 127 MCP tools and 8 MCP resources on the current development branch. The MCP layer is the supported
public interface; the bridge commands are its local implementation protocol.
There is no generic command-dispatch tool.

Every MCP response uses a strict Pydantic model that rejects unknown fields
and non-finite numbers. Tool annotations distinguish read-only inspection,
directly guarded FL setters, non-destructive workflow/dispatch, and destructive
controls; the 13 Creation Review tools are documented in their own section
below.
`fl_set_write_mode` changes the session write capability;
`sound_selection_history_reset` deletes only the bounded local history after
explicit confirmation.

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
| `plugins_atlas_search` | Search the bundled, offline product catalogue by text and bounded static filters. |
| `plugins_atlas_get_product` | Read one bundled product by exact ID with related vendor, adapter, evidence, and stock-alternative records. |
| `plugins_atlas_recommend` | Rank bundled products or explicit stock alternatives from bounded production criteria. |
| `plugins_atlas_inspect_loaded` | Match the current target-aware loaded-plug-in inventory to Atlas records without asserting ownership or control proof. |
| `copilot_capture_readonly_inspection` | Capture project, mixer, and bounded plug-in previews under one observation ID. |
| `fl_list_channels` | List globally indexed Channel Rack targets, mix/identity/routing state, generator identity, and an observation-scoped fingerprint. |
| `fl_get_step_sequence` | Read one globally indexed channel's bounded sixteenth-note grid on the explicitly named current pattern and return a conflict digest. |
| `fl_list_patterns` | List bounded pattern identity, color, length, current state, and default-empty evidence. |
| `fl_find_empty_pattern` | Find a default/empty pattern without changing the current pattern. |
| `fl_list_playlist_tracks` | List every one-based Playlist track and its controllable identity/state. |
| `fl_get_project_history` | Read undo/redo bounds, current position, history hint, and dirty state. |
| `fl_get_plugin_preset_count` | Read FL's authoritative preset count for one explicit effect or generator target. |
| `plugins_list_presets` | Read one bounded, deterministic page of exact preset index/name records, current identity, duplicate names, blank names, and partial/truncated status. |
| `plugins_get_current_preset` | Read the current preset name and a preset index only when FL resolves it uniquely. |
| `plugins_inspect_pad_map` | Read a generic target's reported pad count, MIDI/semitone notes, colors, empty/muted flags, and semitone names where exposed. |

## Sound Selection tools

Sound Selection plans a coherent palette from the currently loaded target pool.
The connected AI turns natural-language direction into a strict
`SoundSelectionRequest`; PostFader does not embed an LLM. User preferences,
exclusions, stock-only direction, locked roles, section scope, and required
drum mappings are hard constraints. The default `balanced` policy preserves
identity sounds within a song and uses bounded local history only to separate
similarly suitable candidates. Planning does not mutate FL or usage history.

| Tool | Purpose and boundary |
| --- | --- |
| `sound_selection_inventory` | Read a compact inventory of loaded Channel Rack generators and optional mixer effects, bounded preset pages, current presets, pad maps, target fingerprints, current palette/locks, Atlas matches, and Atlas-known products not observed as loaded. |
| `sound_selection_plan` | Deterministically rank loaded candidates for requested roles and return a `SoundPalettePlan` with score breakdowns, rationale, anchors, flexible roles, fallbacks, unused targets, conflicts, and blockers. No FL or history changes. |
| `sound_selection_get` | Look up process-local palette state and immutable apply receipts by palette ID. Expiry or another process returns an availability-honest result. |
| `sound_selection_create_variation` | Plan a section-scoped delta from an existing palette. Anchors remain unchanged unless explicitly replaced; the result is read-only. |
| `sound_selection_history_status` | Read the local history path, schema/health, record counts, and configured bounds. |
| `sound_selection_apply` | Authorized mutating workflow that requires the current 32-character lowercase `session_fingerprint`, revalidates the session and loaded targets, applies exact assignments in deterministic order, and stops on the first unknown/unverified result. Earlier receipts remain immutable. |
| `sound_selection_record_feedback` | Store an explicit accepted, rejected, or neutral local verdict when persistence is enabled. Silence is never inferred as acceptance. It does not mutate FL. |
| `sound_selection_history_reset` | Explicitly remove the local Sound Selection history after `confirm=true`; project state is unchanged and the removed file is not recoverable by PostFader. |

Sound Selection uses the same discriminated Track B target as parameter tools:
`mixer_effect` addresses a mixer track and slot, while `channel_generator`
addresses a global Channel Rack generator. Atlas product matches never prove
installation, ownership, loaded state, or control. An unprofiled loaded target
can still participate with lower semantic confidence.

`source_strategy="instrument_pool"` is the default and excludes Loop Starter.
Loop Starter rerolling is available only for an explicit Loop Starter request;
FL does not expose authoritative selected-loop identity, so that result is
dispatch-only rather than a verified sound selection.

## Creation readiness and semantic processing

These focused tools support inspection and debugging. A normal end-to-end
creation request should use one `postfader_execute_run`; it invokes the same
readiness service internally and carries one context through the phases.

| Tool | Purpose and boundary |
| --- | --- |
| `postfader_creation_readiness` | Zero-mutation scorecard across connection/bridge, Piano Roll, instrument pool, drum coverage, patterns/arrangement, mixer effects, and manual scope. Returns blockers together with a reusable bounded context snapshot. |
| `processing_plan` | Read-only effect coverage and semantic plan from loaded, Atlas-matched, adapter-backed, runtime-observed controls. |
| `processing_apply_plan` | One explicitly authorized processing run using existing verified setters and later-tick readback; missing/unresolved controls remain visible. |

Creation plans can include `plan_sound_palette`,
`apply_sound_palette`, `inspect_drum_map`, deterministic generators,
`adapt_note_sequence`, Piano Roll writes, `plan_processing`, and
`apply_processing_plan`. Sound-aware adaptation carries characteristic
provenance/confidence and records register, envelope, articulation, density,
and polyphony decisions. The run's `timing_report` is local diagnostic data,
not telemetry. Its `creation_outcome` separates technical execution,
arrangement delivery, processing, audible quality, and manual handoff;
`audible_quality.status="not_evaluated"` is the default until user or bounce
evidence exists.

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

Every direct state-write tool accepts an optional bridge-lifetime
`session_fingerprint` and a typed `expected_before`, except
`fl_set_step_sequence`, whose required `expected_digest` is its stronger state
guard. The bridge checks supplied guards immediately before undo and mutation.
Omitting them preserves the 0.11 call shape; supplying them makes stale
decisions fail closed. The high-level `sound_selection_apply` workflow is the
exception: its public MCP contract requires `session_fingerprint` because the
palette service cannot apply without a live session token.

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
| `fl_set_plugin_param` | explicit mixer-effect or channel-generator target, or legacy mixer track/slot; parameter index; normalized value 0–1; optional `allow_master` for Master effects | `plugin.set_param` |
| `fl_set_plugin_param_display` | same target forms; parameter index or text; numeric target in displayed units; optional tolerance and `allow_master` for Master effects | `plugin.set_param_display` |
| `fl_set_plugin_param_option` | same target forms; parameter index or text; option text; optional sweep resolution and `allow_master` for Master effects | `plugin.set_param_option` |
| `fl_select_plugin_preset` | explicit target plus exact `preset_name` and/or `preset_index`; optional current/session/target guards and bounded navigation/settling limits | `plugin.select_preset` |

The parameter reads and three plug-in setters also accept an explicit
discriminated target. `mixer_effect` names `track_index`, slot 0–9, and optional
Master authorization. `channel_generator` names a global `channel_index`; the
bridge uses FL's separate `slotIndex=-1` form internally. A call may use that
target or the legacy mixer track/slot pair, never both.

### Preset identity and navigation

`plugins_list_presets` reads a page with `start` and `limit` (1–256), and never
enumerates an unbounded catalog in one response. It reports FL's authoritative
count, index/name rows, current name/index/status, duplicate names, blank
indices, `partial`, `truncated`, and a deterministic continuation position.
`plugins_get_current_preset` reports the current name and only a uniquely
resolved index. A count-only `fl_get_plugin_preset_count` result does not prove
current identity.

`fl_select_plugin_preset` resolves the requested exact name/index against live
state, refuses an ambiguous duplicate-name request unless an index is supplied,
and uses the shortest next/previous path when the current index is known.
Without a known index it uses a bounded fallback search. The bridge yields over
later FL idle ticks, allows bounded settling, and returns success only after
exact current-preset readback matches the request. It reports before/after
identity, direction, step count, target/session guards, and `undo_point_created`
as the value FL exposed (`true`, `false`, or `null`). Dispatch alone is never
success. An unstable identity, stale guard, navigation limit, or unknown
transport outcome stops the operation; it is never automatically replayed,
rolled back, or silently redirected to another preset.

`plugins_inspect_pad_map` is a read-only generic pad observation. Sound
Selection uses its notes and reported names to build semantic drum maps and
does not assume General MIDI. `compose_drums` can consume the resulting typed
map; its General MIDI notes remain an explicit fallback only when no map is
provided. Missing required drum roles block before note writing.

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

## Production Runs

Production Runs are bounded, process-local orchestration records for one
task-scoped request. The connected AI translates the user's objective into a
typed request and a closed ordered plan; PostFader validates scope,
preservation rules, dependencies, references, live capabilities, and session
state before applying any operation. They are not a persistent autonomous
mode.

| Tool | Purpose |
| --- | --- |
| `postfader_creation_readiness` | Aggregate all detectable creation blockers and limitations without enabling writes or changing FL Studio. |
| `postfader_validate_run` | Read-only structural and live-capability validation. Returns the deterministic digest, operation order, required capabilities, expected mutation categories, warnings, and blockers without enabling writes. |
| `postfader_execute_run` | Validate and execute one authorized plan. The existing write boundary is enabled once for the run, receipts are retained in order, and execution stops on an unverified or unknown mutation outcome. |
| `postfader_get_run` | Read a process-local run state, generated outputs, receipts, blockers, and concise summary. |
| `postfader_continue_run` | Append operations or replace only the unexecuted remainder. Completed receipts are immutable, and the FL session and project checkpoint must still match. |
| `postfader_stop_run` | Stop future operations. It does not undo completed changes or claim rollback. |

The MVP operation union includes deterministic chord, melody, bass, and drum
generation; exact pattern preparation or selection; Piano Roll writes and
supported transforms; section markers; supported automation values; and the
existing closed verified batch. Note-sequence references may point only to an
earlier compatible generator output. All plans and histories are bounded.

Creation plans add typed sound-palette and processing references to that same
union. A complete mutating run uses one readiness preflight, one task-scoped
authorization, one in-memory write-mode enable, and one verified shutdown.
The phases are `preflight`, `palette`, `composition`, `note_application`,
`processing`, and `finalization`; omitted work is marked skipped. A dry
composition result reports `not_requested` or `dry_by_design`, while missing
effect coverage reports `dry_missing_effects` or a partial state. No result
claims that a preset or mix was heard, and no run saves automatically. See
[Production Runs](production-runs.md) and [Creation Pipeline](creation-pipeline.md).

Runs disappear when the MCP process exits or when the bounded registry evicts
them. A Production Run never renders, inserts plug-ins, creates Playlist clips,
saves the FL Studio project, retries an ambiguous mutation, or claims an
automatic rollback. See [Production Runs](production-runs.md) for examples and
current limitations.

## Creation Review, revision, and delivery

Creation Review is a bounded continuation of one completed Production Run. It
keeps the source-run snapshot and its receipts immutable, evaluates explicit
caller-selected bounces, records producer feedback and independent locks,
compiles one closed revision plan, and prepares comparison and delivery
evidence. The public surface contains 13 dedicated MCP tools:

| Tool | Purpose and boundary |
| --- | --- |
| `postfader_review_start` | Open a Review Session from one completed Production Run. The request carries the review brief, scope, preservation rules, feedback, revision budget, evaluation policy, and opt-in persistence settings; it does not change FL Studio. |
| `postfader_review_attach_assets` | Validate and attach an explicit full mix, before/after bounce, reference, synchronized stem, or section bounce. Paths must be caller-selected regular audio files; attaching never changes FL Studio. |
| `postfader_review_evaluate` | Measure one attached asset set globally and against the known section map, with optional tempo, time signature, and export offset. It reports findings and limitations and applies zero FL mutations. |
| `postfader_review_get` | Read a process-local or persisted Review Session, retained evidence, status, blockers, and exact next action. |
| `postfader_review_compare` | Compare distinct before and after asset IDs when their digests, channels, duration, offsets, and section alignment are usable. An `after_full_mix` must reference its recorded revision pass; recording comparison advances that pass from `attached` to `compared`. Technical improvements and regressions remain separate from user approval. |
| `postfader_review_plan_revision` | Compile and validate one closed, ordered `RevisionPlan` against findings/feedback, preserved elements, locks, source digests, live targets, scope, and risk limits. The plan binds to a canonical `RevisionRequest` digest; `authorized_to_modify` is excluded and must be asserted afresh at apply. Planning never mutates FL Studio. |
| `postfader_delivery_manifest` | Build the current read-only delivery view, including independent technical, arrangement, processing, audible-quality, approval, and manual-handoff states. It writes no file or project. |
| `postfader_review_export_handoff` | Return one exact next full-mix export request and only the stems needed to resolve an identified uncertainty. It does not render or discover arbitrary files. |
| `postfader_review_apply_revision` | Apply one recorded revision through the existing Production Run executor. A clear modification request uses one readiness preflight and one task-scoped authorization; stale or unknown outcomes stop without replay or rollback. If FL mutation completes but session persistence fails afterward, the result is blocked with a process-local receipt and no replay. |
| `postfader_review_record_feedback` | Store explicit structured producer feedback and any accepted-element locks. Silence, measurements, and model interpretation never become approval. |
| `postfader_review_stop` | Stop future work for one Review Session without undoing completed FL changes or rewriting receipts. |
| `postfader_review_delete` | Delete one Review Session's local metadata after `confirm=true`; audio files and the FL Studio project are untouched. The deleted metadata is not recoverable by PostFader. |
| `postfader_delivery_export_manifest` | Create-only export of the delivery view as JSON and/or Markdown. Existing files are never overwritten; the result includes the logical manifest digest and exact artifact SHA-256 hashes, and newly created companions are cleaned up if the paired write fails. The FL Studio project is never saved. |

The table intentionally names `postfader_delivery_manifest` once as a delivery
view tool; the 13-tool count is the 11 `postfader_review_*` tools, that one
delivery-view tool, and `postfader_delivery_export_manifest`.

Opt-in Review persistence is bounded versioned local JSON at
`<FL Studio user-data>/Settings/PostFader/creation-review-sessions-v1.json`,
unless `POSTFADER_CREATION_REVIEW_PATH` (or its compatibility alias) supplies
another absolute path. The request defaults to three revision passes (hard cap
eight); the store caps 64 sessions, 256 assets, 256 findings per evaluation, 32
evaluations, 64 comparisons, 32 delivery manifests, and a 16 MiB serialized
document. A private per-path advisory lock serializes writers across MCP
processes, and deterministic pruning keeps findings referenced by retained
plans or passes ahead of lower-ranked unreferenced findings. Durable and
delivery serializers remove credentials, prompts, transcripts, encoded or raw
audio, cloud identifiers, and arbitrary private paths; `persist_asset_paths=true`
permits only canonical attached `ReviewAudioAsset.path` fields. The public
delete tool requires explicit confirmation; store reset/repair is also explicit
and never automatic. A persistence failure after a verified FL mutation
returns a blocked process-local receipt and must not be replayed.

### Creation Review operations inside a Production Run

The same workflow is available to a typed Production Run through a closed set
of 9 review operations. Their names are distinct from the 13 top-level MCP
tools and can be linked with the run's typed output references:

| Operation | Result and boundary |
| --- | --- |
| `start_review_session` | Snapshot one completed source run into a bounded Review Session and expose retained authoritative sections as typed `section_definition` items; read-only local state. |
| `attach_review_assets` | Validate and retain explicit audio asset metadata for the referenced Review Session; no FL mutation. |
| `evaluate_creation` | Analyze the selected bounce and known sections, returning an evaluation report and findings; no FL mutation. |
| `record_creation_feedback` | Retain explicit producer feedback and any feedback locks; no FL mutation. |
| `plan_creation_revision` | Compile a closed revision plan with traceable findings, feedback, dependencies, expected movements, and manual actions; no FL mutation. |
| `apply_creation_revision` | Apply the recorded plan through the existing Production Run writers with one preflight and authorization boundary; mutating and verification-gated. |
| `compare_revision_bounces` | Compare distinct before/after assets and expose objective results, improvements, regressions, and insufficient evidence; no FL mutation. |
| `create_playlist_handoff` | Create a precise manual Playlist placement/delta handoff; Playlist clip placement remains outside the public API. |
| `create_delivery_manifest` | Assemble the final multi-dimensional delivery manifest; it does not render, save, or write project state. |

Review operations are bounded by the source request's pass, asset, finding,
feedback, and operation limits. A run can retain review outputs as typed
references (`review_session`, `evaluation_report`, `finding`, `feedback_lock`,
`section_definition`, `revision_plan`, `revision_pass`,
`revision_comparison`, `playlist_handoff`, and `delivery_manifest`).

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
