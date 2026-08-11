# Plug-in support

There is no list of supported plug-ins, and no per-plug-in profiles. The
connector never models a plug-in; it discovers one at runtime. So there is
nothing to add for a plug-in it has not seen: it will attempt any VST, VST3, or
native FL effect that FL Studio exposes to its MIDI scripting API.

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
A wavetable, preset, or impulse-response selector with hundreds of entries is
the common case.

**What to do:** raise `sweep_steps` toward its maximum of 256 on that call. The
MCP argument is spelled `sweep_steps`, and arguments are validated strictly, so
a misspelling is rejected rather than quietly ignored.

If a control has more than 256 options, address it with `fl_set_plugin_param`
on the normalised range instead. The sweep is a discovery aid, not the only way
in.

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

| Plug-in | Format | Parameter slots reported | Notes |
|---|---|---|---|
| FL native effects | internal | real count | Names and displays are well-behaved |
| A vocal-tuning plug-in | VST3 | ~4200 | First real control is nameless; identifies itself only by display. Enumerated key and scale controls resolve at the default 64 steps |
| A harmony plug-in | VST3 | ~4200 | Real controls sparse and interleaved with padding rather than contiguous at the start |

This table is deliberately short. It reflects one contributor's plug-in folder,
not a survey. **Pull requests adding rows are welcome and are the main way this
grows** — see [CONTRIBUTING.md](../CONTRIBUTING.md).

To add a row, run against your own plug-in:

```bash
./.venv/bin/python scripts/inspect_readonly.py --capabilities
```

then use `plugins_scan_parameters` on the slot and report the reported count,
the real count, and anything that surprised you. Do not include project audio,
a project file, or a path from your machine.

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
