from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

BASE_ID = 76561197960265728


def valid_steam_id(value: str) -> bool:
    return bool(re.fullmatch(r"\d{17}", value)) and BASE_ID < int(value) < BASE_ID + 2**32


@dataclass(frozen=True)
class ProfileReference:
    steam_id: str = ""
    vanity: str = ""


def parse_profile(value: str) -> ProfileReference:
    """Accept a SteamID64, a profile/inventory URL, or an explicit /id/ vanity URL."""
    value = value.strip()
    if valid_steam_id(value):
        return ProfileReference(steam_id=value)
    if value.startswith(("steamcommunity.com/", "www.steamcommunity.com/")):
        value = "https://" + value
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"steamcommunity.com", "www.steamcommunity.com"}
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Укажите SteamID64 или HTTPS-ссылку профиля на steamcommunity.com.")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] != "inventory"):
        raise ValueError("Нужна ссылка /profiles/SteamID64 или /id/имя; допускается окончание /inventory.")
    if parts[0] == "profiles" and valid_steam_id(parts[1]):
        return ProfileReference(steam_id=parts[1])
    if parts[0] == "id" and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", parts[1]):
        return ProfileReference(vanity=parts[1])
    raise ValueError("Некорректный идентификатор профиля Steam.")

