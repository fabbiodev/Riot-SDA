"""Background renewal, revocation handling and non-secret session status."""

import copy
import math
import threading
import time
import uuid

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from app.api.riot_api import (refresh_sso_session, SessionExpired, SessionRefreshError,
                              token_expires_at, decode_jwt_payload, SSO_COOKIE_NAMES)
from app.core.search import account_key
from app.core.session_store import SessionStore, VaultError


def same_owner(account, record):
    return (bool(account.get("puuid")) and isinstance(record, dict)
            and str(account["puuid"]).casefold() == str(record.get("puuid", "")).casefold())


def timestamp(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value > 0 else None


def remaining_text(seconds):
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days} д {hours:02}:{minutes:02}:{seconds:02}"
    return f"{hours:02}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


class SessionManager(QObject):
    changed = pyqtSignal()
    ready = pyqtSignal(str, str)
    _finished = pyqtSignal(dict)

    def __init__(self, parent=None, store=None, autostart=True, refresher=None, now=None):
        super().__init__(parent)
        self.store = store or SessionStore()
        self.refresher = refresher or refresh_sso_session
        self.now = now or time.time
        self.sessions = {}
        self.jobs = {}
        self.storage_error = ""
        self.active = autostart
        self._finished.connect(self._apply)
        try:
            loaded = self.store.load()
            self.sessions.update({key: record for key, record in loaded.items()
                                  if isinstance(key, str) and isinstance(record, dict)})
        except VaultError as exc:
            self.storage_error = str(exc)
        # A process may have exited while renewing; restart with a fresh check.
        for record in self.sessions.values():
            if record.get("status") == "refreshing":
                record["status"] = "idle"
            record["expires_at"] = token_expires_at(record.get("access_token"))
        self.timer = QTimer(self)
        self.timer.setInterval(30000)
        self.timer.timeout.connect(self.poll)
        if autostart:
            self.timer.start()

    def _save(self):
        try:
            self.store.save(self.sessions)
            self.storage_error = ""
        except VaultError as exc:
            self.storage_error = str(exc)

    def sync_accounts(self, accounts):
        owners = {account_key(account): account for account in accounts}
        changed = False
        for key in list(self.sessions):
            if key not in owners or not same_owner(owners[key], self.sessions[key]):
                self.sessions.pop(key)
                changed = True
        for key, account in owners.items():
            # Existing authenticator accounts can seed the protected QR vault.
            # A forgotten/revoked session is never silently imported again.
            if key not in self.sessions and account.get("puuid") and (account.get("sso") or {}).get("ssid"):
                self.sessions[key] = self._record(account, account["sso"], None, account.get("access_token"))
                changed = True
        if changed:
            self._save()

    def _record(self, account, sso, cookie_expiry, token=None):
        return {"puuid": account["puuid"], "name": account["name"], "generation": str(uuid.uuid4()),
                "sso": {name: value for name, value in sso.items() if name in SSO_COOKIE_NAMES and isinstance(value, str)},
                "cookie_expires_at": timestamp(cookie_expiry), "access_token": token,
                "expires_at": token_expires_at(token), "status": "idle", "next_attempt": 0, "failures": 0}

    def put(self, account, sso, cookie_expiry=None):
        key = account_key(account)
        if not account.get("puuid") or not isinstance(sso, dict) or not sso.get("ssid"):
            return
        self.sessions[key] = self._record(account, sso, cookie_expiry)
        self._save()
        self.changed.emit()
        if self.active:
            QTimer.singleShot(0, self.poll)

    def remove(self, key):
        self.sessions.pop(key, None)
        self._save()
        self.changed.emit()

    def forget(self, account):
        key = account_key(account)
        self.sessions[key] = {"puuid": account.get("puuid"), "name": account.get("name"),
                              "generation": str(uuid.uuid4()), "status": "forgotten"}
        self._save()
        self.changed.emit()

    def token(self, key):
        record = self.sessions.get(key, {})
        expiry = timestamp(record.get("expires_at"))
        if record.get("status") not in ("login", "forgotten") and expiry and expiry > self.now() + 30:
            return record.get("access_token")
        return None

    def poll(self):
        if not self.active:
            return
        now = self.now()
        for key, record in list(self.sessions.items()):
            if self._expire_cookie(record):
                self._save()
                self.changed.emit()
            if len(self.jobs) >= 2:
                break
            if record.get("sso", {}).get("ssid") and record.get("status") not in ("login", "forgotten"):
                if now >= (timestamp(record.get("next_attempt")) or 0):
                    self.request(key, force=True)

    def _expire_cookie(self, record):
        expiry = timestamp(record.get("cookie_expires_at"))
        if expiry and expiry <= self.now() and record.get("sso"):
            record.pop("sso", None)
            record["status"] = "token_only" if self.token_for_record(record) else "login"
            record["error"] = "Срок SSO-cookie истёк. Для дальнейшего продления нужен вход Riot."
            record["generation"] = str(uuid.uuid4())
            return True
        return False

    def token_for_record(self, record):
        expiry = timestamp(record.get("expires_at"))
        return record.get("access_token") if expiry and expiry > self.now() + 30 else None

    def request(self, key, force=False):
        record = self.sessions.get(key)
        if record and self._expire_cookie(record):
            self._save()
            self.changed.emit()
        if not record or record.get("status") in ("login", "forgotten") or not record.get("sso", {}).get("ssid"):
            self.ready.emit(key, "login")
            return
        if not force and self.token(key):
            self.ready.emit(key, "ready")
            return
        if key in self.jobs:
            return
        if self.now() < (timestamp(record.get("next_attempt")) or 0) and record.get("status") == "retry":
            self.ready.emit(key, "retry")
            return
        self.jobs[key] = record["generation"]
        record["status"] = "refreshing"
        self.changed.emit()
        threading.Thread(target=self._run, args=(key, copy.deepcopy(record)), daemon=True).start()

    def _run(self, key, record):
        result = {"key": key, "generation": record["generation"]}
        try:
            data = self.refresher(record["sso"])
            subject = (decode_jwt_payload(data.get("access_token")) or {}).get("sub")
            if subject and str(subject).casefold() != str(record["puuid"]).casefold():
                raise SessionExpired("Сессия принадлежит другому аккаунту. Войдите заново.")
            result["data"] = data
        except SessionExpired as exc:
            result.update(state="login", error=str(exc))
        except SessionRefreshError as exc:
            result.update(state="retry", error=str(exc), retry_after=exc.retry_after)
        except Exception:
            result.update(state="retry", error="Не удалось обновить сессию. Попытка будет повторена.")
        self._finished.emit(result)

    def _apply(self, result):
        key = result["key"]
        if self.jobs.get(key) == result["generation"]:
            self.jobs.pop(key, None)
        record = self.sessions.get(key)
        if not record or record.get("generation") != result["generation"]:
            if self.active:
                QTimer.singleShot(0, self.poll)
            return
        now = self.now()
        if "data" in result:
            data = result["data"]
            expiry = token_expires_at(data.get("access_token"))
            if not expiry or expiry <= now + 10:
                result = {**result, "state": "retry", "error": "Riot не предоставил действующий токен."}
            else:
                margin = min(300, max(30, (expiry - now) * .2))
                record.update(sso=data["sso"], access_token=data["access_token"], expires_at=expiry,
                              status="ready", last_refresh=now, failures=0,
                              next_attempt=max(now + 30, expiry - margin))
                # Cookie expiry is an upper bound, not the session's promise.
                if timestamp(data.get("cookie_expires_at")) or data.get("cookie_rotated"):
                    record["cookie_expires_at"] = timestamp(data.get("cookie_expires_at"))
                cookie_expiry = timestamp(record.get("cookie_expires_at"))
                if cookie_expiry:
                    cookie_margin = min(300, max(30, (cookie_expiry - now) * .2))
                    record["next_attempt"] = max(now + 30, min(record["next_attempt"], cookie_expiry - cookie_margin))
                record.pop("error", None)
                if not record.get("sso", {}).get("ssid"):
                    record["status"] = "token_only"
                    record["error"] = "Riot не предоставил cookie для дальнейшего продления. Войдите заново."
        if result.get("state") == "login":
            record.update(status="login", error=result["error"])
            for field in ("sso", "access_token", "expires_at", "next_attempt"):
                record.pop(field, None)
        elif result.get("state") == "retry":
            failures = min(10, record.get("failures", 0) + 1)
            delay = max(min(900, 30 * 2 ** (failures - 1)), result.get("retry_after", 0))
            record.update(status="retry", error=result["error"], failures=failures, next_attempt=now + delay)
        self._save()
        self.changed.emit()
        self.ready.emit(key, record["status"])
        if self.active:
            QTimer.singleShot(0, self.poll)

    def public_status(self):
        fields = ("status", "expires_at", "cookie_expires_at", "last_refresh", "next_attempt", "error")
        return {key: {**{field: record[field] for field in fields if field in record},
                      "saved": not bool(self.storage_error), "storage_error": self.storage_error}
                for key, record in self.sessions.items()}

    def stop(self):
        self.active = False
        self.timer.stop()
