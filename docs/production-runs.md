# Production Runs

A Production Run is one bounded, task-scoped request for PostFader to carry
out a multi-stage production objective. The connected AI client reads the
user's chat, inspects the open project, and submits a typed request plus a
closed production plan. PostFader validates that plan, executes supported
operations in order, and returns truthful receipts for the work it attempted.

There is no permanent Autonomous Mode. A later request can use a different
scope, preservation rule, or interaction policy. PostFader is still a
bring-your-own-AI MCP server: it does not contain a language model or try to
interpret arbitrary creative chat on its own.

## From chat to a run

The AI client should preserve the user's intent when it builds the request:

- “Finish this track and make the production decisions yourself.” becomes a
  bounded run with a completion target such as a playable draft and an
  `execute_until_blocked` interaction policy.
- “Turn this into drum and bass, but preserve the vocal.” becomes a run with a
  genre/energy direction, the vocal in `preserve`, and only the requested
  musical and project changes in `allowed_changes`.
- “Only work on the second drop.” becomes a run whose scope description and
  concrete targets/categories identify that section. Represented targets and
  categories outside that scope are rejected by PostFader.
- “Mix the project, but do not change the notes or arrangement.” becomes a
  mix-scoped run that preserves note content and arrangement and rejects
  disallowed content or tempo changes.
- “Make three versions, but do not apply anything yet.” becomes one or more
  `plan_only` requests. The plans and generated material can be reviewed, but
  no write mode or project mutation is enabled.

When the user delegates sound choice, the AI should include Sound Selection in
the same run: inventory the loaded pool, plan the palette, apply it when the
request authorizes changes, then write notes using the returned role targets.
The user does not need a special Sound Selection command.

The user does not need to say any of those policy names. They are internal
run semantics inferred by the connected AI from the current request. A clear
request to change the open project supplies task-scoped authorization when the
submitted request sets `authorized_to_modify=true`; PostFader does not ask for
the old write-mode transition before every operation.

## Creation readiness and phases

For a complete creation request, PostFader performs one read-only readiness
preflight before the first mutation. The same service is available directly as
`postfader_creation_readiness`, and the normal `postfader_execute_run` path
uses it internally. It aggregates independently detectable blockers and
non-blocking limitations across the connection/bridge, Piano Roll, loaded
instrument pool, drum coverage, patterns and arrangement, mixer/effect
coverage, and manual-scope dimensions. Readiness does not enable writes,
change FL Studio, or claim that a sound was heard.

The run caches a bounded context snapshot containing the session and relevant
target fingerprints, project checkpoint, palette/preset/drum/effect digests,
Piano Roll arming receipt, bridge revisions, and preflight timestamp. It is a
same-process concurrency checkpoint, not a durable project identity. A ready
or ready-with-limitations report proceeds automatically; only a blocking
requirement stops the run, with setup actions returned together.

The executor groups work into ordered phases and does not repeat a full
project scan before each one:

| Phase | Work and evidence |
| --- | --- |
| `preflight` | Readiness, scope, capability, session, and cached-context checks; no mutation. |
| `palette` | Loaded-pool inventory, Sound Palette planning/application, preset readback, and drum-map inspection. |
| `composition` | Deterministic harmony, lead, bass, sub, and drum generation plus sound-aware adaptation. |
| `note_application` | Empty-pattern preparation and Piano Roll writes using typed palette targets. |
| `processing` | Effect coverage, semantic processing planning, and restrained loaded-effect writes when requested. |
| `finalization` | Receipt checks, timing/outcome construction, and one verified write-mode shutdown. |

Phase-specific checks still refresh the narrow session or target needed for
the next mutation. A blocked run retains completed receipts, generated
outputs, anchors, phase state, timing, and blockers for a compatible
continuation; completed receipts are never rewritten.

## MCP surface

The normal high-level flow is:

1. `postfader_validate_run` checks the request and plan without enabling
   writes or mutating FL Studio. It returns the plan digest, resolved order,
   required capabilities, expected mutation categories, and known blockers.
2. `postfader_execute_run` accepts the request and plan, validates them again,
   captures the current session fingerprint, enables the existing session write
   gate once when authorized, and executes the bounded plan.
3. `postfader_get_run` returns the current process-local state and concise
   summary.
4. `postfader_continue_run` accepts additional operations, a plan delta, or a
   replacement for the not-yet-executed remainder. Completed receipts cannot
   be rewritten.
5. `postfader_stop_run` prevents future operations. It does not undo changes
   that already completed.

Use lower-level tools for a precise one-off change. Use a Production Run when
the requested outcome spans several supported creative, arrangement, Piano
Roll, automation, or project-state steps.

After a completed run, [Creation Review](creation-review.md) can link a
caller-exported bounce to the run's stored palette, generated notes,
processing receipts, section plan, and creation outcome. A revision pass is
compiled from explicit feedback and measured evidence, then adapted back to
the same Production Run executor. It does not create a second write engine or
rewrite the completed source run.

## Supported MVP operations

The first Production Runs foundation adapts existing PostFader workflows; it
does not introduce a generic bridge command or raw FL API. Its closed
operation union supports:

- generating chord progressions, melodies, basslines, and drum parts;
- preparing a specified pattern that FL verifies is empty, or selecting a
  specified pattern;
- writing a generated or supplied `NoteSequence` through the Piano Roll
  workflow;
- applying supported Piano Roll transforms such as quantize, transpose,
  humanize, duplicate, delete, and clear;
- adding section markers;
- recording one supported automation value;
- planning and applying a Sound Palette, creating a section-scoped palette
  variation, selecting an exact loaded plug-in preset or drum kit, inspecting a
  drum map, and recording explicit local sound feedback; and
- applying an existing closed verified batch of supported mixer, channel,
  pattern, Playlist-track metadata/state, tempo, routing, and loaded plug-in
  parameter changes.

Generated sequences and sound palettes are structured outputs, not opaque
Python objects. The sound operations are:

| Operation | Result and use |
| --- | --- |
| `plan_sound_palette` | Read-only `sound_palette`, `palette_assignment`, and `generator_target` outputs. Planning does not change FL or history. |
| `apply_sound_palette` | Authorized, verified application of a prior palette; returns a `sound_palette` state and per-role receipts. |
| `create_sound_palette_variation` | Read-only `section_variation` plus changed `palette_assignment`/`generator_target` outputs; anchors remain unchanged unless explicitly replaced. |
| `select_plugin_preset` | Exact `selected_preset` receipt for a loaded generator or mixer effect. |
| `select_drum_kit` | Exact `selected_preset`, followed by a `drum_map` read and required-role validation. |
| `inspect_drum_map` | Read-only `drum_map` output from a loaded target. |
| `record_sound_feedback` | Explicit local accepted, rejected, or neutral feedback; no FL mutation. |

A later operation may refer to an earlier output by operation ID when the
reference type is compatible. `write_note_sequence.channel_index` accepts a
`palette_assignment` or `generator_target` reference, and
`generate_drums.drum_map` accepts a `drum_map` reference. Drum-map and target
references can also feed the drum inspection/selection operations. References
must point backward to an earlier operation, and the complete plan is
validated before any project mutation. Creation Review start operations also
publish bounded `section_definition` items; revision planning may reference
those items to add the exact source section ID to its validated scope.

## Execution and receipts

Plans are bounded by operation and iteration limits. They are ordered and
non-atomic: a verified earlier operation remains applied if a later operation
fails or is unverified. PostFader stops on an ambiguous or unverified mutation
unless the operation's existing contract defines a safe continuation. It does
not automatically replay an ambiguous mutation, claim rollback, or claim a
change that FL Studio did not expose evidence for.

Each run records its request, plan digest, session fingerprint, available
project-summary checkpoint, status, operation index, generated outputs,
per-operation receipt, blockers, warnings, and final summary. The project
checkpoint covers metadata, counts, tempo, non-volatile transport state, dirty
state, and undo-history coordinates that FL exposes; it is not a full project
content hash. A blocked run can be continued only after the connected AI
submits a compatible continuation or replacement remainder and the session and
project checks still pass.

Creation runs additionally return `readiness_report`, `run_context`,
`phase_plan`, `timing_report`, and `creation_outcome`. Timing is local and
diagnostic only: phase durations, operation count, target refreshes, full
inventory scans, preset navigation/enumeration, Piano Roll dispatches, manual
waits, blocked time, and write-mode transitions are bounded in the receipt.
Soft timing targets produce warnings rather than failures, and no timing data
is uploaded.

`creation_outcome` keeps technical execution, arrangement delivery, processing,
audible quality, and manual handoff separate. In particular,
`audible_quality.status` remains `not_evaluated` until the user confirms a
draft or supplies an exported/recorded bounce for the audio tools. A dry or
partially processed result is therefore not a claim about audible quality.

## Scope and capability checks

Scope is enforced server-side. A run can constrain sections through concrete
targets, mixer/channel targets, allowed mutation categories, and preserved
material. Free-text descriptions and named elements remain descriptive until
the request identifies concrete targets. For example, `mix_only` rejects note,
pattern-content, tempo, or arrangement operations unless the submitted scope
explicitly adds those categories; a preserved vocal channel cannot be targeted
by a destructive operation; and a track-scoped run cannot write another mixer
track. A `selected_targets` run must explicitly add global tempo or marker
changes because those changes cannot be confined to one target. Capability
validation also distinguishes malformed plans, operations
unsupported by PostFader, operations not exposed by the current FL Studio API,
unavailable project targets, and temporary setup or session blockers.

Production Runs reuse the existing bridge provenance, session-fingerprint,
write-mode, verified-writer, creative, Piano Roll, and verified-batch
boundaries. A `plan_only` run never enables writes. An authorized mutating run
enables the in-memory write gate once and carries the captured session
fingerprint through its operations; a session reload or changed target stops
the run before the next mutation.

Sound Palette application revalidates the loaded target and session before
each exact preset sequence. It applies assignments in deterministic role order,
records only successful verified assignments in local Sound Selection history,
and keeps completed receipts immutable. A failed or unverified preset selection
stops dependent operations; the run never retries an ambiguous mutation or
claims rollback. Continuations should reference the existing palette and use a
variation request so anchors remain stable across sections.

When processing is requested, the run evaluates effect coverage before writing
parameters. A semantic action is eligible only when a loaded target, Atlas
capability evidence, a compatible adapter, and runtime control evidence agree.
The plan prefers displayed-value or exact-option setters and the executor uses
the existing later-tick readback boundary. Missing effects or unresolved
controls remain visible as `dry_missing_effects` or partial processing; an
Atlas product by itself never creates a processing target.

## Process-local lifetime

Run state is held in a bounded, thread-safe registry in the PostFader MCP
process. Older terminal records may be evicted deterministically when the
registry reaches its limit. Runs are not stored in a database, do not contain
credentials, and do not survive an MCP process restart. If `postfader_get_run`
cannot find an ID, it reports that the run may belong to a previous process or
may have expired from the bounded registry.

PostFader changes the open project but does not save it automatically. Save a
version manually in FL Studio after reviewing the receipts and warnings.

Sound Selection chooses only from generators and effects already loaded in the
current project. Atlas-only products can be recommended but cannot become run
assignments, and PostFader cannot insert, remove, or replace plug-ins through
the supported backend. Loop Starter is a separate explicit source strategy;
its reroll has dispatch-only identity and is not a verified palette assignment.

## FL Studio boundaries

Production Runs cannot add capabilities that FL Studio's public MIDI scripting
API does not expose. In particular, the MVP does not create, move, or delete
Playlist clips; render or save a project; insert, remove, or reorder plug-ins;
read live audio buffers; or edit/read automation-clip points.

The closed plan schema includes validation-only markers for Playlist clip
creation, rendering, saving, and plug-in insertion. They always produce a
structured `unsupported_by_fl_studio` or `unsupported_by_postfader` blocker;
the executor never dispatches them.

Piano Roll writes and transforms use FL Studio's separate `.pyscript` runtime.
The one-time PostFader Apply setup must be completed before an automatic run
can dispatch the operation. The controller bridge can verify the selected
channel and pattern and report hotkey delivery, but it cannot read the score
back; `application_verified` therefore remains false. If setup is missing, the
run reports one concise setup blocker and does not repeat it for every note
operation.

Marker names can be read back, but marker times cannot. Automation helpers can
verify the controlled value and capture conditions, but not the new automation
point. A send level requires an existing route. Plug-in operations target
already loaded, supported parameters; unprofiled controls remain unsafe to
modify. Rendering, project saving, plug-in insertion, Playlist clip creation,
and live-audio claims remain explicitly unsupported.

## Maintainer live acceptance

Use [the live creation acceptance harness](../scripts/live_creation_acceptance.py)
with a blank disposable project, not a production song. Prepare the loaded
generator pool, a drum generator with kick/snare/closed-hat mappings, an empty
Pattern 1, and the armed Postfader Apply Piano Roll bridge first. The
`composition` scenario leaves the effect chain empty and checks an honest dry
processing status. The `production` scenario expects supported loaded stock
effects with existing adapters and checks semantic planning, displayed-value
application/readback, and restrained processing. Both scenarios use one
task-scoped authorization, one run, and one consolidated receipt.

Preview without MCP or FL contact:

```bash
python scripts/live_creation_acceptance.py --plan --scenario composition
python scripts/live_creation_acceptance.py --plan --scenario production
```

For a live fixture, pass all three explicit confirmations and a new evidence
path outside the repository:

```bash
python scripts/live_creation_acceptance.py \
  --scenario composition \
  --confirm-user-present \
  --confirm-disposable-project \
  --confirm-safe-to-edit \
  --output /absolute/private/creation-composition.json
```

The harness records observed elapsed time and receipts but labels acceptance
targets as `not_claimed`; it never treats a fast run, a technical receipt, or
metadata as proof of musical or audible quality. Keep project files,
screenshots, logs, and timing evidence outside the public checkout. See
[Creation Pipeline](creation-pipeline.md) for the detailed contracts and
[Setup](setup.md) for the armed-ready checklist.

The acceptance targets are under five minutes from an armed-ready state for a
modest 32-bar draft, under ten minutes when one documented manual setup action
is needed, one task-scoped authorization, no surprise setup blockers that the
preflight could have detected, and at most one manual Playlist handoff. These
are targets for a recorded live run, not claims about this checkout.

See [FL Studio constraints](fl-constraints.md), [Architecture](architecture.md),
and [Tool contracts](tool-contracts.md) for the lower-level evidence and
limitations behind these run results.
