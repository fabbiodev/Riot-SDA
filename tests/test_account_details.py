import base64
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest
import requests

from app.api.account_details import (extract_details, fetch_account_details, AccountDetailsError,
                                     creation_date, _portal_get)
from app.core.account_details import (AccountDetailsManager, AccountDetailsStore, MemoryDetailsStore,
                                      clean_manual)
from app.core.search import account_key
from app.core.sessions import SessionManager
from app.core.session_store import MemorySessionStore, VaultError
from app.core.share import export_account, import_account
from app.ui.account_details_dialog import AccountDetailsDialog

ACCOUNT = {"name": "Example#EUW", "puuid": "synthetic-owner", "local_id": "details-test"}
USER = {"sub": "synthetic-owner", "birth_date": "1998-04-12", "country": "RU",
        "email": "private@example.invalid", "username": "private-login"}
INFO = {"sub": "synthetic-owner", "acct": {"created_at": 1500000000}, "phone_number_verified": False,
        "country": "US", "country_at": 1700000000, "iat": 1800000000}


def token():
    data = {"sub": ACCOUNT["puuid"], "exp": time.time() + 3600}
    payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return "synthetic." + payload + ".signature"


def response(data=None, code=200, headers=None):
    result = Mock(status_code=code, headers=headers or {})
    result.json.return_value = data
    return result


class AccountDetailsApiTests(unittest.TestCase):
    def test_only_confirmed_fields_and_account_creation_timestamp(self):
        details = extract_details(ACCOUNT["puuid"], USER, INFO)
        self.assertEqual(details, {"birthday": "1998-04-12", "registered": "2017-07-14",
                                   "phone_verified": False, "current_country": "RU"})
        self.assertNotIn("registration_country", details)
        self.assertNotIn("phone", details)
        self.assertNotIn("email", details)

    def test_no_guess_from_age_country_or_token_issue_date(self):
        info = dict(INFO, acct={}, age=28)
        self.assertEqual(extract_details(ACCOUNT["puuid"], userinfo=info),
                         {"phone_verified": False, "current_country": "US"})

    def test_masked_birthday_is_preserved_without_inventing_day_and_month(self):
        result = extract_details(ACCOUNT["puuid"], dict(USER, birth_date="1998-**-**", country="RUS"))
        self.assertEqual(result, {"birthday_masked": "1998-**-**", "current_country": "RUS"})

    def test_wrong_owner_rejected_in_either_source(self):
        for user, info in ((dict(USER, sub="other"), INFO), (USER, dict(INFO, sub="other"))):
            with self.assertRaises(AccountDetailsError):
                extract_details(ACCOUNT["puuid"], user, info)

    def test_invalid_dates_and_phone_types_are_unknown(self):
        value = extract_details(ACCOUNT["puuid"], dict(USER, birth_date="2020-02-30"),
                                dict(INFO, acct={"created_at": float("nan")}, phone_number_verified="false"))
        self.assertEqual(value, {"current_country": "RU"})
        for invalid in (None, True, float("inf"), -1, time.time() + 500, "1500000000"):
            self.assertIsNone(creation_date(invalid))
        self.assertEqual(creation_date(1500000000000), "2017-07-14")

    def fake_session(self):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.cookies = requests.cookies.RequestsCookieJar()
        session.cookies.set("a12l-csrf-prod", "synthetic-csrf", domain="account.riotgames.com")
        return session

    def test_fetch_checks_owners_cookie_domains_and_tls(self):
        session = self.fake_session()
        session.get.side_effect = [response(INFO), response(), response(USER)]
        record = {"puuid": ACCOUNT["puuid"], "sso": {"ssid": "synthetic-sso", "unknown": "discard"},
                  "access_token": token()}
        with patch("app.api.account_details.requests.Session", return_value=session):
            details = fetch_account_details(ACCOUNT, record)
        self.assertEqual(details["birthday"], "1998-04-12")
        self.assertFalse(session.trust_env)
        cookies = list(session.cookies)
        self.assertTrue(any(c.name == "ssid" and c.domain == "auth.riotgames.com" for c in cookies))
        self.assertFalse(any(c.name == "unknown" for c in cookies))
        for call in session.get.call_args_list:
            self.assertFalse(call.kwargs.get("allow_redirects", True))
            self.assertNotEqual(call.kwargs.get("verify"), False)
        self.assertNotIn("Authorization", session.get.call_args_list[-1].kwargs["headers"])

    def test_untrusted_redirect_is_never_requested(self):
        for destination in ("http://account.riotgames.com/", "https://evil.invalid/", "https://auth.riotgames.com@evil.invalid/"):
            session = self.fake_session()
            session.get.return_value = response(code=302, headers={"Location": destination})
            with self.assertRaises(AccountDetailsError):
                _portal_get(session, "https://account.riotgames.com/")
            self.assertEqual(session.get.call_count, 1)

    def test_revoked_access_token_does_not_block_birthday_via_sso(self):
        session = self.fake_session()
        session.get.side_effect = [response(code=401), response(), response(USER)]
        with patch("app.api.account_details.requests.Session", return_value=session):
            result = fetch_account_details(ACCOUNT, {"puuid": ACCOUNT["puuid"], "access_token": token(),
                                                      "sso": {"ssid": "synthetic"}})
        self.assertEqual(result["birthday"], "1998-04-12")
        self.assertNotIn("phone_verified", result)

    def test_rate_limit_and_missing_session_do_not_expose_response(self):
        session = self.fake_session()
        session.get.return_value = response({"secret": "do-not-display"}, code=429, headers={"Retry-After": "900"})
        with patch("app.api.account_details.requests.Session", return_value=session):
            with self.assertRaises(AccountDetailsError) as raised:
                fetch_account_details(ACCOUNT, {"puuid": ACCOUNT["puuid"], "access_token": token()})
        self.assertEqual(raised.exception.retry_after, 900)
        self.assertNotIn("do-not-display", str(raised.exception))
        with self.assertRaises(AccountDetailsError):
            fetch_account_details(ACCOUNT, {"puuid": "other"})


class AccountDetailsStoreTests(unittest.TestCase):
    def test_windows_encryption_and_exclusion_from_account_export(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AccountDetailsStore(Path(directory) / "details.dpapi")
            records = {"synthetic-owner": {"riot": {"birthday": "1998-04-12", "email": "discard"},
                                           "manual": {"phone": "+7 999 123 45 67", "registration_country": "Россия"},
                                           "updated_at": 123}}
            store.save(records)
            raw = store.path.read_bytes()
            self.assertNotIn(b"1998-04-12", raw)
            self.assertNotIn(b"999 123", raw)
            loaded = store.load()
            self.assertEqual(loaded["synthetic-owner"]["manual"]["registration_country"], "Россия")
            self.assertNotIn("email", loaded["synthetic-owner"]["riot"])
            account = dict(ACCOUNT, seed="JBSWY3DPEHPK3PXP", personal_details=loaded)
            exported = import_account(export_account(account))
            self.assertNotIn("personal_details", exported)
            self.assertNotIn("birthday", exported)
            self.assertEqual(exported["puuid"], ACCOUNT["puuid"])

    def test_corrupt_vault_is_not_silently_overwritten_on_load(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AccountDetailsStore(Path(directory) / "details.dpapi")
            store.path.write_bytes(b"corrupt")
            with self.assertRaises(VaultError):
                store.load()
            self.assertEqual(store.path.read_bytes(), b"corrupt")

    def test_manual_unknown_dates_and_phone_are_validated(self):
        for value in ("2024-02-30", "2017-07-14garbage", "9999-01-01"):
            with self.assertRaises(ValueError):
                clean_manual({"registered": value})
        with self.assertRaises(ValueError):
            clean_manual({"phone": "<script>123456789</script>"})
        self.assertEqual(clean_manual({"registered": "2017-07-14", "registered_approximate": True})["registered_approximate"], True)


class AccountDetailsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.sessions = SessionManager(store=MemorySessionStore(), autostart=False)
        self.manager = AccountDetailsManager(self.sessions, enabled=False, store=MemoryDetailsStore())
        self.manager.set_accounts([ACCOUNT])

    def tearDown(self):
        self.manager.stop()
        self.sessions.stop()
        self.manager.deleteLater()
        self.sessions.deleteLater()
        self.app.processEvents()

    def test_manual_and_riot_sources_and_masked_phone(self):
        self.manager.put_login(ACCOUNT, extract_details(ACCOUNT["puuid"], USER, INFO))
        self.manager.put_manual(ACCOUNT, {"phone": "+7 999 123 45 67", "registered": "2016-01-01",
                                           "registered_approximate": True, "registration_country": "Россия"})
        dialog = AccountDetailsDialog(ACCOUNT, self.manager)
        self.assertEqual(dialog.values["registered"].text(), "14.07.2017")
        self.assertEqual(dialog.sources["registered"].text(), "Riot · UTC")
        self.assertNotIn("999", dialog.values["phone"].text())
        self.assertIn("Номер указан вручную", dialog.sources["phone"].text())
        dialog.show_phone.setChecked(True)
        self.assertEqual(dialog.values["phone"].text(), "+7 999 123 45 67")
        self.assertEqual(dialog.values["registration_country"].text(), "Россия")
        dialog.reject()

    def test_unverified_does_not_claim_phone_is_unlinked(self):
        self.manager.put_login(ACCOUNT, {"phone_verified": False, "current_country": "RU"})
        dialog = AccountDetailsDialog(ACCOUNT, self.manager)
        self.assertEqual(dialog.values["phone"].text(), "Не подтверждён Riot")
        self.assertEqual(dialog.values["registration_country"].text(), "Riot не предоставил")
        dialog.reject()

    def test_masked_birthday_and_current_country_are_labelled(self):
        self.manager.put_login(ACCOUNT, {"birthday_masked": "1998-**-**", "current_country": "RUS"})
        dialog = AccountDetailsDialog(ACCOUNT, self.manager)
        self.assertEqual(dialog.values["birthday"].text(), "••.••.1998")
        self.assertIn("день и месяц скрыты", dialog.sources["birthday"].text())
        self.assertEqual(dialog.values["registration_country"].text(), "Riot не предоставил")
        self.assertIn("RUS", dialog.country.text())
        dialog.reject()

    def test_late_result_after_removal_does_not_restore_private_data(self):
        self.manager.jobs.add(account_key(ACCOUNT))
        self.manager.set_accounts([])
        self.manager._apply({"key": account_key(ACCOUNT), "account": ACCOUNT,
                             "retry_after": 2, "details": {"birthday": "1998-04-12"}})
        self.assertEqual(self.manager.records, {})
        self.assertEqual(self.manager.jobs, set())

    def test_background_refresh_is_once_per_session_and_cache_survives_errors(self):
        self.sessions.put(ACCOUNT, {"ssid": "synthetic"})
        self.manager.enabled = True
        self.manager.fetcher = Mock(return_value=extract_details(ACCOUNT["puuid"], USER, INFO))
        self.manager.poll()
        for _ in range(100):
            self.app.processEvents()
            if not self.manager.jobs:
                break
            QTest.qWait(5)
        self.manager.next_request = 0
        self.manager.poll()
        self.assertEqual(self.manager.fetcher.call_count, 1)
        self.manager.fetcher.side_effect = AccountDetailsError("Сеть недоступна")
        self.manager.refresh(ACCOUNT)
        for _ in range(100):
            self.app.processEvents()
            if not self.manager.jobs:
                break
            QTest.qWait(5)
        self.assertEqual(self.manager.get(ACCOUNT)["riot"]["birthday"], "1998-04-12")
        self.assertEqual(self.manager.errors[account_key(ACCOUNT)], "Сеть недоступна")
