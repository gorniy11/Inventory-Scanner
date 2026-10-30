from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from .models import GAMES_BY_ID, Item


@dataclass(frozen=True)
class FilterSpec:
    app_ids: tuple[int, ...] = ()
    include_names: tuple[str, ...] = ()
    exclude_names: tuple[str, ...] = ()
    name_mode: str = "contains"
    description: str = ""
    tradable: bool | None = None
    marketable: bool | None = None
    tags: dict[str, str] = field(default_factory=dict)
    float_min: float | None = None
    float_max: float | None = None
    seed_min: int | None = None
    seed_max: int | None = None
    include_unknown_metadata: bool = False
    new_only: bool = False

    def __post_init__(self) -> None:
        if any(not isinstance(app, int) or isinstance(app, bool) or app not in GAMES_BY_ID for app in self.app_ids):
            raise ValueError("Выберите одну из четырёх поддерживаемых игр.")
        if self.name_mode not in {"contains", "exact"}:
            raise ValueError("Режим названия: contains или exact.")
        for value in (self.include_names, self.exclude_names):
            if not isinstance(value, (tuple, list)) or len(value) > 1000 or any(
                not isinstance(text, str) or not text.strip() or len(text) > 300 for text in value
            ):
                raise ValueError("Допускается до 1000 непустых названий длиной до 300 символов.")
        for low, high, maximum, integer in (
            (self.float_min, self.float_max, 1, False),
            (self.seed_min, self.seed_max, 1000, True),
        ):
            for value in (low, high):
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not 0 <= value <= maximum
                    or not math.isfinite(value)
                    or (integer and not isinstance(value, int))
                ):
                    raise ValueError("Float: от 0 до 1; pattern seed: целое число от 0 до 1000.")
            if low is not None and high is not None and low > high:
                raise ValueError("Минимум диапазона не может превышать максимум.")
        if not isinstance(self.tags, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or not k.strip() or not v.strip()
            for k, v in self.tags.items()
        ):
            raise ValueError("Теги задаются непустыми парами категория=значение.")
        if not isinstance(self.description, str):
            raise ValueError("Описание должно быть текстом.")
        for value in (self.tradable, self.marketable):
            if value is not None and not isinstance(value, bool):
                raise ValueError("Доступность: true, false или null.")
        if not isinstance(self.new_only, bool) or not isinstance(self.include_unknown_metadata, bool):
            raise ValueError("Флажки фильтра должны иметь значения true или false.")

    def matches(self, item: Item) -> bool:
        if self.app_ids and item.app_id not in self.app_ids:
            return False
        name = item.name.casefold()
        if self.include_names:
            if self.name_mode == "exact":
                allowed = name in {text.strip().casefold() for text in self.include_names}
            else:
                allowed = any(text.strip().casefold() in name for text in self.include_names)
            if not allowed:
                return False
        if any(text.strip().casefold() in name for text in self.exclude_names):
            return False
        if self.description.casefold() not in "\n".join(item.descriptions).casefold():
            return False
        if self.tradable is not None and item.tradable != self.tradable:
            return False
        if self.marketable is not None and item.marketable != self.marketable:
            return False
        if self.new_only and not item.is_new:
            return False
        categories = {key.casefold(): {value.casefold() for value in values} for key, values in item.tags.items()}
        if any(value.strip().casefold() not in categories.get(key.strip().casefold(), set()) for key, value in self.tags.items()):
            return False
        for value, low, high in (
            (item.float_value, self.float_min, self.float_max),
            (item.pattern_seed, self.seed_min, self.seed_max),
        ):
            if low is None and high is None:
                continue
            if value is None:
                if not self.include_unknown_metadata:
                    return False
            elif (low is not None and value < low) or (high is not None and value > high):
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FilterSpec:
        if not isinstance(data, dict):
            raise ValueError("Фильтр должен быть JSON-объектом.")
        fields = dict(data)
        for key in ("app_ids", "include_names", "exclude_names"):
            if key in fields:
                if not isinstance(fields[key], (list, tuple)):
                    raise ValueError(f"{key} должно быть списком.")
                fields[key] = tuple(fields[key])
        try:
            return cls(**fields)
        except TypeError as error:
            raise ValueError("В фильтре есть неподдерживаемые поля.") from error
