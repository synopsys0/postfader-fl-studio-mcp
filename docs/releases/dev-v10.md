# Next-release notes (development)

These notes describe the integrated Autonomous Creation Quality upgrade on
the `dev` branch. They are not a released v0.20 claim; the historical
[v0.20.0 notes](v0.20.0.md) remain unchanged.

## Creation workflow

- Complete creation requests can run through one bounded Production Run with
  `preflight`, `palette`, `composition`, `note_application`, `processing`,
  and `finalization` phases.
- `postfader_creation_readiness` aggregates setup blockers and limitations
  without mutation. A run performs one readiness preflight and keeps a
  process-local context snapshot for session, target, project, palette,
  preset, drum-map, effect, and Piano Roll evidence.
- Phase timings are local diagnostics with soft warnings. They are not
  uploaded telemetry.
- `CreationOutcome` separates technical execution, arrangement delivery,
  processing, audible quality, and manual handoff. Audible quality remains
  `not_evaluated` until user or bounce evidence exists.

## Sound and processing

- Sound Selection can discover exact preset identities beyond the first
  bounded page and reports coverage, truncation, duplicates, alternatives,
  score margins, metadata confidence, and provenance.
- `lock_existing` and `anchor_after_selection` have separate semantics, and
  sound-aware composition carries register, articulation, envelope, density,
  and mono/poly constraints with evidence.
- Effect coverage and semantic processing use only loaded, Atlas-matched,
  adapter-backed controls. Displayed-value or exact-option setters retain
  later-tick readback; missing effects produce an honest dry or partial state.

## Creation Review, Revision, and Delivery

- The development MCP surface is now 127 tools and 8 resources, including 13
  Creation Review tools. The closed Production Run union adds 9 corresponding
  review operations: session start, asset attachment, evaluation, feedback,
  revision planning, revision application, bounce comparison, Playlist handoff,
  and delivery-manifest creation.
- A completed Production Run can open a bounded Review Session linked to its
  outcome, palette, generated sequences, processing evidence, sections,
  patterns, and manual handoffs.
- Caller-selected bounces can be measured globally and by known section.
  Optional synchronized stems and a reference enrich the evidence without
  turning measurements into artistic approval or copying reference content.
- Explicit producer feedback has the highest taste authority and can lock
  sound assignment, notes, rhythm, register, processing, level, placement, and
  role identity independently.
- One closed RevisionPlan is validated before mutation and adapts to the
  existing Production Run executor. A pass uses one readiness preflight and
  one task-scoped authorization, retains earlier receipts, stops on unknown or
  blocking unverified outcomes, and enters `awaiting_rebounce` when complete.
  The plan is bound to a canonical request digest except for
  `authorized_to_modify`, which is asserted afresh for each mutating task.
  If local Review Session persistence fails after a verified FL mutation, the
  result is blocked with a process-local receipt and no replay.
- Matching before/after exports report improvements, regressions, unchanged
  objectives, and insufficient evidence separately. Playlist placement and
  rendering remain precise manual handoffs.
- Review persistence is opt-in, local, atomic, bounded, and path-redacted by
  default. A private per-path advisory lock serializes writers across MCP
  processes, and deterministic pruning protects findings referenced by
  retained plans or passes. Durable and delivery serializers remove
  credentials, prompts, transcripts, encoded or raw audio, cloud identifiers,
  and arbitrary private paths; opt-in asset-path retention is limited to
  canonical attached asset fields. Delivery JSON and Markdown manifests are
  create-only, return the logical manifest digest plus exact artifact hashes,
  clean up newly created companions after a paired-write failure, and do not
  save the FL Studio project.

The deterministic public fixture generator is
[`scripts/generate_creation_review_fixtures.py`](../../scripts/generate_creation_review_fixtures.py).
The maintainer live workflow is
[`scripts/live_creation_review_acceptance.py`](../../scripts/live_creation_review_acceptance.py);
private bounces and evidence remain outside the repository.

## Acceptance and packaging

The maintainer harness is
[`scripts/live_creation_acceptance.py`](../../scripts/live_creation_acceptance.py).
It provides plan-only and live composition/production scenarios for a blank,
disposable armed-ready project. Live evidence, project files, screenshots,
logs, and timing output must stay outside the repository. Acceptance targets
(including the under-five-minute modest 32-bar goal and one authorization)
remain explicitly unclaimed until a live run records them.

The wheel/source archive and platform bundles include the creation-pipeline and
Creation Review modules, preset metadata resource, public guides, deterministic
fixtures, and acceptance harnesses. MCPB contains runtime modules/package data,
including Creation Review, but not maintainer scripts or tests. Run manifest
synchronization, the focused creation and package tests, public-tree/package
verification, bundle checks, and the installed smoke check before publishing;
do not tag or publish this development note.
