from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from .filters import FilterSpec
from .models import Inventory, Item, Profile


class Store:
    """Atomic full snapshots plus durable first-seen history; failed scans never replace items."""
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA cache_size=-2048")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS items(owner TEXT, app INTEGER, asset TEXT, payload TEXT NOT NULL,
                PRIMARY KEY(owner,app,asset));
            CREATE TABLE IF NOT EXISTS seen(owner TEXT, app INTEGER, asset TEXT, stamp REAL,
                PRIMARY KEY(owner,app,asset));
            CREATE TABLE IF NOT EXISTS scans(owner TEXT, app INTEGER, stamp REAL, count INTEGER,
                pages INTEGER, elapsed REAL, error TEXT, PRIMARY KEY(owner,app));
            CREATE TABLE IF NOT EXISTS enrichment(owner TEXT, app INTEGER, asset TEXT, payload TEXT NOT NULL,
                PRIMARY KEY(owner,app,asset));
        """)
        self.db.commit()

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def profiles(self) -> list[Profile]:
        with self.lock:
            return [Profile(**json.loads(row[0])) for row in self.db.execute("SELECT payload FROM profiles ORDER BY id")]

    def save_profile(self, profile: Profile) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO profiles VALUES(?,?)", (profile.steam_id, json.dumps(asdict(profile))))

    def remove_profile(self, owner: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM profiles WHERE id=?", (owner,))
            for table in ("items", "seen", "scans", "enrichment"):
                self.db.execute(f"DELETE FROM {table} WHERE owner=?", (owner,))

    def setting(self, key: str, default=None):
        with self.lock:
            row = self.db.execute("SELECT payload FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def save_setting(self, key: str, value) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value)))

    def commit(self, inventory: Inventory, now: float | None = None) -> list[Item]:
        now = time.time() if now is None else now
        owner, app = inventory.owner_id, inventory.game.app_id
        if any(item.owner_id != owner or item.app_id != app for item in inventory.items):
            raise ValueError("В снимке есть предмет из другого инвентаря.")
        if len({item.asset_id for item in inventory.items}) != len(inventory.items):
            raise ValueError("В снимке повторяются экземпляры предметов.")
        if inventory.reported_total is not None and len(inventory.items) != inventory.reported_total:
            raise ValueError("Снимок инвентаря неполон.")
        with self.lock, self.db:
            baseline = self.db.execute("SELECT 1 FROM scans WHERE owner=? AND app=? AND count>=0", (owner, app)).fetchone()
            previous = dict(self.db.execute("SELECT asset,stamp FROM seen WHERE owner=? AND app=?", (owner, app)))
            enrichment = {asset: json.loads(payload) for asset, payload in self.db.execute(
                "SELECT asset,payload FROM enrichment WHERE owner=? AND app=?", (owner, app))}
            for item in inventory.items:
                item.first_seen = previous.get(item.asset_id, now)
                item.last_seen = now
                item.is_new = bool(baseline and item.asset_id not in previous)
                if item.metadata_source == "unavailable" and item.asset_id in enrichment:
                    self._enrich(item, enrichment[item.asset_id])
            self.db.execute("DELETE FROM items WHERE owner=? AND app=?", (owner, app))
            self.db.executemany("INSERT INTO items VALUES(?,?,?,?)", [
                (owner, app, item.asset_id, json.dumps(item.to_dict(), ensure_ascii=False)) for item in inventory.items])
            self.db.executemany("INSERT OR IGNORE INTO seen VALUES(?,?,?,?)", [
                (owner, app, item.asset_id, item.first_seen) for item in inventory.items])
            self.db.execute("INSERT OR REPLACE INTO scans VALUES(?,?,?,?,?,?,?)",
                            (owner, app, now, len(inventory.items), inventory.pages, inventory.elapsed, ""))
        return inventory.items

    def record_error(self, owner: str, app: int, message: str) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO scans VALUES(?,?,?,-1,0,0,?) ON CONFLICT(owner,app) DO UPDATE SET error=?",
                            (owner, app, time.time(), message[:500], message[:500]))

    def scans(self) -> dict[tuple[str, int], dict]:
        with self.lock:
            return {(owner, app): dict(stamp=stamp, count=count, pages=pages, elapsed=elapsed, error=error)
                    for owner, app, stamp, count, pages, elapsed, error in self.db.execute("SELECT * FROM scans")}

    def query(self, spec: FilterSpec, limit: int | None = 2000, quick: str = "") -> tuple[list[Item], int, int]:
        result: list[Item] = []
        total = matched = 0
        with self.lock:
            cursor = self.db.execute("""SELECT i.payload FROM items i JOIN profiles p ON p.id=i.owner
                ORDER BY json_extract(i.payload,'$.first_seen') DESC, i.owner,i.app,i.asset""")
            for row in cursor:
                item = Item.from_dict(json.loads(row[0]))
                total += 1
                if spec.matches(item) and quick.casefold() in item.name.casefold():
                    matched += 1
                    if limit is None or len(result) < limit:
                        result.append(item)
        return result, matched, total

    @staticmethod
    def _enrich(item: Item, row: dict) -> None:
        if row.get("float_value") is not None:
            item.float_value = row["float_value"]
        if row.get("pattern_seed") is not None:
            item.pattern_seed = row["pattern_seed"]
        item.metadata_source = "imported metadata"

    def import_metadata(self, rows: Iterable[dict]) -> int:
        validated = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Записи метаданных должны быть JSON-объектами.")
            owner, asset = str(row.get("owner_id", "")), str(row.get("asset_id", ""))
            app = row.get("app_id")
            if not owner or not asset.isdigit() or app != 730 or isinstance(app, bool):
                raise ValueError("Для CS2-метаданных нужны owner_id, app_id=730 и asset_id.")
            value, seed = row.get("float_value"), row.get("pattern_seed")
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or not 0 <= value <= 1):
                raise ValueError("Некорректный float_value: нужно конечное число от 0 до 1.")
            if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 1000):
                raise ValueError("Некорректный pattern_seed: нужно целое число от 0 до 1000.")
            if value is None and seed is None:
                raise ValueError("Укажите float_value или pattern_seed.")
            validated.append((owner, app, asset, dict(float_value=value, pattern_seed=seed)))
        with self.lock, self.db:
            for owner, app, asset, data in validated:
                old = self.db.execute("SELECT payload FROM enrichment WHERE owner=? AND app=? AND asset=?",
                                      (owner, app, asset)).fetchone()
                merged = json.loads(old[0]) if old else {}
                merged.update({key: value for key, value in data.items() if value is not None})
                self.db.execute("INSERT OR REPLACE INTO enrichment VALUES(?,?,?,?)", (owner, app, asset, json.dumps(merged)))
                found = self.db.execute("SELECT payload FROM items WHERE owner=? AND app=? AND asset=?",
                                        (owner, app, asset)).fetchone()
                if found:
                    item = Item.from_dict(json.loads(found[0]))
                    self._enrich(item, merged)
                    self.db.execute("UPDATE items SET payload=? WHERE owner=? AND app=? AND asset=?",
                                    (json.dumps(item.to_dict()), owner, app, asset))
        return len(validated)
