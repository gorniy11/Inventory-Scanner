from __future__ import annotations

import html
import json
import math
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from .inspect import decode_inspect
from .models import Game, Inventory, Item, Profile
from .profiles import parse_profile, valid_steam_id


class ScanCancelled(Exception):
    pass


class InventoryError(Exception):
    def __init__(self, message: str, *, retry_after: float = 0, status: int = 0):
        super().__init__(message)
        self.retry_after = retry_after
        self.status = status


def retry_after(value: str | None, now: float | None = None) -> float:
    try:
        seconds = float(value or "")
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value or "")
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            seconds = stamp.timestamp() - (time.time() if now is None else now)
        except (ValueError, TypeError, OverflowError):
            return 60
    return max(0, seconds) if math.isfinite(seconds) else 60


class RequestGate:
    """One shared request budget across all workers, including pagination and 429 pauses."""
    def __init__(self, interval: float = 2):
        if not math.isfinite(interval) or interval < 0:
            raise ValueError("Интервал запросов должен быть конечным неотрицательным числом.")
        self.interval = interval
        self.lock = threading.Lock()
        self.next_at = 0.0
        self.requests = 0
        self.expires_at = time.monotonic() + 15 * 60

    def wait(self, stop: threading.Event) -> None:
        while not stop.is_set():
            with self.lock:
                if self.requests >= 50 or time.monotonic() >= self.expires_at:
                    raise InventoryError("Лимит ознакомительной сессии исчерпан: 50 запросов или 15 минут.")
                delay = self.next_at - time.monotonic()
                if delay <= 0:
                    self.next_at = time.monotonic() + self.interval
                    self.requests += 1
                    return
            stop.wait(min(delay, .2))
        raise ScanCancelled()

    def defer(self, seconds: float) -> None:
        with self.lock:
            self.next_at = max(self.next_at, time.monotonic() + max(0, seconds))


def validate_proxy(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https", "socks4", "socks5", "socks5h"} or not parts.hostname:
            raise ValueError()
        if not parts.port or parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError()
    except ValueError:
        raise ValueError("Прокси: http(s)://host:port или socks4/5(h)://host:port.") from None
    return value


def plain_text(value: Any) -> str:
    return html.unescape(re.sub(r"<[^>]*>", "", str(value or ""))).strip()


def parse_page(body: Any, owner: str, game: Game) -> list[Item]:
    if not isinstance(body, dict) or body.get("success") not in (1, True):
        raise InventoryError("Steam не подтвердил успешную загрузку инвентаря.")
    if body.get("total_inventory_count") == 0 and not body.get("assets"):
        return []
    assets, descriptions = body.get("assets"), body.get("descriptions")
    if not isinstance(assets, list) or not isinstance(descriptions, list):
        raise InventoryError("Неполный инвентарь: отсутствуют экземпляры или описания.")
    try:
        index = {(str(row["classid"]), str(row.get("instanceid", "0"))): row for row in descriptions}
        items = []
        for asset in assets:
            asset_id = str(asset["assetid"])
            if not re.fullmatch(r"[1-9]\d*", asset_id) or int(asset["appid"]) != game.app_id:
                raise ValueError("Invalid asset identity")
            if str(asset.get("contextid", game.context_id)) != game.context_id:
                raise ValueError("Unexpected context")
            description = index[(str(asset["classid"]), str(asset.get("instanceid", "0")))]
            tags: dict[str, list[str]] = {}
            for tag in description.get("tags", []):
                category = str(tag.get("category") or tag.get("localized_category_name") or "Other")
                values = tags.setdefault(category, [])
                for value in (tag.get("localized_tag_name"), tag.get("internal_name")):
                    if value and str(value) not in values:
                        values.append(str(value))
            inspect = ""
            for action in description.get("actions", []):
                link = str(action.get("link", ""))
                if "csgo_econ_action_preview" in link and game.app_id == 730:
                    inspect = link.replace("%owner_steamid%", owner).replace("%assetid%", asset_id)
                    break
            amount = int(asset.get("amount", 1))
            if amount < 1:
                raise ValueError("Invalid stack size")
            item = Item(
                owner_id=owner, app_id=game.app_id, asset_id=asset_id,
                name=plain_text(description.get("market_hash_name") or description.get("name") or "Unnamed item"),
                market_name=str(description.get("market_hash_name") or description.get("name") or ""),
                type_name=plain_text(description.get("type")), amount=amount,
                tradable=description.get("tradable") in (1, True),
                marketable=description.get("marketable") in (1, True), tags=tags,
                descriptions=[plain_text(row.get("value")) for row in description.get("descriptions", [])],
                inspect_link=inspect,
            )
            metadata = decode_inspect(inspect) if inspect else None
            if metadata is not None and metadata.item_id in (None, asset_id):
                item.float_value = metadata.float_value
                item.pattern_seed = metadata.pattern_seed
                item.metadata_source = "inspect payload"
            items.append(item)
        return items
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise InventoryError("Данные инвентаря неполны или противоречивы; прежний снимок сохранён.") from error


class SteamClient:
    def __init__(self, gate: RequestGate | None = None, proxy: str = "", session=None):
        self.gate = gate if gate is not None else RequestGate()
        self.proxy = validate_proxy(proxy)
        self.session = session if session is not None else requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": "InventoryScanner/1.0", "Accept-Language": "en-US"})

    def close(self) -> None:
        self.session.close()

    def _read(self, path: str, params: dict, stop: threading.Event, limit: int = 32 * 1024 * 1024) -> bytes:
        self.gate.wait(stop)
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else {}
        try:
            with self.session.get(
                "https://steamcommunity.com" + path, params=params, proxies=proxies,
                timeout=(5, 15), allow_redirects=False, stream=True,
            ) as response:
                if response.status_code == 429:
                    delay = retry_after(response.headers.get("Retry-After"))
                    self.gate.defer(delay)
                    raise InventoryError(f"Steam ограничил запросы; сервер требует паузу {delay:.0f} с.",
                                         retry_after=delay, status=429)
                if response.status_code in (401, 403):
                    raise InventoryError("Инвентарь закрыт или доступ отклонён.", status=response.status_code)
                if response.status_code != 200:
                    raise InventoryError(f"Steam вернул HTTP {response.status_code}.", status=response.status_code)
                body = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    if stop.is_set():
                        raise ScanCancelled()
                    body.extend(chunk)
                    if len(body) > limit:
                        raise InventoryError("Ответ Steam превысил допустимый размер.")
                return bytes(body)
        except requests.RequestException as error:
            # Exception text can contain proxy credentials. Expose the type only.
            raise InventoryError(f"Сетевая ошибка ({type(error).__name__}).") from None

    def resolve(self, value: str, stop: threading.Event) -> Profile:
        reference = parse_profile(value)
        if reference.steam_id:
            return Profile(reference.steam_id, reference.steam_id)
        body = self._read(f"/id/{reference.vanity}/", {"xml": 1}, stop, limit=1024 * 1024)
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise InventoryError("Steam вернул неподдерживаемый документ профиля.")
        try:
            tree = ET.fromstring(body)
            steam_id = tree.findtext("steamID64", "")
            if not valid_steam_id(steam_id):
                raise ValueError()
            return Profile(steam_id, tree.findtext("steamID") or reference.vanity)
        except (ET.ParseError, ValueError):
            raise InventoryError("Не удалось определить SteamID по пользовательской ссылке.") from None

    def inventory(self, owner: str, game: Game, stop: threading.Event,
                  on_progress: Callable[[int, int], None] | None = None) -> Inventory:
        if not valid_steam_id(owner):
            raise ValueError("Требуется корректный SteamID64.")
        started = time.monotonic()
        collected: dict[str, Item] = {}
        seen_cursors: set[str] = set()
        cursor = None
        total = None
        pages = 0
        while True:
            if stop.is_set():
                raise ScanCancelled()
            params: dict[str, str | int] = {"l": "english", "count": 2000}
            if cursor:
                params["start_assetid"] = cursor
            try:
                body = json.loads(self._read(f"/inventory/{owner}/{game.app_id}/{game.context_id}/", params, stop))
            except (ValueError, UnicodeDecodeError):
                raise InventoryError("Steam вернул некорректный JSON.") from None
            rows = parse_page(body, owner, game)
            declared = body.get("total_inventory_count")
            if declared is not None:
                if isinstance(declared, bool) or not isinstance(declared, int) or declared < 0:
                    raise InventoryError("Steam вернул некорректное количество предметов.")
                if total is not None and declared != total:
                    raise InventoryError("Инвентарь изменился во время загрузки; полный снимок не сохранён.")
                total = declared
            for row in rows:
                if row.asset_id in collected:
                    raise InventoryError("Steam повторил экземпляр на нескольких страницах; снимок не сохранён.")
                collected[row.asset_id] = row
            pages += 1
            if on_progress:
                on_progress(pages, len(collected))
            more = body.get("more_items", False)
            if more not in (0, 1, False, True):
                raise InventoryError("Steam вернул некорректный признак продолжения выдачи.")
            if not more:
                if total is not None and len(collected) != total:
                    raise InventoryError(f"Неполный инвентарь: получено {len(collected)} из {total} экземпляров.")
                if stop.is_set():
                    raise ScanCancelled()
                return Inventory(owner, game, list(collected.values()), pages, total, time.monotonic() - started)
            cursor = str(body.get("last_assetid", ""))
            if not cursor.isdigit() or cursor in seen_cursors or not rows or pages >= 1000:
                raise InventoryError("Постраничная выдача Steam остановилась; снимок не сохранён.")
            seen_cursors.add(cursor)
