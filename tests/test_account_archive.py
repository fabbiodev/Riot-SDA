import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pyzipper
from PyQt6.QtWidgets import QApplication, QDialog

from app.core.account_archive import (archive_bytes, write_archive, read_archive, is_duplicate, FORMAT,
                                      ArchiveError, ArchivePasswordRequired, ArchivePasswordError)
from app.core.search import account_key
from app.core.storage import load_accounts, save_accounts
from app.ui.archive_dialog import ArchiveExportDialog
from app.ui.main_window import MainWindow

ACCOUNT = {"name": "Owner#Custom", "login": "owner", "puuid": "synthetic-owner", "local_id": "original",
           "seed": "JBSWY3DPEHPK3PXP", "league_region": "EUW",
           "games": {"lol": {"region": "EUW", "level": 30,
               "characters": [{"id": "103", "name": "Ари", "skin_ids": ["1", "2"]}],
               "skins": [{"id": "1", "name": "Дух цветения Ари"}, {"id": "2", "name": "ДУХ ЦВЕТЕНИЯ АРИ"}]}}}


def manifest(entries):
    return json.dumps({"format": FORMAT, "version": 1, "accounts": entries}, ensure_ascii=False).encode()


class AccountArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "accounts.zip"

    def tearDown(self):
        self.directory.cleanup()

    def plain(self, content, name="accounts.json"):
        with zipfile.ZipFile(self.path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(name, content)

    def test_plain_and_aes_unicode_password_roundtrip_preserve_profile_and_sso(self):
        source = dict(ACCOUNT, personal_details={"email": "private@example.invalid"}, access_token="never-export")
        sessions = {account_key(source): {"puuid": source["puuid"], "status": "ready",
                    "sso": {"ssid": "synthetic-cookie", "unrelated": "excluded"},
                    "access_token": "never-export", "cookie_expires_at": 10**10}}
        original = copy.deepcopy(source)
        for password in ("", "пароль 🔐"):
            write_archive(self.path, [source], sessions, password)
            with pyzipper.AESZipFile(str(self.path)) as archive:
                info = archive.infolist()[0]
                self.assertEqual(bool(info.flag_bits & 1), bool(password))
                if password:
                    self.assertEqual(info.wz_aes_strength, 3)
                    archive.setpassword(password.encode())
                raw = archive.read("accounts.json")
                self.assertNotIn(b"never-export", raw)
                self.assertNotIn(b"private@example.invalid", raw)
                self.assertNotIn(b"unrelated", raw)
            entries = read_archive(self.path, password)
            profile = entries[0]["profile"]
            self.assertEqual(profile["name"], source["name"])
            self.assertNotEqual(profile["local_id"], source["local_id"])
            self.assertEqual(len(profile["games"]["lol"]["skins"]), 1)
            self.assertEqual(profile["games"]["lol"]["characters"][0]["skin_ids"], ["1"])
            self.assertEqual(entries[0]["session"]["sso"], {"ssid": "synthetic-cookie"})
        self.assertEqual(source, original)

    def test_seedless_profiles_and_revoked_or_foreign_sessions(self):
        profile = {"name": "Profile#Tag", "puuid": "owner", "seed": None, "login": ""}
        for record in ({"puuid": "other", "sso": {"ssid": "foreign"}},
                       {"puuid": "owner", "status": "forgotten", "sso": {"ssid": "revoked"}}):
            write_archive(self.path, [profile], {account_key(profile): record})
            entries = read_archive(self.path)
            self.assertNotIn("seed", entries[0]["profile"])
            self.assertEqual(entries[0]["session"], {})

    def test_password_required_wrong_password_and_truncated_archive(self):
        write_archive(self.path, [ACCOUNT], password="correct")
        with self.assertRaises(ArchivePasswordRequired):
            read_archive(self.path)
        with self.assertRaises(ArchivePasswordError):
            read_archive(self.path, "wrong")
        self.path.write_bytes(self.path.read_bytes()[:60])
        with self.assertRaises(ArchiveError):
            read_archive(self.path, "correct")

    def test_format_paths_extra_members_and_expansion_limit_are_rejected(self):
        for content, name in ((b"not json", "accounts.json"), (b"{}", "../accounts.json"),
                              (b'{"format":"other","version":1,"accounts":[]}', "accounts.json")):
            self.plain(content, name)
            with self.assertRaises(ArchiveError):
                read_archive(self.path)
        self.plain(manifest([{"profile": ACCOUNT}]))
        with zipfile.ZipFile(self.path, "a") as archive:
            archive.writestr("extra.txt", "unexpected")
        with self.assertRaises(ArchiveError):
            read_archive(self.path)
        self.plain(b" " * 1000)
        with patch("app.core.account_archive.MAX_JSON", 100), self.assertRaises(ArchiveError):
            read_archive(self.path)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [self.path])

    def test_whole_archive_is_validated_before_import_and_owner_mismatch_is_rejected(self):
        for bad in ({"profile": dict(ACCOUNT, games={"lol": {"characters": "bad"}})},
                    {"profile": dict(ACCOUNT, seed="bad")},
                    {"profile": ACCOUNT, "session": {"puuid": "other", "sso": {"ssid": "foreign"}}}):
            self.plain(manifest([{"profile": ACCOUNT}, bad]))
            with self.assertRaises(ArchiveError):
                read_archive(self.path)

    def test_existing_archive_survives_failed_export_and_account_duplicates_use_identity(self):
        self.path.write_bytes(b"previous backup")
        with self.assertRaises(ArchiveError):
            write_archive(self.path, [{"name": "No identity"}])
        self.assertEqual(self.path.read_bytes(), b"previous backup")
        self.assertTrue(is_duplicate(dict(ACCOUNT, puuid="SYNTHETIC-OWNER", seed="different"), [ACCOUNT]))
        self.assertTrue(is_duplicate(dict(ACCOUNT, puuid="new-owner"), [ACCOUNT]))
        self.assertFalse(is_duplicate({"name": ACCOUNT["name"], "puuid": "different"}, [ACCOUNT]))

    def test_account_save_is_atomic_and_persists_unique_collection(self):
        path = Path(self.directory.name) / "accounts.json"
        previous = b'[{"name":"previous"}]'
        path.write_bytes(previous)
        with patch("app.core.storage.ACCOUNTS_FILE", str(path)):
            with patch("app.core.client_switcher.os.replace", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    save_accounts([copy.deepcopy(ACCOUNT)])
            self.assertEqual(path.read_bytes(), previous)
            save_accounts([copy.deepcopy(ACCOUNT)])
            self.assertEqual(len(load_accounts()[0]["games"]["lol"]["skins"]), 1)


class AccountArchiveUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_export_dialog_validates_password_and_allows_plain_backup(self):
        dialog = ArchiveExportDialog(2, ACCOUNT, selected_only=True)
        self.assertEqual(dialog.scope.currentData(), "selected")
        dialog._save()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        dialog.password.setText("one")
        dialog.confirm.setText("two")
        dialog._save()
        self.assertIn("не совпадают", dialog.error.text())
        dialog.protect.setChecked(False)
        dialog._save()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.archive_password(), "")
        dialog.deleteLater()

    def test_zip_import_skips_existing_accounts_and_restores_sso_to_protected_manager(self):
        with patch("app.ui.main_window.FcmService"), patch("app.ui.main_window.threading.Thread"), \
             patch("app.ui.main_window.save_accounts"):
            win = MainWindow(accounts=[copy.deepcopy(ACCOUNT)], start_services=False)
            incoming = {"name": "New#Tag", "puuid": "new-owner", "local_id": "new-local"}
            win._apply_zip_import([{"profile": copy.deepcopy(ACCOUNT), "session": {}},
                                  {"profile": incoming, "session": {"sso": {"ssid": "synthetic-cookie"}}},
                                  {"profile": dict(incoming, local_id="duplicate"), "session": {}}])
            self.assertEqual(len(win.accounts), 2)
            self.assertEqual(win.dashboard.selected_key, account_key(incoming))
            self.assertEqual(win.sessions.sessions[account_key(incoming)]["sso"]["ssid"], "synthetic-cookie")
            self.assertNotIn("sso", incoming)
            self.assertNotIn("access_token", incoming)
            self.assertTrue(win.dashboard.export_action.isEnabled())
            win.timer.stop()
            win._stop_rank_refresh()
            win.deleteLater()
            self.app.processEvents()
