"""Pure direction/level math shared by the capture thread and the overlay.

No Qt, no audio-device imports - everything here is plain numpy/math so it can
be unit-tested on any platform (see tests/) and reused without dragging in
soundcard/COM.

──────────────────────────────────────────────────────────────────────────
 SSD (single-sided-deaf) fork - what this module adds over the upstream
 version:
──────────────────────────────────────────────────────────────────────────
 1. Full 360° azimuth from a STEREO (binaural/HRTF) signal:
      * ILD (level difference) resolves left/right,
      * an adaptive spectral-tilt baseline (brightness of quiet ambience)
        resolves front/back - a rear HRTF source loses more high-frequency
        energy than a frontal one, so short events that sound "darker" than
        the ambient baseline are placed behind the player.
 2. Elevation (up/down) from high-frequency pinna cues: the 6-9.5 kHz notch
    band vs the 9.5-14 kHz "brilliance" band, also baseline-adapted. This is
    a heuristic - it is drawn with reduced confidence at low SNR.
 3. An adaptive noise floor + transient emphasis: quiet footsteps stay
    visible above rain/wind/engine ambience without raising the fixed
    threshold, and their fast attack gets an extra visual boost.
 4. 7.1 / 7.1.4 surround azimuth + height for real multichannel loopbacks.

The direction cues always use the FULL spectrum (fixed cue bands below);
the user's footstep band-pass (freq_low/freq_high) only decides WHICH sounds
are shown, never how they are localized.
"""

import math

import numpy as np

# ── Fixed cue bands (Hz) for HRTF localization ──────────────────────────────
BANDS = {
    "low":    (120,   700),    # localization: ITD-dominated, little head shadow
    "mid":    (900,  3500),    # localization: strong ILD (head shadow)
    "high":   (3500, 8000),    # front/back: attenuated for rear sources
    "notch":  (6000, 9500),    # elevation: pinna notch region
    "brill":  (9500, 14000),   # elevation: "brilliance" region above the notch
}

# Front/back tilt deviation (dB) that saturates the frontness score, and the
# elevation deviation (dB) that saturates the elevation score. Both compare a
# chunk's cue ratio against the running ambient baseline.
TILT_FULL_SCALE_DB = 6.0
ELEV_FULL_SCALE_DB = 4.0

# ── Sound kind classification ──────────────────────────────────────────────
# Gunshots and footsteps are physically very different, so a few robust
# features separate them without needing the game's asset files:
#   * crest factor: an explosion-like impulse peaks FAR above its own RMS;
#     a footstep thump is a dull, damped impact (low crest);
#   * high-frequency fraction: gunfire cracks across 4 kHz+; footsteps carry
#     almost no energy up there;
#   * low-frequency fraction: footsteps are a body thump, heavy below 700 Hz.
KIND_OTHER = 0
KIND_FOOTSTEP = 1
KIND_GUNSHOT = 2     # "combat sound": gunfire + grenades/explosions (no smoke)
KIND_SELF = 3        # the player's own sound (suppressed, shown as a pulse)
KIND_SILENT = 4      # very quiet footstep: possible crouch-walk threat
KIND_THUMP = 5       # INTERNAL: dull transient - footstep, hit or door; the
#                       caller resolves it with the rhythm context.

# Feature thresholds - CALIBRATED ON REAL ABI RECORDINGS.
# Combat sounds: measured crest 19.0±4.1 dB vs footstep 9.4±3.7; hf 0.45 vs
# 0.04. Thresholds keep ~85% of combat sounds with ~0.5% misc leakage.
GUNSHOT_CREST_DB = 14.5     # chunk peak/RMS must exceed this
GUNSHOT_HF_FRAC = 0.22      # energy above 4 kHz share

# Thump (footstep-or-misc candidate): the per-chunk spectrum CANNOT separate
# a footstep from a body hit / door / reload (measured: any threshold leaks
# ~97% of misc into blue). The reliable dimension is RHYTHM: footsteps come
# in trains, misc sounds are isolated. These bounds just pick out "dull
# transients" broadly. THUMP_MID_MAX keeps machine/mechanical noise out: its
# metallic ring puts ~25% of the accumulated energy in 700-3500 Hz, while
# real footsteps stay under ~5% there.
THUMP_LF_MIN = 0.25         # strong low-frequency thump share
THUMP_HF_MAX = 0.45         # ...or very dark sound
THUMP_CREST_MAX = 30.0      # hard-surface (crunchy) steps can reach ~23 dB
THUMP_MID_MAX = 0.15        # machine-noise ring guard (700-3500 Hz share)

# Very quiet footsteps (crouch-walking): real ABI footstep SNR starts at
# ~3.8 dB (p5); anything under ~11 dB (about the quietest quarter) gets the
# dedicated "silent step" threat treatment.
SILENT_STEP_SNR_DB = 11.0

# Burst-decay verification: a dull transient only counts if the level has
# dropped by this much ONE chunk later (footsteps decay fast; ambient gusts
# stay elevated - measured attack p50: steps 4.6 dB vs ambient 0.0 dB).
THUMP_DECAY_DB = 2.5

# Sustained LOUD roar = automatic fire (M4A1 ground truth: the whole burst
# sustains 0.04-0.20 rms with no quiet gaps - spectrally dull and low-crest,
# nothing like a single-shot crack). A pending thump whose level never decays
# AND stays this loud is gunfire, not an ambient swell.
LOUD_ROAR_DB = -28.0        # ≈ 0.04 rms in the gate band

# A centred thump this loud is the player's own fire/impact (own gunfire is
# dull and sustained, not crack-like); quieter centred thumps stay misc.
LOUD_CENTRED_GMAX = 0.05

# Distance model (relative, from SNR): SNR_NEAR_DB is the loudness of a sound
# right next to you; every ~4 dB quieter roughly doubles the distance on the
# radar ring.
SNR_NEAR_DB = 34.0
SNR_SPAN_DB = 28.0

# Own-sound suppression heuristics.
# GROUND TRUTH (user-recorded first-person samples): the game renders your
# own sounds slightly OFF-centre - shots at ±17-18° AND footsteps at
# +1.2..2.3 dB ILD (≈12-21°) - the first-person weapon/body model offset.
# One shared ±25° window covers both.
SELF_LAT_DEG = 25.0         # own footsteps + own actions: near-centre
SELF_SHOT_LAT_DEG = 30.0    # own shots: ±1.5 dB measured, tails scatter to
#                             ~±3.3 dB - 30° covers the whole burst
SELF_STEP_MIN = 4           # need this many rhythmic steps to latch
SELF_STEP_INTERVAL = (0.25, 0.9)   # walk/run cadence window (seconds)
SELF_STEP_JITTER = 0.35     # max interval std/mean

# Onset gate: a footstep/gunshot chunk only counts if the previous ~120 ms
# were quiet. Measured on real recordings: ambient noise (wind, engines) in
# the narrow gate band fires EVERY 25 ms chunk with zero attack - while real
# footsteps come as short bursts separated by silence. Requiring an onset
# kills the continuous-ambient false positives outright.
ONSET_GAP_SEC = 0.12


def event_features(data, cue, gate_rms, samplerate):
    """Per-event classification features from one chunk.

    Returns a dict with crest_db (peak/RMS in the gate band), hf_frac (energy
    share above ~4 kHz) and lf_frac (energy share below ~700 Hz).
    """
    gmax = float(max(gate_rms[0], gate_rms[1])) if len(gate_rms) >= 2 else (
        float(gate_rms[0]) if len(gate_rms) == 1 else 0.0)
    peak = float(np.max(np.abs(data)))
    crest_db = 20.0 * math.log10((peak + 1e-12) / (gmax + 1e-12))
    crest_db = min(max(crest_db, 0.0), 60.0)

    total = sum(float(cue[k][i]) for k in cue for i in range(len(cue[k]))) + 1e-14
    hf = sum(float(cue[k][i]) for k in ("high", "notch", "brill")
             for i in range(len(cue[k])))
    lf = sum(float(cue["low"][i]) for i in range(len(cue["low"])))
    mid = sum(float(cue["mid"][i]) for i in range(len(cue["mid"])))
    return {
        "crest_db": crest_db,
        "hf_frac": hf / total,
        "lf_frac": lf / total,
        "mid_frac": mid / total,
    }


def classify_kind(feat, loud_bypass=False):
    """Provisional per-chunk classification (three ways).

    KIND_GUNSHOT here means COMBAT SOUND: gunfire, grenades (frag/offensive,
    molotov, stun) and explosions - smoke pops are quiet and dull, so they
    naturally fall to "other". loud_bypass marks events louder than max_amp:
    in the narrow gate band that loudness is essentially always gunfire or
    an explosion, so they are combat sounds unconditionally.

    KIND_THUMP = dull transient: could be a footstep, a body hit or a door.
    The caller resolves it with the rhythm context - footsteps come in
    trains, misc sounds are isolated (per-chunk spectra cannot separate
    them: measured, any threshold leaks ~97% of misc into blue).
    """
    if loud_bypass:
        return KIND_GUNSHOT
    if feat["crest_db"] >= GUNSHOT_CREST_DB and feat["hf_frac"] >= GUNSHOT_HF_FRAC:
        return KIND_GUNSHOT
    if (feat["lf_frac"] >= THUMP_LF_MIN or feat["hf_frac"] <= THUMP_HF_MAX) \
            and feat["crest_db"] <= THUMP_CREST_MAX \
            and feat.get("mid_frac", 0.0) <= THUMP_MID_MAX:
        return KIND_THUMP
    return KIND_OTHER


def distance_estimate(snr, attack_db=0.0, tilt_dev=0.0):
    """SNR -> relative distance 0 (next to you) .. 1 (far edge of the radar).

    Sharp attacks pull the estimate closer; unusually dark sounds (rear / far
    HRTF signature) push it away a little."""
    d = (SNR_NEAR_DB - snr) / SNR_SPAN_DB
    d -= min(max(attack_db, 0.0), 12.0) / 12.0 * 0.20
    d += min(max(-tilt_dev, 0.0), 12.0) / 12.0 * 0.15
    return float(np.clip(d, 0.0, 1.0))


def band_energies(data, samplerate, bands=None):
    """Per-channel energy (mean square) in each band, computed in the frequency
    domain with a Hann window (Parseval-corrected, same as band_rms).

    `data` is (frames, channels) float. Returns {name: np.array(channels)}.
    """
    data = np.asarray(data, dtype=np.float64)
    if data.ndim == 1:
        data = data[:, None]
    n = data.shape[0]
    bands = bands or BANDS
    if n == 0:
        return {k: np.zeros(data.shape[1]) for k in bands}
    window = np.hanning(n)
    spec = np.fft.rfft(data * window[:, None], axis=0)
    power = np.abs(spec) ** 2
    freqs = np.fft.rfftfreq(n, d=1.0 / samplerate)
    weights = np.full(len(freqs), 2.0)
    weights[0] = 1.0
    if n % 2 == 0:
        weights[-1] = 1.0
    norm = 1.0 / (n * np.mean(window ** 2))
    out = {}
    for name, (lo, hi) in bands.items():
        sel = (freqs >= lo) & (freqs <= hi)
        if not sel.any():
            out[name] = np.zeros(data.shape[1])
            continue
        band_energy = (power * (weights * sel)[:, None]).sum(axis=0) / n
        out[name] = band_energy * norm
    return out


def band_rms(data, samplerate, freq_low=20, freq_high=20000):
    """Per-channel RMS of `data` restricted to [freq_low, freq_high] Hz.

    `data` is (frames, channels) float. Returns a 1-D array of one RMS value
    per channel. (Kept from upstream - used for the absolute sensitivity gate
    and the multichannel channel-balance path.)
    """
    data = np.asarray(data, dtype=np.float64)
    if data.ndim == 1:
        data = data[:, None]
    n = data.shape[0]
    if n == 0:
        return np.zeros(data.shape[1])

    if freq_low <= 20 and freq_high >= 20000:
        # Full audible range: no filtering needed, plain RMS is exact.
        return np.sqrt(np.mean(data ** 2, axis=0))

    window = np.hanning(n)
    spec = np.fft.rfft(data * window[:, None], axis=0)
    freqs = np.fft.rfftfreq(n, d=1.0 / samplerate)
    in_band = (freqs >= freq_low) & (freqs <= freq_high)

    weights = np.full(len(freqs), 2.0)
    weights[0] = 1.0
    if n % 2 == 0:
        weights[-1] = 1.0

    band_energy = ((np.abs(spec) ** 2) * (weights * in_band)[:, None]).sum(axis=0) / n
    mean_square = band_energy / n / np.mean(window ** 2)
    return np.sqrt(mean_square)


def stereo_angle(left_rms, right_rms):
    """L/R balance -> angle in degrees, -90 (hard left) .. +90 (hard right).

    Kept for the classic "Stereo L/R" mode. The 0.3 exponent expands small
    balance differences so slightly-panned sounds still leave the centre.
    """
    balance = (right_rms - left_rms) / (right_rms + left_rms + 1e-6)
    sign = 1.0 if balance >= 0 else -1.0
    return sign * (abs(balance) ** 0.3) * 90.0


# Broadband ILD balance observed at 90° for a typical head/shoulder model.
# The head-shadow ILD grows roughly like sin(azimuth), so the inverse mapping
# is an arcsin of the normalized balance.
BAL_90 = 0.7


def itd_lateral(data, samplerate):
    """Interaural TIME difference -> lateral angle via the Woodworth
    spherical-head model. ITD dominates at low frequencies where ILD is
    weak, so it is the reliable lateral cue for FOOTSTEPS (the game's most
    common directional sound); ILD stays the cue for broadband gunfire.

    Returns (lateral_deg, confidence 0..1). Sign: + = right, - = left."""
    if data.ndim < 2 or data.shape[1] < 2 or data.shape[0] < 64:
        return 0.0, 0.0
    l = data[:, 0].astype(np.float64)
    r = data[:, 1].astype(np.float64)
    n = data.shape[0]
    spec_l = np.fft.rfft(l)
    spec_r = np.fft.rfft(r)
    fr = np.fft.rfftfreq(n, 1.0 / float(samplerate))
    mask = ((fr >= 80.0) & (fr <= 1600.0)).astype(np.float64)
    lf = np.fft.irfft(spec_l * mask, n)
    rf = np.fft.irfft(spec_r * mask, n)
    e_l = float(np.sqrt(np.mean(lf ** 2)))
    e_r = float(np.sqrt(np.mean(rf ** 2)))
    if e_l < 1e-6 or e_r < 1e-6:
        return 0.0, 0.0
    max_lag = int(0.0007 * samplerate)          # ~0.7 ms head width
    corr = np.correlate(lf, rf, mode="full")
    centre = n - 1
    seg = corr[centre - max_lag: centre + max_lag + 1]
    lag = int(np.argmax(np.abs(seg))) - max_lag
    conf = float(np.max(np.abs(seg)) / (e_l * e_r * (n - abs(lag)) + 1e-14))
    conf = float(np.clip(conf, 0.0, 1.0))
    if abs(lag) < 2:
        return 0.0, conf
    # Woodworth: ITD(θ) = (a/c)·(sin θ + θ), a = 8.75 cm head radius.
    # Invert by table lookup over 0..90°.
    thetas = np.linspace(0.0, math.pi / 2.0, 181)
    itds = (0.0875 / 343.0) * (np.sin(thetas) + thetas)
    tau = abs(lag) / float(samplerate)
    idx = int(np.argmin(np.abs(itds - tau)))
    ang = math.degrees(thetas[idx])
    # numpy.correlate convention: corr[centre + k] = sum a[i+k]·v[i], so a
    # right channel that arrives LATER (source left) peaks at negative k.
    return math.copysign(ang, float(lag)), conf


def stereo_lateral(bal_mid, bal_low):
    """Stereo ILD balance -> lateral angle, -90 (left) .. +90 (right).

    Prefers the mid band (strongest head-shadow contrast); falls back to the
    low band. The arcsin/sin head-shadow model keeps mid-lateral angles
    (30°-60°) roughly linear instead of pinning everything to the sides.
    A small dead-zone pins near-centre sources: ±1 dB of measurement noise
    would otherwise smear truly centred sounds across ±12° (measured on the
    user's own first-person shots)."""
    bal = bal_mid if abs(bal_mid) > 1e-6 else bal_low
    bal = float(np.clip(bal, -1.0, 1.0))
    if abs(bal) < 0.04:
        return 0.0
    return math.degrees(math.asin(np.clip(bal / BAL_90, -1.0, 1.0)))


def surround_azimuth(ch):
    """Channel energies -> (azimuth_deg, elevation).

    Supports 5.1 (6ch), 7.1 (8ch) and 7.1.4 (12ch) layouts:
      0 FL, 1 FR, 2 FC, 3 LFE, 4 BL, 5 BR, 6 SL, 7 SR,
      8 TFL, 9 TFR, 10 TRL, 11 TRR.
    Azimuth: 0 = front, +90 = right, ±180 = rear. Elevation: -1..+1.
    """
    fl = float(ch[0]); fr = float(ch[1])
    fc = float(ch[2]) if len(ch) > 2 else 0.0
    bl = float(ch[4]) if len(ch) > 4 else 0.0
    br = float(ch[5]) if len(ch) > 5 else 0.0
    sl = float(ch[6]) if len(ch) > 6 else 0.0
    sr = float(ch[7]) if len(ch) > 7 else 0.0
    x = (fr + sr) - (fl + sl)
    y = (fl + fr + fc) - (bl + br + sl + sr)
    az = math.degrees(math.atan2(x, y))

    elev = 0.0
    if len(ch) >= 12:
        top = float(ch[8] + ch[9] + ch[10] + ch[11])
        bed = fl + fr + bl + br + sl + sr + 1e-9
        elev = float(np.clip(top / bed * 2.5 - 0.3, -1.0, 1.0))
    return az, elev


def angle_diff(a, b):
    """Smallest absolute difference between two angles in degrees.

    Wraps at +-180 so e.g. angle_diff(179, -179) == 2 - without this, blips
    for a sound directly behind the player split into two.
    """
    return abs((a - b + 180.0) % 360.0 - 180.0)


def wrap_180(a):
    """Wrap an angle in degrees into (-180, 180]."""
    a = float(a) % 360.0
    if a > 180.0:
        a -= 360.0
    return a


def map_intensity(snr_db, footstep_boost=1.0):
    """SNR (dB above the adaptive floor) -> 0..1 visual intensity.

    A log curve keeps quiet footsteps visible: 6 dB SNR is already clearly
    visible, 27 dB saturates the arc. footstep_boost shifts the whole curve
    so boosting the band makes faint steps brighter instead of amplifying
    ambience.
    """
    snr_db += 20.0 * math.log10(max(footstep_boost, 0.1))
    v = (snr_db - 1.0) / 26.0
    return float(np.clip(v, 0.0, 1.0)) ** 0.65


# ── Stateful helpers ────────────────────────────────────────────────────────

class LevelFollower:
    """Exponential level follower (dB) with independent up/down speeds -
    used as a short-term envelope for transient (attack) detection."""

    def __init__(self, init=-80.0, up_per_s=8.0, down_per_s=2.5):
        self.value = float(init)
        self.up = up_per_s
        self.down = down_per_s

    def update(self, level_db, dt):
        rate = self.up if level_db > self.value else self.down
        alpha = min(1.0, dt * rate)
        self.value += alpha * (float(level_db) - self.value)
        return self.value


class NoiseFloor:
    """Adaptive noise floor (dB): the MINIMUM level over a short sliding
    window, tracked with a fast EMA.

    The window minimum is the classic robust ambient estimator:
      * sustained ambience (rain, wind, engine) fills the window at its own
        level, so the floor rises to it and the ambience stops firing;
      * short events (footsteps) leave the dips between them in the window,
        so the floor stays at the pre-event level and the steps stay visible;
      * silence (level clamped to SILENCE_DB by the caller) pulls the floor
        back down so a quiet step after a lull still lights up.
    """

    SILENCE_DB = -90.0

    def __init__(self, init=-50.0, window=0.8, tau=0.25):
        self.value = float(init)
        self.window = window
        self.tau = tau
        self._hist = []
        self._max_len = 0

    def update(self, level_db, dt):
        self._max_len = max(2, int(round(self.window / max(dt, 1e-3))))
        self._hist.append(float(level_db))
        if len(self._hist) > self._max_len:
            self._hist.pop(0)
        target = min(self._hist)
        alpha = min(1.0, dt / self.tau)
        self.value += alpha * (target - self.value)
        return self.value


class _Baseline:
    """Slow ambient-character baseline (a dB ratio). Learned only from chunks
    that are NOT events, so the average game mix (wind, rain, ambience) is the
    reference that short sounds are compared against."""

    def __init__(self, tau=6.0):
        self.value = None
        self.tau = tau

    def update(self, sample, dt):
        if self.value is None:
            self.value = float(sample)
            return self.value
        alpha = min(1.0, dt / self.tau)
        self.value += alpha * (float(sample) - self.value)
        return self.value

    def deviation(self, sample):
        return float(sample) - (self.value if self.value is not None else 0.0)


class StereoLocalizer:
    """Stateful 360° localizer for a stereo (binaural/HRTF) stream.

    Feed one short chunk (~50 ms) at a time:
        feed(data, samplerate, gate_low, gate_high, sensitivity, max_amp,
             footstep_boost, adaptive=True, suppress_own_steps=True,
             suppress_own_shots=True)
    and it returns (azimuth, elevation, intensity, distance, kind, features)
    when an event is detected, else None.

      azimuth:   0 front, +90 right, ±180 rear
      elevation: -1..+1 (down..up)
      intensity: 0..1, mapped for the overlay
      distance:  0 (next to you) .. 1 (far edge of the radar)
      kind:      KIND_OTHER / KIND_FOOTSTEP / KIND_GUNSHOT / KIND_SELF
      features:  dict for the event log (snr, crest, band fractions, ...)

    front_bias (mutable attribute): manual tuning of the front/back cue - the
    value (-1..+1) adds ±4 dB to the tilt deviation, for headsets/HRTFs that
    sit off-centre for a particular user.
    """

    MIN_SNR_DB = 2.0     # events must clear the adaptive floor by this much
    AMBIENT_MAX_ABS = 4e-3  # chunks this quiet are pure ambience (RMS basis)

    def __init__(self, front_bias=0.0):
        self.front_bias = float(front_bias)
        self._floor = NoiseFloor(init=-50.0, window=0.8, tau=0.25)
        self._short = LevelFollower()
        self._tilt_base = _Baseline()
        self._elev_base = _Baseline()
        self._fb = 0.0       # frontness EMA, -1 (rear) .. +1 (front)
        self._elev = 0.0     # elevation EMA, -1 (down) .. +1 (up)
        self._az = None      # wrapped azimuth EMA (degrees)
        self._dist = 0.5     # distance EMA (0 near .. 1 far)
        self._kind = 0.0     # kind EMA float (round() -> kind)
        self._t = 0.0        # running stream time (seconds)
        self._step_times = []  # (time, azimuth) of recent loud centred steps
        self._thumps = []      # (time, azimuth, snr) of recent dull transients
        self._last_quiet_t = -10.0  # last moment the signal was at the floor
        self._last_event_t = -10.0  # last emitted event (burst de-duplication)
        self._pending = None        # (peak_level, age, pend) awaiting decay check
        self._self_latch = False  # keep SELF for the rest of the current event
        self._own_walk_latch = False  # running: suppress centred steps until
        #                                 an off-centre footstep arrives
        self._boost_now = 1.0       # per-feed params captured for the pending
        self._suppress_steps_now = True
        self._suppress_shots_now = True
        self._acc = None            # rolling ~150 ms spectrum for classification

    def feed(self, data, samplerate, gate_low, gate_high, sensitivity,
             max_amp, footstep_boost, adaptive=True,
             suppress_own_steps=True, suppress_own_shots=True):
        n = data.shape[0]
        if n == 0:
            return None
        dt = n / float(samplerate)
        self._t += dt

        cue = band_energies(data, samplerate)

        # Burst-scoped spectrum accumulator: while a thump onset awaits its
        # decay verification, accumulate its spectrum (onset + ringing tail)
        # so the re-classification sees the metallic ring of machine noise.
        # With no pending, the accumulator restarts from the current chunk
        # every time (cheap: 5 tiny arrays).
        if self._acc is None or self._pending is None:
            self._acc = {k: np.asarray(v, dtype=np.float64).copy()
                         for k, v in cue.items()}
        else:
            a = min(1.0, dt / 0.08)   # fast: the ring matters
            for k in self._acc:
                self._acc[k] = (1.0 - a) * self._acc[k] + a * cue[k]

        gate_rms = band_rms(data, samplerate, gate_low, gate_high)
        g_l = float(gate_rms[0]) if len(gate_rms) > 0 else 0.0
        g_r = float(gate_rms[1]) if len(gate_rms) > 1 else g_l
        gmax = max(g_l, g_r)

        # The floor is fed on EVERY chunk (silence clamps to SILENCE_DB) so it
        # rises to sustained ambience and falls back after a lull. The short
        # level follower is too - it must track the AMBIENT level so that a
        # quiet step after a lull doesn't get a phantom 12 dB "attack" boost.
        level_db = 10.0 * math.log10(gmax * gmax + 1e-20)
        level_db = max(level_db, NoiseFloor.SILENCE_DB)

        # ── Burst-decay verification (pending thump) ─────────────────────
        # A dull-transient onset is only a real burst if the level DROPS back
        # shortly after it: footsteps decay within one or two chunks, ambient
        # gusts stay elevated (measured attack p50: steps 4.6 dB vs ambient
        # 0.0 dB). The pending onset may rise for one more chunk (slow-attack
        # soft steps), but must decay by THUMP_DECAY_DB within two chunks or
        # it is dropped as an ambient swell.
        pend_res = None
        if self._pending is not None:
            peak, age, pend = self._pending
            self._pending = None
            peak = max(peak, level_db)
            age += 1
            if peak - level_db >= THUMP_DECAY_DB:
                pend_res = self._finalize_thump(pend)
            elif age < 2:
                self._pending = (peak, age, pend)   # may still be rising
            elif level_db >= LOUD_ROAR_DB:
                # Sustained LOUD sound that never decays: automatic fire
                # (the M4A1 burst sustains with no quiet gaps). Not an
                # ambient swell - emit as combat.
                pend_res = self._finalize_roar(pend)
            # else: sustained ambient swell, drop silently

        self._floor.update(level_db, dt)
        self._short.update(level_db, dt)

        # "Quiet" = the signal sits at the ambient floor (steady rain/wind IS
        # quiet for our purposes). The onset gate needs a quiet moment within
        # the last ~120 ms; bursts alone do not re-arm it.
        if adaptive:
            quiet = (level_db - self._floor.value) < 1.5
        else:
            quiet = gmax < sensitivity * 2.0
        if quiet:
            self._last_quiet_t = self._t

        # Learn ambient character from quiet chunks (keeps the reference
        # ratios up to date while nothing relevant happens).
        if gmax < max(sensitivity, self.AMBIENT_MAX_ABS):
            self._learn_ambient(cue, dt)

        # Loud events above max_amp are NOT dropped anymore: they are almost
        # certainly gunfire, so they are classified and shown as gunshots
        # (max_amp only gates them out of the *footstep* band).
        loud_bypass = gmax > max_amp
        if gmax < sensitivity:
            self._self_latch = False
            return pend_res

        # With the adaptive gate off, the fixed sensitivity alone decides:
        # SNR is then measured against a -90 dB reference.
        snr = level_db - (self._floor.value if adaptive else NoiseFloor.SILENCE_DB)
        if snr < self.MIN_SNR_DB and not loud_bypass:
            self._learn_ambient(cue, dt)
            self._self_latch = False
            return pend_res

        # Transient (attack) emphasis: fast-rising level gets extra dB so
        # footstep thumps pop visually (follower updated per-chunk above).
        attack_db = min(max(level_db - self._short.value, 0.0), 12.0)
        snr = snr + 0.6 * attack_db

        feat = event_features(data, cue, gate_rms, samplerate)
        feat.update({
            "snr": float(snr), "attack_db": float(attack_db),
            "level_db": float(level_db), "gmax": float(gmax),
        })
        self._acc_fractions(feat)

        # ── Lateral angle: ITD (low frequencies) fused with ILD ──────────
        # Footsteps carry almost no energy above ~1 kHz, where the ILD
        # (head shadow) is strong - their lateral position lives in the
        # interaural TIME difference instead. Fuse by low-frequency share:
        # a dull thump trusts ITD, a broadband crack trusts ILD.
        el = cue["low"]; em = cue["mid"]
        bal_mid = (em[1] - em[0]) / (em[0] + em[1] + 1e-14) if len(em) > 1 else 0.0
        bal_low = (el[1] - el[0]) / (el[0] + el[1] + 1e-14) if len(el) > 1 else 0.0
        lat_ild = stereo_lateral(bal_mid, bal_low)
        lat_itd, itd_conf = itd_lateral(data, samplerate)
        lf_w = float(np.clip(feat["lf_frac"], 0.0, 1.0))
        # Consistency guard: a level-only pan (no interaural delay) would
        # read ITD=0 with high confidence and collapse the lateral angle -
        # only trust ITD when it actually indicates a direction, or when
        # the ILD is too weak to say anything (true of low-frequency HRTF).
        ild_strong = abs(bal_mid) > 0.08 or abs(bal_low) > 0.08
        itd_usable = itd_conf > 0.25 and (abs(lat_itd) >= 5.0 or not ild_strong)
        if itd_usable and (lat_itd * lat_ild >= 0.0 or abs(lat_ild) < 8.0):
            lat = lf_w * lat_itd + (1.0 - lf_w) * lat_ild
        else:
            lat = lat_ild

        # ── Front/back from spectral tilt vs ambient baseline ───────────
        # Rear HRTF sources are "darker" (less high-band energy) than frontal
        # ones; comparing against the ambient baseline cancels the sound's own
        # spectrum (gunshot vs footstep) to first order.
        lo = el[0] + el[1] + 1e-14
        hi = cue["high"][0] + cue["high"][1] + 1e-14
        tilt_db = 10.0 * math.log10(hi / lo)
        dev = self._tilt_base.deviation(tilt_db) + self.front_bias * 4.0
        feat["tilt_db"] = float(tilt_db)
        s = float(np.clip(dev / TILT_FULL_SCALE_DB, -1.0, 1.0))
        # Strong cues adapt fast (a clear rear HRTF signature flips the state
        # in ~2 chunks - the player turns constantly in firefights); weak cues
        # drift more slowly so noise can't flicker it.
        tau = 0.08 if abs(s) > 0.5 else 0.35
        self._fb += min(1.0, dt / tau) * (s - self._fb)

        # ── Elevation from the pinna notch / brilliance ratio ───────────
        brill = cue["brill"][0] + cue["brill"][1] + 1e-14
        notch = cue["notch"][0] + cue["notch"][1] + 1e-14
        er_db = 10.0 * math.log10(brill / notch)
        edev = self._elev_base.deviation(er_db)
        e_raw = float(np.clip(edev / ELEV_FULL_SCALE_DB, -1.0, 1.0))
        self._elev += min(1.0, dt / 0.30) * (e_raw - self._elev)
        # Low SNR ⇒ the pinna band is barely excited ⇒ damp the estimate so
        # the display doesn't lie. Elevation needs content above ~6 kHz.
        elev_conf = float(np.clip((snr - 3.0) / 8.0, 0.0, 1.0))
        elev = self._elev * elev_conf

        # ── Sound kind (provisional: combat / thump / other) ─────────────
        # No EMA: the onset gate already limits us to one chunk per burst,
        # and an EMA would smear the classification across chunk boundaries.
        kind = classify_kind(feat, loud_bypass=loud_bypass)
        feat["kind"] = kind

        # ── Onset gate ────────────────────────────────────────────────────
        # Footsteps/combat sounds are SHORT BURSTS separated by (relative)
        # silence; ambient noise (wind/engines) inside the narrow gate band
        # is CONTINUOUS - it fires every chunk with the same dull spectrum.
        # Real data: 84% of "silent" events were ambient with 25 ms spacing
        # and zero attack. A burst chunk therefore counts only if BOTH: a
        # quiet moment occurred within the last ~120 ms (the burst's onset)
        # AND the previous emitted event is older than ~120 ms (one event
        # per burst). Continuous ambient fires exactly once per onset.
        if kind in (KIND_THUMP, KIND_GUNSHOT) \
                and not ((self._t - self._last_quiet_t) <= ONSET_GAP_SEC
                         and (self._t - self._last_event_t) >= ONSET_GAP_SEC):
            self._learn_ambient(cue, dt)
            self._self_latch = False
            return pend_res

        # One event per burst for misc sounds too. A wider window than the
        # onset gate: the bolt-cycle ring of a bolt-action rifle re-triggers
        # every ~120-130 ms for several chunks - one yellow mark per bolt
        # cycle is the intent, not four.
        if kind == KIND_OTHER and (self._t - self._last_event_t) < 0.22:
            self._self_latch = False
            return pend_res

        # ── Relative distance (before the azimuth EMA, so `_az is None`
        # reliably means "first event after a lull" and the very first paint
        # already shows an honest distance instead of the stale default) ──
        dist_raw = distance_estimate(snr, attack_db, dev)
        if self._az is None:
            self._dist = dist_raw
        else:
            self._dist += min(1.0, dt / 0.12) * (dist_raw - self._dist)

        # ── Combine into a 360° azimuth ─────────────────────────────────
        # When the front/back cue is weak (typical for DULL sounds like
        # footsteps), the old blend pulled the azimuth toward the sides and
        # rear - destroying the lateral direction too. Front-defaulting
        # keeps LEFT/RIGHT always correct (the axis that matters most),
        # and the rear half is used only when the HRTF cue is reliable.
        front_az = lat
        rear_az = (math.copysign(180.0 - abs(lat), lat)
                   if abs(lat) > 1e-9 else 180.0)
        w = (self._fb + 1.0) / 2.0
        if abs(self._fb) < 0.2:
            az = front_az      # no reliable cue: front default, L/R exact
        else:
            az = front_az * w + rear_az * (1.0 - w)

        # Smooth wrapped azimuth so a single noisy chunk can't flicker the
        # marker, without lagging a sweep: 0.08 s trades ~1.8° of lag at the
        # measured 22°/s rotation rate for most of the old ±40° scatter.
        if self._az is None:
            self._az = az
        else:
            d = wrap_180(az - self._az)
            alpha = min(1.0, dt / 0.08) if abs(d) < 60.0 else 0.75
            self._az = wrap_180(self._az + alpha * d)

        # ── Own combat sound (own gunfire / grenade throw) ───────────────
        # No loudness bar: the 16-weapon library shows every first-person
        # burst (including its quiet tail chunks) stays centred, while
        # enemy fire rarely sits inside the window. Sound type cannot tell
        # them apart - only distance can, and the centre of the image IS
        # distance zero.
        centred = abs(lat) < SELF_LAT_DEG
        shot_centred = abs(lat) < SELF_SHOT_LAT_DEG
        if kind == KIND_GUNSHOT and suppress_own_shots and shot_centred:
            kind = KIND_SELF
            feat["kind"] = kind
            self._self_latch = True

        # ── Dull transient: verify decay on the NEXT chunk, then resolve ─
        # (rhythm context decides footstep / silent threat / misc). Delaying
        # one chunk costs 25 ms but lets us reject ambient swells that stay
        # elevated - the biggest remaining false-positive source.
        if kind == KIND_THUMP:
            self._boost_now = footstep_boost
            self._suppress_steps_now = suppress_own_steps
            self._suppress_shots_now = suppress_own_shots
            self._pending = (level_db, 0, (self._t, float(self._az),
                                           float(elev), dict(feat),
                                           float(snr), float(lat)))
            return pend_res

        # A loud centred thump that classified OTHER directly (the mid-band
        # guard demoted it from THUMP on the onset chunk): the player's own
        # fire (M4A1 ground truth), not misc.
        if kind == KIND_OTHER and centred and gmax >= LOUD_CENTRED_GMAX:
            kind = KIND_SELF
            feat["kind"] = kind

        # Keep a just-detected own sound suppressed for the rest of its event
        # (the per-chunk classification would otherwise un-latch between
        # chunks of the same sound).
        if self._self_latch and kind in (KIND_FOOTSTEP, KIND_GUNSHOT):
            kind = KIND_SELF
            feat["kind"] = kind

        intensity = map_intensity(snr, footstep_boost)
        dist = 0.0 if kind == KIND_SELF else float(self._dist)
        if kind == KIND_SELF:
            intensity = min(intensity, 0.7)

        self._last_event_t = self._t   # one event per burst
        feat["fb"] = float(self._fb)   # front/back ambiguity at emission
        return (float(self._az), float(elev), float(intensity),
                float(dist), int(kind), feat)

    def _finalize_thump(self, pend):
        """A dull transient whose level has decayed (real burst, not an
        ambient swell): resolve it through the rhythm context and emit.

        Per-chunk spectra cannot separate a footstep from a body hit or a
        door; rhythm can. A dull transient in a regular train (0.25-0.9 s
        cadence from the same direction) is a footstep; an isolated or slow
        QUIET one is the crouch-walk threat (purple); an isolated louder one
        is misc (hit/door/reload, yellow)."""
        t, az, elev, feat, snr, lat = pend
        centred = abs(lat) < SELF_LAT_DEG

        # Re-classify with the now-accumulated spectrum: the ring of machine
        # noise (mid-band energy) only becomes visible a chunk or two after
        # the onset, so the pending's first guess must be re-checked here.
        feat2 = dict(feat)
        self._acc_fractions(feat2)
        kind = classify_kind(feat2, loud_bypass=False)
        if kind != KIND_THUMP:
            feat = feat2
            feat["kind"] = kind
            if kind == KIND_GUNSHOT and self._suppress_shots_now \
                    and abs(lat) < SELF_SHOT_LAT_DEG:
                kind = KIND_SELF
                feat["kind"] = kind
            elif kind == KIND_OTHER and centred \
                    and feat.get("gmax", 0.0) >= LOUD_CENTRED_GMAX:
                # Loud centred thump (the M4A1 burst tail / own fire) that
                # the mid-band guard demoted from THUMP: still the player's
                # own fire, not misc.
                kind = KIND_SELF
                feat["kind"] = kind
            intensity = map_intensity(snr, self._boost_now)
            dist = 0.0 if kind == KIND_SELF else float(self._dist)
            if kind == KIND_SELF:
                intensity = min(intensity, 0.7)
            self._last_event_t = self._t
            feat["fb"] = float(self._fb)
            return (float(az), float(elev), float(intensity),
                    float(dist), int(kind), feat)

        self._thumps.append((t, lat, snr))
        self._thumps = [p for p in self._thumps if t - p[0] <= 3.5]
        # Direction matching in ILD space: own steps sit at lat≈0 even when
        # the azimuth estimate is confused by the (front/back-ambiguous)
        # dull spectrum - azimuth-space matching scattered them and broke
        # the cadence chains.
        same = [p for p in self._thumps[:-1]
                if abs(p[1] - lat) < 30.0][-4:]
        cadence = False
        if len(same) >= 2:
            # Walking cadence with tolerance: real walkers are metronomic
            # (0.3-0.9 s per step) but ONE missed detection doubles an
            # interval. Accept intervals up to 1.8 s when the median of the
            # recent intervals stays in the walking band.
            ivs = [same[i + 1][0] - same[i][0] for i in range(len(same) - 1)]
            ivs.append(t - same[-1][0])
            if all(0.25 <= v <= 1.8 for v in ivs) \
                    and 0.3 <= float(np.median(ivs)) <= 0.9:
                cadence = True
        if cadence:
            kind = KIND_FOOTSTEP
        elif snr <= SILENT_STEP_SNR_DB:
            # ALL quiet steps are shown as the silent threat, INCLUDING the
            # player's own - per the user's explicit request: their own
            # silent-walk steps must appear on the radar in real time, so
            # quiet thumps are never suppressed regardless of direction.
            kind = KIND_SILENT
            if centred:
                # Centred quiet steps (the player's own silent walk) have no
                # front/back cue - pin them to the front centre instead of
                # leaving them at the azimuth-blend artifact (±90°).
                az = 0.0
        elif centred:
            # Image-centred isolated thump: the player's OWN activity. A
            # LOUD one is their own fire/impact (own gunfire is dull and
            # sustained - the M4A1's burst tail decays into silence and
            # lands here); a quieter one is a door/loot/reload/hit (misc).
            # (An enemy exactly dead ahead/behind is also centred; a rare
            # and acceptable trade-off.)
            kind = KIND_SELF if feat.get("gmax", 0.0) >= LOUD_CENTRED_GMAX \
                else KIND_OTHER
        else:
            # Off-centre isolated thump: enemy activity (single step, jump,
            # door). Rhythm can't resolve it (no train), but the ILD can:
            # enemy sounds rarely sit dead-centre.
            kind = KIND_FOOTSTEP

        # ── Own footstep suppression (rhythm + centred, no loudness bar) ─
        if kind == KIND_FOOTSTEP:
            if not centred:
                self._own_walk_latch = False   # someone else's steps: release
            elif self._own_walk_latch:
                kind = KIND_SELF               # still running: keep suppressed
        if kind == KIND_FOOTSTEP and self._suppress_steps_now and centred:
            if not self._step_times or t - self._step_times[-1][0] >= 0.2:
                self._step_times.append((t, az))
            self._step_times = [p for p in self._step_times if t - p[0] <= 3.0]
            if self._step_times[-4:]:
                times = [p[0] for p in self._step_times[-4:]]
                iv = [times[i + 1] - times[i] for i in range(len(times) - 1)]
                lo_i, hi_i = SELF_STEP_INTERVAL
                if (len(iv) >= 3
                        and all(lo_i <= v <= hi_i for v in iv)
                        and (np.std(iv) / (np.mean(iv) + 1e-9)) < SELF_STEP_JITTER):
                    kind = KIND_SELF
                    self._step_times = []   # re-arm once the pattern breaks
                    self._self_latch = True
                    self._own_walk_latch = True
        else:
            self._step_times = [p for p in self._step_times
                                if t - p[0] <= 3.0]
        if self._self_latch and kind in (KIND_FOOTSTEP, KIND_GUNSHOT):
            kind = KIND_SELF

        feat["kind"] = kind
        intensity = map_intensity(snr, self._boost_now)
        if kind == KIND_SELF:
            intensity = min(intensity, 0.7)
            dist = 0.0
        elif kind == KIND_SILENT:
            intensity = max(intensity, 0.55)
            dist = float(self._dist)
        else:
            dist = float(self._dist)

        self._last_event_t = self._t   # finalize time, not the onset
        feat["fb"] = float(self._fb)
        return (float(az), float(elev), float(intensity),
                float(dist), int(kind), feat)

    def _finalize_roar(self, pend):
        """A loud dull onset whose level never decayed: sustained automatic
        fire (M4A1 ground truth - the burst sustains 0.04-0.20 rms with no
        quiet gaps). Emit as combat; suppress the player's own fire."""
        t, az, elev, feat, snr, lat = pend
        feat2 = dict(feat)
        self._acc_fractions(feat2)
        kind = KIND_GUNSHOT
        feat2["kind"] = kind
        if self._suppress_shots_now and abs(lat) < SELF_SHOT_LAT_DEG:
            kind = KIND_SELF
            feat2["kind"] = kind
        intensity = map_intensity(snr, self._boost_now)
        dist = 0.0 if kind == KIND_SELF else float(self._dist)
        if kind == KIND_SELF:
            intensity = min(intensity, 0.7)
        self._last_event_t = self._t
        feat2["fb"] = float(self._fb)
        return (float(az), float(elev), float(intensity),
                float(dist), int(kind), feat2)

    def _acc_fractions(self, feat):
        """Overwrite feat's spectral fractions from the rolling ~150 ms
        accumulated spectrum. A single 25 ms onset chunk misses the metallic
        ring of machine noise (which then looks like a footstep thump); the
        accumulated window sees the ring's mid-band energy."""
        acc = self._acc
        if acc is None:
            return
        total = sum(float(acc[k].sum()) for k in acc) + 1e-14
        feat["hf_frac"] = sum(float(acc[k].sum())
                              for k in ("high", "notch", "brill")) / total
        feat["lf_frac"] = float(acc["low"].sum()) / total
        feat["mid_frac"] = float(acc["mid"].sum()) / total

    def _learn_ambient(self, cue, dt):
        """Update the tilt / elevation baselines from a non-event chunk.
        Guarded against near-silence (tiny energies make meaningless ratios)."""
        el = cue["low"]; em = cue["mid"]
        eh = cue["high"]; en = cue["notch"]; eb = cue["brill"]
        if len(el) < 2 or len(em) < 2:
            return
        total = (el[0] + el[1] + em[0] + em[1] +
                 eh[0] + eh[1] + en[0] + en[1] + eb[0] + eb[1])
        if total < 1e-12:
            return  # digital silence: ratios are noise, keep the old baseline
        lo = el[0] + el[1] + 1e-14
        hi = eh[0] + eh[1] + 1e-14
        brill = eb[0] + eb[1] + 1e-14
        notch = en[0] + en[1] + 1e-14
        self._tilt_base.update(10.0 * math.log10(hi / lo), dt)
        self._elev_base.update(10.0 * math.log10(brill / notch), dt)


class SurroundLocalizer:
    """True multichannel localizer: channel balance in the user's gate band
    gives exact 360° (and height for 7.1.4). Same floor/SNR/boost pipeline and
    the same kind/distance output as the stereo path (own-sound suppression is
    not applied here - multichannel routing is rarely the player's own mix)."""

    def __init__(self):
        self._floor = NoiseFloor(init=-50.0, window=0.8, tau=0.25)
        self._kind = 0.0
        self._dist = 0.5

    def feed(self, data, samplerate, gate_low, gate_high, sensitivity,
             max_amp, footstep_boost, adaptive=True,
             suppress_own_steps=True, suppress_own_shots=True):
        n = data.shape[0]
        if n == 0:
            return None
        dt = n / float(samplerate)
        rms = band_rms(data, samplerate, gate_low, gate_high)
        bed = [float(rms[i]) for i in range(min(len(rms), 8))]
        gmax = max(bed) if bed else 0.0   # a rear/side channel alone must gate too
        level_db = 10.0 * math.log10(gmax * gmax + 1e-20)
        level_db = max(level_db, NoiseFloor.SILENCE_DB)
        self._floor.update(level_db, dt)
        loud_bypass = gmax > max_amp
        if gmax < sensitivity:
            return None
        snr = level_db - (self._floor.value if adaptive else NoiseFloor.SILENCE_DB)
        if snr < StereoLocalizer.MIN_SNR_DB and not loud_bypass:
            return None

        cue = band_energies(data, samplerate)
        feat = event_features(data, cue, rms, samplerate)
        feat["snr"] = float(snr)
        kind_target = float(classify_kind(feat, loud_bypass=loud_bypass))
        alpha = 1.0 if loud_bypass else min(1.0, dt / 0.06)
        self._kind += alpha * (kind_target - self._kind)
        kind = int(round(self._kind))
        feat["kind"] = kind

        ch = list(rms)   # unpadded: surround_azimuth reads height only when
        # the device really exposes >= 12 channels (7.1.4), so a padded zero
        # "top" can never bias a 5.1/7.1 stream downward.
        az, elev = surround_azimuth(ch)
        dist_raw = distance_estimate(snr)
        self._dist += min(1.0, dt / 0.15) * (dist_raw - self._dist)
        intensity = map_intensity(snr, footstep_boost)
        return (float(wrap_180(az)), float(elev), float(intensity),
                float(self._dist), int(kind), feat)
