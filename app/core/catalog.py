"""Public catalog caches, explicitly separate from owned account inventory."""

import json
import time
import tempfile
from pathlib import Path

import requests

from app.api.developer_api import DataError
from app.core.storage import APPDATA_DIR


def load_catalog(game):
    try:
        data = json.loads((Path(APPDATA_DIR) / f"{game}-catalog.json").read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("characters"), list) and isinstance(data.get("skins"), list):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_catalog(game, data):
    directory = Path(APPDATA_DIR)
    temporary = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=f"{game}-catalog-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False)
        temporary.replace(directory / f"{game}-catalog.json")
    except OSError:
        if temporary:
            temporary.unlink(missing_ok=True)


def league_catalog(session=None):
    session = session or requests.Session()
    session.trust_env = False
    def get(path):
        try:
            response = session.get("https://ddragon.leagueoflegends.com/" + path,
                                   timeout=(5, 20), allow_redirects=False)
            if response.status_code != 200:
                raise DataError("Data Dragon Riot временно недоступен.")
            return response.json()
        except (requests.RequestException, ValueError):
            raise DataError("Не удалось загрузить каталог League с Data Dragon Riot.") from None
    version = get("api/versions.json")[0]
    en = get(f"cdn/{version}/data/en_US/championFull.json")["data"]
    ru = get(f"cdn/{version}/data/ru_RU/championFull.json")["data"]
    characters, skins = [], []
    for alias, champion in en.items():
        localized = ru.get(alias, champion)
        local_skins = {str(s["id"]): s for s in localized.get("skins", [])}
        characters.append({"id": str(champion["key"]), "name": localized["name"],
                           "aliases": [alias, champion["name"]], "kind": "catalog"})
        for skin in champion.get("skins", []):
            if skin.get("num") == 0:
                continue
            local = local_skins.get(str(skin["id"]), skin)
            skins.append({"id": str(skin["id"]), "name": local["name"],
                          "owner": localized["name"], "aliases": [skin["name"], alias, champion["name"]],
                          "kind": "catalog"})
    return {"characters": characters, "skins": skins, "version": version, "updated_at": time.time()}
