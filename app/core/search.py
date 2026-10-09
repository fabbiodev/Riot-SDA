"""Search only public account labels and collection names, never credentials."""

import unicodedata
import hashlib


def account_key(account):
    """Stable local selection key, including for legacy accounts without PUUID."""
    identity = account.get("local_id") or account.get("seed") or account.get("puuid") or account.get("name", "")
    return hashlib.sha256(str(identity).encode()).hexdigest()


def normalized(value):
    return unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")


def matches(query, *values):
    terms = normalized(query).split()
    haystack = " ".join(normalized(value) for value in values)
    return all(term in haystack for term in terms)


def account_matches(account, query):
    profiles = account.get("games", {})
    return matches(query, account.get("name"), account.get("login"),
                   *(p.get("riot_id", "") for p in profiles.values() if isinstance(p, dict)))


def item_matches(item, query):
    return matches(query, item.get("name"), item.get("owner"),
                   *item.get("aliases", []))
