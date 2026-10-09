"""Local Riot Client session snapshots and a reversible cold switch on Windows."""

import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
import warnings
from contextlib import contextmanager

import psutil
import requests
import yaml
from urllib3.exceptions import InsecureRequestWarning

from app.api.client_auth import ClientAuthError
from app.api.riot_api import decode_jwt_payload
from app.core.session_store import _dpapi, VaultError
from app.core.storage import APPDATA_DIR

PRIVATE_FILES = ("RiotGamesPrivateSettings.yaml", "RiotClientPrivateSettings.yaml")
GAME_PROCESSES = {"leagueclient.exe", "league of legends.exe", "valorant.exe",
                  "valorant-win64-shipping.exe", "lor.exe"}
MAX_FILE = 1024 * 1024


class SwitchError(Exception):
    pass


def atomic_write(path, data):
    path = Path(path)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="riot-sda-", delete=False) as stream:
            temporary = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


def private_owner(files):
    """Validate an opaque modern snapshot without trusting its displayed label."""
    try:
        raw = files[PRIVATE_FILES[0]]
        if len(raw) > MAX_FILE:
            raise ValueError()
        doc = yaml.safe_load(raw)
        auth = doc["psl"]["authorization"]["riot-client"]
        if not isinstance(auth.get("refresh_token"), str) or not auth["refresh_token"]:
            raise ValueError()
        token = decode_jwt_payload(auth.get("id_token", "")) or {}
        audience = token.get("aud")
        if (token.get("iss") != "https://auth.riotgames.com"
                or not (audience == "riot-client" or isinstance(audience, list) and "riot-client" in audience)):
            raise ValueError()
        owner = token.get("sub")
        if not isinstance(owner, str) or not owner:
            raise ValueError()
        return owner
    except (KeyError, TypeError, ValueError, AttributeError, yaml.YAMLError):
        raise SwitchError("Неизвестный формат сохранённой сессии Riot Client.") from None


def generated_snapshot(tokens):
    # These fields match the modern installed Riot Client's persistence schema.
    stamp = int(tokens["created_at"] * 1000)
    auth = {"claims": [], "id_token": tokens["id_token"], "refresh_token": tokens["refresh_token"],
            "is_dpop_bound": False, "last_token_creation_time": stamp, "original_token_creation_time": stamp,
            "refresh_token_write_count": 0, "refresh_tokens_session_id": str(uuid.uuid4()),
            "scopes": tokens["scopes"]}
    files = {PRIVATE_FILES[0]: json.dumps({"psl": {"authorization": {"riot-client": auth}},
                                         "riot-login": {"persist": None}}).encode("utf-8")}
    if private_owner(files).casefold() != str(tokens["puuid"]).casefold():
        raise SwitchError("Сессия создана для другого аккаунта.")
    return files


class ClientVault:
    """Never included in account exports; bytes are DPAPI protected at rest."""
    def __init__(self, directory=None):
        self.directory = Path(directory or APPDATA_DIR)
        self.path = self.directory / "client-sessions.dpapi"
        self.journal = self.directory / "client-switch-recovery.dpapi"

    def _read(self, path):
        if not path.exists():
            return {}
        try:
            if path.stat().st_size > 32 * MAX_FILE:
                raise ValueError()
            doc = json.loads(_dpapi(path.read_bytes(), decrypt=True))
            if not isinstance(doc, dict) or doc.get("version") != 1:
                raise ValueError()
            return doc
        except (OSError, ValueError, VaultError):
            raise SwitchError("Не удалось открыть защищённые сессии Riot Client.") from None

    def _write(self, path, payload):
        try:
            atomic_write(path, _dpapi(json.dumps({"version": 1, **payload}).encode()))
        except (OSError, VaultError):
            raise SwitchError("Не удалось сохранить защищённую сессию Riot Client.") from None

    @staticmethod
    def encode_files(files):
        if not isinstance(files, dict) or any(name not in PRIVATE_FILES or not isinstance(data, bytes)
                                            or len(data) > MAX_FILE for name, data in files.items()):
            raise SwitchError("Некорректный набор файлов сессии.")
        return {name: base64.b64encode(data).decode() for name, data in files.items()}

    @staticmethod
    def decode_files(files):
        try:
            if not isinstance(files, dict) or any(name not in PRIVATE_FILES or not isinstance(value, str)
                                                or len(value) > 2 * MAX_FILE for name, value in files.items()):
                raise ValueError()
            decoded = {name: base64.b64decode(value, validate=True) for name, value in files.items()}
            if any(len(data) > MAX_FILE for data in decoded.values()):
                raise ValueError()
            return decoded
        except ValueError:
            raise SwitchError("Некорректный набор файлов сессии.") from None

    def records(self):
        records = self._read(self.path).get("sessions", {})
        if not isinstance(records, dict):
            raise SwitchError("Не удалось открыть список сессий Riot Client.")
        return records

    def files(self, owner):
        record = self.records().get(str(owner).casefold())
        if not record:
            return None
        files = self.decode_files(record.get("files"))
        if private_owner(files).casefold() != str(owner).casefold():
            raise SwitchError("Сохранённая сессия принадлежит другому аккаунту.")
        return files

    def put(self, owner, files):
        if private_owner(files).casefold() != str(owner).casefold():
            raise SwitchError("Нельзя сохранить чужую сессию в выбранный профиль.")
        records = self.records()
        records[str(owner).casefold()] = {"saved_at": time.time(), "files": self.encode_files(files)}
        self._write(self.path, {"sessions": records})

    def remove(self, owner):
        records = self.records()
        records.pop(str(owner).casefold(), None)
        self._write(self.path, {"sessions": records})

    def save_recovery(self, files):
        self._write(self.journal, {"files": self.encode_files(files)})

    def recovery_files(self):
        doc = self._read(self.journal)
        return self.decode_files(doc["files"]) if doc else None

    def clear_recovery(self):
        self.journal.unlink(missing_ok=True)


class NativeClient:
    def __init__(self, root=None, executable=None):
        self.root = Path(root or Path(os.environ.get("LOCALAPPDATA", "")) / "Riot Games/Riot Client")
        self.data = self.root / "Data"
        self.executable = Path(executable) if executable else None

    @contextmanager
    def exclusive(self):
        """Serialize all instances; abandoned operations retain their recovery journal."""
        if os.name != "nt":
            raise SwitchError("Переключение Riot Client доступно на Windows.")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        name = "Local\\RiotSDA.ClientSwitch." + hashlib.sha256(str(self.root.resolve()).casefold().encode()).hexdigest()
        handle = kernel.CreateMutexW(None, False, name)
        acquired = handle and kernel.WaitForSingleObject(handle, 0) in (0, 0x80)
        if not acquired:
            if handle:
                kernel.CloseHandle(handle)
            raise SwitchError("Переключение уже выполняется в другом окне Riot SDA.")
        try:
            yield
        finally:
            kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)

    def get(self, path):
        if path not in ("/rso-auth/v1/authorization", "/player-session-lifecycle/v1/session"):
            raise SwitchError("Неизвестный запрос к клиенту.")
        try:
            name, pid, port, password, scheme = (self.root / "Config/lockfile").read_text().strip().split(":")
            if scheme != "https" or not 1 <= int(port) <= 65535:
                return {}
            if psutil.Process(int(pid)).name().casefold() != "riotclientservices.exe":
                return {}
            with requests.Session() as session, warnings.catch_warnings():
                session.trust_env = False
                warnings.simplefilter("ignore", InsecureRequestWarning)
                result = session.get(f"https://127.0.0.1:{int(port)}{path}", auth=("riot", password),
                                     verify=False, timeout=(2, 3), allow_redirects=False)
                return result.json() if result.status_code == 200 else {}
        except (OSError, ValueError, psutil.Error, requests.RequestException):
            return {}

    def owner(self):
        result = self.get("/rso-auth/v1/authorization")
        return str(result.get("subject") or "") if isinstance(result, dict) else ""

    def read_files(self):
        result = {}
        for name in PRIVATE_FILES:
            path = self.data / name
            if path.is_symlink():
                raise SwitchError("Файл сессии не должен быть ссылкой.")
            if path.is_file():
                if path.stat().st_size > MAX_FILE:
                    raise SwitchError("Файл сессии Riot имеет неизвестный размер.")
                try:
                    result[name] = path.read_bytes()
                except OSError:
                    raise SwitchError("Не удалось прочитать сессию Riot Client.") from None
        return result

    def restore(self, files):
        # Validate the complete payload before touching any existing file.
        ClientVault.encode_files(files)
        if self.services():
            raise SwitchError("Riot Client ещё работает. Файлы сессии не изменены.")
        for name in PRIVATE_FILES:
            path = self.data / name
            if path.is_symlink():
                raise SwitchError("Файл сессии не должен быть ссылкой.")
        for name in PRIVATE_FILES:
            path = self.data / name
            if name in files:
                atomic_write(path, files[name])
            else:
                path.unlink(missing_ok=True)

    @staticmethod
    def game_running():
        try:
            return any((p.info.get("name") or "").casefold() in GAME_PROCESSES
                       for p in psutil.process_iter(["name"]))
        except psutil.Error:
            raise SwitchError("Не удалось проверить запущенные игры.") from None

    @staticmethod
    def services():
        try:
            return [p for p in psutil.process_iter(["name", "exe"])
                    if (p.info.get("name") or "").casefold() == "riotclientservices.exe"]
        except psutil.Error:
            raise SwitchError("Не удалось проверить процессы Riot Client.") from None

    def locate(self):
        candidates = [self.executable] if self.executable else []
        candidates += [Path(p.info["exe"]) for p in self.services() if p.info.get("exe")]
        try:
            installs = json.loads((Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) /
                                   "Riot Games/RiotClientInstalls.json").read_text())
            candidates += [Path(installs[k]) for k in ("rc_default", "rc_live") if installs.get(k)]
        except (OSError, ValueError, TypeError):
            pass
        candidates.append(Path("C:/Riot Games/Riot Client/RiotClientServices.exe"))
        for candidate in candidates:
            if candidate.name.casefold() == "riotclientservices.exe" and candidate.is_file():
                self.executable = candidate.resolve()
                return self.executable
        raise SwitchError("Riot Client не найден. Установите клиент Riot Games.")

    def stop(self):
        if self.game_running():
            raise SwitchError("Закройте League of Legends или VALORANT перед переключением аккаунта.")
        services = self.services()
        children = []
        for process in services:
            try:
                children.extend(process.children(recursive=True))
                process.terminate()
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied:
                raise SwitchError("Не удалось закрыть Riot Client. Закройте его вручную.") from None
        _, alive = psutil.wait_procs(services + children, timeout=10)
        if alive or self.services():
            raise SwitchError("Riot Client ещё закрывается. Повторите запуск позже.")

    def launch(self):
        # No tokens in command-line arguments; no game is launched implicitly.
        try:
            subprocess.Popen([str(self.locate())], cwd=str(self.executable.parent))
        except OSError:
            raise SwitchError("Не удалось запустить Riot Client.") from None

    def wait_owner(self, owner, cancelled=lambda: False, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancelled():
                raise SwitchError("Переключение отменено.")
            current = self.owner()
            if current:
                if current.casefold() != str(owner).casefold():
                    raise SwitchError("Riot Client вошёл в другой аккаунт. Восстанавливаю прежнюю сессию.")
                return
            time.sleep(0.5)
        raise SwitchError("Riot Client не восстановил вход. Обновите вход в Riot SDA и повторите.")


def capture_current(native, vault, accounts):
    owner = native.owner()
    if not owner or not any(str(a.get("puuid", "")).casefold() == owner.casefold() for a in accounts):
        return None
    # Authentication may become visible before the SDK finishes its file write.
    deadline = time.monotonic() + 3
    while True:
        if native.owner().casefold() != owner.casefold():
            raise SwitchError("Аккаунт клиента изменился во время сохранения.")
        try:
            files = native.read_files()
            if private_owner(files).casefold() != owner.casefold():
                raise SwitchError("Файл сессии пока относится к другому аккаунту.")
            if native.owner().casefold() != owner.casefold():
                raise SwitchError("Аккаунт клиента изменился во время сохранения.")
            vault.put(owner, files)
            return owner
        except SwitchError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.15)


def recover(native, vault):
    files = vault.recovery_files()
    if files is None:
        return False
    native.locate()
    native.stop()
    native.restore(files)
    vault.clear_recovery()
    native.launch()
    return True


def switch_client(native, vault, account, accounts, cancelled=lambda: False):
    owner = str(account.get("puuid") or "")
    target = vault.files(owner)
    if not owner or not target:
        raise SwitchError("Сессия Riot Client ещё не создана для этого аккаунта.")
    native.locate()
    if native.game_running():
        raise SwitchError("Закройте игру перед переключением аккаунта.")
    if vault.recovery_files() is not None:
        recover(native, vault)
    if native.owner().casefold() == owner.casefold():
        capture_current(native, vault, accounts)
        native.launch()
        return
    capture_current(native, vault, accounts)
    native.stop()
    original = native.read_files()
    vault.save_recovery(original)  # Durably protected before the first mutation.
    try:
        if cancelled():
            raise SwitchError("Переключение отменено.")
        native.restore(target)
        native.launch()
        native.wait_owner(owner, cancelled)
        capture_current(native, vault, accounts)
        vault.clear_recovery()
    except Exception:
        try:
            native.stop()
            native.restore(original)
            native.launch()
            vault.clear_recovery()
        except Exception:
            raise SwitchError("Переключение не завершено. Прежняя сессия защищена; восстановление будет повторено при запуске.") from None
        vault.remove(owner)  # A rejected snapshot must not shadow a refreshed SSO forever.
        raise SwitchError("Riot Client не принял сессию. Прежний вход восстановлен; обновите вход в Riot SDA.") from None
