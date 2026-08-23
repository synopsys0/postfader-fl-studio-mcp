# Validated plug-in matrix

This matrix is evidence, not an allowlist. Postfader does not load this file at
runtime, branch on a plug-in's identity, or require a row before trying a
plug-in. An unlisted mixer effect remains eligible for the same runtime
discovery as a listed one. **Unvalidated means only that nobody has contributed
a publishable result yet; it does not mean unsupported.**

## Read the three axes separately

| Axis | Values | Meaning |
|---|---|---|
| Architectural scope | Compatible by design / Unavailable through backend | Native, VST, VST3, and AU plug-ins exposed as mixer effects or Channel Rack generators use the generic parameter path through distinct target types. Insertion, removal, and reordering remain unavailable through the public MIDI scripting backend. |
| Evidence level | Detected / Read-profiled / Write-validated | Detected means FL exposed the effect but the scan was partial. Read-profiled means every reported index was examined. Write-validated adds one representative normalized write and an independently confirmed exact restore. |
| Evidence source | Maintainer / Community | Who supplied the observation. Community is provenance, not a stronger capability claim. |

`Write-validated` never means every parameter, value, preset, option, or
audible result was tested. It means the row's representative write moved a
control with verified readback and restored the captured original normalized
value, which a separate read confirmed. The matrix records limitations beside
the result. A `value_readback` basis proves the requested normalized destination;
a `display_change_only` basis proves controllability and movement but not that
exact destination.

## Stock mixer effects

Stock coverage means effects installed and loadable in the exact FL Studio
edition/build named by the row. Editions and optional licences differ, so
there is no timeless universal stock list. Each effect gets its own evidence;
three examples are not extrapolated to the whole stock class.

| Plug-in | Origin / format | Evidence | Read result | Write check | Restore | Environment | Source | Limitations |
|---|---|---|---|---|---|---|---|---|
| Fruity Limiter | stock / native | read-profiled | complete; 18/18 real-looking; no padding | not run | not run | FL Studio 26.1.3 build 5336; Producer; Apple silicon | maintainer | Historical read profile predates report schema 1.0; exact Postfader revision was not recorded |
| Fruity Delay 3 | stock / native | read-profiled | complete; 26/26 real-looking; no padding | not run | not run | FL Studio 26.1.3 build 5336; Producer; Apple silicon | maintainer | Historical read profile predates report schema 1.0; exact Postfader revision was not recorded |
| Fruity Filter | stock / native | read-profiled | complete; 6/6 real-looking; no padding | not run | not run | FL Studio 26.1.3 build 5336; Producer; Apple silicon | maintainer | Historical read profile predates report schema 1.0; exact Postfader revision was not recorded |

The next stock pass should use a new blank project, load each available mixer
effect into a slot, and generate a report for each one. The current shareable
reporter validates mixer effects only. Generator parameters are reachable in
v0.20 through an explicit `channel_generator` target, but they have
no publishable matrix evidence yet and must not inherit mixer-effect evidence.

## Third-party mixer effects

No identified third-party product has a publishable schema-1.0 report yet.
That does not disable anything. Postfader will still ask FL for any loaded
third-party mixer's parameter map and report what it can and cannot verify.

Product names in future rows identify compatibility observations only. They
remain trademarks of their respective owners; inclusion does not imply
affiliation, endorsement, or a guarantee from the vendor or Postfader.

## Generate a community report

The installed command is read-only by default:

```bash
postfader-plugin-report \
  --track 3 --slot 1 \
  --plugin-version 2.1 \
  --plugin-origin third-party \
  --plugin-format VST3 \
  --fl-edition Producer
```

Every generated report records its source as `community candidate`. That is a
deliberate review state, not a claim about who ultimately supplied the
evidence. After privacy and technical review, a maintainer records the accepted
matrix row with `community` or `maintainer` provenance.

FL's API does not report native/VST/VST3/AU format or the user's edition, so
those two fields are contributor assertions. Use `unknown` when unsure.

For representative write evidence, use a new blank disposable project, launch
FL Studio with verified writes enabled, choose a real control index from the
read profile, and acknowledge the mutation explicitly:

```bash
postfader-plugin-report \
  --track 3 --slot 1 \
  --plugin-origin stock \
  --plugin-format native \
  --fl-edition Producer \
  --validate-write 2 \
  --confirm-disposable-project
```

The command refuses Master, playback, recording, disabled write mode, an index
that the scan classified as padding, or an FL state that is not safe to edit.
It exits nonzero unless the representative move verifies and the original
normalized value is independently read back after restoration. Even a
successful run creates undo history and can mark the disposable project dirty;
close it without saving.

The report contains setting-independent structural counts and bounded
environment labels. It contains no current values, display-derived control
kinds or units, display text, parameter names, option text,
mixer location, project metadata, path, or timestamp. It also never dumps the
source scan. Review the generated text before sharing it, then use the
[plug-in validation issue form](https://github.com/synopsys0/postfader-fl-studio-mcp/issues/new?template=plugin-validation.yml).

Do not attach raw JSON, logs, screenshots, presets, project files, or audio.
Issue bodies are user-editable and are not scanned by the repository's
public-tree gate, so maintainer review remains the final privacy boundary.
