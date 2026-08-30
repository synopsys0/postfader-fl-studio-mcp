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

The user does not need to say any of those policy names. They are internal
run semantics inferred by the connected AI from the current request. A clear
request to change the open project supplies task-scoped authorization when the
submitted request sets `authorized_to_modify=true`; PostFader does not ask for
the old write-mode transition before every operation.

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
- recording one supported automation value; and
- applying an existing closed verified batch of supported mixer, channel,
  pattern, Playlist-track metadata/state, tempo, routing, and loaded plug-in
  parameter changes.

Generated sequences are structured outputs, not opaque Python objects. A later
operation may refer to an earlier operation's output by operation ID when the
reference type is compatible. References must point backward to an earlier
operation, and the complete plan is validated before any project mutation.

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

## Process-local lifetime

Run state is held in a bounded, thread-safe registry in the PostFader MCP
process. Older terminal records may be evicted deterministically when the
registry reaches its limit. Runs are not stored in a database, do not contain
credentials, and do not survive an MCP process restart. If `postfader_get_run`
cannot find an ID, it reports that the run may belong to a previous process or
may have expired from the bounded registry.

PostFader changes the open project but does not save it automatically. Save a
version manually in FL Studio after reviewing the receipts and warnings.

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

See [FL Studio constraints](fl-constraints.md), [Architecture](architecture.md),
and [Tool contracts](tool-contracts.md) for the lower-level evidence and
limitations behind these run results.
