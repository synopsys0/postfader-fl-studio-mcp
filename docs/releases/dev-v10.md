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

## Acceptance and packaging

The maintainer harness is
[`scripts/live_creation_acceptance.py`](../../scripts/live_creation_acceptance.py).
It provides plan-only and live composition/production scenarios for a blank,
disposable armed-ready project. Live evidence, project files, screenshots,
logs, and timing output must stay outside the repository. Acceptance targets
(including the under-five-minute modest 32-bar goal and one authorization)
remain explicitly unclaimed until a live run records them.

The wheel/source archive and platform bundles include the creation-pipeline
modules, preset metadata resource, public guides, and acceptance harness.
MCPB contains runtime modules/package data but not maintainer scripts or
tests. Run the focused creation, public-tree, package, bundle, and installed
smoke checks before publishing; do not tag or publish this development note.
