# Plug-in support

## What can be reached at all

PostFader has two explicit plug-in target kinds:

- `mixer_effect` names a mixer track plus effect slot 0–9. Mixer track 0
  requires `allow_master: true` for a write.
- `channel_generator` names a global Channel Rack index. The bridge translates
  that target to FL's separate generator addressing form (`slotIndex=-1` with
  global indexing); callers never overload a mixer slot with `-1`.

Use `fl_list_channels` to obtain the global channel index and its
observation-scoped identity fingerprint. Set
`include_channel_generators=true` on `plugins_scan_loaded_plugins` to include
both target kinds in one inventory. Parameter pages, full scans, and all three
parameter setters accept the same discriminated `target` object. The legacy
`track_index`/`slot_index` arguments remain available for mixer effects, but a
call must use either the explicit target or the complete legacy pair, never
both.

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

## How support works for what can be reached

Generic plug-in discovery is identity-independent: there is no allowlist that a
plug-in must enter before the connector can inspect it. PostFader v0.20 also
ships a small set of optional processing-intent adapter profiles. The profiles
describe parameter roles for selected reported names so the `mix_*` planning
tools can resolve intents such as dynamics, EQ, reverb, or delay. They do not
gate generic discovery, enable a plug-in, assert an exact plug-in version, or
certify every parameter or audible result. Use `mix_list_plugin_profiles` and
`mix_inspect_plugin_compatibility` to see the current profile catalog and the
observed matches; an unprofiled plug-in remains eligible for the same runtime
scans and parameter setters.

Attempt is the honest verb. What you get back depends on what FL chooses to
report for that plug-in and on the scan bounds below, and a write is reported
as `verified: false` when FL accepts it and ignores it. The guarantee is not
that every plug-in works — it is that you are told what was found and what did
not land.

That is the generic compatibility story for reading and writing parameters;
optional profiles affect intent planning only. What follows is about the
places where discovery is bounded, because those bounds are where an untested
plug-in can surprise you.

## Addressing a parameter three ways

FL gives a plug-in parameter a normalised `0..1` value, an optional name, and a
display string. Real plug-ins use those inconsistently: many third-party
controls have no name at all and are identifiable only by what they display.
So there are three ways to name the control you mean.

| Tool | You supply | Use it when |
|---|---|---|
| `fl_set_plugin_param` | target, parameter index, normalised `0..1` | You know the curve, or the control is a plain fader |
| `fl_set_plugin_param_display` | target, index/name, and the number the plug-in shows | You want "20 ms" and do not know the curve |
| `fl_set_plugin_param_option` | target, index/name, and exact option text | The control is enumerated: a key, a scale, a mode |

Prefer the second and third. `fl_set_plugin_param_display` searches the control
until FL's own readback agrees, so it never assumes a curve.
`fl_set_plugin_param_option` also returns every option it discovered, which is
the fastest way to learn what an unfamiliar control can do. Copy the exact
label from that list; matching ignores case but refuses substrings.

A text selector is resolved one priority tier at a time: exact name, exact
display, name substring, then display substring. If the first matching tier
contains more than one unique parameter index, the write is refused before an
undo point or mutation. The bounded diagnostic lists candidates and directs
the caller to pass an integer index.

## Three bounds that can hide controls

These are cost ceilings. FL runs script code on the thread driving its UI and
audio, so an unbounded walk stalls the program. Each bound is an engineering
compromise measured on a narrow sample, and each can under-report on a plug-in
outside that sample.

### Enumerated options: `OPTION_SWEEP_STEPS = 64`

FL has no API to list a control's options, so they are found by moving the
control and reading what it displays. 64 steps was sized against a 12-option
musical key selector.

**Where it breaks:** a control with more distinct options than there are steps
returns a partial list — and a partial list looks exactly like a complete one.
An impulse-response picker on a convolution reverb, or a preset or wavetable
selector on a generator, is where this bites.

**How many steps a control actually needs.** Measured against a live VST3: a
29-option Scale control resolved *completely* at the default 64 steps, and
re-sweeping the same control at 256 found the identical 29 options and nothing
more. Options partition the normalised range into roughly equal contiguous
bands, so the sweep does not need fine sampling -- it needs to land in each
band at least once. About two samples per option is the working rule.

That gives a usable guide:

| Options on the control | Steps needed | At the default 64 |
|---|---|---|
| up to 32 | up to 64 | fine |
| 33 - 128 | 66 - 256 | raise `sweep_steps` |
| over 128 | over 256 | cannot be fully enumerated |

**What to do:** raise `sweep_steps` toward its maximum of 256. The MCP argument
is spelled `sweep_steps`, and arguments are validated strictly, so a
misspelling is rejected rather than quietly ignored. Past 256 options, sweeping
cannot see the whole list at all; address the control with
`fl_set_plugin_param` on the normalised range instead.

**Sweeping is not free.** It moves the control to look. Asking for the option a
control is *already* showing keeps the displayed setting, but the control lands
on the nearest sweep step rather than its exact previous value, and each sweep
creates undo points and marks the project dirty. Two sweeps on one control took
a clean project to `dirty_flag: 1` with four undo entries. Do this in a
disposable project, and undo or close without saving afterwards.

### Parameter search: `PARAM_SEARCH_RUN = 256`

FL reports a padded maximum instead of a real parameter count — often thousands
of slots — with the real controls sparse inside it. A name search walks the
range and stops after 256 consecutive empty slots, on the observation that real
controls cluster in the low indices with scattered gaps.

**Where it breaks:** a plug-in that leaves a gap wider than 256 between real
controls loses everything past the gap. The search reports the parameter as not
found, which is indistinguishable from it not existing.

**What to do:** use `plugins_scan_parameters`, which walks the whole range up to
`MAX_PARAM_INDEX_SCAN` (8192) and reports `truncated` honestly, then address the
control by the index it returns.

### Padding detection

A slot counts as padding when it has no name *and* its display is blank or a
bare zero. This is structural rather than plug-in-specific and has held up
across everything measured so far.

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
The MCP `fl_set_plugin_param_option` tool still supports an intentional live
option write, under the mutation warnings above, but that output is not a
community compatibility artifact.

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
- reading audio, rendering, or saving the project.

See [FL Studio constraints](fl-constraints.md) for the full list.
