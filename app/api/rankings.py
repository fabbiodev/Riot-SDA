"""Current queue ranks from Riot's League and TFT Developer APIs."""

import time
from urllib.parse import quote

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
    region = account.get("api_routes", {}).get("lol") or account.get("games", {}).get("lol", {}).get("region")
    return region if region in LOL_PLATFORMS else "RU"


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
