import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.api.developer_api import DataError
from app.core.skin_types import normalize_skin_types, enrich_skin, fetch_skin_types, load_skin_types
from app.core.search import item_matches


class SkinTypesTests(unittest.TestCase):
    def test_league_uses_rarity_and_inherits_for_chromas_without_price_guesses(self):
        types = normalize_skin_types("lol", {
            "103027": {"id": 103027, "rarity": "kLegendary", "chromas": [{"id": 103029}]},
            "1001": {"id": 1001, "rarity": "kNoRarity"},
            "1002": {"id": 1002, "rarity": "new-unknown", "price": 1820}})
        self.assertEqual(types["103027"]["skin_type"], "Легендарный")
        self.assertEqual(types["103029"], types["103027"])
        self.assertEqual(types["1001"]["skin_type"], "Обычный")
        self.assertNotIn("1002", types)

    def test_valorant_joins_content_tiers_for_skins_levels_and_chromas(self):
        types = normalize_skin_types("valorant", [
            {"uuid": "SKIN", "contentTierUuid": "TIER", "levels": [{"uuid": "LEVEL"}], "chromas": [{"uuid": "CHROMA"}]},
            {"uuid": "BASE", "contentTierUuid": None}],
            [{"uuid": "tier", "devName": "Premium", "displayName": "Premium Edition"}])
        self.assertEqual(types["skin"]["skin_type"], "Premium")
        self.assertEqual(types["level"], types["chroma"])
        self.assertNotIn("base", types)

    def test_enrichment_does_not_mutate_owned_inventory_and_type_is_searchable(self):
        row = {"id": "103027", "name": "Дух цветения Ари", "kind": "owned"}
        enriched = enrich_skin(row, {"103027": {"skin_type": "Легендарный", "skin_type_aliases": ["Legendary"]}})
        self.assertTrue(item_matches(enriched, "ари легендарный"))
        self.assertTrue(item_matches(enriched, "legendary"))
        self.assertNotIn("skin_type", row)
        self.assertEqual(enriched["kind"], "owned")
        self.assertEqual(enrich_skin({"id": "unknown"}, {})["skin_type"], "Тип не указан")

    def test_cache_and_public_transport_without_account_headers(self):
        with tempfile.TemporaryDirectory() as directory, patch("app.core.skin_types.APPDATA_DIR", directory):
            session = Mock()
            response = Mock(status_code=200)
            response.json.return_value = {"103027": {"id": 103027, "rarity": "kLegendary"}}
            session.get.return_value = response
            first = fetch_skin_types("lol", session)
            second = fetch_skin_types("lol", session)
            self.assertEqual(first, second)
            session.get.assert_called_once()
            self.assertFalse(session.trust_env)
            args, kwargs = session.get.call_args
            self.assertTrue(args[0].startswith("https://raw.communitydragon.org/"))
            self.assertFalse(kwargs["allow_redirects"])
            self.assertNotIn("headers", kwargs)

    def test_stale_cache_survives_failure_and_bad_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory, patch("app.core.skin_types.APPDATA_DIR", directory):
            path = Path(directory) / "lol-skin-types.json"
            stale = {"schema": 1, "updated_at": 1, "skins": {"103027": {"skin_type": "Легендарный"}}}
            path.write_text(json.dumps(stale))
            session = Mock()
            session.get.return_value = Mock(status_code=503)
            self.assertEqual(fetch_skin_types("lol", session), stale)
            path.write_text('{"schema":1,"updated_at":"bad","skins":{}}')
            self.assertEqual(load_skin_types("lol"), {})
            with self.assertRaises(DataError):
                fetch_skin_types("lol", session)
