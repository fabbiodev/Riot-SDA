import os

from app.core.paths import resource_path


def load_font():
    from PyQt6.QtGui import QFont, QFontDatabase
    path = resource_path(os.path.join("app", "assets", "fonts", "Inter-V.ttf"))
    font_id = QFontDatabase.addApplicationFont(path)
    families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
    return QFont(families[0] if families else "Inter", 10)


def load_stylesheet():
    path = resource_path(os.path.join("app", "assets", "style.qss"))
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().replace("__IMAGE_ROOT__", resource_path("images").replace("\\", "/"))
    except OSError:
        return ""
