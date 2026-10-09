"""Profile-only enrollment must not change an account's authentication setup."""

import copy
import os
import tempfile
import unittest
import time
import json
import base64
from contextlib import ExitStack
from unittest.mock import patch, Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QDialog
from PyQt6.QtTest import QTest
from app.api.riot_api import SessionExpired
from app.core.search import account_key
from app.ui.main_window import MainWindow


class ProfileModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.directory = tempfile.TemporaryDirectory()
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, cls.directory.name)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.stack = ExitStack()
        self.calls = {}
        values = {
            "FcmService": {}, "save_accounts": {}, "show_error": {},
            "patchright_login.login": {"return_value": {"cookies": {"session": "fake"},
                "sso": {"ssid": "fake-session"}}},
            "fetch_new_csrf_token": {"return_value": "fake-csrf"},
            "fetch_account_user": {"return_value": {}},
            "riot_id_from_user": {"return_value": "Owner#EUW"},
            "puuid_from_user": {"return_value": "owner-one"},
            "fetch_mfa_factors": {"return_value": [{"factor": "email", "status": "disabled"}]},
            "enable_mfa": {"return_value": "JBSWY3DPEHPK3PXP"},
            "verify_mfa": {}, "mint_access_token": {"return_value": "fake-access"},
            "QMessageBox.information": {}, "QMessageBox.warning": {},
        }
        for name, kwargs in values.items():
            self.calls[name] = self.stack.enter_context(patch("app.ui.main_window." + name, **kwargs))
        self.win = MainWindow(accounts=[], start_services=False)
        payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 3600, "sub": "owner-one"}).encode()).decode().rstrip("=")
        self.qr_token = f"test.{payload}.signature"
        self.win.sessions.refresher = Mock(return_value={"access_token": self.qr_token, "sso": {"ssid": "rotated-fake-session"}})
        self.push = self.stack.enter_context(patch.object(self.win, "_register_push", return_value=""))

    def tearDown(self):
        self.win.timer.stop()
        self.win.deleteLater()
        self.app.processEvents()
        self.stack.close()

    def wait_for_session(self):
        for _ in range(200):
            self.app.processEvents()
            if not self.win.sessions.jobs:
                return
            QTest.qWait(5)
        self.fail("Session worker did not finish")

    def test_add_profile_does_not_require_or_enroll_mfa_or_save_sessions(self):
        self.win._run_login()
        self.assertEqual(len(self.win.accounts), 1)
        account = self.win.accounts[0]
        self.assertEqual(account["puuid"], "owner-one")
        self.assertTrue(account["local_id"])
        for field in ("seed", "sso", "access_token"):
            self.assertNotIn(field, account)
        for operation in ("fetch_mfa_factors", "enable_mfa", "verify_mfa", "mint_access_token", "show_error"):
            self.calls[operation].assert_not_called()
        self.push.assert_not_called()
        self.assertFalse(self.win.dashboard.code_button.isEnabled())
        self.assertFalse(self.win.dashboard.share_action.isEnabled())
        self.assertTrue(self.win.dashboard.connect_action.isEnabled())
        self.assertTrue(self.win.dashboard.api_button.isEnabled())
        self.assertTrue(self.win.dashboard.client_button.isEnabled())
        self.assertFalse(self.win.dashboard.qr_button.isHidden())
        self.assertTrue(self.win.dashboard.qr_button.isEnabled())
        self.win.dashboard.hidden_codes = False
        self.win.dashboard.tick()
        self.assertNotEqual(self.win.dashboard.code_button.text(), "Ошибка")

    def test_multiple_profiles_have_distinct_stable_selection_keys(self):
        self.win._run_login()
        first = self.win.accounts[0]
        self.calls["puuid_from_user"].return_value = "owner-two"
        self.calls["riot_id_from_user"].return_value = "Other#RU"
        self.win._run_login()
        second = self.win.accounts[1]
        self.assertNotEqual(account_key(first), account_key(second))
        original = account_key(first)
        first["name"] = "Renamed#RU"
        first["seed"] = "added-later"
        self.assertEqual(account_key(first), original)
        self.assertEqual(self.win.dashboard.selected_key, account_key(second))

    def test_readding_existing_auth_account_keeps_codes_and_inventory(self):
        account = {"name": "Owner#EUW", "puuid": "owner-one", "seed": "existing-seed",
                   "sso": {"ssid": "existing-session"}, "games": {"lol": {"level": 123}}}
        self.win.accounts.append(account)
        key = account_key(account)
        self.win._run_login()
        self.assertEqual(len(self.win.accounts), 1)
        self.assertEqual(account["seed"], "existing-seed")
        self.assertEqual(account["sso"], {"ssid": "existing-session"})
        self.assertEqual(account["games"]["lol"]["level"], 123)
        self.assertEqual(account_key(account), key)

    def test_explicit_authenticator_setup_keeps_riots_email_requirement(self):
        self.win._run_login()
        original = copy.deepcopy(self.win.accounts[0])
        self.win._run_login(connect_2fa=True, target=original)
        self.calls["show_error"].assert_called_once()
        self.assertIn("Для подключения 2FA", self.calls["show_error"].call_args.args[1])
        self.calls["enable_mfa"].assert_not_called()
        self.assertEqual(self.win.accounts[0], original)

    def test_different_owner_cannot_attach_authenticator_to_selected_profile(self):
        self.win._run_login()
        original = copy.deepcopy(self.win.accounts[0])
        self.calls["puuid_from_user"].return_value = "another-owner"
        self.win._run_login(connect_2fa=True, target=original)
        self.calls["show_error"].assert_called_once()
        self.assertEqual(self.calls["show_error"].call_args.args[1], "Открыт другой аккаунт")
        self.calls["fetch_mfa_factors"].assert_not_called()
        self.calls["enable_mfa"].assert_not_called()
        self.assertEqual(self.win.accounts[0], original)

    def test_explicit_setup_merges_into_profile_without_losing_data_or_identity(self):
        self.win._run_login()
        account = self.win.accounts[0]
        account["games"] = {"lol": {"level": 123}}
        key = account_key(account)
        self.calls["fetch_mfa_factors"].return_value = [{"factor": "email", "status": "enabled"}]
        self.win._run_login(connect_2fa=True, target=copy.deepcopy(account))
        self.assertEqual(len(self.win.accounts), 1)
        self.assertEqual(account_key(account), key)
        self.assertEqual(account["games"]["lol"]["level"], 123)
        self.assertEqual(account["seed"], "JBSWY3DPEHPK3PXP")
        self.assertTrue(self.win.dashboard.code_button.isEnabled())
        self.calls["enable_mfa"].assert_called_once()

    def test_removing_one_profile_leaves_other_profiles_and_auth_accounts(self):
        self.win.accounts = [
            {"name": "One#EUW", "puuid": "owner-one", "local_id": "one"},
            {"name": "Two#RU", "puuid": "owner-two", "local_id": "two"},
            {"name": "Legacy#EUW", "seed": "existing-seed"}]
        first = self.win.accounts[0]
        self.win._remove_account(first["name"], None, account_key(first))
        self.assertEqual([a["name"] for a in self.win.accounts], ["Two#RU", "Legacy#EUW"])

    def test_profile_only_never_shows_auth_approval_for_old_device_push(self):
        self.win._run_login()
        with patch("app.ui.main_window.MfaPromptDialog") as dialog:
            self.win._on_push({"puuid": "owner-one", "suuid": "old-device-request"})
            dialog.assert_not_called()

    def test_profile_removed_during_setup_is_not_resurrected(self):
        self.win._run_login()
        account = self.win.accounts[0]
        key = account_key(account)
        self.win.accounts.clear()
        self.calls["save_accounts"].reset_mock()
        self.win._on_login_result({"kind": "success", "mode": "2fa", "target_key": key,
                                  "name": account["name"], "account": {"name": account["name"],
                                      "puuid": account["puuid"], "seed": "fake-seed"}})
        self.assertEqual(self.win.accounts, [])
        self.calls["save_accounts"].assert_not_called()

    def test_qr_uses_profile_session_without_enrolling_mfa_or_saving_cookies(self):
        self.win._run_login()
        account = self.win.accounts[0]
        self.calls["save_accounts"].reset_mock()
        with patch("app.ui.main_window.qr_session_info", return_value={}), \
             patch("app.ui.main_window.qr_approve", return_value={"success": True}) as approve, \
             patch("app.ui.main_window.QrConfirmDialog") as confirm:
            confirm.return_value.exec.return_value = QDialog.DialogCode.Accepted
            self.win._complete_qr_signin(account, "fake-request", "test-cluster")
            self.wait_for_session()
            approve.assert_called_once_with(self.qr_token, "fake-request", "test-cluster", remember=True)
        self.calls["enable_mfa"].assert_not_called()
        self.calls["fetch_mfa_factors"].assert_not_called()
        self.calls["save_accounts"].assert_not_called()
        for field in ("seed", "sso", "access_token"):
            self.assertNotIn(field, account)

    def test_profiles_without_saved_sessions_can_be_chosen_for_qr(self):
        self.win._run_login()
        account = self.win.accounts[0]
        with patch("app.ui.main_window.QInputDialog.getItem", return_value=(account["name"], True)):
            self.assertIs(self.win._pick_account(), account)

    def test_qr_without_session_starts_normal_login_for_selected_owner(self):
        self.win._run_login()
        account = self.win.accounts[0]
        self.win._profile_sessions.clear()
        with patch.object(self.win, "_add_via_login") as login:
            self.win._complete_qr_signin(account, "fake-request", "test-cluster")
            login.assert_called_once_with(target=account, qr_request=("fake-request", "test-cluster"))
        self.calls["enable_mfa"].assert_not_called()

    def test_qr_reauthentication_resumes_request_without_creating_an_account(self):
        self.win._run_login()
        account = self.win.accounts[0]
        original = copy.deepcopy(account)
        self.win._profile_sessions.clear()
        self.calls["save_accounts"].reset_mock()
        with patch("app.ui.main_window.qr_session_info", return_value={}), \
             patch("app.ui.main_window.qr_approve", return_value={"success": True}) as approve, \
             patch("app.ui.main_window.QrConfirmDialog") as confirm:
            confirm.return_value.exec.return_value = QDialog.DialogCode.Accepted
            self.win._run_login(target=original, qr_request=("fake-request", "test-cluster"))
            self.wait_for_session()
            approve.assert_called_once()
        self.assertEqual(self.win.accounts, [original])
        self.calls["save_accounts"].assert_not_called()
        self.calls["enable_mfa"].assert_not_called()
        self.calls["fetch_mfa_factors"].assert_not_called()

    def test_qr_reauthentication_for_wrong_owner_does_not_approve(self):
        self.win._run_login()
        account = self.win.accounts[0]
        self.calls["puuid_from_user"].return_value = "another-owner"
        with patch("app.ui.main_window.qr_approve") as approve:
            self.win._run_login(target=copy.deepcopy(account), qr_request=("fake-request", "test-cluster"))
            approve.assert_not_called()
        self.assertEqual(self.calls["show_error"].call_args.args[1], "Открыт другой аккаунт")

    def test_legacy_profile_receives_verified_owner_id_for_renewable_qr_session(self):
        legacy = {"name": "Owner#EUW", "seed": "existing-seed"}
        self.win.accounts.append(legacy)
        self.win._populate()
        key = account_key(legacy)
        self.win._run_login(target=copy.deepcopy(legacy), refresh_session=True)
        self.assertEqual(legacy["puuid"], "owner-one")
        self.assertEqual(account_key(legacy), key)
        self.assertEqual(self.win.sessions.sessions[key]["puuid"], "owner-one")
        self.calls["enable_mfa"].assert_not_called()

    def test_qr_network_failure_does_not_open_browser_or_approve(self):
        from app.api.riot_api import SessionRefreshError
        self.win._run_login()
        self.win.sessions.refresher.side_effect = SessionRefreshError("Нет соединения с Riot.")
        with patch.object(self.win, "_add_via_login") as login, patch("app.ui.main_window.qr_approve") as approve:
            self.win._complete_qr_signin(self.win.accounts[0], "fake-request", "test-cluster")
            self.wait_for_session()
            login.assert_not_called()
            approve.assert_not_called()
        self.calls["QMessageBox.warning"].assert_called_once()

    def test_qr_cancellation_never_approves_the_signin(self):
        self.win._run_login()
        with patch("app.ui.main_window.qr_session_info", return_value={}), \
             patch("app.ui.main_window.qr_approve") as approve, \
             patch("app.ui.main_window.QrConfirmDialog") as confirm:
            confirm.return_value.exec.return_value = QDialog.DialogCode.Rejected
            self.win._complete_qr_signin(self.win.accounts[0], "fake-request", "test-cluster")
            self.wait_for_session()
            approve.assert_not_called()

    def test_qr_reauthentication_failure_does_not_loop_browser_logins(self):
        self.win._run_login()
        account = self.win.accounts[0]
        self.win.sessions.refresher.side_effect = SessionExpired("Riot требует повторный вход.")
        with patch.object(self.win, "_add_via_login") as login:
            self.win._run_login(target=copy.deepcopy(account), qr_request=("fake-request", "test-cluster"))
            self.wait_for_session()
            login.assert_not_called()
        self.calls["QMessageBox.warning"].assert_called_once()
        self.calls["enable_mfa"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
