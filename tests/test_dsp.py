"""Unit tests for the SSD-fork DSP (direction.py).

Run with:  python -m unittest tests.test_dsp -v
(or: pytest tests/test_dsp.py)

The synthetic signals model a binaural/HRTF stereo stream:
  * flat broadband noise = the "ambient" the baselines learn from,
  * a high-shelf above 3.5 kHz   = HRTF "front" (brighter),
  * a high-cut above 3.5 kHz     = HRTF "rear" (darker, head shadow),
  * a brilliance boost 9.5-14k   = HRTF "above" (notch shifts up),
  * a brilliance cut             = HRTF "below",
  * an impulsive noise burst     = a gunshot (huge crest, broadband HF),
  * a low-passed dull burst      = a footstep (low crest, dark thump).
"""

import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from direction import (KIND_FOOTSTEP, KIND_GUNSHOT, KIND_OTHER, KIND_SELF,
                       KIND_SILENT, KIND_THUMP, NoiseFloor, StereoLocalizer,
                       SurroundLocalizer, angle_diff, band_energies, band_rms,
                       classify_kind, distance_estimate, event_features,
                       map_intensity, stereo_angle, stereo_lateral,
                       surround_azimuth, wrap_180)

SR = 48000
N = 2400            # 50 ms chunks, same as the capture thread
DT = N / SR


def rng(seed=1):
    return np.random.default_rng(seed)


def white_noise(n=N, rms=0.02, seed=1):
    x = rng(seed).standard_normal(n)
    return x / np.sqrt(np.mean(x ** 2)) * rms


def shelf(x, sr=SR, cutoff=3500.0, gain_db=0.0):
    """Simple brick-wall shelf: boost/cut everything above `cutoff`."""
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), d=1.0 / sr)
    g = np.where(freqs >= cutoff, 10 ** (gain_db / 20.0), 1.0)
    return np.fft.irfft(spec * g, n=len(x))


def lowpass(x, sr=SR, cutoff=500.0):
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), d=1.0 / sr)
    g = np.where(freqs <= cutoff, 1.0, 0.0)
    return np.fft.irfft(spec * g, n=len(x))


def band_shelf(x, sr=SR, lo=0.0, hi=SR / 2, gain_db=0.0):
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), d=1.0 / sr)
    g = np.where((freqs >= lo) & (freqs <= hi), 10 ** (gain_db / 20.0), 1.0)
    return np.fft.irfft(spec * g, n=len(x))


def stereo(x, pan):
    """pan -1..+1: pan the mono signal x across two channels by amplitude."""
    left = np.sqrt((1.0 - pan) / 2.0)
    right = np.sqrt((1.0 + pan) / 2.0)
    return np.stack([x * left, x * right], axis=1)


def burst(kind="shot", gate_rms=0.05, seed=1, sr=SR):
    """A single-chunk 'shot' (impulsive, broadband) or 'step' (dull thump
    with a smooth 12 ms attack - a real footstep has no click), scaled so its
    90-7000 Hz band RMS equals gate_rms."""
    t = np.arange(N) / sr
    if kind == "shot":
        x = rng(seed).standard_normal(N) * np.exp(-t / 0.012)
    else:
        x = lowpass(rng(seed).standard_normal(N), SR, 400)
        atk = int(0.012 * sr)
        env = np.exp(-t / 0.025)
        env[:atk] *= 0.5 - 0.5 * np.cos(np.pi * np.arange(atk) / atk)
        x = x * env
    cur = band_rms(x, sr, 90, 7000)[0]
    return x * (gate_rms / (cur + 1e-12))


# ── Pure functions ──────────────────────────────────────────────────────────

class TestPureFunctions(unittest.TestCase):
    def test_band_rms_fullband_equals_rms(self):
        x = white_noise(rms=0.1)
        data = np.stack([x, x * 0.5], axis=1)
        full = band_rms(data, SR, 20, 20000)
        self.assertAlmostEqual(full[0], np.sqrt(np.mean(x ** 2)), places=4)
        self.assertAlmostEqual(full[1], np.sqrt(np.mean((x * 0.5) ** 2)), places=4)

    def test_band_energies_isolates_tone(self):
        t = np.arange(N) / SR
        x = np.sin(2 * np.pi * 1000 * t) * 0.1
        e = band_energies(np.stack([x, x], axis=1), SR)
        self.assertGreater(e["mid"][0], 1e-4)
        self.assertLess(e["low"][0], 1e-6)
        self.assertLess(e["brill"][0], 1e-6)

    def test_stereo_lateral_directions(self):
        self.assertAlmostEqual(stereo_lateral(0.7, 0.7), 90.0, places=2)
        self.assertAlmostEqual(stereo_lateral(-0.7, -0.7), -90.0, places=2)
        self.assertAlmostEqual(stereo_lateral(0.0, 0.0), 0.0, places=2)
        # Half the 90° balance = 30° (arcsin head-shadow model)
        self.assertAlmostEqual(stereo_lateral(0.35, 0.35), 30.0, places=1)
        # dead-zone: tiny balances (measurement noise) pin to centre
        self.assertAlmostEqual(stereo_lateral(0.02, 0.02), 0.0, places=2)

    def test_stereo_angle_classic(self):
        self.assertAlmostEqual(stereo_angle(0.0, 1.0), 90.0, places=2)
        self.assertAlmostEqual(stereo_angle(1.0, 0.0), -90.0, places=2)

    def test_surround_azimuth_51(self):
        # channel order FL FR FC LFE BL BR
        az, _ = surround_azimuth([0.0, 1.0, 0.0, 0.0, 0.0, 0.0])
        # FR alone: between centre and side (atan2(1,1) = 45°)
        self.assertAlmostEqual(az, 45.0, places=1)
        az, _ = surround_azimuth([0.0, 0.0, 0.0, 0.0, 1.0, 0.0])
        self.assertAlmostEqual(abs(az), 180.0, places=1)
        az, _ = surround_azimuth([0.0, 0.0, 0.0, 0.0, 0.5, 0.5])
        self.assertAlmostEqual(abs(az), 180.0, places=1)

    def test_surround_azimuth_714_height(self):
        ch = [0.0] * 12
        ch[8] = 1.0                     # TFL
        az, elev = surround_azimuth(ch)
        self.assertGreater(elev, 0.5)
        ch = [0.0] * 12
        ch[0] = 1.0                     # FL only
        _, elev = surround_azimuth(ch)
        self.assertLess(elev, 0.2)

    def test_angle_diff_wraps(self):
        self.assertAlmostEqual(angle_diff(179, -179), 2.0, places=1)
        self.assertAlmostEqual(angle_diff(0, 180), 180.0, places=1)

    def test_wrap_180(self):
        self.assertEqual(wrap_180(190), -170)
        self.assertEqual(wrap_180(-190), 170)
        self.assertIn(wrap_180(180), (180, -180))   # same angle, either rep ok

    def test_map_intensity(self):
        self.assertGreater(map_intensity(20), map_intensity(6))
        self.assertGreaterEqual(map_intensity(30), 0.99)
        self.assertLessEqual(map_intensity(0), 0.05)
        self.assertGreater(map_intensity(6, 2.0), map_intensity(6, 1.0))

    def test_classify_shot_vs_step(self):
        shot = event_features(
            np.stack([burst("shot"), burst("shot")], axis=1),
            band_energies(np.stack([burst("shot"), burst("shot")], axis=1), SR),
            band_rms(np.stack([burst("shot"), burst("shot")], axis=1), SR, 90, 7000),
            SR)
        step = event_features(
            np.stack([burst("step"), burst("step")], axis=1),
            band_energies(np.stack([burst("step"), burst("step")], axis=1), SR),
            band_rms(np.stack([burst("step"), burst("step")], axis=1), SR, 90, 7000),
            SR)
        self.assertGreater(shot["crest_db"], step["crest_db"] + 4)
        self.assertGreater(shot["hf_frac"], step["hf_frac"] * 3)
        self.assertEqual(classify_kind(shot), KIND_GUNSHOT)
        # a dull transient is provisional THUMP: rhythm resolves it later
        self.assertEqual(classify_kind(step), KIND_THUMP)

    def test_distance_monotonic(self):
        self.assertLess(distance_estimate(30), distance_estimate(10))
        self.assertEqual(distance_estimate(40), 0.0)
        self.assertEqual(distance_estimate(0), 1.0)


class TestNoiseFloor(unittest.TestCase):
    def test_rises_to_sustained_ambience(self):
        # Constant "rain" must become the floor (and thus stop being an event).
        f = NoiseFloor(init=-50.0, window=0.8, tau=0.25)
        for _ in range(30):
            f.update(-34.0, DT)
        self.assertGreater(f.value, -36.0)

    def test_short_events_do_not_raise_floor(self):
        # Bursts between silence must not move the floor up.
        f = NoiseFloor(init=-50.0, window=0.8, tau=0.25)
        for _ in range(30):
            f.update(-90.0, DT)          # silence baseline
        for _ in range(4):
            f.update(-20.0, DT)          # a short burst
        for _ in range(20):
            f.update(-90.0, DT)
        self.assertLess(f.value, -70.0)


# ── StereoLocalizer end-to-end ──────────────────────────────────────────────

def learn_ambient(loc, chunks=30):
    """Teach the localizer the game's ambient character (flat noise)."""
    for i in range(chunks):
        x = white_noise(rms=0.002, seed=100 + i)
        loc.feed(np.stack([x, x], axis=1), SR, 20, 20000, 0.005, 1.0, 1.0)


def feed_chunks(loc, data_chunks, **kw):
    """Feed a sequence of (frames, 2) arrays with quiet gaps in between (real
    sounds are bursts separated by silence, and the onset gate needs ~120 ms
    of quiet); a quiet tail at the end flushes the pending burst verification.
    Returns the last non-None result."""
    res = None
    sens = kw.get("sensitivity", 0.005)
    for i, d in enumerate(data_chunks):
        r = loc.feed(d, SR, kw.get("gate_low", 20), kw.get("gate_high", 20000),
                     sens, kw.get("max_amp", 1.0),
                     kw.get("boost", 1.0), adaptive=kw.get("adaptive", True),
                     suppress_own_steps=kw.get("suppress_steps", True),
                     suppress_own_shots=kw.get("suppress_shots", True))
        if r is not None:
            res = r
        if i < len(data_chunks) - 1:
            for j in range(3):      # 0.15 s quiet gap (onset gate)
                amb = white_noise(rms=0.0015, seed=9000 + i * 10 + j)
                r = loc.feed(mono_stack(amb), SR, 20, 20000, sens, 1.0, 1.0)
                if r is not None:
                    res = r
    # quiet tail: flush the pending burst-decay verification
    for j in range(3):
        amb = white_noise(rms=0.0015, seed=9500 + j)
        r = loc.feed(mono_stack(amb), SR, 20, 20000, sens, 1.0, 1.0)
        if r is not None:
            res = r
    return res


def mono_stack(x):
    return np.stack([x, x], axis=1)


class TestStereoLocalizer(unittest.TestCase):
    def _feed_event(self, loc, x, **kw):
        return feed_chunks(loc, [mono_stack(x)], **kw)

    def _feed_event_chunks(self, loc, x, chunks, **kw):
        return feed_chunks(loc, [mono_stack(x)] * chunks, **kw)

    def test_front_vs_rear_azimuth(self):
        # Front event: brighter than the learned ambient baseline. A real sound
        # spans several 50ms chunks, so feed it for a few.
        loc = StereoLocalizer()
        learn_ambient(loc)
        x = shelf(white_noise(rms=0.02, seed=7), gain_db=8.0)
        res = self._feed_event_chunks(loc, x, 6)
        self.assertLess(abs(res[0] - 0.0), 30.0, f"front azimuth {res[0]}")

        # Rear event: darker (head shadow) than ambient.
        loc2 = StereoLocalizer()
        learn_ambient(loc2)
        x2 = shelf(white_noise(rms=0.02, seed=8), gain_db=-8.0)
        res2 = self._feed_event_chunks(loc2, x2, 6)
        self.assertGreater(abs(res2[0]), 150.0, f"rear azimuth {res2[0]}")

    def test_left_right_pan(self):
        x = shelf(white_noise(rms=0.02, seed=9), gain_db=8.0)   # front cue
        loc = StereoLocalizer()
        learn_ambient(loc)
        res = None
        for _ in range(4):
            r = loc.feed(stereo(x, -1.0), SR, 20, 20000, 0.005, 1.0, 1.0)
            if r is not None:
                res = r
        az_l = res[0]
        loc = StereoLocalizer()          # fresh state for the other side
        learn_ambient(loc)
        res = None
        for _ in range(4):
            r = loc.feed(stereo(x, 1.0), SR, 20, 20000, 0.005, 1.0, 1.0)
            if r is not None:
                res = r
        az_r = res[0]
        self.assertLess(az_l, -60.0, f"hard-left azimuth {az_l}")
        self.assertGreater(az_r, 60.0, f"hard-right azimuth {az_r}")

    def test_elevation_up_down(self):
        loc = StereoLocalizer()
        learn_ambient(loc)
        x = band_shelf(white_noise(rms=0.02, seed=11), lo=9500, hi=14000, gain_db=8.0)
        res = self._feed_event_chunks(loc, x, 6)
        self.assertGreater(res[1], 0.5, f"up elevation {res[1]}")
        loc2 = StereoLocalizer()
        learn_ambient(loc2)
        x2 = band_shelf(white_noise(rms=0.02, seed=12), lo=9500, hi=14000, gain_db=-8.0)
        res2 = self._feed_event_chunks(loc2, x2, 6)
        self.assertLess(res2[1], -0.5, f"down elevation {res2[1]}")

    def test_adaptive_gate_finds_steps_above_rain(self):
        """Rain loud enough to cross the fixed threshold must NOT fire; a
        footstep only 3x the rain level must. (Feed rain directly - the
        feed_chunks helper's quiet flush would unrealistically sink the
        floor below the rain level.)"""
        loc = StereoLocalizer()
        # Rain: continuous ambience above sensitivity.
        fired_during_rain = 0
        for i in range(60):
            x = white_noise(rms=0.02, seed=200 + i)
            if loc.feed(mono_stack(x), SR, 20, 20000, 0.005, 1.0, 1.0) is not None:
                fired_during_rain += 1
        self.assertLessEqual(fired_during_rain, 14)   # only the floor warm-up

        # Footstep: a short burst 3x the rain amplitude.
        step = white_noise(rms=0.06, seed=400)
        res = loc.feed(mono_stack(step), SR, 20, 20000, 0.005, 1.0, 1.0)
        self.assertIsNotNone(res)
        self.assertGreater(res[2], 0.2, f"step intensity {res[2]}")

    def test_adaptive_gate_off_fixed_threshold(self):
        loc = StereoLocalizer()
        learn_ambient(loc)
        x = white_noise(rms=0.002, seed=500)      # below sensitivity: nothing
        self.assertIsNone(self._feed_event(loc, x))
        x = white_noise(rms=0.02, seed=501)       # above: fires without gate
        self.assertIsNotNone(self._feed_event(loc, x, adaptive=False))

    def test_loud_sound_becomes_gunshot_not_dropped(self):
        """Above max_amp is no longer dropped: it is classified as gunfire.
        (Own-shot suppression must be off here, or a centred loud shot is
        correctly flagged as the player's own.)"""
        loc = StereoLocalizer()
        learn_ambient(loc)
        x = burst("shot", gate_rms=0.5, seed=600)   # gunshot-level
        res = self._feed_event(loc, x, max_amp=0.1, suppress_shots=False)
        self.assertIsNotNone(res)
        self.assertEqual(res[4], KIND_GUNSHOT)

    def test_loud_dull_boom_is_combat_sound(self):
        """Above max_amp, ANY loud event is a combat sound: grenades and
        explosions (frag / molotov / stun) are not always HF-rich."""
        loc = StereoLocalizer()
        learn_ambient(loc)
        x = lowpass(white_noise(rms=0.3, seed=90), SR, 400)   # dull boom
        x = x / np.sqrt(np.mean(x ** 2)) * 0.3                # re-normalize
        res = self._feed_event(loc, x, max_amp=0.1, suppress_shots=False)
        self.assertEqual(res[4], KIND_GUNSHOT)

    def test_front_bias_flips_a_mild_rear_cue(self):
        # A mild (-3 dB) rear cue: without bias it stays in the rear half,
        # with +1 bias (adds 4 dB to the tilt) it resolves to the front.
        x = shelf(white_noise(rms=0.02, seed=8), gain_db=-3.0)
        loc = StereoLocalizer(front_bias=0.0)
        learn_ambient(loc)
        res = self._feed_event_chunks(loc, x, 6)
        self.assertGreater(res[0], 90.0, f"no bias: expect rear half, got {res[0]}")
        loc2 = StereoLocalizer(front_bias=1.0)
        learn_ambient(loc2)
        res2 = self._feed_event_chunks(loc2, x, 6)
        self.assertLess(res2[0], 90.0, f"+1 bias: expect front half, got {res2[0]}")

    # ── Kinds, distance, own-sound suppression ────────────────────────
    def test_kind_shot_vs_step(self):
        loc = StereoLocalizer()
        learn_ambient(loc)
        res = self._feed_event_chunks(loc, burst("shot", gate_rms=0.02, seed=20),
                                      3, suppress_shots=False)
        self.assertEqual(res[4], KIND_GUNSHOT)
        # a dull transient alone is misc; a REGULAR train becomes footsteps
        # (front HRTF cue so the azimuth resolves to centred, like real
        # own-sounds do)
        loc2 = StereoLocalizer()
        learn_ambient(loc2)
        x = shelf(burst("step", gate_rms=0.02, seed=21), gain_db=8.0)
        res2 = self._feed_event_chunks(loc2, x, 4)
        self.assertEqual(res2[4], KIND_OTHER)      # isolated thump = misc
        kinds = []
        for i in range(5):
            r = feed_chunks(loc2, [mono_stack(x)], sensitivity=0.005)
            if r is not None:
                kinds.append(r[4])
            for j in range(8):      # 0.4 s cadence gaps
                amb = white_noise(rms=0.0015, seed=3100 + i * 10 + j)
                loc2.feed(mono_stack(amb), SR, 20, 20000, 0.005, 1.0, 1.0)
        self.assertEqual(kinds[-1], KIND_FOOTSTEP, f"kinds {kinds}")

    def test_distance_louder_is_nearer(self):
        loc = StereoLocalizer()
        learn_ambient(loc)
        near = self._feed_event_chunks(loc, burst("step", gate_rms=0.15, seed=30), 3)
        far = self._feed_event_chunks(loc, burst("step", gate_rms=0.015, seed=31), 3)
        self.assertLess(near[3], far[3],
                        f"near dist {near[3]:.2f} should be < far dist {far[3]:.2f}")

    def test_own_shot_suppressed(self):
        x = burst("shot", gate_rms=0.5, seed=40)   # way above max_amp
        loc = StereoLocalizer()
        learn_ambient(loc)
        res = self._feed_event(loc, x, max_amp=0.1, suppress_shots=True)
        self.assertEqual(res[4], KIND_SELF)
        loc2 = StereoLocalizer()
        learn_ambient(loc2)
        res2 = self._feed_event(loc2, x, max_amp=0.1, suppress_shots=False)
        self.assertEqual(res2[4], KIND_GUNSHOT)

    def test_own_shot_at_18deg_offsets_suppressed(self):
        """Ground truth from the user's first-person recordings: the game
        pans your own shots ~±17-18° off-centre. The widened own-shot window
        (±25°) must still suppress them, while a 30° enemy shot stays red."""
        x = burst("shot", gate_rms=0.5, seed=41)
        loc = StereoLocalizer()
        learn_ambient(loc)
        res = loc.feed(stereo(x, 0.22), SR, 20, 20000, 0.005, 0.1, 1.0)
        self.assertEqual(res[4], KIND_SELF, f"kind {res[4]}, az {res[0]:.1f}")
        loc2 = StereoLocalizer()
        learn_ambient(loc2)
        res2 = loc2.feed(stereo(x, 0.5), SR, 20, 20000, 0.005, 0.1, 1.0)
        self.assertEqual(res2[4], KIND_GUNSHOT, f"kind {res2[4]}, az {res2[0]:.1f}")

    def test_own_rhythmic_steps_suppressed(self):
        """Centred rhythmic footsteps latch as the player's own (rhythm
        alone - the loudness bar was removed because running raises the
        noise floor). Needs a few steps: the thump rhythm first has to
        resolve them as footsteps, then the own-step cadence latches."""
        loc = StereoLocalizer()
        learn_ambient(loc)
        silence = np.zeros((N, 2), dtype=np.float32)
        step = burst("step", gate_rms=0.1, seed=50)
        results = []
        for i in range(6):
            results.append(feed_chunks(loc, [mono_stack(step)]))
            # ~0.4 s gap between steps (walk cadence)
            for _ in range(8):
                loc.feed(silence, SR, 20, 20000, 0.005, 1.0, 1.0)
        kinds = [r[4] for r in results]
        self.assertEqual(kinds[-1], KIND_SELF, f"step kinds {kinds}")

    def test_single_centred_thump_is_misc_not_suppressed(self):
        """One isolated QUIETER centred dull thump is misc (hit/door), not a
        footstep and not suppressed (a LOUD centred one is own fire)."""
        loc = StereoLocalizer()
        learn_ambient(loc)
        step = shelf(burst("step", gate_rms=0.03, seed=60), gain_db=8.0)
        res = feed_chunks(loc, [mono_stack(step)] * 4)
        self.assertEqual(res[4], KIND_OTHER)

    def test_off_centre_quiet_step_is_silent_threat(self):
        """A quiet footstep from an OFF-CENTRE direction (enemy creep) gets
        the dedicated silent-step threat kind."""
        loc = StereoLocalizer()
        learn_ambient(loc)
        x = burst("step", gate_rms=0.003, seed=70)
        res = feed_chunks(loc, [stereo(x, 0.5)] * 2, sensitivity=0.001)
        self.assertIsNotNone(res)
        self.assertEqual(res[4], KIND_SILENT)
        self.assertGreaterEqual(res[2], 0.5)   # always bright, never missed

    def test_own_silent_step_centred_is_displayed(self):
        """Per the user's request, the player's OWN silent-walk steps must
        show on the radar in real time (purple threat marker), never
        suppressed - and pinned to the front centre."""
        loc = StereoLocalizer()
        learn_ambient(loc)
        x = burst("step", gate_rms=0.003, seed=74)
        res = feed_chunks(loc, [mono_stack(x)] * 2, sensitivity=0.001)
        self.assertIsNotNone(res)
        self.assertEqual(res[4], KIND_SILENT)
        self.assertAlmostEqual(res[0], 0.0, delta=5.0)   # pinned to front

    def test_own_steps_with_ild_bias_suppressed(self):
        """Ground truth from the training-range recordings: first-person
        footsteps carry a small right bias (ILD +1.2..2.3 dB ≈ 12-21°).
        The widened ±25° own-step window must still suppress them."""
        loc = StereoLocalizer()
        learn_ambient(loc)
        step = burst("step", gate_rms=0.1, seed=52)
        silence = np.zeros((N, 2), dtype=np.float32)
        results = []
        for i in range(6):
            results.append(feed_chunks(loc, [stereo(step, 0.2)]))   # lat ≈ 17°
            for _ in range(8):
                loc.feed(silence, SR, 20, 20000, 0.005, 1.0, 1.0)
        kinds = [r[4] for r in results]
        self.assertEqual(kinds[-1], KIND_SELF, f"step kinds {kinds}")

    def test_loud_centred_thump_is_own_fire(self):
        """A LOUD centred dull thump is the player's own fire/impact (own
        gunfire is dull and sustained - the M4A1 ground truth) - suppressed,
        never shown as misc at their own position."""
        loc = StereoLocalizer()
        learn_ambient(loc)
        x = shelf(burst("step", gate_rms=0.08, seed=71), gain_db=8.0)
        res = self._feed_event_chunks(loc, x, 4, sensitivity=0.001)
        self.assertEqual(res[4], KIND_SELF)

    def test_quieter_centred_thump_is_misc(self):
        """A quieter centred thump (door/loot/reload/hit) stays misc."""
        loc = StereoLocalizer()
        learn_ambient(loc)
        x = shelf(burst("step", gate_rms=0.03, seed=72), gain_db=8.0)
        res = self._feed_event_chunks(loc, x, 4, sensitivity=0.001)
        self.assertEqual(res[4], KIND_OTHER)

    def test_sustained_loud_roar_is_combat(self):
        """A loud dull sound whose level never decays is automatic fire
        (M4A1 ground truth: the burst sustains with no quiet gaps) - it
        must emit as combat, not be dropped as an ambient swell."""
        loc = StereoLocalizer()
        learn_ambient(loc)
        roar = shelf(burst("step", gate_rms=0.07, seed=80), gain_db=6.0)
        res = None
        for _ in range(4):   # sustained: the level never drops
            r = loc.feed(stereo(roar, 0.5), SR, 20, 20000, 0.005, 1.0, 1.0)
            if r is not None:
                res = r
        self.assertIsNotNone(res, "sustained roar dropped as ambient")
        self.assertEqual(res[4], KIND_GUNSHOT, f"kind {res[4]}")

    def test_off_centre_isolated_thump_is_footstep(self):
        """An isolated dull thump from an OFF-CENTRE direction is enemy
        activity (single step / jump / door), not the player's own misc -
        own actions always sound from straight ahead."""
        loc = StereoLocalizer()
        learn_ambient(loc)
        x = shelf(burst("step", gate_rms=0.05, seed=75), gain_db=8.0)
        res = feed_chunks(loc, [stereo(x, 0.5)] * 1, sensitivity=0.005)
        self.assertIsNotNone(res)
        self.assertEqual(res[4], KIND_FOOTSTEP)

    def test_itd_delayed_sound_localizes_left(self):
        """A REAL interaural time difference (right channel delayed) must
        localize to the left - ITD is the reliable lateral cue for dull
        sounds whose ILD is near zero."""
        loc = StereoLocalizer()
        # Dull ambient so the front/back cue stays neutral and the azimuth
        # equals the lateral angle (the cue itself is synthetic here).
        amb = lowpass(white_noise(rms=0.002, seed=200), SR, 500)
        for _ in range(30):
            loc.feed(mono_stack(amb), SR, 20, 20000, 0.005, 1.0, 1.0)
        x = burst("step", gate_rms=0.05, seed=90)     # dull thump
        delay = 8                                       # ≈167 µs -> ~19° left
        pair = np.zeros((len(x), 2), dtype=np.float32)
        pair[:, 0] = x
        pair[delay:, 1] = x[:-delay]                    # right arrives later
        res = feed_chunks(loc, [pair])
        self.assertIsNotNone(res)
        self.assertLess(res[0], 0.0, f"azimuth {res[0]} should be left")

    def test_level_only_pan_keeps_ild(self):
        """A level-only pan (no time difference) must keep the ILD lateral -
        the ITD guard rejects its zero-lag 'confidence'."""
        loc = StereoLocalizer()
        learn_ambient(loc)
        x = shelf(burst("step", gate_rms=0.05, seed=91), gain_db=8.0)
        res = feed_chunks(loc, [stereo(x, 0.5)])
        self.assertIsNotNone(res)
        self.assertGreater(res[0], 20.0, f"azimuth {res[0]} should stay right")

    def test_machine_clank_is_misc_not_footstep(self):
        """A mid-band-heavy pulse with a ringing tail (training-range machine
        noise, hard-panned ±90°) must NOT become a footstep/silent-step: the
        accumulated spectrum sees the metallic ring that the onset chunk
        alone misses."""
        loc = StereoLocalizer()
        learn_ambient(loc)
        # onset chunk is dull (looks like a thump), the ring is mid-band
        b1 = band_shelf(white_noise(rms=0.06, seed=100), SR, 700, 3500, -8.0)
        r2 = band_shelf(white_noise(rms=0.03, seed=101), SR, 700, 3500, 12.0)
        r3 = band_shelf(white_noise(rms=0.015, seed=102), SR, 700, 3500, 12.0)
        res = None
        for d in (stereo(b1, 0.7), stereo(r2, 0.7), stereo(r3, 0.7)):
            r = loc.feed(d, SR, 20, 20000, 0.005, 1.0, 1.0)
            if r is not None:
                res = r
        r = loc.feed(mono_stack(white_noise(rms=0.001, seed=103)), SR, 20,
                     20000, 0.005, 1.0, 1.0)
        if r is not None:
            res = r
        self.assertIsNotNone(res)
        self.assertEqual(res[4], KIND_OTHER, f"kind {res[4]}")

    def test_onset_gate_blocks_continuous_ambient(self):
        """Continuous dull noise (wind/engine inside the gate band) emits
        NOTHING: its onset never decays, so the burst verification rejects
        it entirely. (Adaptive gate off so the floor can't mask the
        difference.)"""
        loc = StereoLocalizer()
        # quiet preamble: arms the onset gate
        for i in range(6):
            q = white_noise(rms=0.001, seed=85 + i)
            loc.feed(mono_stack(q), SR, 20, 20000, 0.005, 1.0, 1.0,
                     adaptive=False)
        amb = lowpass(white_noise(rms=0.02, seed=80), SR, 400)
        amb = amb / np.sqrt(np.mean(amb ** 2)) * 0.02   # re-normalize post-filter
        fired = 0
        for i in range(30):
            if loc.feed(mono_stack(amb), SR, 20, 20000, 0.005, 1.0, 1.0,
                        adaptive=False) is not None:
                fired += 1
        self.assertEqual(fired, 0)

    def test_onset_gate_allows_separate_bursts(self):
        """A burst emits once its decay is verified; a repeat within 120 ms
        is dropped; after a quiet gap the next burst fires again."""
        loc = StereoLocalizer()
        for i in range(6):    # quiet preamble arms the onset gate
            q = white_noise(rms=0.001, seed=86 + i)
            loc.feed(mono_stack(q), SR, 20, 20000, 0.005, 1.0, 1.0,
                     adaptive=False)
        step = burst("step", gate_rms=0.05, seed=81)
        r0 = loc.feed(mono_stack(step), SR, 20, 20000, 0.005, 1.0, 1.0,
                      adaptive=False)
        self.assertIsNone(r0)          # onset: pending decay verification
        tail = white_noise(rms=0.001, seed=89)
        r1 = loc.feed(mono_stack(tail), SR, 20, 20000, 0.005, 1.0, 1.0,
                      adaptive=False)
        self.assertIsNotNone(r1)       # decayed -> burst confirmed
        # immediate repeat within 120 ms: dropped (one event per burst)
        self.assertIsNone(loc.feed(mono_stack(step), SR, 20, 20000, 0.005, 1.0,
                                   1.0, adaptive=False))
        for _ in range(4):    # 0.2 s quiet gap
            amb = white_noise(rms=0.001, seed=88)
            loc.feed(mono_stack(amb), SR, 20, 20000, 0.005, 1.0, 1.0)
        self.assertIsNone(loc.feed(mono_stack(step), SR, 20, 20000, 0.005, 1.0,
                                   1.0, adaptive=False))   # pending again
        r2 = loc.feed(mono_stack(white_noise(rms=0.001, seed=90)), SR, 20,
                      20000, 0.005, 1.0, 1.0, adaptive=False)
        self.assertIsNotNone(r2)       # new onset confirmed

    def test_quiet_walker_cadence_stays_footstep(self):
        """A REGULAR train of quiet steps is a distant walker: the first two
        steps alert (purple), then the cadence confirms and they stay blue.
        (Low ambience between steps, not digital silence - a real map always
        has background sound, and silence would sink the floor unrealistically.)"""
        loc = StereoLocalizer()
        learn_ambient(loc)
        # A quiet step from a FIXED direction (front-right, bright HRTF cue)
        # so the azimuth stays stable across the train.
        step = shelf(burst("step", gate_rms=0.003, seed=72), gain_db=8.0)
        step = stereo(step, 0.5)
        kinds = []
        for i in range(5):
            res = feed_chunks(loc, [step] * 1, sensitivity=0.001)
            if res is not None:
                kinds.append(res[4])
            for j in range(10):      # 0.55 s gap = walking cadence
                amb = white_noise(rms=0.0015, seed=300 + i * 20 + j)
                loc.feed(mono_stack(amb), SR, 20, 20000, 0.001, 1.0, 1.0)
        self.assertEqual(kinds[0], KIND_SILENT)
        self.assertEqual(kinds[1], KIND_SILENT)
        self.assertNotIn(KIND_SILENT, kinds[2:], f"kinds {kinds}")

    def test_walker_cadence_tolerates_one_missed_step(self):
        """One missed detection doubles an interval (0.55s -> 1.1s): the
        cadence must survive it and keep resolving footsteps."""
        loc = StereoLocalizer()
        learn_ambient(loc)
        step = shelf(burst("step", gate_rms=0.003, seed=73), gain_db=8.0)
        step = stereo(step, 0.5)
        gaps = [10, 10, 20, 10, 10, 10, 10]  # quiet chunks; 20 = one missed slot
        kinds = []
        for i in range(7):
            res = feed_chunks(loc, [step] * 1, sensitivity=0.001)
            if res is not None:
                kinds.append(res[4])
            for j in range(gaps[i]):
                amb = white_noise(rms=0.0015, seed=400 + i * 30 + j)
                loc.feed(mono_stack(amb), SR, 20, 20000, 0.001, 1.0, 1.0)
        self.assertEqual(kinds[0], KIND_SILENT)
        self.assertEqual(kinds[1], KIND_SILENT)
        # from step 3 on, cadence holds even across the 1.1 s gap
        self.assertNotIn(KIND_SILENT, kinds[2:], f"kinds {kinds}")
        self.assertEqual(kinds[-1], KIND_FOOTSTEP)


class TestSurroundLocalizer(unittest.TestCase):
    def test_channel_balance_360(self):
        loc = SurroundLocalizer()
        x = white_noise(rms=0.02, seed=700)
        data = np.zeros((N, 6))
        data[:, 5] = x                          # BR = rear right
        res = loc.feed(data, SR, 20, 20000, 0.005, 1.0, 1.0)
        self.assertGreater(res[0], 90.0)
        self.assertIsNotNone(res[2])
        self.assertAlmostEqual(res[1], 0.0, places=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
