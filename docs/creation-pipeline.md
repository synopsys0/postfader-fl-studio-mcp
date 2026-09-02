# Autonomous Creation Quality and Fast Execution

The development branch extends task-scoped Production Runs with one creation
pipeline. A connected AI still interprets the producer's request and submits a
closed typed plan; PostFader does not contain an LLM, audition live audio, or
invent unsupported FL Studio operations.

The pipeline's completion outcome is also the source state for a bounded
[Creation Review](creation-review.md) session. Review never rewrites creation
receipts: it retains the original palette, generated sequences, processing
evidence, section intent, timing, and manual handoffs, then records evaluation
and revision results alongside that immutable source snapshot.

## One readiness preflight

`postfader_creation_readiness` is a read-only scorecard. The normal
`postfader_execute_run` path invokes the same service internally, so it is not
a mandatory extra user step. The scorecard reports `ready`,
`ready_with_limitations`, or `blocked` across these dimensions:

- connection, package/bridge revision, process, MIDI transport, session, and
  runtime write control;
- Piano Roll script presence, process-local arming, authenticated receipt,
  target selection, and persistence-receipt support;
- loaded generators, Atlas matches, current presets, navigation, discovery,
  and requested-role coverage;
- drum-capable targets, exact kit support, the reported pad map, and missing
  semantic drum roles;
- patterns, empty targets, existing material, and expected manual Playlist
  placement;
- loaded effects, Atlas products, semantic adapters, control evidence,
  supported techniques, and missing processing categories; and
- preservation rules, allowed mutations, unsupported capabilities, and known
  manual work.

All independently observable blockers are returned together. A missing effect
is a limitation for a playable or first-pass draft and a blocker only when the
completion target requires processing that cannot be delivered. Playlist clip
placement remains a manual handoff rather than a false failure of pattern
creation.

The preflight creates an immutable process-local context snapshot containing
the session and relevant target fingerprints, project checkpoint, pattern
identities, inventory/preset/drum/effect digests, Piano Roll arming receipt,
bridge revisions, MIDI endpoint, and timestamp. It is a run cache, not a
permanent project identity.

## Phased execution and timing

An applicable run is represented internally as:

1. `preflight`
2. `palette`
3. `composition`
4. `note_application`
5. `processing`
6. `finalization`

Empty phases are skipped. The executor retains one captured session and cached
inventory, validates only the needed target at mutation time, and preserves
later-tick readback. It enables the task-scoped write gate once for an
authorized execution segment and verifies automatic shutdown when the run
terminates. It does not ask for phase-by-phase approval or replay an ambiguous
write.

Local timing records phase duration, operations, full inventory scans,
target-specific refreshes, preset navigation/enumeration, Piano Roll dispatch,
setup/manual wait where measurable, blocked duration, and write authorization
transitions. Soft warnings flag repeated scans/preparation or excessive
refreshes but never fail a correct run. Timing is kept in process-local state;
no telemetry is uploaded.

## Truthful completion

The legacy run status remains for compatibility, but `CreationOutcome` reports
independent dimensions:

- technical execution: verified, limited, partial, blocked, failed, or stopped;
- arrangement delivery: playable, manual-Playlist handoff, patterns not
  placed, partial, or not delivered;
- processing: processed, restrained first pass, dry by design, dry because
  effects are missing, partial, or not requested;
- audible quality: not evaluated, explicit user feedback, or bounce-analysis
  evidence; and
- outstanding or completed manual handoffs.

Successful tool receipts never imply that PostFader heard the result. A user
can explicitly confirm, approve, or reject a draft later without rewriting the
technical receipts.

## Sound-aware composition

Selected sounds may carry bounded characteristics for attack, release,
sustain, articulation, plucked/sustained behavior, mono/poly behavior,
polyphony, register/range, sub suitability, transient intensity, density,
motion, brightness, width, aggressiveness, softness, complexity, tonality, and
rhythmic function. Every characteristic records confidence and provenance.
Preset-name inference is never high-confidence audible knowledge.

The optional sound-aware profile keeps old deterministic composition callers
compatible. It can shorten and separate plucks, lengthen and thin pads, enforce
monophonic lines, constrain chord voice count and register, keep sub-bass
monophonic and low, and reduce supporting density for complex timbres.
Adaptation returns the resulting `NoteSequence` plus a report of connected-AI
constraints, server-derived decisions, changes, reasons, and confidence.
Section development reuses identity material while changing only declared
dimensions such as density, register, voicing, articulation, bass rhythm, or
supporting percussion.

## Preset knowledge and preferences

Preset discovery is deterministic and bounded. Small catalogs can be fully
enumerated; large catalogs use current, first, final, quartile, seed, explicit
preference, history, anchor, and adjacent candidates within page/candidate
limits. Coverage says `complete`, `stratified`, `targeted`, or `minimal` and
reports pages, considered/omitted counts, matches, exclusions, and limitations.
PostFader does not call stratified coverage complete.

Versioned bundled metadata and an isolated user-local overlay provide reviewed
exact/family characteristics where evidence exists. A controlled synonym map
normalizes producer terms while retaining the original wording. Selection
reports metadata, role-fit, identity, and total confidence plus a winner and a
bounded shortlist with score margins. Weak metadata stays labeled weak.

`PreferenceDirective` distinguishes `user_explicit`, explicit profile or
feedback, connected-model inference/suggestion, history, and system defaults.
Only explicit user/profile/feedback direction may be hard; model and history
suggestions remain soft. Per-role feedback updates only named assignments or
descriptors. `lock_existing` preserves the pre-run identity;
`anchor_after_selection` stabilizes a newly verified choice; and
`preserve_across_sections` keeps that role recognizable while allowing a
declared complementary variation.

## Semantic processing

`processing_plan` maps a bounded goal such as reducing mud, adding depth,
controlling dynamics, keeping low end centered, taming harshness, or limiting
peaks through this evidence chain:

`goal → technique → Atlas capability → loaded effect → adapter/control evidence → semantic action`

Only loaded targets are candidates. A semantic action resolves the exact
parameter/name, unit, setter, dependencies, current observation, and
verification basis. Displayed-value and exact-option setters are preferred;
normalized writes require an established adapter mapping. Unknown controls,
stale targets/sessions, and unknown or failed readback stop dependent actions
without replay or rollback. `processing_apply_plan` is a focused lower-level
workflow; complete creation should use `plan_processing` and
`apply_processing_plan` inside the same high-level Production Run.

The default first-pass policy is conservative and Master-protected. It does
not treat metadata reasoning as audible proof.

## Armed-ready acceptance templates

Maintainers can run the documented live acceptance workflow against disposable
projects. The composition template has an armed Piano Roll bridge, loaded
generator/drum pool, and empty patterns but no effects. The production template
also has supported loaded stock effects and adapter evidence. Artifacts,
screenshots, projects, and timing logs stay outside the repository.

Acceptance goals are under five minutes for a modest 32-bar draft from an
armed-ready project, under ten minutes with one documented setup action, one
task-scoped authorization, zero preflight-detectable surprise blockers, and at
most one Playlist handoff. These are targets, not claims; only a recorded live
run can establish them.

See [Production Runs](production-runs.md), [Sound Selection](sound-selection.md),
[Plugin Atlas](plugin-atlas.md), [FL constraints](fl-constraints.md), and
[Setup](setup.md).
