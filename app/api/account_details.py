"""Read personal fields only from the selected account's authenticated session."""

from datetime import date, datetime, timezone
from html.parser import HTMLParser
import math
import re
import time
from urllib.parse import urljoin, urlsplit

import requests

from app.api.riot_api import SSO_COOKIE_NAMES, token_expires_at, _riot_api_headers

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


class _PageToken(HTMLParser):
    token = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "csrf-token":
            self.token = values.get("content")


def page_csrf(html):
    parser = _PageToken()
    parser.feed(html if isinstance(html, str) else "")
    return parser.token


def short_text(value, limit=120):
    return value.strip()[:limit] if isinstance(value, str) and not any(ord(c) < 32 for c in value) else None


def extract_settings(settings):
    """Save display metadata only; MFA secrets, client IDs and URLs are excluded."""
    result = {}
    factors = settings.get("mfa")
    if isinstance(factors, list):
        result["mfa_factors"] = [{"factor": f["factor"], "status": f["status"],
                                  "required": f.get("mfaOptIn") == "FORCE_ENABLED"}
                                 for f in factors[:12] if isinstance(f, dict)
                                 and isinstance(f.get("factor"), str) and re.fullmatch(r"[a-z_]{1,30}", f["factor"])
                                 and f.get("status") in ("enabled", "disabled", "action_required", "issue")]
    links = settings.get("apps")
    if isinstance(links, dict) and isinstance(links.get("links"), list):
        apps = []
        for link in links["links"][:50]:
            if not isinstance(link, dict):
                continue
            names = link.get("localizedClientName")
            name = short_text(names.get("default")) if isinstance(names, dict) else None
            app = {"name": name or "Приложение Riot"}
            connected = creation_date(link.get("connectionTime"))
            if connected:
                app["connected_at"] = connected
            apps.append(app)
        result["authorized_apps"] = apps
    privacy = settings.get("privacy")
    if isinstance(privacy, dict):
        for remote, local in (("email_subscribe", "riot_news"), ("third_party_opt_in", "partner_offers")):
            if type(privacy.get(remote)) is bool:
                result[local] = privacy[remote]
    game_pass = settings.get("game_pass")
    if isinstance(game_pass, dict) and game_pass.get("status") in ("ACTIVE", "PENDING", "NONE"):
        result["game_pass"] = game_pass["status"]
    return result


def extract_details(owner, user=None, userinfo=None):
    """Whitelist confirmed schema fields; raw PII responses/tokens are never saved."""
    result = {}
    for response in (user, userinfo):
        if response is not None and not same_owner(owner, response):
            raise AccountDetailsError("Riot вернул сведения другого аккаунта.")
    if user:
        for remote, local in (("username", "account_login"), ("region", "account_region"), ("locale", "locale")):
            text = short_text(user.get(remote), 80)
            if text:
                result[local] = text
        email = user.get("email")
        if isinstance(email, str) and len(email) <= 254 and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            result["email"] = email
        email_status = user.get("email_status")
        if email_status in ("validated", "not_validated"):
            result["email_verified"] = email_status == "validated"
        alias = user.get("alias")
        if isinstance(alias, dict):
            name, tag = short_text(alias.get("game_name")), short_text(alias.get("tag_line"), 30)
            if name and tag:
                result["account_riot_id"] = name + "#" + tag
        providers = []
        for field in ("federated_identities", "linked_identities"):
            values = user.get(field)
            if isinstance(values, list):
                providers.extend(v.lower() for v in values[:20] if isinstance(v, str)
                                 and re.fullmatch(r"[A-Za-z_]{1,30}", v) and v.lower() != "riot_identity")
        if any(isinstance(user.get(k), list) for k in ("federated_identities", "linked_identities")):
            result["connected_accounts"] = sorted(set(providers))
        birthday = iso_date(user.get("birth_date"))
        if birthday:
            result["birthday"] = birthday
        elif isinstance(user.get("birth_date"), str) and re.fullmatch(r"\d{4}-\*\*-\*\*", user["birth_date"]):
            year = int(user["birth_date"][:4])
            if 1900 <= year <= date.today().year:
                result["birthday_masked"] = user["birth_date"]
    if userinfo:
        if "email_verified" not in result and type(userinfo.get("email_verified")) is bool:
            result["email_verified"] = userinfo["email_verified"]
        password = userinfo.get("pw")
        changed = creation_date(password.get("cng_at")) if isinstance(password, dict) else None
        if changed:
            result["password_changed"] = changed
        acct = userinfo.get("acct")
        created = creation_date(acct.get("created_at")) if isinstance(acct, dict) else None
        if created:
            result["registered"] = created
        verified = userinfo.get("phone_number_verified")
        if type(verified) is bool:
            result["phone_verified"] = verified
    country = (user or {}).get("country") or (userinfo or {}).get("country")
    if (isinstance(country, str) and len(country) in (2, 3) and country.isascii()
            and country.isalpha() and country.lower() != "nan"):
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
    settings, unavailable = {}, []
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
                csrf = page_csrf(page.text)
                if page.status_code != 200 or not csrf:
                    raise AccountDetailsError("Для даты рождения нужен повторный вход в Riot SDA.", 300)
                headers = _riot_api_headers(csrf)
                response = session.get(PORTAL + "/api/account/v1/user", headers=headers,
                                       timeout=(5, 15), allow_redirects=False)
                user = _response_json(response)
                if not same_owner(owner, user):
                    raise AccountDetailsError("Riot вернул сведения другого аккаунта.")
                for key, path in (("mfa", "/api/mfa/v2/factors"),
                                  ("apps", "/api/links/v1/authorizations"),
                                  ("privacy", "/api/privacy/v1/subscriptions"),
                                  ("game_pass", "/api/subscriptions/v1/game-pass-status")):
                    try:
                        response = session.get(PORTAL + path, headers=headers, timeout=(5, 15), allow_redirects=False)
                        if response.status_code == 429:
                            _response_json(response)  # Respect the same global cooldown as the base request.
                        if response.status_code == 200:
                            data = response.json()
                            if isinstance(data, list if key == "mfa" else dict):
                                settings[key] = data
                                continue
                        unavailable.append(key)
                    except (requests.RequestException, ValueError):
                        unavailable.append(key)
        if user is None and userinfo is None:
            raise AccountDetailsError("Для личных сведений нужен вход в выбранный аккаунт.")
        result = extract_details(owner, user, userinfo)
        result.update(extract_settings(settings))
        result["unavailable_sections"] = unavailable
        result["settings_version"] = 2
        return result
    except requests.RequestException:
        raise AccountDetailsError("Не удалось связаться с Riot. Сохранённые сведения доступны.") from None
