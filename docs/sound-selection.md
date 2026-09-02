# Sound Selection

Sound Selection lets the connected AI choose the instruments, loaded generator
plug-ins, exact presets, drum kit, and sound palette for a production request.
It is a normal PostFader workflow: the AI translates the producer's direction
into a strict `SoundSelectionRequest`, PostFader inventories the open project,
plans a coherent palette, and applies supported choices when the request
authorizes project changes.

There is no Sound Selection mode to enable. Say what the track should feel like
and which decisions you want to delegate. The connected AI supplies roles such
as `main_chords`, `main_lead`, `primary_bass`, `sub_bass`, `drums`, `texture`,
or a bounded custom role. It can also carry product or preset preferences,
exclusions, stock-only direction, section scope, register, articulation,
descriptor, layering, continuity, and novelty instructions.

## Direction comes first

Explicit direction is stronger than recency, novelty, or a tie-breaking seed.
For example:

- “Use Vital for the chords” is a must-use product preference.
- “Do not use FLEX” is a hard exclusion.
- “Use only stock FL Studio instruments” excludes third-party candidates.
- “Keep the lead but change the bass” preserves one role and frees the other.
- “Use something familiar” favors continuity; “surprise me” favors bounded
  exploration without ignoring role fit.

If a requested product or preset is not loaded, Sound Selection reports the
blocker instead of silently substituting an unrelated sound. Atlas may suggest
known alternatives, but a recommendation is not an executable assignment.

## Balanced continuity and novelty

Planning is deterministic for the same request, loaded inventory, palette state,
history state, and seed. A seeded tie-break is used only when meaningful
candidates have equivalent scores; this is not random preset roulette or a
fixed genre-to-preset table.

Candidates are considered in this order:

1. hard constraints such as exclusions, stock-only policy, target kind, locks,
   section scope, loaded-target presence, and required drum mappings;
2. user-direction fit, which is the strongest ranking signal;
3. role and product suitability;
4. metadata-level palette cohesion and complementary layering;
5. continuity within the current project or Production Run;
6. bounded cross-project usage and explicit feedback; and
7. identity and verification confidence.

Every selected candidate carries a score breakdown and concise rationale. The
cohesion score is based on declared roles, descriptors, registers, product
knowledge, and layering intent. PostFader does not claim spectral or audible
proof without an audio measurement.

The default `balanced` policy normally keeps identity sounds stable in one
song while allowing more movement in textures, fills, counterleads, effects,
and percussion. Across unrelated projects it mildly penalizes recently reused
exact presets and same-product choices when otherwise suitable candidates are
tied. A substantially better match still wins. `consistent`, `balanced`,
`exploratory`, and bounded `custom` policies adjust ranking; none bypasses a
hard constraint.

Creation Review retains the palette and its evidence from the source
Production Run. Producer feedback may lock sound assignment independently from
notes, rhythm, register, level, or processing. A revision that asks to keep the
melody but change the sound can therefore use a verified palette alternative
without discarding the generated sequence; keeping the sound while rewriting
the melody does the converse. Review history never turns silence into an
accepted assignment.

## Anchors and section variations

Core identity roles are anchors by default. Typical anchors are the main chords,
main lead, primary bass, sub-bass, vocal chop, and primary drum kit. Locked
roles and preserved existing assignments cannot be replaced accidentally.

`lock_existing` protects an assignment that was already present before
planning. `anchor_after_selection` stabilizes a newly selected assignment for
later sections. They are intentionally different: a new palette can establish
an anchor without claiming that the role was previously locked, and a locked
existing role is not silently replaced by a variation.

For a later section, Sound Selection normally develops the identity through an
extra layer, changed register or voicing, complementary preset family,
countermelody, articulation, percussion, texture, or section-specific
processing. `sound_selection_create_variation` returns a read-only,
section-scoped delta with unchanged role IDs and parent assignment references.
It preserves anchors unless the request explicitly names a replacement. The
delta is not an implicit FL mutation; apply it only through a later authorized
workflow.

## Inspecting the available pool

`sound_selection_inventory` performs one target-aware read of the loaded
project and returns compact summaries for:

- global Channel Rack generator targets;
- mixer-effect targets when effects are requested;
- current preset identity and bounded preset pages;
- preset navigation and readback support;
- generic pad maps for generators;
- current palette and locked roles; and
- Atlas matches plus Atlas-known products not observed as loaded.

Use the lower-level `plugins_list_presets`,
`plugins_get_current_preset`, and `plugins_inspect_pad_map` tools for a focused
read. The target model is shared with the rest of PostFader:
`mixer_effect` names a mixer track and effect slot, while
`channel_generator` names a global Channel Rack channel. These are never
interchanged, even when their display names match.

Preset pages are bounded and deterministic. They report FL's preset count,
index/name rows, blank names, duplicate names, current identity, and
partial/truncated warnings. A current preset index is returned only when the
index can be resolved unambiguously. Do not treat a page as a complete catalog
when its `partial` or `truncated` flag says otherwise.

## Exact preset selection and verification

`fl_select_plugin_preset` accepts an exact reported `preset_name`, an exact
`preset_index`, or both, plus optional expected-current, session, and target
fingerprints. Duplicate names are ambiguous and require an index. The
operation validates the live target, reads the available catalog, chooses the
shortest valid next/previous path when the current index is known, and otherwise
uses a bounded fallback search.

Navigation is spread across later FL idle ticks and has bounded navigation and
settling limits. It succeeds only when FL reads the requested identity back;
dispatch alone is not success. A stale session or target, unstable identity,
missing readback, navigation bound, or contradictory response stops the
operation. An ambiguous mutation outcome is never replayed, and the tool never
searches indefinitely or silently chooses an unrelated preset. The receipt
includes before/after identity, steps, direction, warnings, and the undo
evidence FL actually exposed (`true`, `false`, or `null`).

Sound Palette application follows the same rule. It revalidates the inventory,
applies assignments in deterministic role order, stops at the first unknown or
unverified preset result, and retains earlier truthful receipts. Successfully
verified assignments may update local usage history; planning alone never does.
The public `sound_selection_apply` tool requires the current 32-character
lowercase `session_fingerprint` from a recent live read; omission is rejected
by MCP schema validation before the mutating service runs. Task-scoped
Production Runs capture and forward that fingerprint automatically.

## Candidate discovery and metadata confidence

Palette planning uses bounded preset-candidate discovery across later pages of
the reported catalog when the first page is not enough. It records pages
visited, requested/observed coverage, truncation, duplicate identities, and
any exact requested identity that could not be found. A larger catalog is not
silently treated as complete, and a candidate's page coverage is evidence
about discovery rather than evidence that the preset was heard.

The bundled `preset-metadata-v1.json` resource contains conservative family
annotations and may contain reviewed exact records. User-local reviewed
metadata is a separate optional layer. Every descriptor, articulation,
register, envelope, mono/poly, and role claim carries provenance and a
confidence level. A normalized preset-name token can be useful for ranking,
but it cannot carry high confidence; absence of metadata is reported as
`metadata_insufficient` or `unknown`, not as a negative sound judgment.

Preference provenance is kept distinct from metadata: an explicit user
preference or explicit per-role feedback can be a hard constraint, while a
model suggestion, history preference, or system default remains soft. Feedback
updates only the addressed role/descriptor and never silently changes another
assignment. See [Creation Pipeline](creation-pipeline.md) for the immutable
characteristic and outcome contracts.

## Drum kits and pad maps

Drum-kit choice is part of the palette. `plugins_inspect_pad_map` reads the
generic FL pad API and reports pad index, MIDI/semitone note, color, empty and
muted state, and a reported semitone name where FL provides one. Sound
Selection maps names to semantic roles such as kick, snare, clap, closed hat,
open hat, crash, ride, tom, and percussion. It does not assume General MIDI
notes: a non-General-MIDI kit must use its reported map.

`select_drum_kit` in a Production Run selects an exact preset and then reads
the resulting map. `inspect_drum_map` exposes the same typed map for a read-only
run. A required kick, snare, hat, or other declared role that cannot be mapped
blocks before drum notes are written. `compose_drums` accepts a typed
`DrumPadMap`; when no map is supplied, its General MIDI mapping remains an
explicit backwards-compatible fallback and is labeled in the result.

## Plugin Atlas is not the live inventory

Plugin Atlas is a bundled, offline knowledge layer for product categories,
common roles, techniques, limitations, stock origin, related products, and
adapter knowledge. It enriches a loaded observation and can recommend products
that are not present in the project. It does not prove installation, ownership,
loaded state, a plug-in version, parameter control, or an audible result.

Sound Selection still accepts an unprofiled loaded plug-in. Runtime name,
target kind, current preset, exposed preset page, user direction, and local
metadata can make it a lower-confidence candidate. Atlas adapter availability
does not replace the live target and preset readback checks.

## Loop Starter is a separate source

Loop Starter is available only when the request explicitly uses
`source_strategy="loop_starter"` (or an explicitly mixed strategy). A Loop
Starter request uses FL's channel reroll path. The selected loop has no
authoritative identity readback, so the result is dispatch-only and is not
counted as a verified sound choice. An original instrument-pool request never
silently falls back to Loop Starter.

## Local feedback and history

Sound Selection keeps a bounded local history when `persist_history=true`.
By default it lives at `Settings/PostFader/sound-selection-history-v1.json`
under the FL Studio user-data directory. Set the absolute
`POSTFADER_SOUND_SELECTION_HISTORY_PATH` (or its compatibility alias
`POSTFADER_SOUND_SELECTION_HISTORY`) to use another local path.

The store records only product ID, preset identity digest (and locally useful
preset name), role, style tags, timestamps, usage and consecutive-use counts,
explicit accepted/rejected feedback, and palette digest. It stores no audio,
project files, prompts, credentials, transcripts, vendor manuals, or cloud
data. Writes are local, schema-versioned, bounded, thread-safe, atomic, and
deterministically pruned. A corrupt file is isolated and left untouched with a
warning; it is not silently replaced.

Use `sound_selection_record_feedback` for an explicit `accepted`, `rejected`,
or `neutral` verdict, optionally scoped to a role and descriptors. Silence is
never inferred as acceptance. Feedback changes future ranking as a bounded
preference, not an absolute rule. `persist_history=false` performs no history
write, including during application. `sound_selection_history_status` reports
path, health, schema, and counts. `sound_selection_history_reset(confirm=true)`
is an explicit destructive local reset; the removed file is not recoverable by
PostFader.

## Production Runs and typed references

Production Runs can keep sound choice and composition in one bounded plan. The
closed operation union includes:

- `plan_sound_palette` (read-only planning);
- `apply_sound_palette` (authorized, verified application);
- `create_sound_palette_variation` (read-only section delta);
- `select_plugin_preset` and `select_drum_kit` (verified preset operations);
- `inspect_drum_map` (read-only map inspection); and
- `record_sound_feedback` (local workflow state).

Outputs are typed and addressable by a later operation: `sound_palette`,
`palette_assignment`, `generator_target`, `drum_map`, `selected_preset`, and
`section_variation`. A note-writing operation can use a prior palette role as
its channel target, and `generate_drums` can consume a prior `drum_map`
reference. References must point to a compatible earlier operation; a
dependent operation stops if its prerequisite is blocked.

For a clear creation request, the connected AI should inventory, plan, and
apply the palette before writing the complete arrangement. A plan-only run
never changes FL or history. An authorized run enables the existing session
write boundary once, applies in order, and keeps immutable receipts. Runs are
bounded and non-atomic: earlier verified assignments remain recorded if a later
selection is unverified, and there is no automatic retry or rollback.

The same run can continue from selected sounds into sound-aware composition.
`adapt_note_sequence` carries selected-sound characteristics and reports the
register, articulation, envelope, density, and practical-polyphony decisions
used for that role. Metadata confidence is preserved in the adaptation; weak
or missing evidence produces a limitation, not an audible-quality claim. If
the request also includes processing, effect coverage and semantic processing
are reported separately from palette selection and arrangement delivery.

## What FL Studio cannot prove

Sound Selection chooses from instruments and effects already loaded in the
current project. PostFader cannot insert, remove, replace, or reorder plug-ins
through the supported backend, and Atlas cannot make an unloaded product
available. Load the desired pool manually in FL Studio before planning.

PostFader also cannot hear FL Studio's live output, audition a preset, render,
or save the project. Descriptor and cohesion decisions are metadata-level
reasoning. Save manually in FL Studio after reviewing the plan, receipts, and
warnings. Supported preset mutations use later-tick readback and report when
FL exposes weaker evidence; they do not claim artistic quality, guaranteed
undo, or rollback.

## Example workflows

### Delegate a complete palette

“Create a melodic bass track with bright, colorful synths, keep the top end
smooth, and choose all sounds yourself.” The AI can request roles for chords,
lead, bass, sub, and drums; set descriptors and register; call
`sound_selection_inventory`; plan with the balanced policy; apply the verified
assignments; then generate notes and drums using the returned role and map
references.

### Make a second drop bigger

“Keep the main lead and chords, but make Drop B bigger and vary the bass.” The
variation planner preserves the anchor assignments, then emits only a
section-scoped bass change and/or a complementary wide texture. “Replace the
lead too” explicitly frees that anchor; absent that direction, it remains.

### Enforce a loaded pool

“Use only stock FL Studio instruments, do not use FLEX, and use Vital for the
chords.” Sound Selection hard-filters those instructions. If Vital is not
loaded, the chord role blocks and Atlas can recommend known products without
claiming that any recommendation is installed.

### Request Loop Starter deliberately

“Reroll the Loop Starter channel for a quick loop idea.” The AI selects the
explicit Loop Starter source strategy and reports the bounded dispatch. For
“compose an original instrument-based drum part,” it uses loaded generators
and a reported drum map instead.

See [Tool and command reference](tool-contracts.md),
[Production Runs](production-runs.md), [Plugin Atlas](plugin-atlas.md), and
[FL Studio constraints](fl-constraints.md) for the lower-level contracts.
