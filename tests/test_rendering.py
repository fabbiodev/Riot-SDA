"""Exercise Qt in fresh processes: DPI must be configured before QApplication."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


@unittest.skipUnless(sys.platform == "win32", "Windows pixel sizing")
class FixedWindowsScaleTests(unittest.TestCase):
    def test_geometry_and_font_pixels_stay_identical_with_inherited_scale_settings(self):
        script = """
import json
from app.styles import configure_rendering, load_font
configure_rendering()
from PyQt6.QtWidgets import QApplication, QLabel
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFontMetrics
app = QApplication([])
app.setFont(load_font())
w = QLabel('League of Legends · Легендарный')
w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
w.resize(1080, 820)
w.show()
app.processEvents()
image = w.grab()
print(json.dumps({'size': [image.width(), image.height()], 'dpr': w.devicePixelRatioF(),
                  'text_width': QFontMetrics(w.font()).horizontalAdvance(w.text())}))
w.close()
"""
        samples = []
        for scale in ("1", "1.25", "1.5"):
            env = {**os.environ, "QT_QPA_PLATFORM": "windows:fontengine=directwrite",
                   "QT_SCALE_FACTOR": scale, "QT_SCREEN_SCALE_FACTORS": scale,
                   "QT_ENABLE_HIGHDPI_SCALING": "1"}
            result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                                    text=True, timeout=30, check=True, cwd=Path(__file__).resolve().parents[1])
            samples.append(json.loads(result.stdout))
        self.assertEqual(samples[0]["size"], [1080, 820])
        self.assertEqual(samples[0]["dpr"], 1.0)
        self.assertEqual(samples, [samples[0]] * 3)
