# Maintainability and module-decomposition plan

Status: planning only. This is not a post-release rewrite proposal and does
not authorize changing the public tool surface. PostFader v0.20 is intentionally
left as a behaviorally stable release after qualification on Windows and
macOS. Any extraction should be a focused, separately reviewable change with
the existing contracts and safety tests passing before and after it.

The current code is organized by feature and safety boundary, but several
modules are large because they keep a complete protocol or workflow together.
That size alone is not evidence that a split is safe. In particular,
fl_studio_mcp/mcp_server.py, performance.py, verified_writer.py, bridge_client.py,
acceptance.py, and _bridge/device_UniversalBridge.py contain policy and
ordering decisions that should not be separated mechanically without focused
regression tests.

## Non-negotiable extraction rules

- Preserve read-only startup, session-only write authorization, explicit Master
  protection, later-idle-tick readback, no automatic replay after an ambiguous
  mutation, no automatic project save, and honest partial/unverified evidence.
- Preserve bridge-source provenance, strict argument/result contracts, bounded
  MIDI/filesystem/decoded-audio behavior, current-pattern and observation
  fingerprints, and all public tool/resource names.
- Do not add a generic bridge dispatcher, remote/hosted MCP path, telemetry, or
  a new major tool category as part of a split.
- Keep fl_studio_mcp/_bridge/device_UniversalBridge.py ASCII-only, callback
  safe, FL-thread-only, and within its idle-tick budget. It must continue to
  deploy as one controller script without an __init__.py.
- Make one logical extraction per commit or pull request where practical.
  Avoid mixing renamed imports, formatting rewrites, dependency changes, and
  behavior changes in the same commit.
- Use the existing fake FL API and synthetic audio for deterministic tests.
  Any behavior that requires live FL Studio remains explicitly qualified and
  must not be represented as hermetic coverage.

## Proposed extraction register

The table is an order-of-operations plan, not a promise that every boundary
will be extracted. “Mechanical” means imports/registration wiring can likely
move with identical behavior after characterization tests; “No” means the
boundary includes policy or protocol decisions and needs a staged design.

| ID / order | Current module(s) | Intended module/package boundary | Behavior that must remain identical | Tests that protect it | Risk | Mechanical? |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | mcp_server.py resource decorators and resource_* handlers | mcp_resources.py (or a small mcp_resources/ package) | Exactly 8 URI registrations, read-only routing, shared compatibility gates, typed payloads, and no resource write path | tests/test_sdk_compatibility.py, resource assertions in tests/test_readonly_mcp.py, tests/test_readonly.py | Medium: registration/import cycles and SDK metadata | Mostly yes after characterization |
| 2 | mcp_server.py capability/project/history/transport read handlers; readonly_inspector.py; track_b_contracts.py snapshots | project_tools.py and capability_tools.py | Read allowlist, strict response contracts, evidence kind/warnings, project/transport/history semantics, and no mutation dispatch | tests/test_readonly.py, tests/test_readonly_mcp.py, tests/test_diagnostics.py, fake bridge tests in tests/test_bridge.py | Medium | No: keep gateway/policy adapters explicit |
| 3 | mcp_server.py mixer handlers; verified_writer.py mixer gateway; readonly_inspector.py mixer parsing; bridge mixer commands | mixer_tools.py plus an unchanged safety gateway | Track indexing, Master authorization, expected-before checks, later-tick readback, per-field proof, undo evidence, and send-route refusal | tests/test_readonly.py, tests/test_bridge.py, tests/test_mixing.py, tests/test_live_acceptance.py | High | No |
| 4 | mcp_server.py plug-in handlers; plugin_profile.py; plugin_report.py; plug-in portions of verified_writer.py, readonly_inspector.py, track_b_contracts.py, and bridge | plugin_tools.py, with plugin_profiles.py and plugin_reporting.py only if contracts stay separate | Discriminated mixer-effect/channel-generator targets, slot/parameter bounds, unprofiled read-only status, option-sweep restore evidence, and no claim of universal plug-in support | tests/test_plugin_profile.py, tests/test_plugin_report.py, plug-in sections of tests/test_readonly.py and tests/test_bridge.py | High | No |
| 5 | mcp_server.py Channel Rack/pattern/Playlist/step handlers; performance.py; track_b_contracts.py; bridge channel/pattern/sequence commands | channel_pattern_tools.py (or channel/ and pattern/ subpackages) | Global channel scope, current-pattern refusal, channel fingerprint/digest guards, bounded step batches, non-atomic receipts, and later readback | tests/test_performance.py, tests/test_readonly.py, tests/test_bridge.py, tests/test_tick_budget.py, tests/test_live_acceptance.py | Very high | No |
| 6 | Composition, MIDI serialization, Piano Roll, arrangement, and automation sections of creative.py; corresponding mcp_server.py wrappers | creative/composition.py, creative/midi.py, creative/piano_roll.py, creative/arrangement.py | Deterministic note digests, Type-1 atomic write/reopen checks, manual Piano Roll handshake and dispatch-only evidence, marker/automation partial evidence, and bounded files | tests/test_creative.py, creative read-only assertions in tests/test_readonly.py, tests/test_package_hygiene.py | High: generated scripts and platform focus | No |
| 7 | audio.py, advisory.py, music_analysis.py, and audio wrappers in mcp_server.py | audio_analysis/ package with dsp.py, advisory.py, music.py, and a thin host adapter | Absolute path policy, fixed recent-bounce roots, decoded-memory limits, synthetic provenance/hash reporting, confidence/limitations, and no FL live-buffer claim | tests/test_audio.py, tests/test_advisory.py, audio/music assertions in tests/test_creative.py and tests/test_readonly.py | Medium-high | No for path/DSP split; yes for wrapper wiring |
| 8 | mixing.py, mix wrappers in mcp_server.py, and workflows.py plan/batch adapters | mix_workflows.py plus explicit batch_executor.py adapter | Mix Doctor thresholds, peak-watch bounds, process-local IDs, plan one-shot lifecycle, batch non-atomicity, and read-only analysis/application separation | tests/test_mixing.py, tests/test_workflows.py, tests/test_readonly_mcp.py, tests/test_readonly.py | High | No |
| 9 | acceptance.py read supervisor, coverage validation, and read scenario helpers | acceptance/read.py and acceptance/contracts.py | Isolated worker/timeouts, read-only tool-surface coverage, evidence output policy, deadlines, and no live claim from fake tests | tests/test_live_acceptance.py, tests/test_file_transport.py, tests/test_readonly_mcp.py | High: subprocess/timeouts and output safety | No |
| 10 | acceptance.py write scenario planning, templates, validation, and write runner | acceptance/write.py and acceptance/scenarios.py | Explicit disposable/live marker, precondition/template validation, Master refusal, non-replay, restore evidence, and private output destinations | tests/test_live_acceptance.py, tests/test_bridge.py, tests/test_readonly.py | Very high | No |
| 11 | bridge_client.py TCP/file/MIDI framing, correlation, ownership, and recovery | transport/protocol.py, transport/mailbox.py, transport/midi.py, transport/ownership.py | Wire protocol version gates, fragment bounds/expiry, request serialization, response correlation, endpoint lock, read-only retry classification, and write no-replay | tests/test_midi_framing.py, tests/test_midi_transport.py, tests/test_file_transport.py, tests/test_bridge_client_recovery.py, tests/test_resource_bounds.py | Very high | No |
| 12 | Bridge read helpers/commands in _bridge/device_UniversalBridge.py | bridge_read_commands.py or generated/registered read-command table in the bridge source | Read allowlist, handshake/provenance, bounded scans, later-idle jobs, ASCII source, callback exception containment, and no writes reachable through read dispatch | tests/test_bridge.py, tests/test_bridge_stamp.py, tests/test_readonly.py, tests/test_tick_budget.py, tests/test_package_hygiene.py | Very high | No; bridge deployment makes this a staged extraction |
| 13 | Bridge direct writes, Track B writes, jobs, and creative dispatch in _bridge/device_UniversalBridge.py | bridge_write_commands.py plus a small shared safety helper kept in the deployed source | Session gate, source stamp, Master guard, expected-before/session/digest guards, undo/readback sequence, ambiguous-outcome behavior, no save, and bounded idle work | tests/test_bridge.py, tests/test_tick_budget.py, tests/test_readonly.py, tests/test_performance.py, tests/test_live_acceptance.py | Critical | No |
| 14 | verified_writer.py write-mode gateway/manager and write result normalization; bridge session.set_write_mode | write_mode.py and a narrow write_receipts.py contract adapter | Literal present-user confirmation, provenance/session checks, second handshake, session-only state, disable semantics, and typed proof fields | tests/test_readonly.py, tests/test_readonly_mcp.py, tests/test_bridge.py, tests/test_bridge_stamp.py | Critical | No |
| 15 | contracts.py and track_b_contracts.py shared base/serialization helpers | contracts/base.py, contracts/project.py, contracts/write.py, and contracts/track_b.py | Pydantic strictness, immutable models, finite-number rejection, discriminated target unions, and wire-compatible JSON | tests/test_readonly_mcp.py, tests/test_readonly.py, tests/test_performance.py, tests/test_bridge.py, tests/test_sdk_compatibility.py | High | No: contract extraction only after snapshots are pinned |

The intended module names are placeholders. A maintainer may choose different
names if the dependency direction and public behavior remain the same. The
important boundary is the responsibility and its tests, not the filename.

## Boundary details and recommended order

### MCP resource registration

Move only decorator registration and resource adapter wiring first. Keep the
connection/inspection gateways in their current modules until the registration
split is green. Characterize all eight URIs, returned media/content types,
annotations, and read-only behavior before moving code. This is the best first
mechanical extraction because it has a clear SDK test oracle and no new safety
policy.

### Project and capability tools

Separate the read-facing wrappers from mixer and creative registration, but
leave ReadOnlyInspector, TrackBInspector, and the bridge client as shared
services. Do not duplicate ping/provenance or build a second project snapshot
parser. Project history reads and history mutations must remain distinct in
contracts and evidence.

### Mixer and plug-in tools

These are related but should not be merged into one large “controls” module.
Mixer writes carry explicit Master and send-route policy. Plug-in writes carry
ambiguous target kinds, parameter discovery/sweep behavior, and unprofiled
read-only status. Extract wrappers only after tests pin the gateway call graph;
keep verified_writer.py or a successor as the single mutation policy owner.

### Channel Rack, patterns, Playlist, and step sequence

Treat this as a safety-critical extraction. Global channel indices, current
pattern checks, observation-scoped fingerprints, and canonical step digests
must stay adjacent to their contract validation. Do not move a handler before
characterizing refusal order and the exact later-tick boundaries in fake FL.
A pattern-selection helper must never be reused to make an implicit pattern
switch for a step read/write.

### Creative tools

Split by runtime and evidence boundary, not merely by function count:
composition and offline MIDI are pure; Type-1 export is bounded filesystem
output; Piano Roll runs in FL's separate .pyscript runtime and has dispatch-
only evidence; arrangement/automation depend on asymmetric FL getters. Keep
request registries and serialized in-flight guards in one owner. Generated
script text must remain ASCII/portable as currently required and must never
become an unrestricted script injection surface.

### Audio-analysis tools

The pure DSP layer can eventually move independently from advisory path/root
policy and MCP adapters. Keep path validation before decoding, and preserve
separate on-disk and decoded-memory bounds. Preserve provenance, file hash,
confidence, and limitations fields. Never describe fake audio fixtures as live
FL coverage or add a background scan/telemetry path.

### Mix workflows and batch execution

mixing.py should remain explicit about which operations are analysis, plan
creation, or application. The batch executor must remain closed-union,
bounded, ordered, and non-atomic. If extraction creates a shared executor,
make the plan one-shot state machine and its terminal partial/failed semantics
visible through tests rather than hiding them behind generic task helpers.

### Acceptance reads and writes

The acceptance harness is an operator tool, not part of the ordinary MCP
request path. Split read and write runners only after their output-destination,
subprocess, deadline, and private evidence rules are covered. Write scenarios
must continue to require a live/disposable marker and explicit restoration
logic; no extraction should turn a live acceptance result into a package claim.

### Transport protocol

Separate wire framing from platform endpoint adapters only after golden frames,
size ceilings, and recovery behavior are pinned. A transport abstraction must
retain the distinction between a read that can be safely retried and a mutation
whose response was ambiguous. Endpoint ownership is cooperative and must not be
represented as cryptographic authentication. Keep native MIDI opt-in and
sandboxed CI behavior in a platform adapter with tests.

### Bridge read commands

The bridge is deployed as one script and is constrained by FL's embedded
runtime. A future extraction might use a source-generation step or explicit
command table, but generated output must be byte-stamped and inspected before
packaging. Read command extraction must not import server-only dependencies or
introduce background threads. First characterize command allowlists, job
resumption, per-idle tick ceilings, and callback exception behavior.

### Bridge write commands

This is the highest-risk boundary. Do not split it until a focused bridge test
suite covers every direct and Track B mutation, session/provenance/expected-state
check, Master rule, undo/readback ordering, ambiguity handling, and no-save
invariant. If extraction is approved, retain one small shared guard function
or generated policy table rather than copying checks into each command module.

### Session/write-mode behavior

Keep write-mode transition as a single capability owner. The host manager, bridge
session.set_write_mode, handshake verification, source stamp, session
fingerprint, and result contract must not drift into parallel implementations.
A future write_mode.py should own only transition policy; direct write tools
should ask it for capability and should not be able to enable writes as a side
effect.

### Contracts

Contract decomposition is last because contracts are the stable seam between
many modules. Before moving classes, snapshot JSON schemas and representative
refusal/error payloads. Preserve strict unknown-field rejection, finite-number
checks, immutable response models, and discriminated unions. A mechanical file
move is acceptable only after import-cycle checks and schema snapshots are
present.

## Suggested pull-request sequence

1. Add characterization tests/schema snapshots for the eight resources and
   existing public tool list.
2. Extract resource registration, then project/capability read wrappers.
3. Extract one feature surface at a time: mixer, plug-in, then Channel Rack /
   pattern / Playlist.
4. Split creative pure/file/runtime boundaries.
5. Split audio analysis from advisory path policy.
6. Split mix workflows from the batch executor without changing lifecycle
   behavior.
7. Split acceptance reads, then acceptance writes.
8. Split transport framing/adapters only with wire/recovery golden tests.
9. Consider bridge read-command generation/extraction after deployment tests;
   defer bridge writes and write-mode until a separate safety review.
10. Decompose contracts last and only if import/schema snapshots make the seam
    unambiguous.

Every pull request should identify the moved current module, intended owner,
unchanged safety properties, tests run, and whether the extraction was
mechanical. A failing live-FL qualification should block a safety-sensitive
split even when the hermetic suite passes.

## Exit criteria for any extraction

- The safe suite and focused tests pass from a clean checkout.
- Public tool/resource counts, names, annotations, argument schemas, result
  schemas, and refusal types are unchanged unless a separately approved
  compatibility change exists.
- Read-only startup, provenance failure behavior, session write authorization,
  Master protection, later-idle-tick readback, no-replay behavior, no-save
  behavior, evidence labels, and resource bounds have explicit regression
  coverage.
- scripts/check_public_tree.py and package-hygiene tests still reject private
  project/audio/log/cache material.
- No new telemetry, hosted service, arbitrary filesystem search, generic bridge
  dispatch, or major tool category entered as an incidental dependency.
- The diff contains no unrelated formatter rewrite or mass rename that obscures
  the moved boundary.

This plan intentionally leaves architecture changes for a later design review.
A smaller module is not automatically a safer module; the safety boundary and
its tests are the deliverable.
