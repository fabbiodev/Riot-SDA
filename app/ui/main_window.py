import os
import time
import logging
import threading
import webbrowser
import copy
import uuid

from PyQt6.QtWidgets import (
    QMainWindow,
    QMessageBox,
    QDialog,
    QInputDialog,
    QSystemTrayIcon,
    QMenu,
    QApplication,
    QProgressDialog,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon

from app.core import load_accounts, save_accounts
from app.core.share import export_account, import_account, ShareError
from app.core.fcm_service import FcmService
from app.core import updater
from app.core import patchright_login
from app.core.paths import resource_path
from app.version import __version__
from app.api import (
    is_valid_jwt,
    describe_exception,
    fetch_mfa_factors,
    is_email_mfa_enabled,
    enable_mfa,
    verify_mfa,
    register_mfa_push_device,
    mint_access_token,
    parse_qr_login,
    qr_session_info,
    qr_approve,
    fetch_new_csrf_token,
    fetch_account_user,
    riot_id_from_user,
    puuid_from_user,
)
from app.ui.toast import Toast
from app.ui.mfa_prompt_dialog import MfaPromptDialog
from app.ui.qr_confirm_dialog import QrConfirmDialog
from app.ui.error_dialog import show_error
from app.ui.share_dialog import ShareCodeDialog
from app.ui.update_dialog import UpdateDialog
from app.core import debug_log
from app.core.debug_log import mask
from app.core.search import account_key
from app.core.sessions import SessionManager
from app.core.session_store import MemorySessionStore
from app.core.client_sessions import ClientSessionManager
from app.core.account_details import AccountDetailsManager, MemoryDetailsStore
from app.api.account_details import extract_details, AccountDetailsError
from app.ui.account_details_dialog import AccountDetailsDialog
from app.api.developer_api import DeveloperApi, DataError
from app.api.game_clients import fetch_collection
from app.api.rankings import merge_rankings, rank_region, resolve_rank_region
from app.api.opgg import fetch_auto_rankings
from app.core.catalog import load_catalog, save_catalog, league_catalog
from app.core.skin_types import fetch_skin_types
from app.ui.dashboard import Dashboard, DataSettingsDialog

ICON_PATH = resource_path(os.path.join("images", "icon.ico"))

log = logging.getLogger(__name__)

class _UpdateCancelled(Exception):
    """Raised inside the download loop when the user cancels the update."""

class MainWindow(QMainWindow):
    _update_found = pyqtSignal(dict)
    _login_result = pyqtSignal(dict)
    _import_result = pyqtSignal(dict)
    _update_progress = pyqtSignal(int, int)
    _update_ready = pyqtSignal(str)
    _update_error = pyqtSignal(str)
    _game_data_result = pyqtSignal(dict)
    _skin_type_result = pyqtSignal(str, dict)

    def __init__(self, accounts=None, start_services=True):
        super().__init__()
        self.setWindowTitle(f"Riot Auth  ·  v{__version__}")
        self.setMinimumSize(900, 640)
        self.resize(1080, 820)

        self.accounts = load_accounts() if accounts is None else accounts
        self.sessions = SessionManager(self, store=None if start_services else MemorySessionStore(), autostart=start_services)
        self._profile_sessions = self.sessions.sessions
        self._pending_qr = {}
        self._qr_scanner = None
        QApplication.instance().aboutToQuit.connect(self._close_qr_scanner)
        self.api_keys = {"lol": os.getenv("RIOT_API_KEY", ""),
                         "tft": os.getenv("RIOT_TFT_API_KEY", ""),
                         "valorant": os.getenv("RIOT_VALORANT_API_KEY", "")}
        self._data_jobs = set()
        self._skin_type_jobs = set()
        self._skin_types_auto_enabled = start_services
        self._skin_type_result.connect(self._on_skin_types)
        self._rank_auto_enabled = start_services
        self._rank_pending = set()
        self._rank_running = set()
        self._rank_waiting_sessions = set()
        self._rank_not_before = 0
        self._rank_stopped = False
        self._game_data_result.connect(self._on_game_data_result)
        self.dashboard = Dashboard(self, load_artwork=start_services)
        self.dashboard.catalogs = {game: load_catalog(game) for game in ("lol", "valorant")}
        self.setCentralWidget(self.dashboard)
        self.dashboard.action_requested.connect(self._dashboard_action)
        self.dashboard.refresh_requested.connect(self._refresh_game_data)
        self.dashboard.copied.connect(lambda: self.toast.popup("Код скопирован"))
        self.sessions.changed.connect(self._update_session_status)
        self.sessions.ready.connect(self._on_session_ready)
        QApplication.instance().aboutToQuit.connect(self.sessions.stop)
        self.toast = Toast(self.dashboard)

        self.client_sessions = ClientSessionManager(self.sessions, self, enabled=start_services)
        self.client_sessions.changed.connect(self._update_client_status)
        self.client_sessions.failed.connect(lambda message: QMessageBox.warning(self, "Riot Client", message))
        self.client_sessions.launched.connect(lambda key: self.toast.popup("Riot Client открыт с выбранным аккаунтом"))
        QApplication.instance().aboutToQuit.connect(self.client_sessions.stop)

        self.account_details = AccountDetailsManager(self.sessions, self, enabled=start_services,
                                                     store=None if start_services else MemoryDetailsStore())
        QApplication.instance().aboutToQuit.connect(self.account_details.stop)

        self._populate()
        if start_services:
            QTimer.singleShot(0, self.sessions.poll)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(250)

        self._active_prompts = []
        self._tray_hint_shown = False
        if start_services:
            self._setup_tray()
        else:
            self.tray = QSystemTrayIcon(QIcon(ICON_PATH), self)

        self.fcm = FcmService(self)
        self.fcm.push_received.connect(self._on_push)
        if start_services:
            self.fcm.start()

        self._update_found.connect(self._on_update_found)
        self._login_result.connect(self._on_login_result)
        self._import_result.connect(self._on_import_result)
        self._update_progress.connect(self._on_update_progress)
        self._update_ready.connect(self._on_update_ready)
        self._update_error.connect(self._on_update_error)
        self._progress = None
        self._update_cancelled = False
        self._login_busy = False
        self.rank_timer = QTimer(self)
        self.rank_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.rank_timer.setInterval(60 * 60 * 1000)
        self.rank_timer.timeout.connect(self._queue_rank_refresh)
        self.rank_timer.timeout.connect(self._refresh_skin_types)
        self.rank_queue_timer = QTimer(self)
        self.rank_queue_timer.setInterval(2000)
        self.rank_queue_timer.timeout.connect(self._drain_rank_queue)
        QApplication.instance().aboutToQuit.connect(self._stop_rank_refresh)
        if start_services:
            self.rank_timer.start()
            self.rank_queue_timer.start()
            QTimer.singleShot(0, self._queue_rank_refresh)
            QTimer.singleShot(0, self._refresh_skin_types)
        # This local fork must not replace itself with upstream's original UI.
        # Updater functionality is retained but no automatic upstream install is offered.

    def _dashboard_action(self, action):
        handlers = {"add": self._add_via_login, "import": self._import_account,
                    "qr": self._scan_qr, "settings": self._data_settings,
                    "requests": self._show_requests}
        if action in handlers:
            handlers[action]()
            return
        account = self.dashboard.current_account
        if not account:
            return
        if action == "connect-2fa":
            if not account.get("seed"):
                self._add_via_login(connect_2fa=True, target=copy.deepcopy(account))
        elif action == "client-launch":
            self.client_sessions.launch(account)
        elif action == "account-details":
            dialog = AccountDetailsDialog(account, self.account_details, self)
            self.account_details.refresh(account)
            dialog.exec()
        elif action == "client-login":
            self._add_via_login(target=copy.deepcopy(account), refresh_session=True)
        elif action == "session-refresh":
            key = account_key(account)
            if key not in self.sessions.sessions or self.sessions.sessions[key].get("status") in ("login", "forgotten", "token_only"):
                self._add_via_login(target=copy.deepcopy(account), refresh_session=True)
            else:
                self.sessions.request(key, force=True)
        elif action == "session-login":
            self._add_via_login(target=copy.deepcopy(account), refresh_session=True)
        elif action == "session-forget":
            self._pending_qr.pop(account_key(account), None)
            self.sessions.forget(account)
            self.toast.popup("QR-сессия удалена с этого компьютера")
        elif action in ("edit-login", "edit-riot-id"):
            field = "login" if action == "edit-login" else "name"
            title = "Логин для поиска" if field == "login" else "Riot ID для API"
            prompt = "Имя входа Riot (без пароля):" if field == "login" else "Полный Riot ID: ник#тег"
            text, ok = QInputDialog.getText(self, title, prompt, text=account.get(field, ""))
            if ok:
                text = text.strip()
                if field == "name" and (not text.rpartition("#")[0] or not text.rpartition("#")[2]):
                    QMessageBox.warning(self, title, "Введите полный Riot ID: ник#тег.")
                    return
                account[field] = text
                if field == "name":
                    # A renamed lookup target must not keep another profile's data.
                    account.pop("games", None)
                self._save_and_refresh()
                if field == "name" and self._rank_auto_enabled:
                    self._queue_rank_refresh([account])
        elif action == "share":
            if account.get("seed"):
                self._share_account(account["name"], account["seed"])
        elif action == "remove":
            answer = QMessageBox.question(self, "Удалить аккаунт", f"Удалить {account['name']} из приложения?\n"
                "Сессия Riot и серверная 2FA при этом не отключаются.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                self._remove_account(account["name"], account.get("seed"), account_key(account))

    def _show_requests(self):
        if not self._active_prompts:
            self.toast.popup("Новых запросов на вход нет")
        for prompt in self._active_prompts:
            prompt.show()
            prompt.raise_()
            prompt.activateWindow()

    def _data_settings(self):
        account = self.dashboard.current_account
        dialog = DataSettingsDialog(self.api_keys, account, self.dashboard.hidden_codes,
                                    self.dashboard.compact, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        self.api_keys = {"lol": dialog.lol_key.text().strip(), "tft": dialog.tft_key.text().strip(),
                         "valorant": dialog.val_key.text().strip()}
        if account:
            old_region = rank_region(account)
            manual = dialog.lol_region.currentIndex() > 0
            account["api_routes"] = {"lol": dialog.lol_region.currentText() if manual else "",
                                     "lol_manual": manual,
                                     "valorant": dialog.val_region.currentText()}
            if old_region != rank_region(account):
                account.get("games", {}).get("lol", {}).pop("ranks", None)
            save_accounts(self.accounts)
            self._populate()
            if self._rank_auto_enabled:
                QTimer.singleShot(0, lambda: self._queue_rank_refresh([account]))
        self.dashboard.apply_preferences(dialog.hidden_codes.isChecked(), dialog.compact.isChecked())
        return True

    def _queue_rank_refresh(self, accounts=None):
        if self._rank_stopped:
            return
        for account in self.accounts if accounts is None else accounts:
            name, sep, tag = account.get("name", "").rpartition("#")
            key = account_key(account)
            if sep and name.strip() and tag.strip() and key not in self._rank_running:
                self._rank_pending.add(key)
        self._drain_rank_queue()

    def _drain_rank_queue(self):
        if self._rank_stopped or self._rank_running or time.monotonic() < self._rank_not_before:
            return
        existing = {account_key(a) for a in self.accounts}
        self._rank_pending.intersection_update(existing)
        self._rank_waiting_sessions.intersection_update(existing)
        for account in self.accounts:
            key = account_key(account)
            if key in self._rank_pending and (key, "lol") not in self._data_jobs:
                record = self._profile_sessions.get(key, {})
                if (not account.get("api_routes", {}).get("lol_manual") and not self.sessions.token(key)
                        and record.get("sso", {}).get("ssid")
                        and record.get("status") not in ("login", "forgotten")):
                    if key not in self._rank_waiting_sessions:
                        self._rank_waiting_sessions.add(key)
                        self.sessions.request(key)
                    continue
                self._rank_pending.remove(key)
                self._rank_running.add(key)
                self._start_game_data(account, "lol", "ranks", rank_region(account))
                break

    def _stop_rank_refresh(self):
        self._rank_stopped = True
        self._rank_pending.clear()
        self._rank_waiting_sessions.clear()
        self.rank_timer.stop()
        self.rank_queue_timer.stop()

    def _refresh_skin_types(self, game=None):
        if not self._skin_types_auto_enabled or self._rank_stopped:
            return
        for current in ((game,) if game else ("lol", "valorant")):
            if current not in self._skin_type_jobs:
                self._skin_type_jobs.add(current)
                threading.Thread(target=self._skin_types_worker, args=(current,), daemon=True).start()

    def _skin_types_worker(self, game):
        try:
            data = fetch_skin_types(game)
        except DataError:
            data = {}
        try:
            self._skin_type_result.emit(game, data)
        except RuntimeError:
            pass  # Window was destroyed while the public request was in flight.

    def _on_skin_types(self, game, data):
        self._skin_type_jobs.discard(game)
        if self._rank_stopped or not data.get("skins"):
            return
        self.dashboard.skin_types[game] = data["skins"]
        if self.dashboard.game == game:
            self.dashboard._render_grid()

    def _refresh_game_data(self, operation):
        account = self.dashboard.current_account
        if not account:
            return
        game = self.dashboard.game
        if operation == "ranks" and game != "lol":
            return
        if operation == "ranks":
            self._queue_rank_refresh([account])
            return
        if operation == "profile" and self.dashboard.mode.currentData() == "catalog":
            operation = "catalog"
        key = account_key(account)
        if (key, game) in self._data_jobs:
            return
        needs_key = operation != "collection" and not (operation == "catalog" and game == "lol") and not self.api_keys[game]
        if needs_key:
            if not self._data_settings():
                return
        region = rank_region(account) if game == "lol" else account.get("api_routes", {}).get(game, "EU")
        self._refresh_skin_types(game)
        self._start_game_data(account, game, operation, region)

    def _start_game_data(self, account, game, operation, region):
        key = account_key(account)
        self._data_jobs.add((key, game))
        self.dashboard.set_busy(key, game, True)
        record = self._profile_sessions.get(key, {})
        token = self.sessions.token(key) if (operation == "ranks" and account.get("puuid")
                    and str(record.get("puuid") or "").casefold() == str(account["puuid"]).casefold()) else None
        identity = {"token": token, "generation": record.get("generation")} if token else {}
        threading.Thread(target=self._game_data_worker,
                         args=(key, copy.deepcopy(account), game, operation, region, self.api_keys[game], self.api_keys["tft"], identity),
                         daemon=True).start()

    def _game_data_worker(self, key, account, game, operation, region, api_key, tft_key="", identity=None):
        result = {"key": key, "game": game, "operation": operation, "lookup_name": account.get("name"),
                  "lookup_region": region}
        try:
            if operation == "ranks":
                identity = identity or {}
                result["lookup_puuid"] = account.get("puuid")
                if identity.get("token"):
                    result["lookup_generation"] = identity.get("generation")
                region = resolve_rank_region(account, identity.get("token"))
                result["resolved_region"] = region
                result["data"] = fetch_auto_rankings(account["name"], region, tft_key)
            elif operation == "collection":
                result["data"] = fetch_collection(account, game)
            elif operation == "catalog":
                result["data"] = league_catalog() if game == "lol" else DeveloperApi(api_key).valorant_catalog(region)
            else:
                result["data"] = DeveloperApi(api_key).profile(account["name"], game, region)
        except DataError as exc:
            result["error"] = str(exc)
        except Exception as exc:
            log.error("Game data refresh failed (%s)", type(exc).__name__)
            result["error"] = "Не удалось обработать игровые данные. Повторите обновление."
        self._game_data_result.emit(result)

    def _on_game_data_result(self, result):
        key, game = result["key"], result["game"]
        self._data_jobs.discard((key, game))
        if result.get("operation") == "ranks":
            self._rank_running.discard(key)
            self._rank_not_before = max(self._rank_not_before, time.monotonic() + 2)
            retry_after = result.get("data", {}).get("rank_retry_after", 0)
            if retry_after:
                self._rank_not_before = time.monotonic() + retry_after
                self._rank_pending.add(key)
        account = next((a for a in self.accounts if account_key(a) == key), None)
        self.dashboard.set_busy(key, game, False)
        # Schedule the next job after this result has been applied on the UI thread.
        QTimer.singleShot(0, self._drain_rank_queue)
        if (self._rank_stopped or account is None or account.get("name") != result["lookup_name"]
                or ("lookup_puuid" in result and account.get("puuid") != result["lookup_puuid"])
                or ("lookup_generation" in result and self._profile_sessions.get(key, {}).get("generation") != result["lookup_generation"])
                or (result.get("operation") == "ranks" and result.get("lookup_region", rank_region(account)) != rank_region(account))):
            if account is not None and not self._rank_stopped and result.get("operation") == "ranks":
                self._queue_rank_refresh([account])
            return  # Account removed/renamed while the worker was in flight.
        if result.get("error"):
            self.dashboard.set_error(key, game, result["error"])
            return
        if result["operation"] == "catalog":
            self.dashboard.catalogs[game] = result["data"]
            save_catalog(game, result["data"])
        else:
            profile = account.setdefault("games", {}).setdefault(game, {})
            if result.get("operation") == "ranks" and result.get("resolved_region"):
                resolved = result["resolved_region"]
                if rank_region(account) != resolved:
                    profile.pop("ranks", None)
                account["league_region"] = resolved
                profile["region"] = resolved
            data = dict(result["data"])
            data.pop("rank_retry_after", None)
            if "ranks" in data:
                data["ranks"] = merge_rankings(profile.get("ranks", {}), data["ranks"])
            profile.update({k: v for k, v in data.items() if v is not None})
            save_accounts(self.accounts)
        self._populate()

    def _check_update(self):
        info = updater.check_for_update()
        if info:
            self._update_found.emit(info)

    def _on_update_found(self, info):
        if UpdateDialog(__version__, info, self).exec() != QDialog.DialogCode.Accepted:
            return
        if updater.is_frozen() and info.get("asset_url"):
            self._begin_update(info["asset_url"])
            return
        webbrowser.open(info["url"])

    def _begin_update(self, asset_url):
        self._update_cancelled = False
        self._progress = QProgressDialog("Downloading update…", "Cancel", 0, 100, self)
        self._progress.setWindowTitle("Updating")
        self._progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._progress.setMinimumDuration(0)
        self._progress.setAutoReset(False)
        self._progress.setAutoClose(False)
        self._progress.canceled.connect(self._cancel_update)
        self._progress.setValue(0)
        threading.Thread(
            target=self._update_worker, args=(asset_url,), daemon=True
        ).start()

    def _update_worker(self, asset_url):
        last = -1

        def cb(done, total):
            nonlocal last
            if self._update_cancelled:
                raise _UpdateCancelled()
            pct = int(done * 100 / total) if total else -1
            if pct != last:
                last = pct
                self._update_progress.emit(done, total)

        try:
            new_path = updater.download_update(asset_url, progress_cb=cb)
        except _UpdateCancelled:
            return
        except Exception as exc:
            self._update_error.emit(describe_exception(exc))
            return
        self._update_ready.emit(new_path)

    def _on_update_progress(self, done, total):
        if self._progress is None:
            return
        if total > 0:
            self._progress.setMaximum(100)
            self._progress.setValue(int(done * 100 / total))
            self._progress.setLabelText(
                f"Downloading update…  {done / 1048576:.1f} / {total / 1048576:.1f} MB"
            )
        else:
            self._progress.setMaximum(0)  # indeterminate

    def _on_update_ready(self, new_path):
        if self._progress is not None:
            self._progress.setMaximum(100)
            self._progress.setValue(100)
            self._progress.setCancelButton(None)
            self._progress.setLabelText("Installing… the app will restart.")
        try:
            updater.launch_swap(new_path)
        except Exception as exc:
            if self._progress is not None:
                self._progress.close()
                self._progress = None
            show_error(self, "Update failed", "Could not start the installer.", exc=exc)
            return
        self.fcm.stop()
        self.tray.hide()
        QApplication.instance().quit()

    def _on_update_error(self, details):
        if self._progress is not None:
            self._progress.close()
            self._progress = None
        show_error(
            self,
            "Update failed",
            "The update could not be downloaded. You can try again, or download "
            "it manually from the releases page.",
            details=details,
        )

    def _cancel_update(self):
        self._update_cancelled = True
        if self._progress is not None:
            self._progress.close()
            self._progress = None

    def _setup_tray(self):
        icon = QIcon(ICON_PATH)
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("Riot 2FA")
        menu = QMenu()
        menu.addAction("Show", self._show_from_tray)
        if debug_log.enabled():
            menu.addAction("Open debug log", self._open_debug_log)
        menu.addSeparator()
        menu.addAction("Quit", self._quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._show_from_tray()

    def _show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _open_debug_log(self):
        try:
            os.startfile(debug_log.LOG_FILE)  # noqa: S606  (Windows-only convenience)
        except Exception:
            show_error(
                self, "Debug log", f"Log file:\n{debug_log.LOG_FILE}"
            )

    def _quit_app(self):
        self._stop_rank_refresh()
        self.sessions.stop()
        self.fcm.stop()
        self.tray.hide()
        QApplication.instance().quit()

    def closeEvent(self, event):

        event.ignore()
        self.hide()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.showMessage(
                "Riot 2FA",
                "Still running in the tray — you'll get login approval prompts here.",
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )

    def _populate(self):
        self.sessions.sync_accounts(self.accounts)
        self._update_session_status()
        self.dashboard.set_accounts(self.accounts)
        self.client_sessions.set_accounts(self.accounts)
        self.account_details.set_accounts(self.accounts)
        self._update_client_status()

    def _update_client_status(self):
        self.dashboard.client_session_info = dict(self.client_sessions.info)
        self.dashboard.client_session_busy = self.client_sessions.busy
        self.dashboard.update_client_status()

    def _update_session_status(self):
        self.dashboard.session_info = self.sessions.public_status()
        self.dashboard.tick()

    def _tick(self):
        self.dashboard.tick()

    def _save_and_refresh(self):
        save_accounts(self.accounts)
        self._populate()

    def _remove_account(self, name, seed, key=None):
        removed_keys = [account_key(a) for a in self.accounts if
                        (account_key(a) == key if key is not None else a["name"] == name and a.get("seed") == seed)]
        self.accounts = [
            a for a in self.accounts if not (account_key(a) == key if key is not None
                                            else a["name"] == name and a.get("seed") == seed)
        ]
        for removed in removed_keys:
            self._pending_qr.pop(removed, None)
            self.sessions.remove(removed)
        self._save_and_refresh()

    def _on_push(self, data):
        """A login attempt arrived via push — show the approve/deny prompt."""
        puuid = data.get("puuid")
        known = [(a.get("name"), a.get("puuid")) for a in self.accounts]
        log.debug("_on_push: incoming puuid=%r; known accounts=%r", puuid, known)
        account = next(
            (a for a in self.accounts if a.get("seed") and a.get("puuid") and a["puuid"] == puuid), None
        )
        if account is None:
            log.warning(
                "_on_push: DROPPED — no stored account matches puuid=%r "
                "(account added manually/without puuid, or push is for another account)",
                puuid,
            )
            return
        log.debug("_on_push: matched account %r", account.get("name"))

        if self._push_is_stale(data):
            return

        suuid = data.get("suuid")
        if suuid and any(
            p.push.get("suuid") == suuid for p in self._active_prompts
        ):
            log.debug("_on_push: DROPPED — a prompt is already open for suuid=%s", suuid)
            return
        log.debug("_on_push: showing approve/deny prompt for suuid=%s", suuid)

        self.tray.showMessage(
            "Riot login attempt",
            f"Approve or deny the login for {account.get('name', 'your account')}.",
            QSystemTrayIcon.MessageIcon.Warning,
            5000,
        )

        prompt = MfaPromptDialog(data, account, self)
        self._active_prompts.append(prompt)
        self.dashboard.update_requests(len(self._active_prompts))

        def _cleanup(_result, p=prompt):
            if p in self._active_prompts:
                self._active_prompts.remove(p)
            self.dashboard.update_requests(len(self._active_prompts))
            verb = p.outcome or "dismissed"
            if self.isVisible():
                self.toast.popup(f"Login {verb}")

        prompt.finished.connect(_cleanup)
        prompt.show()
        prompt.raise_()
        prompt.activateWindow()

    _PUSH_TTL_SECONDS = 180

    def _push_is_stale(self, data):
        attempted_at = data.get("attempted_at")
        try:
            ts = int(attempted_at) / 1000.0
        except (TypeError, ValueError):
            log.debug(
                "_push_is_stale: no usable attempted_at (%r) — treating as fresh",
                attempted_at,
            )
            return False
        age = time.time() - ts
        stale = age > self._PUSH_TTL_SECONDS
        log.debug(
            "_push_is_stale: attempted_at=%s age=%.1fs ttl=%ds -> %s"
            "%s",
            attempted_at, age, self._PUSH_TTL_SECONDS,
            "STALE (dropped)" if stale else "fresh",
            "  [check the device clock if this is a live attempt!]" if stale else "",
        )
        return stale

    def _add_via_login(self, connect_2fa=False, target=None, qr_request=None, refresh_session=False):
        if self._login_busy:
            self.toast.popup("Сначала завершите текущий вход Riot")
            return
        self._login_busy = True
        self.toast.popup("Войдите в Riot в открывшемся браузере")
        threading.Thread(target=self._login_worker, args=(connect_2fa, target, qr_request, refresh_session), daemon=True).start()

    def _login_worker(self, connect_2fa=False, target=None, qr_request=None, refresh_session=False):
        try:
            self._run_login(connect_2fa, target, qr_request, refresh_session)
        except Exception as exc:  # never leave the button stuck busy
            self._login_result.emit(self._error_result("Login Failed", str(exc), exc))

    @staticmethod
    def _error_result(title, text, exc=None):
        result = {"kind": "error", "title": title, "text": text}
        if exc is not None:
            result["details"] = describe_exception(exc)
        return result

    def _run_login(self, connect_2fa=False, target=None, qr_request=None, refresh_session=False):
        """Runs off the GUI thread: drive the stealth browser, then build the account."""
        try:
            data = patchright_login.login()
        except Exception as exc:
            self._login_result.emit(self._error_result("Login Failed", str(exc), exc))
            return
        if not data:
            self._login_result.emit({"kind": "cancelled"})
            return

        cookies = data.get("cookies") or {}
        if not cookies:
            self._login_result.emit(
                {"kind": "error", "title": "Error", "text": "Login OK but the session could not be captured."}
            )
            return

        try:
            csrf = fetch_new_csrf_token(cookies)
        except Exception:
            csrf = data.get("csrf")
        if not csrf:
            self._login_result.emit(
                {"kind": "error", "title": "Error", "text": "Login OK but the CSRF token could not be read."}
            )
            return

        puuid = None
        username = None
        personal_details = {}
        try:
            user = fetch_account_user(cookies, csrf)
            name = riot_id_from_user(user) or "Unknown"
            puuid = puuid_from_user(user)
            username = user.get("username")
            try:
                personal_details = extract_details(puuid, user=user) if puuid else {}
            except AccountDetailsError:
                pass  # Optional metadata must not break otherwise valid enrollment.
        except Exception:
            name = "Unknown"
        if target and not self._login_owner_matches(target, puuid, name):
            self._login_result.emit(self._error_result(
                "Открыт другой аккаунт", "Войдите в выбранный аккаунт Riot."))
            return
        if not connect_2fa:
            if name == "Unknown" or not puuid:
                self._login_result.emit(self._error_result(
                    "Не удалось прочитать профиль", "Вход завершён, но Riot ID или ID аккаунта недоступны. Повторите вход."))
                return
            if qr_request is not None or refresh_session:
                self._login_result.emit({"kind": "success", "mode": "qr" if qr_request else "session", "target_key": account_key(target),
                                         "puuid": puuid, "name": name, "qr_session": data.get("sso") or {},
                                         "cookie_expires_at": data.get("cookie_expires_at"),
                                         "personal_details": personal_details,
                                         "qr_request": qr_request})
                return
            # Profile metadata stays separate from the protected QR session.
            account = {"name": name, "puuid": puuid, "local_id": str(uuid.uuid4())}
            if isinstance(username, str) and username:
                account["login"] = username
            self._login_result.emit({"kind": "success", "mode": "profile", "account": account, "name": name,
                                     "personal_details": personal_details,
                                     "cookie_expires_at": data.get("cookie_expires_at"),
                                     "qr_session": data.get("sso") or {}})
            return
        bearer = None

        try:
            factors = fetch_mfa_factors(cookies, csrf)
        except Exception:
            factors = None
        if factors is not None and not is_email_mfa_enabled(factors):
            self._login_result.emit({
                "kind": "error",
                "title": "Для подключения 2FA нужна защита через почту",
                "text": (
                    "Riot требует включить email MFA перед подключением Riot Mobile 2FA. "
                    "Включите её на account.riotgames.com и повторите.\n\n"
                    "Игровой профиль уже добавлен: просмотр данных и коллекций доступен без подключения 2FA."
                ),
            })
            return

        try:
            seed = enable_mfa(cookies, csrf)
        except Exception as exc:
            self._login_result.emit(self._error_result("Enable MFA Failed", str(exc), exc))
            return

        account = {"name": name, "seed": seed}
        if isinstance(username, str) and username:
            account["login"] = username
        if puuid:
            account["puuid"] = puuid
        sso = data.get("sso") or {}
        access_token = None
        if sso.get("ssid"):
            account["sso"] = sso
            try:
                access_token = mint_access_token(sso)
            except Exception:
                access_token = None
            if access_token:
                account["access_token"] = access_token

        warn = None
        verify_tok = bearer or access_token
        if verify_tok:
            try:
                verify_mfa(verify_tok, seed)
            except Exception as exc:
                warn = f"MFA enabled but verification failed:\n{exc}\n\nSeed saved anyway."

        push_note = self._register_push(access_token, bearer, puuid)

        self._login_result.emit({
            "kind": "success",
            "mode": "2fa",
            "personal_details": personal_details,
            "target_key": account_key(target) if target else None,
            "account": account,
            "name": name,
            "warn": warn,
            "push_note": push_note,
            "cookie_expires_at": data.get("cookie_expires_at"),
        })

    @staticmethod
    def _login_owner_matches(account, puuid, name):
        if account.get("puuid"):
            return bool(puuid) and str(account["puuid"]).casefold() == str(puuid).casefold()
        return bool(name) and name != "Unknown" and str(account.get("name", "")).casefold() == str(name).casefold()

    def _on_login_result(self, result):
        self._login_busy = False
        kind = result.get("kind")
        if kind == "cancelled":
            return
        if kind == "error":
            show_error(
                self,
                result.get("title", "Error"),
                result.get("text", ""),
                details=result.get("details", ""),
            )
            return
        if result.get("mode") in ("qr", "session"):
            account = next((a for a in self.accounts if account_key(a) == result["target_key"]), None)
            if account is None or not self._login_owner_matches(account, result.get("puuid"), result.get("name")):
                self.toast.popup("Выбранный профиль был удалён или изменён")
                return
            if not account.get("puuid"):
                # Legacy imports have only a seed/name. Attach the verified raw
                # owner ID after the normal login, before saving the QR session.
                account["puuid"] = result["puuid"]
                self.dashboard.selected_key = account_key(account)
                self._save_and_refresh()
            self.sessions.put(account, result["qr_session"], result.get("cookie_expires_at"))
            self.account_details.put_login(account, result.get("personal_details", {}))
            if self._rank_auto_enabled:
                self._queue_rank_refresh([account])
            if result.get("qr_request"):
                self._complete_qr_signin(account, *result["qr_request"], allow_reauth=False)
            else:
                self.toast.popup("QR-сессия обновлена")
            return
        if result.get("warn"):
            QMessageBox.warning(self, "Verify Warning", result["warn"])
        incoming = result["account"]
        mode = result.get("mode", "2fa")
        target_key = result.get("target_key")
        existing = next((a for a in self.accounts if account_key(a) == target_key), None) if target_key else next(
            (a for a in self.accounts if incoming.get("puuid") and
             str(a.get("puuid", "")).casefold() == str(incoming["puuid"]).casefold()), None)
        if target_key and (existing is None or
                           str(existing.get("puuid", "")).casefold() != str(incoming.get("puuid", "")).casefold()):
            self.toast.popup("Выбранный профиль был удалён или изменён")
            return
        if existing is not None:
            # Keep game data, legacy credentials and the stable local ID.
            fields = ("name", "login", "puuid") if mode == "profile" else (
                "name", "login", "puuid", "seed", "sso", "access_token")
            existing.update({k: incoming[k] for k in fields if k in incoming})
            account = existing
        else:
            account = incoming
            self.accounts.append(account)
        self.dashboard.selected_key = account_key(account)
        self.account_details.put_login(account, result.get("personal_details", {}))
        if mode == "profile" and result.get("qr_session"):
            self.sessions.put(account, result["qr_session"], result.get("cookie_expires_at"))
        elif mode == "2fa" and account.get("sso"):
            self.sessions.put(account, account["sso"], result.get("cookie_expires_at"))
        self._save_and_refresh()
        if self._rank_auto_enabled:
            self._queue_rank_refresh([account])
        if mode == "profile":
            self.toast.popup("Игровой профиль добавлен" if existing is None else "Игровой профиль обновлён")
        else:
            QMessageBox.information(self, "2FA подключена", f"2FA подключена для {result['name']}{result.get('push_note', '')}")

    def _register_push(self, access_token, id_tok, puuid):
        """Register this account's FCM device so logins push here. Best-effort."""
        log.debug(
            "_register_push: puuid=%r access_token=%s id_tok=%s",
            puuid, mask(access_token), mask(id_tok),
        )
        if not puuid:
            log.warning("_register_push: no puuid — push cannot be enabled for this account")
            return "\n\n(Push approval unavailable: could not read account id.)"

        tokens = [t for t in (access_token, id_tok) if t]
        if not tokens:
            log.warning("_register_push: no access/id token — cannot register with Riot")
            return "\n\n(Push approval unavailable: missing access token.)"
        log.debug("_register_push: waiting up to 30s for FCM token…")
        fcm_token = self.fcm.wait_for_token(30)
        if not fcm_token:
            log.warning("_register_push: FCM token not ready — listener never registered")
            return "\n\n(Push approval unavailable: listener not ready.)"
        last_exc = None
        for token in tokens:
            try:
                register_mfa_push_device(token, fcm_token)
                log.debug("_register_push: SUCCESS — this device is now registered for puuid=%r", puuid)
                return "\n\nPush approval is enabled for this account."
            except Exception as exc:
                last_exc = exc
                log.warning("_register_push: registration attempt failed: %r", exc)
        return f"\n\n(Push approval registration failed: {last_exc})"

    def _valid_access_token(self, account, persist=True):
        """A currently-valid access token for the account.

        Mints a fresh token from the stored SSO cookies (the persistent session —
        no re-login needed), so it always carries the current scopes (including
        session.auth for QR). Falls back to a cached token only if minting fails.
        """
        sso = account.get("sso")
        if sso:
            try:
                token = mint_access_token(sso)
            except Exception:
                token = None
            if token:
                account["access_token"] = token
                if persist:
                    save_accounts(self.accounts)
                return token
        token = account.get("access_token")
        if token and is_valid_jwt(token):
            return token
        return None

    def _pick_account(self):
        """Choose which stored account to sign in with (the QR doesn't say which).

        Always asks, so you explicitly pick the account every time.
        """
        usable = list(self.accounts)
        if not usable:
            QMessageBox.warning(
                self,
                "Нет аккаунтов",
                "Сначала добавьте игровой профиль через вход Riot.",
            )
            return None

        labels = [a.get("name", "Unknown") for a in usable]
        for i, label in enumerate(labels):
            if labels.count(label) > 1:
                tail = (usable[i].get("puuid") or "")[:6] or str(i + 1)
                labels[i] = f"{label}  ·  {tail}"
        label, ok = QInputDialog.getItem(
            self, "Вход по QR", "Выберите аккаунт для входа:", labels, 0, False
        )
        if not ok:
            return None
        return usable[labels.index(label)]

    def _scan_qr(self):
        from app.ui.qr_scanner_dialog import QrScannerDialog

        if self._qr_scanner is not None:
            self._qr_scanner.showNormal()
            self._qr_scanner.raise_()
            self._qr_scanner.activateWindow()
            return
        scanner = QrScannerDialog()
        scanner.setWindowIcon(QIcon(ICON_PATH))
        self._qr_scanner = scanner
        scanner.finished.connect(lambda result: self._on_qr_scan_finished(scanner, result))
        self.destroyed.connect(scanner.close)
        scanner.show()
        scanner.raise_()
        scanner.activateWindow()

    def _close_qr_scanner(self):
        if self._qr_scanner is not None:
            self._qr_scanner.reject()

    def _on_qr_scan_finished(self, scanner, result):
        if self._qr_scanner is not scanner:
            return
        text = scanner.result_text
        self._qr_scanner = None
        scanner.deleteLater()
        if result != QDialog.DialogCode.Accepted or not text:
            return
        suuid, cluster = parse_qr_login(text)
        if not suuid or not cluster:
            QMessageBox.warning(
                self,
                "Not a Riot QR",
                "That QR code isn't a Riot sign-in code.",
            )
            return

        account = self._pick_account()
        if account is None:
            return
        self._complete_qr_signin(account, suuid, cluster)

    def _complete_qr_signin(self, account, suuid, cluster, allow_reauth=True):
        key = account_key(account)
        session = self._profile_sessions.get(key)
        if session and not self._login_owner_matches(account, session.get("puuid"), session.get("name")):
            self.sessions.remove(key)
        token = self.sessions.token(key)
        if not token:
            self._pending_qr[key] = {"puuid": account.get("puuid"), "name": account.get("name"),
                                     "request": (suuid, cluster), "allow_reauth": allow_reauth}
            self.sessions.request(key)
            return
        self._confirm_qr_signin(account, token, suuid, cluster)

    def _on_session_ready(self, key, state):
        if key in self._rank_waiting_sessions:
            if self.sessions.token(key):
                self._rank_waiting_sessions.discard(key)
                QTimer.singleShot(0, self._drain_rank_queue)
            elif state in ("login", "forgotten"):
                self._rank_waiting_sessions.discard(key)
                self._rank_pending.discard(key)
                self.dashboard.set_error(key, "lol", "Выберите сервер League в настройках API или повторите вход в Riot SDA.")
        pending = self._pending_qr.pop(key, None)
        if pending is None:
            return
        account = next((a for a in self.accounts if account_key(a) == key), None)
        if account is None or not self._login_owner_matches(account, pending["puuid"], pending["name"]):
            return
        token = self.sessions.token(key)
        if token:
            self._confirm_qr_signin(account, token, *pending["request"])
        elif state == "login" and pending["allow_reauth"]:
            self._add_via_login(target=copy.deepcopy(account), qr_request=pending["request"])
        else:
            message = self.sessions.public_status().get(key, {}).get("error") or "Riot не предоставил сессию для QR-входа. Повторите вход."
            QMessageBox.warning(self, "Сессия недоступна", message)

    def _confirm_qr_signin(self, account, token, suuid, cluster):

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            try:
                info = qr_session_info(token, suuid, cluster)
            except Exception as exc:
                info = {}
                self._qr_warn("Could not load the sign-in request", exc)
                return
        finally:
            QApplication.restoreOverrideCursor()

        confirm = QrConfirmDialog(account.get("name", "Account"), info, self)
        if confirm.exec() != QDialog.DialogCode.Accepted:
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = qr_approve(token, suuid, cluster, remember=True)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            self._qr_warn("Sign-in failed", exc)
            return
        QApplication.restoreOverrideCursor()

        if result.get("success") is True or result == {}:
            self.toast.popup("Signed in ✓")
            QMessageBox.information(
                self, "Signed in", f"Approved the QR sign-in for {account.get('name')}."
            )
        else:
            show_error(
                self,
                "Sign-in not confirmed",
                "Riot did not confirm the sign-in.",
                details=f"Riot returned:\n{result}",
            )

    def _qr_warn(self, title, exc):
        show_error(self, title, str(exc), exc=exc)

    def _share_account(self, name, seed):
        account = next(
            (a for a in self.accounts if a["name"] == name and a.get("seed") == seed), None
        )
        if account is None:
            return
        try:
            code = export_account(account)
        except Exception as exc:
            show_error(self, "Share failed", "Could not build the share code.", exc=exc)
            return
        ShareCodeDialog(name, code, self).exec()

    def _import_account(self):
        text, ok = QInputDialog.getMultiLineText(
            self, "Import account", "Paste the share code:", ""
        )
        if not ok or not text.strip():
            return
        try:
            account = import_account(text)
        except ShareError as exc:
            show_error(self, "Import failed", str(exc))
            return

        if any(a.get("seed") == account.get("seed") for a in self.accounts):
            QMessageBox.information(
                self, "Already added", f"{account['name']} is already in your list."
            )
            return

        self.accounts.append(account)
        self.dashboard.selected_key = account_key(account)
        self._save_and_refresh()
        self.toast.popup(f"Imported {account['name']}")
        if self._rank_auto_enabled:
            self._queue_rank_refresh([account])
        threading.Thread(
            target=self._import_push_worker, args=(account,), daemon=True
        ).start()

    def _import_push_worker(self, account):
        """Re-register push with this device's FCM token so login prompts arrive
        here, not on the device the account was shared from."""
        token = self._valid_access_token(account)
        note = self._register_push(token, None, account.get("puuid"))
        self._import_result.emit({"name": account["name"], "note": note})

    def _on_import_result(self, result):
        save_accounts(self.accounts)
        QMessageBox.information(
            self, "Imported", f"Added {result['name']}{result['note']}"
        )
