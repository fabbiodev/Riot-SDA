"""Obtain Riot Client credentials through an existing owner's SSO and PKCE.

No password, QR approval, or fabricated refresh token is used here. A challenge
or denied grant stops the exchange and requires the normal interactive login.
"""

import base64
import hashlib
import secrets
import time
from urllib.parse import urlsplit, parse_qs

import requests

from app.api.riot_api import SSO_COOKIE_NAMES, decode_jwt_payload

AUTHORIZE = "https://auth.riotgames.com/api/v1/authorization"
TOKEN = "https://auth.riotgames.com/token"
USERINFO = "https://auth.riotgames.com/userinfo"
REDIRECT = "http://localhost/redirect"
DESKTOP_SCOPES = ("openid", "link", "ban", "lol_region", "lol", "account")
SCOPES = " ".join((*DESKTOP_SCOPES, "offline_access"))


class ClientAuthError(Exception):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


class ClientLoginRequired(ClientAuthError):
    pass


def _json(response):
    if response.status_code == 429:
        retry = response.headers.get("Retry-After", "")
        raise ClientAuthError("Riot ограничил запросы. Повторите позже.",
                              min(int(retry), 86400) if retry.isdigit() else 60)
    if response.status_code in (401, 403):
        raise ClientLoginRequired("Riot требует повторный вход в аккаунт.")
    if response.status_code != 200:
        raise ClientAuthError("Riot не предоставил сессию для клиента.")
    try:
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except ValueError:
        raise ClientAuthError("Не удалось прочитать ответ Riot.") from None


def create_client_session(account, sso):
    """Return server-issued desktop tokens, after server-side owner verification."""
    owner = str(account.get("puuid") or "")
    if not owner or not isinstance(sso, dict) or not sso.get("ssid"):
        raise ClientLoginRequired("Для создания сессии сначала войдите в аккаунт в Riot SDA.")
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    with requests.Session() as session:
        session.trust_env = False
        for name, value in sso.items():
            if name in SSO_COOKIE_NAMES and isinstance(value, str) and value:
                session.cookies.set(name, value, domain="auth.riotgames.com")
        try:
            auth = _json(session.post(AUTHORIZE, json={
                "client_id": "riot-client", "redirect_uri": REDIRECT, "response_type": "code",
                "scope": SCOPES, "state": state, "nonce": nonce,
                "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "none",
            }, timeout=(5, 15), allow_redirects=False))
            if auth.get("type") != "response":
                raise ClientLoginRequired("Riot требует обычный вход или подтверждение. Обновите вход в Riot SDA.")
            uri = ((auth.get("response") or {}).get("parameters") or {}).get("uri", "")
            parsed = urlsplit(uri)
            if parsed.scheme != "http" or parsed.netloc != "localhost" or parsed.path != "/redirect":
                raise ClientAuthError("Riot вернул неизвестный адрес авторизации.")
            params = parse_qs(parsed.query or parsed.fragment)
            if params.get("state") != [state] or len(params.get("code", [])) != 1:
                raise ClientAuthError("Не удалось проверить ответ авторизации Riot.")
            tokens = _json(session.post(TOKEN, data={
                "client_id": "riot-client", "grant_type": "authorization_code", "code": params["code"][0],
                "redirect_uri": REDIRECT, "code_verifier": verifier,
            }, timeout=(5, 15), allow_redirects=False))
            if (tokens.get("token_type", "").casefold() != "bearer"
                    or not all(isinstance(tokens.get(k), str) and tokens[k] for k in
                               ("access_token", "id_token", "refresh_token"))):
                raise ClientAuthError("Riot не выдал сохраняемую сессию клиента.")
            identity = decode_jwt_payload(tokens["id_token"]) or {}
            audience = identity.get("aud")
            expires = identity.get("exp")
            if (identity.get("sub", "").casefold() != owner.casefold()
                    or identity.get("iss") != "https://auth.riotgames.com"
                    or identity.get("nonce") != nonce
                    or not (audience == "riot-client" or isinstance(audience, list) and "riot-client" in audience)
                    or type(expires) not in (int, float) or expires <= time.time()):
                raise ClientAuthError("Сессия Riot не соответствует выбранному аккаунту или клиенту.")
            # Claims alone are not an ownership proof: verify through Riot over TLS.
            info = _json(session.get(USERINFO, headers={"Authorization": "Bearer " + tokens["access_token"]},
                                     timeout=(5, 15), allow_redirects=False))
            if str(info.get("sub", "")).casefold() != owner.casefold():
                raise ClientAuthError("Riot подтвердил другой аккаунт. Сессия не сохранена.")
            scopes = tokens.get("scope", SCOPES)
            if not isinstance(scopes, str):
                raise ClientAuthError("Неизвестный формат прав сессии Riot.")
            if not set(DESKTOP_SCOPES).issubset(scopes.split()):
                raise ClientAuthError("Riot не предоставил нужные права для клиента.")
            return {"puuid": owner, "id_token": tokens["id_token"], "refresh_token": tokens["refresh_token"],
                    "scopes": list(DESKTOP_SCOPES), "created_at": time.time()}
        except requests.RequestException:
            raise ClientAuthError("Нет соединения с Riot. Сессия клиента не создана.") from None
