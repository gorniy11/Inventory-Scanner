import contextlib
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from inventory_scanner.__main__ import main
from inventory_scanner.filters import FilterSpec
from inventory_scanner.models import GAMES, Inventory, Item, Profile
from inventory_scanner.scanner import EventBus, ScanOptions, Scanner
from inventory_scanner.steam import InventoryError
from inventory_scanner.storage import Store

OWNER = "76561198000000001"


class FakeClient:
    calls = []
    lock = threading.Lock()

    def __init__(self, **kwargs):
        self.gate = kwargs.get("gate")

    def inventory(self, owner, game, stop):
        with self.lock:
            self.calls.append((owner, game.key))
        items = [Item(owner, game.app_id, "1", "Example", "Example")]
        return Inventory(owner, game, items, 1, 1, .01)

    def resolve(self, value, stop):
        return Profile(OWNER, "Тест")

    def close(self):
        pass


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/"session.sqlite3")
        self.store.save_profile(Profile(OWNER, "Тест"))
        self.events = EventBus()
        FakeClient.calls = []
        self.scanner = Scanner(self.store, self.events, FakeClient)

    def tearDown(self):
        self.scanner.cancel()
        for thread in self.scanner.threads:
            thread.join(2)
        self.store.close()
        self.temp.cleanup()

    def run_scan(self, options=ScanOptions()):
        self.scanner.start(options, FilterSpec())
        for thread in self.scanner.threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_one_pass_all_four_games(self):
        self.run_scan()
        self.assertEqual(set(FakeClient.calls), {(OWNER, key) for key in GAMES})
        self.assertEqual(self.scanner.stats()["successes"], 4)

    def test_repeat_scan_disabled(self):
        self.run_scan()
        with self.assertRaises(ValueError):
            self.scanner.start(ScanOptions(), FilterSpec())
        self.assertEqual(len(FakeClient.calls), 4)

    def test_can_scan_previously_unselected_game_once(self):
        self.run_scan(ScanOptions(("cs2",)))
        self.run_scan(ScanOptions(("tf2",)))
        self.assertEqual(len(FakeClient.calls), 2)

    def test_disabled_profile_is_not_scanned(self):
        self.store.save_profile(Profile("76561198000000002", "Выключен", enabled=False))
        self.run_scan(ScanOptions(("cs2",)))
        self.assertEqual(FakeClient.calls, [(OWNER, "cs2")])

    def test_three_profile_limit(self):
        for n in range(2, 5):
            self.store.save_profile(Profile(str(int(OWNER)+n), str(n)))
        with self.assertRaises(ValueError):
            self.scanner.start(ScanOptions(), FilterSpec())

    def test_removed_profile_still_counts_towards_session_limit(self):
        self.run_scan(ScanOptions(("cs2",)))
        self.store.remove_profile(OWNER)
        for n in range(2, 5):
            self.store.save_profile(Profile(str(int(OWNER)+n), str(n)))
        with self.assertRaises(ValueError):
            self.scanner.start(ScanOptions(), FilterSpec())

    def test_failed_scan_is_not_automatically_retried(self):
        class Failure(FakeClient):
            def inventory(self, owner, game, stop):
                super().inventory(owner, game, stop)
                raise InventoryError("Синтетическая ошибка", status=429)
        self.scanner.client_factory = Failure
        self.run_scan(ScanOptions(("cs2",)))
        self.assertEqual(len(FakeClient.calls), 1)
        self.assertEqual(self.scanner.stats()["failures"], 1)
        with self.assertRaises(ValueError):
            self.scanner.start(ScanOptions(("cs2",)), FilterSpec())

    def test_mailbox_bounded_and_refresh_coalesced(self):
        bus = EventBus(10)
        for n in range(1000):
            bus.put("log", {"message": str(n)})
            bus.put("refresh")
        values = bus.drain(1000)
        self.assertLessEqual(len(values), 10)
        self.assertLessEqual(sum(kind == "refresh" for kind, _ in values), 1)

    def test_invalid_games(self):
        with self.assertRaises(ValueError):
            ScanOptions(("unknown",)).validate()


class CliTests(unittest.TestCase):
    def test_version_does_not_start_network(self):
        with patch("requests.sessions.Session.request") as network, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as exited:
                main(["--version"])
            self.assertEqual(exited.exception.code, 0)
            network.assert_not_called()

    def test_live_requires_explicit_flag(self):
        with patch("requests.sessions.Session.request") as network, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exited:
                main(["scan", OWNER, "--output", "unused.json"])
            self.assertEqual(exited.exception.code, 2)
            network.assert_not_called()

    def test_scan_exports_once(self):
        with tempfile.TemporaryDirectory() as folder, patch("inventory_scanner.steam.SteamClient", FakeClient):
            output = Path(folder)/"example.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["scan", OWNER, "--live", "--games", "cs2", "tf2", "--output", str(output)])
            self.assertEqual(code, 0)
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
