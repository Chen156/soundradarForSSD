"""Analyse real gameplay recordings (event_log.csv + recordings/*.wav).

Computes per-kind feature statistics and per-class spectra, then prints
calibration recommendations (band, sensitivity, max_amp, thresholds, bias).
Run with any python that has numpy:
    python tools/analyze_recordings.py <data_dir>
"""

import csv
import glob
import os
import statistics
import sys
import wave

import numpy as np

SR = 48000


def pct(a, q):
    return float(np.percentile(a, q)) if len(a) else float("nan")


def stat(a):
    if not len(a):
        return {}
    return {
        "n": len(a),
        "mean": float(np.mean(a)),
        "std": float(np.std(a)),
        "p5": pct(a, 5), "p25": pct(a, 25), "p50": pct(a, 50),
        "p75": pct(a, 75), "p95": pct(a, 95),
    }


def band_energy_frac(spec_pow, freqs, lo, hi):
    sel = (freqs >= lo) & (freqs <= hi)
    return float(spec_pow[sel].sum() / (spec_pow.sum() + 1e-20))


def snippet_spectrum(path):
    """Mean power spectrum of a WAV snippet (first 2 channels)."""
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        ch = w.getnchannels()
        raw = w.readframes(n)
    x = np.frombuffer(raw, dtype=np.int16).reshape(n, ch).astype(np.float64) / 32768.0
    mono = x.mean(axis=1)
    nfft = 1024
    hop = 512
    acc = np.zeros(nfft // 2 + 1)
    cnt = 0
    for i in range(0, len(mono) - nfft, hop):
        seg = mono[i:i + nfft] * np.hanning(nfft)
        acc += np.abs(np.fft.rfft(seg)) ** 2
        cnt += 1
    if cnt == 0:
        return None
    freqs = np.fft.rfftfreq(nfft, d=1.0 / SR)
    return acc / cnt, freqs


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else r"D:\Program Files\SoundRadar360"
    csv_path = os.path.join(base, "event_log.csv")
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    print(f"== event_log.csv: {len(rows)} events ==")

    kinds = {"0": "other", "1": "footstep", "2": "gunshot", "3": "self",
             "4": "silent"}
    by_kind = {k: [] for k in kinds}
    for r in rows:
        by_kind[r["kind"]].append(r)

    for k, name in kinds.items():
        rs = by_kind[k]
        print(f"\n--- kind {k} ({name}): n={len(rs)}")
        for col in ("snr_db", "crest_db", "hf_frac", "lf_frac", "gmax",
                    "level_db", "distance", "tilt_db"):
            vals = [float(r[col]) for r in rs if r[col] not in ("", "nan")]
            s = stat(vals)
            if s:
                print(f"  {col:10s} mean={s['mean']:8.2f} std={s['std']:7.2f} "
                      f"p5={s['p5']:8.2f} p50={s['p50']:8.2f} p95={s['p95']:8.2f}")

    # Own-step rhythm check (kind 3 during movement)
    self_rs = sorted(by_kind["3"], key=lambda r: float(r["t"]))
    iv = [float(self_rs[i + 1]["t"]) - float(self_rs[i]["t"])
          for i in range(len(self_rs) - 1)]
    iv = [v for v in iv if 0.15 < v < 1.5]
    if iv:
        print(f"\nown-sound inter-event intervals: n={len(iv)} "
              f"median={statistics.median(iv):.3f}s")

    # Front/back tilt check: does tilt separate front from rear?
    fs = [r for r in by_kind["1"]]
    front = [float(r["tilt_db"]) for r in fs if abs(float(r["azimuth"])) < 60]
    rear = [float(r["tilt_db"]) for r in fs if abs(float(r["azimuth"])) > 120]
    if front and rear:
        print(f"\ntilt front (|az|<60): mean={np.mean(front):.1f} "
              f"(n={len(front)}), rear (|az|>120): mean={np.mean(rear):.1f} "
              f"(n={len(rear)})")

    # ── WAV spectra per class ──────────────────────────────────────────
    rec_dir = os.path.join(base, "recordings")
    sessions = sorted(glob.glob(os.path.join(rec_dir, "session_*")))
    print(f"\n== recordings: {len(sessions)} sessions ==")
    for cls, label in (("footstep", "footstep"), ("gunshot", "gunshot"),
                       ("self", "self")):
        wavs = sorted(glob.glob(os.path.join(rec_dir, "session_*",
                                             f"{cls}_*.wav")))[:60]
        specs = []
        for w in wavs:
            sp = snippet_spectrum(w)
            if sp is not None:
                specs.append(sp[0])
        if not specs:
            print(f"\n[{cls}] no files")
            continue
        mean = np.mean(specs, axis=0)
        freqs = snippet_spectrum(wavs[0])[1]
        total = mean.sum() + 1e-20
        # energy-weighted band edges (5% .. 95% cumulative energy)
        cum = np.cumsum(mean) / total
        lo = freqs[np.searchsorted(cum, 0.05)]
        hi = freqs[np.searchsorted(cum, 0.95)]
        # fractional energy in fixed bands
        bands = {
            "<120Hz": band_energy_frac(mean, freqs, 0, 120),
            "120-700": band_energy_frac(mean, freqs, 120, 700),
            "700-3500": band_energy_frac(mean, freqs, 700, 3500),
            "3.5k-8k": band_energy_frac(mean, freqs, 3500, 8000),
            ">8k": band_energy_frac(mean, freqs, 8000, 20000),
        }
        print(f"\n[{cls}] n={len(specs)}  5-95% energy band: {lo:.0f}-{hi:.0f} Hz")
        print("  band fractions:", {k: round(v, 3) for k, v in bands.items()})

    # ── Calibration recommendations ────────────────────────────────────
    print("\n== CALIBRATION RECOMMENDATIONS ==")
    fs = by_kind["1"]; gs = by_kind["2"]
    f_gmax = [float(r["gmax"]) for r in fs]
    g_gmax = [float(r["gmax"]) for r in gs]
    if f_gmax and g_gmax:
        # max_amp: separate footstep vs gunshot loudness (log space)
        f_db = np.log10(f_gmax); g_db = np.log10(g_gmax)
        print(f"footstep gmax p5..p95: {pct(f_gmax,5):.4f} .. {pct(f_gmax,95):.4f}")
        print(f"gunshot  gmax p5..p95: {pct(g_gmax,5):.4f} .. {pct(g_gmax,95):.4f}")
    f_crest = [float(r["crest_db"]) for r in fs]
    g_crest = [float(r["crest_db"]) for r in gs]
    f_hf = [float(r["hf_frac"]) for r in fs]
    g_hf = [float(r["hf_frac"]) for r in gs]
    if f_crest and g_crest:
        print(f"crest: footstep {np.mean(f_crest):.1f}±{np.std(f_crest):.1f} dB | "
              f"gunshot {np.mean(g_crest):.1f}±{np.std(g_crest):.1f} dB")
        # optimal threshold: midpoint of means in log/linear space
        thr = (np.percentile(f_crest, 95) + np.percentile(g_crest, 5)) / 2
        print(f"  -> GUNSHOT crest threshold ~ {thr:.1f} dB "
              f"(f p95={np.percentile(f_crest,95):.1f}, g p5={np.percentile(g_crest,5):.1f})")
    if f_hf and g_hf:
        print(f"hf_frac: footstep {np.mean(f_hf):.2f}±{np.std(f_hf):.2f} | "
              f"gunshot {np.mean(g_hf):.2f}±{np.std(g_hf):.2f}")
        thr = (np.percentile(f_hf, 95) + np.percentile(g_hf, 5)) / 2
        print(f"  -> GUNSHOT hf threshold ~ {thr:.2f} "
              f"(f p95={np.percentile(f_hf,95):.2f}, g p5={np.percentile(g_hf,5):.2f})")
    snr_fs = [float(r["snr_db"]) for r in fs]
    if snr_fs:
        print(f"footstep snr: p5={pct(snr_fs,5):.1f} p50={pct(snr_fs,50):.1f} "
              f"p95={pct(snr_fs,95):.1f} dB")
    # ambient reference: level_db of the quietest gated events (kind 0)
    oth = [float(r["level_db"]) for r in by_kind["0"]]
    if oth:
        print(f"other/misc events level: p25={pct(oth,25):.1f} dB "
              f"(n={len(oth)})")


if __name__ == "__main__":
    main()
