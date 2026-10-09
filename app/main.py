import os
import sys
import json

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon, QPixmap

from app.styles import load_stylesheet, load_font, configure_rendering
from app.core.paths import resource_path
from app.core import debug_log
from app.ui import MainWindow

ICON_PATH = resource_path(os.path.join("images", "icon.ico"))


def _set_app_id():
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "sysys.riot2fa.desktop"
            )
        except Exception:
            pass


def main():
    log_path = debug_log.init()
    if log_path:
        import logging

        from app.version import __version__

        logging.getLogger(__name__).info(
            "Riot2FA v%s starting (debug logging active) -> %s", __version__, log_path
        )
    _set_app_id()
    configure_rendering()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(load_font())
    app.setStyleSheet(load_stylesheet())
    app.setWindowIcon(QIcon(ICON_PATH))
    if "--check-ui" in sys.argv:
        from PyQt6.QtNetwork import QSslSocket
        from app.ui.dashboard import Dashboard
        from app.api.rankings import TIERS
        from app.core.session_store import _dpapi, VaultError
        from pathlib import Path
        index = sys.argv.index("--check-ui")
        dashboard = Dashboard(load_artwork=False)
        dashboard.resize(1080, 820)
        dashboard.set_accounts([])
        try:
            protected_sessions = _dpapi(_dpapi(b"synthetic-ui-self-check"), decrypt=True) == b"synthetic-ui-self-check"
        except VaultError:
            protected_sessions = False
        from app.version import __version__
        from PyQt6.QtGui import QFont, QFontInfo
        summary = {"ok": "Inter" in app.font().family(), "font": app.font().family(),
                   "version": __version__,
                   "font_actual": QFontInfo(app.font()).family(),
                   "dpi_scale": app.primaryScreen().devicePixelRatio(),
                   "ui_size_pixels": [round(dashboard.width() * dashboard.devicePixelRatioF()),
                                      round(dashboard.height() * dashboard.devicePixelRatioF())],
                   "font_antialias": bool(app.font().styleStrategy() & QFont.StyleStrategy.PreferAntialias),
                   "account_search": bool(dashboard.account_search),
                   "inventory_search": bool(dashboard.inventory_search),
                   "artwork_delegate": bool(dashboard.inventory_grid.itemDelegate()),
                   "inventory_grid": dashboard.inventory_grid.viewMode() == dashboard.inventory_grid.ViewMode.IconMode,
                   "rank_cards": sorted(dashboard.rank_values),
                   "session_ui": bool(dashboard.session_label), "session_dpapi": protected_sessions,
                   "client_switch_ui": bool(dashboard.launch_button),
                   "rank_assets": all(not QPixmap(resource_path(f"app/assets/ranks/{product}-{tier}.png")).isNull()
                                      for product in ("lol", "tft") for tier in TIERS),
                   "tls": QSslSocket.supportsSsl()}
        summary["ok"] = summary["ok"] and all(summary[field] for field in (
            "font_antialias", "account_search", "inventory_search", "artwork_delegate", "inventory_grid", "rank_assets", "session_ui", "session_dpapi", "client_switch_ui", "tls"))
        Path(sys.argv[index + 1]).write_text(json.dumps(summary), encoding="utf-8")
        return
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
