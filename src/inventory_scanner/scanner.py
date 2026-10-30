from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass #
from typing import Callable

from .filters import FilterSpec
from .models import GAMES
from .steam import InventoryError, RequestGate, ScanCancelled, SteamClient, validate_proxy
from .storage import Store

MAX_PROFILES = 3


class EventBus:
    """Ограниченный почтовый ящик интерфейса с объединением перерисовок."""
    def __init__(self, capacity: int = 600):
        self.capacity = capacity
        self.lock = threading.Lock()
        self.pending: OrderedDict[object, tuple[str, dict]] = OrderedDict()
        self.serial = 0

    def put(self, kind: str, data: dict | None = None) -> None:
        with self.lock:
            self.serial += 1
            key = kind if kind in {"refresh", "stats", "view"} else self.serial
            self.pending[key] = kind, data or {}
            while len(self.pending) > self.capacity:
                self.pending.popitem(last=False)

    def drain(self, limit: int = 100) -> list[tuple[str, dict]]:
        with self.lock:
            return [self.pending.popitem(last=False)[1] for _ in range(min(limit, len(self.pending)))]


@dataclass
class ScanOptions:
    games: tuple[str, ...] = tuple(GAMES)
    proxy: str = ""

    def validate(self) -> None:
        if not self.games or any(key not in GAMES for key in self.games):
            raise ValueError("Выберите хотя бы одну поддерживаемую игру.")
        validate_proxy(self.proxy)


class Scanner:
    """Один проход; каждый инвентарь доступен для проверки один раз за сессию."""
    def __init__(self, store: Store, events: EventBus, client_factory: Callable | None = None):
        self.store, self.events = store, events
        self.client_factory = client_factory or SteamClient
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.stop.set()
        self.threads: list[threading.Thread] = []
        self.jobs: OrderedDict[tuple[str, str], dict] = OrderedDict()
        self.attempted: set[tuple[str, str]] = set()
        self.profile_history: set[str] = set()
        self.gate = RequestGate(interval=2)
        self.started_at = self.finished_at = 0.0
        self.successes = self.failures = 0
        self.spec = FilterSpec()

    def active(self) -> bool:
        return any(thread.is_alive() for thread in self.threads)

    def start(self, options: ScanOptions, spec: FilterSpec) -> None:
        options.validate()
        with self.lock:
            if self.active():
                raise ValueError("Предыдущие запросы ещё завершаются.")
            profiles = [profile for profile in self.store.profiles() if profile.enabled]
            owners = self.profile_history | {profile.steam_id for profile in profiles}
            if not profiles or len(owners) > MAX_PROFILES:
                raise ValueError("Ознакомительная сессия поддерживает от 1 до 3 профилей.")
            self.jobs = OrderedDict(
                ((profile.steam_id, key), dict(profile=profile, game=GAMES[key], busy=False, done=False))
                for profile in profiles for key in dict.fromkeys(options.games)
                if (profile.steam_id, key) not in self.attempted
            )
            if not self.jobs:
                raise ValueError("Эти инвентари уже проверялись. В ознакомительной сессии повторы отключены.")
            self.profile_history = owners
            self.spec = spec
            self.started_at = time.monotonic()
            self.finished_at = 0
            self.successes = self.failures = 0
            self.stop.clear()
            self.threads = [threading.Thread(target=self._worker, args=(options,), daemon=True,
                                             name=f"Inventory-{n+1}") for n in range(min(2, len(self.jobs)))]
            for thread in self.threads:
                thread.start()
        self.events.put("log", {"message": "Начат разовый ознакомительный проход."})

    def cancel(self) -> None:
        self.stop.set()

    def stats(self) -> dict:
        with self.lock:
            end = self.finished_at or time.monotonic()
            return dict(successes=self.successes, failures=self.failures,
                        elapsed=max(0., end-self.started_at) if self.started_at else 0,
                        attempted=len(self.attempted), total=len(self.jobs))

    def _claim(self):
        with self.lock:
            for key, job in self.jobs.items():
                if not job["busy"] and not job["done"]:
                    job["busy"] = True
                    self.attempted.add(key)
                    return key, job
        return None

    def _worker(self, options: ScanOptions) -> None:
        client = None
        try:
            client = self.client_factory(gate=self.gate, proxy=options.proxy)
            while not self.stop.is_set():
                claim = self._claim()
                if claim is None:
                    break
                key, job = claim
                profile, game = job["profile"], job["game"]
                try:
                    inventory = client.inventory(profile.steam_id, game, self.stop)
                    if self.stop.is_set():
                        raise ScanCancelled()
                    items = self.store.commit(inventory)
                    with self.lock:
                        matches = sum(self.spec.matches(item) for item in items)
                        self.successes += 1
                    self.events.put("log", {"message": f"{profile.label} · {game.name}: {len(items)} предметов, "
                                                       f"{inventory.pages} стр., {inventory.elapsed:.2f} с. Совпадений: {matches}."})
                    if matches:
                        self.events.put("matches", {"count": matches})
                except ScanCancelled:
                    break
                except InventoryError as error:
                    self.store.record_error(profile.steam_id, game.app_id, str(error))
                    with self.lock:
                        self.failures += 1
                    self.events.put("log", {"message": f"{profile.label} · {game.name}: {error}"})
                except Exception as error:
                    with self.lock:
                        self.failures += 1
                    self.events.put("log", {"message": f"Ошибка обработки: {type(error).__name__}."})
                finally:
                    with self.lock:
                        self.jobs[key].update(busy=False, done=True)
                    self.events.put("refresh")
        finally:
            if client is not None:
                client.close()
            with self.lock:
                others = [thread for thread in self.threads if thread is not threading.current_thread()]
                if not any(thread.is_alive() for thread in others):
                    self.finished_at = time.monotonic()
            self.events.put("refresh")
