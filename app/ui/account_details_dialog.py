"""Personal account details in a compact, source-labelled window."""

from datetime import date, datetime

from PyQt6.QtCore import Qt, QLocale
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QFrame,
                             QLabel, QCheckBox, QFormLayout, QLineEdit, QMessageBox, QTabWidget,
                             QWidget, QScrollArea)

from app.core.account_details import clean_manual
from app.core.search import account_key
from app.ui.motion import AnimatedButton


def label(text, name="mutedLabel"):
    result = QLabel(text)
    result.setObjectName(name)
    result.setTextFormat(Qt.TextFormat.PlainText)
    result.setWordWrap(True)
    return result


def date_text(value):
    try:
        return date.fromisoformat(value).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return "Riot не предоставил"


def country_text(value):
    if not value:
        return "Riot не предоставил"
    alpha2 = {"RUS": "RU", "USA": "US", "GBR": "GB", "DEU": "DE", "FRA": "FR", "ESP": "ES",
              "ITA": "IT", "POL": "PL", "UKR": "UA", "BLR": "BY", "KAZ": "KZ", "TUR": "TR",
              "CAN": "CA", "BRA": "BR", "MEX": "MX", "ARG": "AR", "CHL": "CL", "AUS": "AU",
              "JPN": "JP", "KOR": "KR", "CHN": "CN", "VNM": "VN", "IND": "IN", "NLD": "NL",
              "SWE": "SE", "NOR": "NO", "FIN": "FI", "DNK": "DK", "CZE": "CZ", "PRT": "PT"}.get(value, value)
    if len(alpha2) == 2:
        territory = QLocale.codeToTerritory(alpha2)
        if territory != QLocale.Country.AnyCountry:
            name = {"RU": "Россия", "US": "США", "GB": "Великобритания", "DE": "Германия", "FR": "Франция",
                    "ES": "Испания", "IT": "Италия", "PL": "Польша", "UA": "Украина", "BY": "Беларусь",
                    "KZ": "Казахстан", "TR": "Турция", "CA": "Канада", "BR": "Бразилия", "MX": "Мексика",
                    "AR": "Аргентина", "CL": "Чили", "AU": "Австралия", "JP": "Япония", "KR": "Южная Корея",
                    "CN": "Китай", "VN": "Вьетнам", "IN": "Индия", "NL": "Нидерланды", "SE": "Швеция",
                    "NO": "Норвегия", "FI": "Финляндия", "DK": "Дания", "CZ": "Чехия", "PT": "Португалия"}.get(alpha2)
            return (name or QLocale.territoryToString(territory)) + " · " + value
    return value


class ManualDetailsDialog(QDialog):
    def __init__(self, record, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Дополнить сведения")
        self.setMinimumWidth(440)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(14)
        root.addWidget(label("Личные заметки", "dialogTitle"))
        root.addWidget(label("Сведения сохраняются только в Riot SDA с пометкой «Указано вручную». "
                             "Введите даты в формате ГГГГ-ММ-ДД; неизвестные поля оставьте пустыми."))
        manual = record.get("manual", {})
        self.fields = {}
        form = QFormLayout()
        form.setVerticalSpacing(12)
        for key, caption, placeholder in (("phone", "Номер телефона", "+7 …"),
                                           ("registration_country", "Страна регистрации", "Например, Россия"),
                                           ("registered", "Дата регистрации", "ГГГГ-ММ-ДД"),
                                           ("birthday", "День рождения", "ГГГГ-ММ-ДД")):
            edit = QLineEdit(str(manual.get(key) or ""))
            edit.setPlaceholderText(placeholder)
            edit.setMaxLength(80 if key == "registration_country" else 40)
            self.fields[key] = edit
            form.addRow(caption, edit)
        self.approximate = QCheckBox("Дата регистрации приблизительная")
        self.approximate.setChecked(manual.get("registered_approximate", True))
        form.addRow(self.approximate)
        root.addLayout(form)
        root.addWidget(label("Полученные от Riot даты имеют приоритет над заметками. "
                             "Номер в заметке не подтверждает его привязку к аккаунту."))
        row = QHBoxLayout()
        row.addStretch()
        cancel = AnimatedButton("Отмена")
        cancel.clicked.connect(self.reject)
        save = AnimatedButton("Сохранить")
        save.setObjectName("dialogAddBtn")
        save.clicked.connect(self._save)
        row.addWidget(cancel)
        row.addWidget(save)
        root.addLayout(row)
        self.values = None

    def _save(self):
        values = {k: edit.text().strip() for k, edit in self.fields.items()}
        values["registered_approximate"] = self.approximate.isChecked()
        try:
            self.values = clean_manual(values)
        except ValueError as exc:
            QMessageBox.warning(self, "Проверьте сведения", str(exc))
            return
        self.accept()


class AccountDetailsDialog(QDialog):
    def __init__(self, account, manager, parent=None):
        super().__init__(parent)
        self.account, self.manager = dict(account), manager
        self.setWindowTitle("Об аккаунте")
        self.setMinimumWidth(600)
        self.resize(640, 660)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(16)
        root.addWidget(label("Об аккаунте", "dialogTitle"))
        root.addWidget(label(str(account.get("name", "Аккаунт")), "accountDetailsName"))
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.pages = {}
        for key, title in (("information", "Сведения"), ("security", "Защита"), ("connections", "Связи")):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            content = QWidget()
            content.setObjectName("accountSettingsPage")
            box = QVBoxLayout(content)
            box.setContentsMargins(2, 16, 2, 8)
            box.setSpacing(14)
            scroll.setWidget(content)
            self.tabs.addTab(scroll, title)
            self.pages[key] = box
        information = self.pages["information"]
        grid = QGridLayout()
        grid.setSpacing(12)
        self.values, self.sources = {}, {}
        for i, (key, caption) in enumerate((("phone", "Телефон этого аккаунта"), ("birthday", "День рождения"),
                                             ("registered", "Дата регистрации"), ("registration_country", "Страна регистрации"))):
            card = QFrame()
            card.setObjectName("personalCard")
            box = QVBoxLayout(card)
            box.setContentsMargins(16, 14, 16, 14)
            box.setSpacing(7)
            box.addWidget(label(caption))
            value = label("—", "personalValue")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            box.addWidget(value)
            source = label("")
            box.addWidget(source)
            self.values[key], self.sources[key] = value, source
            grid.addWidget(card, i // 2, i % 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        information.addLayout(grid)
        self.show_phone = QCheckBox("Показать номер из личной заметки")
        self.show_phone.toggled.connect(self.render)
        information.addWidget(self.show_phone)
        self.country = label("")
        information.addWidget(self.country)
        self.identity = label("")
        self.identity.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        information.addWidget(self.identity)
        information.addWidget(label("Данные загружены из настроек Riot. Скрытые сведения можно дополнить вручную; "
                                    "день и месяц рождения сайт тоже может маскировать."))
        information.addStretch()
        security = self.pages["security"]
        self.email_value = label("—", "personalValue")
        self.email_value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.email_note = label("")
        email_card = QFrame()
        email_card.setObjectName("personalCard")
        email_box = QVBoxLayout(email_card)
        email_box.setContentsMargins(16, 14, 16, 14)
        email_box.addWidget(label("Почта аккаунта"))
        email_box.addWidget(self.email_value)
        email_box.addWidget(self.email_note)
        security.addWidget(email_card)
        self.show_email = QCheckBox("Показать адрес почты")
        self.show_email.toggled.connect(self.render)
        security.addWidget(self.show_email)
        self.password_changed = label("")
        security.addWidget(self.password_changed)
        security.addWidget(label("Двухфакторная защита", "personalValue"))
        self.mfa_status = label("")
        security.addWidget(self.mfa_status)
        self.mfa_box = QVBoxLayout()
        self.mfa_box.setSpacing(9)
        security.addLayout(self.mfa_box)
        security.addWidget(label("Настройки показаны как на сайте Riot. Изменить их можно в личном кабинете."))
        security.addStretch()
        connections = self.pages["connections"]
        connections.addWidget(label("Способы входа и привязки", "personalValue"))
        self.providers = label("")
        connections.addWidget(self.providers)
        self.game_pass = label("")
        connections.addWidget(self.game_pass)
        connections.addWidget(label("Приложения с доступом к аккаунту", "personalValue"))
        self.apps_status = label("")
        connections.addWidget(self.apps_status)
        self.apps_box = QVBoxLayout()
        self.apps_box.setSpacing(9)
        connections.addLayout(self.apps_box)
        connections.addWidget(label("Параметры рассылок", "personalValue"))
        self.subscriptions = label("")
        connections.addWidget(self.subscriptions)
        connections.addStretch()
        self.status = label("")
        root.addWidget(self.status)
        row = QHBoxLayout()
        self.refresh = AnimatedButton("Обновить")
        self.refresh.clicked.connect(lambda: manager.refresh(self.account))
        self.manual = AnimatedButton("Дополнить…")
        self.manual.setEnabled(bool(account.get("puuid")))
        self.manual.clicked.connect(self._edit)
        row.addWidget(self.refresh)
        row.addWidget(self.manual)
        row.addStretch()
        close = AnimatedButton("Готово")
        close.setObjectName("dialogAddBtn")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        root.addLayout(row)
        manager.changed.connect(self.render)
        self.finished.connect(self._disconnect)
        self.render()

    def _disconnect(self, *_):
        self.manager.changed.disconnect(self.render)

    def _edit(self):
        dialog = ManualDetailsDialog(self.manager.get(self.account), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.manager.put_manual(self.account, dialog.values)
            except ValueError as exc:
                QMessageBox.warning(self, "Об аккаунте", str(exc))

    @staticmethod
    def _fill_rows(box, entries):
        while box.count():
            item = box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for title, detail in entries:
            card = QFrame()
            card.setObjectName("personalCard")
            row = QHBoxLayout(card)
            row.setContentsMargins(13, 10, 13, 10)
            row.addWidget(label(title, ""), 1)
            row.addWidget(label(detail))
            box.addWidget(card)

    def _render_settings(self, riot):
        email = riot.get("email")
        if email:
            local, _, domain = email.partition("@")
            masked = local[:1] + "•••@" + domain
            self.email_value.setText(email if self.show_email.isChecked() else masked)
        else:
            self.email_value.setText("Riot не предоставил")
        verified = riot.get("email_verified")
        self.email_note.setText("Riot · " + ("почта подтверждена" if verified is True else
                                            "почта не подтверждена" if verified is False else "статус неизвестен"))
        self.show_email.setVisible(bool(email))
        self.password_changed.setText("Последняя смена пароля: " + date_text(riot.get("password_changed")) +
                                       (" · Riot, UTC" if riot.get("password_changed") else ""))
        unavailable = riot.get("unavailable_sections", [])
        factors = riot.get("mfa_factors")
        self.mfa_status.setText("Сохранённые сведения · обновление недоступно" if "mfa" in unavailable and factors is not None else
                               "Riot · настройки 2FA" if factors is not None else "Riot не предоставил настройки 2FA")
        factor_names = {"email": "Код на почту", "sms": "SMS", "riotmobile": "Riot Mobile",
                        "riot_app": "Riot Mobile", "thirdparty": "Приложение-аутентификатор"}
        states = {"enabled": "Включено", "disabled": "Выключено", "action_required": "Нужно действие", "issue": "Ошибка"}
        entries = [(factor_names.get(f["factor"], f["factor"]),
                    states.get(f["status"], "Неизвестно") + (" · требуется Riot" if f.get("required") else ""))
                   for f in factors or []]
        if factors == []:
            entries.append(("Способы 2FA", "Список пуст"))
        self._fill_rows(self.mfa_box, entries)
        names = {"google": "Google", "apple": "Apple", "facebook": "Facebook", "xbox": "Xbox",
                 "playstation": "PlayStation", "nintendo": "Nintendo", "discord": "Discord",
                 "gamecenter": "Game Center", "okta": "Okta"}
        providers = riot.get("connected_accounts")
        self.providers.setText(" · ".join(names.get(p, p) for p in providers) if providers else
                               "Других привязок нет" if providers == [] else "Riot не предоставил сведения")
        game_pass = riot.get("game_pass")
        self.game_pass.setText("Xbox Game Pass: " + {"ACTIVE": "активен", "PENDING": "проверяется", "NONE": "не активен"}.get(game_pass, "неизвестно") +
                               (" · сохранено" if "game_pass" in unavailable and game_pass else ""))
        apps = riot.get("authorized_apps")
        self.apps_status.setText("Сохранённые сведения · обновление недоступно" if "apps" in unavailable and apps is not None else
                                "Нет подключённых приложений" if apps == [] else
                                "Riot · доступ выдан приложениям ниже" if apps else "Riot не предоставил список")
        self._fill_rows(self.apps_box, [(a["name"], "С " + date_text(a["connected_at"]) if a.get("connected_at") else "Доступ разрешён")
                                       for a in apps or []])
        def toggle_text(key):
            value = riot.get(key)
            return "Включены" if value is True else "Выключены" if value is False else "Неизвестно"
        self.subscriptions.setText("Новости Riot: " + toggle_text("riot_news") + "\nПредложения партнёров: " + toggle_text("partner_offers") +
                                   ("\nСохранённые сведения · обновление недоступно" if "privacy" in unavailable else ""))

    def render(self, *_):
        record = self.manager.get(self.account)
        riot, manual = record.get("riot", {}), record.get("manual", {})
        for key in ("birthday", "registered"):
            value = riot.get(key) or manual.get(key)
            approximate = key == "registered" and key not in riot and manual.get("registered_approximate")
            self.values[key].setText(("≈ " if approximate else "") + date_text(value))
            self.sources[key].setText(("Riot · " + ("UTC" if key == "registered" else "личные сведения")) if key in riot
                                      else "Указано вручную" if value else "Нет доступных данных")
            if key == "birthday" and not value and riot.get("birthday_masked"):
                self.values[key].setText("••.••." + riot["birthday_masked"][:4])
                self.sources[key].setText("Riot · день и месяц скрыты")
        phone = manual.get("phone")
        verified = riot.get("phone_verified")
        phone_status = "Подтверждён Riot" if verified is True else "Не подтверждён Riot" if verified is False else "Статус неизвестен"
        if phone:
            digits = "".join(c for c in phone if c.isdigit())
            self.values["phone"].setText(phone if self.show_phone.isChecked() else "••• ••• " + digits[-4:])
            self.sources["phone"].setText("Номер указан вручную · " + phone_status)
        else:
            self.values["phone"].setText(phone_status)
            self.sources["phone"].setText("Riot · номер скрыт" if verified is not None else "Riot не предоставил номер")
        self.show_phone.setVisible(bool(phone))
        self.values["registration_country"].setText(country_text(manual.get("registration_country")))
        self.sources["registration_country"].setText("Указано вручную" if manual.get("registration_country") else "Нет доступных данных")
        self.country.setText("Текущая страна аккаунта: " + country_text(riot.get("current_country")))
        identity = []
        for key, caption in (("account_riot_id", "Riot ID"), ("account_login", "Логин Riot"),
                              ("account_region", "Регион аккаунта"), ("locale", "Язык аккаунта")):
            if riot.get(key):
                identity.append(caption + ": " + riot[key])
        self.identity.setText("\n".join(identity))
        self._render_settings(riot)
        key = account_key(self.account)
        busy = bool(self.manager.jobs)
        self.refresh.setEnabled(not busy and bool(self.account.get("puuid")))
        self.refresh.setText("Обновляю…" if key in self.manager.jobs else "Обновить")
        error = self.manager.errors.get(key) or self.manager.storage_error
        updated = record.get("updated_at")
        self.status.setText(error or ("Обновлено " + datetime.fromtimestamp(updated).strftime("%d.%m.%Y в %H:%M")
                                      if updated else "Обновите сведения через сохранённый вход в Riot SDA."))
