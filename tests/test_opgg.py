import json
import unittest
from unittest.mock import Mock, patch

import requests

from app.api.developer_api import DataError
from app.api.opgg import (ENDPOINT, OpggLimit, decode_profile, normalize_profile,
                          fetch_auto_rankings, fetch_opgg_rankings)


def profile():
    return {"data": {"summoner": {"game_name": "Owner", "tagline": "EUW",
        "updated_at": "2026-10-08T12:00:00Z", "league_stats": [
            {"game_type": "SOLORANKED", "tier_info": {"tier": "EMERALD", "division": 2, "lp": 48}},
            {"game_type": "FLEXRANKED", "tier_info": {"tier": None, "division": None, "lp": None}},
            {"game_type": "ARENA", "tier_info": {"tier": "GOLD", "division": 1, "lp": 90}}]}}}


def response(result=None, status=200, headers=None, request_id=1):
    return Mock(status_code=status, headers=headers or {},
                text=json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result or {}}))


class OpggTests(unittest.TestCase):
    def test_current_queues_identity_and_source_cache_timestamp(self):
        ranks = normalize_profile(profile(), "owner#euw", "EUW", 2000000000)
        self.assertEqual(set(ranks), {"solo", "flex"})
        self.assertEqual(ranks["solo"]["division"], "II")
        self.assertEqual(ranks["solo"]["lp"], 48)
        self.assertEqual(ranks["flex"]["status"], "unranked")
        self.assertEqual(ranks["solo"]["source"], "OP.GG")
        self.assertLess(ranks["solo"]["source_updated_at"], ranks["solo"]["updated_at"])
        with self.assertRaises(DataError):
            normalize_profile(profile(), "Other#EUW", "EUW", 200)

    def test_missing_malformed_or_duplicate_queue_never_means_unranked(self):
        for variant in ([], [{"game_type": "SOLORANKED", "tier_info": {}}],
                        [profile()["data"]["summoner"]["league_stats"][0]] * 2,
                        [{"game_type": "SOLORANKED", "tier_info": {"tier": "GOLD", "division": 2, "lp": True}}]):
            data = profile()
            data["data"]["summoner"]["league_stats"] = variant
            with self.subTest(variant=variant):
                ranks = normalize_profile(data, "Owner#EUW", "EUW", 200)
                self.assertEqual(ranks["solo"]["status"], "unavailable")
                self.assertEqual(ranks["flex"]["status"], "unavailable")

    def test_compact_response_supports_escaped_names_and_null_without_execution(self):
        text = ('class Result: data\nclass Data: summoner\nclass Summoner: game_name,tagline,league_stats\n'
                'class Stat: game_type,tier_info\nclass Tier: tier,division,lp\n\n'
                'Result(Data(Summoner("A \\\"name\\\"", "EUW", [Stat("FLEXRANKED", Tier(null,null,null))])))')
        data = decode_profile(text)
        self.assertEqual(data["data"]["summoner"]["game_name"], 'A "name"')
        self.assertIsNone(data["data"]["summoner"]["league_stats"][0]["tier_info"]["tier"])
        for bad in ('class Result: data\n\nResult(__import__("os").getcwd())',
                    'class Result: data\n\nResult(data=1)', 'class Result: data\n\nResult(1,2)'):
            with self.subTest(bad=bad), self.assertRaises(DataError):
                decode_profile(bad)

    @patch("app.api.opgg.requests.Session")
    def test_mcp_handshake_and_json_profile_send_only_public_identity(self, constructor):
        session = constructor.return_value.__enter__.return_value
        session.post.side_effect = [response({"protocolVersion": "2025-06-18"},
                                             headers={"Mcp-Session-Id": "public-mcp-session"}),
                                    response(status=202),
                                    response({"content": [{"type": "text", "text": json.dumps(profile())}]}, request_id=2)]
        ranks = fetch_opgg_rankings("Owner#EUW", "EUW")
        self.assertEqual(ranks["solo"]["tier"], "EMERALD")
        self.assertFalse(session.trust_env)
        self.assertEqual(session.post.call_count, 3)
        for call in session.post.call_args_list:
            self.assertEqual(call.args[0], ENDPOINT)
            self.assertFalse(call.kwargs["allow_redirects"])
        args = session.post.call_args.kwargs["json"]["params"]["arguments"]
        self.assertEqual(set(args), {"game_name", "tag_line", "region", "desired_output_fields"})
        self.assertEqual(args["region"], "EUW")

    @patch("app.api.opgg.requests.Session")
    def test_sse_transport_and_structured_content(self, constructor):
        session = constructor.return_value.__enter__.return_value
        final = response({"structuredContent": profile()}, request_id=2)
        final.headers = {"Content-Type": "text/event-stream"}
        final.text = 'event: message\r\ndata: ' + final.text + '\r\n\r\n'
        session.post.side_effect = [response({"protocolVersion": "2025-06-18"}), response(status=202), final]
        self.assertEqual(fetch_opgg_rankings("Owner#EUW", "EUW")["flex"]["status"], "unranked")

    @patch("app.api.opgg.requests.Session")
    def test_limits_redirects_and_network_errors_are_safe(self, constructor):
        session = constructor.return_value.__enter__.return_value
        session.post.return_value = response(status=429, headers={"Retry-After": "7200"})
        with self.assertRaises(OpggLimit) as caught:
            fetch_opgg_rankings("Owner#EUW", "EUW")
        self.assertEqual(caught.exception.seconds, 7200)
        session.post.return_value = response(status=302)
        with self.assertRaises(DataError):
            fetch_opgg_rankings("Owner#EUW", "EUW")
        session.post.side_effect = requests.Timeout("raw-private-network-detail")
        with self.assertRaises(DataError) as caught:
            fetch_opgg_rankings("Owner#EUW", "EUW")
        self.assertNotIn("raw-private", str(caught.exception))

    @patch("app.api.rankings.fetch_rankings")
    @patch("app.api.opgg.fetch_opgg_rankings")
    def test_tft_uses_supported_riot_route_even_when_opgg_is_limited(self, opgg, riot):
        opgg.side_effect = OpggLimit(3600)
        riot.return_value = {"ranks": {"tft": {"status": "ranked", "tier": "GOLD", "division": "I", "lp": 20}}}
        result = fetch_auto_rankings("Owner#EUW", "EUW", "tft-key")
        riot.assert_called_once_with("Owner#EUW", "EUW", "", "tft-key")
        self.assertEqual(result["ranks"]["solo"]["status"], "unavailable")
        self.assertEqual(result["ranks"]["tft"]["source"], "Riot Developer API")
        self.assertEqual(result["rank_retry_after"], 3600)


if __name__ == "__main__":
    unittest.main()
