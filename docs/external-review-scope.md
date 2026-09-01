# External security and architecture review scope

Status: review preparation only. No independent security or architecture
review has been performed or commissioned for this repository. This document
is a bounded hand-off for a future reviewer; it is not a certification, audit
report, or claim that the current release is secure against every local
workstation threat.

The review should use the current `main` revision and the matching public
release artifacts. If the revision changes, the reviewer should record the
exact commit and release in the deliverables. The public architecture and
tool contracts are described in [architecture](architecture.md) and
[tool contracts](tool-contracts.md); those documents are the starting point,
not substitutes for inspecting the implementation.

## Architecture overview

The intended local path is:

```text
MCP client / model host
        │ local stdio MCP transport
        ▼
PostFader local server
        │ bounded named tools, resources, contracts, policy gates
        ├──────────────► audio-file analysis (caller-selected files)
        ├──────────────► generated Type-1 MIDI / Piano Roll workflow files
        │
        │ local bridge transport (MIDI SysEx in production)
        ▼
configured virtual MIDI endpoint
        ▼
Universal Bridge in FL Studio's controller-script runtime
        ▼
FL Studio project and its UI/API state
```

The components in scope are:

- **MCP client.** A local host invokes named PostFader tools and reads their
  typed results. The client and any model provider it uses are outside
  PostFader's trust boundary; a client may send tool arguments and results to a
  remote provider according to its own policy.
- **Local stdio server.** `fl_studio_mcp/mcp_server.py` registers the 114 MCP
  tools and 8 live resources. It routes reads, bounded audio analysis,
  creative/file workflows, verified mutations, and session write-mode control
  through typed contracts. It must not become a generic bridge-command or
  unrestricted filesystem interface.
- **Transport.** `fl_studio_mcp/bridge_client.py` implements the production
  local MIDI SysEx path and bounded TCP/file transports used by tests. It
  serializes requests, validates framing and response correlation, owns the
  endpoint lock, and classifies ambiguous reads and writes differently.
- **Virtual MIDI endpoint.** CoreMIDI/IAC on the qualified macOS path and
  WinMM-compatible virtual MIDI on Windows carry the local SysEx protocol. The
  endpoint is shared and unauthenticated; the cooperative process lock avoids
  duplicate PostFader clients but is not authentication.
- **Universal Bridge.**
  `fl_studio_mcp/_bridge/device_UniversalBridge.py` is copied into FL Studio's
  controller-script folder. It runs on FL's main scripting thread, catches
  callback-boundary errors, bounds work per idle tick, stamps its source hash,
  and exposes separate read, session-capability, and verified-mutation
  command allowlists.
- **FL Studio project.** The bridge reads and, only after a session-only gate,
  changes the selected FL Studio project. A normal process starts read-only;
  supported setters yield to a later FL idle tick and report observed
  readback. The bridge never calls `saveProject`, does not guarantee rollback
  or an undo point, and does not provide live FL audio buffers.
- **Audio-file analysis.** `audio.py` performs bounded offline DSP over files
  selected by the caller; `advisory.py` applies typed path/root policy and
  exposes measurements, comparisons, masking analysis, and bounded recent
  bounce discovery. The FL API is not used to read live audio.
- **Piano Roll script workflow.** `creative.py` prepares an atomic generated
  `.pyscript` beneath the FL Studio user-data scripts directory, verifies the
  channel/pattern/visibility preconditions through the controller bridge, and
  may dispatch a platform shortcut after the user's one-time manual bootstrap
  confirmation. The controller side cannot read the resulting note grid, so
  focus or hotkey dispatch is not application verification.

The current safety-critical sequence for a supported state write is:

```text
read handshake and stamped provenance
  → resolve target
  → check session fingerprint and expected-before state
  → request an FL undo point where applicable
  → issue one bounded mutation command
  → yield to a later FL idle tick
  → read back and return per-field evidence
```

The reviewer should verify this sequence in code rather than infer it from a
successful client response. `verified: false` is an honest observation result,
not permission to replay or roll back the mutation.

## Trust boundaries

The review should state which claims rely on the following assumptions and
which controls hold when an assumption is violated:

- **Trusted local workstation assumption.** PostFader is intended for a
  single-user macOS or Windows workstation. It is not a remote service or a
  multi-user isolation boundary. A local attacker able to control the client,
  selected MIDI endpoint, desktop focus, or writable paths may be able to
  influence the workflow.
- **MCP client outside PostFader's trust boundary.** The client decides which
  model/provider sees tool arguments and results. Results can contain project
  metadata, track and plug-in names, canonical audio paths, file hashes, and
  measurements. PostFader provides no hosted service and no telemetry.
- **Unauthenticated shared virtual MIDI endpoint.** SysEx traffic is local but
  not cryptographically authenticated. The endpoint provider and another local
  MIDI-capable process are outside the bridge's authentication boundary. The
  request/session correlation and cooperative lock are integrity and safety
  checks for ordinary operation, not identity proof.
- **Local filesystem access.** Direct audio analysis accepts caller-selected
  absolute files subject to extension, regular-file, size, duration, and
  decoded-memory bounds. Recent-bounce discovery uses fixed FL/project roots.
  Generated MIDI and Piano Roll files are intentionally writable capabilities;
  the caller can name an existing MIDI path only with explicit overwrite.
- **Caller-selected audio paths.** A valid absolute path can identify an audio
  file outside FL Studio's folders. The MCP client therefore needs trusted
  path arguments and appropriate filesystem permissions; PostFader does not
  promise that a path is private merely because it passed validation.
- **Generated MIDI and Piano Roll files.** A Type-1 MIDI export is atomically
  written, reopened, parsed, and verified, but remains a file the caller asked
  PostFader to create. The Piano Roll `.pyscript` is atomically replaced in a
  configured scripts directory and may be dispatched with desktop focus; the
  script's execution and resulting note edits are not fully observable.

The reviewer should also inspect the boundary between the packaged bridge and
the deployed copy. A missing or mismatched source stamp fails closed for
mutations while preserving warning-bearing reads. Session fingerprints and
`expected_before`/step-digest checks are optimistic-concurrency controls, not
authentication or durable project identity.

## Critical review areas

The following list is the minimum review scope. Each item should result in a
code location, a finding or explicit no-finding, and a suggested synthetic
reproduction where useful.

### Read-only allowlist

- Confirm that startup and an ordinary FL Studio launch are read-only.
- Enumerate the bridge read allowlist and the MCP tools/resources reachable
  without the session write gate.
- Confirm no read path can smuggle a mutation command, generic command name, or
  arbitrary FL API call.
- Check that malformed, stale, or contradictory bridge replies fail as errors
  rather than becoming plausible success evidence.

### Write-mode authorization

- Trace `fl_set_write_mode` through the host gateway, bridge command, and
  second handshake.
- Confirm that enabling requires literal `confirm_user_present=true`, a
  current session fingerprint, matching bridge provenance, runtime-control
  support, and an independently observed write-mode transition.
- Confirm that the state is in memory for the bridge session only, disabling is
  available without positive confirmation, and normal reload/new-process
  behavior returns to read-only.
- Verify that individual write tools do not silently enable write mode or add a
  second unreviewed confirmation path.

### `confirm_user_present` limitations

Treat the field as a client-supplied assertion, not proof of human identity,
authentication, or informed consent. Review whether the docs and tool
annotations make that limitation clear and whether a compromised or
misleading MCP client can set it without an actual user decision. Do not
describe the current field as an out-of-band confirmation mechanism.

### Session fingerprint

- Check generation, scope, comparison, and invalidation of the optional
  bridge-lifetime fingerprint.
- Verify that it prevents stale-session decisions but is not treated as a
  durable project ID, user identity, or secret.
- Trace expected-before and current-pattern/channel fingerprints through target
  resolution and the bridge's immediate pre-mutation checks.

### Bridge-source provenance

- Recompute the packaged bridge source hash and compare it with the deployed
  stamp and handshake response.
- Exercise missing, malformed, stale, and mismatched provenance.
- Confirm that all mutation gateways fail closed while reads remain explicitly
  warning-bearing for repair and diagnosis.
- Inspect public-package hygiene so host records, project data, and private
  validation outputs cannot enter the packaged bridge or release bundles.

### Expected-before guards

- Verify that typed `expected_before` state is checked at the host and again in
  the bridge after target resolution and immediately before undo/mutation.
- Cover mixer, plug-in, transport, Channel Rack, pattern, Playlist, and step
  digest guards, including current-pattern and observation-scoped channel
  identity requirements.
- Confirm that omitted guards preserve the documented compatibility call shape
  without being described as concurrency protection.

### Master protection

- Confirm that mixer track 0 and a Master-source send require explicit
  `allow_master` authorization for mutations.
- Check all aliases, batch operation kinds, plan/application paths, and direct
  bridge commands for a bypass.
- Confirm that sending *to* Master retains the documented distinction and does
  not accidentally broaden source authorization.

### Ambiguous mutation outcome handling

- Force lost, malformed, delayed, and contradictory responses after a mutation
  could have reached FL Studio.
- Confirm the result is treated as unknown or unverified and does not imply
  rollback, undo, save, or artistic success.
- Verify that only independently classified read-only requests may be retried
  after ambiguous transport failures.

### Non-replay behavior

Check every mutation path, including direct setters, verified batches, plans,
option sweeps, arrangement helpers, Piano Roll dispatch, and live-note
audition. A setter may contain the bounded same-command retries needed for
known FL behavior, but the client/transport must never automatically replay a
mutating request whose response was ambiguous, and an unverified result must
not be retried for the caller.

### Batch non-atomicity

- Verify that the closed batch union is bounded (currently 1–32 operations),
  ordered, and explicitly non-atomic.
- Confirm that earlier verified changes remain when a later item fails or is
  unverified, that `stop_on_unverified` only controls later dispatch, and that
  no rollback or save is implied.
- Check session pinning, per-item receipts, Master protection, and duplicate or
  conflicting target validation.

### Plan one-shot behavior

- Trace plan creation, retrieval, application, and terminal state transitions.
- Confirm that applying a plan is a distinct destructive operation, occurs at
  most once, and does not retry a `partial` or `failed` plan automatically.
- Verify that plans and peak watches are process-local and that a restart does
  not create durable project or approval state.

### Undo evidence

- Confirm that persistent mutations request one FL undo point where the API
  supports it and compare the before/after history observation.
- Check that `undo_point_created` is `true`, `false`, or `null` according to
  observed evidence, never assumed from the request.
- Verify that transient transport/note actions report `null` and that no code
  claims guaranteed rollback or guaranteed undo.

### MIDI fragmentation and queue bounds

- Inspect frame size, fragment count, sequence validation, request/response
  correlation, expiration, queue limits, and endpoint ownership locking.
- Exercise malformed, oversized, incomplete, out-of-order, duplicate, and
  cross-request frames on TCP, file, and MIDI paths where practical.
- Confirm that production MIDI is opt-in, sandboxed CI cannot open native MIDI,
  and a write loss is never replayed.
- Verify the distinct bridge-command and MIDI-wire protocol gates and mixed
  client/bridge refusal behavior.

### File path validation

- Test absolute-path requirements, `..` components, symlinks, unsupported
  extensions, directories, empty files, oversized files, fixed recent-bounce
  roots, scan depth, and bounded result counts.
- Confirm that canonical paths and hashes are disclosed to the MCP client by
  design, with no audio samples or raw logs included in ordinary results.
- Inspect generated MIDI overwrite handling and Piano Roll script directory
  overrides for traversal or unintended replacement.

### Decoded-audio memory bounds

- Verify that file-size, duration, sample-rate/channel-width, and decoded
  memory ceilings are independent.
- Use synthetic compressed/wide/long files to confirm refusal or bounded
  truncation, including truthful `truncated_by: "decode_limit"` evidence.
- Confirm that DSP never assumes a file-size limit alone bounds decoded memory.

### Generated file replacement

- Review atomic temporary-file and replacement behavior for Type-1 MIDI and the
  generated Piano Roll script.
- Exercise existing-target refusal without explicit overwrite, parse/reopen
  verification, interruption, permission failure, and cross-device or
  symlink-like targets.
- Confirm that only the intended generated file is replaced and that user
  project/audio/preset content is never bundled or uploaded.

### Piano Roll focus and keyboard dispatch

- Confirm the prepare/manual-run/confirm handshake is required before automatic
  dispatch and is scoped to the current MCP process.
- Verify channel, pattern, and Piano Roll visibility checks, platform shortcut
  selection, serialized in-flight operations, and the distinction between
  `hotkey_dispatched` and `application_verified`.
- Test an unfocused/shared desktop, a missing script directory, and a shortcut
  failure without claiming that notes changed.

### No implicit save

Search every bridge and host workflow for save calls, including batches, plans,
arrangement preparation, option sweeps, Piano Roll operations, and acceptance
harnesses. Confirm that project dirty state and undo observations are reported
honestly and that neither successful readback nor a completed plan saves the
project.

### Public-package hygiene

- Reproduce the public-tree and package-hygiene checks from a clean checkout.
- Inspect release bundle inputs for project files, audio, presets, screenshots,
  absolute home paths, credentials, private logs, host validation records, and
  generated caches.
- Confirm that public fixtures are synthetic and pinned by the repository's
  fixture manifest, and that the bridge remains ASCII-only with no runtime
  import from the MCP process.

## Reviewer deliverables

For every observation, ask the reviewer to classify it as exactly one primary
type, with optional secondary tags:

- **Exploitable issue:** a reproducible violation that can cross a stated trust
  or safety boundary under a realistic threat model. Include impact,
  preconditions, affected revision, and a private reproduction.
- **Design limitation:** behavior that is intentional or imposed by FL Studio,
  MIDI, the MCP client, or an explicit evidence boundary. Explain the user
  consequence and whether documentation is sufficient.
- **Documentation issue:** implementation and contract disagree, or a safety
  limitation is not discoverable where a user needs it. Include an exact
  proposed wording/location.
- **Hardening suggestion:** a defense-in-depth improvement that does not
  currently demonstrate exploitability. Include the threat addressed, cost,
  compatibility impact, and test plan.
- **False-positive concern:** a check, warning, or evidence label may report
  stronger or weaker evidence than the observed behavior warrants. Include a
  synthetic fixture or fake-API reproduction.
- **Behavior requiring live FL validation:** behavior that cannot be settled by
  static inspection or the fake FL API. Record the exact FL Studio edition,
  build, MIDI API, platform/architecture, bridge revision, and a disposable
  reproduction; do not generalize one host to all hosts or plug-ins.

Each finding should also include severity/rationale, affected boundary, exact
code/document locations, a minimal reproduction, expected versus observed
result, and whether it affects read-only startup, session write authorization,
Master protection, later-idle-tick readback, no-replay behavior, no-save
behavior, evidence honesty, provenance, strict contracts, or resource bounds.

## Reproduction guidance

Use synthetic values, the repository fake FL API, and a new blank or disposable
FL Studio project. Never use a real user's project, audio, presets, screenshots,
session export, raw private logs, credentials, personal filesystem paths, or
unredacted host identifiers in a report. Close a disposable project without
saving after mutation tests, including a passing plug-in write validation.

Separate what can be tested hermetically from what needs a live host:

- Hermetic tests should cover contracts, allowlists, provenance failures,
  guard logic, MIDI framing/queue limits, path and decode bounds, generated
  file replacement, no-save static checks, and fake-FL later-tick evidence.
- Live tests should use the documented qualified FL Studio environment or
  clearly record a different environment. Start read-only, use a disposable
  project, enable writes only after an explicit present-user request, and
  retain raw acceptance output under a private ignored directory.
- A live test that cannot prove an outcome must report `false`, `null`,
  `unknown`, or another documented partial/unverified result. Do not turn
  focus, dispatch, a setter return, a changed dirty flag, or an undo request
  into proof of an artistic result.

The reviewer may run the safe suite with:

```bash
./.venv/bin/python scripts/run_safe_tests.py
```

That suite reduces implementation risk but does not provide live FL Studio
coverage, authenticated MIDI, rollback, or a guarantee about an untested
plug-in. No external review should be described as complete until a maintainer
has received and reviewed the classified deliverables.
