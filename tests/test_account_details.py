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
                                     creation_date, _portal_get, extract_settings, page_csrf)
from app.core.account_details import (AccountDetailsManager, AccountDetailsStore, MemoryDetailsStore,
                                      clean_manual)
from app.core.search import account_key
from app.core.sessions import SessionManager
from app.core.session_store import MemorySessionStore, VaultError
from app.core.share import export_account, import_account
from app.ui.account_details_dialog import AccountDetailsDialog, country_text

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
    result.text = '<meta content="synthetic-page-token" name="csrf-token">'
    return result


class AccountDetailsApiTests(unittest.TestCase):
    def test_only_confirmed_fields_and_account_creation_timestamp(self):
        details = extract_details(ACCOUNT["puuid"], USER, INFO)
        self.assertEqual(details, {"birthday": "1998-04-12", "registered": "2017-07-14",
                                   "phone_verified": False, "current_country": "RU",
                                   "account_login": "private-login", "email": "private@example.invalid"})
        self.assertNotIn("registration_country", details)
        self.assertNotIn("phone", details)
        self.assertNotIn("email_domain", details)

    def test_no_guess_from_age_country_or_token_issue_date(self):
        info = dict(INFO, acct={}, age=28)
        self.assertEqual(extract_details(ACCOUNT["puuid"], userinfo=info),
                         {"phone_verified": False, "current_country": "US"})

    def test_masked_birthday_is_preserved_without_inventing_day_and_month(self):
        result = extract_details(ACCOUNT["puuid"], dict(USER, birth_date="1998-**-**", country="RUS"))
        self.assertEqual(result["birthday_masked"], "1998-**-**")
        self.assertEqual(result["current_country"], "RUS")
        self.assertNotIn("birthday", result)

    def test_wrong_owner_rejected_in_either_source(self):
        for user, info in ((dict(USER, sub="other"), INFO), (USER, dict(INFO, sub="other"))):
            with self.assertRaises(AccountDetailsError):
                extract_details(ACCOUNT["puuid"], user, info)

    def test_invalid_dates_and_phone_types_are_unknown(self):
        value = extract_details(ACCOUNT["puuid"], dict(USER, birth_date="2020-02-30"),
                                dict(INFO, acct={"created_at": float("nan")}, phone_number_verified="false"))
        self.assertNotIn("birthday", value)
        self.assertNotIn("registered", value)
        self.assertNotIn("phone_verified", value)
        self.assertEqual(value["current_country"], "RU")
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
        session.get.side_effect = [response(INFO), response(), response(USER),
                                  response([]), response({"links": []}), response({}), response({"status": "NONE"})]
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
        self.assertEqual(session.get.call_args_list[-1].kwargs["headers"]["csrf-token"], "synthetic-page-token")

    def test_untrusted_redirect_is_never_requested(self):
        for destination in ("http://account.riotgames.com/", "https://evil.invalid/", "https://auth.riotgames.com@evil.invalid/"):
            session = self.fake_session()
            session.get.return_value = response(code=302, headers={"Location": destination})
            with self.assertRaises(AccountDetailsError):
                _portal_get(session, "https://account.riotgames.com/")
            self.assertEqual(session.get.call_count, 1)

    def test_revoked_access_token_does_not_block_birthday_via_sso(self):
        session = self.fake_session()
        session.get.side_effect = [response(code=401), response(), response(USER),
                                  response([]), response({"links": []}), response({}), response({"status": "NONE"})]
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

    def test_csrf_comes_from_page_meta_not_cookie_and_handles_attribute_order(self):
        self.assertEqual(page_csrf('<meta content="page&amp;token" name="csrf-token"/>'), "page&token")
        self.assertEqual(page_csrf("<meta name='csrf-token' content='another-token'>"), "another-token")
        self.assertIsNone(page_csrf("<html>No token</html>"))

    def test_settings_exclude_mfa_secrets_oauth_ids_and_urls(self):
        result = extract_settings({"mfa": [{"factor": "riotmobile", "status": "enabled", "secret": "discard-secret"},
                                            {"factor": "email", "status": "enabled", "mfaOptIn": "FORCE_ENABLED"}],
                                   "apps": {"links": [{"clientId": "discard-id", "scopes": ["secret"],
                                                        "localizedClientName": {"default": "Example app"},
                                                        "localizedLogoURI": {"default": "https://private.invalid"},
                                                        "connectionTime": 1500000000000}]},
                                   "privacy": {"email_subscribe": True, "third_party_opt_in": False},
                                   "game_pass": {"status": "ACTIVE", "remaining": 15}})
        self.assertEqual(result["authorized_apps"], [{"name": "Example app", "connected_at": "2017-07-14"}])
        self.assertTrue(result["mfa_factors"][1]["required"])
        self.assertTrue(result["riot_news"])
        self.assertFalse(result["partner_offers"])
        self.assertEqual(result["game_pass"], "ACTIVE")
        text = json.dumps(result)
        for secret in ("discard-secret", "discard-id", "private.invalid", "scopes", "remaining"):
            self.assertNotIn(secret, text)

    def test_email_state_and_connected_accounts_match_portal_not_available_methods(self):
        user = dict(USER, email_status="not_validated", federated_identities=["google"],
                    linked_identities=["discord"], alias={"game_name": "Example", "tag_line": "EUW"})
        details = extract_details(ACCOUNT["puuid"], user, dict(INFO, email_verified=True, pw={"cng_at": 1500000000}))
        self.assertFalse(details["email_verified"])
        self.assertEqual(details["connected_accounts"], ["discord", "google"])
        self.assertEqual(details["password_changed"], "2017-07-14")
        self.assertEqual(details["account_riot_id"], "Example#EUW")

    def test_optional_failure_does_not_hide_account_and_unknown_is_not_disabled(self):
        session = self.fake_session()
        session.get.side_effect = [response(INFO), response(), response(USER), response(code=403),
                                  response({"links": []}), response(code=503), response(code=403)]
        with patch("app.api.account_details.requests.Session", return_value=session):
            details = fetch_account_details(ACCOUNT, {"puuid": ACCOUNT["puuid"], "sso": {"ssid": "synthetic"}, "access_token": token()})
        self.assertEqual(details["birthday"], "1998-04-12")
        self.assertEqual(details["unavailable_sections"], ["mfa", "privacy", "game_pass"])
        self.assertNotIn("mfa_factors", details)
        self.assertNotIn("game_pass", details)
        self.assertEqual(details["authorized_apps"], [])

    def test_wrong_portal_owner_stops_before_any_optional_request(self):
        session = self.fake_session()
        session.get.side_effect = [response(), response(dict(USER, sub="other"))]
        with patch("app.api.account_details.requests.Session", return_value=session):
            with self.assertRaises(AccountDetailsError):
                fetch_account_details(ACCOUNT, {"puuid": ACCOUNT["puuid"], "sso": {"ssid": "synthetic"}})
        self.assertEqual(session.get.call_count, 2)


class AccountDetailsStoreTests(unittest.TestCase):
    def test_windows_encryption_and_exclusion_from_account_export(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AccountDetailsStore(Path(directory) / "details.dpapi")
            records = {"synthetic-owner": {"riot": {"birthday": "1998-04-12", "email": "private@example.invalid", "access_token": "discard"},
                                           "manual": {"phone": "+7 999 123 45 67", "registration_country": "Россия"},
                                           "updated_at": 123}}
            store.save(records)
            raw = store.path.read_bytes()
            self.assertNotIn(b"1998-04-12", raw)
            self.assertNotIn(b"999 123", raw)
            loaded = store.load()
            self.assertEqual(loaded["synthetic-owner"]["manual"]["registration_country"], "Россия")
            self.assertNotIn(b"private@example.invalid", raw)
            self.assertEqual(loaded["synthetic-owner"]["riot"]["email"], "private@example.invalid")
            self.assertNotIn("access_token", loaded["synthetic-owner"]["riot"])
            account = dict(ACCOUNT, seed="JBSWY3DPEHPK3PXP", personal_details=loaded)
            exported = import_account(export_account(account))
            self.assertNotIn("personal_details", exported)
            self.assertNotIn("birthday", exported)
            self.assertNotIn("email", exported)
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

    def test_security_and_connections_show_sources_without_plain_email_by_default(self):
        self.manager.put_login(ACCOUNT, {"email": "private@example.invalid", "email_verified": True,
                                        "mfa_factors": [{"factor": "email", "status": "enabled", "required": False}],
                                        "connected_accounts": ["google"], "authorized_apps": [], "game_pass": "NONE",
                                        "riot_news": False, "partner_offers": True})
        dialog = AccountDetailsDialog(ACCOUNT, self.manager)
        self.assertEqual(dialog.tabs.count(), 3)
        self.assertNotIn("private@", dialog.email_value.text())
        dialog.show_email.setChecked(True)
        self.assertEqual(dialog.email_value.text(), "private@example.invalid")
        self.assertIn("подтверждена", dialog.email_note.text())
        self.assertEqual(dialog.providers.text(), "Google")
        self.assertEqual(dialog.apps_status.text(), "Нет подключённых приложений")
        self.assertIn("не активен", dialog.game_pass.text())
        self.assertIn("Новости Riot: Выключены", dialog.subscriptions.text())
        dialog.reject()

    def test_repeated_refresh_reuses_rows_and_removed_cards_are_hidden_immediately(self):
        self.manager.put_login(ACCOUNT, {"authorized_apps": [{"name": "First"}, {"name": "Second"}],
                                        "mfa_factors": [{"factor": "email", "status": "enabled"}]})
        dialog = AccountDetailsDialog(ACCOUNT, self.manager)
        dialog.show()
        dialog.tabs.setCurrentIndex(2)
        self.app.processEvents()
        first = dialog.apps_box.itemAt(0).widget()
        removed = dialog.apps_box.itemAt(1).widget()
        for _ in range(10):
            dialog.render()
            self.assertIs(dialog.apps_box.itemAt(0).widget(), first)
            self.assertEqual(dialog.apps_box.count(), 2)
        self.manager.put_login(ACCOUNT, {"authorized_apps": [{"name": "Updated"}]})
        self.assertFalse(removed.isVisible())
        self.assertIsNone(removed.parent())
        self.assertEqual(first.title_label.text(), "Updated")
        self.assertEqual(dialog.apps_box.count(), 1)
        dialog.reject()

    def test_country_codes_are_shown_verbatim(self):
        for code in ("RUS", "CHN", "ARG", "RU"):
            self.assertEqual(country_text(code), code)
        self.manager.put_login(ACCOUNT, {"current_country": "CHN"})
        dialog = AccountDetailsDialog(ACCOUNT, self.manager)
        self.assertEqual(dialog.country.text(), "Страна аккаунта (код Riot): CHN")
        dialog.reject()

    def test_cached_optional_sections_are_labelled_when_refresh_is_unavailable(self):
        self.manager.put_login(ACCOUNT, {"mfa_factors": [], "authorized_apps": [], "game_pass": "ACTIVE",
                                        "unavailable_sections": ["mfa", "apps", "privacy", "game_pass"]})
        dialog = AccountDetailsDialog(ACCOUNT, self.manager)
        self.assertIn("Сохранённые", dialog.mfa_status.text())
        self.assertIn("Сохранённые", dialog.apps_status.text())
        self.assertIn("сохранено", dialog.game_pass.text())
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
