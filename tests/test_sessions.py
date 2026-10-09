import base64
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import requests
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest

from app.api.riot_api import (refresh_sso_session, SessionExpired, SessionRefreshError,
                              token_expires_at, mint_access_token)
from app.core.sessions import SessionManager, remaining_text
from app.core.session_store import SessionStore, MemorySessionStore, VaultError
from app.core.search import account_key

NOW = 2000000000
ACCOUNT = {"local_id": "local-owner", "name": "Owner#RU", "puuid": "owner-one"}


def jwt(exp=NOW + 3600, sub="owner-one"):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp, "sub": sub}).encode()).decode().rstrip("=")
    return f"test.{payload}.signature"


class RefreshApiTests(unittest.TestCase):
    def make_session(self, response):
        session = Mock()
        session.cookies = requests.cookies.RequestsCookieJar()
        session.post.return_value = response
        return session

    def response(self, status=200, data=None, headers=None):
        response = Mock(status_code=status, headers=headers or {})
        response.json.return_value = data if data is not None else {"type": "response", "response": {
            "parameters": {"uri": "http://localhost/redirect#access_token=" + quote(jwt(), safe="")}}}
        return response

    @patch("app.api.riot_api.time.time", return_value=NOW)
    def test_rotation_expiry_and_only_riot_cookies_are_retained(self, _time):
        response = self.response()
        session = self.make_session(response)
        def post(*args, **kwargs):
            session.cookies.set("ssid", "rotated-cookie", domain="auth.riotgames.com", expires=NOW + 86400)
            session.cookies.set("arbitrary", "not-session-data", domain="auth.riotgames.com")
            session.cookies.set("ssid", "wrong-host", domain="other.example")
            return response
        session.post.side_effect = post
        with patch("app.api.riot_api.requests.Session", return_value=session):
            result = refresh_sso_session({"ssid": "original-cookie", "other": "not-allowed"})
        self.assertEqual(result["access_token"], jwt())
        self.assertEqual(result["sso"], {"ssid": "rotated-cookie"})
        self.assertEqual(result["expires_at"], NOW + 3600)
        self.assertEqual(result["cookie_expires_at"], NOW + 86400)
        call = session.post.call_args
        self.assertEqual(call.args[0], "https://auth.riotgames.com/api/v1/authorization")
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertFalse(session.trust_env)
        self.assertNotIn("original-cookie", str(call))

    def test_login_challenge_and_revocation_stop_refresh(self):
        for response in (self.response(401), self.response(403), self.response(data={"type": "auth"}),
                         self.response(data={"type": "multifactor"})):
            with self.subTest(response=response), patch("app.api.riot_api.requests.Session", return_value=self.make_session(response)):
                with self.assertRaises(SessionExpired):
                    refresh_sso_session({"ssid": "fake-cookie"})

    def test_network_and_rate_limit_are_retryable_without_leaking_secrets(self):
        session = self.make_session(self.response())
        session.post.side_effect = requests.ConnectionError("synthetic-cookie-in-exception")
        with patch("app.api.riot_api.requests.Session", return_value=session):
            with self.assertRaises(SessionRefreshError) as caught:
                refresh_sso_session({"ssid": "synthetic-cookie-in-exception"})
        self.assertNotIn("synthetic-cookie", str(caught.exception))
        session = self.make_session(self.response(429, headers={"Retry-After": "120"}))
        with patch("app.api.riot_api.requests.Session", return_value=session):
            with self.assertRaises(SessionRefreshError) as caught:
                refresh_sso_session({"ssid": "fake-cookie"})
        self.assertEqual(caught.exception.retry_after, 120)

    @patch("app.api.riot_api.time.time", return_value=NOW)
    def test_invalid_token_or_missing_exp_is_not_a_successful_refresh(self, _time):
        for token in ("bad-token", jwt(None), jwt(True), jwt(NOW - 1), jwt("tomorrow")):
            response = self.response(data={"type": "response", "response": {"parameters": {
                "uri": "http://localhost/redirect#access_token=" + quote(token)}}})
            with self.subTest(token=token), patch("app.api.riot_api.requests.Session", return_value=self.make_session(response)):
                with self.assertRaises(SessionRefreshError):
                    refresh_sso_session({"ssid": "fake-cookie"})
        self.assertIsNone(token_expires_at("bad-token"))

    def test_legacy_mint_persists_cookie_rotation_in_provided_jar(self):
        cookies = {"ssid": "old"}
        with patch("app.api.riot_api.refresh_sso_session", return_value={"access_token": jwt(), "sso": {"ssid": "new"}}):
            self.assertEqual(mint_access_token(cookies), jwt())
        self.assertEqual(cookies, {"ssid": "new"})


class SessionManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.store = Mock(spec=MemorySessionStore)
        self.store.load.return_value = {}
        self.refresh = Mock(return_value={"access_token": jwt(), "sso": {"ssid": "rotated"}})
        self.now = NOW
        self.manager = SessionManager(store=self.store, autostart=False, refresher=self.refresh, now=lambda: self.now)
        self.manager.put(ACCOUNT, {"ssid": "fake-cookie"}, NOW + 86400)
        self.key = account_key(ACCOUNT)

    def tearDown(self):
        self.manager.stop()
        self.manager.deleteLater()
        self.app.processEvents()

    def wait_worker(self):
        for _ in range(200):
            self.app.processEvents()
            if not self.manager.jobs:
                return
            QTest.qWait(5)
        self.fail("Worker did not finish")

    def test_refresh_keeps_rotated_session_and_renews_before_expiry(self):
        self.manager.request(self.key)
        self.wait_worker()
        record = self.manager.sessions[self.key]
        self.assertEqual(record["sso"]["ssid"], "rotated")
        self.assertEqual(record["expires_at"], NOW + 3600)
        self.assertEqual(record["next_attempt"], NOW + 3300)
        self.assertEqual(record["cookie_expires_at"], NOW + 86400)
        self.assertEqual(self.manager.token(self.key), jwt())
        self.manager.active = True
        self.manager.poll()
        self.refresh.assert_called_once()
        self.now = NOW + 3300
        self.refresh.return_value = {"access_token": jwt(NOW + 7200), "sso": {"ssid": "rotated-again"}}
        self.manager.poll()
        self.wait_worker()
        self.assertEqual(self.manager.token(self.key), jwt(NOW + 7200))

    def test_cached_token_does_not_make_a_request_for_every_qr(self):
        self.manager.request(self.key)
        self.wait_worker()
        ready = Mock()
        self.manager.ready.connect(ready)
        self.manager.request(self.key)
        self.manager.request(self.key)
        self.refresh.assert_called_once()
        self.assertEqual(ready.call_args.args, (self.key, "ready"))

    def test_cookie_deadline_is_renewed_early_and_honored_if_it_expires(self):
        self.manager.sessions[self.key]["cookie_expires_at"] = NOW + 600
        self.manager.request(self.key)
        self.wait_worker()
        self.assertEqual(self.manager.sessions[self.key]["next_attempt"], NOW + 480)
        self.now = NOW + 601
        self.manager.active = True
        self.manager.poll()
        self.refresh.assert_called_once()
        self.assertNotIn("sso", self.manager.sessions[self.key])
        self.assertEqual(self.manager.sessions[self.key]["status"], "token_only")
        self.assertEqual(self.manager.token(self.key), jwt())
        self.now = NOW + 3600
        self.assertIsNone(self.manager.token(self.key))

    def test_rotation_without_expiry_does_not_show_an_old_cookie_deadline(self):
        self.refresh.return_value.update(cookie_rotated=True, cookie_expires_at=None)
        self.manager.request(self.key)
        self.wait_worker()
        self.assertIsNone(self.manager.sessions[self.key]["cookie_expires_at"])

    def test_revocation_clears_credentials_and_does_not_retry(self):
        self.refresh.side_effect = SessionExpired("Riot требует вход.")
        self.manager.request(self.key)
        self.wait_worker()
        self.manager.active = True
        self.now += 100000
        self.manager.poll()
        self.refresh.assert_called_once()
        record = self.manager.sessions[self.key]
        self.assertEqual(record["status"], "login")
        self.assertNotIn("sso", record)
        self.assertNotIn("access_token", record)
        self.assertIsNone(self.manager.token(self.key))

    def test_transient_failure_backs_off_and_retains_valid_token(self):
        self.manager.request(self.key)
        self.wait_worker()
        self.refresh.side_effect = SessionRefreshError("Riot ограничил обновление.", retry_after=120)
        self.manager.request(self.key, force=True)
        self.wait_worker()
        self.assertEqual(self.manager.sessions[self.key]["next_attempt"], NOW + 120)
        self.assertEqual(self.manager.token(self.key), jwt())
        self.manager.request(self.key, force=True)
        self.assertEqual(self.refresh.call_count, 2)
        self.now = NOW + 120
        self.manager.request(self.key, force=True)
        self.wait_worker()
        self.assertEqual(self.refresh.call_count, 3)
        self.assertEqual(self.manager.sessions[self.key]["next_attempt"], self.now + 120)

    def test_wrong_token_owner_requires_login(self):
        self.refresh.return_value["access_token"] = jwt(sub="other-owner")
        self.manager.request(self.key)
        self.wait_worker()
        self.assertIsNone(self.manager.token(self.key))
        self.assertEqual(self.manager.sessions[self.key]["status"], "login")

    def test_late_worker_cannot_resurrect_deleted_or_replaced_session(self):
        record = copy.deepcopy(self.manager.sessions[self.key])
        result = {"key": self.key, "generation": record["generation"], "data": self.refresh.return_value}
        self.manager.jobs[self.key] = record["generation"]
        self.manager.remove(self.key)
        self.manager._apply(result)
        self.assertNotIn(self.key, self.manager.sessions)
        self.manager.put(ACCOUNT, {"ssid": "new-interactive-login"})
        self.manager._apply(result)
        self.assertEqual(self.manager.sessions[self.key]["sso"]["ssid"], "new-interactive-login")

    def test_poll_limits_concurrency_and_duplicate_requests(self):
        for i in range(4):
            self.manager.put({**ACCOUNT, "local_id": f"owner-{i}"}, {"ssid": "fake-cookie"})
        self.manager.active = True
        with patch("app.core.sessions.threading.Thread") as thread:
            self.manager.poll()
            self.manager.poll()
            self.assertEqual(thread.call_count, 2)
            self.assertEqual(len(self.manager.jobs), 2)
        self.manager.jobs.clear()

    def test_restored_sessions_match_account_owner_and_survive_restart(self):
        self.manager.request(self.key)
        self.wait_worker()
        self.store.load.return_value = copy.deepcopy(self.manager.sessions)
        restored = SessionManager(store=self.store, autostart=False, now=lambda: NOW)
        restored.sync_accounts([ACCOUNT])
        self.assertEqual(restored.token(self.key), jwt())
        restored.sync_accounts([{**ACCOUNT, "puuid": "different-owner"}])
        self.assertNotIn(self.key, restored.sessions)
        restored.deleteLater()

    def test_forgetting_prevents_legacy_credentials_from_reappearing(self):
        self.manager.forget(ACCOUNT)
        self.manager.sync_accounts([{**ACCOUNT, "sso": {"ssid": "legacy-cookie"}}])
        self.assertEqual(self.manager.sessions[self.key]["status"], "forgotten")
        self.assertNotIn("sso", self.manager.sessions[self.key])

    def test_status_contains_no_credentials_and_reports_save_failure(self):
        self.store.save.side_effect = VaultError("Не удалось сохранить сессию.")
        self.manager.put(ACCOUNT, {"ssid": "secret-cookie"})
        status = self.manager.public_status()[self.key]
        self.assertFalse(status["saved"])
        self.assertIn("сохранить", status["storage_error"])
        for secret in ("ssid", "puuid", "secret-cookie", "access_token"):
            self.assertNotIn(secret, str(status))
        self.assertEqual(remaining_text(3661), "01:01:01")
        self.assertEqual(remaining_text(-4), "00:00")


@unittest.skipUnless(os.name == "nt", "Windows DPAPI required")
class SessionVaultTests(unittest.TestCase):
    def test_encrypted_roundtrip_and_atomic_failure_preserve_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.dpapi"
            store = SessionStore(path)
            records = {"test": {"puuid": "fake-owner", "name": "Test#RU", "sso": {"ssid": "synthetic-cookie"}}}
            store.save(records)
            self.assertEqual(store.load(), records)
            self.assertNotIn(b"synthetic-cookie", path.read_bytes())
            with patch("app.core.session_store.os.replace", side_effect=OSError("test-failure")):
                with self.assertRaises(VaultError):
                    store.save({})
            self.assertEqual(store.load(), records)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_corrupt_file_is_not_treated_as_a_valid_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.dpapi"
            path.write_bytes(b"not-a-dpapi-vault")
            with self.assertRaises(VaultError):
                SessionStore(path).load()
