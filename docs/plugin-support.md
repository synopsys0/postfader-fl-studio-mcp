# Plug-in support

## What can be reached at all

**Mixer effect slots only.** A plug-in is addressed by mixer track and slot
0-9, so this covers effects on a mixer track and nothing else. Instruments in
the Channel Rack -- Serum, Sytrus, Harmor, a Kontakt library -- are not
addressable, however well discovery works, because FL's scripting API exposes
no parameter access for them here. `fl_get_project_summary` can count channels;
it cannot reach inside one.

That limit is worth checking first: no amount of scan tuning helps a plug-in
that is not on a mixer track.

## How support works for what can be reached

There is no list of supported plug-ins, and no per-plug-in profiles. The
connector never models a plug-in; it discovers one at runtime. So there is
nothing to add for a plug-in it has not seen: it will attempt any VST, VST3, or
native FL effect loaded in a mixer slot.

Attempt is the honest verb. What you get back depends on what FL chooses to
report for that plug-in and on the scan bounds below, and a write is reported
as `verified: false` when FL accepts it and ignores it. The guarantee is not
that every plug-in works — it is that you are told what was found and what did
not land.

That is the whole compatibility story for reading and writing parameters. What
follows is about the places where discovery is bounded, because those bounds
are where an untested plug-in can surprise you.

## Addressing a parameter three ways

FL gives a plug-in parameter a normalised `0..1` value, an optional name, and a
display string. Real plug-ins use those inconsistently: many third-party
controls have no name at all and are identifiable only by what they display.
So there are three ways to name the control you mean.

| Tool | You supply | Use it when |
|---|---|---|
| `fl_set_plugin_param` | normalised `0..1` | You know the curve, or the control is a plain fader |
| `fl_set_plugin_param_display` | the number the plug-in shows | You want "20 ms" and do not know the curve |
| `fl_set_plugin_param_option` | the option text | The control is enumerated: a key, a scale, a mode |

Prefer the second and third. `fl_set_plugin_param_display` searches the control
until FL's own readback agrees, so it never assumes a curve.
`fl_set_plugin_param_option` also returns every option it discovered, which is
the fastest way to learn what an unfamiliar control can do.

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
An impulse-response picker on a convolution reverb, or a preset selector on a
large effect, is where this bites. (A synth wavetable list would be the obvious
example, but Channel Rack instruments are out of reach entirely -- see above.)

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

## Validated against

Rows record what a contributor actually observed. "Parameter slots" is what FL
*reported*, not what the plug-in has.

**FL's stock effects are the easy case and share one row.** They report their
real parameter count, name their parameters, and pad nothing: Fruity Limiter
reports 18 for 18, Fruity Delay 3 reports 26, Fruity Filter reports 6. Nothing
about them needs tuning, so listing them individually would pad this table
without informing anyone. A stock effect gets its own row only if it
misbehaves.

Third-party plug-ins are where a row earns its place.

| Plug-in | Format | Slots reported | What is worth knowing |
|---|---|---|---|
| FL stock effects (as a class) | internal | real count | Named parameters, no padding, no tuning needed |
| A vocal-tuning plug-in | VST3 | ~4240 | ~40 real controls; widest gap 15; 38% nameless, identified only by display. A 29-option Scale control resolved fully at the default 64 sweep steps |
| A harmony plug-in | VST3 | ~4240 | Real controls sparse and interleaved with padding rather than contiguous at the start |

This table is deliberately short. It reflects one contributor's plug-in folder,
not a survey. **Pull requests adding rows are welcome and are the main way this
grows** — see [CONTRIBUTING.md](../CONTRIBUTING.md).

To add a row, run the report against your own plug-in:

```bash
./scripts/plugin_report.py --track 3 --slot 1
```

It prints a pasteable table row and the reasoning behind it. The scan it uses
is on the bridge's read-only allowlist, so it is safe against a project you are
working in and needs no write mode.

**It reports the plug-in, never your settings.** A scan returns current values,
and those describe your session rather than the plug-in: a control reading
`Key = A` states what key your song is in, and a retune speed is a mixing
decision. The report keeps the shape -- how many controls exist, where they
sit, whether they are named, what kind they are, what units they use -- and
discards every value. Read what it prints before sharing it, but there should
be nothing left to redact.

Option lists are the one thing the read-only run cannot fill in. Add `--sweep`
to discover them, but only in a disposable project with write mode enabled --
it moves every enumerated control it surveys:

```bash
./scripts/plugin_report.py --track 3 --slot 1 --sweep
```

Option *text* is the plug-in's own vocabulary and is the same for everyone who
owns it, so the report prints it. The exception is a control that enumerates
things you made -- a preset or sample selector -- where a single personal-looking
entry causes the whole list to be withheld rather than published.

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

## What cannot be done, for any plug-in

FL's scripting API has no function for these, so no plug-in supports them:

- adding, removing, or reordering plug-ins;
- bypassing a slot or changing its wet/dry mix — FL ignores both when a script
  drives them;
- reading audio, rendering, or saving the project.

See [FL Studio constraints](fl-constraints.md) for the full list.
