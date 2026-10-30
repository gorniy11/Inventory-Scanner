from __future__ import annotations

import argparse
import json
import tempfile
import threading
from pathlib import Path

from . import __version__
from .filters import FilterSpec
from .models import GAMES


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Inventory Scanner — ознакомительный сканер Steam-инвентарей.")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--live", action="store_true", help="Включить разовую проверку публичных инвентарей вместо сохранённого примера.")
    result.add_argument("--data-dir", type=Path, help="Каталог локальных результатов; по умолчанию временный.")
    result.add_argument("--smoke", action="store_true", help=argparse.SUPPRESS)
    commands = result.add_subparsers(dest="command")
    scan = commands.add_parser("scan", help="Один публичный профиль, один проход, без повторов.")
    scan.add_argument("profile", help="SteamID64 или HTTPS-ссылка профиля.")
    scan.add_argument("--live", dest="live_scan", action="store_true", help="Разрешить эту разовую сетевую проверку.")
    scan.add_argument("--games", nargs="+", choices=tuple(GAMES), default=["cs2"], help="Игры для проверки.")
    scan.add_argument("--filter", type=Path, help="JSON-файл общих фильтров.")
    scan.add_argument("--output", type=Path, required=True, help="Результат JSON или CSV.")
    return result


def main(argv=None) -> int:
    arguments = parser()
    args = arguments.parse_args(argv)
    if args.command == "scan":
        if not (args.live or args.live_scan):
            arguments.error("Для разовой сетевой проверки укажите scan … --live.")
        from .exporting import export_items
        from .steam import InventoryError, SteamClient
        client = SteamClient()
        try:
            spec = FilterSpec.from_dict(json.loads(args.filter.read_text(encoding="utf-8-sig"))) if args.filter else FilterSpec()
            stop = threading.Event()
            profile = client.resolve(args.profile, stop)
            items = []
            for key in dict.fromkeys(args.games):
                inventory = client.inventory(profile.steam_id, GAMES[key], stop)
                items.extend(item for item in inventory.items if spec.matches(item))
                print(f"{GAMES[key].name}: {len(inventory.items)} предметов, {inventory.pages} стр.")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            count = export_items(args.output, items)
            print(f"Экспортировано совпадений: {count} → {args.output}")
            return 0
        except (ValueError, OSError, InventoryError) as error:
            print(f"Проверка не завершена: {error}")
            return 1
        except KeyboardInterrupt:
            return 130
        finally:
            client.close()
    from .sample import seed_sample
    from .gui import Application
    from .storage import Store
    with tempfile.TemporaryDirectory(prefix="inventory-scanner-") as temporary:
        directory = args.data_dir or Path(temporary)
        # Режимы используют разные базы: пример не попадут в реальную сессию.
        store = Store(directory / ("session.sqlite3" if args.live else "sample.sqlite3"))
        try:
            if not args.live:
                seed_sample(store)
            window = Application(store, demo=not args.live)
            if args.smoke:
                window.after(1200, window.close)
            window.mainloop()
        finally:
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
