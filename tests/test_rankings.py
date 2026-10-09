import unittest
from unittest.mock import Mock, patch

from app.api.developer_api import DataError
from app.api.rankings import fetch_rankings, normalize_entries, merge_rankings, rank_text


def entry(queue, tier="PLATINUM", division="II", lp=48):
    return {"queueType": queue, "tier": tier, "rank": division, "leaguePoints": lp}


class RankingTests(unittest.TestCase):
    def test_queue_mapping_excludes_double_up_and_hyper_roll(self):
        entries = [entry("RANKED_SOLO_5x5"), entry("RANKED_FLEX_SR", "EMERALD", "IV", 12),
                   entry("RANKED_TFT", "DIAMOND", "I", 77),
                   {"queueType": "RANKED_TFT_TURBO", "ratedTier": "BLUE"},
                   entry("RANKED_TFT_DOUBLE_UP", "GOLD", "III", 10)]
        ranks = normalize_entries(entries, ("solo", "flex", "tft"), "EUW", 100)
        self.assertEqual(rank_text(ranks["solo"]), "Платина II")
        self.assertEqual(rank_text(ranks["flex"]), "Изумруд IV")
        self.assertEqual(ranks["tft"]["lp"], 77)
        self.assertEqual(ranks["tft"]["region"], "EUW")

    def test_absent_queue_is_unranked_only_after_successful_response(self):
        ranks = normalize_entries([], ("solo", "flex"), "RU", 100)
        self.assertEqual(rank_text(ranks["solo"]), "Без ранга")
        self.assertEqual(rank_text({}), "Не загружено")
        self.assertEqual(rank_text({"status": "unavailable"}), "Недоступно")
        for response in ({}, None, [None], [{}], [entry("RANKED_SOLO_5x5", lp=True)],
                         [entry("RANKED_SOLO_5x5", division=None)],
                         [entry("RANKED_SOLO_5x5"), entry("RANKED_SOLO_5x5")]):
            with self.subTest(response=response), self.assertRaises(DataError):
                normalize_entries(response, ("solo",), "RU", 100)

    def test_apex_tiers_have_no_division(self):
        for tier in ("MASTER", "GRANDMASTER", "CHALLENGER"):
            rank = normalize_entries([entry("RANKED_TFT", tier, "I", 810)], ("tft",), "RU", 100)["tft"]
            self.assertEqual(rank["division"], "")
            self.assertNotIn(" I", rank_text(rank))

    @patch("app.api.rankings.DeveloperApi")
    def test_product_keys_resolve_separate_puuids_and_current_routes(self, constructor):
        lol, tft = Mock(), Mock()
        constructor.side_effect = [lol, tft]
        lol.account.return_value = {"puuid": "lol/encrypted"}
        tft.account.return_value = {"puuid": "tft/encrypted"}
        lol.get.return_value = [entry("RANKED_SOLO_5x5")]
        tft.get.return_value = [entry("RANKED_TFT", "DIAMOND", "I", 77)]
        data = fetch_rankings("Owner#EUW", "EUW", "lol-key", "tft-key")
        lol.account.assert_called_once_with("Owner#EUW", "europe")
        tft.account.assert_called_once_with("Owner#EUW", "europe")
        lol.get.assert_called_once_with("euw1", "/lol/league/v4/entries/by-puuid/lol%2Fencrypted")
        tft.get.assert_called_once_with("euw1", "/tft/league/v1/by-puuid/tft%2Fencrypted")
        self.assertEqual(data["ranks"]["tft"]["lp"], 77)
        self.assertEqual(data["ranks"]["flex"]["status"], "unranked")
        self.assertNotIn("puuid", data)
        self.assertNotIn("lol-key", str(data))
        self.assertNotIn("tft-key", str(data))

    @patch("app.api.rankings.DeveloperApi")
    def test_one_product_failure_does_not_lose_other_product(self, constructor):
        lol, tft = Mock(), Mock()
        constructor.side_effect = [lol, tft]
        lol.account.side_effect = DataError("Ключ истёк")
        tft.account.return_value = {"puuid": "encrypted"}
        tft.get.return_value = []
        ranks = fetch_rankings("Owner#RU", "RU", "lol-key", "tft-key")["ranks"]
        self.assertEqual(ranks["solo"]["status"], "unavailable")
        self.assertEqual(ranks["flex"]["status"], "unavailable")
        self.assertEqual(ranks["tft"]["status"], "unranked")
        lol.get.assert_not_called()

    @patch("app.api.rankings.DeveloperApi")
    def test_missing_key_does_not_make_requests(self, constructor):
        ranks = fetch_rankings("Owner#RU", "RU", "", "")["ranks"]
        self.assertEqual(ranks["tft"]["status"], "missing_key")
        self.assertEqual(ranks["solo"]["status"], "missing_key")
        constructor.assert_not_called()

    def test_failed_refresh_keeps_snapshot_and_success_clears_error(self):
        snapshot = normalize_entries([entry("RANKED_SOLO_5x5")], ("solo", "flex"), "RU", 100)
        merged = merge_rankings(snapshot, {"solo": {"status": "unavailable", "error": "Лимит API"}})
        self.assertEqual(merged["solo"]["lp"], 48)
        self.assertEqual(merged["solo"]["updated_at"], 100)
        self.assertEqual(merged["solo"]["refresh_error"], "Лимит API")
        self.assertNotIn("refresh_error", snapshot["solo"])
        fresh = normalize_entries([], ("solo", "flex"), "RU", 200)
        merged = merge_rankings(merged, fresh)
        self.assertEqual(merged["solo"]["status"], "unranked")
        self.assertNotIn("refresh_error", merged["solo"])
