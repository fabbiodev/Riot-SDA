"""Public cosmetic classifications, separate from account ownership and tokens."""

import json
import tempfile
import time
from pathlib import Path

import requests

from app.api.developer_api import DataError
from app.core.storage import APPDATA_DIR

LEAGUE_TYPES = {
    "kNoRarity": "Обычный", "kRare": "Редкий", "kEpic": "Эпический",
    "kLegendary": "Легендарный", "kMythic": "Мифический", "kUltimate": "Абсолютный",
    "kExalted": "Возвышенный", "kTranscendent": "Трансцендентный",
}
VAL_TYPES = {"Select": "Select", "Deluxe": "Deluxe", "Premium": "Premium",
             "Exclusive": "Exclusive", "Ultra": "Ultra"}
VAL_ALIASES = {"Select": "Селект", "Deluxe": "Делюкс", "Premium": "Премиум",
               "Exclusive": "Эксклюзив", "Ultra": "Ультра"}
UNKNOWN_TYPE = "Тип не указан"


def league_skin_type(rarity):
    if rarity not in LEAGUE_TYPES:
        return {}
    return {"skin_type": LEAGUE_TYPES[rarity], "skin_type_id": rarity,
            "skin_type_aliases": [rarity.removeprefix("k")]}


def normalize_skin_types(game, skins, tiers=None):
    result = {}
    if game == "lol":
        rows = skins.values() if isinstance(skins, dict) else skins
        for row in rows:
            if not isinstance(row, dict):
                continue
            value = league_skin_type(row.get("rarity"))
            if row.get("id") is not None and value:
                result[str(row["id"])] = value
                for chroma in row.get("chromas") or []:
                    if isinstance(chroma, dict) and chroma.get("id") is not None:
                        result[str(chroma["id"])] = value
    elif game == "valorant":
        categories = {str(t["uuid"]).lower(): t for t in (tiers or []) if isinstance(t, dict) and t.get("uuid")}
        for row in skins:
            if not isinstance(row, dict):
                continue
            tier_id = str(row.get("contentTierUuid") or "").lower()
            tier = categories.get(tier_id)
            if row.get("uuid") and tier:
                label = VAL_TYPES.get(tier.get("devName")) or tier.get("displayName")
                if label:
                    value = {
                        "skin_type": label, "skin_type_id": tier_id,
                        "skin_type_aliases": [tier.get("displayName", ""), tier.get("devName", ""),
                                              VAL_ALIASES.get(tier.get("devName"), "")]}
                    result[str(row["uuid"]).lower()] = value
                    for child in [*(row.get("levels") or []), *(row.get("chromas") or [])]:
                        if isinstance(child, dict) and child.get("uuid"):
                            result[str(child["uuid"]).lower()] = value
    return result


def load_skin_types(game):
    if game not in ("lol", "valorant"):
        return {}
    try:
        value = json.loads((Path(APPDATA_DIR) / f"{game}-skin-types.json").read_text(encoding="utf-8"))
        if (value.get("schema") == 1 and isinstance(value.get("skins"), dict)
                and isinstance(value.get("updated_at"), (int, float))
                and all(isinstance(row, dict) and isinstance(row.get("skin_type"), str)
                        for row in value["skins"].values())):
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def fetch_skin_types(game, session=None):
    if game not in ("lol", "valorant"):
        raise DataError("Неизвестная игра.")
    cached = load_skin_types(game)
    if time.time() - cached.get("updated_at", 0) < 86400:
        return cached
    owned_session = session is None
    session = session or requests.Session()
    session.trust_env = False

    def get(url):
        response = session.get(url, timeout=(5, 20), allow_redirects=False)
        if response.status_code != 200:
            raise DataError("Справочник типов скинов временно недоступен.")
        return response.json()

    try:
        if game == "lol":
            rows = get("https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/skins.json")
            types = normalize_skin_types(game, rows)
        else:
            rows = get("https://valorant-api.com/v1/weapons/skins")["data"]
            tiers = get("https://valorant-api.com/v1/contenttiers")["data"]
            types = normalize_skin_types(game, rows, tiers)
        if not types:
            raise DataError("В справочнике нет типов скинов.")
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError, DataError):
        if cached:
            return cached
        raise DataError("Не удалось загрузить типы скинов. Повторите обновление позже.") from None
    finally:
        if owned_session:
            session.close()
    data = {"schema": 1, "updated_at": time.time(), "skins": types}
    directory = Path(APPDATA_DIR)
    temporary = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=f"{game}-types-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False)
        temporary.replace(directory / f"{game}-skin-types.json")
    except OSError:
        if temporary:
            temporary.unlink(missing_ok=True)
    return data


def enrich_skin(row, types):
    # Unknown metadata must not be guessed from a price or the skin's name.
    metadata = types.get(str(row.get("id", "")).lower(), {})
    result = {**row, **metadata} if not row.get("skin_type") else dict(row)
    result.setdefault("skin_type", UNKNOWN_TYPE)
    return result
