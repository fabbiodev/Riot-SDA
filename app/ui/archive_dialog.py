"""Choose backup scope and optional ZIP protection."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QHBoxLayout, QLabel,
                             QComboBox, QCheckBox, QLineEdit)

from app.ui.motion import AnimatedButton


class ArchiveExportDialog(QDialog):
    def __init__(self, count, account=None, selected_only=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Экспорт аккаунтов в ZIP")
        self.setMinimumWidth(460)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(14)
        title = QLabel("Резервная копия аккаунтов")
        title.setObjectName("dialogTitle")
        root.addWidget(title)
        note = QLabel("В ZIP сохраняются профили, коллекции, данные 2FA и доступные сессии входа. "
                      "Без пароля их сможет прочитать любой, у кого есть архив. "
                      "Личные заметки и сведения из окна «Об аккаунте» в архив не включаются.")
        note.setWordWrap(True)
        root.addWidget(note)
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.scope = QComboBox()
        self.scope.addItem(f"Все аккаунты ({count})", "all")
        if account:
            self.scope.addItem("Выбранный аккаунт", "selected")
            self.scope.setToolTip(account.get("name", ""))
            if selected_only:
                self.scope.setCurrentIndex(1)
        form.addRow("Что экспортировать", self.scope)
        self.protect = QCheckBox("Защитить ZIP паролем")
        self.protect.setChecked(True)
        form.addRow(self.protect)
        self.password = QLineEdit()
        self.confirm = QLineEdit()
        for edit in (self.password, self.confirm):
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setMaxLength(256)
        form.addRow("Пароль", self.password)
        form.addRow("Повторите пароль", self.confirm)
        self.protect.toggled.connect(self.password.setEnabled)
        self.protect.toggled.connect(self.confirm.setEnabled)
        root.addLayout(form)
        self.error = QLabel("")
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setObjectName("mutedLabel")
        self.error.setProperty("error", True)
        self.error.setWordWrap(True)
        root.addWidget(self.error)
        row = QHBoxLayout()
        row.addStretch()
        cancel = AnimatedButton("Отмена")
        cancel.clicked.connect(self.reject)
        save = AnimatedButton("Сохранить ZIP…")
        save.setObjectName("dialogAddBtn")
        save.clicked.connect(self._save)
        row.addWidget(cancel)
        row.addWidget(save)
        root.addLayout(row)

    def _save(self):
        if self.protect.isChecked():
            if not self.password.text():
                self.error.setText("Введите пароль или отключите защиту ZIP.")
                self.password.setFocus()
                return
            if self.password.text() != self.confirm.text():
                self.error.setText("Пароли не совпадают.")
                self.confirm.setFocus()
                return
        self.accept()

    def archive_password(self):
        return self.password.text() if self.protect.isChecked() else ""
