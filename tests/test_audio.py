"""Check the audio measurements against signals whose answers we know."""

import math
import os
import sys
import tempfile

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fl_studio_mcp import audio  # noqa: E402

PASS = 0
FAIL = 0
TMP = tempfile.mkdtemp(prefix="flmcp-audio-")
SR = 48000


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s  %s" % (label, detail))


def write(name, data, rate=SR):
    path = os.path.join(TMP, name)
    sf.write(path, data, rate)
    return path


def sine(freq, secs=4.0, amp=0.5, rate=SR):
    t = np.arange(int(secs * rate)) / rate
    return amp * np.sin(2 * np.pi * freq * t)


def main():
    print("\n-- level measurement --")
    # A 0.5-amplitude sine: peak -6.02 dBFS, RMS 3.01 dB below peak.
    p = write("sine1k.wav", sine(1000, amp=0.5))
    a = audio.load(p)
    loud = audio.measure_loudness(a)
    check("sample peak of 0.5 sine", abs(loud["sample_peak_db"] - (-6.02)) < 0.1,
          loud["sample_peak_db"])
    check("rms is 3.01 dB under peak",
          abs(loud["rms_db"] - (-9.03)) < 0.1, loud["rms_db"])
    check("crest factor of a sine is ~3 dB",
          abs(loud["crest_factor_db"] - 3.01) < 0.15, loud["crest_factor_db"])
    check("no clipping reported", loud["clipped_samples"] == 0, loud)
    # Files are written as 16-bit PCM, so quantisation leaves ~1e-5 of offset.
    check("dc offset ~0", abs(loud["dc_offset"]) < 1e-4, loud["dc_offset"])
    check("lufs in plausible range for -9 dB sine",
          -16 < loud["lufs_integrated"] < -6, loud["lufs_integrated"])

    # Halving amplitude must drop loudness by 6 dB.
    p2 = write("sine1k_quiet.wav", sine(1000, amp=0.25))
    l2 = audio.measure_loudness(audio.load(p2))
    check("6 dB drop tracked in LUFS",
          abs((loud["lufs_integrated"] - l2["lufs_integrated"]) - 6.02) < 0.2,
          (loud["lufs_integrated"], l2["lufs_integrated"]))

    print("\n-- clipping and dc --")
    clipped = np.clip(sine(1000, amp=1.4), -1.0, 1.0)
    lc = audio.measure_loudness(audio.load(write("clipped.wav", clipped)))
    check("clipped samples detected", lc["clipped_samples"] > 1000, lc["clipped_samples"])
    dc = sine(1000, amp=0.3) + 0.05
    ldc = audio.measure_loudness(audio.load(write("dc.wav", dc)))
    check("dc offset detected", abs(ldc["dc_offset"] - 0.05) < 0.005, ldc["dc_offset"])

    print("\n-- channel-aware full-duration peaks --")
    anti = sine(440, secs=2, amp=0.8)
    anti_path = write("anti_phase_levels.wav", np.column_stack([anti, -anti]))
    anti_loaded = audio.load(anti_path)
    anti_loud = audio.measure_loudness(anti_loaded)
    check("anti-phase channels do not cancel sample peak",
          abs(anti_loud["sample_peak_db"] - (-1.94)) < 0.12,
          anti_loud)
    check("anti-phase channels do not cancel RMS",
          anti_loud["rms_db"] is not None and anti_loud["rms_db"] > -6.0,
          anti_loud)
    check("per-channel true peaks are reported",
          len(anti_loud["true_peak_dbtp_per_channel"]) == 2
          and all(v is not None for v in anti_loud["true_peak_dbtp_per_channel"]),
          anti_loud)
    check("load retains full channels despite silent mono fold-down",
          np.max(np.abs(anti_loaded.samples)) < 1e-4
          and anti_loaded.channel_samples.shape[1] == 2,
          anti_loaded.samples[:5])

    left_clipped = np.clip(sine(1000, amp=1.4), -1.0, 1.0)
    right_clean = sine(1000, amp=0.2)
    one_channel_clip = audio.measure_loudness(audio.load(write(
        "one_channel_clip.wav", np.column_stack([left_clipped, right_clean])
    )))
    check("clipping in one stereo channel is detected",
          one_channel_clip["clipped_samples_per_channel"][0] > 1000
          and one_channel_clip["clipped_samples_per_channel"][1] == 0,
          one_channel_clip)
    check("scalar clipped count is the channel-sample total",
          one_channel_clip["clipped_samples"]
          == sum(one_channel_clip["clipped_samples_per_channel"]),
          one_channel_clip)
    check("clipped frames are distinct from channel-sample count",
          one_channel_clip["clipped_frames"]
          == one_channel_clip["clipped_samples_per_channel"][0],
          one_channel_clip)

    # This impulse is beyond the old 60-second inspection cap. A smaller
    # sample rate keeps the fixture quick while preserving the time boundary.
    late_rate = 8000
    late = np.zeros(int(60.25 * late_rate))
    late[int(60.1 * late_rate)] = 0.8
    late_true_peak = audio._true_peak_db(late, late_rate)
    check("true peak scans beyond 60 seconds",
          np.isfinite(late_true_peak) and late_true_peak > -3.0,
          late_true_peak)

    print("\n-- spectrum --")
    spec = audio.measure_spectrum(audio.load(p))
    check("1 kHz lands in the mid band", 900 < spec["dominant_hz"] < 1100,
          spec["dominant_hz"])
    check("mid band dominates",
          spec["bands"]["mid"]["energy_share"] > 0.8, spec["bands"]["mid"])
    check("centroid near 1 kHz", 900 < spec["spectral_centroid_hz"] < 1200,
          spec["spectral_centroid_hz"])

    low = audio.measure_spectrum(audio.load(write("sine80.wav", sine(80))))
    check("80 Hz lands in the low band",
          low["bands"]["low"]["energy_share"] > 0.8, low["bands"]["low"])
    check("brighter signal has higher centroid",
          low["spectral_centroid_hz"] < spec["spectral_centroid_hz"],
          (low["spectral_centroid_hz"], spec["spectral_centroid_hz"]))

    print("\n-- sibilance --")
    body = sine(700, amp=0.4)
    essy = body + sine(7000, amp=0.4)
    s_plain = audio.measure_spectrum(audio.load(write("plain.wav", body)))
    s_essy = audio.measure_spectrum(audio.load(write("essy.wav", essy)))
    check("sibilance ratio rises with 7 kHz energy",
          s_essy["sibilance_ratio"] > s_plain["sibilance_ratio"] * 10,
          (s_plain["sibilance_ratio"], s_essy["sibilance_ratio"]))
    check("de-esser target frequency found",
          s_essy["sibilance_peak_hz"] is not None
          and abs(s_essy["sibilance_peak_hz"] - 7000) < 150,
          s_essy["sibilance_peak_hz"])

    print("\n-- rumble --")
    rum = audio.measure_spectrum(audio.load(write("rumble.wav",
                                                  sine(700, amp=0.3) + sine(25, amp=0.3))))
    check("sub-40 Hz rumble flagged", rum["sub_40hz_share"] > 0.02,
          rum["sub_40hz_share"])

    print("\n-- pitch --")
    # 220 Hz is exactly A3, so cents-off should be ~0.
    pa = audio.measure_pitch(audio.load(write("a3.wav", sine(220, secs=3))))
    check("A3 identified", pa["median_note"] == "A3", pa)
    check("A3 reads in tune", abs(pa["median_cents_off"]) < 8, pa["median_cents_off"])
    check("frequency accurate", abs(pa["median_hz"] - 220) < 2, pa["median_hz"])
    check("mostly voiced", pa["voiced_share"] > 0.8, pa["voiced_share"])
    check("high in-tune share", pa["in_tune_share_within_20c"] > 0.9, pa)

    # 223.2 Hz is 25 cents sharp of A3 — clear of the A3/A#3 boundary at
    # 226.45 Hz, where a fraction of a percent of error flips the note name.
    sharp_hz = 220 * 2 ** (25 / 1200)
    expected = 1200 * math.log2(sharp_hz / 220)
    ps = audio.measure_pitch(audio.load(write("sharp.wav", sine(sharp_hz, secs=3))))
    check("sharp note still maps to A3", ps["median_note"] == "A3", ps)
    check("cents-off matches theory (%.1f)" % expected,
          abs(ps["median_cents_off"] - expected) < 8, ps["median_cents_off"])
    check("flagged as out of tune", ps["in_tune_share_within_20c"] < 0.2, ps)
    check("voiced_share never exceeds 1", ps["voiced_share"] <= 1.0,
          ps["voiced_share"])

    # A vibrato-ish glide should read as unsteady.
    t = np.arange(int(3 * SR)) / SR
    glide = 0.5 * np.sin(2 * np.pi * np.cumsum(220 + 12 * np.sin(2 * np.pi * 5 * t)) / SR)
    pv = audio.measure_pitch(audio.load(write("vib.wav", glide)))
    check("vibrato raises jitter", pv["pitch_jitter_cents"] > pa["pitch_jitter_cents"],
          (pa["pitch_jitter_cents"], pv["pitch_jitter_cents"]))

    rng = np.random.default_rng(0)
    pn = audio.measure_pitch(audio.load(write("noise.wav",
                                              rng.normal(0, 0.2, SR * 2))))
    check("noise yields no confident pitch",
          pn.get("voiced_share", 0) < 0.5 or pn.get("note"), pn)

    print("\n-- stereo --")
    mono_sig = sine(440, secs=2, amp=0.4)
    st_same = np.column_stack([mono_sig, mono_sig])
    r1 = audio.measure_stereo(audio.load(write("same.wav", st_same)))
    check("identical channels correlate at 1", abs(r1["correlation"] - 1.0) < 1e-6, r1)
    check("no width when identical", r1["mid_side_ratio"] < 1e-9, r1)

    st_inv = np.column_stack([mono_sig, -mono_sig])
    r2 = audio.measure_stereo(audio.load(write("inv.wav", st_inv)))
    check("inverted channels correlate at -1", abs(r2["correlation"] + 1.0) < 1e-6, r2)
    check("inverted flagged mono-incompatible", r2["mono_compatible"] is False, r2)

    print("\n-- dynamics --")
    quiet = sine(440, secs=2, amp=0.02)
    loud_s = sine(440, secs=2, amp=0.6)
    dyn = audio.measure_dynamics(audio.load(write("dyn.wav",
                                                  np.concatenate([quiet, loud_s]))))
    check("dynamic spread detected", dyn["dynamic_spread_db"] > 15,
          dyn["dynamic_spread_db"])

    print("\n-- full analyze() --")
    full = audio.analyze(write("full.wav", np.column_stack([essy, essy])),
                         include_pitch=True, target_lufs=-14.0)
    check("all sections present",
          all(k in full for k in
              ("file", "loudness", "spectrum", "dynamics", "stereo", "pitch", "readings")),
          list(full))
    check("readings generated", len(full["readings"]) > 0, full["readings"])
    check("sibilance surfaced in readings",
          any("ibilan" in r for r in full["readings"]), full["readings"])
    check("file metadata correct",
          full["file"]["sample_rate"] == SR and full["file"]["channels"] == 2,
          full["file"])
    check("analysis records deterministic file provenance",
          len(full["file"]["sha256"]) == 64
          and full["file"]["sha256"] == full["provenance"]["input"]["sha256"],
          full["provenance"])
    check("analysis reports analyzer versions and confidence",
          full["provenance"]["analyzer_version"] == audio.ANALYZER_VERSION
          and full["confidence"]["level"] in ("high", "medium", "low"),
          (full["provenance"], full["confidence"]))
    check("analysis states measurement limitations",
          any("certified" in item for item in full["limitations"])
          and any("fold-down" in item for item in full["limitations"]),
          full["limitations"])

    truncated = audio.analyze(write("truncated_source.wav", essy), max_seconds=1.0)
    check("truncated analysis is explicit in provenance",
          truncated["provenance"]["input"]["truncated"] is True
          and truncated["file"]["analyzed_frames"] == SR,
          truncated["provenance"])
    check("truncation lowers confidence and adds limitation",
          truncated["confidence"]["score"] < full["confidence"]["score"]
          and any("truncated" in item for item in truncated["limitations"]),
          (truncated["confidence"], truncated["limitations"]))

    print("\n-- compare() --")
    bright = np.column_stack([sine(700, amp=0.3) + sine(9000, amp=0.35)] * 2)
    dull = np.column_stack([sine(700, amp=0.3) + sine(9000, amp=0.02)] * 2)
    cmp_ = audio.compare(write("ref_bright.wav", bright), write("tgt_dull.wav", dull))
    air_delta = (cmp_["band_deltas"]["presence"]["difference_db"]
                 + cmp_["band_deltas"]["air"]["difference_db"])
    check("dull target reads darker than bright reference", air_delta < -5, cmp_["band_deltas"])
    check("comparison produces readings", len(cmp_["readings"]) > 0, cmp_["readings"])
    check("centroid comparison present",
          cmp_["centroid_hz"]["target"] < cmp_["centroid_hz"]["reference"],
          cmp_["centroid_hz"])

    print("\n-- aligned loudness-matched comparison --")
    compare_rng = np.random.default_rng(20260809)
    base = compare_rng.normal(0.0, 0.12, SR * 5)
    fade = np.linspace(0.0, 1.0, 1000)
    base[:1000] *= fade
    base[-1000:] *= fade[::-1]
    reference_audio = np.column_stack([base, base * 0.83])
    delay_samples = int(0.137 * SR)
    target_audio = np.zeros_like(reference_audio)
    target_audio[delay_samples:] = reference_audio[:-delay_samples] * 0.25
    aligned_ref_path = write("aligned_reference.wav", reference_audio)
    aligned_target_path = write("aligned_target.wav", target_audio)
    aligned = audio.compare(
        aligned_ref_path,
        aligned_target_path,
        max_alignment_seconds=0.5,
    )
    check("delayed copy has the documented positive lag",
          abs(aligned["alignment"]["target_lag_samples_at_reference_rate"]
              - delay_samples) <= SR // 2000,
          aligned["alignment"])
    check("same-material alignment is high confidence",
          aligned["alignment"]["confidence"]["level"] == "high"
          and aligned["alignment"]["absolute_correlation"] > 0.98,
          aligned["alignment"])
    check("quarter-amplitude target receives about 12 dB",
          aligned["loudness_matching"]["applied"] is True
          and abs(aligned["loudness_matching"]["gain_db_applied"] - 12.04) < 0.12,
          aligned["loudness_matching"])
    check("loudness-match residual is negligible",
          abs(aligned["loudness_matching"]["residual_lu"]) <= 0.05,
          aligned["loudness_matching"])
    check("aligned same-source pair is comparison-ready",
          aligned["comparison_ready"] is True
          and aligned["confidence"]["level"] == "high",
          aligned["confidence"])
    check("aligned/loudness-matched tonal deltas stay near zero",
          max(abs(v["difference_db"]) for v in aligned["band_deltas"].values()) < 0.25,
          aligned["band_deltas"])
    check("comparison provenance hashes both immutable inputs",
          len(aligned["provenance"]["reference"]["sha256"]) == 64
          and len(aligned["provenance"]["target"]["sha256"]) == 64
          and aligned["loudness_matching"]["source_files_modified"] is False,
          aligned["provenance"])
    check("comparison limitations prevent artistic overclaim",
          any("artistic verdict" in item for item in aligned["limitations"]),
          aligned["limitations"])

    repeated = audio.compare(
        aligned_ref_path,
        aligned_target_path,
        max_alignment_seconds=0.5,
    )
    check("alignment is deterministic across repeated runs",
          repeated["alignment"] == aligned["alignment"]
          and repeated["loudness_matching"] == aligned["loudness_matching"]
          and repeated["band_deltas"] == aligned["band_deltas"],
          (aligned["alignment"], repeated["alignment"]))

    unrelated_a = compare_rng.normal(0.0, 0.1, SR * 3)
    unrelated_b = np.random.default_rng(77).normal(0.0, 0.1, SR * 3)
    unrelated = audio.compare(
        write("unrelated_a.wav", unrelated_a),
        write("unrelated_b.wav", unrelated_b),
        max_alignment_seconds=0.25,
    )
    check("unrelated material fails closed",
          unrelated["comparison_ready"] is False
          and unrelated["alignment"]["confidence"]["level"] == "low",
          (unrelated["alignment"], unrelated["confidence"]))
    check("weak alignment is disclosed as a limitation",
          any("ambiguous" in item for item in unrelated["limitations"])
          and any("do not rank" in item
                  for item in unrelated["confidence"]["basis"]),
          (unrelated["limitations"], unrelated["confidence"]))

    guarded_gain = audio.compare(
        p,
        write("forty_db_quieter.wav", sine(1000, amp=0.005)),
        max_alignment_seconds=0.0,
    )
    check("pathological loudness-match gain is refused",
          guarded_gain["loudness_matching"]["applied"] is False
          and guarded_gain["comparison_ready"] is False,
          guarded_gain["loudness_matching"])
    check("refused loudness match still returns a readable report",
          any("not applied" in item for item in guarded_gain["readings"]),
          guarded_gain["readings"])

    print("\n-- synchronized vocal / instrumental masking --")
    context_vocal = sine(1000, secs=4, amp=0.25)
    masking_instrument = (
        sine(1000, secs=4, amp=0.4) + sine(100, secs=4, amp=0.08)
    )
    clear_instrument = sine(100, secs=4, amp=0.4)
    context_vocal_path = write("context_vocal.wav", context_vocal)
    masking_instrument_path = write("masking_instrument.wav", masking_instrument)
    clear_instrument_path = write("clear_instrument.wav", clear_instrument)
    masked_context = audio.analyze_masking(
        context_vocal_path, masking_instrument_path
    )
    clear_context = audio.analyze_masking(context_vocal_path, clear_instrument_path)
    check("sample-synchronous context passes the readiness gate",
          masked_context["context_ready"] is True
          and masked_context["readiness_reasons"] == [],
          masked_context)
    check("same-band instrumental produces high possible masking",
          masked_context["masking"]["possible_masking_index"] > 0.8,
          masked_context["masking"])
    check("mid band is identified as the likely conflict",
          "mid" in masked_context["masking"]["candidate_bands"]
          and masked_context["masking"]["bands"]["mid"]["possible_masking_score"] > 0.8,
          masked_context["masking"])
    check("spectrally separate instrumental scores much lower",
          clear_context["masking"]["possible_masking_index"] < 0.1
          and clear_context["masking"]["bands"]["mid"]["possible_masking_score"] < 0.1,
          clear_context["masking"])

    anti_phase_mask = np.column_stack([masking_instrument, -masking_instrument])
    anti_phase_context = audio.analyze_masking(
        context_vocal_path, write("masking_instrument_antiphase.wav", anti_phase_mask)
    )
    check("anti-phase stereo energy does not disappear in masking analysis",
          anti_phase_context["masking"]["possible_masking_index"] > 0.8,
          anti_phase_context["masking"])

    mismatched_context = audio.analyze_masking(
        context_vocal_path,
        write("short_context_instrument.wav", sine(1000, secs=3, amp=0.4)),
    )
    check("mismatched stems fail closed without masking metrics",
          mismatched_context["context_ready"] is False
          and "masking" not in mismatched_context
          and any("frame counts differ" in reason
                  for reason in mismatched_context["readiness_reasons"]),
          mismatched_context)
    check("masking report binds both immutable input hashes",
          len(masked_context["vocal"]["sha256"]) == 64
          and len(masked_context["instrumental"]["sha256"]) == 64
          and masked_context["provenance"]["masking_analyzer_version"]
          == audio.MASKING_ANALYSIS_VERSION,
          masked_context["provenance"])
    check("masking limitations prevent an artistic overclaim",
          any("not proof" in item for item in masked_context["limitations"])
          and any("not automatic" in item for item in masked_context["readings"]),
          (masked_context["limitations"], masked_context["readings"]))

    print("\n-- error handling --")
    try:
        audio.load("/nope/missing.wav")
        check("missing file raises", False)
    except audio.AudioError as e:
        check("missing file raises clearly", "No such audio file" in str(e), str(e))

    tiny = write("tiny.wav", np.zeros(100))
    try:
        audio.analyze(tiny)
        check("very short file handled", True)
    except audio.AudioError as e:
        check("very short file raises clearly", "short" in str(e).lower(), str(e))

    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
