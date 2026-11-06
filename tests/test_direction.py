"""Unit tests for direction.py - the pure math behind the radar.

These run without any audio hardware, Qt, or Windows APIs (plain numpy), so
they work in CI on Linux. (Converted from the upstream pytest style so the
whole suite runs on plain `python -m unittest`; the SSD fork replaced
surround_angle with surround_azimuth.)
"""

import math
import unittest

import numpy as np

from direction import angle_diff, band_rms, stereo_angle, surround_azimuth

SR = 48000
N = 2400  # one 50ms capture chunk, same as audio_capture.py


def sine(freq, amp=1.0, n=N, sr=SR):
    t = np.arange(n) / sr
    return amp * np.sin(2 * np.pi * freq * t)


class TestBandRms(unittest.TestCase):
    def test_full_range_equals_plain_rms(self):
        rng = np.random.default_rng(0)
        data = rng.standard_normal((N, 2))
        expected = np.sqrt(np.mean(data ** 2, axis=0))
        np.testing.assert_allclose(band_rms(data, SR, 20, 20000), expected)

    def test_in_band_sine_keeps_its_rms(self):
        data = sine(440, amp=0.5)[:, None]
        rms = band_rms(data, SR, 100, 900)
        self.assertAlmostEqual(rms[0], 0.5 / math.sqrt(2), delta=0.05)

    def test_out_of_band_sine_is_rejected(self):
        data = sine(4000, amp=0.5)[:, None]
        rms = band_rms(data, SR, 100, 900)
        self.assertLess(rms[0], 0.01)

    def test_band_separates_mixed_signal(self):
        in_band = sine(300, amp=0.2)
        rumble = sine(40, amp=1.0)
        data = (in_band + rumble)[:, None]
        rms = band_rms(data, SR, 100, 900)
        self.assertAlmostEqual(rms[0], 0.2 / math.sqrt(2), delta=0.1)

    def test_per_channel_independence(self):
        data = np.stack([sine(300, amp=0.4), sine(4000, amp=0.4)], axis=1)
        rms = band_rms(data, SR, 100, 900)
        self.assertGreater(rms[0], 10 * rms[1])


class TestStereoAngle(unittest.TestCase):
    def test_stereo_centre_is_zero(self):
        self.assertAlmostEqual(stereo_angle(0.5, 0.5), 0.0, delta=0.1)

    def test_stereo_hard_right_and_left(self):
        self.assertAlmostEqual(stereo_angle(0.0, 0.5), 90.0, delta=1.0)
        self.assertAlmostEqual(stereo_angle(0.5, 0.0), -90.0, delta=1.0)

    def test_stereo_slight_pan_is_expanded(self):
        angle = stereo_angle(0.4, 0.6)
        self.assertTrue(30.0 < angle < 90.0)


class TestSurroundAzimuth(unittest.TestCase):
    def test_surround_front_is_zero(self):
        az, _ = surround_azimuth([1.0, 1.0, 1.0, 0.0, 0.0, 0.0])
        self.assertAlmostEqual(az, 0.0, delta=0.5)

    def test_surround_right_is_90(self):
        az, _ = surround_azimuth([0.0, 1.0, 0.0, 0.0, 0.0, 1.0])
        self.assertAlmostEqual(az, 90.0, delta=0.5)

    def test_surround_left_is_minus_90(self):
        az, _ = surround_azimuth([1.0, 0.0, 0.0, 0.0, 1.0, 0.0])
        self.assertAlmostEqual(az, -90.0, delta=0.5)

    def test_surround_rear_is_180(self):
        az, _ = surround_azimuth([0.0, 0.0, 0.0, 1.0, 1.0, 0.0])
        self.assertAlmostEqual(abs(az), 180.0, delta=0.5)


class TestAngleDiff(unittest.TestCase):
    def test_angle_diff_simple(self):
        self.assertAlmostEqual(angle_diff(10.0, 30.0), 20.0)
        self.assertAlmostEqual(angle_diff(30.0, 10.0), 20.0)

    def test_angle_diff_wraps_at_180(self):
        # A sound directly behind the player: -179 and +179 are 2 degrees
        # apart, not 358 (the bug that split rear blips in two).
        self.assertAlmostEqual(angle_diff(179.0, -179.0), 2.0)
        self.assertAlmostEqual(angle_diff(-170.0, 170.0), 20.0)

    def test_angle_diff_opposites(self):
        self.assertAlmostEqual(angle_diff(90.0, -90.0), 180.0)


if __name__ == "__main__":
    unittest.main()
