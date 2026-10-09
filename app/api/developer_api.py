"""Official Riot Developer API. API keys stay separate from mobile/RSO tokens."""

import time
from urllib.parse import quote

import requests

LOL_PLATFORMS = {
    "RU": ("ru", "europe"), "EUW": ("euw1", "europe"),
    "EUNE": ("eun1", "europe"), "NA": ("na1", "americas"),
    "BR": ("br1", "americas"), "LAN": ("la1", "americas"),
    "LAS": ("la2", "americas"), "KR": ("kr", "asia"),
    "JP": ("jp1", "asia"), "OCE": ("oc1", "sea"),
    "TR": ("tr1", "europe"), "PH": ("ph2", "sea"),
    "SG": ("sg2", "sea"), "TH": ("th2", "sea"),
    "TW": ("tw2", "sea"), "VN": ("vn2", "sea"),
}
VAL_PLATFORMS = {
    "EU": ("eu", "europe"), "NA": ("na", "americas"),
    "BR": ("br", "americas"), "LATAM": ("latam", "americas"),
    "AP": ("ap", "asia"), "KR": ("kr", "asia"),
}
_HOSTS = {route for pair in (*LOL_PLATFORMS.values(), *VAL_PLATFORMS.values())
          for route in pair}


class DataError(Exception):
    """User-safe error: contains no request headers, tokens, or raw responses."""


class DeveloperApi:
    def __init__(self, key, session=None):
        self.key = (key or "").strip()
        self.session = session or requests.Session()
        self.session.trust_env = False

    def get(self, route, path, **params):
        if not self.key:
            raise DataError("Укажите Riot API Key в настройках API.")
        if route not in _HOSTS or not path.startswith("/") or "?" in path:
            raise DataError("Неверный маршрут Riot API.")
        try:
            response = self.session.get(
                f"https://{route}.api.riotgames.com{path}",
                headers={"X-Riot-Token": self.key}, params=params,
                timeout=(5, 15), allow_redirects=False,
            )
        except requests.RequestException:
            raise DataError("Не удалось соединиться с Riot API. Проверьте интернет.") from None
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "")
            suffix = f" Повторите через {retry} с." if retry.isdigit() else " Повторите позже."
            raise DataError("Достигнут лимит Riot API." + suffix)
        messages = {
            401: "Riot API Key недействителен.",
            403: "Ключ истёк или не имеет доступа к этому API.",
            404: "Профиль не найден. Проверьте Riot ID и выбранный сервер.",
        }
        if response.status_code != 200:
            raise DataError(messages.get(response.status_code,
                            f"Riot API временно недоступен (HTTP {response.status_code})."))
        try:
            return response.json()
        except ValueError:
            raise DataError("Riot API вернул некорректный ответ.") from None

    def account(self, riot_id, route):
        name, separator, tag = (riot_id or "").rpartition("#")
        if not separator or not name.strip() or not tag.strip():
            raise DataError("Для Riot API нужен полный Riot ID: ник#тег. Логин не подходит.")
        result = self.get(route, f"/riot/account/v1/accounts/by-riot-id/"
                          f"{quote(name.strip(), safe='')}/{quote(tag.strip(), safe='')}")
        if not isinstance(result, dict) or not all(result.get(k) for k in ("puuid", "gameName", "tagLine")):
            raise DataError("Riot API не вернул идентификатор профиля.")
        return result

    def profile(self, riot_id, game, region):
        routes = LOL_PLATFORMS if game == "lol" else VAL_PLATFORMS
        if game not in ("lol", "valorant") or region not in routes:
            raise DataError("Выберите сервер игры в настройках API.")
        platform, routing = routes[region]
        identity = self.account(riot_id, routing)
        # Developer API PUUIDs may be encrypted/app-scoped. Never overwrite the
        # raw PUUID used by mobile push approval and local client matching.
        result = {"riot_id": f"{identity['gameName']}#{identity['tagLine']}",
                  "api_puuid": identity["puuid"], "api_region": region,
                  "profile_source": "Riot Developer API", "profile_updated_at": time.time()}
        if game == "lol":
            summoner = self.get(platform, "/lol/summoner/v4/summoners/by-puuid/"
                                + quote(identity["puuid"], safe=""))
            if not isinstance(summoner, dict) or "summonerLevel" not in summoner:
                raise DataError("Riot API не вернул уровень League of Legends.")
            result.update(region=region, level=summoner["summonerLevel"],
                          profile_icon_id=summoner.get("profileIconId"))
        else:
            result["profile_note"] = "Riot API не предоставляет отдельный уровень и личную коллекцию VALORANT."
        return result

    def valorant_catalog(self, region):
        if region not in VAL_PLATFORMS:
            raise DataError("Выберите сервер VALORANT.")
        content = self.get(VAL_PLATFORMS[region][0], "/val/content/v1/contents", locale="ru-RU")
        if not isinstance(content, dict):
            raise DataError("Riot API не вернул каталог VALORANT.")
        def items(key):
            return [{"id": x["id"], "name": x.get("localizedNames", {}).get("ru-RU") or x["name"],
                     "aliases": [x["name"]], "owner": "", "kind": "catalog"}
                    for x in content.get(key, []) if isinstance(x, dict) and x.get("id") and x.get("name")]
        return {"characters": items("characters"), "skins": items("skins"),
                "version": content.get("version", ""), "updated_at": time.time()}
