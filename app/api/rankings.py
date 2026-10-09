"""Current queue ranks from Riot's League and TFT Developer APIs."""

import time
from urllib.parse import quote

import requests

from app.api.developer_api import DeveloperApi, DataError, LOL_PLATFORMS

QUEUES = {"solo": "RANKED_SOLO_5x5", "flex": "RANKED_FLEX_SR", "tft": "RANKED_TFT"}
TIERS = {
    "IRON": "Железо", "BRONZE": "Бронза", "SILVER": "Серебро",
    "GOLD": "Золото", "PLATINUM": "Платина", "EMERALD": "Изумруд",
    "DIAMOND": "Алмаз", "MASTER": "Мастер", "GRANDMASTER": "Грандмастер",
    "CHALLENGER": "Претендент",
}
TOP_TIERS = {"MASTER", "GRANDMASTER", "CHALLENGER"}


def rank_region(account):
    routes = account.get("api_routes", {})
    candidates = [routes.get("lol")] if routes.get("lol_manual") else [
        account.get("league_region"), routes.get("lol"), account.get("games", {}).get("lol", {}).get("region")]
    return next((region for value in candidates if (region := normalize_platform(value))), "")


def normalize_platform(value):
    if not isinstance(value, str):
        return ""
    value = value.strip().upper()
    for region, (platform, _) in LOL_PLATFORMS.items():
        if value in (region, platform.upper()):
            return region
    return ""


def region_from_userinfo(account, data):
    owner = str(account.get("puuid") or "").casefold()
    if not owner or not isinstance(data, dict) or str(data.get("sub") or "").casefold() != owner:
        raise DataError("Riot вернул регион другого аккаунта. Войдите заново в Riot SDA.")
    lol = data.get("lol")
    if isinstance(lol, dict) and lol.get("active") is not False:
        region = normalize_platform(lol.get("cpid"))
        if region:
            return region
    entries = data.get("lol_region")
    regions = {normalize_platform(item.get("cpid")) for item in entries
               if isinstance(item, dict) and item.get("active") is True} if isinstance(entries, list) else set()
    regions.discard("")
    if len(regions) == 1:
        return regions.pop()
    raise DataError("Riot не предоставил сервер League. Выберите его в настройках API.")


def resolve_rank_region(account, token=None):
    """Resolve the account's League platform without contacting a local game client."""
    cached = rank_region(account)
    if account.get("api_routes", {}).get("lol_manual") and cached:
        return cached
    if token:
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get("https://auth.riotgames.com/userinfo",
                                       headers={"Authorization": "Bearer " + token},
                                       timeout=(5, 15), allow_redirects=False)
                if response.status_code == 200:
                    return region_from_userinfo(account, response.json())
        except (requests.RequestException, ValueError):
            pass
    if cached:
        return cached
    raise DataError("Не удалось определить сервер League. Войдите в Riot SDA или выберите сервер в настройках API.")


def normalize_entries(entries, queues, region, updated_at):
    if not isinstance(entries, list) or any(not isinstance(x, dict) or not x.get("queueType") for x in entries):
        raise DataError("Riot API не вернул список рангов.")
    result = {}
    for key in queues:
        matches = [entry for entry in entries if entry["queueType"] == QUEUES[key]]
        if len(matches) > 1:
            raise DataError("Riot API вернул неоднозначные данные ранга.")
        rank = {"status": "unranked", "region": region, "updated_at": updated_at}
        if matches:
            entry = matches[0]
            tier, division, lp = entry.get("tier"), entry.get("rank"), entry.get("leaguePoints")
            if (tier not in TIERS or (tier not in TOP_TIERS and division not in ("I", "II", "III", "IV"))
                    or type(lp) is not int or lp < 0):
                raise DataError("Riot API вернул неполные данные ранга.")
            rank.update(status="ranked", tier=tier,
                        division="" if tier in TOP_TIERS else division, lp=lp)
        result[key] = rank
    return result


def fetch_rankings(riot_id, region, lol_key, tft_key):
    if region not in LOL_PLATFORMS:
        raise DataError("Выберите сервер League / TFT в настройках API.")
    platform, routing = LOL_PLATFORMS[region]
    ranks = {}
    for product, api_key, queues, path in (
        ("League", lol_key, ("solo", "flex"), "/lol/league/v4/entries/by-puuid/"),
        ("TFT", tft_key, ("tft",), "/tft/league/v1/by-puuid/"),
    ):
        if not api_key:
            ranks.update({key: {"status": "missing_key", "error": f"Укажите ключ {product} в настройках API."}
                          for key in queues})
            continue
        try:
            api = DeveloperApi(api_key)
            # Each key resolves its own app-scoped PUUID; never reuse the auth
            # PUUID or the PUUID resolved with another product's key.
            identity = api.account(riot_id, routing)
            entries = api.get(platform, path + quote(identity["puuid"], safe=""))
            ranks.update(normalize_entries(entries, queues, region, time.time()))
        except DataError as exc:
            ranks.update({key: {"status": "unavailable", "error": str(exc)} for key in queues})
    return {"ranks": ranks}


def merge_rankings(previous, incoming):
    """Keep a successful snapshot if a subsequent refresh fails."""
    result = dict(previous)
    for queue, rank in incoming.items():
        old = previous.get(queue, {})
        if rank.get("status") in ("missing_key", "unavailable") and old.get("status") in ("ranked", "unranked"):
            result[queue] = {**old, "refresh_error": rank.get("error", "Не удалось обновить ранг.")}
        else:
            result[queue] = rank
    return result


def rank_text(rank):
    status = rank.get("status")
    if status == "ranked":
        return (TIERS.get(rank.get("tier"), "—") + " " + rank.get("division", "")).strip()
    return {"unranked": "Без ранга", "missing_key": "Нужен API-ключ",
            "unavailable": "Недоступно"}.get(status, "Не загружено")
