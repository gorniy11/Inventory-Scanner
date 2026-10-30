from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable

from .models import Item


def _safe_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_items(path: Path, items: Iterable[Item], format: str | None = None) -> int:
    rows = [item.to_dict() for item in items]
    format = format or path.suffix.lower().lstrip(".")
    if format == "json":
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    elif format == "csv":
        fields = ["owner_id", "app_id", "asset_id", "name", "type_name", "amount", "tradable", "marketable",
                  "float_value", "pattern_seed", "metadata_source", "first_seen", "last_seen"]
        with path.open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerows({key: _safe_cell(row.get(key)) for key in fields} for row in rows)
    else:
        raise ValueError("Выберите формат JSON или CSV.")
    return len(rows)
