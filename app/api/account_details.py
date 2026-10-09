"""Read personal fields only from the selected account's authenticated session."""

from datetime import date, datetime, timezone
import math
import re
import time
from urllib.parse import urljoin, urlsplit

import requests

from app.api.riot_api import SSO_COOKIE_NAMES, token_expires_at

PORTAL = "https://account.riotgames.com"
USERINFO = "https://auth.riotgames.com/userinfo"


class AccountDetailsError(Exception):
    def __init__(self, message, retry_after=60):
        super().__init__(message)
        self.retry_after = retry_after


def iso_date(value):
    if not isinstance(value, str):
        return None
    try:
        result = date.fromisoformat(value[:10])
        return result.isoformat() if date(1900, 1, 1) <= result <= date.today() else None
    except ValueError:
        return None


def creation_date(value):
    # acct.created_at is an account timestamp, never OAuth iat/country_at.
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    if value > 10**12:
        value /= 1000
    if not 946684800 <= value <= time.time():
        return None
    return datetime.fromtimestamp(value, timezone.utc).date().isoformat()


def same_owner(owner, value):
    return (isinstance(value, dict) and bool(owner) and
            str(value.get("sub") or "").casefold() == str(owner).casefold())


def extract_details(owner, user=None, userinfo=None):
    """Whitelist confirmed schema fields; raw PII responses/tokens are never saved."""
    result = {}
    for response in (user, userinfo):
        if response is not None and not same_owner(owner, response):
            raise AccountDetailsError("Riot вернул сведения другого аккаунта.")
    if user:
        birthday = iso_date(user.get("birth_date"))
        if birthday:
            result["birthday"] = birthday
        elif isinstance(user.get("birth_date"), str) and re.fullmatch(r"\d{4}-\*\*-\*\*", user["birth_date"]):
            year = int(user["birth_date"][:4])
            if 1900 <= year <= date.today().year:
                result["birthday_masked"] = user["birth_date"]
    if userinfo:
        acct = userinfo.get("acct")
        created = creation_date(acct.get("created_at")) if isinstance(acct, dict) else None
        if created:
            result["registered"] = created
        verified = userinfo.get("phone_number_verified")
        if type(verified) is bool:
            result["phone_verified"] = verified
    country = (user or {}).get("country") or (userinfo or {}).get("country")
    if isinstance(country, str) and len(country) in (2, 3) and country.isascii() and country.isalpha():
        result["current_country"] = country.upper()
    return result


def _response_json(response):
    if response.status_code == 429:
        try:
            retry = max(60, min(86400, int(response.headers.get("Retry-After", 300))))
        except (ValueError, TypeError):
            retry = 300
        raise AccountDetailsError("Riot ограничил запросы. Попробуйте позже.", retry)
    if response.status_code in (401, 403):
        raise AccountDetailsError("Для личных сведений нужен повторный вход в Riot SDA.", 300)
    if response.status_code != 200:
        raise AccountDetailsError("Riot пока не предоставил личные сведения.")
    try:
        data = response.json()
    except ValueError:
        raise AccountDetailsError("Не удалось прочитать ответ Riot.") from None
    if not isinstance(data, dict):
        raise AccountDetailsError("Не удалось прочитать ответ Riot.")
    return data


def _portal_get(session, url):
    # Normal SSO redirects, with an explicit HTTPS host boundary before each hop.
    for _ in range(8):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in
                ("account.riotgames.com", "auth.riotgames.com") or parsed.username or parsed.password
                or parsed.port not in (None, 443)):
            raise AccountDetailsError("Riot перенаправил вход на неизвестный адрес.")
        response = session.get(url, timeout=(5, 15), allow_redirects=False)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("Location")
        if not location:
            break
        url = urljoin(url, location)
    raise AccountDetailsError("Вход Riot не завершён. Войдите заново в Riot SDA.", 300)


def fetch_account_details(account, record):
    owner = account.get("puuid")
    if not owner or str(record.get("puuid") or "").casefold() != str(owner).casefold():
        raise AccountDetailsError("Для личных сведений нужен вход в выбранный аккаунт.")
    user = userinfo = None
    try:
        with requests.Session() as session:
            session.trust_env = False
            token = record.get("access_token")
            if token and (token_expires_at(token) or 0) > time.time() + 30:
                response = session.get(USERINFO, headers={"Authorization": "Bearer " + token},
                                       timeout=(5, 15), allow_redirects=False)
                if response.status_code not in (401, 403):
                    userinfo = _response_json(response)
                    if not same_owner(owner, userinfo):
                        raise AccountDetailsError("Riot вернул сведения другого аккаунта.")
            sso = record.get("sso") or {}
            if record.get("status") not in ("forgotten", "login") and sso.get("ssid"):
                for name, value in sso.items():
                    if name in SSO_COOKIE_NAMES and isinstance(value, str):
                        session.cookies.set(name, value, domain="auth.riotgames.com", path="/")
                page = _portal_get(session, PORTAL + "/")
                csrf = next((c.value for c in session.cookies if c.name == "a12l-csrf-prod"
                             and c.domain.lstrip(".") == "account.riotgames.com"), None)
                if page.status_code != 200 or not csrf:
                    raise AccountDetailsError("Для даты рождения нужен повторный вход в Riot SDA.", 300)
                response = session.get(PORTAL + "/api/account/v1/user", headers={"csrf-token": csrf},
                                       timeout=(5, 15), allow_redirects=False)
                user = _response_json(response)
        if user is None and userinfo is None:
            raise AccountDetailsError("Для личных сведений нужен вход в выбранный аккаунт.")
        return extract_details(owner, user, userinfo)
    except requests.RequestException:
        raise AccountDetailsError("Не удалось связаться с Riot. Сохранённые сведения доступны.") from None
