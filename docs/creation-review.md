# Creation Review, Revision, and Delivery

Creation Review connects a completed Production Run to one or more bounces that
the user exported from FL Studio. It measures the supplied audio, keeps direct
producer feedback as the highest-authority taste signal, compiles one bounded
revision, compares the next bounce, and creates a precise delivery handoff.
PostFader does not render, save, or listen to the live project, and it does not
contain a language or audio model.

## Review Sessions

A Review Session retains the source run snapshot, its independent creation
outcomes, generated note outputs, Sound Palette and anchors, processing
receipts, section and pattern intent, attached asset metadata, evaluations,
explicit feedback and locks, revision passes, comparisons, and delivery
manifests. Completed source-run receipts are copied as immutable evidence; a
review never rewrites them.

Sessions are bounded and process-local unless `persist_session=true`. Persistent
state is schema-versioned JSON under:

```text
<FL Studio user-data>/Settings/PostFader/creation-review-sessions-v1.json
```

`POSTFADER_CREATION_REVIEW_PATH` (or the compatibility alias
`POSTFADER_CREATION_REVIEW_SESSIONS_PATH`) may select another absolute path.
Writes are atomic and thread-safe, and a private per-path advisory lock
serializes writers across MCP processes. Corrupt input is reported and left
untouched; reset or deletion is explicit. `persist_session=false` does not
write the session to this store. The durable serializer removes credentials,
prompts, transcripts, encoded or raw audio, cloud identifiers, and arbitrary
private paths from durable metadata. It enforces bounded sessions, assets,
findings, feedback, evaluations, comparisons, and manifests with deterministic
pruning; findings referenced by retained plans or passes are kept ahead of
lower-ranked unreferenced findings. With `persist_asset_paths=false`, durable
state keeps asset hashes and labels but omits absolute paths. With
`persist_asset_paths=true`, only canonical attached `ReviewAudioAsset.path`
fields may be retained; arbitrary metadata paths are still removed.
The request defaults to three revision passes (hard cap eight); the store caps
64 sessions, 256 assets, 256 findings per evaluation, 32 evaluations, 64
comparisons, 32 delivery manifests, and a 16 MiB serialized document. The public
`postfader_review_delete` requires explicit confirmation; store reset/repair is
also explicit and never runs automatically.

The lifecycle is bounded: create or await assets, evaluate, plan and apply a
revision, await a new bounce, compare, then accept, reject, stop, or complete.
The default is at most three revision passes.

## Bounce workflow

The normal workflow is:

1. Start a session from a completed Production Run.
2. Attach an explicit full-mix path and, only when useful, a reference, stem,
   or section bounce.
3. Evaluate the full mix globally and against known sections.
4. Record structured producer feedback and independent accepted-element locks.
5. Plan and apply one bounded revision through the existing Production Run
   executor.
6. Export one revised full mix with matching settings.
7. Compare before and after, then accept, revise again, or deliver.

Evaluation and revision can occur during one connected-AI turn after a clear
request to improve the project. The revision uses one readiness preflight and
one task-scoped write authorization; individual findings, operations, and
readbacks do not create confirmation loops. A request to analyze only never
enables writes.

## Public tools and Production Run operations

The direct MCP surface has 13 tools. They are task-scoped calls, not a
persistent autonomous mode:

An earlier analyze-only turn does not authorize a later write, but it also
does not permanently freeze the Review Session. A later explicit request to
revise may reuse the retained assets and evidence; the mutating call and its
`RevisionRequest` must both carry that new task-scoped authorization.

| Tool | Use |
| --- | --- |
| `postfader_review_start` | Start a Review Session from one completed Production Run. |
| `postfader_review_attach_assets` | Validate and attach explicit full-mix, reference, stem, or section audio. |
| `postfader_review_evaluate` | Measure an attached bounce globally and by the known section map. |
| `postfader_review_get` | Read retained state, evidence, lifecycle status, and the next action. |
| `postfader_review_compare` | Compare distinct aligned before/after bounces without implying approval. |
| `postfader_review_plan_revision` | Compile and validate a closed, traceable revision plan. |
| `postfader_delivery_manifest` | Build the current read-only multi-dimensional delivery view. |
| `postfader_review_export_handoff` | Request one exact next full-mix export and only necessary stems. |
| `postfader_review_apply_revision` | Apply one validated revision through the existing Production Run executor. |
| `postfader_review_record_feedback` | Store explicit producer feedback and independent accepted-element locks. |
| `postfader_review_stop` | Stop future Review Session work without undoing completed changes. |
| `postfader_review_delete` | Delete Review Session metadata after explicit confirmation. |
| `postfader_delivery_export_manifest` | Create JSON and/or Markdown delivery files without overwriting or saving FL. |

The same workflow can be expressed inside one typed Production Run through 9
closed operations: `start_review_session`, `attach_review_assets`,
`evaluate_creation`, `record_creation_feedback`, `plan_creation_revision`,
`apply_creation_revision`, `compare_revision_bounces`,
`create_playlist_handoff`, and `create_delivery_manifest`. The first, second,
third, fourth, fifth, seventh, eighth, and ninth are local/read-only workflow
state or evidence operations; `apply_creation_revision` is the single
verification-gated mutating operation. All nine remain bounded by the run's
operation/pass limits and preserve typed output references. Starting a review
also exposes each retained authoritative section as a typed
`section_definition` item, so a later revision can scope itself by section ID
without reconstructing the boundary from prose.

## Assets and alignment

Supported assets are candidate, before, after, and reference full mixes;
instrumental, vocal, drum, bass, chord, lead, and generic role stems; section
bounces; and synchronized combinations of those files. Inputs must be explicit
caller-selected paths or exact results from PostFader's bounded recent-audio
listing. Directories, unsupported containers, oversized or changing files,
unsafe symlink resolution, duplicate before/after files, and stale hashes are
rejected.

Duration similarity is not treated as synchronization proof. The asset set
records start confidence, duration and sample-rate compatibility, declared
offsets, and limitations. Each unique digest is decoded once for an evaluation.
Derived features are cached by file digest, analyzer version, section-map
digest, and analysis-policy digest.

## Sections and measurements

Section mapping prefers explicit user ranges, then the source Production Run,
section markers, Playlist handoff, and finally bounded detected suggestions.
Detected boundaries are never silently authoritative. A retained source run's
`run_context.project_checkpoint.transport` can provide a documented tempo-change
map; supported points are bounded, normalized
to ordered unique numeric start bars (including fractional positions), with
positive BPM. Otherwise the map uses scalar constant tempo and time signature.
The live FL `ProjectSummary` normally exposes only scalar transport, so a tempo
map is used only when it survives in
the source context or is supplied by the caller. Unsupported or malformed maps
(for example, missing or duplicate points, nonnumeric bars, or invalid BPM) are
not guessed and can block map construction. Bar/time conversion accounts for
time signature and declared
export offset; fractional bar positions, fractional tempo-change boundaries,
and marker beat offsets expressed in denominator beats are supported in the
conversion, while canonical
section and export-handoff bar fields stay integer. Gaps, overlaps, lead
silence, and tail regions remain visible.

PostFader reuses its decoded-audio analyzers for peak and true-peak estimates,
clipping, loudness, RMS body, crest factor, dynamics, DC offset, spectral-band
shares, sub-40 Hz share, low-mid concentration, brightness, sibilance-region
energy, stereo width and correlation, noise floor, silence, duration, and
channel validity. Bounded section analysis adds energy, spectral, stereo,
transient/onset, silence, and sustained-tail measurements with coverage and
confidence.

Arrangement observations are explicitly proxies: intro/build movement,
build-to-drop contrast, drop impact, relief and return, Drop A/Drop B
similarity, density, brightness, low-end, stereo movement, and ending behavior.
Measurements do not prove that a section is exciting, emotional, or good.
Stored PostFader-generated notes may also be compared for count, density,
register, range, velocity, polyphony, rhythmic occupancy, motif overlap,
harmonic agreement, and section development. Arbitrary user-created Piano Roll
content is not fabricated.

Synchronized stems permit bounded masking and role-energy findings. A full mix
alone never attributes a problem to a specific channel. A user-supplied
reference is loudness-matched directional evidence only, restricted to the
requested tonal, dynamic, stereo, contrast, transient, loudness, low-end, or
energy-curve dimensions. Reference section metrics are opt-in: each
`reference_section_pairs` entry must explicitly name both the reference and
candidate section and provide usable start/end seconds. IDs alone, names or
ordering do not infer a pairing, and unpaired sections receive no section
delta. Alignment or loudness-matching failure withholds the comparison.
`reference_goals` on the session request selects requested dimensions.
Reference pairs may be retained on `ReviewSessionRequest` or supplied for one
evaluation through `ReviewEvaluateRequest`; the closed Production Run
`evaluate_creation` operation carries the same typed rows. Explicit review
section ranges accept a complete bar pair, a complete time pair, or both. A
reference is never a melody, progression, or arrangement copy target.

## Findings, feedback, and locks

Every finding exposes its evidence source: decoded measurement, synchronized
stem, reference comparison, source receipt, palette metadata, explicit user
feedback, or connected-AI interpretation. Priorities are bounded and rank
direct feedback first, followed by critical technical failures, verified
delivery failures, stem evidence, high-confidence full-mix evidence, weaker
proxies, and model interpretation.

Goals are classified as `technically_evaluable`, `proxy_evaluable`,
`requires_user_judgment`, or `not_evaluable_from_supplied_assets`. Goal rows are
derived, bounded, and deduplicated from the source brief, source goals or
objectives, requested focus, and `reference_goals`; the report retains each
row's origin, evidence, rationale, and required additional evidence in
`goal_evaluations` and its summary counts. “No clipping” can be measured. “The
drop is larger” can have a disclosed proxy. “The lead feels emotional” still
requires the producer. These states are evidence labels, not fabricated
objective pass/fail or artistic approval; missing analyses can leave the report
partial.

Structured feedback may target the song, section, palette role or exact
assignment, generated part, kit or drum role, processing goal, arrangement, or
manual delivery. Independent locks protect sound assignment, notes, rhythm,
register, processing, level, section placement, and role identity. This allows
“keep the melody, change the sound” and “keep the sound, rewrite the melody” to
mean different things. Only an explicit later directive releases a lock;
silence and broad ambiguity are not acceptance.

## One-pass revisions

A Revision Plan is closed, bounded, ordered, and traceable. Every operation
identifies its finding or feedback objective, section and role, preserved
elements, expected measurable movement, subjective objective, confidence,
fallback, and verification method. Validation checks the source session,
evaluation and run snapshot, backward references, accepted locks, section and
role scope, stored note digests, palette continuity, live targets, Piano Roll
arming, loaded semantic controls, change/pass budgets, and unsupported FL
operations before the first project mutation.

The plan is bound to a canonical digest of the complete `RevisionRequest`
except `authorized_to_modify`; authorization is asserted afresh at the
mutating boundary, so a later explicit task-scoped authorization may apply the
same otherwise-unchanged plan.

The revision schema is a closed union, but a schema entry is not automatically
a built-in FL capability. The default executor has adapters for local
`record_feedback_lock` and `create_playlist_handoff_delta`; deterministic
stored-`NoteSequence` replacement for `transform_generated_sequence`,
`create_section_note_variation`, and the section density/register/voicing/
rhythm/velocity/articulation operations; palette or drum selection for
`change_sound_assignment`, `create_sound_palette_variation`, and
`change_drum_kit`; resolved `adjust_role_level` and `adjust_channel_mix`;
resolved `apply_semantic_processing` and `replace_processing_plan`; and
`update_section_markers`. The stored-note adapter starts from a stored PostFader
`NoteSequence` and supports bounded transpose/semitone and octave shifts,
density, note length/articulation, velocity, register clamping, rhythmic
displacement, quantize/humanize/duplicate transforms, and voicing inversion.
It writes a replacement to an explicit channel/pattern target.

`regenerate_role_sequence`, `change_drum_role_mapping`,
`add_supporting_layer`, `remove_generated_layer`, and any other closed operation
not listed above require a source-specific
`RevisionExecutionContext.operation_adapters[operation_id]` that returns
existing Production Run operation(s). A missing or failed adapter blocks apply
before FL dispatch; source-specific adapters may also be required when a target
or processing context cannot be resolved. The default executor does not read
arbitrary Piano Roll content or generate fills, countermelodies, bass-rhythm
variations, or generator-style motif-preserving regeneration. Those outcomes
require a supplied stored output or source-specific adapter. Transforming
existing identity is preferred unless replacement was requested or required.

Typical blockers are a missing source run or evaluation, stale or unavailable
stored-sequence or palette digests, unresolved channel/pattern/mixer/effect
targets, absent processing controls, an unarmed Piano Roll, stale session or
target fingerprints, violated locks/scopes, exceeded operation/pass budgets,
unauthorized Master targeting, and unsupported or unadapted closed operations.
Preflight or adapter blockers
produce no FL mutation for that apply; a runtime unknown or blocking unverified
outcome can stop after earlier verified writes, which remain in the receipts
without replay or rollback. A Playlist handoff remains a manual, non-FL-mutating
artifact.

Application compiles to the existing Production Run operations and verified
writers. One cached revision context, one live session fingerprint, one
preflight, one authorization, phase checks, target-specific refreshes, batched
compatible writes, and later-tick readback are used per pass. Unknown or
blocking unverified outcomes stop dependent work without replay, rollback, or
receipt rewriting. Successful passes enter `awaiting_rebounce`, and write mode
is shut down automatically. If the FL mutation completes but the Review
Session store fails afterward, the result is `blocked` with a process-local
receipt and an explicit no-replay blocker; the mutation is never retried.

## Comparison and acceptance

Before/after comparison requires distinct digests, compatible channels and
section ranges, a common export start or declared offset, and usable alignment.
It reports global, section, and supported-stem deltas for peaks, loudness,
dynamics, spectral and low-end balance, stereo behavior, transient density,
section contrast, Drop A/Drop B differentiation, masking, and the revision's
expected objectives. Each objective moved toward, moved away, stayed unchanged,
was not measurable, or lacked evidence.

An `after_full_mix` asset must declare the revision-pass ID produced by its
bounce, and that pass must belong to this Review Session and source run. The
pass checkpoint advances from `attached` to `compared` only when the comparison
is recorded; an unbound or already-compared asset cannot start a new comparison.

Improvements and regressions can coexist. Reduced clipping with destroyed
dynamics, more bass with worse supported masking evidence, or an accepted
identity violation remains a regression. Technical movement never grants
artistic approval. Only explicit feedback sets user-confirmed draft, approved,
rejected, or needs-revision state.

## Playlist, export, and delivery

PostFader cannot create or move Playlist clips. A handoff therefore lists the
exact pattern, section, Playlist track, bars, length, layer order, repeat and
mute intent, dependencies, and whether a revision replaces content or adds a
layer. Revision handoffs are deltas rather than copies of unchanged rows, and
placement remains unverified until the user confirms it.

An Export Handoff requests one full mix with an exact bar/time range, filename,
tail and normalization settings, expected root, and next action. It requests
only stems that resolve a specific uncertainty. Before and after use matching
settings.

The final read-only delivery view combines every outcome dimension, accepted
palette and sections, patterns, handoffs, comparisons, limitations, approval,
and exact next action. An explicit export may create JSON and Markdown under:

```text
<FL Studio user-data>/Settings/PostFader/deliveries/
```

`POSTFADER_CREATION_DELIVERY_PATH` may select another absolute delivery
directory. Each requested JSON or Markdown file is create-only: an existing
file with identical content is idempotent, while different content (or a
non-decodable existing file) raises instead of overwriting. The returned digest
identifies the logical manifest; the result also returns the exact SHA-256 of
each requested JSON and Markdown artifact. New files use restrictive `0600`
permissions, and `overwrite` is not supported. If a companion-format write
fails, newly created files from that call are cleaned up while pre-existing
identical files are preserved. Delivery export is a separate serialization
boundary from session persistence: a manifest built from in-memory assets may
retain canonical attached asset paths, but its serializer still removes
credentials, prompts, transcripts, encoded audio, and arbitrary private paths.
Manifests never save the FL project.

## Maintainer live acceptance

Public tests use only the deterministic synthetic fixtures under
`tests/fixtures/creation_review/`. Regenerate and verify that set with:

```bash
./.venv/bin/python scripts/generate_creation_review_fixtures.py
./.venv/bin/python -m unittest \
  tests.test_creation_review_fixtures \
  tests.test_creation_review_analysis \
  tests.test_creation_review_comparison \
  tests.test_creation_review_delivery
```

The maintainer harness defaults to an offline, plan-only report and never
contacts MCP or FL Studio in that mode:

```bash
./.venv/bin/python scripts/live_creation_review_acceptance.py --plan
```

For a live run, use an already armed disposable project, an explicit completed
Production Run ID, and caller-selected bounce paths. Evidence output must be a
new file below `.private/` and live work requires both user-presence and
disposable-project confirmations:

```bash
./.venv/bin/python scripts/live_creation_review_acceptance.py --live \
  --source-run-id RUN_ID --bounce /absolute/path/before.wav \
  --before-bounce /absolute/path/before.wav \
  --output .private/creation-review-acceptance.json \
  --confirm-user-present --confirm-disposable-project
```

Add `--apply --authorize-apply --confirm-safe-to-edit --midi-port "Exact
Endpoint"` only for the invocation that is deliberately applying one revision.
Use `--before-bounce` and `--after-bounce` together for comparison, and
`--export-delivery` only when create-only delivery files are wanted below the
private boundary. The harness does not render, save, click the FL UI, replay an
unknown mutation, or treat its timing as a product qualification. Keep private
bounces, project files, screenshots, logs, and evidence outside source control.

## Current boundaries

Creation Review has no live audio capture, automatic render or save/Save As,
Playlist clip CRUD or placement readback, plug-in insertion/removal/reorder,
automation-point read/edit, unrestricted file or URL access, cloud telemetry,
stem separation, embedded model, or undocumented UI automation. Piano Roll
score enumeration/editing remains a separate user-run `.pyscript` path and is
not application-verified. Marker names can be observed, but marker times are
not verified. The live FL `ProjectSummary` normally provides scalar tempo and
meter, not automatic tempo-map discovery; map support depends on retained or
caller-supplied source context. Vocal and instrumental stems can be measured
when supplied, but source separation, slicing, pitch mapping, and chop
generation remain future Vocal Chop Engine work. Musical approval and the
under-five-minute armed-ready live revision target require maintainer
acceptance against a disposable project and private bounces; public tests use
only deterministic synthetic material.
