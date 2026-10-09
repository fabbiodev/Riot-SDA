"""Read a signed-in owner's collections. All client operations are GET only.

Local credentials are ephemeral and restricted to 127.0.0.1. VALORANT bearer
tokens go only to its fixed Riot game-service host; they are never sent to the
public metadata service or persisted. No automatic login/account switching.
"""

import base64
import json
import os
import re
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import psutil
import requests
from urllib3.exceptions import InsecureRequestWarning

from app.api.developer_api import DataError
from app.core.storage import APPDATA_DIR

AGENTS_TYPE = "01bb38e1-da47-4e6a-9b3d-945fe4655707"
SKINS_TYPE = "e7c63390-eda7-46e0-bb7a-a6abdacd2433"
VARIANTS_TYPE = "3ad1b2b2-acdb-4524-852f-954a76ddae0a"
VAL_SHARDS = {"na", "eu", "ap", "kr", "pbe"}
STARTER_AGENTS = {"Brimstone", "Jett", "Phoenix", "Sage", "Sova"}


@dataclass(repr=False)
class LocalClient:
    port: int
    password: str = field(repr=False)
    session: object = field(default_factory=requests.Session, repr=False)

    def __post_init__(self):
        if not 1 <= self.port <= 65535 or not self.password:
            raise DataError("Некорректный lockfile клиента.")
        self.session.trust_env = False

    @classmethod
    def from_lockfile(cls, path):
        try:
            name, pid, port, password, protocol = Path(path).read_text(encoding="utf-8").strip().split(":")
            if protocol != "https" or not pid.isdigit():
                raise ValueError()
            return cls(int(port), password)
        except (OSError, ValueError):
            raise DataError("Клиент не запущен или его lockfile недоступен.") from None

    def get(self, path):
        if not path.startswith("/") or "://" in path:
            raise DataError("Некорректный локальный запрос.")
        try:
            # Riot's local APIs use a self-signed certificate. This exception
            # applies ONLY to the hardcoded loopback origin, never remote TLS.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", InsecureRequestWarning)
                response = self.session.get(
                    f"https://127.0.0.1:{self.port}{path}", auth=("riot", self.password),
                    verify=False, timeout=(3, 10), allow_redirects=False,
                )
            if response.status_code != 200:
                raise DataError(f"Клиент не предоставил данные (HTTP {response.status_code}). Войдите в игру и повторите.")
            return response.json()
        except (requests.RequestException, ValueError):
            raise DataError("Не удалось прочитать данные игрового клиента. Проверьте, что он запущен.") from None


def find_client(game):
    if game == "valorant":
        path = Path(os.getenv("LOCALAPPDATA", "")) / "Riot Games/Riot Client/Config/lockfile"
        return LocalClient.from_lockfile(path)
    candidates = []
    for process in psutil.process_iter(["name", "exe"]):
        try:
            if (process.info.get("name") or "").casefold() in {"leagueclient.exe", "leagueclientux.exe"}:
                executable = process.info.get("exe")
                if executable:
                    candidates.append(Path(executable).parent / "lockfile")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    candidates.append(Path("C:/Riot Games/League of Legends/lockfile"))
    for path in candidates:
        if path.is_file():
            return LocalClient.from_lockfile(path)
    raise DataError("Запустите League of Legends и войдите в выбранный аккаунт, затем нажмите «Коллекция из клиента».")


def require_owner(account, puuid):
    expected = account.get("puuid")
    if not expected:
        raise DataError("У этого аккаунта нет ID для сверки с клиентом. Добавьте его через вход Riot.")
    if not puuid or str(expected).casefold() != str(puuid).casefold():
        raise DataError("В игровом клиенте открыт другой аккаунт. Войдите в выбранный аккаунт и повторите.")


def _owned(item):
    return item.get("ownership", {}).get("owned") is True


def league_collection(account, client=None):
    client = client or find_client("lol")
    summoner = client.get("/lol-summoner/v1/current-summoner")
    require_owner(account, summoner.get("puuid"))
    sid = int(summoner["summonerId"])
    inventory = client.get(f"/lol-champions/v1/inventories/{sid}/champions")
    if not isinstance(inventory, list):
        raise DataError("Клиент League вернул неизвестный формат коллекции.")
    characters, skins = [], []
    for champion in inventory:
        cid = int(champion["id"])
        if cid <= 0:
            continue
        # This endpoint includes unowned champions too. Their skins can still
        # be owned, so inspect all champions rather than only owned ones.
        champion_skins = champion.get("skins")
        if champion_skins is None:
            champion_skins = client.get(f"/lol-champions/v1/inventories/{sid}/champions/{cid}/skins")
        if not isinstance(champion_skins, list):
            raise DataError("Не удалось прочитать полный список скинов League.")
        owner_name = champion.get("name") or str(cid)
        aliases = [champion.get("alias", "")]
        owned_skins = []
        for skin in champion_skins:
            if not _owned(skin) or skin.get("isBase") or int(skin["id"]) == cid * 1000:
                continue
            row = {"id": str(skin["id"]), "name": skin.get("name") or str(skin["id"]),
                   "owner": owner_name, "owner_id": str(cid), "aliases": aliases, "kind": "owned"}
            skins.append(row)
            owned_skins.append(row["id"])
        if _owned(champion):
            characters.append({"id": str(cid), "name": owner_name, "aliases": aliases,
                               "role": ", ".join(champion.get("roles", [])),
                               "skin_ids": owned_skins, "kind": "owned"})
    require_owner(account, client.get("/lol-summoner/v1/current-summoner").get("puuid"))
    try:
        region = client.get("/riotclient/region-locale").get("region")
    except DataError:
        region = None
    name = summoner.get("gameName")
    tag = summoner.get("tagLine")
    return {"riot_id": f"{name}#{tag}" if name and tag else account["name"],
            "region": region, "level": summoner.get("summonerLevel"),
            "characters": characters, "skins": skins,
            "collection_source": "League Client", "collection_updated_at": time.time(),
            "profile_source": "League Client", "profile_updated_at": time.time()}


def _public_json(path, **params):
    # Fresh, credential-free session. Never reuse the Riot/local sessions here.
    with requests.Session() as session:
        session.trust_env = False
        try:
            response = session.get("https://valorant-api.com/v1/" + path,
                                   params=params, timeout=(5, 20), allow_redirects=False)
            if response.status_code != 200:
                raise DataError("Справочник названий VALORANT временно недоступен.")
            payload = response.json().get("data")
            if payload is None:
                raise ValueError()
            return payload
        except (requests.RequestException, ValueError):
            raise DataError("Не удалось загрузить справочник названий VALORANT.") from None


def valorant_metadata(force=False):
    path = Path(APPDATA_DIR) / "valorant-public-catalog.json"
    cached = None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if not force and cached.get("schema") == 1 and time.time() - cached.get("updated_at", 0) < 86400:
            return cached
    except (OSError, ValueError, AttributeError, TypeError):
        cached = None
    try:
        agents_en = _public_json("agents", language="en-US", isPlayableCharacter="true")
        weapons_en = _public_json("weapons", language="en-US")
        agents_ru = _public_json("agents", language="ru-RU", isPlayableCharacter="true")
        weapons_ru = _public_json("weapons", language="ru-RU")
        data = normalize_val_catalog(agents_en, weapons_en, agents_ru, weapons_ru)
    except DataError:
        if isinstance(cached, dict) and cached.get("schema") == 1:
            return cached
        raise
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass  # A cache write failure must not lose a successfully fetched collection.
    return data


def normalize_val_catalog(agents_en, weapons_en, agents_ru, weapons_ru):
    agents_local = {a["uuid"]: a for a in agents_ru}
    weapons_local = {w["uuid"]: w for w in weapons_ru}
    agents, levels, chromas = {}, {}, {}
    for a in agents_en:
        if not a.get("isPlayableCharacter", True):
            continue
        local = agents_local.get(a["uuid"], a)
        agents[a["uuid"]] = {"id": a["uuid"], "name": local["displayName"],
                              "aliases": [a["displayName"]],
                              "role": (local.get("role") or {}).get("displayName", ""),
                              "starter": a["displayName"] in STARTER_AGENTS}
    for weapon in weapons_en:
        local_weapon = weapons_local.get(weapon["uuid"], weapon)
        local_skins = {s["uuid"]: s for s in local_weapon.get("skins", [])}
        for skin in weapon.get("skins", []):
            local_skin = local_skins.get(skin["uuid"], skin)
            row = {"id": skin["uuid"], "name": local_skin["displayName"],
                   "aliases": [skin["displayName"], weapon["displayName"]],
                   "owner": local_weapon["displayName"],
                   "base": skin["displayName"].startswith("Standard ")}
            for level in skin.get("levels", []):
                levels[level["uuid"]] = row
            local_chromas = {c["uuid"]: c for c in local_skin.get("chromas", [])}
            for chroma in skin.get("chromas", []):
                chromas[chroma["uuid"]] = {
                    "skin_id": skin["uuid"],
                    "name": local_chromas.get(chroma["uuid"], chroma)["displayName"]}
    return {"schema": 1, "updated_at": time.time(), "agents": agents,
            "levels": levels, "chromas": chromas}


def entitlement_ids(payload, item_type):
    if not isinstance(payload, dict):
        raise DataError("Неизвестный формат коллекции VALORANT.")
    # Riot returns either the category directly or grouped categories.
    if "Entitlements" in payload:
        values = payload["Entitlements"]
    elif "EntitlementsByTypes" in payload:
        values = [x for group in payload["EntitlementsByTypes"]
                  if group.get("ItemTypeID") == item_type for x in group.get("Entitlements", [])]
    else:
        raise DataError("Riot не вернул коллекцию VALORANT. Повторите после входа в игру.")
    return {x["ItemID"].lower() for x in values if x.get("ItemID")}


def normalize_val_collection(owned_agents, owned_levels, owned_chromas, metadata):
    characters = []
    known_agents = metadata["agents"]
    for aid in sorted(owned_agents | {key for key, a in known_agents.items() if a.get("starter")}):
        a = known_agents.get(aid, {"id": aid, "name": f"Агент {aid}", "aliases": [], "role": ""})
        characters.append({**a, "kind": "owned"})
    skins = {}
    for level in sorted(owned_levels):
        row = metadata["levels"].get(level)
        if row is None:
            # Unknown upgraded IDs cannot reliably be counted as unique skins.
            raise DataError("Справочник ещё не содержит новые скины коллекции. Повторите обновление позже.")
        if not row.get("base"):
            skins[row["id"]] = {**row, "kind": "owned", "variants": []}
    for chroma in sorted(owned_chromas):
        variant = metadata["chromas"].get(chroma)
        if variant and variant["skin_id"] in skins:
            skins[variant["skin_id"]]["variants"].append(variant["name"])
    return characters, list(skins.values())


def valorant_collection(account, client=None, metadata=None, remote_session=None):
    client = client or find_client("valorant")
    tokens = client.get("/entitlements/v1/token")
    require_owner(account, tokens.get("subject"))
    sessions = client.get("/product-session/v1/external-sessions")
    active = [s for s in sessions.values() if s.get("productId") == "valorant"
              and s.get("phase") in ("running", "launching")]
    if not active:
        raise DataError("Запустите VALORANT и войдите в выбранный аккаунт.")
    launch = active[0]
    args = launch.get("launchConfiguration", {}).get("arguments", [])
    deployment = next((a.split("=", 1)[1] for a in args if a.startswith("-ares-deployment=")), "")
    shard = {"latam": "na", "br": "na"}.get(deployment, deployment)
    if shard not in VAL_SHARDS or not launch.get("version"):
        raise DataError("Не удалось определить сервер или версию VALORANT. Перезапустите игру.")
    chat = client.get("/chat/v1/session")
    require_owner(account, chat.get("puuid"))
    puuid = tokens["subject"]
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", puuid) or not tokens.get("accessToken") or not tokens.get("token"):
        raise DataError("Сессия VALORANT ещё не готова. Повторите после входа в игру.")
    session = remote_session or requests.Session()
    session.trust_env = False
    platform = base64.b64encode(json.dumps({"platformType": "PC", "platformOS": "Windows",
                                          "platformOSVersion": "10.0.19042.1.256.64bit",
                                          "platformChipset": "Unknown"}).encode()).decode()
    headers = {"Authorization": "Bearer " + tokens["accessToken"],
               "X-Riot-Entitlements-JWT": tokens["token"],
               "X-Riot-ClientVersion": launch["version"], "X-Riot-ClientPlatform": platform}
    def get(path):
        try:
            response = session.get(f"https://pd.{shard}.a.pvp.net{path}", headers=headers,
                                   timeout=(5, 15), allow_redirects=False)
            if response.status_code != 200:
                raise DataError(f"VALORANT не предоставил коллекцию (HTTP {response.status_code}). Повторите после входа.")
            return response.json()
        except (requests.RequestException, ValueError):
            raise DataError("Не удалось получить коллекцию VALORANT.") from None
    xp = get(f"/account-xp/v1/players/{puuid}")
    if xp.get("Subject") and xp["Subject"].casefold() != puuid.casefold():
        raise DataError("Ответ VALORANT относится к другому аккаунту.")
    ids = [entitlement_ids(get(f"/store/v1/entitlements/{puuid}/{typ}"), typ)
           for typ in (AGENTS_TYPE, SKINS_TYPE, VARIANTS_TYPE)]
    catalog = metadata if metadata is not None else valorant_metadata()
    try:
        characters, skins = normalize_val_collection(*ids, catalog)
    except DataError:
        if metadata is not None:
            raise
        characters, skins = normalize_val_collection(*ids, valorant_metadata(force=True))
    require_owner(account, client.get("/entitlements/v1/token").get("subject"))
    name, tag = chat.get("game_name"), chat.get("game_tag")
    return {"riot_id": f"{name}#{tag}" if name and tag else account["name"],
            "region": deployment.upper(), "level": xp.get("Progress", {}).get("Level"),
            "characters": characters, "skins": skins,
            "collection_source": "VALORANT Client", "collection_updated_at": time.time(),
            "profile_source": "VALORANT Client", "profile_updated_at": time.time()}


def fetch_collection(account, game):
    if game == "lol":
        return league_collection(account)
    if game == "valorant":
        return valorant_collection(account)
    raise DataError("Неизвестная игра.")
