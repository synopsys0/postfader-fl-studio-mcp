# Tool-surface evaluation

> Historical snapshot: this playbook records the v0.20 surface. For the
> current `dev` contract and counts, use [Tool and command reference](tool-contracts.md).

PostFader v0.20 exposes 111 MCP tools and 8 live resources. This document is a
maintainer and early-user playbook for collecting real compatibility evidence
about that surface. It does not propose an immediate redesign, tool removal,
profile rollout, telemetry, or a silent change to the default surface.

The authoritative contract remains [Tool and command reference](tool-contracts.md)
and the implementation registration remains
`fl_studio_mcp/mcp_server.py`. When a count or behavior changes, update those
sources first and then refresh this evaluation guide. Do not infer a new tool
count from a model transcript alone.

## Current surface to evaluate

The current contract groups the 111 tools as follows:

| Surface | Count | What it represents | Typical evidence source |
| --- | ---: | --- | --- |
| Read-only tools | 50 | Project, transport, mixer, plug-in, preset, pad-map, Plugin Atlas, Channel Rack, pattern, Playlist, history, audio, Sound Selection, and non-mutating workflow observations | SDK listing, fake FL, or live read acceptance |
| Directly guarded FL setters | 39 | Narrow mixer, plug-in, transport, Channel Rack, pattern, Playlist, sequencer, and local MIDI mutations with independently checkable preconditions | Contract tests, fake FL, then disposable live write acceptance |
| Specialized mutating workflows | 12 | Preset selection, batch, Production Run, Sound Selection application, Piano Roll, arrangement, and automation workflows whose verification or restore boundary needs dedicated evidence | Deterministic workflow tests and focused disposable-project acceptance |
| Non-destructive workflow/dispatch tools | 8 | Note audition, peak watches, plans, explicit Sound Selection feedback, and other process-local preparation or dispatch surfaces that do not delete data or apply a persistent project mutation | Deterministic workflow tests and response evidence |
| Idempotent destructive controls | 2 | `fl_set_write_mode`, the session write-capability transition, and `sound_selection_history_reset`, which deletes only confirmed local history | Capability-handshake and explicit local-history reset tests |

The category labels are evaluation aids, not a second API taxonomy. Some tools
have a nuanced evidence boundary: an arrangement marker or automation receipt
can contain later-tick observations while remaining aggregate-unverified, a
Piano Roll shortcut can be dispatched without note readback, and a verified
batch is ordered but non-atomic. Preserve those distinctions when reporting
tool-selection behavior.

The eight resources are:

```text
fl://capabilities  fl://status       fl://project       fl://transport
fl://mixer         fl://channels     fl://plugins       fl://patterns
```

They use the same compatibility gates and contracts as their corresponding
reads; resources do not bypass read-only mode. A client that does not expose
resources is a client-integration limitation, not evidence that the resources
are absent from the server.

## Evaluation goals

The purpose of an evaluation is to determine whether a real MCP client and
model can discover and select the existing surface safely and predictably. A
report should answer:

1. What user goal was expressed, in sanitized terms?
2. What tools/resources did the client expose?
3. What did the model select, in what order, and with which arguments?
4. What did the server return, including warnings, `verified`, partial, or
   refusal evidence?
5. What selection would a careful producer expect, and why?
6. Did the behavior cross a safety boundary, or is it a usability/compatibility
   observation with no unsafe mutation?

Run a read-only reproduction first. If a write is necessary, use a new blank or
disposable project, explicitly request session write mode, and use a supported
setter that can be read back. Do not test selection behavior by mutating a
valuable project. Disable writes again and close a disposable project without
saving.

PostFader has no telemetry and must not gain automatic transcript collection or
background analytics for this evaluation. Evidence is opt-in, sanitized, and
submitted by a maintainer or user who chooses to report it.

## What to report

Use the following fields in an issue, review note, or private maintainer
record. Keep the report generic enough to avoid project disclosure.

### Environment and exposure

- PostFader version and commit, if available.
- MCP client/host and version.
- Model/provider, only if relevant to tool selection; do not include prompts
  or account identifiers.
- Operating system and architecture, Python version, FL Studio edition/build,
  and virtual MIDI provider when a live bridge was involved.
- Whether the run was hermetic fake-FL, live read-only, disposable live write,
  or a pure client schema/listing check.
- The number and names of tools/resources the client actually exposed. If the
  client truncated or filtered the list, record that as the observation.

### Sanitized request and trace

- A short producer-facing goal, such as “inspect the vocal bus before changing
  its fader” or “prepare a MIDI file from a deterministic melody.”
- The selected tool/resource sequence and high-level argument shapes. Remove
  project titles, track names, plug-in names if sensitive, values that identify
  a session, absolute paths, prompts, credentials, and raw model transcripts.
- Whether the client made reads before a mutation, whether it requested
  `fl_set_write_mode`, whether `confirm_user_present` was supplied, and whether
  a Master target was explicitly authorized.
- The relevant response evidence: `verified`, per-field proof,
  `verification_basis`, warnings, refusal type, plan state, and whether the
  result was partial or unverified.
- The expected safe behavior and the observed behavior.

### Safety and privacy checklist

Do not attach project files, audio, presets, raw private logs, credentials,
personal filesystem paths, screenshots containing private project information,
or unredacted client configuration. Do not paste full MCP transcripts when a
short sanitized tool sequence is enough. A generated plug-in report should be
reviewed before sharing; its reducer is designed to omit current values,
display strings, parameter names, locations, paths, project metadata, and
timestamps, but the issue author remains responsible for the final text.

If a report suggests a security issue, stop testing writes and use the private
vulnerability-reporting path described in [SECURITY.md](../SECURITY.md). This
document is not a replacement for that process.

## Evaluation cases

The cases below cover the expected failure modes. A report may cover more than
one case, but should name each applicable case explicitly.

### Wrong tool selected

Report when the model selects a tool whose target, evidence, or mutation scope
does not fit the expressed goal. Examples include choosing a normalized setter
when the user supplied a plug-in display value, using a mixer effect target for
a Channel Rack generator, or using a mutating tool for a read-only question.

Record the user goal, the candidate tools exposed, the selected tool, why it
was a mismatch, whether it dispatched, and the safer existing tool. If no
mutation occurred, classify it as a selection/usability observation rather than
an exploit. If a mutation did occur, include the write-mode, Master, guard, and
readback evidence.

### Workflow tool ignored in favor of low-level setters

Report when a request clearly matches an existing bounded workflow—such as Mix
Doctor recommendations, a gain-staging plan, `mix_create_plan`,
`arrangement_prepare_pattern`, or a deterministic composition tool—but the
model repeatedly constructs low-level operations instead.

Include whether the workflow was listed and its schema was available, the
low-level sequence chosen, whether the workflow would have preserved a stronger
evidence boundary, and whether any operation was applied. Do not treat a
workflow preference as a security defect unless it bypassed a documented gate.

### Repeated tool-selection loops

Report repeated calls that do not make progress: the same tool/arguments, a
cycle among equivalent reads, repeated retries after a refusal, or a plan that
is repeatedly recreated after a terminal outcome. Include a short sequence
with repetition count and the server's responses.

The server's safety rule still applies: a mutating call with an ambiguous
outcome must not be replayed automatically. If a client loop attempts such a
replay, stop and report the client/model behavior without allowing it on a
valuable project.

### Schema overload

Report when a client or model cannot reliably distinguish strict arguments,
discriminated targets, optional guards, bounded arrays, or evidence fields.
Examples include confusing `track_index`/`slot_index` with a
`channel_generator` target, treating unknown fields as accepted, sending a
step update without its required digest, or interpreting `verified: false` as
an exception.

Include the smallest sanitized schema fragment and the exact validation/refusal
observed. Do not “fix” overload by weakening contracts, accepting unknown
fields, widening bounds, or hiding partial evidence.

### Client fails to expose all tools

Record the server SDK listing and the client's displayed listing separately.
Check whether the client imposes a count/token limit, filters destructive tools,
does not support resources, or failed to start the server. Attach the output of
an explicit tool-listing check only after removing paths, environment values,
and private metadata.

The expected server values are 111 tools and 8 resources for v0.20. A client
showing fewer is not evidence that the repository should silently change its
default surface. Escalate client limits or MCP SDK compatibility separately.

### Model writes before sufficient reads

Report when a model attempts a state mutation without first obtaining the
target identity, relevant current value, applicable session/expected state, or
required step digest. Note that some setters intentionally preserve a legacy
call shape with optional guards, but omission is not concurrency protection.

The report should state which read would establish the missing context and
whether the server refused, required write-mode confirmation, rejected a stale
guard, or allowed a write. Never solve this observation by adding automatic
confirmation, replay, rollback, or hidden reads that make a mutation appear
safer than its contract.

### Plan/apply confusion

Report when the model treats a created plan as already applied, applies a plan
without an explicit user decision, calls `mix_apply_plan` on a `partial` or
`failed` plan, or assumes a plan can be retried safely after a terminal result.

Include the plan lifecycle (`draft`, `applied`, `partial`, or `failed`), the
selected tools, and the evidence returned. Plan application remains a distinct
destructive, one-shot operation; no tool-selection experiment should weaken
that separation.

### Direct setter versus batch confusion

Report when a model uses `fl_apply_verified_batch` for an operation that needs a
single direct setter, or emits a long low-level sequence where a bounded batch
would make ordering and receipts clearer. Include operation count, target
types, `stop_on_unverified`, and per-item outcomes.

Remember that a batch is ordered and non-atomic. Earlier verified changes stay
applied when a later item fails or is unverified. A batch is not a transaction,
does not save the project, and must never be used as an implied rollback.

### Composition versus Piano Roll confusion

Report when the model confuses deterministic offline note generation and
Type-1 MIDI export with the separate live Piano Roll script workflow, or treats
Piano Roll hotkey dispatch as note readback. Include whether the request was to
create a file, prepare a pattern, modify live score notes, or merely analyze
audio.

The safe distinction is:

- composition tools generate bounded, deterministic note sequences without
  touching FL;
- `midi_export_type1` writes and reopens an explicitly requested MIDI file;
- Piano Roll tools require their setup handshake and can report focus/dispatch,
  but `application_verified` remains false because the controller bridge has no
  score-note getter.

### Resource discovery problems

Report when a client cannot list or read `fl://capabilities`, `fl://status`,
`fl://project`, `fl://transport`, `fl://mixer`, `fl://channels`,
`fl://plugins`, or `fl://patterns`, or when it treats a resource as a write
surface. Record the URI, listing/read result, and whether the corresponding
tool worked.

Resources are read-only and use the same compatibility/provenance gates as
their tools. A resource error may be a client capability problem, bridge
availability problem, or contract problem; do not infer which without the
sanitized response.

## Maintainer evaluation workflow

1. **Reproduce hermetically first.** Use the fake FL API, synthetic audio, and
   the SDK/listing tests. Pin the PostFader revision and client version.
2. **Classify the observation.** Separate client/model selection, schema
   usability, documented design limitations, server defects, and security
   boundary violations.
3. **Check the contract.** Compare the trace with
   `docs/tool-contracts.md`, `docs/architecture.md`, and `SECURITY.md`. Treat
   warnings and partial evidence as behavior to preserve, not noise to hide.
4. **Use a disposable live project only if needed.** Start read-only, request
   writes explicitly, avoid recording, test one supported change, capture
   later-tick evidence, disable writes, and close without saving.
5. **Add a focused regression test or documentation correction.** A client
   compatibility report alone is not a reason to expand the tool count.
6. **Decide whether profile evidence exists.** Do not add a profile merely
   because one model chose poorly; require repeated, reproducible evidence
   across supported clients and a design review for any default change.

Suggested sanitized report template:

```text
PostFader version/commit:
Client/host and version:
Model/provider (if relevant):
Platform / Python / FL Studio / MIDI provider (if live):
Run type: hermetic | live read-only | disposable live write | listing-only

Goal (sanitized):
Tools/resources exposed by the client:
Observed sequence:
Expected safer sequence:
Relevant evidence/refusal/verification basis:
Safety boundary touched: none | read-only | write mode | Master | evidence | other
Reproduction steps using synthetic/disposable data:
Privacy review complete: yes/no
Primary classification: selection | schema | client compatibility | design limitation |
  documentation | hardening | security issue | live-FL validation needed
```

## Potential future profiles

The following names are recorded for evaluation only:

- `core` — a small read-first discovery surface for clients that cannot handle
  the complete catalog;
- `mixing` — mixer, plug-in inspection, audio analysis, Mix Doctor, peak, and
  plan workflows;
- `composition` — deterministic composition, MIDI, audio music analysis,
  pattern preparation, and Piano Roll workflows;
- `full` — the current complete surface.

No profile is implemented or selected by default today. A future profile
design must answer whether profiles are server-side registration, client-side
filtering, or an explicit user configuration; preserve strict contracts and
all safety gates; define how resources are exposed; and define how a client
discovers a temporarily unavailable tool. It must not silently change the
default tool surface in a patch release, hide write tools in a way that makes
the safety model ambiguous, or claim that a profile is supported without
compatibility evidence.

Evidence sufficient to consider profiles should include repeated sanitized
reports across more than one MCP client/model, a measurable discovery or
selection problem, a reviewed mapping that does not duplicate or contradict
tool contracts, and hermetic tests for each profile's exact list and safety
annotations. Do not add telemetry to gather that evidence. Until those inputs
exist, keep the v0.20 default at 111 tools and 8 resources and improve guidance,
schemas, or client-specific documentation instead.

## What this guide does not claim

- It does not claim that every model will choose the ideal tool.
- It does not claim that a client exposing all 111 tools can fit them into every
  model context window.
- It does not claim that tool selection proves a live FL Studio mutation,
  audible quality, undo point, rollback, or project save.
- It does not claim that a report from one client generalizes to all clients,
  models, plug-ins, FL Studio builds, or virtual MIDI providers.
- It does not add a hosted service, telemetry, or automatic analytics.
