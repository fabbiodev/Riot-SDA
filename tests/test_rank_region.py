import unittest
from unittest.mock import Mock, patch

import requests

from app.api.developer_api import DataError
from app.api.rankings import normalize_platform, rank_region, region_from_userinfo, resolve_rank_region


class RankRegionTests(unittest.TestCase):
    def test_riot_platforms_are_normalized_without_guessing_from_tag_or_country(self):
        for platform, region in (("euw1", "EUW"), ("EUN1", "EUNE"), ("NA1", "NA"),
                                 ("LA1", "LAN"), ("OC1", "OCE"), ("RU", "RU")):
            self.assertEqual(normalize_platform(platform), region)
        self.assertEqual(rank_region({"name": "Owner#RU", "country": "RUS"}), "")
        self.assertEqual(normalize_platform("UNKNOWN"), "")

    def test_verified_active_platform_is_available_before_game_profile(self):
        account = {"puuid": "owner", "name": "Owner#Custom"}
        self.assertEqual(region_from_userinfo(account, {"sub": "OWNER", "lol": {"cpid": "EUW1"}}), "EUW")
        self.assertEqual(region_from_userinfo(account, {"sub": "owner", "lol_region": [
            {"cpid": "NA1", "active": False}, {"cpid": "EUN1", "active": True}]}), "EUNE")
        for data in ({"sub": "other", "lol": {"cpid": "RU"}},
                     {"sub": "owner", "lol": {"cpid": "EUW1", "active": False}},
                     {"sub": "owner", "lol_region": [{"cpid": "RU", "active": True},
                                                       {"cpid": "EUW1", "active": True}]}):
            with self.assertRaises(DataError):
                region_from_userinfo(account, data)

    @patch("app.api.rankings.requests.Session")
    def test_remote_identity_corrects_legacy_default_and_never_uses_local_client(self, constructor):
        session = constructor.return_value.__enter__.return_value
        session.get.return_value = Mock(status_code=200)
        session.get.return_value.json.return_value = {"sub": "owner", "lol": {"cpid": "EUW1"}}
        account = {"puuid": "owner", "name": "Owner#RU", "api_routes": {"lol": "RU"}}
        self.assertEqual(resolve_rank_region(account, "private-token"), "EUW")
        session.get.assert_called_once_with("https://auth.riotgames.com/userinfo",
                                           headers={"Authorization": "Bearer private-token"},
                                           timeout=(5, 15), allow_redirects=False)
        self.assertFalse(session.trust_env)
        session.get.return_value.json.return_value["sub"] = "other"
        with self.assertRaises(DataError):
            resolve_rank_region(account, "private-token")

    @patch("app.api.rankings.requests.Session")
    def test_manual_server_and_cached_server_work_without_session_or_game(self, constructor):
        account = {"league_region": "EUW", "api_routes": {"lol": "KR", "lol_manual": True}}
        self.assertEqual(resolve_rank_region(account, "private-token"), "KR")
        constructor.assert_not_called()
        account["api_routes"]["lol_manual"] = False
        self.assertEqual(resolve_rank_region(account), "EUW")
        session = constructor.return_value.__enter__.return_value
        session.get.side_effect = requests.ConnectionError("sensitive response")
        self.assertEqual(resolve_rank_region(account, "private-token"), "EUW")
        with self.assertRaises(DataError) as error:
            resolve_rank_region({"puuid": "owner"}, "private-token")
        self.assertNotIn("sensitive", str(error.exception))


if __name__ == "__main__":
    unittest.main()
