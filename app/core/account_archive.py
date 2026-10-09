"""Portable account backups: standard ZIP or authenticated WinZip AES-256 ZIP."""

import copy
import base64
import binascii
import io
import json
from pathlib import Path
import re
import uuid
import zipfile

import pyzipper

from app.api.riot_api import SSO_COOKIE_NAMES
from app.core.client_switcher import atomic_write
from app.core.inventory import normalize_collection
from app.core.search import account_key
from app.core.sessions import same_owner, timestamp

FORMAT = "Riot-SDA.accounts"
MAX_JSON = 64 * 1024 * 1024
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_ACCOUNTS = 500
PROFILE_FIELDS = ("name", "login", "puuid", "seed", "games", "api_routes", "league_region")
GAME_FIELDS = {"riot_id", "region", "level", "icon", "profile_icon_id", "profile_note", "api_puuid", "api_region", "profile_source", "profile_updated_at",
               "characters", "skins", "collection_source", "collection_updated_at", "ranks", "updated_at"}


class ArchiveError(Exception):
    pass


class ArchivePasswordRequired(ArchiveError):
    pass


class ArchivePasswordError(ArchiveError):
    pass


def _text(value, limit=256):
    return isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 for c in value)


def _validate_profile(raw):
    if not isinstance(raw, dict) or not _text(raw.get("name")):
        raise ArchiveError("В архиве есть некорректный профиль аккаунта.")
    profile = copy.deepcopy({k: v for k, v in raw.items() if k in PROFILE_FIELDS and v is not None
                             and not (k in ("login", "seed", "league_region") and v == "")})
    if not profile.get("puuid") and not profile.get("seed"):
        raise ArchiveError("У профиля нет ID Riot или данных 2FA.")
    for key in ("puuid", "login", "league_region"):
        if key in profile and not _text(profile[key]):
            raise ArchiveError("В архиве есть некорректные сведения аккаунта.")
    if "seed" in profile:
        seed = profile["seed"]
        if not isinstance(seed, str) or not re.fullmatch(r"[A-Z2-7]{16,512}={0,6}", seed, re.I):
            raise ArchiveError("В архиве есть некорректные данные 2FA.")
        seed = seed.upper().rstrip("=")
        try:
            base64.b32decode(seed + "=" * (-len(seed) % 8))
        except binascii.Error:
            raise ArchiveError("В архиве есть некорректные данные 2FA.") from None
        profile["seed"] = seed
    routes = profile.get("api_routes", {})
    if not isinstance(routes, dict) or any(k in routes and not isinstance(routes[k], str) for k in ("lol", "valorant")):
        raise ArchiveError("В архиве есть некорректные настройки сервера.")
    profile["api_routes"] = {k: v for k, v in routes.items() if k in ("lol", "valorant", "lol_manual")}
    if "lol_manual" in routes and type(routes["lol_manual"]) is not bool:
        raise ArchiveError("В архиве есть некорректные настройки сервера.")
    games = profile.get("games", {})
    if not isinstance(games, dict) or any(k not in ("lol", "valorant") for k in games):
        raise ArchiveError("В архиве есть некорректные игровые профили.")
    for game, data in games.items():
        if not isinstance(data, dict):
            raise ArchiveError("В архиве есть некорректный игровой профиль.")
        data = {k: v for k, v in data.items() if k in GAME_FIELDS}
        for field in ("riot_id", "region", "profile_source", "collection_source"):
            if field in data and not _text(data[field]):
                raise ArchiveError("В архиве есть некорректные игровые сведения.")
        if data.get("level") is not None and (type(data["level"]) is not int or not 0 <= data["level"] <= 1000000):
            raise ArchiveError("В архиве указан некорректный уровень.")
        for field in ("characters", "skins"):
            rows = data.get(field, [])
            if not isinstance(rows, list) or len(rows) > 50000:
                raise ArchiveError("В архиве есть некорректная коллекция.")
            for row in rows:
                if not isinstance(row, dict) or not _text(row.get("name"), 512):
                    raise ArchiveError("В архиве есть некорректная карточка коллекции.")
                if row.get("id") is not None and type(row["id"]) not in (str, int):
                    raise ArchiveError("В архиве есть некорректный ID предмета.")
                for text_field in ("owner", "owner_id", "role", "kind", "skin_type", "skin_type_id", "image", "icon_url"):
                    if row.get(text_field) is not None and not isinstance(row[text_field], str):
                        raise ArchiveError("В архиве есть некорректные сведения предмета.")
                for values in ("skin_ids", "aliases", "variants"):
                    if values in row and (not isinstance(row[values], list) or
                            any(type(v) not in (str, int) for v in row[values])):
                        raise ArchiveError("В архиве есть некорректные сведения предмета.")
        ranks = data.get("ranks", {})
        if not isinstance(ranks, dict):
            raise ArchiveError("В архиве есть некорректные ранги.")
        for rank in ranks.values():
            if not isinstance(rank, dict) or rank.get("status") not in ("ranked", "unranked", "missing_key", "unavailable"):
                raise ArchiveError("В архиве есть некорректный ранг.")
            for text_field in ("tier", "division", "region", "error", "refresh_error", "source"):
                if text_field in rank and not isinstance(rank[text_field], str):
                    raise ArchiveError("В архиве есть некорректный ранг.")
            if rank.get("status") == "ranked" and (not _text(rank.get("tier")) or
                    type(rank.get("lp")) is not int or rank["lp"] < 0 or not isinstance(rank.get("division", ""), str)):
                raise ArchiveError("В архиве есть некорректный ранг.")
        games[game] = normalize_collection(data)
    return profile


def _session(profile, record):
    if not same_owner(profile, record) or record.get("status") in ("forgotten", "login"):
        return {}
    source = record.get("sso", {})
    if not isinstance(source, dict):
        raise ArchiveError("В архиве есть некорректная сессия.")
    sso = {k: v for k, v in source.items() if k in SSO_COOKIE_NAMES and _text(v, 16000)}
    if not sso.get("ssid"):
        return {}
    return {"puuid": profile["puuid"], "sso": sso,
            "cookie_expires_at": timestamp(record.get("cookie_expires_at"))}


def archive_bytes(accounts, sessions=None, password=""):
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= MAX_ACCOUNTS:
        raise ArchiveError(f"Выберите от 1 до {MAX_ACCOUNTS} аккаунтов.")
    entries = []
    for account in accounts:
        profile = _validate_profile(account)
        record = (sessions or {}).get(account_key(account))
        if record is None:
            record = {"puuid": account.get("puuid"), "sso": account.get("sso", {})}
        entries.append({"profile": profile, "session": _session(profile, record)})
    try:
        raw = json.dumps({"format": FORMAT, "version": 1, "accounts": entries},
                         ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise ArchiveError("Не удалось подготовить данные аккаунтов.") from None
    if len(raw) > MAX_JSON:
        raise ArchiveError("Коллекции слишком велики для одного архива.")
    output = io.BytesIO()
    with pyzipper.AESZipFile(output, "w", compression=pyzipper.ZIP_DEFLATED) as archive:
        if password:
            archive.setpassword(password.encode("utf-8"))
            archive.setencryption(pyzipper.WZ_AES, nbits=256)
        archive.writestr("accounts.json", raw)
    payload = output.getvalue()
    if len(payload) > MAX_ARCHIVE:
        raise ArchiveError("Архив слишком велик.")
    return payload


def write_archive(path, accounts, sessions=None, password=""):
    payload = archive_bytes(accounts, sessions, password)
    try:
        atomic_write(Path(path), payload)
    except OSError:
        raise ArchiveError("Не удалось сохранить ZIP. Проверьте папку и доступное место.") from None


def read_archive(path, password=""):
    try:
        if Path(path).stat().st_size > MAX_ARCHIVE:
            raise ArchiveError("ZIP слишком велик.")
        with pyzipper.AESZipFile(str(path)) as archive:
            entries = archive.infolist()
            if len(entries) != 1 or entries[0].filename != "accounts.json" or entries[0].is_dir():
                raise ArchiveError("Это не архив аккаунтов Riot SDA.")
            info = entries[0]
            if info.compress_type not in (pyzipper.ZIP_STORED, pyzipper.ZIP_DEFLATED):
                raise ArchiveError("ZIP использует неподдерживаемый способ сжатия.")
            if info.file_size > MAX_JSON:
                raise ArchiveError("Данные в ZIP слишком велики.")
            if info.flag_bits & 1 and not password:
                raise ArchivePasswordRequired("Для этого ZIP нужен пароль.")
            if password:
                archive.setpassword(password.encode("utf-8"))
            try:
                with archive.open(info) as stream:
                    raw = stream.read(MAX_JSON + 1)
            except (RuntimeError, pyzipper.BadZipFile):
                if info.flag_bits & 1:
                    raise ArchivePasswordError("Неверный пароль или повреждённый ZIP.") from None
                raise
            if len(raw) > MAX_JSON:
                raise ArchiveError("Данные в ZIP слишком велики.")
        data = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (not isinstance(data, dict) or data.get("format") != FORMAT or
                type(data.get("version")) is not int or data["version"] != 1):
            raise ArchiveError("Формат архива Riot SDA не поддерживается.")
        entries = data.get("accounts")
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ACCOUNTS:
            raise ArchiveError("В ZIP нет корректного списка аккаунтов.")
        result = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ArchiveError("В ZIP есть некорректная запись.")
            profile = _validate_profile(entry.get("profile"))
            profile["local_id"] = str(uuid.uuid4())
            record = entry.get("session", {})
            if not isinstance(record, dict):
                raise ArchiveError("В ZIP есть некорректная сессия.")
            if record and not same_owner(profile, record):
                raise ArchiveError("Сессия в ZIP принадлежит другому аккаунту.")
            result.append({"profile": profile, "session": _session(profile, record)})
        return result
    except ArchiveError:
        raise
    except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError, NotImplementedError,
            pyzipper.BadZipFile, zipfile.BadZipFile):
        raise ArchiveError("Не удалось прочитать ZIP. Архив повреждён или имеет неподдерживаемый формат.") from None


def is_duplicate(account, existing):
    return any((account.get("puuid") and str(account["puuid"]).casefold() == str(a.get("puuid") or "").casefold())
               or (account.get("seed") and account["seed"].upper().rstrip("=") == str(a.get("seed") or "").upper().rstrip("=")) for a in existing)
