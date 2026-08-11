# Hermetic synthetic audio fixtures

These files are generated from code and contain no user audio, recordings,
stems, samples, presets, or project data. Never replace them with material from
a real FL Studio project.

- `audio/reference_mix.wav` is deterministic synthetic stereo PCM.
- `audio/candidate_delayed_minus6db.wav` is the reference delayed by 480
  samples and attenuated by 6 dB for alignment and loudness-match tests.
- `audio/boundary_impulses.wav` contains impulses at exact sample boundaries.
- `audio/fixture_manifest.json` records hashes and expected measurements.

Regenerate the WAV fixtures with:

```bash
./.venv/bin/python scripts/generate_audio_fixtures.py
```
