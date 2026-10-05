# Plug-in support

## What can be reached at all

PostFader has two explicit plug-in target kinds:

- `mixer_effect` names a mixer track plus effect slot 0–9. Mixer track 0 is
  Master, and its target also needs `allow_master: true`.
- `channel_generator` names a global Channel Rack index. The bridge translates
  that target to FL's separate generator addressing form (`slotIndex=-1` with
  global indexing); callers never overload a mixer slot with `-1`.

Use `channel_list` to obtain the global channel index and its
observation-scoped identity fingerprint. `plugin_list_loaded` returns both
target kinds in one inventory, each entry with its `target` object.
`plugin_list_parameters`, `plugin_set_parameter`, `plugin_list_presets`,
`plugin_select_preset`, and `plugin_get_pad_map` all take that same
discriminated `target`; there is no separate `track_index`/`slot_index` form.

FL does not expose a durable channel UUID or authoritative loaded plug-in
version. A channel fingerprint is therefore a same-session stale-target guard,
not an identity that can be carried across projects or bridge reloads, and
exact plug-in version remains unknown.

## Atlas knowledge is separate from compatibility evidence

The [Plugin Atlas](plugin-atlas.md) is a static, offline catalog of product
purpose and related documentation. Its current Image-Line pricing snapshot
contains 119 rows, plus separately scoped auxiliary, manual/index, and legacy
records; selected third-party entries are not a completeness claim. Atlas does
not prove that a product is installed, owned, or loaded, and it is not a
runtime allowlist.

The validated [plug-in matrix](plugin-matrix.md) records bounded observations
from actual loaded instances. Its detected, read-profiled, and write-validated
rows are compatibility/write evidence, not product knowledge and not a gate on
generic discovery. Neither surface can insert, remove, or reorder a plug-in;
neither can save or render a project or read FL Studio's live audio output.

Sound Selection builds on this same distinction. It chooses only among the
loaded targets observed in the current project, while Atlas-only products are
recommendations rather than executable assignments. A loaded but unprofiled
plug-in remains eligible for palette planning with lower semantic confidence.

## Effect coverage and semantic processing

Effect coverage is an observation, not a support badge. For each requested
role and technique, PostFader records the loaded mixer-effect targets,
product/Atlas matches, adapter matches, supported semantic techniques, and
unresolved controls. A loaded target is processing-ready only when the Atlas
capability, compatible adapter, and runtime parameter evidence agree. Missing
or unresolved capabilities are returned before a creation run writes anything;
an Atlas-only product is never treated as a loaded effect.

`processing_plan` is read-only and resolves conservative goals such as
`reduce_mud`, `control_dynamics`, `add_depth`, or `rhythmic_echo` to exact
controls. Each action's `setter` names the `plugin_set_parameter` value
argument that writes it; the plan prefers `display_value` or an exact `option`
when the adapter establishes that representation. `processing_apply` and the
equivalent Production Run operation use the existing session/target guards and
later-idle-tick readback. Results distinguish restrained first-pass,
partial-processing, dry-by-design, and dry-missing-effects states. None of
these technical states is an audible-quality verdict; that dimension remains
unevaluated until a user review or bounce analysis.

## Presets and drum maps

The preset tools are target-aware and work for both mixer effects and global
Channel Rack generators. `plugin_list_presets` returns bounded pages of FL's
reported index/name rows, count, current identity, blank names, duplicate
names, and partial/truncated status; it reports a current index only when the
name can be resolved uniquely. `plugin_select_preset` accepts an exact name or
index; duplicate names require an index.

Preset navigation uses FL's `nextPreset`/`prevPreset` path within explicit
navigation and settling limits and succeeds only after later-idle-tick current
preset readback matches the requested identity. It reports the path and the
undo evidence FL exposed. Dispatch is not proof, and an ambiguous outcome is
never retried or rolled back.

`plugin_get_pad_map` reads FL's generic pad API, including semitone/MIDI
note, color, empty, muted, and reported name fields. Sound Selection uses that
observation to build semantic drum roles without assuming General MIDI. The
fixed General MIDI map in `compose_drums` remains an explicit fallback only
when no reported map is supplied.

Sound Selection can walk bounded preset pages beyond the first page when a
requested identity or a useful candidate is not yet observed. The result
records page coverage, truncation, duplicate names, and unresolved exact
identities. Bundled preset/family metadata is versioned separately from Atlas
and may be supplemented by an isolated user-local layer. Provenance and
confidence distinguish reviewed exact metadata, family evidence, normalized
name inference, explicit user preference, and unknowns; absence never proves
that a preset is unsuitable.

## How support works for what can be reached

Generic plug-in discovery is identity-independent: there is no allowlist that a
plug-in must enter before the connector can inspect it. PostFader also
ships a small set of optional Plugin Atlas control adapters. The adapters
describe parameter roles for selected reported names so `processing_plan` can
resolve goals such as dynamics, EQ, reverb, or delay. They do not
gate generic discovery, enable a plug-in, assert an exact plug-in version, or
certify every parameter or audible result. Use `atlas_get_product` to read a
product's adapters and `atlas_match_loaded` to see which loaded plug-ins match
one; a plug-in without an adapter remains eligible for the same
`plugin_list_parameters` scan and `plugin_set_parameter` writes.

Attempt is the honest verb. What you get back depends on what FL chooses to
report for that plug-in and on the scan bounds below, and a write is reported
as `verified: false` when FL accepts it and ignores it. The guarantee is not
that every plug-in works — it is that you are told what was found and what did
not land.

That is the generic compatibility story for reading and writing parameters;
optional adapters affect processing planning, not those reads and writes. What
follows is about the places where discovery is bounded, because those bounds
are where an untested plug-in can surprise you.

## Addressing a parameter three ways

FL gives a plug-in parameter a normalised `0..1` value, an optional name, and a
display string. Real plug-ins use those inconsistently: many third-party
controls have no name at all and are identifiable only by what they display.
So `plugin_set_parameter` takes the control as `parameter` (an index, or text
matched against names and display strings) and exactly one of three value
forms.

| Value argument | You supply | Use it when |
|---|---|---|
| `normalized_value` | target, parameter index, normalised `0..1` | You know the curve, or the control is a plain fader |
| `display_value` (optional `unit`, `tolerance`) | target, index/name, and the number the plug-in shows | You want "20 ms" and do not know the curve |
| `option` (optional `sweep_steps`) | target, index/name, and exact option text | The control is enumerated: a key, a scale, a mode |

Prefer the second and third. A `display_value` write searches the control
until FL's own readback agrees, so it never assumes a curve. An `option` write
also returns every option it discovered, which is the fastest way to learn
what an unfamiliar control can do. Copy the exact label from that list;
matching ignores case but refuses substrings.

A text selector is resolved one priority tier at a time: exact name, exact
display, name substring, then display substring. If the first matching tier
contains more than one unique parameter index, the write is refused before an
undo point or mutation. The bounded diagnostic lists candidates and directs
the caller to pass an integer index.

## Three bounds that can hide controls

These are cost ceilings. FL runs script code on the thread driving its UI and
audio, so an unbounded walk stalls the program. Each bound limits the time spent inside a scan and can under-report on a
plug-in whose controls fall outside the sampled range.

### Enumerated options: `OPTION_SWEEP_STEPS = 64`

FL has no API to list a control's options, so they are found by moving the
control and reading what it displays. The default is 64 steps; callers may
request up to 256 steps.

**Where it breaks:** a control with more distinct options than there are steps
returns a partial list — and a partial list looks exactly like a complete one.
An impulse-response picker on a convolution reverb, or a preset or wavetable
selector on a generator, is where this bites.

**What to do:** raise `sweep_steps` toward its maximum of 256 when a list
appears incomplete. More samples improve coverage, but a control may map its
options unevenly across the normalized range. Neither the default nor a higher
step count proves that every option was discovered. Write a known
`normalized_value`, with the parameter's index, when an option cannot be
located by its label.

**Sweeping is not free.** It moves the control to look. Asking for the option a
control is *already* showing keeps the displayed setting, but the control lands
on the nearest sweep step rather than its exact previous value, and each sweep
can create undo points and mark the project dirty. Use a disposable project
when exploring an unfamiliar enumerated control.

### Parameter search: `PARAM_SEARCH_RUN = 256`

FL reports a padded maximum instead of a real parameter count — often thousands
of slots — with the real controls sparse inside it. A name search walks the
range and stops after 256 consecutive empty slots, on the observation that real
controls cluster in the low indices with scattered gaps.

**Where it breaks:** a plug-in that leaves a gap wider than 256 between real
controls loses everything past the gap. The search reports the parameter as not
found, which is indistinguishable from it not existing.

**What to do:** use `plugin_list_parameters`, which walks the whole range up to
`MAX_PARAM_INDEX_SCAN` (8192) and reports `truncated` honestly, then address the
control by the index it returns.

### Padding detection

A slot counts as padding when it has no name *and* its display is blank or a
bare zero. This rule depends on the reported control structure rather than a
plug-in name.

**Where it breaks:** a real, nameless control sitting at exactly zero with a
bare-zero display is classified as padding. In practice nameless controls
display something meaningful, which is what the rule keys on.

## Validated observations

The [validated plug-in matrix](plugin-matrix.md) lists each observed product
separately. Its evidence level and source are different axes: a community
report may be read-profiled or write-validated, and an unlisted effect remains
compatible by design rather than being blocked.

To generate a read-only community report against a loaded effect:

```bash
postfader-plugin-report \
  --track 3 --slot 1 \
  --plugin-version 2.1 \
  --plugin-origin third-party \
  --plugin-format VST3 \
  --fl-edition Producer
```

From a source checkout, `./scripts/plugin_report.py` invokes the same installed
implementation. The scan command is on the bridge's read-only allowlist and
needs no write mode.

**It reports structural evidence, never your settings.** A scan necessarily
reads current values and display strings to distinguish real controls from
padding. The shareable reducer discards them, parameter names, option text,
mixer locations, project metadata, paths, and timestamps. Aggregate display-
derived kinds and units are also omitted because they can reveal a coarse
mode. Read the result before sharing it and do not attach the source scan.

The reporter does not sweep enumerated controls and never publishes their
option strings: preset and sample selectors may expose user-created names.
The MCP `plugin_set_parameter` tool's `option` form still supports an
intentional live option write, under the mutation warnings above, but that
output is not a community compatibility artifact.

Representative write validation is a separate, explicit mode. It requires a
blank disposable project and only earns `write-validated` when both the test
move and exact restore are confirmed. See the matrix for the command and
refusal conditions.

## When a plug-in misbehaves

1. **A parameter cannot be found by name.** Many controls have no name. Search
   by display string, or scan the slot and use the index.
2. **An option list looks short.** Raise `sweep_steps`. See above.
3. **A write reports `verified: false`.** FL accepted the write and then
   ignored it. The setter was already repeated inside that write; what does not
   happen is a further replay afterwards, or a rollback. Read the track back
   before deciding what to do next — this is a real FL behaviour, not a
   transport error.
4. **A scan says `truncated`.** It hit a work ceiling. The result is a valid
   partial answer, not a complete one.

## What cannot be done, for any plug-in target

FL's scripting API has no function for these, so no plug-in supports them:

- adding, removing, or reordering plug-ins through the public MIDI scripting
  backend;
- bypassing a slot or changing its wet/dry mix — FL ignores both when a script
  drives them;
- hearing, auditioning, or measuring the live output of a selected preset;
- reading audio, rendering, or saving the project.

See [FL Studio constraints](fl-constraints.md) for the full list.
