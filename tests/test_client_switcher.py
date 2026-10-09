import base64
import copy
from contextlib import nullcontext
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import requests
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest

from app.api.client_auth import create_client_session, ClientAuthError, ClientLoginRequired, SCOPES, DESKTOP_SCOPES
from app.core.client_switcher import (ClientVault, NativeClient, SwitchError, generated_snapshot,
                                      private_owner, switch_client, recover, capture_current, PRIVATE_FILES)
from app.core.client_sessions import ClientSessionManager
from app.core.sessions import SessionManager
from app.core.session_store import MemorySessionStore
from app.core.search import account_key
from app.ui.main_window import MainWindow

ACCOUNT = {"name": "Synthetic#EU", "puuid": "synthetic-owner", "local_id": "client-test"}
OTHER = {"name": "Other#EU", "puuid": "other-owner", "local_id": "other-test"}


def jwt(owner="synthetic-owner", nonce="test", audience="riot-client"):
    payload = {"sub": owner, "aud": audience, "iss": "https://auth.riotgames.com",
               "exp": time.time() + 3600, "nonce": nonce}
    return "test." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".signature"


def tokens(owner="synthetic-owner"):
    return {"puuid": owner, "id_token": jwt(owner), "refresh_token": "synthetic-refresh",
            "scopes": list(DESKTOP_SCOPES), "created_at": time.time()}


def response(payload, status=200):
    result = Mock(status_code=status, headers={})
    result.json.return_value = payload
    return result


class ClientAuthTests(unittest.TestCase):
    def fake_session(self, owner="synthetic-owner", wrong_state=False):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.cookies = requests.cookies.RequestsCookieJar()
        context = {}
        def post(url, **kwargs):
            if url.endswith("authorization"):
                context.update(kwargs["json"])
                state = "wrong" if wrong_state else context["state"]
                return response({"type": "response", "response": {"parameters": {
                    "uri": "http://localhost/redirect?code=synthetic-code&state=" + state}}})
            return response({"token_type": "Bearer", "access_token": "synthetic-access",
                             "id_token": jwt(owner, context["nonce"]),
                             "refresh_token": "synthetic-refresh", "scope": SCOPES})
        session.post.side_effect = post
        session.get.return_value = response({"sub": owner})
        return session

    def test_pkce_and_server_owner_validation_and_no_redirects_or_proxy(self):
        session = self.fake_session()
        with patch("app.api.client_auth.requests.Session", return_value=session):
            result = create_client_session(ACCOUNT, {"ssid": "synthetic-cookie", "unrelated": "excluded"})
        self.assertEqual(result["puuid"], ACCOUNT["puuid"])
        self.assertEqual(result["refresh_token"], "synthetic-refresh")
        self.assertNotIn("access_token", result)
        self.assertEqual(result["scopes"], list(DESKTOP_SCOPES))
        self.assertNotIn("offline_access", result["scopes"])  # Native restore requires its exact scopes.
        first, second = session.post.call_args_list
        self.assertEqual(first.kwargs["json"]["response_type"], "code")
        self.assertEqual(first.kwargs["json"]["code_challenge_method"], "S256")
        self.assertGreater(len(second.kwargs["data"]["code_verifier"]), 42)
        self.assertFalse(session.trust_env)
        self.assertEqual(session.cookies.get_dict(), {"ssid": "synthetic-cookie"})
        for call in [first, second, session.get.call_args]:
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertNotIn("verify", call.kwargs)  # Remote TLS stays enabled.
        self.assertTrue(session.get.call_args.args[0].startswith("https://auth.riotgames.com/"))

    def test_wrong_state_stops_before_token_exchange(self):
        session = self.fake_session(wrong_state=True)
        with patch("app.api.client_auth.requests.Session", return_value=session), self.assertRaises(ClientAuthError):
            create_client_session(ACCOUNT, {"ssid": "synthetic-cookie"})
        self.assertEqual(session.post.call_count, 1)
        session.get.assert_not_called()

    def test_wrong_owner_and_wrong_server_identity_are_rejected(self):
        for token_owner, server_owner in [("other-owner", "other-owner"), (ACCOUNT["puuid"], "other-owner")]:
            session = self.fake_session(owner=token_owner)
            session.get.return_value = response({"sub": server_owner})
            with self.subTest(token_owner=token_owner), patch("app.api.client_auth.requests.Session", return_value=session), \
                    self.assertRaises(ClientAuthError):
                create_client_session(ACCOUNT, {"ssid": "synthetic-cookie"})

    def test_challenge_is_not_approved_and_stops_exchange(self):
        session = self.fake_session()
        session.post.side_effect = None
        session.post.return_value = response({"type": "multifactor"})
        with patch("app.api.client_auth.requests.Session", return_value=session), self.assertRaises(ClientLoginRequired):
            create_client_session(ACCOUNT, {"ssid": "synthetic-cookie"})
        self.assertEqual(session.post.call_count, 1)

    def test_no_refresh_token_cannot_be_called_a_persistent_session(self):
        session = self.fake_session()
        original = session.post.side_effect
        def post(url, **kwargs):
            result = original(url, **kwargs)
            if url.endswith("/token"):
                result.json.return_value.pop("refresh_token")
            return result
        session.post.side_effect = post
        with patch("app.api.client_auth.requests.Session", return_value=session), self.assertRaises(ClientAuthError):
            create_client_session(ACCOUNT, {"ssid": "synthetic-cookie"})


class FakeNative:
    def __init__(self):
        self.files = generated_snapshot(tokens(OTHER["puuid"]))
        self.running = True
        self.game = False
        self.restores = []
        self.fail_wait = False
        self.fail_restore = False

    def exclusive(self): return nullcontext()
    def locate(self): return Path("synthetic/RiotClientServices.exe")
    def game_running(self): return self.game
    def owner(self): return private_owner(self.files) if self.running and self.files else ""
    def read_files(self): return dict(self.files)
    def stop(self): self.running = False
    def restore(self, files):
        if self.fail_restore: raise OSError("synthetic write failure")
        self.files = dict(files)
        self.restores.append(dict(files))
    def launch(self): self.running = True
    def wait_owner(self, owner, cancelled=lambda: False):
        if self.fail_wait or cancelled() or self.owner() != owner:
            raise SwitchError("synthetic restore denied")


class VaultAndSwitchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.vault = ClientVault(self.directory.name)
        self.native = FakeNative()
        self.target = generated_snapshot(tokens())
        self.vault.put(ACCOUNT["puuid"], self.target)

    def test_dpapi_roundtrip_keeps_refresh_tokens_out_of_plaintext(self):
        self.assertEqual(self.vault.files(ACCOUNT["puuid"]), self.target)
        self.assertNotIn(b"synthetic-refresh", self.vault.path.read_bytes())
        self.vault.save_recovery({})
        self.assertEqual(self.vault.recovery_files(), {})

    def test_wrong_owner_and_path_traversal_are_rejected_before_writes(self):
        with self.assertRaises(SwitchError): self.vault.put(OTHER["puuid"], self.target)
        for files in [{"../bad": b"payload"}, {PRIVATE_FILES[0]: "not-bytes"}]:
            with self.assertRaises(SwitchError): ClientVault.encode_files(files)
        with self.assertRaises(SwitchError): ClientVault.decode_files({"../bad": "cGF5bG9hZA=="})

    def test_success_saves_current_restores_target_and_verifies_owner(self):
        switch_client(self.native, self.vault, ACCOUNT, [ACCOUNT, OTHER])
        self.assertEqual(self.native.owner(), ACCOUNT["puuid"])
        self.assertIsNotNone(self.vault.files(OTHER["puuid"]))
        self.assertIsNone(self.vault.recovery_files())
        self.assertEqual(self.native.restores, [self.target])

    def test_failed_login_rolls_back_without_revoking_previous_session(self):
        original = dict(self.native.files)
        self.native.fail_wait = True
        with self.assertRaises(SwitchError): switch_client(self.native, self.vault, ACCOUNT, [ACCOUNT, OTHER])
        self.assertEqual(self.native.files, original)
        self.assertEqual(self.native.owner(), OTHER["puuid"])
        self.assertIsNone(self.vault.recovery_files())
        self.assertIsNone(self.vault.files(ACCOUNT["puuid"]))  # Updating SSO can prepare a new session.

    def test_cancellation_restores_the_original_session(self):
        original = dict(self.native.files)
        with self.assertRaises(SwitchError):
            switch_client(self.native, self.vault, ACCOUNT, [ACCOUNT, OTHER], cancelled=lambda: True)
        self.assertEqual(self.native.files, original)
        self.assertIsNone(self.vault.recovery_files())

    def test_partial_sdk_write_is_not_saved_as_a_session(self):
        with self.assertRaises(SwitchError): private_owner({PRIVATE_FILES[0]: b"psl:"})
        with self.assertRaises(SwitchError): private_owner({PRIVATE_FILES[0]: b"partial-write"})

    def test_rollback_write_failure_keeps_encrypted_recovery_for_next_start(self):
        original = dict(self.native.files)
        self.native.fail_restore = True
        with self.assertRaises(SwitchError): switch_client(self.native, self.vault, ACCOUNT, [ACCOUNT, OTHER])
        self.assertEqual(self.vault.recovery_files(), original)
        self.native.fail_restore = False
        self.assertTrue(recover(self.native, self.vault))
        self.assertEqual(self.native.files, original)
        self.assertIsNone(self.vault.recovery_files())

    def test_running_game_prevents_process_stop_and_file_changes(self):
        self.native.game = True
        with self.assertRaises(SwitchError): switch_client(self.native, self.vault, ACCOUNT, [ACCOUNT, OTHER])
        self.assertTrue(self.native.running)
        self.assertFalse(self.native.restores)
        self.assertIsNone(self.vault.recovery_files())

    def test_current_client_is_saved_only_for_an_existing_matching_profile(self):
        self.assertIsNone(capture_current(self.native, self.vault, [ACCOUNT]))
        self.assertIsNone(self.vault.files(OTHER["puuid"]))

    def test_real_restore_refuses_to_write_while_client_is_running(self):
        native = NativeClient(root=self.directory.name)
        with patch.object(native, "services", return_value=[object()]), self.assertRaises(SwitchError):
            native.restore(self.target)
        self.assertFalse(native.data.exists())


class ClientManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.sessions = SessionManager(store=MemorySessionStore(), autostart=False)
        self.sessions.put(ACCOUNT, {"ssid": "synthetic-cookie"})
        self.native = FakeNative()
        self.native.files = {}
        self.native.running = False
        self.manager = ClientSessionManager(self.sessions, enabled=False,
                                            vault=ClientVault(self.directory.name), native=self.native)
        self.manager.set_accounts([ACCOUNT])
        self.addCleanup(self.manager.stop)

    def wait_done(self):
        deadline = time.monotonic() + 3
        while self.manager.busy and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertFalse(self.manager.busy)

    def test_existing_sso_creates_desktop_session_automatically_once(self):
        with patch("app.core.client_sessions.create_client_session", return_value=tokens()) as create:
            self.manager.enabled = True
            self.manager.poll()
            self.wait_done()
            self.assertIsNotNone(self.manager.vault.files(ACCOUNT["puuid"]))
            self.assertEqual(self.manager.info[account_key(ACCOUNT)]["status"], "ready")
            self.manager.next_request = 0
            self.manager.poll()
            self.wait_done()
            create.assert_called_once()

    def test_launch_prepares_missing_session_then_switches_without_qr(self):
        with patch("app.core.client_sessions.create_client_session", return_value=tokens()), \
                patch("app.core.client_sessions.switch_client") as switch:
            self.manager.launch(ACCOUNT)
            self.wait_done()
            switch.assert_called_once()
            self.assertEqual(switch.call_args.args[2], ACCOUNT)

    def test_deleted_account_during_exchange_is_not_saved_or_launched(self):
        def mint(*args):
            self.manager.set_accounts([])
            return tokens()
        with patch("app.core.client_sessions.create_client_session", side_effect=mint), \
                patch("app.core.client_sessions.switch_client") as switch:
            self.manager.launch(ACCOUNT)
            self.wait_done()
            switch.assert_not_called()
            self.assertIsNone(self.manager.vault.files(ACCOUNT["puuid"]))

    def test_ui_button_uses_selected_profile_and_disables_during_switch(self):
        win = MainWindow(accounts=[copy.deepcopy(ACCOUNT), copy.deepcopy(OTHER)], start_services=False)
        self.addCleanup(win.deleteLater)
        self.addCleanup(win.timer.stop)
        with patch.object(win.client_sessions, "launch") as launch:
            win._dashboard_action("client-launch")
            launch.assert_called_once_with(win.dashboard.current_account)
        win.client_sessions.busy = True
        win._update_client_status()
        self.assertFalse(win.dashboard.launch_button.isEnabled())
