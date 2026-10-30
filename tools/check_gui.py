"""Офлайн-проверка настоящего Tk-окна; внешние запросы запрещены."""
import tempfile
import time
import tkinter as tk
from pathlib import Path
from unittest.mock import patch

from inventory_scanner.sample import seed_sample, snapshot_metadata #
from inventory_scanner.gui import Application
from inventory_scanner.storage import Store


def check():
    with tempfile.TemporaryDirectory() as directory, patch(
        "requests.sessions.Session.request", side_effect=AssertionError("Сеть в GUI-тесте запрещена")
    ) as network:
        store = Store(Path(directory)/"demo.sqlite3")
        seed_sample(store)
        window = Application(store, demo=True)
        sample = snapshot_metadata()
        rows = [row for inv in sample["inventories"] for row in inv["items"]]
        errors = []
        window.report_callback_exception = lambda *args: errors.append(str(args))
        def spin(seconds=.5):
            deadline = time.monotonic()+seconds
            while time.monotonic() < deadline:
                window.update()
                time.sleep(.01)
        try:
            spin()
            assert len(window.item_tree.get_children()) == len(rows)
            window.filter_vars["game"].set("Counter-Strike 2")
            window.filter_vars["float_max"].set("0,1")
            window.apply_filters()
            spin()
            assert len(window.item_tree.get_children()) == sum(row["app_id"] == 730 and row["float_value"] is not None and row["float_value"] <= .1 for row in rows)
            window.unknown.set(True)
            window.apply_filters()
            spin()
            assert len(window.item_tree.get_children()) == sum(row["app_id"] == 730 and (row["float_value"] is None or row["float_value"] <= .1) for row in rows)
            window.reset_filters()
            spin()
            window.start()
            spin(1.5)
            assert not window.scanner.active()
            assert window.scanner.stats()["successes"] == len(sample["inventories"])
            for size in ("1380x900", "1080x740"):
                window.geometry(size)
                for tab in window.notebook.tabs():
                    window.notebook.select(tab)
                    spin(.05)
            assert not errors, errors
            network.assert_not_called()
            print("GUI: реальный сохранённый снимок, фильтры, unknown-режим, все вкладки; сетевых запросов: 0.")
        finally:
            window.close()
            deadline = time.monotonic()+3
            while time.monotonic() < deadline:
                try:
                    if not window.winfo_exists():
                        break
                    window.update()
                except tk.TclError:
                    break
                time.sleep(.01)
            store.close()


if __name__ == "__main__":
    check()
