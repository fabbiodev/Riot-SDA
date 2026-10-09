"""Personal account details in a compact, source-labelled window."""

from datetime import date, datetime

from PyQt6.QtCore import Qt, QLocale
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QFrame,
                             QLabel, QCheckBox, QFormLayout, QLineEdit, QMessageBox)

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
        self.setMinimumWidth(550)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(16)
        root.addWidget(label("Об аккаунте", "dialogTitle"))
        root.addWidget(label(str(account.get("name", "Аккаунт")), "accountDetailsName"))
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
        root.addLayout(grid)
        self.show_phone = QCheckBox("Показать номер из личной заметки")
        self.show_phone.toggled.connect(self.render)
        root.addWidget(self.show_phone)
        self.country = label("")
        root.addWidget(self.country)
        root.addWidget(label("Riot не всегда возвращает сам номер и страну регистрации. "
                             "Их можно дополнить по своим данным или выгрузке из поддержки Riot."))
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
        key = account_key(self.account)
        busy = bool(self.manager.jobs)
        self.refresh.setEnabled(not busy and bool(self.account.get("puuid")))
        self.refresh.setText("Обновляю…" if key in self.manager.jobs else "Обновить")
        error = self.manager.errors.get(key) or self.manager.storage_error
        updated = record.get("updated_at")
        self.status.setText(error or ("Обновлено " + datetime.fromtimestamp(updated).strftime("%d.%m.%Y в %H:%M")
                                      if updated else "Обновите сведения через сохранённый вход в Riot SDA."))
