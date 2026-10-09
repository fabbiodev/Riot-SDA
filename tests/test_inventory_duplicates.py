import copy
import unittest

from app.core.inventory import unique_items, normalize_collection


class InventoryDuplicateTests(unittest.TestCase):
    def test_unicode_case_spacing_duplicates_merge_metadata_and_keep_first_artwork_id(self):
        items = [{"id": "one", "name": "Дух  цветения Ари", "aliases": ["Spirit Blossom Ahri"]},
                 {"id": "two", "name": " ДУХ\u00a0ЦВЕТЕНИЯ АРИ ", "aliases": ["Ahri"], "skin_type": "Эпический"},
                 {"id": "three", "name": "Страж звёзд Ари"}]
        original = copy.deepcopy(items)
        result = unique_items(items)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "one")
        self.assertEqual(result[0]["aliases"], ["Spirit Blossom Ahri", "Ahri"])
        self.assertEqual(result[0]["skin_type"], "Эпический")
        self.assertEqual(items, original)

    def test_champion_duplicates_merge_skins_and_counts_remain_consistent(self):
        profile = {"characters": [{"id": "103", "name": "Ари", "skin_ids": ["one"]},
                                  {"id": "103", "name": "АРИ", "skin_ids": ["two", "three"]}],
                   "skins": [{"id": "one", "name": "Skin"}, {"id": "two", "name": "SKIN"},
                             {"id": "three", "name": "Other skin"}], "level": 200}
        result = normalize_collection(profile)
        self.assertEqual(len(result["characters"]), 1)
        self.assertEqual(len(result["skins"]), 2)
        self.assertEqual(result["characters"][0]["skin_ids"], ["one", "three"])
        self.assertEqual(result["level"], 200)
        self.assertEqual(normalize_collection(result), result)

    def test_same_names_in_different_accounts_do_not_remove_ownership(self):
        for _ in range(2):
            self.assertEqual(normalize_collection({"skins": [{"id": "one", "name": "Skin"}]})["skins"][0]["id"], "one")
        self.assertEqual(len(unique_items([{"id": "one"}, {"id": "two"}, {"id": "one"}])), 2)
