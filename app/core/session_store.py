"""An atomic, per-Windows-user DPAPI vault, separate from account exports."""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile

from app.core.storage import APPDATA_DIR


class VaultError(Exception):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data, decrypt=False):
    if os.name != "nt":
        raise VaultError("Сохранение QR-сессий доступно через Windows DPAPI.")
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = _Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        function = crypt.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        arguments = (ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        function = crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        arguments = (ctypes.byref(source), "Riot Auth QR session", None, None, None, 1, ctypes.byref(output))
    function.restype = wintypes.BOOL
    if not function(*arguments):
        raise VaultError("Windows не смог открыть или сохранить защищённую QR-сессию.")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(output.pbData)


class SessionStore:
    def __init__(self, path=None):
        self.path = Path(path or Path(APPDATA_DIR) / "qr-sessions.dpapi")

    def load(self):
        if not self.path.exists():
            return {}
        try:
            if self.path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError()
            data = json.loads(_dpapi(self.path.read_bytes(), decrypt=True))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("sessions"), dict):
                raise ValueError()
            return data["sessions"]
        except (OSError, ValueError):
            raise VaultError("Не удалось открыть сохранённые QR-сессии. Войдите в Riot заново.") from None

    def save(self, sessions):
        allowed = ("puuid", "name", "generation", "sso", "access_token", "expires_at",
                   "cookie_expires_at", "last_refresh", "status", "next_attempt", "failures", "error")
        payload = {key: {field: value for field, value in record.items() if field in allowed}
                   for key, record in sessions.items()}
        encrypted = _dpapi(json.dumps({"version": 1, "sessions": payload}).encode("utf-8"))
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix="qr-", suffix=".tmp", delete=False) as file:
                temporary = file.name
                file.write(encrypted)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise VaultError("Не удалось сохранить QR-сессию. Пока она доступна только в этом запуске.") from None
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


class MemorySessionStore:
    """Used by offline UI checks and tests; never reads real account secrets."""
    def load(self):
        return {}

    def save(self, sessions):
        pass
