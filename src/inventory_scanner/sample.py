"""Сохранённый пример реальной публичной выдачи Steam; без сетевых запросов."""
from __future__ import annotations

import json
import time
from datetime import datetime
from importlib.resources import files

from .models import GAMES, Inventory, Item, Profile
from .steam import InventoryError, ScanCancelled
from .storage import Store


def snapshot_metadata() -> dict:
    text = files("inventory_scanner").joinpath("resources/public_inventory.json").read_text(encoding="utf-8")
    return json.loads(text)


def seed_sample(store: Store) -> None:
    data = snapshot_metadata()
    profile = data["profile"]
    store.save_profile(Profile(profile["steam_id"], profile["label"]))
    stamp = datetime.fromisoformat(data["captured_at"]).timestamp()
    for row in data["inventories"]:
        items = [Item.from_dict(value) for value in row["items"]]
        game = GAMES[row["game"]]
        store.commit(Inventory(profile["steam_id"], game, items, row["pages"], len(items), 0), now=stamp)


class SnapshotClient:
    def __init__(self, **kwargs):
        self.data = snapshot_metadata()

    def inventory(self, owner, game, stop):
        if stop.is_set():
            raise ScanCancelled()
        started = time.monotonic()
        for row in self.data["inventories"]:
            if self.data["profile"]["steam_id"] == owner and row["game"] == game.key:
                items = [Item.from_dict(value) for value in row["items"]]
                return Inventory(owner, game, items, row["pages"], len(items), time.monotonic()-started)
        raise InventoryError("В сохранённом примере нет этой игры. Разовая сетевая проверка доступна с --live.")

    def close(self):
        pass
