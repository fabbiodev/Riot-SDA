"""Stable, per-collection deduplication by displayed name."""

import copy
import unicodedata


def name_key(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def unique_items(items):
    result, seen = [], {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = name_key(item.get("name"))
        key = ("name", name) if name else ("id", str(item["id"])) if item.get("id") else ("row", len(result))
        if key not in seen:
            seen[key] = len(result)
            row = copy.deepcopy(item)
            if row.get("id") is not None:
                row["id"] = str(row["id"])
            result.append(row)
            continue
        target = result[seen[key]]
        for field, value in item.items():
            if field in ("aliases", "variants", "skin_ids") and isinstance(value, list):
                if not isinstance(target.get(field), list):
                    target[field] = []
                values = target[field]
                keys = {name_key(v) for v in values}
                for entry in value:
                    if name_key(entry) not in keys:
                        values.append(copy.deepcopy(entry))
                        keys.add(name_key(entry))
            elif field not in target or target[field] in (None, "", []):
                target[field] = copy.deepcopy(value)
    return result


def normalize_collection(profile):
    result = dict(profile)
    skins = profile.get("skins")
    canonical_ids = {}
    if isinstance(skins, list):
        result["skins"] = unique_items(skins)
        by_name = {name_key(s.get("name")): str(s["id"]) for s in result["skins"] if s.get("name") and s.get("id")}
        canonical_ids = {str(s["id"]): by_name[name_key(s["name"])] for s in skins
                         if isinstance(s, dict) and s.get("id") and name_key(s.get("name")) in by_name}
    if isinstance(profile.get("characters"), list):
        result["characters"] = unique_items(profile["characters"])
        for character in result["characters"]:
            if isinstance(character.get("skin_ids"), list):
                character["skin_ids"] = list(dict.fromkeys(canonical_ids.get(str(sid), str(sid)) for sid in character["skin_ids"]))
    return result
