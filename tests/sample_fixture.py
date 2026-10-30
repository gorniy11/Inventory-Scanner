"""Синтетический набор для знакомства с интерфейсом без сетевых запросов."""
from __future__ import annotations

import time

from inventory_scanner.models import GAMES, Inventory, Item, Profile
from inventory_scanner.storage import Store


def demo_items(owner: str, key: str) -> list[Item]:
    names = {
        "cs2": ["AK-47 | Slate (Minimal Wear)", "M4A1-S | Printstream (Factory New)",
                "AWP | Atheris (Field-Tested)", "USP-S | Cortex (Well-Worn)", "Recoil Case"],
        "dota2": ["Blade of the Silent Tempest", "Mantle of the Celestial Guardian", "Crimson Warden",
                  "Ancient Conqueror", "Treasure of the Forgotten City"],
        "rust": ["Tempered AK47", "Arctic Wolf", "Blackout Hoodie", "Whiteout Facemask", "High Quality Crate"],
        "tf2": ["The Sandvich", "The Original", "The Team Captain", "The Gunslinger", "Mann Co. Supply Crate Key"],
    }
    rows = []
    for n, name in enumerate(names[key]):
        tags = {"Type": ["Container" if n == 4 else "Weapon" if n % 2 == 0 else "Cosmetic"],
                "Quality": [["Normal", "Strange", "Unique"][n % 3]]}
        if key == "cs2" and n < 4:
            tags["Exterior"] = [["Minimal Wear", "Factory New", "Field-Tested", "Well-Worn"][n]]
            tags["Rarity"] = [["Classified", "Covert", "Restricted", "Classified"][n]]
        elif key == "dota2":
            tags["Rarity"] = [["Rare", "Mythical", "Immortal", "Legendary", "Rare"][n]]
        row = Item(owner, GAMES[key].app_id, str(10000+n), name, name,
                   type_name=tags["Type"][0], tradable=n != 3, marketable=n != 4, tags=tags,
                   descriptions=["Синтетический предмет демонстрационного набора.", "Данные не связаны с реальными аккаунтами."])
        if key == "cs2" and n < 3:
            row.float_value = [.084721, .008315, .241506][n]
            row.pattern_seed = [417, 18, 829][n]
            row.metadata_source = "demo"
        rows.append(row)
    return rows


def seed_demo(store: Store) -> None:
    for index, label in enumerate(("Север · демопрофиль", "Вектор · демопрофиль")):
        profile = Profile(f"demo-{index+1}", label, demo=True)
        store.save_profile(profile)
        for key, game in GAMES.items():
            rows = demo_items(profile.steam_id, key)
            store.commit(Inventory(profile.steam_id, game, rows, 1, len(rows), .24), now=time.time()-86400)


class DemoClient:
    def __init__(self, **kwargs):
        pass

    def inventory(self, owner, game, stop):
        stop.wait(.18)
        rows = demo_items(owner, game.key)
        return Inventory(owner, game, rows, 1, len(rows), .18)

    def close(self):
        pass
