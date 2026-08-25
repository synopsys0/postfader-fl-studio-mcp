# Validated plug-in matrix

This matrix is evidence, not an allowlist. PostFader does not load this file at
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

## Contributor campaign

The matrix is a place to preserve bounded observations, not a product-support
catalog. A contributor can help without changing PostFader code:

1. Load one effect into a mixer slot in a stable FL Studio session and confirm
   that it is a mixer effect. The current shareable reporter does not produce
   publishable matrix evidence for Channel Rack generators.
2. Run the reporter in its default read-only mode and review the reduced report
   before posting it. Use `unknown` for the FL format or edition when you do not
   know those facts; they are contributor assertions because FL's scripting API
   does not provide them.
3. If you choose representative write validation, create a new blank,
   disposable project, enable session writes explicitly, and select a real
   parameter index from the read profile. The test may create undo history and
   mark the project dirty even when it passes. Close the project without
   saving.
4. Submit only the generated privacy-safe report through the [plug-in
   validation issue form](https://github.com/synopsys0/postfader-fl-studio-mcp/issues/new?template=plugin-validation.yml).
   A report remains a `community candidate` until a maintainer reviews it.
5. Maintainers check the report schema, privacy reducer, environment labels,
   evidence level, and reproducibility before adding or changing a matrix row.

### Evidence labels in contributor terms

| Label | What it means | What it does not prove |
|---|---|---|
| `detected` | FL exposed the effect during the observed inventory, but the scan was partial or did not establish a complete parameter profile. | That every control is exposed, readable, writable, or audible. |
| `read-profiled` | The bounded report examined every parameter index it reported as real for that observation. | That a write works, that options are complete, that presets are supported, or that a later FL build behaves identically. |
| `write-validated` | One representative normalized control write was followed by the required verification and an independent read confirming the captured original value after restore. | Every parameter, display-unit setter, option, preset, bypass/wet-dry control, plug-in version, load operation, or musical result. |

Representative validation is intentionally narrow. It does not certify a
plug-in as fully supported, prove compatibility with every version or format,
prove that FL's audio output changed, or prove that the project remained clean.
The matrix records those limitations beside each accepted row.

### Priority validation backlog

The following is a prioritized list of commonly searched products and product
families for future evidence collection. It is a validation target list, not a
support claim, compatibility promise, or pre-approved matrix row. No row should
be added until a privacy-reviewed report exists.

1. FabFilter Pro-Q
2. FabFilter Pro-C
3. Valhalla reverbs
4. Serum
5. Vital
6. Soundtoys effects
7. Waves vocal processors
8. iZotope tools
9. Antares products
10. Common FL Studio stock effects not yet represented in this matrix

The order is a starting point for contributor interest, not a ranking of
technical quality or expected support. Contributors should name the exact
product, version, format, FL Studio edition/build, and evidence level in the
generated report; maintainers should not infer a family-wide result from one
product.

## Stock mixer effects

Stock coverage means effects installed and loadable in the exact FL Studio
edition/build named by the row. Editions and optional licences differ, so
there is no timeless universal stock list. Each effect gets its own evidence;
three examples are not extrapolated to the whole stock class.

| Plug-in | Origin / format | Evidence | Read result | Write check | Restore | Environment | Source | Limitations |
|---|---|---|---|---|---|---|---|---|
| Fruity Limiter | stock / native | read-profiled | complete; 18/18 real-looking; no padding | not run | not run | FL Studio 26.1.3 build 5336; Producer; Apple silicon | maintainer | Historical read profile predates report schema 1.0; exact PostFader revision was not recorded |
| Fruity Delay 3 | stock / native | read-profiled | complete; 26/26 real-looking; no padding | not run | not run | FL Studio 26.1.3 build 5336; Producer; Apple silicon | maintainer | Historical read profile predates report schema 1.0; exact PostFader revision was not recorded |
| Fruity Filter | stock / native | read-profiled | complete; 6/6 real-looking; no padding | not run | not run | FL Studio 26.1.3 build 5336; Producer; Apple silicon | maintainer | Historical read profile predates report schema 1.0; exact PostFader revision was not recorded |

The next stock pass should use a new blank project, load each available mixer
effect into a slot, and generate a report for each one. The current shareable
reporter validates mixer effects only. Generator parameters are reachable in
v0.20 through an explicit `channel_generator` target, but they have
no publishable matrix evidence yet and must not inherit mixer-effect evidence.

## Third-party mixer effects

No identified third-party product has a publishable schema-1.0 report yet.
That does not disable anything. PostFader will still ask FL for any loaded
third-party mixer's parameter map and report what it can and cannot verify.

Product names in future rows identify compatibility observations only. They
remain trademarks of their respective owners; inclusion does not imply
affiliation, endorsement, or a guarantee from the vendor or PostFader.

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
