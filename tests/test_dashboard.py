import copy
import os
import tempfile
import unittest
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt, QSettings, QSize, QPoint, QPointF, QRect, QObject
from PyQt6.QtGui import QPixmap, QColor, QWheelEvent, QPainter
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QStyleOptionViewItem

from app.styles import load_font, load_stylesheet
from app.ui.dashboard import Dashboard
from app.ui.main_window import MainWindow
from app.core.search import account_key
from app.ui.artwork import public_asset_url, league_artwork
from app.ui.rank_widgets import rank_pixmap
from app.core.paths import resource_path
from app.api.rankings import TIERS


def fixture_accounts():
    return [{"name": "nightshift#EUW", "login": "night_login", "seed": "JBSWY3DPEHPK3PXP", "puuid": "owner-one",
             "games": {"lol": {"region": "EUW", "level": 247, "riot_id": "nightshift#EUW",
                 "ranks": {"solo": {"status": "ranked", "tier": "PLATINUM", "division": "II", "lp": 48, "region": "EUW", "updated_at": 1781000000},
                           "flex": {"status": "unranked", "region": "EUW", "updated_at": 1781000000},
                           "tft": {"status": "ranked", "tier": "DIAMOND", "division": "IV", "lp": 21, "region": "EUW", "updated_at": 1781000000}},
                 "characters": [{"id": "103", "name": "Ари", "aliases": ["Ahri"], "role": "Маг", "skin_ids": ["103027"], "kind": "owned"},
                                {"id": "222", "name": "Джинкс", "aliases": ["Jinx"], "role": "Стрелок", "skin_ids": [], "kind": "owned"}],
                 "skins": [{"id": "103027", "name": "Дух цветения Ари", "owner": "Ари", "aliases": ["Spirit Blossom Ahri"], "kind": "owned"}],
                 "collection_source": "League Client", "collection_updated_at": 1781000000},
                       "valorant": {"region": "EU", "level": 162,
                 "characters": [{"id": "jett", "name": "Jett", "role": "Дуэлянт", "kind": "owned"}],
                 "skins": [{"id": "reaver", "name": "Жнец Vandal", "owner": "Vandal", "aliases": ["Reaver Vandal"], "kind": "owned"}]}}},
            {"name": "sage#313", "login": "alt_account", "seed": "GEZDGNBVGY3TQOJQ", "puuid": "owner-two"}]


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        cls.app.setFont(load_font())
        cls.app.setStyleSheet(load_stylesheet())
        cls.directory = tempfile.TemporaryDirectory()
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, cls.directory.name)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.widget = Dashboard(load_artwork=False)
        self.widget.resize(1080, 820)
        self.widget.set_accounts(fixture_accounts())
        self.widget.show()
        self.app.processEvents()

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()

    def test_inter_variable_loaded(self):
        self.assertIn("Inter", self.app.font().family())

    def test_typing_filters_login_and_clearing_restores_list(self):
        self.widget.account_search.setFocus()
        QTest.keyClicks(self.widget.account_search, "ALT_ACCOUNT")
        self.assertEqual(self.widget.account_list.count(), 1)
        self.assertEqual(self.widget.current_account["name"], "sage#313")
        self.assertEqual(self.app.focusWidget(), self.widget.account_search)
        self.widget.account_search.clear()
        self.assertEqual(self.widget.account_list.count(), 2)

    def test_english_champion_and_skin_search_and_empty_result(self):
        QTest.keyClicks(self.widget.inventory_search, "AHRI")
        self.assertEqual(self.widget.inventory_grid.count(), 1)
        self.assertEqual(self.widget.inventory_grid.item(0).text(), "Ари")
        self.widget.set_collection("skins")
        QTest.keyClicks(self.widget.inventory_search, "SPIRIT")
        self.assertEqual(self.widget.inventory_grid.count(), 1)
        self.widget.inventory_search.setText("does-not-exist")
        self.assertTrue(self.widget.empty_collection.isVisible())
        self.assertFalse(self.widget.inventory_grid.isVisible())
        self.assertIn("Найдено 0", self.widget.result_count.text())

    def test_val_profile_inventory_and_catalog_kept_separate(self):
        self.widget.set_game("valorant")
        self.assertEqual(self.widget.stat_values[1].text(), "162")
        self.widget.set_collection("skins")
        self.widget.inventory_search.setText("reaver")
        self.assertEqual(self.widget.inventory_grid.count(), 1)
        self.widget.catalogs["valorant"] = {"skins": [{"id": "other-skin", "name": "Unowned", "kind": "catalog"}]}
        self.widget.mode.setCurrentIndex(1)
        self.assertEqual(self.widget.inventory_grid.item(0).text(), "Unowned")
        self.assertEqual(self.widget.stat_values[3].text(), "1")
        self.assertIn("не показывает", self.widget.status.text())
        self.widget.mode.setCurrentIndex(0)
        self.assertEqual(self.widget.inventory_grid.item(0).text(), "Жнец Vandal")

    def test_no_account_match_disables_account_actions(self):
        self.widget.account_search.setText("nothing-matches")
        self.assertIsNone(self.widget.current_account)
        self.assertFalse(self.widget.api_button.isEnabled())
        self.assertFalse(self.widget.code_button.isEnabled())

    def test_minimum_window_controls_fit(self):
        self.widget.session_info[self.widget.selected_key] = {"status": "ready", "expires_at": time.time() + 3600}
        self.widget.tick()
        self.widget.resize(900, 640)
        self.app.processEvents()
        self.assertLessEqual(self.widget.width(), 900)
        self.assertLessEqual(self.widget.height(), 640)
        for child in (self.widget.api_button, self.widget.client_button, self.widget.ranks_button,
                      *self.widget.rank_cards.values(),
                      self.widget.inventory_search, self.widget.mode, self.widget.stat_values[3]):
            point = child.mapTo(self.widget, child.rect().bottomRight())
            self.assertLess(point.x(), self.widget.width())
            self.assertLess(point.y(), self.widget.height())

    def test_session_countdown_distinguishes_token_expiry_from_sso_and_other_accounts(self):
        key = self.widget.selected_key
        self.widget.session_info[key] = {"status": "ready", "expires_at": 2000003600,
                                        "cookie_expires_at": 2000086400, "saved": True}
        with patch("app.ui.dashboard.time.time", return_value=2000000000):
            self.widget.tick()
        self.assertEqual(self.widget.session_label.text(), "QR-токен: 01:00:00 · автообновление")
        self.assertIn("Общий срок SSO-сессии Riot не сообщает", self.widget.session_label.toolTip())
        self.assertIn("DPAPI", self.widget.session_label.toolTip())
        self.assertIn("До истечения SSO-cookie", self.widget.session_label.toolTip())
        self.widget.account_search.setText("alt_account")
        self.assertEqual(self.widget.session_label.text(), "QR-сессия: войдите в Riot")
        self.widget.account_search.clear()
        self.widget.selected_key = key
        self.widget._filter_accounts()
        self.widget.session_info[key] = {"status": "login", "error": "Riot завершил сессию."}
        self.widget.tick()
        self.assertIn("нужен повторный вход", self.widget.session_label.text())

    def test_rank_cards_queue_values_visibility_and_busy_state(self):
        self.assertEqual(self.widget.rank_values["solo"].text(), "Платина II")
        self.assertEqual(self.widget.rank_notes["solo"].text(), "48 LP · EUW")
        self.assertEqual(self.widget.rank_values["flex"].text(), "Без ранга")
        self.assertEqual(self.widget.rank_values["tft"].text(), "Алмаз IV")
        self.assertTrue(self.widget.rank_strip.isVisible())
        self.assertIn("Обновлено", self.widget.rank_cards["tft"].toolTip())
        operations = []
        self.widget.refresh_requested.connect(operations.append)
        QTest.mouseClick(self.widget.ranks_button, Qt.MouseButton.LeftButton)
        self.assertEqual(operations, ["ranks"])
        self.widget.set_busy(self.widget.selected_key, "lol", True)
        self.assertFalse(self.widget.ranks_button.isEnabled())
        self.widget.set_busy(self.widget.selected_key, "lol", False)
        self.widget.set_game("valorant")
        self.assertFalse(self.widget.rank_strip.isVisible())
        self.assertFalse(self.widget.ranks_button.isVisible())
        self.widget.set_game("lol")
        self.widget.account_search.setText("alt_account")
        self.assertEqual(self.widget.rank_values["solo"].text(), "Не загружено")

    def test_official_rank_assets_and_distinct_tft_emblem_work_offline(self):
        for product in ("lol", "tft"):
            for tier in TIERS:
                asset = QPixmap(resource_path(f"app/assets/ranks/{product}-{tier}.png"))
                self.assertFalse(asset.isNull(), f"{product} {tier}")
        lol = rank_pixmap("solo", "PLATINUM", "ranked", 42)
        tft = rank_pixmap("tft", "PLATINUM", "ranked", 42)
        self.assertEqual(lol.devicePixelRatio(), 2)
        self.assertNotEqual(lol.toImage(), tft.toImage())
        expected = lol.scaled(42, 42, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
        self.assertEqual(self.widget.rank_icons["solo"].pixmap().toImage(), expected.toImage())
        self.assertNotEqual(rank_pixmap("solo", None, "unranked", 42).toImage(), lol.toImage())
        self.assertEqual(self.widget.artwork.active, 0)

    def test_mini_account_ranks_follow_game_filter_and_click_selection(self):
        item = self.widget.account_list.item(0)
        card = item.data(Qt.ItemDataRole.UserRole + 1)
        self.assertEqual(card["ranks"]["solo"]["tier"], "PLATINUM")
        self.assertIn("Solo / Duo: Платина II · 48 LP", item.toolTip())
        target = self.widget.account_list.item(1)
        QTest.mouseClick(self.widget.account_list.viewport(), Qt.MouseButton.LeftButton,
                         pos=self.widget.account_list.visualItemRect(target).center())
        self.assertEqual(self.widget.current_account["name"], "sage#313")
        self.widget.set_game("valorant")
        self.assertFalse(self.widget.account_list.item(0).data(Qt.ItemDataRole.UserRole + 1)["ranks"])
        self.widget.set_game("lol")
        self.widget.account_search.setText("night_login")
        self.assertEqual(self.widget.account_list.count(), 1)
        self.assertEqual(self.widget.account_list.item(0).data(Qt.ItemDataRole.UserRole + 1)["ranks"]["tft"]["tier"], "DIAMOND")

    def test_rank_worker_result_keeps_cached_rank_and_identity_on_failure(self):
        with patch("app.ui.main_window.FcmService"), patch("app.ui.main_window.save_accounts"):
            win = MainWindow(accounts=fixture_accounts(), start_services=False)
            account = win.accounts[0]
            key = account_key(account)
            win._on_game_data_result({"key": key, "game": "lol", "operation": "ranks", "lookup_name": account["name"],
                                     "data": {"ranks": {"solo": {"status": "unavailable", "error": "Ключ истёк"},
                                                         "tft": {"status": "unranked", "region": "EUW", "updated_at": 200}}}})
            self.assertEqual(account["puuid"], "owner-one")
            self.assertEqual(win.dashboard.rank_values["solo"].text(), "Платина II")
            self.assertIn("сохранено", win.dashboard.rank_notes["solo"].text())
            self.assertIn("Ключ истёк", win.dashboard.rank_cards["solo"].toolTip())
            self.assertEqual(win.dashboard.rank_values["tft"].text(), "Без ранга")
            win.timer.stop()
            win.deleteLater()
            self.app.processEvents()

    def test_rank_refresh_in_catalog_mode_uses_both_keys_without_login(self):
        with patch("app.ui.main_window.FcmService"), patch("app.ui.main_window.threading.Thread") as thread:
            win = MainWindow(accounts=fixture_accounts(), start_services=False)
            win.api_keys = {"lol": "league-key", "tft": "tft-key", "valorant": ""}
            win.dashboard.mode.setCurrentIndex(1)
            win._refresh_game_data("ranks")
            arguments = thread.call_args.kwargs["args"]
            self.assertEqual(arguments[3], "ranks")
            self.assertEqual(arguments[-2:], ("league-key", "tft-key"))
            self.assertIsNot(arguments[1], win.accounts[0])
            self.assertFalse(win.dashboard.ranks_button.isEnabled())
            thread.return_value.start.assert_called_once()
            win.timer.stop()
            win.deleteLater()
            self.app.processEvents()

    def test_fast_tab_changes_finish_fade_and_keep_search_responsive(self):
        self.widget.set_game("valorant")
        self.widget.set_collection("skins")
        self.widget.set_game("lol")
        self.widget.inventory_search.setFocus()
        QTest.keyClicks(self.widget.inventory_search, "JINX")
        self.assertEqual(self.widget.inventory_grid.item(0).text(), "Джинкс")
        self.assertEqual(self.app.focusWidget(), self.widget.inventory_search)
        QTest.qWait(250)
        self.assertEqual(self.widget._content_fade.effect.opacity(), 1.0)

    def test_tab_hover_stays_inside_selected_button(self):
        for button, border in [(self.widget.game_buttons["valorant"], "#69414a"),
                               (self.widget.collection_buttons["skins"], "#373b40")]:
            button.setChecked(True)
            button._hover_animation.stop()
            button.hoverAmount = 1.0
            self.app.processEvents()
            image = button.grab().toImage()
            edge = image.pixelColor(image.width() - 1, image.height() // 2)
            # The right edge is the selected tab's border, never the neutral
            # hover strip that used to be painted into its stylesheet margin.
            self.assertEqual(edge, QColor(border), button.objectName())

    def test_artwork_stays_with_item_after_sort_and_search(self):
        store = self.widget.artwork
        for identifier, color in [("103", "#ff0000"), ("222", "#00ff00")]:
            url = f"https://ddragon.leagueoflegends.com/cdn/test/{identifier}.png"
            pixmap = QPixmap(40, 40)
            pixmap.fill(QColor(color))
            store.sources["lol"][("characters", identifier)] = url
            store.images[url] = pixmap
        self.widget.inventory_grid.sortItems(Qt.SortOrder.DescendingOrder)
        self.widget.inventory_search.setText("JINX")
        index = self.widget.inventory_grid.model().index(0, 0)
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, self.widget.inventory_grid.gridSize().width(), self.widget.inventory_grid.gridSize().height())
        option.font = self.app.font()
        canvas = QPixmap(option.rect.size())
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        self.widget.inventory_grid.itemDelegate().paint(painter, option, index)
        painter.end()
        image = canvas.toImage()
        self.assertEqual(image.pixelColor(70, 60), QColor("#00ff00"))
        self.assertEqual(index.data(Qt.ItemDataRole.UserRole)["id"], "222")
        self.assertEqual(self.widget.artwork.active, 0)

    def test_bad_artwork_has_placeholder_and_never_contacts_unknown_hosts(self):
        self.assertTrue(public_asset_url("https://media.valorant-api.com/agents/icon.png"))
        for url in ["http://media.valorant-api.com/icon.png", "https://evil.example/icon.png",
                    "https://media.valorant-api.com.evil.example/icon.png",
                    "https://user:password@media.valorant-api.com/icon.png",
                    "https://media.valorant-api.com/icon.png?token=private", "file:///private.png"]:
            self.assertFalse(public_asset_url(url))
        self.widget.artwork._loaded("broken-image", b"not an image")
        self.assertIn("broken-image", self.widget.artwork.failed)
        placeholder = self.widget.artwork.thumbnail("lol", "characters", {"id": "unknown"}, QSize(36, 36))
        self.assertFalse(placeholder.isNull())
        self.assertEqual(self.widget.artwork.active, 0)

    def test_destroyed_artwork_reply_releases_slot_without_crashing(self):
        from PyQt6 import sip
        store = self.widget.artwork
        reply = QObject()
        url = "https://ddragon.leagueoflegends.com/cdn/test/image.png"
        store.replies[url] = reply
        store.pending.add(url)
        store.active = 1
        sip.delete(reply)
        callback = []
        store._progress(reply, 100, 100, 50)
        store._finished(reply, url, callback.append, 1000)
        self.assertEqual(store.active, 0)
        self.assertNotIn(url, store.pending)
        self.assertIn(url, store.failed)
        self.assertFalse(callback)

    def test_old_artwork_completion_does_not_consume_retried_request_slot(self):
        from PyQt6 import sip
        store = self.widget.artwork
        old, current = QObject(), QObject()
        url = "https://ddragon.leagueoflegends.com/cdn/test/image.png"
        sip.delete(old)
        store.replies[url] = current
        store.pending.add(url)
        store.active = 1
        store._finished(old, url, lambda body: self.fail("Old reply must be ignored"), 1000)
        self.assertEqual(store.active, 1)
        self.assertIn(url, store.pending)
        self.assertIs(store.replies[url], current)

    def test_league_skin_artwork_uses_skin_number_and_chroma_parent(self):
        result = league_artwork({"Ahri": {"key": "103", "skins": [
            {"id": "103027", "num": 27}, {"id": "103029", "num": 29, "parentSkin": 27}]}}, "16.20.1")
        self.assertTrue(result[("characters", "103")].endswith("/Ahri.png"))
        self.assertTrue(result[("skins", "103027")].endswith("/Ahri_27.jpg"))
        self.assertEqual(result[("skins", "103027")], result[("skins", "103029")])

    def test_wheel_scroll_is_smooth_and_accumulates(self):
        account = self.widget.current_account
        account["games"]["lol"]["characters"] = [
            {"id": str(i), "name": f"Champion {i:03}", "kind": "owned"} for i in range(100)]
        self.widget._render_detail()
        self.app.processEvents()
        bar = self.widget.inventory_grid.verticalScrollBar()
        viewport = self.widget.inventory_grid.viewport()
        for _ in range(2):
            event = QWheelEvent(QPointF(20, 20), QPointF(viewport.mapToGlobal(QPoint(20, 20))),
                                QPoint(), QPoint(0, -120), Qt.MouseButton.NoButton,
                                Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
            self.app.sendEvent(viewport, event)
        self.assertEqual(bar.value(), 0)
        QTest.qWait(220)
        self.assertEqual(bar.value(), 220)

    def test_grid_reflows_on_resize_without_horizontal_overflow(self):
        account = self.widget.current_account
        account["games"]["lol"]["characters"] = [
            {"id": str(i), "name": f"Champion {i:03}", "kind": "owned"} for i in range(40)]
        self.widget._render_detail()
        self.app.processEvents()
        grid = self.widget.inventory_grid
        self.assertEqual(grid.viewMode(), grid.ViewMode.IconMode)
        wide_columns = sum(grid.visualItemRect(grid.item(i)).top() == 0 for i in range(grid.count()))
        self.assertEqual(wide_columns, grid.viewport().width() // 148)
        self.widget.resize(900, 640)
        self.app.processEvents()
        narrow_columns = sum(grid.visualItemRect(grid.item(i)).top() == 0 for i in range(grid.count()))
        self.assertEqual(narrow_columns, grid.viewport().width() // 148)
        self.assertGreater(wide_columns, narrow_columns)
        self.assertGreaterEqual(narrow_columns, 2)
        for i in range(grid.count()):
            rect = grid.visualItemRect(grid.item(i))
            self.assertGreaterEqual(rect.left(), 0)
            self.assertLessEqual(rect.right(), grid.viewport().width())
        self.assertEqual(grid.horizontalScrollBar().maximum(), 0)

    def test_grid_refresh_keeps_scroll_and_selected_item_but_new_account_resets(self):
        account = self.widget.current_account
        account["games"]["lol"]["characters"] = [
            {"id": str(i), "name": f"Champion {i:03}", "kind": "owned"} for i in range(100)]
        self.widget._render_detail()
        self.app.processEvents()
        grid = self.widget.inventory_grid
        grid.setCurrentRow(45)
        grid.scrollToItem(grid.currentItem())
        self.app.processEvents()
        old_scroll = grid.verticalScrollBar().value()
        selected = grid.currentItem().data(Qt.ItemDataRole.UserRole)["id"]
        self.widget.set_busy(self.widget.selected_key, "lol", True)
        self.app.processEvents()
        self.assertEqual(grid.currentItem().data(Qt.ItemDataRole.UserRole)["id"], selected)
        self.assertEqual(grid.verticalScrollBar().value(), old_scroll)
        self.widget.account_search.setText("alt_account")
        self.app.processEvents()
        self.assertEqual(grid.count(), 0)
        self.assertEqual(grid.verticalScrollBar().value(), 0)

    def test_filtered_grid_opens_correct_details_by_double_click_and_keyboard(self):
        self.widget.inventory_search.setText("ahri")
        self.app.processEvents()
        grid = self.widget.inventory_grid
        point = grid.visualItemRect(grid.item(0)).center()
        # The viewport may be shorter than a full tile in small-window layouts.
        point.setY(min(point.y(), grid.viewport().height() - 5))
        from PyQt6.QtWidgets import QDialog, QListWidget
        opened = []
        def inspect(dialog):
            opened.append(dialog.windowTitle())
            self.assertEqual(dialog.windowTitle(), "Ари")
            details = dialog.findChild(QListWidget)
            self.assertEqual(details.item(0).text(), "Дух цветения Ари")
            return 0
        with patch.object(QDialog, "exec", inspect):
            QTest.mouseClick(grid.viewport(), Qt.MouseButton.LeftButton, pos=point)
            QTest.mouseDClick(grid.viewport(), Qt.MouseButton.LeftButton, pos=point)
            self.assertEqual(len(opened), 1)
            grid.setFocus()
            QTest.keyClick(grid, Qt.Key.Key_Return)
            self.assertEqual(len(opened), 2)

    def test_grid_skin_shapes_and_catalog_labels(self):
        grid = self.widget.inventory_grid
        self.widget.set_collection("skins")
        portrait = grid.gridSize()
        self.assertGreater(portrait.height(), portrait.width())
        self.widget.set_game("valorant")
        self.widget.set_collection("skins")
        self.assertGreater(grid.gridSize().width(), grid.gridSize().height())
        self.assertEqual(grid.item(0).data(Qt.ItemDataRole.UserRole + 1)["subtitle"], "Vandal")
        self.widget.catalogs["valorant"] = {"skins": [{"id": "other", "name": "Other", "kind": "catalog"}]}
        self.widget.mode.setCurrentIndex(1)
        self.assertIn("Каталог", grid.item(0).data(Qt.ItemDataRole.UserRole + 1)["subtitle"])

    def test_skin_type_is_visible_in_grid_and_search_without_changing_ownership(self):
        self.widget.skin_types["lol"] = {"103027": {"skin_type": "Легендарный", "skin_type_aliases": ["Legendary"]}}
        self.widget.set_collection("skins")
        self.widget.inventory_search.setText("легендарный")
        self.assertEqual(self.widget.inventory_grid.count(), 1)
        item = self.widget.inventory_grid.item(0)
        self.assertEqual(item.data(Qt.ItemDataRole.UserRole)["skin_type"], "Легендарный")
        self.assertIn("Легендарный", item.toolTip())
        self.assertEqual(item.data(Qt.ItemDataRole.UserRole)["kind"], "owned")
        self.assertNotIn("skin_type", self.widget.current_account["games"]["lol"]["skins"][0])

    def test_background_types_deduplicate_and_refresh_existing_skin_cards(self):
        win = MainWindow(accounts=fixture_accounts(), start_services=False)
        self.addCleanup(win.deleteLater)
        self.addCleanup(win.timer.stop)
        win._skin_types_auto_enabled = True
        win.dashboard.set_collection("skins")
        with patch("app.ui.main_window.threading.Thread") as thread:
            win._refresh_skin_types("lol")
            win._refresh_skin_types("lol")
            self.assertEqual(thread.call_count, 1)
            win._on_skin_types("lol", {"skins": {"103027": {"skin_type": "Легендарный"}}})
            self.assertEqual(win.dashboard.inventory_grid.item(0).data(Qt.ItemDataRole.UserRole)["skin_type"], "Легендарный")
            self.assertEqual(win._skin_type_jobs, set())
            win._on_skin_types("lol", {})
            self.assertEqual(win.dashboard.skin_types["lol"]["103027"]["skin_type"], "Легендарный")
            self.assertEqual(win._data_jobs, set())

    def test_async_result_does_not_change_selected_account_or_auth_puuid(self):
        with patch("app.ui.main_window.FcmService"), patch("app.ui.main_window.save_accounts") as save:
            win = MainWindow(accounts=fixture_accounts(), start_services=False)
            first = win.accounts[0]
            key = account_key(first)
            win.dashboard.selected_key = account_key(win.accounts[1])
            win._populate()
            win._on_game_data_result({"key": key, "game": "lol", "operation": "profile", "lookup_name": first["name"],
                                     "data": {"level": 250, "api_puuid": "encrypted", "region": "EUW"}})
            self.assertEqual(win.dashboard.current_account["name"], "sage#313")
            self.assertEqual(first["puuid"], "owner-one")
            self.assertEqual(first["games"]["lol"]["level"], 250)
            first["name"] = "renamed#RU"
            save.reset_mock()
            win._on_game_data_result({"key": key, "game": "lol", "operation": "profile", "lookup_name": "nightshift#EUW", "data": {"level": 999}})
            self.assertEqual(first["games"]["lol"]["level"], 250)
            save.assert_not_called()
            win.timer.stop()
            win.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
