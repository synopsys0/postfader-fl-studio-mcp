# 0.12.0 capability and evidence matrix

This is the 0.12.0 release-candidate evidence snapshot. The MCP surface
contains exactly **36 tools**: the 24-tool 0.11 baseline plus 12 Track B tools.
The packaged 0.12.0 candidate completed its retained hardware-in-loop run. A
public demo was not recorded; the release owner deferred it until a dedicated
public fixture is available, and it is not a 0.12.0 release gate. An evidence
label applies only to the named tool and behavior described here; it is not a
blanket statement about FL Studio or every project configuration.

## Evidence labels

- **`live-verified`** — the behavior has existing evidence from a real FL Studio
  session. For baseline tools, this records the shipped 0.11 evidence; it does
  not substitute for a 0.12 regression run when implementation details change.
- **`hardware-in-loop`** — the exact release-candidate implementation has been
  exercised end to end against the documented FL Studio version/build on the
  production CoreMIDI/IAC transport, with retained results.
- **`mock-tested`** — the implementation or contract is covered by the hermetic
  fake-FL/synthetic test suite, but the current release-candidate behavior has
  not yet cleared the required hardware-in-loop run.
- **`experimental`** — the interface is intentionally narrower than the normal
  verified-state contract or still needs live evidence before it can be
  presented as dependable production control.
- **`advisory`** — the tool returns measurements or discovery data for an agent
  to interpret. It neither changes FL Studio nor makes an artistic judgment.
- **`unavailable`** — there is no public tool for the capability. This can be a
  public-backend limitation or an intentional safety exclusion; the reason must
  be stated instead of implying that FL Studio itself cannot do it.

Statuses are deliberately not inferred upward. A video, a passing mock, or a
successful command dispatch is not by itself `hardware-in-loop` evidence. A
mutation is promoted only after the response proves its requested absolute
state from a later FL idle tick and records the undo observation and
`project_saved: false`.

Six existing plug-in tools have mixed evidence in 0.12. Their shipped 0.11
mixer-effect behavior retains its `live-verified` label. The new
`channel_generator` inventory and parameter-read paths are `live-verified`,
and its three parameter-setter paths are `hardware-in-loop`. Generator behavior
earned those labels from the packaged 0.12.0 run; it does not inherit
mixer-effect evidence merely because it uses the same public tool name.

## 36-tool surface

| # | Tool | Kind | Evidence status | Scope and release note |
|---:|---|---|---|---|
| 1 | `fl_get_capabilities` | Live read | `live-verified` | Reports direct, partial, unavailable, and unvalidated paths; it does not turn an unvalidated path into support. |
| 2 | `fl_get_project_summary` | Live read | `live-verified` | Reads project metadata, counts, dirty/undo tokens, version, and transport state. The observation is not atomic. |
| 3 | `fl_get_transport_state` | Live read | `live-verified` | Reads playing, recording, loop mode, song position, and length. |
| 4 | `fl_get_selected_range` | Live read | `live-verified` | Preserves raw selection endpoints and PPQ; meter and render semantics remain explicitly unvalidated. |
| 5 | `fl_list_mixer_tracks` | Live read | `live-verified` | Lists bounded mixer state and loaded effects; instantaneous peaks are observations, not audio analysis. |
| 6 | `fl_inspect_mixer_track` | Live read | `live-verified` | Reads one mixer track, its effects, built-in EQ, and outgoing routes. |
| 7 | `plugins_scan_loaded_plugins` | Live read | `live-verified` mixer effects and generator extension | Inventories effects already loaded on observed mixer tracks. The 0.12 packaged run live-verified the optional Channel Rack generator inventory. It does not insert a plug-in. |
| 8 | `plugins_inspect_parameter_map` | Live read | `live-verified` mixer effects and generator extension | Reads a bounded parameter page and keeps unknown controls unsafe. The 0.12 packaged run live-verified the explicit `channel_generator` target path. |
| 9 | `plugins_scan_parameters` | Live read | `live-verified` mixer effects and generator extension | De-pads a bounded or complete exposed parameter map; detection is not universal write support. The 0.12 packaged run live-verified the generator-target scan. |
| 10 | `copilot_capture_readonly_inspection` | Live read | `live-verified` | Captures a compact read-only project/mixer/effect report. |
| 11 | `fl_set_mixer_volume` | Verified mutation | `live-verified` | 0.11 baseline evidence exists. Absolute fader target, Master protected by default, later-tick readback, no automatic replay. |
| 12 | `fl_set_mixer_pan` | Verified mutation | `live-verified` | 0.11 baseline evidence exists. Absolute pan target, Master protected by default, later-tick readback. |
| 13 | `fl_set_mixer_mute` | Verified mutation | `live-verified` | 0.11 baseline evidence exists. Explicit boolean state, never a toggle; Master protected by default. |
| 14 | `fl_set_track_eq` | Verified mutation | `live-verified` | 0.11 baseline evidence exists. Gain/frequency proof is per requested field and aggregate verification is their logical AND. |
| 15 | `fl_set_mixer_name` | Verified mutation | `live-verified` | 0.11 baseline evidence exists. Sets an absolute name; an empty requested name restores FL's default behavior. |
| 16 | `fl_set_mixer_send` | Verified mutation | `live-verified` | 0.11 baseline evidence exists. Sets an explicit route state; it is not a toggle. |
| 17 | `fl_set_mixer_send_level` | Verified mutation | `live-verified` | 0.11 baseline evidence exists. Sets an absolute level only on an existing route. |
| 18 | `fl_set_plugin_param_display` | Verified mutation | `live-verified` mixer effects; `hardware-in-loop` generator extension | 0.11 mixer-effect evidence exists. Resolves a numeric display target cautiously and reports proof strength. The packaged 0.12 run verified and restored the generator-target path. |
| 19 | `fl_set_plugin_param_option` | Verified mutation | `live-verified` mixer effects; `hardware-in-loop` generator extension | 0.11 mixer-effect evidence exists. Resolves an enumerated display option, restoring and reporting failure if the search cannot land safely. The packaged 0.12 run verified and restored the generator-target path. |
| 20 | `fl_set_plugin_param` | Verified mutation | `live-verified` mixer effects; `hardware-in-loop` generator extension | 0.11 mixer-effect evidence exists. Sets one absolute normalized value and reports the later display/value evidence. The packaged 0.12 run verified and restored the generator-target path. |
| 21 | `audio_analyze_file` | Local measurement | `advisory` | Measures one caller-selected render with provenance, confidence, and limitations; it does not hear FL's live output. |
| 22 | `audio_compare_files` | Local measurement | `advisory` | Compares aligned, loudness-matched file overlap and can refuse weak comparisons. |
| 23 | `audio_analyze_masking` | Local measurement | `advisory` | Measures synchronized spectral overlap and level margins without prescribing processing. |
| 24 | `audio_find_recent_bounces` | Local discovery | `advisory` | Lists bounded recent audio files in fixed FL locations; it does not render or open them. |
| 25 | `fl_set_playing` | New verified mutation | `hardware-in-loop` | Sets an absolute playing boolean, never toggles. The packaged 0.12 run verified the requested state. |
| 26 | `fl_stop` | New verified mutation | `hardware-in-loop` | Requests both `playing=false` and position `0.0`; each field is proved and aggregate `verified` is their logical AND. The packaged 0.12 run verified both fields. |
| 27 | `fl_set_song_position` | New verified mutation | `hardware-in-loop` | Sets an absolute normalized song position with bounded tolerance. The packaged 0.12 run verified and restored it. |
| 28 | `fl_set_loop_mode` | New verified mutation | `hardware-in-loop` | Sets explicit `pattern` or `song` mode, never toggles. The packaged 0.12 run verified and restored it. |
| 29 | `fl_set_tempo` | New verified mutation | `hardware-in-loop` | Sets an absolute bounded BPM value. The packaged 0.12 run included fractional tempo and verified exact restoration. |
| 30 | `fl_list_channels` | New live read | `live-verified` | Lists global Channel Rack indices plus an observation-scoped identity fingerprint. The packaged 0.12 run used fresh live observations for guarded channel mutations and final fixture comparison. |
| 31 | `fl_set_channel_mix` | New verified mutation | `hardware-in-loop` | Sets any requested combination of absolute volume, pan, and mute state. Proof is per requested field and aggregate `verified` is their logical AND. The packaged 0.12 run verified all three fields and restoration. |
| 32 | `fl_set_channel_identity` | New verified mutation | `hardware-in-loop` | Sets an absolute name and/or color using an observation-scoped target guard. Per-field proof uses aggregate AND. The packaged 0.12 run verified both fields and restoration. |
| 33 | `fl_route_channel_to_mixer` | New verified mutation | `hardware-in-loop` | Sets one absolute mixer destination for a globally indexed channel. The packaged 0.12 run verified and restored the route. |
| 34 | `fl_get_step_sequence` | New live read | `live-verified` | Reads up to 512 current-pattern sixteenth-note cells and a canonical digest. The packaged 0.12 run used its live observation and digest for the guarded write case. |
| 35 | `fl_set_step_sequence` | New verified mutation | `hardware-in-loop` | Sets explicit cells only on grids of at most 256 cells, requires the observed digest, verifies every requested cell, and ANDs the results. Its atomic budget is `step_count + update_count + 8 <= 320` (at most 56 updates on a 256-cell grid); split larger edits and refresh the digest between calls. The packaged run verified mutation and restoration, and refused a stale digest without replay. |
| 36 | `fl_trigger_note` | Live audition dispatch | `experimental` | Dispatch-only note audition with a bounded duration and note-off receipt. The packaged 0.12 run observed a successful live note-on/note-off dispatch, but there is no authoritative state readback, so it remains `experimental` and must never be described as a verified mutation. |

The count is 36, below the release ceiling of approximately 40. The packaged
0.12.0 run promoted the new Track B state mutations and all three
generator-target parameter mutation paths to `hardware-in-loop`, and promoted
the new live-read paths to `live-verified`. `fl_trigger_note` remains
`experimental` even after a successful live dispatch because its contract
honestly provides no state readback.

## Unavailable backend capabilities and intentional exclusions

These are not hidden tools and do not count toward the 36-tool surface.

| Capability | Status | Reason |
|---|---|---|
| Playback-speed control | `unavailable` | FL Studio exposes a playback-speed setter but no authoritative playback-speed getter. It therefore cannot satisfy the later-idle-tick readback rule. |
| Insert, remove, or reorder plug-in slots | `unavailable` | **Unavailable through public MIDI scripting backend.** This is a backend-boundary statement, not a claim that the operation is impossible in FL Studio. |
| Render/bounce control or live audio buffers | `unavailable` | The public MIDI scripting backend exposes neither the audio buffers nor a supported render command needed for an honest tool. The advisory audio tools operate only on files already written to disk. |
| Exact loaded plug-in version metadata | `unavailable` | The public MIDI scripting backend does not expose an authoritative exact plug-in version. Reports must leave it unknown rather than guess. |
| Save, autosave, arbitrary API invocation, or MCP undo command | `unavailable` | Intentionally excluded from the public surface. Mutations report whether an FL undo point was observed and always report `project_saved: false`; the connector never saves for the caller. |

## Retained 0.12.0 release-candidate evidence

The candidate was built as a 0.12.0 wheel, installed into a clean environment,
and exercised through the production CoreMIDI/IAC transport with matching MIDI
write-test and packaged-bridge provenance. The retained sanitized summary
records all **12 of 12 cases passed**, the exact **36-tool** surface, **26
mutating public MCP calls**, and **zero client mutation retries**. Each case was
restored, and a fresh final observation exactly matched the initial fixture.

The run also proved that stale expected-before, bridge-session, and step-digest
guards refused before mutation; no rejected operation was replayed. The runner
required each state case's later-tick verification, confirmed that no tool
reported a project save, and then proved restoration from a fresh observation.
The live-note case retained note-on/note-off dispatch evidence without
promoting it to a verified-state claim.

The sanitized result remains outside this repository under the public-content
rule: no project/session record, private path, raw fingerprint, MIDI endpoint,
or user material is committed here. The deferred public demo supplied no part
of these hardware-in-loop labels and is not a 0.12.0 release gate.
