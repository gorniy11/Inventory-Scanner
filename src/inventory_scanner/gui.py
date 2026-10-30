from __future__ import annotations

import json
import threading
import time
import tkinter as tk
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .sample import SnapshotClient, snapshot_metadata
from .exporting import export_items
from .filters import FilterSpec
from .models import GAMES, Profile
from .scanner import EventBus, ScanOptions, Scanner
from .steam import InventoryError, SteamClient
from .storage import Store

BG, PANEL, FIELD, FG = "#f0f0f0", "#ffffff", "#ffffff", "#202020"


class Application(tk.Tk):
    def __init__(self, store: Store, *, demo: bool = True):
        super().__init__()
        self.store, self.demo = store, demo
        self.events = EventBus()
        self.scanner = Scanner(store, self.events, SnapshotClient if demo else None)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="InventoryView")
        self.view_future = None
        self.view_generation = 0
        self.view_dirty = False
        self.tasks = []
        self.closing_stop = threading.Event()
        self.closed = False
        self.resolving = False
        self.rows = {}
        self.added_profiles = {profile.steam_id for profile in store.profiles()}
        self.spec = FilterSpec.from_dict(store.setting("filter", {}))
        self.title("Inventory Scanner")
        self.geometry("1160x720")
        self.minsize(900, 560)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._style()
        self._build()
        if demo:
            sample = snapshot_metadata()
            available = {row["game"] for row in sample["inventories"]}
            for key, variable in self.game_vars.items():
                variable.set(key in available)
        self._fill_filter(self.spec)
        self.refresh_profiles()
        self.request_view()
        self.after(100, self.poll)

    def _style(self):
        style = ttk.Style(self)
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")
        style.configure(".", font=("Segoe UI", 9))
        style.configure("Treeview", rowheight=23)

    def _build(self):
        shell = ttk.Frame(self, padding=8)
        shell.pack(fill="both", expand=True)
        controls = ttk.Frame(shell)
        controls.pack(fill="x", pady=(0, 8))
        self.scan_button = ttk.Button(controls, text="Проверить", command=self.start)
        self.scan_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Стоп", command=self.scanner.cancel)
        self.stop_button.pack(side="left", padx=5)
        ttk.Button(controls, text="Экспорт", command=self.export).pack(side="left")
        ttk.Button(controls, text="Настройки", command=self.settings_dialog).pack(side="right")
        self.game_vars = {key: tk.BooleanVar(value=True) for key in GAMES}
        self.proxy = tk.StringVar()
        self.status = tk.StringVar()
        self.metrics = {key: tk.StringVar() for key in ("profiles", "items", "matches", "scans")}
        self.notebook = ttk.Notebook(shell)
        self.notebook.pack(fill="both", expand=True)
        tabs = {}
        for name in ("Предметы", "Профили", "Фильтры", "Журнал"):
            tabs[name] = ttk.Frame(self.notebook, padding=8)
            self.notebook.add(tabs[name], text=name)
        self._items_tab(tabs["Предметы"])
        self._profiles_tab(tabs["Профили"])
        self._filters_tab(tabs["Фильтры"])
        self._logs_tab(tabs["Журнал"])

    def _tree(self, parent, columns):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=tuple(columns), show="headings", selectmode="extended")
        for key, (caption, width) in columns.items():
            tree.heading(key, text=caption, command=lambda k=key: self.sort_tree(tree, k))
            tree.column(key, width=width, minwidth=65, stretch=key == "name")
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        return tree

    def _items_tab(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Поиск:").pack(side="left", padx=(0, 6))
        self.quick = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.quick, width=42)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<KeyRelease>", lambda _: self.request_view())
        ttk.Button(bar, text="Импорт метаданных", command=self.import_metadata).pack(side="right", padx=(8, 0))
        self.item_tree = self._tree(parent, {
            "name": ("Название", 320), "game": ("Игра", 110), "float": ("Float", 100),
            "seed": ("Pattern", 65), "quality": ("Качество", 140),
            "tradable": ("Обмен", 65), "profile": ("Профиль", 100), "amount": ("Кол-во", 55),
        })
        self.item_tree.bind("<Double-1>", lambda _: self.item_details())
        self.item_tree.bind("<Button-3>", self.item_menu)

    def _profiles_tab(self, parent):
        entry_row = ttk.Frame(parent)
        entry_row.pack(fill="x", pady=(0, 8))
        ttk.Label(entry_row, text="Профиль:").pack(side="left", padx=(0, 6))
        self.profile_input = self._text(entry_row, height=2)
        self.profile_input.pack(side="left", fill="x", expand=True)
        self.add_button = ttk.Button(entry_row, text="Добавить", command=self.add_profiles)
        self.add_button.pack(side="left", padx=(8, 0))
        self.remove_button = ttk.Button(entry_row, text="Удалить", command=self.remove_profiles)
        self.remove_button.pack(side="left", padx=(5, 0))
        games = ttk.Frame(parent)
        games.pack(fill="x", pady=(0, 8))
        ttk.Label(games, text="Игры:").pack(side="left", padx=(0, 6))
        for key, game in GAMES.items():
            ttk.Checkbutton(games, text=game.name, variable=self.game_vars[key]).pack(side="left", padx=(0, 8))
        self.profile_tree = self._tree(parent, {"name": ("Профиль", 180), "id": ("SteamID64", 170),
                                              "enabled": ("Вкл.", 65), "state": ("Инвентари", 540)})
        self.profile_tree.bind("<Double-1>", self.toggle_profile)
        if self.demo:
            self.add_button.configure(state="disabled")
            self.remove_button.configure(state="disabled")

    def _text(self, parent, **kwargs):
        return tk.Text(parent, bg=FIELD, fg=FG, insertbackground=FG, relief="solid", borderwidth=1,
                       highlightthickness=0, padx=5, pady=4, font=("Segoe UI", 9), **kwargs)

    def _filters_tab(self, parent):
        canvas = tk.Canvas(parent, background=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        body = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        left, right = ttk.Frame(body), ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        right.grid(row=0, column=1, sticky="nsew")
        self.filter_vars = {key: tk.StringVar() for key in ("game", "name_mode", "description", "tradable", "marketable", "float_min", "float_max", "seed_min", "seed_max")}
        self.unknown = tk.BooleanVar()
        self.new_only = tk.BooleanVar()
        self.include_names = self._filter_text(left, "Названия", 3)
        self.exclude_names = self._filter_text(left, "Исключить", 2)
        self._combo(left, "Совпадение", "name_mode", ("Содержит", "Точное"))
        self._combo(left, "Игра", "game", ("Все игры", *[game.name for game in GAMES.values()]))
        ttk.Label(left, text="Описание").pack(anchor="w", pady=(7, 3))
        ttk.Entry(left, textvariable=self.filter_vars["description"]).pack(fill="x")
        self._combo(right, "Обмен", "tradable", ("Любое", "Да", "Нет"))
        self._combo(right, "Торговая площадка", "marketable", ("Любое", "Да", "Нет"))
        self.tag_rules = self._filter_text(right, "Теги", 3)
        for label, low, high in (("Float", "float_min", "float_max"), ("Pattern", "seed_min", "seed_max")):
            bar = ttk.Frame(right)
            bar.pack(fill="x", pady=(9, 0))
            ttk.Label(bar, text=label, width=10).pack(side="left")
            ttk.Entry(bar, width=12, textvariable=self.filter_vars[low]).pack(side="left", padx=(0, 5))
            ttk.Label(bar, text="—").pack(side="left")
            ttk.Entry(bar, width=12, textvariable=self.filter_vars[high]).pack(side="left", padx=5)
        ttk.Checkbutton(right, text="Включать неизвестные значения", variable=self.unknown).pack(anchor="w", pady=(8, 0))
        ttk.Checkbutton(right, text="Только новые", variable=self.new_only).pack(anchor="w")
        bar = ttk.Frame(body)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        ttk.Button(bar, text="Применить", command=self.apply_filters).pack(side="left")
        ttk.Button(bar, text="Сбросить", command=self.reset_filters).pack(side="left", padx=5)
        ttk.Button(bar, text="Сохранить", command=self.save_filters).pack(side="right")
        ttk.Button(bar, text="Загрузить", command=self.load_filters).pack(side="right", padx=5)

    def _filter_text(self, parent, label, height):
        ttk.Label(parent, text=label).pack(anchor="w", pady=(7, 3))
        widget = self._text(parent, height=height)
        widget.pack(fill="x")
        return widget

    def _combo(self, parent, label, key, values):
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Label(bar, text=label).pack(side="left")
        ttk.Combobox(bar, textvariable=self.filter_vars[key], values=values, state="readonly", width=24).pack(side="right")

    def _logs_tab(self, parent):
        self.log_text = self._text(parent, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)

    def settings_dialog(self):
        popup = tk.Toplevel(self)
        popup.title("Настройки")
        popup.resizable(False, False)
        popup.transient(self)
        body = ttk.Frame(popup, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Прокси:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(body, textvariable=self.proxy, width=48, show="•").grid(row=0, column=1, sticky="ew")
        ttk.Button(body, text="Закрыть", command=popup.destroy).grid(row=1, column=1, sticky="e", pady=(12, 0))

    def _fill_filter(self, spec):
        for widget, values in ((self.include_names, spec.include_names), (self.exclude_names, spec.exclude_names),
                               (self.tag_rules, [f"{key}={value}" for key, value in spec.tags.items()])):
            widget.delete("1.0", "end")
            widget.insert("1.0", "\n".join(values))
        values = dict(game=next((game.name for game in GAMES.values() if spec.app_ids == (game.app_id,)), "Все игры"),
                      name_mode="Точное" if spec.name_mode == "exact" else "Содержит", description=spec.description,
                      tradable={None: "Любое", True: "Да", False: "Нет"}[spec.tradable],
                      marketable={None: "Любое", True: "Да", False: "Нет"}[spec.marketable])
        for key in ("float_min", "float_max", "seed_min", "seed_max"):
            values[key] = "" if getattr(spec, key) is None else str(getattr(spec, key))
        for key, value in values.items():
            self.filter_vars[key].set(value)
        self.unknown.set(spec.include_unknown_metadata)
        self.new_only.set(spec.new_only)

    def _read_filter(self):
        values = {key: variable.get().strip() for key, variable in self.filter_vars.items()}
        tags = {}
        for line in self.tag_rules.get("1.0", "end").splitlines():
            if not line.strip():
                continue
            key, separator, value = line.partition("=")
            if not separator or not key.strip() or not value.strip():
                raise ValueError("Тег задаётся как категория=значение.")
            tags[key.strip()] = value.strip()
        numbers = {}
        for key in ("float_min", "float_max", "seed_min", "seed_max"):
            numbers[key] = (int(values[key]) if key.startswith("seed") else float(values[key].replace(",", "."))) if values[key] else None
        app = next((game.app_id for game in GAMES.values() if game.name == values["game"]), None)
        return FilterSpec(
            app_ids=(app,) if app is not None else (),
            include_names=tuple(line.strip() for line in self.include_names.get("1.0", "end").splitlines() if line.strip()),
            exclude_names=tuple(line.strip() for line in self.exclude_names.get("1.0", "end").splitlines() if line.strip()),
            name_mode="exact" if values["name_mode"] == "Точное" else "contains", description=values["description"],
            tradable={"Любое": None, "Да": True, "Нет": False}[values["tradable"]],
            marketable={"Любое": None, "Да": True, "Нет": False}[values["marketable"]], tags=tags,
            include_unknown_metadata=self.unknown.get(), new_only=self.new_only.get(), **numbers,
        )

    def apply_filters(self):
        try:
            self.spec = self._read_filter()
            self.store.save_setting("filter", self.spec.to_dict())
            self.request_view()
        except (ValueError, KeyError) as error:
            messagebox.showerror("Параметры фильтра", str(error), parent=self)

    def reset_filters(self):
        self.spec = FilterSpec()
        self._fill_filter(self.spec)
        self.quick.set("")
        self.store.save_setting("filter", self.spec.to_dict())
        self.request_view()

    def save_filters(self):
        try:
            spec = self._read_filter()
            filename = filedialog.asksaveasfilename(parent=self, defaultextension=".json", filetypes=[("JSON", "*.json")])
            if filename:
                Path(filename).write_text(json.dumps(spec.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        except (ValueError, OSError) as error:
            messagebox.showerror("Сохранение фильтра", str(error), parent=self)

    def load_filters(self):
        filename = filedialog.askopenfilename(parent=self, filetypes=[("JSON", "*.json")])
        if not filename:
            return
        try:
            self.spec = FilterSpec.from_dict(json.loads(Path(filename).read_text(encoding="utf-8-sig")))
            self._fill_filter(self.spec)
            self.store.save_setting("filter", self.spec.to_dict())
            self.request_view()
        except (ValueError, OSError) as error:
            messagebox.showerror("Загрузка фильтра", str(error), parent=self)

    def request_view(self):
        self.view_generation += 1
        self.view_dirty = True

    def _start_view(self):
        generation, spec, quick = self.view_generation, self.spec, self.quick.get().strip()
        self.view_dirty = False
        self.view_future = self.executor.submit(lambda: (generation, self.store.query(spec, quick=quick)))

    def _render_view(self, result):
        items, matched, total = result
        self.metrics["profiles"].set(str(len(self.store.profiles())))
        self.metrics["items"].set(f"{total:,}".replace(",", " "))
        self.metrics["matches"].set(str(matched))
        selected = set(self.item_tree.selection())
        self.item_tree.delete(*self.item_tree.get_children())
        profiles = {profile.steam_id: profile.label for profile in self.store.profiles()}
        self.rows = {}
        for item in items:
            key = f"{item.owner_id}:{item.app_id}:{item.asset_id}"
            self.rows[key] = item
            quality = (item.tags.get("Rarity") or item.tags.get("Quality") or ["—"])[0]
            self.item_tree.insert("", "end", iid=key, values=(
                item.name, item.game.name, f"{item.float_value:.8f}" if item.float_value is not None else "—",
                item.pattern_seed if item.pattern_seed is not None else "—", quality,
                "Да" if item.tradable else "Нет", profiles.get(item.owner_id, item.owner_id), item.amount,
            ))
        if selected:
            self.item_tree.selection_set([key for key in selected if key in self.rows])
        self.status.set(f"Совпадений: {matched} из {total}. В таблице: {len(items)} (до 2000); экспорт включает все совпадения.")

    def _task(self, function, kind):
        def work():
            try:
                self.events.put(kind, {"result": function()})
            except Exception as error:
                self.events.put("error", {"message": str(error) if isinstance(error, (ValueError, OSError, InventoryError))
                                         else f"Не удалось выполнить действие: {type(error).__name__}."})
        future = self.executor.submit(work)
        self.tasks.append(future)

    def add_profiles(self):
        if self.demo or self.scanner.active():
            return
        inputs = [line.strip() for line in self.profile_input.get("1.0", "end").splitlines() if line.strip()]
        if not inputs or len(inputs) > 3:
            messagebox.showinfo("Профили", "Введите от 1 до 3 ссылок или SteamID64.", parent=self)
            return
        self.add_button.configure(state="disabled")
        self.resolving = True
        proxy = self.proxy.get()
        history = set(self.added_profiles)
        def resolve():
            client = SteamClient(gate=self.scanner.gate, proxy=proxy)
            try:
                found = {}
                for value in inputs:
                    profile = client.resolve(value, self.closing_stop)
                    found[profile.steam_id] = profile
                if len(history | set(found)) > 3:
                    raise ValueError("Доступно не больше 3 уникальных профилей за сессию.")
                for profile in found.values():
                    self.store.save_profile(profile)
                return list(found)
            finally:
                client.close()
        self._task(resolve, "profiles_added")

    def remove_profiles(self):
        if self.demo or self.scanner.active():
            return
        for owner in self.profile_tree.selection():
            self.store.remove_profile(owner)
        self.refresh_profiles()
        self.request_view()

    def toggle_profile(self, _event=None):
        if self.scanner.active():
            return
        selected = set(self.profile_tree.selection())
        for profile in self.store.profiles():
            if profile.steam_id in selected:
                self.store.save_profile(Profile(profile.steam_id, profile.label, not profile.enabled, profile.demo))
        self.refresh_profiles()

    def refresh_profiles(self):
        self.profile_tree.delete(*self.profile_tree.get_children())
        scans = self.store.scans()
        for profile in self.store.profiles():
            labels = []
            for game in GAMES.values():
                scan = scans.get((profile.steam_id, game.app_id))
                if scan:
                    labels.append(f"{game.name}: " + ("ошибка" if scan["error"] else str(scan["count"])))
            self.profile_tree.insert("", "end", iid=profile.steam_id, values=(profile.label, profile.steam_id,
                    "Да" if profile.enabled else "Нет", " · ".join(labels) or "Ещё не проверялся"))

    def start(self):
        try:
            options = ScanOptions(tuple(key for key, value in self.game_vars.items() if value.get()), self.proxy.get())
            self.scanner.start(options, self.spec)
        except ValueError as error:
            messagebox.showinfo("Ознакомительный режим", str(error), parent=self)

    def import_metadata(self):
        filename = filedialog.askopenfilename(parent=self, filetypes=[("JSON", "*.json")])
        if not filename:
            return
        def load():
            data = json.loads(Path(filename).read_text(encoding="utf-8-sig"))
            if not isinstance(data, list) or len(data) > 5000:
                raise ValueError("Нужен JSON-массив не более чем из 5000 записей.")
            return self.store.import_metadata(data)
        self._task(load, "metadata_imported")

    def export(self):
        filename = filedialog.asksaveasfilename(parent=self, defaultextension=".json",
                                               filetypes=[("JSON", "*.json"), ("CSV", "*.csv")])
        if filename:
            spec, quick = self.spec, self.quick.get().strip()
            self._task(lambda: export_items(Path(filename), self.store.query(spec, limit=None, quick=quick)[0]), "exported")

    def sort_tree(self, tree, column):
        reverse = getattr(self, "last_sort", None) == (id(tree), column, False)
        def key(row):
            value = tree.set(row, column)
            try:
                return (0, float(value))
            except ValueError:
                return (2 if value == "—" else 1, value.casefold())
        for n, row in enumerate(sorted(tree.get_children(), key=key, reverse=reverse)):
            tree.move(row, "", n)
        self.last_sort = id(tree), column, reverse

    def item_menu(self, event):
        row = self.item_tree.identify_row(event.y)
        if not row:
            return
        if row not in self.item_tree.selection():
            self.item_tree.selection_set(row)
        menu = tk.Menu(self, tearoff=False, bg=PANEL, fg=FG)
        menu.add_command(label="Сведения о предмете", command=self.item_details)
        item = self.rows[row]
        menu.add_command(label="Открыть инвентарь Steam", command=lambda: webbrowser.open(item.inventory_url),
                         state="normal" if item.inventory_url else "disabled")
        menu.add_command(label="Скопировать названия", command=self.copy_names)
        menu.tk_popup(event.x_root, event.y_root)

    def copy_names(self):
        names = dict.fromkeys(self.rows[key].name for key in self.item_tree.selection() if key in self.rows)
        self.clipboard_clear()
        self.clipboard_append("\n".join(names))

    def item_details(self):
        selected = self.item_tree.selection()
        if not selected:
            return
        item = self.rows[selected[0]]
        popup = tk.Toplevel(self)
        popup.title(item.name)
        popup.geometry("760x570")
        popup.configure(bg=BG)
        ttk.Label(popup, text=item.name, font=("Segoe UI", 15, "bold"), wraplength=710).pack(anchor="w", padx=18, pady=15)
        text = self._text(popup, wrap="word")
        text.pack(fill="both", expand=True, padx=18, pady=(0, 15))
        text.insert("1.0", "Источник метаданных: " + item.metadata_source + "\n\n" +
                    "\n".join(item.descriptions) + "\n\n" + json.dumps(item.to_dict(), ensure_ascii=False, indent=2))
        text.configure(state="disabled")

    def _log(self, message):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", time.strftime("%H:%M:%S") + "  " + message + "\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 500:
            self.log_text.delete("1.0", f"{lines-500}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def poll(self):
        if self.closed:
            return
        for kind, data in self.events.drain():
            if kind == "log":
                self._log(data["message"])
            elif kind == "refresh":
                self.refresh_profiles()
                self.request_view()
            elif kind == "profiles_added":
                self.resolving = False
                self.added_profiles.update(data["result"])
                self.profile_input.delete("1.0", "end")
                self.add_button.configure(state="normal")
                self.refresh_profiles()
                self.request_view()
            elif kind == "metadata_imported":
                self._log(f"Импортировано записей метаданных: {data['result']}.")
                self.request_view()
            elif kind == "exported":
                self.status.set(f"Экспортировано предметов: {data['result']}.")
            elif kind == "error":
                self.resolving = False
                self._log(data["message"])
                if not self.demo:
                    self.add_button.configure(state="normal")
                messagebox.showerror("Действие не выполнено", data["message"], parent=self)
        if self.view_future is not None and self.view_future.done():
            try:
                generation, result = self.view_future.result()
                if generation == self.view_generation:
                    self._render_view(result)
            except Exception as error:
                self._log(f"Ошибка отображения: {type(error).__name__}.")
            self.view_future = None
        if self.view_dirty and self.view_future is None:
            self._start_view()
        self.tasks = [future for future in self.tasks if not future.done()]
        active = self.scanner.active()
        self.scan_button.configure(state="disabled" if active or self.resolving else "normal")
        self.stop_button.configure(state="normal" if active else "disabled")
        if not self.demo:
            self.add_button.configure(state="disabled" if active or self.resolving else "normal")
            self.remove_button.configure(state="disabled" if active else "normal")
        stats = self.scanner.stats()
        elapsed = int(stats["elapsed"])
        self.metrics["scans"].set(f"{stats['successes']} · {elapsed//60:02d}:{elapsed%60:02d}")
        self.after(100, self.poll)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.closing_stop.set()
        self.scanner.cancel()
        self.withdraw()
        self.after(100, self._finish_close)

    def _finish_close(self):
        if self.scanner.active() or any(not future.done() for future in self.tasks) or (
            self.view_future is not None and not self.view_future.done()
        ):
            self.after(100, self._finish_close)
            return
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.destroy()
