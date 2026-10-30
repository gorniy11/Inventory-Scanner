import tempfile
import unittest
from pathlib import Path

from inventory_scanner.filters import FilterSpec
from inventory_scanner.sample import seed_sample, snapshot_metadata
from inventory_scanner.storage import Store


class PublicSampleTests(unittest.TestCase):
    def test_sample_has_provenance_and_real_profile(self):
        data = snapshot_metadata()
        self.assertEqual(data["profile"]["steam_id"], "76561197960435530")
        self.assertEqual(data["profile_url"], "https://steamcommunity.com/id/RobinWalker/")
        self.assertTrue(data["captured_at"])
        self.assertEqual(len(data["inventories"]), 3)

    def test_sample_has_unique_actual_asset_keys(self):
        data = snapshot_metadata()
        keys = []
        for inventory in data["inventories"]:
            self.assertGreaterEqual(inventory["source_total"], len(inventory["items"]))
            for row in inventory["items"]:
                self.assertTrue(row["asset_id"].isdigit())
                self.assertEqual(row["owner_id"], data["profile"]["steam_id"])
                self.assertNotEqual(row["metadata_source"], "demo")
                keys.append((row["owner_id"], row["app_id"], row["asset_id"]))
        self.assertEqual(len(keys), len(set(keys)))

    def test_sample_loads_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder)/"example.sqlite3")
            try:
                seed_sample(store)
                expected = sum(len(row["items"]) for row in snapshot_metadata()["inventories"])
                self.assertEqual(store.query(FilterSpec())[2], expected)
            finally:
                store.close()
