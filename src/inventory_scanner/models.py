from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Game:
    key: str
    name: str
    app_id: int
    context_id: str = "2"


GAMES = {
    "cs2": Game("cs2", "Counter-Strike 2", 730),
    "dota2": Game("dota2", "Dota 2", 570),
    "rust": Game("rust", "Rust", 252490),
    "tf2": Game("tf2", "Team Fortress 2", 440),
}
GAMES_BY_ID = {game.app_id: game for game in GAMES.values()}


@dataclass(frozen=True)
class Profile:
    steam_id: str
    label: str
    enabled: bool = True
    demo: bool = False

    @property
    def url(self) -> str:
        return "" if self.demo else f"https://steamcommunity.com/profiles/{self.steam_id}/"


@dataclass
class Item:
    owner_id: str
    app_id: int
    asset_id: str
    name: str
    market_name: str
    type_name: str = ""
    amount: int = 1
    tradable: bool = False
    marketable: bool = False
    tags: dict[str, list[str]] = field(default_factory=dict)
    descriptions: list[str] = field(default_factory=list)
    inspect_link: str = ""
    float_value: float | None = None
    pattern_seed: int | None = None
    metadata_source: str = "unavailable"
    first_seen: float = 0
    last_seen: float = 0
    is_new: bool = False

    @property
    def key(self) -> tuple[str, int, str]:
        return self.owner_id, self.app_id, self.asset_id

    @property
    def game(self) -> Game:
        return GAMES_BY_ID[self.app_id]

    @property
    def inventory_url(self) -> str:
        if self.owner_id.startswith("demo-"):
            return ""
        return f"https://steamcommunity.com/profiles/{self.owner_id}/inventory/#{self.app_id}_2_{self.asset_id}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Item:
        return cls(**data)


@dataclass(frozen=True)
class Inventory:
    owner_id: str
    game: Game
    items: list[Item]
    pages: int
    reported_total: int | None
    elapsed: float

