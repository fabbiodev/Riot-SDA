import unittest
from unittest.mock import Mock

from app.api.developer_api import DeveloperApi, DataError
from app.api.game_clients import league_collection, require_owner, entitlement_ids, normalize_val_collection, AGENTS_TYPE, valorant_collection
from app.core.search import account_matches, item_matches


class GameDataTests(unittest.TestCase):
    def test_search_unicode_login_alias_and_never_credentials(self):
        account = {"name": "Ёжик#RU", "login": "real_login", "seed": "PRIVATE-SEED", "sso": {"ssid": "COOKIE"}}
        self.assertTrue(account_matches(account, "ежик #ru"))
        self.assertTrue(account_matches(account, "REAL_LOGIN"))
        self.assertFalse(account_matches(account, "PRIVATE-SEED"))
        self.assertFalse(account_matches(account, "COOKIE"))
        self.assertTrue(item_matches({"name": "Дух цветения", "owner": "Ари", "aliases": ["Spirit Blossom Ahri"]}, "SPIRIT AHRI"))

    def test_developer_key_only_in_header_and_escaped_riot_id(self):
        session = Mock()
        response = Mock(status_code=200)
        response.json.return_value = {"puuid": "encrypted-id", "gameName": "a/b", "tagLine": "EUW"}
        session.get.return_value = response
        api = DeveloperApi("fake-key", session)
        api.account("a/b#EUW", "europe")
        url = session.get.call_args.args[0]
        self.assertIn("a%2Fb/EUW", url)
        self.assertNotIn("fake-key", url)
        self.assertEqual(session.get.call_args.kwargs["headers"], {"X-Riot-Token": "fake-key"})
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        with self.assertRaises(DataError):
            api.get("malicious.example", "/anything")

    def test_rate_limit_and_expired_key_are_safe(self):
        session = Mock()
        session.get.return_value = Mock(status_code=429, headers={"Retry-After": "45"})
        with self.assertRaisesRegex(DataError, "45"):
            DeveloperApi("fake-key", session).get("ru", "/lol/summoner/v4/example")
        session.get.return_value = Mock(status_code=403)
        with self.assertRaisesRegex(DataError, "истёк") as raised:
            DeveloperApi("fake-key", session).get("ru", "/example")
        self.assertNotIn("fake-key", str(raised.exception))

    def test_official_profile_keeps_auth_puuid_separate(self):
        api = DeveloperApi("fake-key")
        api.get = Mock(side_effect=[{"puuid": "encrypted", "gameName": "Owner", "tagLine": "RU"},
                                    {"summonerLevel": 247, "profileIconId": 5}])
        profile = api.profile("Owner#RU", "lol", "RU")
        self.assertEqual(profile["level"], 247)
        self.assertEqual(profile["api_puuid"], "encrypted")
        self.assertNotIn("puuid", profile)
        self.assertNotIn("characters", profile)
        self.assertNotIn("skins", profile)

    def test_val_official_profile_does_not_invent_level_or_owned_items(self):
        api = DeveloperApi("fake-key")
        api.get = Mock(return_value={"puuid": "encrypted", "gameName": "Owner", "tagLine": "RU"})
        profile = api.profile("Owner#RU", "valorant", "EU")
        self.assertNotIn("level", profile)
        self.assertNotIn("characters", profile)
        self.assertNotIn("region", profile)
        self.assertEqual(profile["api_region"], "EU")

    def test_wrong_local_owner_rejected_before_inventory_read(self):
        client = Mock()
        client.get.return_value = {"puuid": "other-owner"}
        with self.assertRaisesRegex(DataError, "другой аккаунт"):
            league_collection({"name": "Owner#RU", "puuid": "owner-id"}, client)
        self.assertEqual(client.get.call_count, 1)
        with self.assertRaisesRegex(DataError, "нет ID"):
            require_owner({}, "anything")

    def test_league_owned_only_but_skins_for_unowned_champions_are_kept(self):
        summoner = {"puuid": "owner", "summonerId": 10, "summonerLevel": 40, "gameName": "Owner", "tagLine": "RU"}
        skin = lambda sid, owned, base=False: {"id": sid, "name": f"Skin {sid}", "isBase": base, "ownership": {"owned": owned}}
        client = Mock()
        client.get.side_effect = [summoner, [
            {"id": 1, "name": "One", "ownership": {"owned": True}, "skins": [skin(1000, True, True), skin(1001, True), skin(1002, False)]},
            {"id": 2, "name": "Two", "ownership": {"owned": False}, "freeToPlay": True, "skins": [skin(2001, True)]}],
            summoner, {"region": "RU"}]
        result = league_collection({"name": "Owner#RU", "puuid": "owner"}, client)
        self.assertEqual(len(result["characters"]), 1)
        self.assertEqual({s["id"] for s in result["skins"]}, {"1001", "2001"})
        self.assertEqual(result["characters"][0]["skin_ids"], ["1001"])

    def test_account_switch_during_read_rejected(self):
        client = Mock()
        client.get.side_effect = [{"puuid": "owner", "summonerId": 10}, [], {"puuid": "another"}]
        with self.assertRaisesRegex(DataError, "другой аккаунт"):
            league_collection({"name": "Owner#RU", "puuid": "owner"}, client)

    def test_val_upgrades_deduplicate_and_variants_only_owned(self):
        skin = {"id": "reaver", "name": "Reaver Vandal", "owner": "Vandal", "base": False}
        metadata = {"agents": {"jett": {"id": "jett", "name": "Jett", "starter": True},
                               "omen": {"id": "omen", "name": "Omen", "starter": False}},
                    "levels": {"level1": skin, "level2": skin, "base": {"id": "standard", "base": True}},
                    "chromas": {"red": {"skin_id": "reaver", "name": "Red"},
                                "blue": {"skin_id": "reaver", "name": "Blue"}}}
        agents, skins = normalize_val_collection({"omen"}, {"level1", "level2", "base"}, {"red"}, metadata)
        self.assertEqual(len(agents), 2)
        self.assertEqual(len(skins), 1)
        self.assertEqual(skins[0]["variants"], ["Red"])
        with self.assertRaises(DataError):
            normalize_val_collection(set(), {"unknown-new-level"}, set(), metadata)

    def test_entitlement_response_shapes(self):
        self.assertEqual(entitlement_ids({"Entitlements": [{"ItemID": "TEST"}]}, AGENTS_TYPE), {"test"})
        self.assertEqual(entitlement_ids({"EntitlementsByTypes": [
            {"ItemTypeID": "other", "Entitlements": [{"ItemID": "wrong"}]},
            {"ItemTypeID": AGENTS_TYPE, "Entitlements": [{"ItemID": "correct"}]}]}, AGENTS_TYPE), {"correct"})

    def test_val_client_transport_uses_owner_tokens_only_on_riot_host(self):
        tokens = {"subject": "owner", "accessToken": "fake-bearer", "token": "fake-entitlement"}
        client = Mock()
        client.get.side_effect = [tokens, {"game": {"productId": "valorant", "phase": "running", "version": "release-shipping-123",
            "launchConfiguration": {"arguments": ["-ares-deployment=eu"]}}},
            {"puuid": "owner", "game_name": "Owner", "game_tag": "RU"}, tokens]
        session = Mock()
        bodies = [{"Subject": "owner", "Progress": {"Level": 162}},
                  {"Entitlements": [{"ItemID": "omen"}]},
                  {"Entitlements": [{"ItemID": "skin-level"}]}, {"Entitlements": []}]
        responses = []
        for body in bodies:
            response = Mock(status_code=200)
            response.json.return_value = body
            responses.append(response)
        session.get.side_effect = responses
        metadata = {"agents": {"omen": {"id": "omen", "name": "Omen", "starter": False}},
                    "levels": {"skin-level": {"id": "skin", "name": "Reaver Vandal", "owner": "Vandal", "base": False}}, "chromas": {}}
        result = valorant_collection({"name": "Owner#RU", "puuid": "owner"}, client, metadata, session)
        self.assertEqual(result["level"], 162)
        self.assertEqual(result["region"], "EU")
        self.assertEqual(len(result["skins"]), 1)
        for call in session.get.call_args_list:
            self.assertTrue(call.args[0].startswith("https://pd.eu.a.pvp.net/"))
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertNotIn("verify", call.kwargs)
            self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer fake-bearer")
        self.assertNotIn("fake-bearer", str(result))
        self.assertNotIn("fake-entitlement", str(result))

    def test_wrong_val_owner_does_not_make_remote_request(self):
        client, remote = Mock(), Mock()
        client.get.return_value = {"subject": "different"}
        with self.assertRaisesRegex(DataError, "другой аккаунт"):
            valorant_collection({"name": "Owner#RU", "puuid": "owner"}, client, {}, remote)
        remote.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
