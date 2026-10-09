import os
import sys

from app.core.paths import resource_path


def configure_rendering():
    # DirectWrite supports the bundled variable font and fractional Windows DPI.
    # Keep explicit platform choices (including offscreen UI checks) intact.
    if sys.platform == "win32":
        os.environ.setdefault("QT_QPA_PLATFORM", "windows:fontengine=directwrite")


def load_font():
    from PyQt6.QtGui import QFont, QFontDatabase
    path = resource_path(os.path.join("app", "assets", "fonts", "Inter-V.ttf"))
    font_id = QFontDatabase.addApplicationFont(path)
    families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
    font = QFont(families[0] if families else "Segoe UI", 10)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.PreferQuality)
    # Align small glyphs vertically without forcing uneven horizontal spacing.
    font.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
    return font


def load_stylesheet():
    path = resource_path(os.path.join("app", "assets", "style.qss"))
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().replace("__IMAGE_ROOT__", resource_path("images").replace("\\", "/"))
    except OSError:
        return ""
