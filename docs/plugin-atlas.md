# Plugin Atlas

Plugin Atlas is PostFader's bundled, versioned plug-in knowledge layer. It is
loaded from package data and works offline; the normal runtime does not scrape
vendor sites or search the user's filesystem. The installed bundle is the
source of truth for the CLI and the four Atlas MCP reads.

Creation Review may retain Atlas-backed capability and palette evidence from a
source Production Run when a revision changes a sound or semantic processing
goal. Atlas still does not prove that a target remains loaded or controllable;
the revision preflight must revalidate the live target and established control
before the existing verified writer can apply it.

## What Atlas means

Atlas keeps four kinds of information separate:

- Product knowledge describes a plug-in's purpose, techniques, modules, and
  limitations.
- Control-adapter knowledge describes controls that a particular product and
  format may expose to PostFader.
- Runtime evidence describes whether a loaded instance matched that knowledge
  and which controls were actually observed or validated.
- Availability describes only the current observation: `loaded`,
  `not_observed`, or `availability_unknown`.

An Atlas entry is not proof of ownership, installation, an exact loaded
version, or control of every parameter. A representative write validates only
the specific control tested. Generic loaded-plug-in discovery remains
available for unprofiled effects and Channel Rack generators through the
existing plug-in tools.

Sound Selection uses Atlas as one descriptor and role-suitability source when
planning a palette. A product known to Atlas but absent from the live inventory
can be recommended as a next step, but it cannot be assigned or selected by a
run. A loaded product with no Atlas record remains eligible through its runtime
name, target kind, preset identity, and user direction, with lower semantic
confidence. Atlas adapter metadata does not replace live target validation or
exact preset readback.

## Atlas capability and semantic processing

Atlas product knowledge and control-adapter knowledge can contribute to
effect-coverage planning, but neither one creates a live effect target. A
processing candidate must be a currently loaded mixer effect with a matching
Atlas capability, compatible adapter, and runtime control evidence. The
read-only `processing_plan` tool reports candidates, requested technique
categories, resolved display/option controls, missing capabilities, and
warnings. `processing_apply_plan` applies one authorized plan through the
existing verified setters and later-idle-tick readback; it does not bypass
Master protection or claim that the result was heard.

When a complete creation request includes processing, Production Runs keep
effect coverage in the readiness report and return `restrained_first_pass`,
`partially_processed`, `dry_missing_effects`, or another honest processing
status separately from technical execution and audible quality. Missing or
unresolved controls remain visible. See [Creation Pipeline](creation-pipeline.md)
for the phase, timing, and outcome contracts.

## Bundled snapshot

The checked-in snapshot is dated 2026-08-30 and describes FL Studio 26.1.5.
Its manifest is
[`fl_studio_mcp/plugin_atlas_data/manifests/atlas.json`](../fl_studio_mcp/plugin_atlas_data/manifests/atlas.json).
The catalog is intentionally scoped rather than presented as a claim that
every plug-in on the market is supported.

| Scope | Records | Meaning |
| --- | ---: | --- |
| `current_edition_matrix` | 119 | The current Image-Line pricing rows, with category and minimum FL Studio edition. |
| `current_auxiliary` | 5 | Current Image-Line plugin-like auxiliary modules: Mobile Rack + FX, Envelope Controller, Keyboard Controller, Voltage Controller, and MIDI Out. |
| `manual_index_only` | 14 | Older manual/index primitives or modules kept for identity and documentation context; they are not current pricing-matrix rows. |
| `legacy_discontinued` | 10 | Explicitly deprecated, removed, or unsupported Image-Line records; they are excluded from the current 119. |
| `selected_third_party` | 3 | Selected official records for FabFilter Pro-Q 4, Valhalla Supermassive, and Vital; this is not a completeness claim. |
| **Total** | **151** | Four vendors: Image-Line (148) plus three selected third-party vendors. |

The current 119-row Image-Line matrix contains 71 effects, 39 instruments,
6 visual plug-ins, and 3 audio editors. Its edition minimums are Fruity 81,
Producer 12, Signature 10, and All Plugins 16. DAW Core Features such as the
Piano Roll, Mixer, Audio Logger, and FL Studio Remote are not Atlas product
records. The five current auxiliary rows are plugin-like records; the 14
manual/index rows and 10 legacy rows remain separately scoped.

Image-Line separately advertises 82, 93, 103, and 118 instruments plus effects
for Fruity, Producer, Signature, and All Plugins editions. Those site-reported
headline totals use different inclusion semantics from the 119 category rows;
Atlas preserves the row snapshot and does not manufacture one total by mixing
the two counting schemes.

The canonical SHA-256 digest of the current matrix rows is
`42dbec71f6da690a13127d8062033b0d374c6302d77cdcd08465ada997d0cb4c`.
Use the CLI `digest` command for the complete loaded-bundle digest, which also
covers techniques, adapters, evidence, and selected third-party records.

## Sources and refresh policy

The current matrix is a reviewed snapshot of [Image-Line's FL Studio pricing
page](https://www.image-line.com/fl-studio/pricing). Auxiliary and
manual/index records are checked against Image-Line's [effects and plug-in
manual index](https://www.image-line.com/fl-studio-learning/fl-studio-online-manual/html/effects_plugins.htm),
[generator manual index](https://www.image-line.com/fl-studio-learning-content/fl-studio-online-manual/html/generator_plugins.htm),
and the [FL Studio 2026 release notes](https://www.image-line.com/fl-studio/release/2026).
Product records retain their official manual path where one exists. The
legacy records retain the relevant official manual or Image-Line support
article so that removal and deprecation are not silently converted into
current support.

Third-party records link to the vendors' official product pages in their data
files. Compatibility evidence is a separate reviewed input and is not used as
a runtime allowlist.

When refreshing the catalog, update the resource counts, source snapshot,
catalog date, and expected digests together. Keep current pricing rows,
plugin-like auxiliaries, manual/index context, and legacy records in separate
scopes. Do not fold DAW Core Features into the plug-in catalog.

## Bounds and trust boundaries

The loader is fail-closed and enforces local relative resource paths, duplicate
ID/alias checks, cross-reference checks, and manifest counts. Default resource
and JSON limits are:

| Limit | Default |
| --- | ---: |
| One resource | 16 MiB |
| All resources | 64 MiB |
| Manifest resources | 128 |
| JSON nesting depth | 32 |
| JSON nodes | 200,000 |
| Products / adapters | 4,096 each |
| Vendors | 256 |
| Techniques | 512 |
| Evidence records | 8,192 |
| Controls per adapter | 512 |
| Parameter index | 0–8,191 |

Registry search is capped at 128 results. Atlas MCP query strings are capped
at 512 characters, static result pages at 128 records, and loaded-runtime
match pages at 128 records. These are response and resource-safety bounds,
not claims about the size of a user's FL Studio project.

Availability intentionally has no `owned` or `installed` state. A loaded
observation can support a runtime match; it cannot establish licensing or
installation. A product-name match is reported as `name_only` and is never
control proof or permission to write a parameter. A validated control proves
only that control and that observation.

Preset and family metadata is a separate versioned resource at
`fl_studio_mcp/sound_selection/data/preset-metadata-v1.json`. It is reviewed
evidence for ranking and sound-aware composition, not part of Atlas's claim
that a product is loaded. Exact-preset, family, name-inferred, and unknown
coverage are reported separately; name inference cannot be promoted to high
confidence. Optional user-local annotations remain isolated from the bundled
catalog.

## CLI

The standalone CLI reads only the installed bundle:

```text
postfader-plugin-atlas search compressor --json
postfader-plugin-atlas show <product-id> --json
postfader-plugin-atlas digest --json
```

Unknown product IDs, invalid filters, and out-of-bounds limits return a
non-zero status without contacting FL Studio. The CLI is useful for inspecting
the static catalog; it does not inspect ownership, installation, or live
runtime state.

## MCP reads

The MCP server exposes four generic, read-only Atlas tools:

- `plugins_atlas_search` searches static knowledge with bounded text, vendor,
  kind, stock, and result filters.
- `plugins_atlas_get_product` fetches one product by exact ID together with
  related vendor, adapter, evidence, and stock-alternative records.
- `plugins_atlas_recommend` ranks products or explicit stock alternatives from
  bounded production criteria.
- `plugins_atlas_inspect_loaded` matches the current target-aware Track B
  inventory to static Atlas knowledge.

The first three are local closed-world reads. The loaded-inventory tool reads
the existing target-aware Track B inventory, so mixer-effect slots and global
Channel Rack generators remain distinct even when their display names are
identical. It preserves generic discovery for names with no Atlas record.

See [Tool and command reference](tool-contracts.md) for exact request and
response contracts, [Sound Selection](sound-selection.md) for palette and
preset-selection behavior, and [Plug-in support](plugin-support.md) for the
separate runtime parameter and compatibility-evidence workflow.
