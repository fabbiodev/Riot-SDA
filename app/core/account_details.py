"""Encrypted personal metadata; intentionally separate from public profiles/exports."""

import copy
import json
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from app.api.account_details import fetch_account_details, AccountDetailsError, iso_date
from app.core.client_switcher import atomic_write
from app.core.search import account_key
from app.core.session_store import _dpapi, VaultError
from app.core.storage import APPDATA_DIR

AUTO_FIELDS = {"birthday", "birthday_masked", "registered", "phone_verified", "current_country", "email",
               "email_verified", "account_login", "account_riot_id", "account_region", "locale",
               "password_changed", "connected_accounts", "authorized_apps", "mfa_factors", "riot_news",
               "partner_offers", "game_pass", "unavailable_sections", "settings_version"}
MANUAL_FIELDS = {"phone", "birthday", "registered", "registration_country", "registered_approximate"}


def clean_manual(values):
    result = {}
    for field in ("birthday", "registered"):
        value = iso_date(values.get(field))
        if values.get(field) and (not value or values[field] != value):
            raise ValueError("Введите дату в формате ГГГГ-ММ-ДД, не позднее сегодняшнего дня.")
        if value:
            result[field] = value
    phone = values.get("phone")
    if isinstance(phone, str) and phone.strip():
        phone = phone.strip()
        digits = "".join(c for c in phone if c in "0123456789")
        if not 7 <= len(digits) <= 15 or any(c not in "+0123456789 ()-." for c in phone):
            raise ValueError("Введите номер телефона с кодом страны.")
        result["phone"] = phone
    country = values.get("registration_country")
    if isinstance(country, str) and country.strip():
        result["registration_country"] = country.strip()[:80]
    if "registered" in result:
        result["registered_approximate"] = bool(values.get("registered_approximate"))
    return result


class AccountDetailsStore:
    def __init__(self, path=None):
        self.path = Path(path or Path(APPDATA_DIR) / "account-details.dpapi")

    def load(self):
        if not self.path.exists():
            return {}
        try:
            if self.path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError()
            data = json.loads(_dpapi(self.path.read_bytes(), decrypt=True))
            if data.get("version") != 1 or not isinstance(data.get("accounts"), dict):
                raise ValueError()
            return {owner: record for owner, record in data["accounts"].items()
                    if isinstance(owner, str) and isinstance(record, dict)}
        except (ValueError, OSError, AttributeError):
            raise VaultError("Не удалось открыть личные сведения. Обновите их после входа.") from None

    def save(self, records):
        payload = {owner: {"riot": {k: v for k, v in record.get("riot", {}).items() if k in AUTO_FIELDS},
                           "manual": clean_manual(record.get("manual", {})),
                           "updated_at": record.get("updated_at")}
                   for owner, record in records.items()}
        try:
            encrypted = _dpapi(json.dumps({"version": 1, "accounts": payload}, ensure_ascii=False).encode("utf-8"))
            atomic_write(self.path, encrypted)
        except OSError:
            raise VaultError("Не удалось сохранить личные сведения на этом компьютере.") from None


class MemoryDetailsStore:
    def load(self): return {}
    def save(self, records): pass


class AccountDetailsManager(QObject):
    changed = pyqtSignal()
    _finished = pyqtSignal(dict)

    def __init__(self, sessions, parent=None, enabled=True, store=None, fetcher=None):
        super().__init__(parent)
        self.sessions, self.enabled = sessions, enabled
        self.store = store or AccountDetailsStore()
        self.fetcher = fetcher or fetch_account_details
        self.accounts, self.records, self.errors, self.jobs = [], {}, {}, set()
        self.attempted, self.next_request = {}, 0
        self.stopped = False
        self.storage_error = ""
        self._finished.connect(self._apply)
        try:
            self.records = self.store.load()
        except VaultError as exc:
            self.storage_error = str(exc)
        self.timer = QTimer(self)
        self.timer.setInterval(15000)
        self.timer.timeout.connect(self.poll)
        if enabled:
            self.timer.start()
            sessions.ready.connect(lambda *_: QTimer.singleShot(0, self.poll))

    def _save(self):
        try:
            self.store.save(self.records)
            self.storage_error = ""
        except VaultError as exc:
            self.storage_error = str(exc)

    def set_accounts(self, accounts):
        self.accounts = copy.deepcopy(accounts)
        owners = {str(a.get("puuid") or "").casefold() for a in accounts}
        retained = {k: v for k, v in self.records.items() if k in owners}
        if retained != self.records:
            self.records = retained
            self._save()
        if self.enabled:
            QTimer.singleShot(0, self.poll)

    def get(self, account):
        return copy.deepcopy(self.records.get(str(account.get("puuid") or "").casefold(), {}))

    def put_login(self, account, details):
        if not account.get("puuid"):
            return
        owner = str(account["puuid"]).casefold()
        record = self.records.setdefault(owner, {})
        record.setdefault("riot", {}).update({k: v for k, v in details.items() if k in AUTO_FIELDS})
        record["updated_at"] = time.time()
        self._save()
        self.changed.emit()

    def put_manual(self, account, values):
        owner = str(account.get("puuid") or "").casefold()
        if not owner or not any(str(a.get("puuid") or "").casefold() == owner for a in self.accounts):
            raise ValueError("Этот аккаунт удалён или ещё не подтверждён входом.")
        self.records.setdefault(owner, {})["manual"] = clean_manual(values)
        self._save()
        self.changed.emit()

    def poll(self):
        if not self.enabled or self.stopped or self.jobs or time.monotonic() < self.next_request:
            return
        for account in self.accounts:
            key = account_key(account)
            session = self.sessions.sessions.get(key, {})
            if (not account.get("puuid") or session.get("status") in ("forgotten", "login")
                    or not session.get("sso", {}).get("ssid")):
                continue
            info = self.get(account)
            fields = info.get("riot", {})
            complete = (("birthday" in fields or "birthday_masked" in fields)
                        and all(k in fields for k in ("registered", "phone_verified"))
                        and fields.get("settings_version") == 2)
            if complete and time.time() - (info.get("updated_at") or 0) < 86400:
                continue
            generation = session.get("generation")
            last = self.attempted.get(key)
            if last and last[0] == generation and time.time() - last[1] < 86400:
                continue
            self.refresh(account)
            break

    def refresh(self, account):
        key = account_key(account)
        if self.stopped or self.jobs:
            return False
        if time.monotonic() < self.next_request:
            self.errors[key] = "Следующий запрос доступен через " + str(max(1, int(self.next_request - time.monotonic()))) + " с."
            self.changed.emit()
            return False
        record = copy.deepcopy(self.sessions.sessions.get(key, {}))
        self.attempted[key] = (record.get("generation"), time.time())
        self.jobs.add(key)
        self.errors.pop(key, None)
        self.changed.emit()
        account = copy.deepcopy(account)
        def worker():
            result = {"key": key, "account": account, "generation": record.get("generation"), "retry_after": 2}
            try:
                result["details"] = self.fetcher(account, record)
            except AccountDetailsError as exc:
                result.update(error=str(exc), retry_after=exc.retry_after)
            except Exception:
                result.update(error="Не удалось обновить личные сведения.", retry_after=60)
            if not self.stopped:
                self._finished.emit(result)
        threading.Thread(target=worker, daemon=True).start()
        return True

    def _apply(self, result):
        key = result["key"]
        self.jobs.discard(key)
        self.next_request = time.monotonic() + result["retry_after"]
        if not any(account_key(a) == key and a.get("puuid") == result["account"].get("puuid") for a in self.accounts):
            self.changed.emit()
            return
        if "generation" in result and self.sessions.sessions.get(key, {}).get("generation") != result["generation"]:
            self.changed.emit()
            return
        if "details" in result:
            self.put_login(result["account"], result["details"])
        else:
            self.errors[key] = result["error"]
            self.changed.emit()

    def stop(self):
        self.stopped = True
        self.timer.stop()
