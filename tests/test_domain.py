import itertools
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from sample_fixture import demo_items, seed_demo
from inventory_scanner.exporting import export_items
from inventory_scanner.filters import FilterSpec
from inventory_scanner.inspect import decode_inspect
from inventory_scanner.models import GAMES, Inventory, Item, Profile
from inventory_scanner.profiles import parse_profile
from inventory_scanner.storage import Store

OWNER = "76561198000000001"
# Контрольные векторы upstream; источник и лицензия — THIRD_PARTY_NOTICES.md.
GOLDEN = "00180720DA03280638FBEE88F90340B2026BC03C96"
MASKED = "7C6CF88390E2D17D64655C1C547D4C7844BE8B98897F3C8578145B0C64904061B1"


class ProfileTests(unittest.TestCase):
    def test_id_and_inventory_url(self):
        for value in (OWNER, f"https://steamcommunity.com/profiles/{OWNER}/inventory/#730_2_1",
                      f"steamcommunity.com/profiles/{OWNER}/"):
            self.assertEqual(parse_profile(value).steam_id, OWNER)

    def test_vanity(self):
        self.assertEqual(parse_profile("https://steamcommunity.com/id/example-user/inventory").vanity, "example-user")

    def test_reject_other_hosts_and_ambiguous_inputs(self):
        for value in ("http://steamcommunity.com/id/user", "https://evil.test/id/user",
                      "https://steamcommunity.com.evil.test/id/user", "https://user@steamcommunity.com/id/user",
                      "123", "custom-name", "https://steamcommunity.com/id/%2fetc", "https://steamcommunity.com/profiles/123"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_profile(value)


class InspectTests(unittest.TestCase):
    def test_upstream_wrapped_golden(self):
        metadata = decode_inspect("csgo_econ_action_preview " + GOLDEN)
        self.assertIsNotNone(metadata)
        self.assertAlmostEqual(metadata.float_value, .6336590647697449)
        self.assertEqual(metadata.pattern_seed, 306)

    def test_upstream_masked_golden(self):
        result = decode_inspect("steam://run/730//+csgo_econ_action_preview%20" + MASKED)
        self.assertIsNotNone(result)
        self.assertEqual(result.item_id, "46504034180")
        self.assertEqual(result.pattern_seed, 633)
        self.assertAlmostEqual(result.float_value, .3617840409)

    def test_encoded_link(self):
        link = "steam://rungame/730/76561202255233023/+csgo_econ_action_preview " + GOLDEN
        self.assertEqual(decode_inspect(link), decode_inspect(quote(link)))

    def test_invalid_checksum_and_nonpayload(self):
        for value in (GOLDEN[:-1]+"0", "00", "859F6DF769919C54", "not-hex"):
            self.assertIsNone(decode_inspect("csgo_econ_action_preview " + value))

    def test_legacy_stays_unknown(self):
        self.assertIsNone(decode_inspect(f"steam://rungame/730/76561202255233023/+csgo_econ_action_preview S{OWNER}A1D123"))


class FilterTests(unittest.TestCase):
    def item(self, **kwargs):
        base = dict(owner_id=OWNER, app_id=730, asset_id="1", name="AK-47 | Slate", market_name="AK-47 | Slate",
                    tradable=True, marketable=True, tags={"Rarity": ["Classified", "rarity_mythical"]},
                    descriptions=["A synthetic description"], float_value=.1, pattern_seed=0)
        return Item(**{**base, **kwargs})

    def test_defaults_and_casefold(self):
        self.assertTrue(FilterSpec().matches(self.item()))
        self.assertTrue(FilterSpec(include_names=("ak-47",)).matches(self.item()))
        self.assertTrue(FilterSpec(tags={"rarity": "CLASSIFIED"}).matches(self.item()))

    def test_exact_and_exclusions(self):
        self.assertFalse(FilterSpec(include_names=("AK-47",), name_mode="exact").matches(self.item()))
        self.assertTrue(FilterSpec(include_names=("ak-47 | slate",), name_mode="exact").matches(self.item()))
        self.assertFalse(FilterSpec(exclude_names=("slate",)).matches(self.item()))

    def test_ranges_include_boundaries_and_zero(self):
        self.assertTrue(FilterSpec(float_min=.1, float_max=.1, seed_min=0, seed_max=0).matches(self.item()))
        self.assertFalse(FilterSpec(float_max=.099).matches(self.item()))
        self.assertFalse(FilterSpec(seed_min=1).matches(self.item()))

    def test_unknown_is_not_zero(self):
        unknown = self.item(float_value=None, pattern_seed=None)
        self.assertFalse(FilterSpec(float_max=.01).matches(unknown))
        self.assertTrue(FilterSpec(float_max=.01, include_unknown_metadata=True).matches(unknown))

    def test_combination_truth_table(self):
        for app, tradable, marketable, wear, quality in itertools.product((730, 440), (True, False), (True, False), (None, .05, .2), ("Rare", "Classified")):
            row = self.item(app_id=app, tradable=tradable, marketable=marketable, float_value=wear, tags={"Rarity": [quality]})
            spec = FilterSpec(app_ids=(730,), tradable=True, marketable=True, tags={"Rarity": "Classified"}, float_max=.1)
            expected = app == 730 and tradable and marketable and quality == "Classified" and wear is not None and wear <= .1
            self.assertEqual(spec.matches(row), expected)

    def test_description_and_newness(self):
        self.assertTrue(FilterSpec(description="SYNTHETIC").matches(self.item()))
        self.assertFalse(FilterSpec(new_only=True).matches(self.item()))
        self.assertTrue(FilterSpec(new_only=True).matches(self.item(is_new=True)))

    def test_invalid_filter_configuration(self):
        for data in ({"float_min": .9, "float_max": .1}, {"float_max": float("nan")}, {"seed_min": True},
                     {"seed_max": 1001}, {"tradable": "yes"}, {"app_ids": [1]}, {"include_names": "AK"},
                     {"tags": {"Rarity": ""}}, {"unknown_field": True}, {"app_ids": [{}]},
                     {"float_max": 10**1000}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                FilterSpec.from_dict(data)

    def test_round_trip(self):
        value = FilterSpec(app_ids=(730,), include_names=("AK",), float_max=.1)
        self.assertEqual(FilterSpec.from_dict(json.loads(json.dumps(value.to_dict()))), value)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name)/"inventory.sqlite3")
        self.store.save_profile(Profile(OWNER, "Тест"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def snapshot(self, assets=("1",), app="cs2"):
        rows = [Item(OWNER, GAMES[app].app_id, asset, "Test", "Test") for asset in assets]
        return Inventory(OWNER, GAMES[app], rows, 1, len(rows), .1)

    def test_first_baseline_and_new_asset(self):
        self.assertFalse(self.store.commit(self.snapshot(), now=10)[0].is_new)
        rows = self.store.commit(self.snapshot(("1", "2")), now=20)
        self.assertEqual([row.is_new for row in rows], [False, True])
        self.assertEqual(rows[0].first_seen, 10)
        self.assertEqual(rows[1].first_seen, 20)

    def test_error_keeps_full_snapshot(self):
        self.store.commit(self.snapshot())
        self.store.record_error(OWNER, 730, "ошибка")
        self.assertEqual(self.store.query(FilterSpec())[2], 1)
        self.assertEqual(self.store.scans()[(OWNER, 730)]["count"], 1)

    def test_incomplete_commit_rejected(self):
        self.store.commit(self.snapshot())
        broken = self.snapshot(("2",))
        broken = Inventory(OWNER, GAMES["cs2"], broken.items, 1, 2, .1)
        with self.assertRaises(ValueError):
            self.store.commit(broken)
        self.assertEqual(self.store.query(FilterSpec())[0][0].asset_id, "1")

    def test_game_and_owner_isolation(self):
        self.store.commit(self.snapshot())
        self.store.commit(self.snapshot(app="tf2"))
        self.store.commit(self.snapshot(("2",)))
        self.assertEqual(self.store.query(FilterSpec())[2], 2)

    def test_metadata_import_and_future_snapshots(self):
        self.store.commit(self.snapshot())
        self.store.import_metadata([dict(owner_id=OWNER, app_id=730, asset_id="1", float_value=.0123, pattern_seed=0)])
        self.assertEqual(self.store.query(FilterSpec(float_max=.02))[1], 1)
        self.assertEqual(self.store.commit(self.snapshot())[0].float_value, .0123)

    def test_metadata_validation_is_atomic(self):
        rows = [dict(owner_id=OWNER, app_id=730, asset_id="1", float_value=.1),
                dict(owner_id=OWNER, app_id=730, asset_id="2", float_value=float("inf"))]
        with self.assertRaises(ValueError):
            self.store.import_metadata(rows)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM enrichment").fetchone()[0], 0)

    def test_remove_profile_removes_local_rows(self):
        self.store.commit(self.snapshot())
        self.store.remove_profile(OWNER)
        self.assertEqual(self.store.query(FilterSpec())[2], 0)

    def test_view_limit_does_not_limit_match_count(self):
        self.store.commit(self.snapshot(tuple(str(n) for n in range(1, 101))))
        rows, matched, total = self.store.query(FilterSpec(), limit=3)
        self.assertEqual((len(rows), matched, total), (3, 100, 100))

    def test_demo_and_exports(self):
        seed_demo(self.store)
        rows, matched, total = self.store.query(FilterSpec())
        self.assertEqual(total, 40)
        output = Path(self.tmp.name)/"result.json"
        self.assertEqual(export_items(output, rows), matched)
        self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))), 40)

    def test_csv_formula_sanitized(self):
        row = demo_items("demo-1", "cs2")[0]
        row.name = "=HYPERLINK(\"https://example.test\")"
        output = Path(self.tmp.name)/"result.csv"
        export_items(output, [row])
        self.assertIn("'=HYPERLINK", output.read_text(encoding="utf-8-sig"))
