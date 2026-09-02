# Hermetic fixtures

These fixtures contain no user audio, recordings, stems, samples, presets, or
project data. Never replace them with material from a real FL Studio project.
The audio files are generated from code and pinned by hash.

- `audio/reference_mix.wav` is deterministic synthetic stereo PCM.
- `audio/candidate_delayed_minus6db.wav` is the reference delayed by 480
  samples and attenuated by 6 dB for alignment and loudness-match tests.
- `audio/boundary_impulses.wav` contains impulses at exact sample boundaries.
- `audio/fixture_manifest.json` records hashes and expected measurements.
- `write-scenario-v1.json` is deterministic schema input for the fake-bridge
  safety tests. It contains generic disposable-project values, not a live run,
  operator instructions, or a saved validation record.

Regenerate the WAV fixtures with:

```bash
./.venv/bin/python scripts/generate_audio_fixtures.py
```

`creation_review/` contains short deterministic bounces and synchronized test
stems for the Creation Review lifecycle. They cover clipping, tonal imbalance,
weak and improved section contrast, one deliberate before/after regression,
alignment and duration mismatch, and tail behavior. The adjacent structural
fixture represents a synthetic four-section Production Run, palette, feedback,
revision, comparison, and Playlist handoff.

Regenerate that fixture family with:

```bash
./.venv/bin/python scripts/generate_creation_review_fixtures.py
```
