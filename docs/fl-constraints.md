# FL Studio constraints

These FL Studio 2026 MIDI-scripting behaviors determine what FL Studio MCP
Bridge can safely expose. They are runtime constraints, not musical policy.

## Plugin Atlas does not extend the FL Studio API

[Plugin Atlas](plugin-atlas.md) is bundled static product knowledge. It can
describe a product and link to reviewed sources, but it cannot establish that
the product is installed, owned, or loaded, and it is never a runtime
allowlist. The [validated plug-in matrix](plugin-matrix.md) is a separate set
of bounded observations and compatibility/write evidence; a matrix row is not
an Atlas record or a guarantee about every version, control, or format.

Neither surface adds capabilities that FL Studio does not expose here. In
particular, Atlas and the matrix cannot insert, remove, or reorder plug-ins,
save or render a project, or read FL Studio's live audio output.

Creation Review consequently works from explicit audio exports selected by the
caller. It can measure those files, map known Production Run sections, compare
matching before/after bounces, and request only the stems needed for an
unresolved finding. It cannot capture FL's live output, render the revised
project, save the project, separate stems, verify manual Playlist placement, or
establish artistic approval from measurements.

Sound Selection follows the same boundary: it chooses only from generators and
effects already loaded in the open project. Atlas-only products can be
recommended, but they cannot be loaded or assigned by PostFader. Load the
instrument pool manually before planning a palette. Loop Starter is separate
and must be requested explicitly; its reroll exposes dispatch but no stable
selected-loop identity.

## Creation readiness is observational

The creation-readiness scorecard aggregates what this bridge can detect before
the first write: connection/provenance, Piano Roll arming, loaded generators,
semantic drum coverage, empty patterns and arrangement limits, loaded-effect
coverage, and manual handoff requirements. It performs no mutation and cannot
make an unloaded instrument/effect, missing pad, or unavailable Playlist
operation appear ready. A complete Production Run performs that readiness
preflight once, caches the bounded session/target/project context, and then
uses phase-specific checks rather than rescanning the full project before
every phase.

Sound-aware composition uses metadata such as register, articulation, envelope,
density, and mono/poly behavior only when its provenance and confidence allow
it. Name inference is never audible evidence. A selected sound can therefore
produce a technical composition receipt while audible quality remains
`not_evaluated`.

Effect coverage has the same boundary. A semantic processing action requires a
loaded effect, Atlas capability evidence, an adapter, and a runtime control
observation. Processing plans are read-only; applies use existing verified
display/option/normalized setters and later-tick readback. Missing effects or
unresolved controls yield an honest dry/partial result, not a claim that FL
was processed or that the result sounds correct.

## The embedded Python environment cannot use files or sockets

Inside FL Studio's MIDI-script interpreter, low-level file construction can
return no object without setting a Python exception. Socket construction also
fails because the embedded sub-interpreter cannot safely initialize the host
process's `_socket` extension.

Consequences:

- the installed bridge cannot read or hash its own source, so
  `scripts/install.sh` stamps the repository source hash into the deployed
  copy;
- the TCP and file-mailbox transports remain useful to deterministic tests but
  are not the production connection; and
- production communication uses local MIDI SysEx over CoreMIDI/IAC on macOS
  or an explicitly configured virtual endpoint through WinMM on Windows.

## Process flags are fixed, but the session write gate can change

FL Studio's embedded Python cannot change its own process environment. The
bridge therefore reads mode flags once at module import.

PostFader does not need to change that environment to enable writes. Protocol
2 exposes one bounded `session.set_write_mode` control that changes the
bridge's in-memory allowlist after it verifies the current session fingerprint
and literal user-present confirmation. The MCP host then proves the result with
a second handshake. This state is not written to a project, configuration, or
environment variable.

`FL_BRIDGE_ENABLE_WRITES=1` remains a legacy startup compatibility path and is
still read only when the script loads. Changing that variable after FL Studio
starts has no effect. A script reload resets the in-memory gate to the startup
default; a normal FL Studio process with no startup opt-in therefore returns to
read-only mode.

## The bridge source must be ASCII-only

FL Studio loads the MIDI controller script through an ASCII code path. A smart
quote, typographic dash, or other non-ASCII byte can stop the script before it
runs. `tests/test_bridge.py` checks every byte in
`fl_studio_mcp/_bridge/device_UniversalBridge.py`.

This restriction applies to the FL-side bridge file, not to the MCP server or
documentation.

## All FL API work must stay on FL Studio's thread

FL Studio calls `OnIdle` about every 20 milliseconds and expects the callback
to finish promptly. Nothing may raise out of the callback, and no background
thread may call the FL Studio API.

A full mixer scan with peak values can require thousands of API calls. Long
mixer and parameter scans are therefore generators processed in bounded chunks
over successive idle ticks.

## VST parameter counts are padded

On the validated build, a VST3 can report 4,240 parameters even when its real
control surface is much smaller:

- indices below 4,096 contain real parameters mixed with padding;
- padding entries have empty or placeholder names and display strings; and
- indices 4,096 through 4,239 are 144 MIDI CC pass-through entries, not the
  plug-in's own controls.

The reported count is therefore only an address-space upper bound. Parameter
scans are paged, omit padding, and stop before treating the MIDI CC block as a
plug-in surface.

## Preset identity and drum-pad reads are bounded

FL's preset APIs expose a reported count, current-preset name, and
`nextPreset`/`prevPreset` navigation, but not a universal durable preset UUID.
PostFader therefore reads bounded index/name pages and reports duplicate or
blank names explicitly. A current index is used only when it resolves
uniquely. Exact selection accepts a name and/or index, refuses ambiguous names,
uses a bounded shortest navigation path when possible, and verifies the
requested identity after later idle ticks. Unknown or unverified mutation
outcomes are not replayed or rolled back; FL's `undo_point_created` evidence is
reported as observed, including `null` when unavailable.

`plugins.getPadInfo` is similarly optional and generic. When exposed,
PostFader reads each pad's semitone/MIDI note, color, empty/muted flags, and
reported name, then derives semantic drum roles. It cannot assume General MIDI
note numbers or invent a missing kick, snare, or hat. A Sound Selection drum
plan with required unmapped roles blocks before Piano Roll note writing.

## Same-tick readback can return the previous value

Reading a mixer or plug-in control in the same `OnIdle` callback that wrote it
can return the value from before the write. Every verified write yields back to
FL Studio for an idle tick before it reads the target.

Without that yield, a successful write could be reported as a failure. With
the yield, `verified: false` means only that the later read did not prove the
requested outcome. It does not trigger a further replay of the write, and it
does not roll anything back.

## Playback speed has a setter but no authoritative getter

The public transport module exposes `setPlaybackSpeed`, but no matching getter
that can prove the actual speed on a later FL idle tick. PostFader therefore
does not expose playback-speed control. This is a verification-boundary choice,
not a claim that FL Studio itself cannot change playback speed.

## Channel and step APIs require explicit index scope

Channel APIs can interpret an index relative to a selected group unless the
global-index flag is supplied. PostFader lists and mutates Channel Rack targets
only with global indexing, echoes that scope in every contract, and uses an
observation-scoped channel fingerprint to catch a changed or reordered target.
FL exposes no durable channel UUID, so that fingerprint must not be treated as
project identity.

FL's channel-color getter can return a signed Python integer even though the
public contract uses the equivalent unsigned 32-bit `0x--BBGGRR` word. The
high byte is FL-owned: a low-24-bit setter request can read back with a
different high byte. PostFader preserves the exact observed 32-bit word in
reads and fingerprints, but expected-before guards and later-tick write proof
compare the controllable low 24 color bits.

`channels.getGridBit` and `channels.setGridBit` address only the current
pattern. They do not expose an arbitrary-pattern read surface. Step reads and
writes therefore require an explicit `pattern_number`, compare it with the
current pattern before and after observation, and refuse rather than switching
patterns. Writes also require the canonical grid digest returned by the read
and recheck it immediately before any cell changes.

Generator parameters use a different address form from mixer effects: global
channel index plus `slotIndex=-1`. Public contracts keep that as the
`channel_generator` variant of a discriminated target; mixer effects retain
the strict 0–9 slot range.

## Numeric and displayed plug-in values can disagree temporarily

`plugins.getParamValue()` can lag while `plugins.getParamValueString()` already
shows the updated setting. The verified setters use both observations:

- a matching numeric readback confirms the destination;
- a changed display string proves movement but may not prove the exact
  destination; and
- no supporting observation returns `verified: false`.

Use `fl_set_plugin_param_display` when the target must land in the units the
plug-in shows. A name such as `Attack` and a target such as `20` can be
resolved without the caller knowing the plug-in's normalized curve.

## Parameter writes require pickup mode to be disabled

`plugins.setParamValue` accepts a pickup mode. FL Studio's default pickup
behavior can leave a scripted control waiting for a hardware pickup position
and silently refuse later writes. Every public parameter setter passes
`midi.PIM_None` and issues the required paired write before yielding for
readback.

The fake FL API reproduces the pickup trap so an omitted pickup mode fails the
safe test suite.

## Enumerated options can only be discovered by moving the control

FL Studio exposes no function that lists the valid text options for a plug-in
parameter. `fl_set_plugin_param_option` learns them by sweeping normalized
values and recording the displayed text, then lands on the requested exact
label. Matching ignores case but refuses substrings.

This is a mutating search. It can move the control through intermediate values
and should not be run during recording or on an irreplaceable project. If the
requested option is not found, the bridge attempts to restore the starting
value and reports the result.

## The public API does not insert or render

The supported MIDI scripting modules provide no operation for:

- reading raw audio buffers;
- rendering or exporting audio;
- inserting, removing, or reordering plug-ins;
- enumerating detailed Playlist clips or reading/editing automation points; or
- performing a verified Save As or version-copy operation.

FL Studio contains undocumented internal operations, but they are not a stable
third-party integration surface and this project does not depend on them.

Plug-ins can be inserted manually through FL Studio's UI and then inspected
immediately through `plugins.isValid`, `plugins.getPluginName`, and the
parameter tools. The division is explicit: insertion stays outside this MCP
server; verification and parameter configuration remain inside it.

Audio must likewise be exported or recorded through FL Studio before the
audio-analysis tools can measure it.

## Piano Roll scripts are a separate runtime

FL's controller scripting API can select a global channel and pattern and show
the Piano Roll, but it cannot enumerate or edit the score. FL exposes those
notes to a separate `.pyscript` runtime instead. PostFader therefore installs a
small user-run bootstrap and atomically replaces one generated **Postfader
Apply** script for each requested write or transform.

The controller bridge verifies the intended channel, pattern, and Piano Roll
visibility before the host sends the platform shortcut. A successful shortcut
dispatch proves focus/key delivery only. Because the controller side has no
note getter, `application_verified` is always false and no second Piano Roll
mutation should be inferred safe merely from dispatch.

## Markers and automation have asymmetric getters

Image-Line exposes marker insertion and marker-name enumeration, but no public
getter for each marker's timeline position. Section-marker receipts can prove
names on a later tick while leaving `times_verified=false`.

The public REC-event path can record one controller value while playback and
recording are active. PostFader verifies those capture conditions and reads the
controlled mixer/channel value back, but FL exposes no public getter for the
new automation point. `automation_event_recorded` therefore remains null and
the aggregate receipt remains unverified. These helpers do not provide
automation-clip CRUD.

## A send level requires an existing route

`mixer.getRouteToLevel(source, destination)` raises an out-of-range error when
the route does not exist instead of returning zero. A send level cannot
therefore be verified before its route exists.

`fl_set_mixer_send_level` refuses that case and directs the caller to
`fl_set_mixer_send`. Creating a route can restore its previous level rather
than choosing a new fixed default, so inspect it before changing the amount.

## Per-slot effect bypass and wet/dry setters are unreliable

The validated FL Studio build exposes per-slot mute and mix-level setters, but
they are not safely reversible through a script:

- the slot mute setter can bypass a slot without reliably enabling it again;
  and
- the slot mix-level setter can accept values while its getter remains
  unchanged.

PostFader therefore exposes neither operation. Use a plug-in's own
documented bypass or mix parameter when it provides one, or make the change
manually in FL Studio.

`getPluginMixLevel` and `getPluginMuteState` can also return values for an empty
slot. Use `plugins.isValid` to determine whether a plug-in is loaded.

## Playlist selection semantics remain raw

Image-Line documents integer Playlist selection endpoints but not their full
coordinate system, no-selection sentinel, or render inclusivity. The bridge
reads both endpoints twice and returns them with project PPQ, but deliberately
leaves interpreted state, units, and normalized ticks unknown.

Every ordinary `fl_get_selected_range` result is marked unsafe for automated
rendering. A caller may display the raw observation but must not turn it into
render boundaries.

## What the connector exposes from the mixer

The verified write surface includes these narrow mixer operations:

| FL Studio behavior | MCP operation |
| --- | --- |
| Set one track name | `fl_set_mixer_name` |
| Create or remove one route | `fl_set_mixer_send` |
| Set one existing route's amount | `fl_set_mixer_send_level` |
| Set one fader by normalized value or dB | `fl_set_mixer_volume`, `fl_set_mixer_volume_db` |
| Set pan, mute, solo, recording arm, color, or stereo separation | Corresponding `fl_set_mixer_*` tool |
| Select the active mixer track | `fl_select_mixer_track` |
| Set one built-in EQ band | `fl_set_track_eq` |

Channel linking, polarity, channel swapping, and the unreliable per-effect-slot
controls remain outside the public MCP write surface.
