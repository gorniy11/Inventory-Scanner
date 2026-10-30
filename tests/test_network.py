import json
import threading
import unittest
from unittest.mock import Mock

import requests

from inventory_scanner.models import GAMES
from inventory_scanner.steam import (
    InventoryError, RequestGate, ScanCancelled, SteamClient, parse_page, retry_after, validate_proxy,
)

OWNER = "76561198000000001"


def page(assets=("1",), *, game="cs2", total=None, more=False):
    return dict(success=1, total_inventory_count=len(assets) if total is None else total, more_items=more,
                last_assetid=assets[-1] if assets else "0",
                assets=[dict(assetid=value, classid="1", instanceid="0", appid=GAMES[game].app_id,
                             contextid="2", amount="1") for value in assets],
                descriptions=[dict(classid="1", instanceid="0", name="Test Item", tradable=1, marketable=1,
                                   tags=[dict(category="Rarity", localized_tag_name="Rare", internal_name="rarity_rare")],
                                   descriptions=[dict(value="<b>Описание</b>")])])


class Response:
    def __init__(self, body, status=200, headers=None):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def iter_content(self, _size):
        yield self.body


def client(responses):
    session = Mock(headers={})
    session.get.side_effect = responses
    return SteamClient(gate=RequestGate(0), session=session), session


class PaginationTests(unittest.TestCase):
    def test_all_four_games(self):
        for key, game in GAMES.items():
            with self.subTest(game=key):
                api, session = client([Response(page(game=key))])
                result = api.inventory(OWNER, game, threading.Event())
                self.assertEqual(len(result.items), 1)
                self.assertIn(f"/{game.app_id}/2/", session.get.call_args.args[0])
                self.assertEqual(session.get.call_args.kwargs["params"]["l"], "english")

    def test_three_pages_exact_assets(self):
        responses = [Response(page(("1", "2"), total=5, more=True)), Response(page(("3", "4"), total=5, more=True)),
                     Response(page(("5",), total=5))]
        api, session = client(responses)
        result = api.inventory(OWNER, GAMES["cs2"], threading.Event())
        self.assertEqual({item.asset_id for item in result.items}, {"1", "2", "3", "4", "5"})
        self.assertEqual(result.pages, 3)
        self.assertEqual([call.kwargs["params"].get("start_assetid") for call in session.get.call_args_list], [None, "2", "4"])
        self.assertTrue(all(response.closed for response in responses))

    def test_short_last_page_is_checked_against_total(self):
        api, _ = client([Response(page(total=2))])
        with self.assertRaises(InventoryError):
            api.inventory(OWNER, GAMES["cs2"], threading.Event())

    def test_duplicate_assets_rejected(self):
        api, _ = client([Response(page(("1",), total=2, more=True)), Response(page(("1",), total=2))])
        with self.assertRaises(InventoryError):
            api.inventory(OWNER, GAMES["cs2"], threading.Event())

    def test_changed_count_rejected(self):
        api, _ = client([Response(page(total=2, more=True)), Response(page(("2",), total=3))])
        with self.assertRaises(InventoryError):
            api.inventory(OWNER, GAMES["cs2"], threading.Event())

    def test_missing_description(self):
        body = page()
        body["descriptions"] = []
        with self.assertRaises(InventoryError):
            parse_page(body, OWNER, GAMES["cs2"])

    def test_wrong_game_or_context_rejected(self):
        for key, value in (("appid", 570), ("contextid", "1")):
            body = page()
            body["assets"][0][key] = value
            with self.assertRaises(InventoryError):
                parse_page(body, OWNER, GAMES["cs2"])

    def test_empty_inventory(self):
        api, _ = client([Response(dict(success=1, total_inventory_count=0))])
        self.assertEqual(api.inventory(OWNER, GAMES["cs2"], threading.Event()).items, [])

    def test_cancel_before_request(self):
        api, session = client([])
        stop = threading.Event()
        stop.set()
        with self.assertRaises(ScanCancelled):
            api.inventory(OWNER, GAMES["cs2"], stop)
        session.get.assert_not_called()

    def test_malformed_json_is_an_error(self):
        api, _ = client([Response(b"<html>Oops</html>")])
        with self.assertRaises(InventoryError):
            api.inventory(OWNER, GAMES["cs2"], threading.Event())

    def test_instance_descriptions_not_confused(self):
        body = page(("1", "2"))
        body["assets"][1]["instanceid"] = "2"
        body["descriptions"].append(dict(classid="1", instanceid="2", name="Different", tradable=0))
        rows = parse_page(body, OWNER, GAMES["cs2"])
        self.assertEqual([item.name for item in rows], ["Test Item", "Different"])
        self.assertFalse(rows[1].tradable)


class NetworkPolicyTests(unittest.TestCase):
    def test_rate_limit_defers_shared_gate_without_retry(self):
        api, session = client([Response({}, 429, {"Retry-After": "90"})])
        with self.assertRaises(InventoryError) as caught:
            api.inventory(OWNER, GAMES["cs2"], threading.Event())
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.retry_after, 90)
        self.assertGreater(api.gate.next_at, 0)
        self.assertEqual(session.get.call_count, 1)

    def test_private_inventory(self):
        api, _ = client([Response({}, 403)])
        with self.assertRaises(InventoryError) as caught:
            api.inventory(OWNER, GAMES["cs2"], threading.Event())
        self.assertEqual(caught.exception.status, 403)

    def test_network_error_does_not_expose_proxy_credentials(self):
        api, session = client([])
        session.get.side_effect = requests.exceptions.ProxyError("http://user:secret@proxy:80")
        with self.assertRaises(InventoryError) as caught:
            api.inventory(OWNER, GAMES["cs2"], threading.Event())
        self.assertNotIn("secret", str(caught.exception))

    def test_no_redirects_or_environment_proxy(self):
        api, session = client([Response({}, 302)])
        with self.assertRaises(InventoryError):
            api.inventory(OWNER, GAMES["cs2"], threading.Event())
        self.assertFalse(session.trust_env)
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    def test_response_size_limited(self):
        api, _ = client([Response(b"12345")])
        with self.assertRaises(InventoryError):
            api._read("/id/example/", {}, threading.Event(), limit=4)

    def test_fifty_request_budget(self):
        gate = RequestGate(0)
        for _ in range(50):
            gate.wait(threading.Event())
        with self.assertRaises(InventoryError):
            gate.wait(threading.Event())

    def test_expired_session(self):
        gate = RequestGate(0)
        gate.expires_at = 0
        with self.assertRaises(InventoryError):
            gate.wait(threading.Event())

    def test_manual_proxy_schemes(self):
        for scheme in ("http", "https", "socks4", "socks5", "socks5h"):
            self.assertEqual(validate_proxy(f"{scheme}://host:8080"), f"{scheme}://host:8080")
        for value in ("ftp://host:80", "host:80", "http://host:99999", "http://host:80/path"):
            with self.assertRaises(ValueError):
                validate_proxy(value)

    def test_retry_after_invalid_and_numeric(self):
        self.assertEqual(retry_after("120"), 120)
        self.assertEqual(retry_after("invalid"), 60)
        self.assertEqual(retry_after("inf"), 60)

    def test_vanity_resolves_without_api_key(self):
        api, session = client([Response(f"<profile><steamID64>{OWNER}</steamID64><steamID>Пример</steamID></profile>".encode())])
        profile = api.resolve("https://steamcommunity.com/id/example/", threading.Event())
        self.assertEqual(profile.steam_id, OWNER)
        self.assertEqual(profile.label, "Пример")
        self.assertEqual(session.get.call_args.kwargs["params"], {"xml": 1})
