# Canonical distribution metadata

This page is the reusable source of truth for directory listings, package
indexes, community catalogs, and maintained software lists. Copy the metadata
without strengthening its claims. A listing is not evidence that a directory
has accepted PostFader; record actual submissions in the ledger at the bottom.

## Product name

PostFader

## One-line description

PostFader is a local FL Studio MCP server for AI-assisted mixing, plug-in
control, MIDI composition, and arrangement on Windows and macOS.

## Release scope and directory copy

PostFader **V10 (10.0.1)** exposes **134 tools and 8 resources**. Use that
count in current listings. Older version counts belong only in historical
release notes. The immutable package version, registry record, and GitHub tag
must agree before a listing is marked updated.

> PostFader is a local FL Studio MCP server for Windows and macOS. Its 134 tools
> and 8 resources support project inspection, mix analysis, plug-in control,
> MIDI composition, sound selection, recoverable production runs, revision
> workflows, note inspection, and saved-project rendering. Starts read-only;
> never saves automatically. Requires local FL Studio, Universal Bridge and
> virtual MIDI. Newer host paths have documented live-validation limits.

## Short description

PostFader connects Claude, Codex, Cursor, and other local MCP clients to the
project open in FL Studio. Inspect mixer routing and loaded plug-ins, diagnose
exported mixes, choose coherent sound palettes, control the session, generate
MIDI parts, and prepare patterns and arrangement markers through 134 tools and
8 live resources. Creation Review can evaluate caller-exported bounces, plan
one bounded revision, compare before/after evidence, and produce manual
delivery handoffs. Guided packages support Windows and macOS. PostFader starts
read-only and never saves automatically.

## Long description

PostFader is the local Model Context Protocol (MCP) copilot for producers using
FL Studio. Connect Claude, Codex, Cursor, or another local MCP host to the
project already open. The AI can inspect mixer inserts and routing, Channel
Rack generators, loaded effects, patterns, Playlist tracks, undo/redo history
state, transport, steps, and plug-in parameters exposed by FL Studio. It can
control the session and supported loaded plug-ins without assuming every
producer uses the same chain.

PostFader also connects production decisions across tools. Run Mix Doctor on an
exported bounce, compare loudness and tonal balance with a reference, examine
synchronized vocal and instrumental renders for likely masking, watch mixer
peaks during playback, and use your AI to turn selected findings into
reviewable mix plans. Generate chords, melody, bass, and drums; export
multi-track Type-1 MIDI; estimate tempo and key; transcribe monophonic audio;
and prepare patterns, section markers, or Piano Roll transforms. Sound Selection
can choose a coherent role-based palette from loaded generators, verify exact
preset navigation, preserve anchors across sections, and map reported drum
pads.

For a delegated creation request, the connected AI can run one bounded,
phase-based Production Run: read a multi-dimensional readiness scorecard,
choose sounds from the loaded pool, adapt composition to sound metadata,
write a modest pattern, and optionally apply restrained semantic processing
to loaded, adapter-backed effects. Technical execution, arrangement delivery,
processing state, manual handoff, and audible quality are reported separately;
the connector does not claim to have heard the live project.

Creation Review continues a completed Production Run with explicit bounces. It
retains immutable source evidence, structured producer feedback, independent
locks, bounded revision receipts, comparisons, and delivery metadata. It does
not capture live audio, render, save, create Playlist clips, insert plug-ins,
or infer artistic approval from technical measurements.

Separate V10 tools inspect existing Piano Roll notes, retain Production
Run journals across restarts, explicitly resume saved plans after revalidation,
and render an already-saved FLP to WAV in a background job. Native macOS
Add-menu discovery and loading identify new instruments/effects through bridge
readback. Loading requires Accessibility access and supported English menus;
Windows insertion, removal, and reordering are unavailable. Live qualification
of the new note-read and renderer paths is still pending.

The current V10 surface contains 134 tools and 8 live resources. Guided Windows and
macOS packages, dedicated Codex packages, a Claude Desktop MCPB, and Python
distributions cover different setup needs. PostFader starts read-only, requires
session-only authorization for writes, never saves automatically, and reads
supported direct changes back from FL Studio. It has no hosted PostFader
service, account, or telemetry.

## Safe claims to reuse

- PostFader is a local MCP server for FL Studio.
- Windows and macOS are supported, with the exact qualified environments
  documented in the repository.
- Startup is read-only.
- Writes require explicit session-only authorization and retain the existing
  Master protection.
- Supported direct setters use later-update readback; weaker evidence is
  labeled partial or unverified.
- The current V10 surface has 134 tools and 8 live resources, including
  13 Creation Review tools and 9 corresponding Production Run operations.
- Creation Review measures explicit bounces, preserves producer feedback and
  locks, applies one bounded revision through the existing run executor, and
  prepares comparison and delivery handoffs; it does not render, save, or
  establish artistic approval.
- Sound Selection plans and applies exact loaded presets, coherent role-based
  palettes, section variations, and reported drum maps; it does not insert
  plug-ins or claim that a preset was heard.
- Creation readiness, phase timing, sound-aware composition, and semantic
  processing are available as bounded local workflows; missing capabilities
  remain explicit and audible quality is not inferred from technical receipts.
- Guided setup connects the local package, Universal Bridge, virtual MIDI
  selection, client configuration, and doctor checks; it does not install a
  virtual MIDI provider.
- PostFader has no hosted PostFader service, no PostFader telemetry, and no
  automatic project save.
- Standard ZIPs, Codex ZIPs, the Claude Desktop MCPB, and Python distributions
  are separate package choices with documented boundaries.

## Claims that must not be made

- PostFader guarantees rollback or a particular undo point.
- Every action or every plug-in parameter is verified.
- Every plug-in is fully supported.
- PostFader can hear FL Studio's live audio output.
- PostFader can load every plug-in, insert on Windows, or remove/reorder plug-ins.
- Older release downloads contain V10 features.
- PostFader is officially ranked first or independently certified as the best
  FL Studio MCP server without current, attributable evidence.
- PostFader cannot damage or dirty a project.
- PostFader is made by, endorsed by, or affiliated with Image-Line.
- PostFader saves, backs up, or automatically restores a project.

## Categories and tags

Use only tags that fit the listing's vocabulary. The canonical set is:

`audio`, `music-production`, `mixing`, `composition`, `arrangement`, `FL Studio`,
`MCP`, `Model Context Protocol`, `AI tools`, `local-first`, `MIDI`,
`audio-analysis`, `plug-in-compatibility`, `Windows`, `macOS`.

Do not add `remote MCP`, `hosted service`, `telemetry`, `rollback`, or
`Image-Line` as a category or tag.

## Canonical links

| Purpose | Link |
| --- | --- |
| Repository | [github.com/synopsys0/postfader-fl-studio-mcp](https://github.com/synopsys0/postfader-fl-studio-mcp) |
| Latest release | [GitHub releases/latest](https://github.com/synopsys0/postfader-fl-studio-mcp/releases/latest) |
| Setup guide | [docs/setup.md](setup.md) |
| Tool contracts | [docs/tool-contracts.md](tool-contracts.md) |
| Sound Selection | [docs/sound-selection.md](sound-selection.md) |
| Creation Pipeline | [docs/creation-pipeline.md](creation-pipeline.md) |
| Creation Review | [docs/creation-review.md](creation-review.md) |
| V10 feature notes | [docs/releases/dev-v10.md](releases/dev-v10.md) |
| Security policy | [SECURITY.md](../SECURITY.md) |
| Plug-in matrix | [docs/plugin-matrix.md](plugin-matrix.md) |
| Issue tracker | [GitHub Issues](https://github.com/synopsys0/postfader-fl-studio-mcp/issues) |
| Python package | [PyPI: postfader-fl-studio-mcp](https://pypi.org/project/postfader-fl-studio-mcp/) |

Use the repository's release page for checksums and platform assets. Do not
invent a directory listing URL, an acceptance status, or an independent
qualification result.

## Creation Review release contents and verification

The V10 source and platform bundles include the versioned
`fl_studio_mcp/creation_pipeline/` and `fl_studio_mcp/creation_review/`
contracts, the bundled `sound_selection/data/preset-metadata-v1.json` resource,
the creation acceptance harnesses, and the public creation guides. The Claude
Desktop MCPB contains runtime modules/package data, including Creation Review,
but intentionally excludes maintainer scripts and tests; the standard/Codex
ZIPs and source archive carry the maintainer docs, fixture generator, and
acceptance scripts. The source archive also carries the deterministic fixture
payload used by tests; the platform ZIPs intentionally exclude `tests/` and
ship the fixture generator instead.

Before publishing, run the public-tree check, build the wheel/source archive,
run the clean installed-package smoke test, verify the release bundles, and
run the MCPB checker where that bundle is produced. These checks assert the
creation-pipeline and Creation Review modules, preset metadata, docs, the
appropriate fixture generator or source-archive fixture payload, and acceptance
scripts are present; they do not qualify a live FL Studio run. Keep live projects, screenshots,
logs, timing evidence, and acceptance output outside the public repository.

## Package-selection explanation

| Package | Intended user | Boundary to state clearly |
| --- | --- | --- |
| Standard Windows ZIP | Windows users of Claude Desktop, Claude Code, Cursor, OpenCode, T3 Code provider flows, Grok Build, or another local `stdio` host | Platform installer and guided setup; still requires FL Studio, the matching Universal Bridge, a virtual MIDI endpoint, and FL Studio MIDI configuration. |
| Standard macOS ZIP | macOS users of the same local MCP hosts | Same platform/setup boundary as the Windows ZIP; the package does not install or configure the IAC bus for the user. |
| Codex Windows ZIP | Codex CLI, IDE extension, or desktop Codex users on Windows | Adds Codex-specific guided registration after the same local platform setup. It is not a remote service and does not replace FL Studio, bridge, MIDI, or Python prerequisites. |
| Codex macOS ZIP | Codex users on macOS | Same Codex registration boundary; the user still completes the local FL Studio and virtual MIDI setup. |
| Claude Desktop MCPB | Users who want Claude Desktop's extension packaging after base setup | The MCPB does not replace the platform ZIP/Python installation, Universal Bridge, virtual MIDI endpoint, FL Studio MIDI settings, or doctor. It is not a general MCP host package. |
| Python wheel/source archive | Advanced users, maintainers, and environments that manage Python dependencies themselves | The user runs the documented setup flow and supplies FL Studio, a virtual MIDI provider, and a local MCP host. The source archive is not a prequalified platform installer. |

Keep package names and compatibility identifiers exactly as published. Public
prose uses **PostFader**; Python package, command, registry, and compatibility
names remain lowercase where their interfaces require it.

## Directory-maintenance ledger

Record only real submissions and verify each listing against the canonical
metadata. `—` means no submission has been made; it is not an acceptance.

| Platform | Canonical destination | Update mechanism |
| --- | --- | --- |
| GitHub Releases | [V10](https://github.com/synopsys0/postfader-fl-studio-mcp/releases/tag/v10.0.1) | Tagged release workflow uploads four platform ZIPs, MCPB, wheel, sdist, SBOM, and checksums; publication must be verified. |
| PyPI | [Package](https://pypi.org/project/postfader-fl-studio-mcp/) | Trusted Publishing from the version tag; immutable versions cannot be replaced. |
| Official MCP Registry | [Latest record](https://registry.modelcontextprotocol.io/v0.1/servers/io.github.synopsys0%2Fpostfader-fl-studio-mcp/versions/latest) | OIDC publication after PyPI; verify the server and package versions. |
| Glama | [Listing](https://glama.ai/mcp/servers/synopsys0/postfader-fl-studio-mcp) | Maintainer claimed; edit description and request repository sync after main is updated. There is no hosted FL Studio runtime release. |
| MCP Market | [Listing](https://mcpmarket.com/server/postfader) | Existing indexed listing; owner editing was unavailable. Operator correction is needed for stale summary/counts. |
| mcpindex.ai | [Registry mirror](https://mcpindex.ai/server/io-github-synopsys0-postfader-fl-studio-mcp) | Downstream registry refresh; search snippets can lag the live page. |
| mcpbeat | [Listing](https://mcpbeat.com/mcp-servers/synopsys0/postfader-fl-studio-mcp/) | Downstream refresh/operator correction; generated configuration must use `fl-studio-mcp` and include bridge/MIDI prerequisites. |

This is a publication map, not a claim that every downstream index has refreshed.
No Smithery publication was verified. Glama container releases do not provide
access to a producer's local FL Studio instance; preserve the local-only classification.

Before updating a listing, check the product name, one-line description, safe
claims, package boundary, repository/release links, version, tool count, and
platform text. Never claim that a directory has accepted PostFader until its
listing is visible and verified by a maintainer.

## Search visibility and evidence

Use “FL Studio MCP server” naturally in the repository title, opening copy,
package descriptions, and relevant feature documentation. Keep product name,
repository URL, release version, operating systems, and installation requirements
consistent across directories. Link directly to setup, tool contracts, current
release notes, and development notes. Avoid keyword stuffing, invented reviews,
unearned “official” badges, or a numerical ranking without a dated source.

Search engines and directory operators control indexing and rankings. Strong
positioning should rest on reproducible installation, meaningful tests, explicit
feature boundaries, maintained documentation, and independently reported user
results. A tool count is useful inventory, not evidence of superior musical output.

Glama documents [ownership and synchronization](https://glama.ai/blog/2025-07-08-what-is-glamajson).
The MCP Registry documents [immutable version metadata](https://modelcontextprotocol.io/registry/versioning).
