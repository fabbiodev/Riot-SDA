"""Compact account browser with independent account and collection searches."""

import time

from PyQt6.QtCore import Qt, QSettings, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QFrame,
    QButtonGroup, QComboBox, QMenu,
    QApplication, QDialog, QDialogButtonBox, QFormLayout, QCheckBox,
)

from app.api.developer_api import LOL_PLATFORMS, VAL_PLATFORMS
from app.api.rankings import rank_text, rank_region
from app.core import get_code, PERIOD
from app.core.search import account_key, account_matches, item_matches
from app.core.skin_types import load_skin_types, enrich_skin
from app.core.sessions import remaining_text, timestamp
from app.ui.motion import AnimatedButton as QPushButton, ContentFade, SmoothScroll
from app.ui.artwork import ArtworkStore, ArtworkPreview
from app.ui.inventory_grid import InventoryGrid
from app.ui.rank_widgets import rank_pixmap, AccountDelegate


def plain_label(text="", object_name=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    if object_name:
        label.setObjectName(object_name)
    return label


class DataSettingsDialog(QDialog):
    def __init__(self, keys, account, hidden_codes, compact, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки игровых данных")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        title = plain_label("Игровые данные", "dialogTitle")
        layout.addWidget(title)
        form = QFormLayout()
        self.lol_key = QLineEdit(keys.get("lol", ""))
        self.tft_key = QLineEdit(keys.get("tft", ""))
        self.val_key = QLineEdit(keys.get("valorant", ""))
        for edit in (self.lol_key, self.tft_key, self.val_key):
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setPlaceholderText("RGAPI-…")
        form.addRow("Ключ League", self.lol_key)
        form.addRow("Ключ TFT", self.tft_key)
        form.addRow("Ключ VALORANT", self.val_key)
        self.lol_region = QComboBox()
        self.lol_region.addItems(list(LOL_PLATFORMS))
        self.val_region = QComboBox()
        self.val_region.addItems(list(VAL_PLATFORMS))
        routes = (account or {}).get("api_routes", {})
        self.lol_region.setCurrentText(rank_region(account or {}))
        self.val_region.setCurrentText(routes.get("valorant", "EU"))
        form.addRow("Сервер League / TFT", self.lol_region)
        form.addRow("Сервер VALORANT", self.val_region)
        self.hidden_codes = QCheckBox("Скрывать коды 2FA")
        self.hidden_codes.setChecked(hidden_codes)
        self.compact = QCheckBox("Компактные карточки")
        self.compact.setChecked(compact)
        form.addRow(self.hidden_codes)
        form.addRow(self.compact)
        layout.addLayout(form)
        note = plain_label("Solo / Duo и Flex: OP.GG, ключ не нужен. Обновление при запуске, после входа и каждый час. "
                           "Для ранга TFT нужен ключ Riot TFT API: OP.GG не публикует этот метод. "
                           "Ключи используются только в этом запуске и не сохраняются на диск. "
                           "Можно задать RIOT_API_KEY, RIOT_TFT_API_KEY и RIOT_VALORANT_API_KEY. "
                           "Для VALORANT нужен ключ с доступом к его API.", "mutedLabel")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class Dashboard(QWidget):
    action_requested = pyqtSignal(str)
    refresh_requested = pyqtSignal(str)
    copied = pyqtSignal()

    def __init__(self, parent=None, load_artwork=True):
        super().__init__(parent)
        self.setObjectName("dashboard")
        self.accounts = []
        self.selected_key = None
        self.game = "lol"
        self.collection = "characters"
        self.rows = []
        self.busy = set()
        self.errors = {}
        self.catalogs = {}
        self.skin_types = {game: load_skin_types(game).get("skins", {}) for game in ("lol", "valorant")}
        self.session_info = {}
        self.client_session_info = {}
        self.client_session_busy = False
        self._view_key = None
        self.settings = QSettings("RiotAuthLocal", "Desktop")
        self.hidden_codes = self.settings.value("hidden_codes", True, type=bool)
        self.compact = self.settings.value("compact", False, type=bool)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        titlebar = QFrame()
        titlebar.setObjectName("titlebar")
        title_layout = QHBoxLayout(titlebar)
        title_layout.setContentsMargins(20, 12, 20, 12)
        title_layout.addWidget(plain_label("Riot Auth", "titleLabel"))
        title_layout.addStretch()
        outer.addWidget(titlebar)

        shell = QHBoxLayout()
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(245)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(14, 22, 14, 14)
        side.setSpacing(12)
        self.account_count = plain_label("Аккаунты", "mutedLabel")
        side.addWidget(self.account_count)
        self.account_search = QLineEdit()
        self.account_search.setObjectName("accountSearch")
        self.account_search.setPlaceholderText("Ник или логин")
        self.account_search.setAccessibleName("Поиск аккаунтов по нику или логину")
        self.account_search.setClearButtonEnabled(True)
        self.account_search.textChanged.connect(self._filter_accounts)
        side.addWidget(self.account_search)
        self.account_list = QListWidget()
        self.account_list.setObjectName("accountList")
        self.account_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.account_list.setItemDelegate(AccountDelegate(self.account_list))
        self.account_list.currentItemChanged.connect(self._select_account)
        side.addWidget(self.account_list, 1)
        self.no_accounts = plain_label("", "mutedLabel")
        self.no_accounts.setWordWrap(True)
        side.addWidget(self.no_accounts)
        account_actions = QHBoxLayout()
        account_actions.addWidget(self._action_button("Добавить", "add"))
        account_actions.addWidget(self._action_button("Импорт", "import"))
        side.addLayout(account_actions)
        self.requests_button = self._action_button("Запросы на вход", "requests")
        side.addWidget(self.requests_button)
        side.addWidget(self._action_button("Настройки API", "settings"))
        shell.addWidget(sidebar)

        self.detail = QWidget()
        main = QVBoxLayout(self.detail)
        main.setContentsMargins(28, 16, 28, 12)
        main.setSpacing(8)
        profile_header = QHBoxLayout()
        identity = QVBoxLayout()
        identity.setSpacing(3)
        self.session_label = plain_label("Riot ID", "mutedLabel")
        identity.addWidget(self.session_label)
        self.riot_id = plain_label("Добавьте аккаунт", "riotId")
        identity.addWidget(self.riot_id)
        self.login_label = plain_label("", "mutedLabel")
        identity.addWidget(self.login_label)
        profile_header.addLayout(identity, 1)
        self.launch_button = self._action_button("Riot Client", "client-launch")
        self.launch_button.setToolTip("Запустить Riot Client с выбранным аккаунтом без QR")
        profile_header.addWidget(self.launch_button)
        self.qr_button = self._action_button("Вход по QR", "qr")
        profile_header.addWidget(self.qr_button)
        self.menu_button = QPushButton("···")
        self.menu_button.setAccessibleName("Действия с аккаунтом")
        self.menu_button.setObjectName("accountMenu")
        self.menu_button.setFixedWidth(36)
        menu = QMenu(self.menu_button)
        menu.addAction("Обновить вход для Riot Client…", lambda: self.action_requested.emit("client-login"))
        menu.addSeparator()
        self.session_refresh_action = menu.addAction("Обновить QR-сессию", lambda: self.action_requested.emit("session-refresh"))
        self.session_login_action = menu.addAction("Войти заново для QR…", lambda: self.action_requested.emit("session-login"))
        self.session_forget_action = menu.addAction("Забыть QR-сессию", lambda: self.action_requested.emit("session-forget"))
        menu.addSeparator()
        self.connect_action = menu.addAction("Подключить 2FA…", lambda: self.action_requested.emit("connect-2fa"))
        self.share_action = None
        for label, action in [("Логин для поиска…", "edit-login"), ("Riot ID для API…", "edit-riot-id"),
                              ("Экспорт данных доступа…", "share"), ("Удалить из приложения…", "remove")]:
            menu_action = menu.addAction(label, lambda checked=False, a=action: self.action_requested.emit(a))
            if action == "share":
                self.share_action = menu_action
        self.menu_button.setMenu(menu)
        profile_header.addWidget(self.menu_button)
        main.addLayout(profile_header)

        auth = QHBoxLayout()
        self.auth_caption = plain_label("Код 2FA", "mutedLabel")
        auth.addWidget(self.auth_caption)
        self.auth_note = plain_label("Не подключён", "mutedLabel")
        auth.addWidget(self.auth_note)
        self.connect_button = self._action_button("Подключить 2FA", "connect-2fa")
        auth.addWidget(self.connect_button)
        self.code_button = QPushButton("••• •••")
        self.code_button.setObjectName("codeButton")
        self.code_button.setAccessibleName("Скопировать код 2FA")
        self.code_button.clicked.connect(self.copy_code)
        auth.addWidget(self.code_button)
        self.visibility_button = QPushButton("Показать")
        self.visibility_button.setObjectName("textButton")
        self.visibility_button.clicked.connect(self._toggle_code)
        auth.addWidget(self.visibility_button)
        auth.addStretch()
        self.timer_label = plain_label("", "mutedLabel")
        auth.addWidget(self.timer_label)
        main.addLayout(auth)
        main.addWidget(self._line())

        game_row = QHBoxLayout()
        game_row.setSpacing(14)
        self.game_buttons = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for label, game in [("League of Legends", "lol"), ("VALORANT", "valorant")]:
            button = QPushButton(label)
            button.setObjectName("gameTab")
            button.setCheckable(True)
            button.setChecked(game == self.game)
            button.clicked.connect(lambda checked=False, g=game: self.set_game(g))
            group.addButton(button)
            game_row.addWidget(button)
            self.game_buttons[game] = button
        game_row.addStretch()
        main.addLayout(game_row)

        stats = QHBoxLayout()
        self.stat_labels, self.stat_values = [], []
        for caption in ["Регион", "Уровень аккаунта", "Открытые чемпионы", "Скины аккаунта"]:
            box = QVBoxLayout()
            box.setSpacing(6)
            label, value = plain_label(caption, "mutedLabel"), plain_label("—", "statValue")
            box.addWidget(label)
            box.addWidget(value)
            stats.addLayout(box, 1)
            self.stat_labels.append(label)
            self.stat_values.append(value)
        main.addLayout(stats)

        self.rank_strip = QWidget()
        rank_row = QHBoxLayout(self.rank_strip)
        rank_row.setContentsMargins(0, 0, 0, 0)
        rank_row.setSpacing(10)
        self.rank_values, self.rank_notes, self.rank_cards, self.rank_icons = {}, {}, {}, {}
        for key, caption in [("solo", "Solo / Duo"), ("flex", "Flex"), ("tft", "TFT Ranked")]:
            card = QFrame()
            card.setObjectName("rankCard")
            content = QHBoxLayout(card)
            content.setContentsMargins(10, 9, 10, 9)
            content.setSpacing(9)
            icon = plain_label()
            icon.setFixedSize(42, 42)
            icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            content.addWidget(icon)
            box = QVBoxLayout()
            box.setSpacing(3)
            box.addWidget(plain_label(caption, "mutedLabel"))
            value, note = plain_label("Не загружено", "rankValue"), plain_label("", "mutedLabel")
            box.addWidget(value)
            box.addWidget(note)
            content.addLayout(box, 1)
            rank_row.addWidget(card, 1)
            self.rank_values[key], self.rank_notes[key], self.rank_cards[key] = value, note, card
            self.rank_icons[key] = icon
        main.addWidget(self.rank_strip)

        updates = QHBoxLayout()
        self.api_button = QPushButton("Профиль из Riot API")
        self.api_button.clicked.connect(lambda: self.refresh_requested.emit("profile"))
        self.client_button = QPushButton("Коллекция из клиента")
        self.client_button.clicked.connect(lambda: self.refresh_requested.emit("collection"))
        self.ranks_button = QPushButton("Обновить ранги")
        self.ranks_button.setToolTip("Solo / Duo и Flex — OP.GG · TFT — Riot API\n"
                                     "Автоматически при запуске, после входа и каждый час, в том числе в трее.\n"
                                     "Сервер аккаунта задаётся в настройках API. OP.GG может отдавать кэшированные данные.")
        self.ranks_button.clicked.connect(lambda: self.refresh_requested.emit("ranks"))
        updates.addWidget(self.api_button)
        updates.addWidget(self.client_button)
        updates.addWidget(self.ranks_button)
        updates.addStretch()
        main.addLayout(updates)
        self.status = plain_label("", "mutedLabel")
        self.status.setWordWrap(True)
        main.addWidget(self.status)

        collection_bar = QHBoxLayout()
        collection_bar.setSpacing(12)
        self.collection_buttons = {}
        collection_group = QButtonGroup(self)
        collection_group.setExclusive(True)
        for label, key in [("Чемпионы", "characters"), ("Скины", "skins")]:
            button = QPushButton(label)
            button.setObjectName("collectionTab")
            button.setCheckable(True)
            button.setChecked(key == self.collection)
            button.clicked.connect(lambda checked=False, k=key: self.set_collection(k))
            collection_group.addButton(button)
            collection_bar.addWidget(button)
            self.collection_buttons[key] = button
        collection_bar.addStretch()
        self.mode = QComboBox()
        self.mode.addItem("Моя коллекция", "owned")
        self.mode.addItem("Каталог игры", "catalog")
        self.mode.setAccessibleName("Источник списка персонажей и скинов")
        self.mode.currentIndexChanged.connect(self._mode_changed)
        collection_bar.addWidget(self.mode)
        main.addLayout(collection_bar)

        self.inventory_search = QLineEdit()
        self.inventory_search.setObjectName("inventorySearch")
        self.inventory_search.setPlaceholderText("Поиск по чемпионам")
        self.inventory_search.setAccessibleName("Поиск по персонажам и скинам")
        self.inventory_search.setClearButtonEnabled(True)
        self.inventory_search.textChanged.connect(self._render_grid)
        main.addWidget(self.inventory_search)
        self.artwork = ArtworkStore(self, enabled=load_artwork)
        self.refresh_requested.connect(lambda operation: self.artwork.retry())
        self.inventory_grid = InventoryGrid(self.artwork, self)
        self.inventory_grid.itemActivated.connect(self._show_item)
        self._inventory_context = None
        self._scroll_motion = [SmoothScroll(self.inventory_grid), SmoothScroll(self.account_list)]
        main.addWidget(self.inventory_grid, 1)
        self.empty_collection = plain_label("", "mutedLabel")
        self.empty_collection.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_collection.setWordWrap(True)
        main.addWidget(self.empty_collection, 1)
        bottom = QHBoxLayout()
        self.result_count = plain_label("", "mutedLabel")
        bottom.addWidget(self.result_count)
        bottom.addStretch()
        self.updated_label = plain_label("", "mutedLabel")
        bottom.addWidget(self.updated_label)
        main.addLayout(bottom)
        shell.addWidget(self.detail, 1)
        outer.addLayout(shell, 1)
        footer = plain_label("Данные аккаунтов хранятся локально. Коллекции обновляются по запросу.", "footer")
        outer.addWidget(footer)
        self._content_fade = ContentFade(self.detail)
        self._render_detail()

    @staticmethod
    def _line():
        line = QFrame()
        line.setObjectName("divider")
        line.setFixedHeight(1)
        return line

    def _action_button(self, label, action):
        button = QPushButton(label)
        button.clicked.connect(lambda checked=False: self.action_requested.emit(action))
        return button

    @property
    def current_account(self):
        return next((a for a in self.accounts if account_key(a) == self.selected_key), None)

    def set_accounts(self, accounts):
        self.accounts = accounts
        self._filter_accounts()

    def _filter_accounts(self):
        query = self.account_search.text()
        filtered = [a for a in self.accounts if account_matches(a, query)]
        self.account_list.blockSignals(True)
        self.account_list.clear()
        target = None
        for account in filtered:
            profile = account.get("games", {}).get(self.game, {})
            meta = " · ".join(str(x) for x in [profile.get("region"),
                               f"уровень {profile['level']}" if profile.get("level") is not None else ""] if x)
            login = account.get("login", "")
            text = account.get("name", "Аккаунт") + "\n" + (login or meta or "Данные ещё не загружены")
            item = QListWidgetItem(text)
            key = account_key(account)
            item.setData(Qt.ItemDataRole.UserRole, key)
            ranks = profile.get("ranks", {}) if self.game == "lol" else {}
            item.setData(Qt.ItemDataRole.UserRole + 1, {"name": account.get("name", "Аккаунт"),
                         "subtitle": login or meta or "Данные ещё не загружены", "ranks": ranks})
            tooltip = account.get("name", "") + (f"\nЛогин: {login}" if login else "") + ("\n" + meta if meta else "")
            for queue, caption in (("solo", "Solo / Duo"), ("flex", "Flex"), ("tft", "TFT Ranked")):
                if ranks:
                    rank = ranks.get(queue, {})
                    tooltip += "\n" + caption + ": " + rank_text(rank)
                    if rank.get("status") == "ranked":
                        tooltip += f" · {rank['lp']} LP"
                    if rank.get("refresh_error") or rank.get("error"):
                        tooltip += " — " + (rank.get("refresh_error") or rank["error"])
            item.setToolTip(tooltip)
            self.account_list.addItem(item)
            if key == self.selected_key:
                target = item
        if target is None and self.account_list.count():
            target = self.account_list.item(0)
        self.account_list.setCurrentItem(target)
        self.selected_key = target.data(Qt.ItemDataRole.UserRole) if target else None
        self.account_list.blockSignals(False)
        self.account_count.setText(f"Аккаунты · {len(filtered)} / {len(self.accounts)}" if query else f"Аккаунты · {len(self.accounts)}")
        self.no_accounts.setText("Ничего не найдено" if query else "Добавьте аккаунт через вход Riot или импорт.")
        self.no_accounts.setVisible(not filtered)
        self._render_detail()

    def _select_account(self, item, previous):
        self.selected_key = item.data(Qt.ItemDataRole.UserRole) if item else None
        self._render_detail()

    def set_game(self, game):
        self.game = game
        self.game_buttons[game].setChecked(True)
        self.collection = "characters"
        self.collection_buttons["characters"].setChecked(True)
        self.inventory_search.clear()
        self._filter_accounts()

    def set_collection(self, collection):
        self.collection = collection
        self.collection_buttons[collection].setChecked(True)
        self.inventory_search.clear()
        self._render_detail()

    def _mode_changed(self):
        self.inventory_search.clear()
        self._render_detail()

    def _render_detail(self):
        account = self.current_account
        p = (account or {}).get("games", {}).get(self.game, {})
        catalog_mode = self.mode.currentData() == "catalog"
        catalog = self.catalogs.get(self.game, {})
        self.riot_id.setText(p.get("riot_id") or (account or {}).get("name") or "Выберите аккаунт")
        self.login_label.setText("Логин: " + account["login"] if account and account.get("login") else "")
        self.login_label.setVisible(bool(self.login_label.text()))
        self.stat_labels[2].setText("Открытые чемпионы" if self.game == "lol" else "Открытые агенты")
        self.stat_labels[3].setText("Скины аккаунта" if self.game == "lol" else "Скины оружия")
        values = [p.get("region"), p.get("level"),
                  len(p["characters"]) if "characters" in p else None,
                  len(p["skins"]) if "skins" in p else None]
        for widget, value in zip(self.stat_values, values):
            widget.setText(str(value) if value is not None else "—")
        self.rank_strip.setVisible(self.game == "lol")
        self.ranks_button.setVisible(self.game == "lol")
        for queue, value in self.rank_values.items():
            rank = p.get("ranks", {}).get(queue, {})
            self.rank_icons[queue].setPixmap(rank_pixmap(queue, rank.get("tier"), rank.get("status"), 42))
            value.setText(rank_text(rank))
            note = f"{rank['lp']} LP" if rank.get("status") == "ranked" else ""
            error = rank.get("refresh_error") or rank.get("error")
            if rank.get("region"):
                note = (note + " · " + rank["region"]).strip(" ·")
            if rank.get("refresh_error"):
                note += " · сохранено"
            self.rank_notes[queue].setText(note)
            tooltip = "Источник: " + rank.get("source", "Riot Developer API" if rank else
                                              ("Riot Developer API" if queue == "tft" else "OP.GG"))
            if rank.get("updated_at"):
                tooltip += time.strftime("\nОбновлено в приложении %d.%m.%Y %H:%M", time.localtime(rank["updated_at"]))
            if rank.get("source_updated_at"):
                tooltip += time.strftime("\nДанные OP.GG от %d.%m.%Y %H:%M", time.localtime(rank["source_updated_at"]))
            elif rank.get("source") == "OP.GG":
                tooltip += "\nOP.GG не сообщил время обновления своего кэша."
            if error:
                tooltip += "\n" + error
            self.rank_cards[queue].setToolTip(tooltip)
        characters = "Чемпионы" if self.game == "lol" else "Агенты"
        skins = "Скины" if self.game == "lol" else "Скины оружия"
        self.collection_buttons["characters"].setText(characters)
        self.collection_buttons["skins"].setText(skins)
        self.inventory_search.setPlaceholderText("Поиск по " +
            ("чемпионам" if self.game == "lol" else "агентам") if self.collection == "characters"
            else "Скин, чемпион или оружие")
        for widget in (self.code_button, self.visibility_button, self.qr_button, self.menu_button,
                       self.api_button, self.client_button):
            widget.setEnabled(account is not None)
        has_2fa = bool(account and account.get("seed"))
        self.code_button.setEnabled(has_2fa)
        self.visibility_button.setEnabled(has_2fa)
        self.code_button.setVisible(has_2fa)
        self.visibility_button.setVisible(has_2fa)
        self.timer_label.setVisible(has_2fa)
        self.qr_button.setVisible(True)
        self.auth_caption.setText("Код 2FA" if has_2fa else "Аутентификатор")
        self.auth_note.setVisible(not has_2fa)
        self.connect_button.setVisible(bool(account) and not has_2fa)
        self.connect_action.setEnabled(bool(account) and not has_2fa)
        self.share_action.setEnabled(has_2fa)
        key = (self.selected_key, self.game)
        self.api_button.setEnabled(bool(account) and key not in self.busy)
        self.client_button.setEnabled(bool(account) and key not in self.busy)
        self.ranks_button.setEnabled(bool(account) and self.game == "lol" and key not in self.busy)
        self.api_button.setText("Загрузить каталог Riot API" if catalog_mode else "Профиль из Riot API")
        error = self.errors.get(key)
        if key in self.busy:
            status = "Загрузка данных…"
        elif error:
            status = error
        elif catalog_mode:
            status = "Каталог всех объектов игры. Он не показывает, чем владеет аккаунт."
        elif "characters" in p:
            status = "Коллекция: " + p.get("collection_source", "локальные данные")
            if self.game == "valorant":
                status += " · названия: valorant-api.com"
        elif account:
            status = (p.get("profile_note") or "Профиль: Riot Developer API.") if p.get("profile_updated_at") else "Обновите профиль через Riot API."
            status += " Для своей коллекции войдите в этот аккаунт в игре."
        else:
            status = "Добавьте аккаунт или измените поиск слева."
        self.status.setText(status)
        self.status.setProperty("error", bool(error))
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)
        self.rows = (catalog if catalog_mode else p).get(self.collection, [])
        self.update_client_status()
        updated = (catalog.get("updated_at") if catalog_mode else p.get("collection_updated_at")) or p.get("profile_updated_at")
        self.updated_label.setText(time.strftime("Обновлено %d.%m %H:%M", time.localtime(updated)) if updated else "")
        self._render_grid()
        self.tick()
        view_key = (self.selected_key, self.game, self.collection, self.mode.currentData())
        if self._view_key is not None and view_key != self._view_key:
            self._content_fade.start()
        self._view_key = view_key

    def update_client_status(self):
        account = self.current_account
        status = self.client_session_info.get(self.selected_key, {})
        self.launch_button.setToolTip("Запустить Riot Client с выбранным аккаунтом без QR\n" +
                                      status.get("text", "Сессия создаётся из сохранённого входа Riot SDA"))
        self.launch_button.setEnabled(bool(account and account.get("puuid")) and not self.client_session_busy)
        self.launch_button.setText("Запуск…" if status.get("status") == "switching" else "Riot Client")

    def _render_grid(self):
        query = self.inventory_search.text()
        is_character = self.collection == "characters"
        rows = self.rows if is_character else [enrich_skin(row, self.skin_types[self.game]) for row in self.rows]
        filtered = [r for r in rows if item_matches(r, query)]
        catalog_mode = self.mode.currentData() == "catalog"
        grid = self.inventory_grid
        context = (self.selected_key, self.game, self.collection, catalog_mode, query)
        keep_position = context == self._inventory_context
        selected = grid.currentItem()
        selected_id = selected.data(Qt.ItemDataRole.UserRole).get("id") if selected and keep_position else None
        scroll = grid.verticalScrollBar().value() if keep_position else 0
        grid.setUpdatesEnabled(False)
        grid.clear()
        grid.set_context(self.game, self.collection, self.compact)
        for row in sorted(filtered, key=lambda r: str(r.get("name", "")).casefold()):
            info = [row.get("role", "")] if is_character else [row.get("owner", "")]
            if is_character and self.game == "lol" and not catalog_mode:
                info.append(f"Скины: {len(row.get('skin_ids', []))}")
            if catalog_mode:
                info.insert(0, "Каталог")
            subtitle = " · ".join(str(x) for x in info if x)
            item = QListWidgetItem(str(row.get("name", "")))
            item.setData(Qt.ItemDataRole.UserRole, row)
            item.setData(Qt.ItemDataRole.UserRole + 1, {"game": self.game, "collection": self.collection,
                                                      "subtitle": subtitle})
            item.setToolTip("\n".join(str(x) for x in [row.get("name", ""), subtitle,
                row.get("skin_type", ""), *row.get("aliases", [])] if x))
            grid.addItem(item)
            if selected_id is not None and row.get("id") == selected_id:
                grid.setCurrentItem(item)
        grid.setVisible(bool(filtered))
        grid.doItemsLayout()
        grid.verticalScrollBar().setValue(scroll)
        grid.setUpdatesEnabled(True)
        self._inventory_context = context
        self.empty_collection.setVisible(not filtered)
        self.empty_collection.setText("Ничего не найдено. Измените запрос." if query else
            ("Коллекция пуста." if self.current_account and self.collection in self.current_account.get("games", {}).get(self.game, {}) and not catalog_mode
             else "Данные ещё не загружены."))
        self.result_count.setText(f"Найдено {len(filtered)} из {len(self.rows)}" if query else f"Всего: {len(self.rows)}" if self.rows else "")

    def _show_item(self, item):
        if item is None:
            return
        row = item.data(Qt.ItemDataRole.UserRole)
        dialog = QDialog(self)
        dialog.setWindowTitle(row.get("name", ""))
        dialog.resize(460, 340)
        layout = QVBoxLayout(dialog)
        layout.addWidget(plain_label(row.get("name", ""), "dialogTitle"))
        layout.addWidget(ArtworkPreview(self.artwork, self.game, self.collection, row, dialog))
        account = self.current_account or {}
        p = account.get("games", {}).get(self.game, {})
        if row.get("kind") == "catalog":
            details = ["Каталог игры · владение не определяется", row.get("id", "")]
        elif self.collection == "characters" and self.game == "lol":
            ids = set(row.get("skin_ids", []))
            details = [s["name"] for s in p.get("skins", []) if s.get("id") in ids]
            details = details or ["Для этого чемпиона нет приобретённых скинов."]
        elif self.collection == "characters":
            details = [row.get("role", ""), "Агент открыт"]
        else:
            details = [row.get("owner", ""), *row.get("variants", [])]
        if self.collection == "skins":
            details.insert(0, f"Тип: {row.get('skin_type', 'Тип не указан')}")
        values = QListWidget()
        values.addItems([str(x) for x in details if x])
        layout.addWidget(values)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def apply_preferences(self, hidden_codes, compact):
        self.hidden_codes, self.compact = hidden_codes, compact
        self.settings.setValue("hidden_codes", hidden_codes)
        self.settings.setValue("compact", compact)
        self._render_grid()
        self.tick()

    def _toggle_code(self):
        self.hidden_codes = not self.hidden_codes
        self.tick()

    def tick(self):
        account = self.current_account
        key = account_key(account) if account else None
        session = self.session_info.get(key, {})
        now = time.time()
        expiry = timestamp(session.get("expires_at"))
        state = session.get("status")
        if not account:
            caption = "Riot ID"
        elif state == "refreshing":
            caption = "QR-сессия: обновление…"
        elif state == "login":
            caption = "QR-сессия: нужен повторный вход"
        elif state == "forgotten" or not session:
            caption = "QR-сессия: войдите в Riot"
        elif expiry and expiry > now:
            caption = "QR-токен: " + remaining_text(expiry - now)
            caption += (" · повтор обновления" if state == "retry" else
                        " · продление требует входа" if state == "token_only" else " · автообновление")
        elif state == "token_only":
            caption = "QR-сессия: нужен повторный вход"
        elif state == "retry":
            caption = "QR-сессия: ожидание обновления"
        else:
            caption = "QR-сессия: подготовка…"
        self.session_label.setText(caption)
        details = ["Таймер показывает срок текущего access token из ответа Riot.",
                   "Новые токены обновляются автоматически, пока действительна сессия SSO.",
                   "Общий срок SSO-сессии Riot не сообщает; он может завершить её раньше."]
        cookie_expiry = timestamp(session.get("cookie_expires_at"))
        if cookie_expiry:
            details.append("SSO-cookie: до " + time.strftime("%d.%m.%Y %H:%M", time.localtime(cookie_expiry))
                           + " (не гарантия срока сессии).")
            details.append("До истечения SSO-cookie: " + remaining_text(cookie_expiry - now))
        if timestamp(session.get("last_refresh")):
            details.append(time.strftime("Последнее обновление: %d.%m %H:%M:%S", time.localtime(session["last_refresh"])))
        if timestamp(session.get("next_attempt")) and state == "retry":
            details.append("Следующая попытка через " + remaining_text(session["next_attempt"] - now))
        if state == "forgotten":
            details.append("Локальная QR-сессия удалена.")
        elif session:
            details.append("Сохранена с защитой Windows DPAPI." if session.get("saved") else "Доступна только в этом запуске.")
        for field in ("error", "storage_error"):
            if session.get(field):
                details.append(session[field])
        self.session_label.setToolTip("\n".join(details))
        self.session_refresh_action.setEnabled(bool(account) and state != "refreshing")
        self.session_login_action.setEnabled(bool(account))
        self.session_forget_action.setEnabled(bool(session) and state != "forgotten")
        code = "••• •••"
        if account and account.get("seed") and not self.hidden_codes:
            try:
                digits = get_code(account["seed"])
                code = digits[:3] + " " + digits[3:]
            except Exception:
                code = "Ошибка"
        self.code_button.setText(code)
        self.visibility_button.setText("Показать" if self.hidden_codes else "Скрыть")
        self.timer_label.setText(f"{PERIOD - int(time.time() % PERIOD)} с" if account and account.get("seed") else "")

    def copy_code(self):
        if self.current_account and self.current_account.get("seed"):
            try:
                QApplication.clipboard().setText(get_code(self.current_account["seed"]))
                self.copied.emit()
            except Exception:
                self.status.setText("Не удалось создать код: проверьте секрет 2FA.")

    def set_busy(self, key, game, busy):
        token = (key, game)
        if busy:
            self.busy.add(token)
            self.errors.pop(token, None)
        else:
            self.busy.discard(token)
        self._render_detail()

    def set_error(self, key, game, message):
        self.errors[(key, game)] = message
        self._render_detail()

    def update_requests(self, count):
        self.requests_button.setText(f"Запросы на вход · {count}" if count else "Запросы на вход")
