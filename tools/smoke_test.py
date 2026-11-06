"""Full-app smoke test: boots the real AudioRadarApp (dashboard + overlay +
capture thread machinery), lets it run a few seconds, then shuts down cleanly.

Run offscreen (no windows pop up):
    $env:QT_QPA_PLATFORM='offscreen'; python tools/smoke_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

import main


def run():
    app = QApplication(sys.argv)
    win = main.AudioRadarApp()
    win.show()

    def probe():
        print("app alive, radar_active =", win.radar_active)
        # Exercise the audio pipeline entry points without hardware: the
        # localizer is pure numpy, so feed a synthetic chunk directly.
        import numpy as np
        rng = np.random.default_rng(0)
        x = rng.standard_normal(2400).astype(np.float32) * 0.02
        thread = main.AudioCaptureThread()
        got = []
        thread.audio_data_signal.connect(
            lambda az, el, it, d, k: got.append(
                (round(az, 1), round(el, 2), round(it, 2), round(d, 2), k)))
        thread._process_stereo(np.stack([x, x], axis=1))
        print("synthetic chunk ->", got)
        win.close()
        app.quit()

    QTimer.singleShot(5000, probe)
    app.exec()
    print("clean exit")


if __name__ == "__main__":
    run()
