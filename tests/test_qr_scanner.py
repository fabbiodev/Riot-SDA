import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from app.ui.main_window import MainWindow
from app.ui.qr_scanner_dialog import QrScannerDialog


class QrScannerWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.account = {"name": "Demo#EUW", "local_id": "synthetic-qr-window"}
        self.win = MainWindow(accounts=[self.account], start_services=False)

    def tearDown(self):
        self.win._close_qr_scanner()
        self.win.timer.stop()
        self.win.deleteLater()
        self.app.processEvents()

    def test_scan_is_a_nonmodal_independent_window_and_repeated_click_reuses_it(self):
        with patch.object(self.win, "_pick_account") as pick:
            self.win._scan_qr()
            scanner = self.win._qr_scanner
            scanner._startup_timer.stop()
            self.assertIsNone(scanner.parentWidget())
            self.assertEqual(scanner.windowType(), Qt.WindowType.Window)
            self.assertEqual(scanner.windowModality(), Qt.WindowModality.NonModal)
            self.assertFalse(scanner.isModal())
            self.assertFalse(scanner.windowIcon().isNull())
            self.assertTrue(self.win.isEnabled())
            self.assertIsNone(self.app.activeModalWidget())
            self.win._scan_qr()
            self.assertIs(self.win._qr_scanner, scanner)
            scanner._startup_timer.stop()
            self.win.hide()
            self.assertTrue(scanner.isVisible())
            pick.assert_not_called()

    def test_cancel_during_startup_cannot_restart_background_screen_capture(self):
        scanner = QrScannerDialog()
        self.addCleanup(scanner.deleteLater)
        scanner.show()
        self.assertTrue(scanner._startup_timer.isActive())
        scanner.reject()
        self.assertFalse(scanner._startup_timer.isActive())
        self.assertFalse(scanner._timer.isActive())
        QTest.qWait(1100)
        self.assertFalse(scanner._timer.isActive())
        self.assertFalse(scanner.isVisible())

    def test_accepted_scan_keeps_explicit_account_selection_and_confirmation_flow(self):
        with patch("app.ui.main_window.parse_qr_login", return_value=("synthetic-request", "eu")), \
             patch.object(self.win, "_pick_account", return_value=self.account) as pick, \
             patch.object(self.win, "_complete_qr_signin") as complete:
            self.win._scan_qr()
            scanner = self.win._qr_scanner
            scanner._result = "synthetic-qr-content"
            scanner.accept()
            self.assertIsNone(self.win._qr_scanner)
            self.assertFalse(scanner._timer.isActive())
            self.assertFalse(scanner._startup_timer.isActive())
            pick.assert_called_once()
            complete.assert_called_once_with(self.account, "synthetic-request", "eu")

    def test_escape_clears_window_reference_and_does_not_start_sign_in(self):
        with patch.object(self.win, "_complete_qr_signin") as complete:
            self.win._scan_qr()
            QTest.keyClick(self.win._qr_scanner, Qt.Key.Key_Escape)
            self.assertIsNone(self.win._qr_scanner)
            complete.assert_not_called()
