"""Public OP.GG MCP profile API. Only public Riot ID and region leave this client."""

import ast
import json
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime

import requests

from app.api.developer_api import DataError, LOL_PLATFORMS
from app.api.rankings import normalize_entries

ENDPOINT = "https://mcp-api.op.gg/mcp"
FIELDS = ["data.summoner.{game_name,tagline,updated_at}",
          "data.summoner.league_stats[].{game_type,updated_at}",
          "data.summoner.league_stats[].tier_info.{tier,division,lp}"]


class OpggLimit(DataError):
    def __init__(self, seconds):
        super().__init__("Лимит OP.GG. Обновление отложено; сохранённые ранги доступны.")
        self.seconds = seconds


def _retry_after(value):
    try:
        seconds = int(value)
    except (ValueError, TypeError):
        try:
            seconds = int(parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            seconds = 300
    return max(60, seconds)


def decode_profile(text):
    """Decode JSON or OP.GG's compact class notation without evaluating code."""
    if not isinstance(text, str) or len(text) > 1_000_000:
        raise DataError("OP.GG вернул слишком большой ответ.")
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        declarations, body = text.split("\n\n", 1)
        classes = {}
        for line in declarations.splitlines():
            match = re.fullmatch(r"class ([A-Za-z_]\w*): ([A-Za-z_]\w*(?:,[A-Za-z_]\w*)*)", line)
            if not match or match[1] in classes:
                raise ValueError()
            classes[match[1]] = match[2].split(",")
        tree = ast.parse(body.strip(), mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 10000:
            raise ValueError()

        def read(node, depth=0):
            if depth > 30:
                raise ValueError()
            if isinstance(node, ast.Constant) and type(node.value) in (str, int, float, bool, type(None)):
                return node.value
            if isinstance(node, ast.Name) and node.id in ("null", "true", "false"):
                return {"null": None, "true": True, "false": False}[node.id]
            if isinstance(node, ast.List):
                return [read(x, depth + 1) for x in node.elts]
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
                fields = classes[node.func.id]
                if len(fields) != len(node.args):
                    raise ValueError()
                return dict(zip(fields, (read(x, depth + 1) for x in node.args)))
            raise ValueError()

        return read(tree.body)
    except (ValueError, SyntaxError, KeyError, RecursionError):
        raise DataError("Формат ответа OP.GG изменился. Прошлые ранги сохранены.") from None


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo else None
    except ValueError:
        return None


def normalize_profile(payload, riot_id, region, fetched_at):
    try:
        profile = payload["data"]["summoner"]
        name, _, tag = riot_id.rpartition("#")
        if (profile["game_name"].casefold() != name.casefold()
                or profile["tagline"].casefold() != tag.casefold()):
            raise ValueError()
        stats = profile["league_stats"]
        if not isinstance(stats, list) or any(not isinstance(x, dict) for x in stats):
            raise ValueError()
    except (KeyError, TypeError, AttributeError, ValueError):
        raise DataError("OP.GG не вернул профиль выбранного Riot ID. Проверьте ник и сервер.") from None
    ranks = {}
    for queue, game_type, riot_queue in (("solo", "SOLORANKED", "RANKED_SOLO_5x5"),
                                         ("flex", "FLEXRANKED", "RANKED_FLEX_SR")):
        try:
            matches = [x for x in stats if x.get("game_type") == game_type]
            if len(matches) != 1:
                raise ValueError()
            stat = matches[0]
            info = stat["tier_info"]
            tier, division, lp = info["tier"], info["division"], info["lp"]
            entries = []
            if not (tier is None and division is None and lp is None):
                if type(division) is int:
                    division = {1: "I", 2: "II", 3: "III", 4: "IV"}.get(division)
                entries = [{"queueType": riot_queue, "tier": tier, "rank": division, "leaguePoints": lp}]
            rank = normalize_entries(entries, (queue,), region, fetched_at)[queue]
            # Fetch time is not the source's cache refresh time.
            rank.update(source="OP.GG", source_updated_at=_timestamp(stat.get("updated_at"))
                        or _timestamp(profile.get("updated_at")))
            ranks[queue] = rank
        except (KeyError, TypeError, ValueError, DataError):
            ranks[queue] = {"status": "unavailable", "source": "OP.GG",
                            "error": "OP.GG не вернул текущий ранг этой очереди."}
    return ranks


def fetch_opgg_rankings(riot_id, region):
    name, sep, tag = str(riot_id).rpartition("#")
    if not sep or not name.strip() or not tag.strip() or region not in LOL_PLATFORMS:
        raise DataError("Для OP.GG укажите полный Riot ID (ник#тег) и сервер League.")
    headers = {"Accept": "application/json, text/event-stream"}
    with requests.Session() as session:
        session.trust_env = False

        def rpc(method, params=None, request_id=None):
            body = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                body["params"] = params
            if request_id is not None:
                body["id"] = request_id
            try:
                response = session.post(ENDPOINT, json=body, headers=headers,
                                        timeout=(5, 25), allow_redirects=False)
            except requests.RequestException:
                raise DataError("OP.GG недоступен. Проверьте подключение к сети.") from None
            if response.status_code == 429:
                raise OpggLimit(_retry_after(response.headers.get("Retry-After")))
            if not 200 <= response.status_code < 300:
                raise DataError(f"OP.GG недоступен (HTTP {response.status_code}).")
            if response.headers.get("Mcp-Session-Id"):
                headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
            if request_id is None:
                return None
            try:
                text = response.text
                if len(text) > 1_000_000:
                    raise ValueError()
                if "text/event-stream" in response.headers.get("Content-Type", ""):
                    messages = [json.loads("\n".join(line[5:].lstrip() for line in block.splitlines()
                                                    if line.startswith("data:")))
                                for block in text.replace("\r\n", "\n").split("\n\n")
                                if any(line.startswith("data:") for line in block.splitlines())]
                    envelope = next(x for x in messages if x.get("id") == request_id)
                else:
                    envelope = json.loads(text)
                if envelope.get("id") != request_id or "error" in envelope:
                    raise ValueError()
                result = envelope["result"]
                if not isinstance(result, dict) or result.get("isError"):
                    raise ValueError()
                return result
            except (ValueError, KeyError, TypeError, AttributeError, StopIteration):
                raise DataError("OP.GG не смог загрузить профиль. Проверьте Riot ID и сервер.") from None

        init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "RiotAuth", "version": "1.0"}}, 1)
        headers["MCP-Protocol-Version"] = init.get("protocolVersion", "2025-06-18")
        rpc("notifications/initialized")
        result = rpc("tools/call", {"name": "lol_get_summoner_profile", "arguments": {
            "game_name": name, "tag_line": tag, "region": region, "desired_output_fields": FIELDS}}, 2)
        payload = result.get("structuredContent")
        if not isinstance(payload, dict):
            texts = [x["text"] for x in result.get("content", []) if x.get("type") == "text"]
            if len(texts) != 1:
                raise DataError("OP.GG не вернул данные профиля.")
            payload = decode_profile(texts[0])
        return normalize_profile(payload, riot_id, region, time.time())


def fetch_auto_rankings(riot_id, region, tft_key=""):
    # OP.GG publishes no TFT player rank method. Keep the supported Riot route.
    from app.api.rankings import fetch_rankings
    retry_after = 0
    try:
        ranks = fetch_opgg_rankings(riot_id, region)
    except DataError as exc:
        ranks = {q: {"status": "unavailable", "source": "OP.GG", "error": str(exc)}
                 for q in ("solo", "flex")}
        if isinstance(exc, OpggLimit):
            retry_after = exc.seconds
    ranks["tft"] = fetch_rankings(riot_id, region, "", tft_key)["ranks"]["tft"]
    ranks["tft"]["source"] = "Riot Developer API"
    if not tft_key:
        ranks["tft"]["error"] = "У OP.GG нет открытого метода ранга TFT. Укажите ключ TFT API в настройках."
    return {"ranks": ranks, "rank_retry_after": retry_after}
