import os
import time
import wave

import numpy as np
import soundcard as sc
from PyQt6.QtCore import QThread, pyqtSignal

from direction import (StereoLocalizer, SurroundLocalizer, band_rms,
                       stereo_angle)

# Raw audio snippet recording (for calibration with REAL game sounds):
# when Event log is on, every footstep/gunshot/own-sound event also saves a
# ~0.55 s WAV (0.5 s of history + the current chunk) into recordings/.
RING_FRAMES = 24000          # 0.5 s at 48 kHz
MAX_SNIPPETS = 500           # per session (~40 MB)

# Analysis chunk: 25 ms (was 50 ms upstream). Halving the chunk cuts the
# radar's pointing latency in half - the player's view spins constantly in
# firefights, so every millisecond of tracking lag is visible on the radar.
CHUNK_FRAMES = 1200

# HRTF mode values (also referenced by main.py / dashboard):
#   "auto"       - true surround when the loopback device really exposes
#                  >= 6 active channels, otherwise 360° HRTF stereo.
#   "stereo360"  - always the HRTF stereo localizer (works with stereo loopback).
#   "stereo-lr"  - classic left/right only (upstream behaviour, [-90, +90]).
#   "surround"   - multichannel balance only; falls back to stereo360 on a
#                  stereo device.
HRTF_MODES = ("auto", "stereo360", "stereo-lr", "surround")


class AudioCaptureThread(QThread):
    # (azimuth°, elevation -1..+1, intensity 0..1, distance 0..1, kind) per
    # detected event chunk. kind: 0 other / 1 footstep / 2 gunshot / 3 self.
    audio_data_signal = pyqtSignal(float, float, float, float, int, float)
    # (device label, channel count, mode description)
    device_info_signal = pyqtSignal(str, int, str)
    # Human-readable capture problems (device gone, no loopback, fallbacks).
    status_signal = pyqtSignal(str)

    def __init__(self, sensitivity=0.005, gain=1.0, freq_low=20, freq_high=20000,
                 max_amplitude=1.0, adaptive_gate=True, footstep_boost=2.0,
                 front_bias=0.0, hrtf_mode="auto",
                 show_gunshots=True, suppress_own_steps=True,
                 suppress_own_shots=True, log_events=False,
                 target_pid=None, target_name=None):
        super().__init__()
        self.sensitivity = sensitivity
        self.gain = gain
        self.freq_low = freq_low
        self.freq_high = freq_high
        self.max_amplitude = max_amplitude  # louder than this = gunshot-class events
        # SSD fork additions:
        self.adaptive_gate = adaptive_gate      # adaptive noise floor on/off
        self.footstep_boost = footstep_boost    # SNR boost for faint events
        self.front_bias = front_bias            # -1..+1 HRTF front/back tuning
        self.hrtf_mode = hrtf_mode if hrtf_mode in HRTF_MODES else "auto"
        self.show_gunshots = show_gunshots      # emit gunshot events at all
        self.suppress_own_steps = suppress_own_steps
        self.suppress_own_shots = suppress_own_shots
        self.log_events = log_events            # write events.csv for tuning
        self.log_path = None
        self.record_dir = None                  # recordings/ parent folder
        self.running = True
        self.samplerate = 48000
        self.target_pid = target_pid
        self.target_name = target_name
        # Stateful localizers - recreated when the thread is recreated.
        self._hrtf = StereoLocalizer(front_bias=self.front_bias)
        self._surround = SurroundLocalizer()
        self._log_fh = None
        # Raw-snippet recorder state.
        self._snippet_dir = None
        self._snippets_written = 0
        self._ring = np.zeros((RING_FRAMES, 2), dtype=np.float32)
        self._ring_pos = 0

    def set_target(self, pid, name=None):
        """Choose the capture source. Only takes effect before the thread starts."""
        self.target_pid = pid
        self.target_name = name

    def set_sensitivity(self, sensitivity):
        self.sensitivity = sensitivity

    def set_gain(self, gain):
        self.gain = gain

    def set_freq_range(self, low, high):
        self.freq_low = low
        self.freq_high = high

    def set_max_amplitude(self, max_amp):
        self.max_amplitude = max_amp

    def set_adaptive_gate(self, enabled):
        self.adaptive_gate = bool(enabled)

    def set_footstep_boost(self, boost):
        self.footstep_boost = boost

    def set_front_bias(self, bias):
        self.front_bias = float(bias)
        self._hrtf.front_bias = float(bias)

    def set_hrtf_mode(self, mode):
        if mode in HRTF_MODES:
            self.hrtf_mode = mode

    def set_show_gunshots(self, enabled):
        self.show_gunshots = bool(enabled)

    def set_suppress_own_steps(self, enabled):
        self.suppress_own_steps = bool(enabled)

    def set_suppress_own_shots(self, enabled):
        self.suppress_own_shots = bool(enabled)

    def set_log_events(self, enabled):
        self.log_events = bool(enabled)

    def set_log_path(self, path):
        self.log_path = path

    def set_record_dir(self, path):
        self.record_dir = path

    def _open_log(self):
        if not self.log_events or not self.log_path:
            return
        try:
            self._log_fh = open(self.log_path, "w", encoding="utf-8")
            self._log_fh.write(
                "t,kind,azimuth,elevation,distance,intensity,snr_db,"
                "crest_db,hf_frac,lf_frac,gmax,level_db,attack_db,tilt_db\n")
        except Exception as e:
            print(f"Event log unavailable ({e})")
            self._log_fh = None

    def _close_log(self):
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None

    def _log_event(self, t, az, elev, intensity, dist, kind, feat):
        if self._log_fh is None:
            return
        try:
            self._log_fh.write(
                f"{t:.3f},{kind},{az:.1f},{elev:.2f},{dist:.2f},{intensity:.2f},"
                f"{feat.get('snr', 0):.1f},{feat.get('crest_db', 0):.1f},"
                f"{feat.get('hf_frac', 0):.3f},{feat.get('lf_frac', 0):.3f},"
                f"{feat.get('gmax', 0):.5f},{feat.get('level_db', 0):.1f},"
                f"{feat.get('attack_db', 0):.1f},{feat.get('tilt_db', 0):.1f}\n")
        except Exception:
            pass

    # ── Raw audio snippet recording ─────────────────────────────────────
    def _feed_ring(self, data):
        """Keep the last 0.5 s of (up to 2-channel) audio for snippets."""
        ch = min(data.shape[1], 2)
        n = data.shape[0]
        if n == 0:
            return
        if n >= RING_FRAMES:
            self._ring[:] = data[-RING_FRAMES:, :ch]
            self._ring_pos = 0
            return
        pos = self._ring_pos
        if pos + n <= RING_FRAMES:
            self._ring[pos:pos + n] = data[:, :ch]
        else:
            k = RING_FRAMES - pos
            self._ring[pos:] = data[:k, :ch]
            self._ring[:n - k] = data[k:, :ch]
        self._ring_pos = (pos + n) % RING_FRAMES

    def _record_snippet(self, data, kind):
        """Save the real game audio around an event as a 16-bit WAV so the
        classification thresholds can later be calibrated against actual
        footstep/gunshot recordings."""
        if not self.log_events or self.record_dir is None:
            return
        if kind not in (1, 2, 3, 4):      # footstep / gunshot / self / silent
            return
        if self._snippets_written >= MAX_SNIPPETS:
            return
        try:
            if self._snippet_dir is None:
                stamp = time.strftime("%Y%m%d_%H%M%S")
                self._snippet_dir = os.path.join(self.record_dir, f"session_{stamp}")
                os.makedirs(self._snippet_dir, exist_ok=True)
            ch = min(data.shape[1], 2)
            ring = np.concatenate(
                [self._ring[self._ring_pos:], self._ring[:self._ring_pos]], axis=0)
            snippet = np.concatenate([ring, data[:, :ch]], axis=0).astype(np.float32)
            name = {1: "footstep", 2: "gunshot", 3: "self", 4: "silent"}[kind]
            path = os.path.join(self._snippet_dir,
                                f"{name}_{self._snippets_written:04d}.wav")
            pcm16 = (np.clip(snippet, -1.0, 1.0) * 32767).astype(np.int16)
            with wave.open(path, "wb") as w:
                w.setnchannels(ch)
                w.setsampwidth(2)
                w.setframerate(self.samplerate)
                w.writeframes(pcm16.tobytes())
            self._snippets_written += 1
        except Exception:
            pass

    def run(self):
        # Per-app capture takes the process-loopback path; otherwise capture the
        # whole system mix the way we always have.
        self._open_log()
        try:
            if self.target_pid:
                self._run_process_loopback()
            else:
                self._run_system_loopback()
        finally:
            self._close_log()

    def _run_process_loopback(self):
        """Capture a single program's audio. Falls back to system audio on failure."""
        try:
            from process_loopback import ProcessLoopbackCapture
        except Exception as e:
            print(f"Process loopback unavailable ({e}); using system audio.")
            self.status_signal.emit("Per-app capture unavailable - using system audio")
            self._run_system_loopback()
            return

        cap = ProcessLoopbackCapture(self.target_pid, samplerate=self.samplerate, channels=2)
        try:
            cap.start()
        except Exception as e:
            print(f"Process loopback failed ({e}); using system audio.")
            self.status_signal.emit("Per-app capture failed - using system audio")
            self._run_system_loopback()
            return

        label = self.target_name or f"PID {self.target_pid}"
        mode = "HRTF 360°" if self.hrtf_mode != "stereo-lr" else "Stereo L/R"
        print(f"Capturing app audio: {label} (per-app, Stereo L/R)")
        self.device_info_signal.emit(f"{label} (per-app)", 2, mode)
        try:
            while self.running:
                data = cap.read(CHUNK_FRAMES)
                # _process_chunk (not _process_stereo directly): it also
                # feeds the 0.5 s ring buffer used by the snippet recorder -
                # skipping it left every recorded WAV's pre-ring silent.
                self._process_chunk(data, False)
        except Exception as e:
            print(f"Process loopback capture error: {e}")
            if self.running:
                self.status_signal.emit(
                    f"Capture of {label} stopped unexpectedly - restart the radar")
        finally:
            cap.close()

    def _run_system_loopback(self):
        try:
            mics = sc.all_microphones(include_loopback=True)
            loopbacks = [m for m in mics if m.isloopback]

            if not loopbacks:
                print("No loopback device found.")
                self.status_signal.emit(
                    "No audio output device found - the radar can't capture anything")
                return

            # Pick device by priority
            device = None
            try:
                default_name = sc.default_speaker().name
                for lb in loopbacks:
                    if default_name in lb.name:
                        device = lb
                        break
            except Exception:
                pass

            if not device:
                for lb in loopbacks:
                    if "Microphone" not in lb.name:
                        device = lb
                        break

            if not device:
                device = loopbacks[0]

            print(f"Using loopback device: {device.name}")
            self._capture_loop(device, loopbacks)

        except Exception as e:
            print(f"Error in audio capture: {e}")
            import traceback
            traceback.print_exc()
            if self.running:
                self.status_signal.emit(f"Audio capture error: {e}")

    def _capture_loop(self, device, all_loopbacks):
        try:
            with device.recorder(samplerate=self.samplerate) as mic:
                first_data = mic.record(numframes=CHUNK_FRAMES)
                raw_channels = first_data.shape[1]

                # True surround only when the loopback actually exposes >= 6
                # channels AND the extra channels carry signal. Most "7.1"
                # headsets still present a stereo loopback - for those the HRTF
                # stereo localizer reconstructs the full 360° instead.
                use_surround = False
                if raw_channels >= 6:
                    surround_max = max(
                        float(np.max(np.abs(first_data[:, ch])))
                        for ch in range(2, min(raw_channels, 8))
                    )
                    if surround_max > 0.0001:
                        use_surround = True

                if use_surround and self.hrtf_mode == "stereo-lr":
                    use_surround = False
                if not use_surround and self.hrtf_mode == "surround" and raw_channels >= 6:
                    use_surround = True

                effective = raw_channels if use_surround else min(raw_channels, 2)
                if use_surround:
                    mode = "Surround 360°" + ("+height" if raw_channels >= 10 else "")
                elif self.hrtf_mode == "stereo-lr":
                    mode = "Stereo L/R"
                else:
                    mode = "HRTF 360°"
                print(f"Channels: {raw_channels} | Mode: {mode}")
                self.device_info_signal.emit(device.name, effective, mode)

                self._process_chunk(first_data, use_surround)

                while self.running:
                    data = mic.record(numframes=CHUNK_FRAMES)
                    self._process_chunk(data, use_surround)

        except RuntimeError as e:
            print(f"Device '{device.name}' failed: {e}")
            if self.running:
                self.status_signal.emit(
                    f"Audio device '{device.name}' failed - trying another output")
            for lb in all_loopbacks:
                if lb.name == device.name or "Microphone" in lb.name:
                    continue
                try:
                    time.sleep(0.5)
                    self._capture_loop(lb, [])
                    return
                except Exception:
                    continue
            if self.running and all_loopbacks:
                self.status_signal.emit(
                    "All audio devices failed - stop and restart the radar")

    # ── Analysis (SSD fork) ────────────────────────────────────────────────
    # The gain knob multiplies the GATE-band level only (as upstream did), so
    # it does not distort the HRTF cue bands used for localization.
    def _effective_sensitivity(self):
        return self.sensitivity * (1.0 / max(self.gain, 1e-6))

    def _process_chunk(self, data, use_surround):
        self._feed_ring(data)
        if use_surround and data.shape[1] >= 6:
            self._process_surround(data)
        elif data.shape[1] >= 2:
            self._process_stereo(data)
        elif data.shape[1] == 1:
            rms = band_rms(data, self.samplerate, self.freq_low, self.freq_high)
            intensity = float(max(rms[0], 0.0))
            if intensity > self.sensitivity and intensity < self.max_amplitude:
                self.audio_data_signal.emit(0.0, 0.0, min(1.0, intensity * 5.0),
                                            0.5, 0, 0.0)

    def _process_surround(self, data):
        res = self._surround.feed(
            data, self.samplerate,
            self.freq_low, self.freq_high,
            self._effective_sensitivity(), self.max_amplitude,
            self.footstep_boost, adaptive=self.adaptive_gate)
        if res is not None:
            az, elev, intensity, dist, kind, feat = res
            if kind == 2 and not self.show_gunshots:
                return
            self._log_event(self._surround_t, az, elev, intensity, dist, kind, feat)
            self._record_snippet(data, kind)
            self.audio_data_signal.emit(az, elev, intensity, dist, kind,
                                        feat.get("fb", 0.0))

    _surround_t = 0.0  # surround localizer has no internal clock: count chunks

    def _process_stereo(self, data):
        if self.hrtf_mode == "stereo-lr":
            rms = band_rms(data, self.samplerate, self.freq_low, self.freq_high) * self.gain
            left_rms, right_rms = float(rms[0]), float(rms[1])
            intensity = max(left_rms, right_rms)
            if intensity > self.sensitivity and intensity < self.max_amplitude:
                angle_deg = stereo_angle(left_rms, right_rms)
                self.audio_data_signal.emit(
                    float(angle_deg), 0.0, min(1.0, intensity * 5.0), 0.5, 0, 0.0)
            return

        res = self._hrtf.feed(
            data, self.samplerate,
            self.freq_low, self.freq_high,
            self._effective_sensitivity(), self.max_amplitude,
            self.footstep_boost, adaptive=self.adaptive_gate,
            suppress_own_steps=self.suppress_own_steps,
            suppress_own_shots=self.suppress_own_shots)
        if res is not None:
            az, elev, intensity, dist, kind, feat = res
            if kind == 2 and not self.show_gunshots:
                return
            self._log_event(self._hrtf._t, az, elev, intensity, dist, kind, feat)
            self._record_snippet(data, kind)
            self.audio_data_signal.emit(az, elev, intensity, dist, kind,
                                        feat.get("fb", 0.0))

    def stop(self):
        self.running = False
        self.wait()
