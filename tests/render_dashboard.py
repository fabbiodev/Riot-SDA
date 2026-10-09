"""Render synthetic accounts; --live-artwork fetches public images only."""

import os
import sys
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PyQt6.QtWidgets import QApplication, QLabel
from PyQt6.QtTest import QTest
from app.styles import load_font, load_stylesheet
from app.ui.dashboard import Dashboard
from app.core.search import account_key
from test_dashboard import fixture_accounts

app = QApplication([])
app.setStyle("Fusion")
app.setFont(load_font())
app.setStyleSheet(load_stylesheet())
dashboard = Dashboard(load_artwork="--live-artwork" in sys.argv)
dashboard.resize(1080, 820)
dashboard.hidden_codes = True
preview_accounts = fixture_accounts()
if "--grid-fixtures" in sys.argv:
    preview_accounts[0]["games"]["lol"]["characters"].extend([
        {"id": identifier, "name": name, "role": role, "kind": "owned", "skin_ids": []}
        for identifier, name, role in [("84", "Акали", "Убийца"), ("22", "Эш", "Стрелок"),
                                       ("99", "Люкс", "Маг"), ("89", "Леона", "Танк"),
                                       ("25", "Моргана", "Маг"), ("157", "Ясуо", "Боец")]])
dashboard.set_accounts(preview_accounts)
dashboard.session_info[account_key(dashboard.current_account)] = {
    "status": "ready", "expires_at": time.time() + 3235, "last_refresh": time.time(),
    "cookie_expires_at": time.time() + 86400, "saved": True}
dashboard.tick()
dashboard.findChild(QLabel, "footer").setText("Демонстрационные аккаунты и коллекции · предпросмотр интерфейса")
dashboard.show()
app.processEvents()
if "--live-artwork" in sys.argv:
    QTest.qWait(12000)
output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
dashboard.grab().save(str(output / "league.png"))
dashboard.set_game("valorant")
dashboard.set_collection("skins")
app.processEvents()
QTest.qWait(250)
if "--live-artwork" in sys.argv:
    QTest.qWait(12000)
dashboard.grab().save(str(output / "valorant.png"))

dashboard.set_collection("characters")
QTest.qWait(250)
if "--live-artwork" in sys.argv:
    QTest.qWait(5000)
dashboard.grab().save(str(output / "valorant-agents.png"))

dashboard.set_game("lol")
dashboard.set_collection("skins")
QTest.qWait(250)
if "--live-artwork" in sys.argv:
    QTest.qWait(5000)
dashboard.grab().save(str(output / "league-skins.png"))
dashboard.account_search.setText("missing")
app.processEvents()
QTest.qWait(250)
dashboard.grab().save(str(output / "empty.png"))

profile = fixture_accounts()[0]
profile.pop("seed")
profile["local_id"] = "demo-profile"
dashboard.account_search.clear()
dashboard.set_game("lol")
dashboard.set_accounts([profile])
dashboard.session_info[account_key(profile)] = {
    "status": "ready", "expires_at": time.time() + 3235, "last_refresh": time.time(), "saved": True}
dashboard.tick()
QTest.qWait(250)
dashboard.grab().save(str(output / "profile.png"))
print("Rendered 6 views with synthetic accounts")
