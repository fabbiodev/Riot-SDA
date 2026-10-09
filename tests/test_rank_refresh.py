import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication
from app.core.search import account_key
from app.core.sessions import SessionManager
from app.core.session_store import MemorySessionStore
from app.ui.main_window import MainWindow


def accounts():
    return [{"name": "One#EUW", "local_id": "one", "games": {"lol": {"region": "EUW"}}},
            {"name": "Two#RU", "local_id": "two", "api_routes": {"lol": "RU"}},
            {"name": "Unknown", "local_id": "legacy"}]


class RankRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.thread = patch("app.ui.main_window.threading.Thread").start()
        patch.object(MainWindow, "_refresh_skin_types").start()
        patch("app.ui.main_window.ClientSessionManager").start()
        patch("app.ui.main_window.AccountDetailsManager").start()
        patch("app.ui.main_window.FcmService").start()
        patch("app.ui.main_window.save_accounts").start()
        self.win = MainWindow(accounts=accounts(), start_services=False)

    def tearDown(self):
        self.win._stop_rank_refresh()
        self.win.timer.stop()
        self.win.deleteLater()
        self.app.processEvents()
        patch.stopall()

    def finish(self, account, **extra):
        result = {"key": account_key(account), "game": "lol", "operation": "ranks",
                  "lookup_name": account["name"], "lookup_region": account.get("api_routes", {}).get("lol", "EUW"),
                  "data": {"ranks": {"solo": {"status": "unranked", "updated_at": 200, "source": "OP.GG"}}}}
        result.update(extra)
        self.win._on_game_data_result(result)

    def test_startup_hourly_all_accounts_and_sequential_requests(self):
        # Services enabled, but session persistence/network are isolated in memory.
        self.win.deleteLater()
        self.app.processEvents()
        def memory_sessions(parent, **kwargs):
            return SessionManager(parent, store=MemorySessionStore(), autostart=False)
        with patch("app.ui.main_window.SessionManager", side_effect=memory_sessions), \
             patch.object(MainWindow, "_setup_tray"):
            self.win = MainWindow(accounts=accounts(), start_services=True)
        self.app.processEvents()
        self.assertTrue(self.win.rank_timer.isActive())
        self.assertEqual(self.win.rank_timer.interval(), 3600000)
        self.assertEqual(self.thread.call_count, 1)
        self.assertEqual(self.thread.call_args.kwargs["args"][4], "EUW")
        one, two = self.win.accounts[:2]
        self.assertEqual(self.win._rank_pending, {account_key(two)})
        self.win.rank_timer.timeout.emit()
        self.assertEqual(self.thread.call_count, 1)
        with patch("app.ui.main_window.time.monotonic", return_value=100):
            self.finish(one)
        with patch("app.ui.main_window.time.monotonic", return_value=103):
            self.win._drain_rank_queue()
        self.assertEqual(self.thread.call_count, 2)
        self.assertEqual(self.thread.call_args.kwargs["args"][1]["name"], "Two#RU")
        with patch("app.ui.main_window.time.monotonic", return_value=104):
            self.finish(two)
        with patch("app.ui.main_window.time.monotonic", return_value=3600):
            self.win.rank_timer.timeout.emit()
        self.assertEqual(self.thread.call_count, 3)

    def test_manual_refresh_requires_no_key_or_dialog_and_preserves_selection(self):
        self.win.api_keys = {"lol": "", "tft": "", "valorant": ""}
        selected = self.win.dashboard.selected_key
        with patch.object(self.win, "_data_settings") as settings:
            self.win._refresh_game_data("ranks")
        settings.assert_not_called()
        self.assertEqual(self.thread.call_count, 1)
        self.finish(self.win.accounts[0])
        self.assertEqual(self.win.dashboard.selected_key, selected)

    def test_busy_account_waits_and_removed_account_is_not_queried(self):
        one, two = self.win.accounts[:2]
        self.win._data_jobs.add((account_key(one), "lol"))
        self.win._queue_rank_refresh()
        self.assertEqual(self.thread.call_args.kwargs["args"][1]["name"], "Two#RU")
        self.win.accounts.remove(one)
        self.finish(two)
        with patch("app.ui.main_window.time.monotonic", return_value=self.win._rank_not_before + 1):
            self.win._drain_rank_queue()
        self.assertEqual(self.thread.call_count, 1)
        self.assertFalse(self.win._rank_pending)

    def test_rate_limit_pauses_whole_queue_and_retries_without_wiping_rank(self):
        one = self.win.accounts[0]
        one["games"]["lol"]["ranks"] = {"solo": {"status": "ranked", "tier": "GOLD", "division": "I", "lp": 50}}
        self.win._queue_rank_refresh()
        with patch("app.ui.main_window.time.monotonic", return_value=100):
            self.finish(one, data={"ranks": {"solo": {"status": "unavailable", "error": "Лимит OP.GG"}},
                                   "rank_retry_after": 7200})
        self.assertEqual(one["games"]["lol"]["ranks"]["solo"]["lp"], 50)
        self.assertNotIn("rank_retry_after", one["games"]["lol"])
        with patch("app.ui.main_window.time.monotonic", return_value=7299):
            self.win._drain_rank_queue()
        self.assertEqual(self.thread.call_count, 1)
        with patch("app.ui.main_window.time.monotonic", return_value=7300):
            self.win._drain_rank_queue()
        self.assertEqual(self.thread.call_count, 2)

    def test_region_change_rejects_old_result_and_queries_new_region(self):
        one = self.win.accounts[0]
        self.win._queue_rank_refresh([one])
        one["api_routes"] = {"lol": "KR"}
        self.finish(one, lookup_region="EUW")
        self.assertNotIn("ranks", one["games"]["lol"])
        with patch("app.ui.main_window.time.monotonic", return_value=self.win._rank_not_before + 1):
            self.win._drain_rank_queue()
        self.assertEqual(self.thread.call_args.kwargs["args"][4], "KR")

    def test_login_queues_account_and_quit_stops_future_refresh(self):
        self.win._rank_auto_enabled = True
        self.win._on_login_result({"kind": "success", "mode": "profile", "account": {
            "name": "New#KR1", "local_id": "new"}, "name": "New#KR1"})
        self.assertEqual(self.thread.call_args.kwargs["args"][1]["name"], "New#KR1")
        self.win._stop_rank_refresh()
        self.win._queue_rank_refresh()
        self.assertFalse(self.win._rank_pending)
        self.assertEqual(self.thread.call_count, 1)

    def test_unknown_server_waits_for_saved_session_then_fetches_without_game_or_key(self):
        account = {"name": "New#Custom", "local_id": "new", "puuid": "owner"}
        self.win.accounts.append(account)
        key = account_key(account)
        record = {"puuid": "owner", "generation": "first", "sso": {"ssid": "cookie"}, "status": "retry"}
        self.win._profile_sessions[key] = record
        with patch.object(self.win.sessions, "request") as request:
            self.win._queue_rank_refresh([account])
        request.assert_called_once_with(key)
        self.thread.assert_not_called()
        record.update(access_token="private-token", expires_at=10**11, status="ready")
        self.win._on_session_ready(key, "ready")
        self.app.processEvents()
        self.assertEqual(self.thread.call_count, 1)
        args = self.thread.call_args.kwargs["args"]
        self.assertEqual(args[4], "")
        self.assertFalse(args[5])  # No Developer API key or local game needed.
        with patch("app.ui.main_window.resolve_rank_region", return_value="EUW") as resolve, \
             patch("app.ui.main_window.fetch_auto_rankings", return_value={"ranks": {}}) as fetch:
            self.win._game_data_worker(*args)
        resolve.assert_called_once_with(args[1], "private-token")
        fetch.assert_called_once_with("New#Custom", "EUW", "")
        self.assertEqual(account["league_region"], "EUW")
        self.assertNotIn("access_token", account)

    def test_new_region_drops_foreign_rank_and_session_change_rejects_inflight_result(self):
        account = self.win.accounts[0]
        account["puuid"] = "owner"
        key = account_key(account)
        self.win._profile_sessions[key] = {"generation": "new"}
        account["games"]["lol"]["ranks"] = {"solo": {"status": "ranked", "tier": "GOLD", "lp": 50}}
        self.finish(account, lookup_puuid="owner", lookup_generation="old", resolved_region="KR")
        self.assertNotIn("league_region", account)
        self.finish(account, lookup_puuid="owner", lookup_generation="new", resolved_region="KR",
                    data={"ranks": {"solo": {"status": "unavailable"}}})
        self.assertEqual(account["league_region"], "KR")
        self.assertEqual(account["games"]["lol"]["ranks"]["solo"]["status"], "unavailable")

    def test_session_retry_keeps_pending_rank_until_session_is_ready(self):
        account = {"name": "New#Custom", "local_id": "new", "puuid": "owner"}
        self.win.accounts.append(account)
        key = account_key(account)
        self.win._rank_pending.add(key)
        self.win._rank_waiting_sessions.add(key)
        self.win._on_session_ready(key, "retry")
        self.assertIn(key, self.win._rank_pending)
        self.assertIn(key, self.win._rank_waiting_sessions)
        self.win._on_session_ready(key, "login")
        self.assertNotIn(key, self.win._rank_waiting_sessions)
        self.assertNotIn(key, self.win._rank_pending)

    def test_legacy_default_waits_for_identity_instead_of_requesting_wrong_server(self):
        account = self.win.accounts[1]
        account["puuid"] = "owner"
        key = account_key(account)
        self.win._profile_sessions[key] = {"puuid": "owner", "sso": {"ssid": "cookie"}, "status": "retry"}
        with patch.object(self.win.sessions, "request") as request:
            self.win._queue_rank_refresh([account])
        request.assert_called_once_with(key)
        self.thread.assert_not_called()
        self.assertIn(key, self.win._rank_pending)


if __name__ == "__main__":
    unittest.main()
