"""Serial background preparation and launch of desktop sessions."""

import copy
import threading
import time

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from app.api.client_auth import create_client_session, ClientAuthError
from app.core.client_switcher import (ClientVault, NativeClient, SwitchError,
                                      generated_snapshot, capture_current, switch_client, recover)
from app.core.search import account_key


class ClientSessionManager(QObject):
    changed = pyqtSignal()
    launched = pyqtSignal(str)
    failed = pyqtSignal(str)
    _finished = pyqtSignal(dict)

    def __init__(self, sessions, parent=None, enabled=True, vault=None, native=None):
        super().__init__(parent)
        self.sessions = sessions
        self.enabled = enabled
        self.vault = vault or ClientVault()
        self.native = native or NativeClient()
        self.accounts = []
        self.info = {}
        self.busy = False
        self.attempted = {}
        self.cancelled = threading.Event()
        self.next_request = 0
        self._finished.connect(self._apply)
        self.timer = QTimer(self)
        self.timer.setInterval(15000)
        self.timer.timeout.connect(self.poll)
        if enabled:
            self.timer.start()
            sessions.changed.connect(self.schedule)

    def set_accounts(self, accounts):
        self.accounts = copy.deepcopy(accounts)
        keys = {account_key(a) for a in accounts}
        self.info = {k: v for k, v in self.info.items() if k in keys}
        if self.enabled:
            self.schedule()

    def _present(self, account):
        return any(account_key(a) == account_key(account) and a.get("puuid") == account.get("puuid")
                   for a in self.accounts)

    def schedule(self):
        QTimer.singleShot(0, self.poll)

    def _set_info(self, account, status, text):
        self.info[account_key(account)] = {"status": status, "text": text}
        self.changed.emit()

    def poll(self):
        if not self.enabled or self.busy or self.cancelled.is_set() or time.monotonic() < self.next_request:
            return
        accounts = copy.deepcopy(self.accounts)
        source = copy.deepcopy(self.sessions.sessions)
        self._run("prepare", accounts, source)

    def launch(self, account):
        if self.busy:
            self.failed.emit("Дождитесь завершения подготовки или переключения.")
            return
        if not account.get("puuid"):
            self.failed.emit("Для этого профиля сначала выполните обычный вход в Riot SDA.")
            return
        self._set_info(account, "switching", "Запускаю выбранный аккаунт…")
        self._run("launch", copy.deepcopy(self.accounts), copy.deepcopy(self.sessions.sessions), copy.deepcopy(account))

    def _run(self, operation, accounts, source, selected=None):
        self.busy = True
        self.changed.emit()
        attempted = dict(self.attempted)
        def worker():
            result = {"operation": operation, "statuses": {}, "attempted": {}, "owners": {},
                      "retry_after": 0, "prepared": False}
            if selected:
                result["selected_key"] = account_key(selected)
            try:
                with self.native.exclusive():
                    if self.cancelled.is_set():
                        return
                    recover(self.native, self.vault)
                    known_owners = {str(a.get("puuid") or "").casefold() for a in self.accounts}
                    for owner in self.vault.records():
                        if owner not in known_owners:
                            self.vault.remove(owner)
                    try:
                        capture_current(self.native, self.vault, accounts)
                    except SwitchError:
                        # Signed-out/unsupported clients do not prevent SSO preparation.
                        pass
                    records = self.vault.records()
                    candidates = [selected] if selected else accounts
                    for account in candidates:
                        if self.cancelled.is_set():
                            break
                        key, owner = account_key(account), str(account.get("puuid") or "")
                        result["owners"][key] = owner
                        session = source.get(key, {})
                        generation = session.get("generation")
                        if owner.casefold() in records:
                            self.vault.files(owner)  # Validate before claiming readiness.
                            result["statuses"][key] = {"status": "ready", "text": "Сессия Riot Client готова"}
                            continue
                        if operation == "prepare" and attempted.get(key) == generation and key in attempted:
                            continue
                        result["attempted"][key] = generation
                        expiry = session.get("cookie_expires_at")
                        if (not owner or session.get("puuid", "").casefold() != owner.casefold()
                                or session.get("status") in ("forgotten", "login")
                                or not session.get("sso", {}).get("ssid")
                                or isinstance(expiry, (float, int)) and expiry <= time.time()):
                            result["statuses"][key] = {"status": "login", "text": "Для Riot Client нужен вход в Riot SDA"}
                            if selected:
                                raise SwitchError("Сохранённый вход недоступен. Войдите заново в Riot SDA; затем сессия клиента создастся автоматически.")
                            continue
                        try:
                            tokens = create_client_session(account, session["sso"])
                            if not self._present(account):
                                continue
                            self.vault.put(owner, generated_snapshot(tokens))
                            result["prepared"] = True
                            result["statuses"][key] = {"status": "ready", "text": "Сессия Riot Client готова"}
                        except ClientAuthError as exc:
                            result["statuses"][key] = {"status": "login", "text": str(exc)}
                            result["retry_after"] = exc.retry_after
                            if selected:
                                raise
                            if exc.retry_after:
                                break
                        # One OAuth exchange per worker keeps cancellation and rate limits responsive.
                        if not selected:
                            break
                    if selected and not self.cancelled.is_set():
                        if not self._present(selected):
                            raise SwitchError("Выбранный аккаунт был удалён или изменён.")
                        switch_client(self.native, self.vault, selected, accounts,
                                      lambda: self.cancelled.is_set() or not self._present(selected))
                        result["launched"] = account_key(selected)
            except (SwitchError, ClientAuthError) as exc:
                result["error"] = str(exc)
            except Exception:
                result["error"] = "Не удалось подготовить или переключить Riot Client. Прежний вход сохранён."
            finally:
                self._finished.emit(result)
        # Switching has a protected recovery journal. Finish its rollback even if Qt quits.
        self.thread = threading.Thread(target=worker, daemon=False, name="riot-client-session")
        self.thread.start()

    def _apply(self, result):
        self.busy = False
        existing = {account_key(a): str(a.get("puuid") or "") for a in self.accounts}
        for key, status in result.get("statuses", {}).items():
            if key in existing and existing[key].casefold() == result["owners"].get(key, "").casefold():
                self.info[key] = status
        self.attempted.update(result.get("attempted", {}))
        self.next_request = time.monotonic() + max(2, result.get("retry_after", 0))
        if result.get("error") and result["operation"] == "launch":
            key = result.get("selected_key")
            if key in existing:
                self.info[key] = {"status": "error", "text": result["error"]}
            self.failed.emit(result["error"])
        elif result.get("launched"):
            self.launched.emit(result["launched"])
        self.changed.emit()
        if self.enabled and not self.cancelled.is_set():
            delay = 2000 if result.get("prepared") else 15000
            QTimer.singleShot(max(delay, result.get("retry_after", 0) * 1000), self.poll)

    def stop(self):
        self.enabled = False
        self.cancelled.set()
        self.timer.stop()
