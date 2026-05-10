"""Decode Spitogatos API fields, photo URLs, and JSON store I/O."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config as cfg

logger = logging.getLogger(__name__)

_ENERGY_CLASS: dict[int, str] = {
    1: "A+", 2: "A", 3: "B+", 4: "B", 5: "C", 6: "D", 7: "E", 8: "Z",
    9: "N/A",
}
_HEATING_CTRL: dict[int, str] = {
    1: "Autonomous", 2: "Central", 3: "None", 4: "VRV/VRF", 5: "Heat pump",
    6: "District heating",
}
_HEATING_FUEL: dict[int, str] = {
    1: "Oil", 2: "Natural gas", 3: "Wood/Biomass", 4: "Electricity",
    5: "Solar", 6: "Geothermal",
}


def decode_energy_class(code: Any) -> str | None:
    if code is None:
        return None
    return _ENERGY_CLASS.get(int(code), str(code))


def decode_heating(ctrl: Any, medium: Any) -> str | None:
    if ctrl is None and medium is None:
        return None
    parts = []
    if ctrl is not None:
        parts.append(_HEATING_CTRL.get(int(ctrl), f"type-{ctrl}"))
    if medium is not None:
        parts.append(_HEATING_FUEL.get(int(medium), f"fuel-{medium}"))
    return " / ".join(parts) if parts else None


def neighborhood_from_geo(geo_by_level: dict) -> tuple[str | None, str | None]:
    if not isinstance(geo_by_level, dict):
        return None, None
    neighborhood = None
    municipality = None
    for level in ("5", "4"):
        entry = geo_by_level.get(level)
        if isinstance(entry, dict):
            name = entry.get("name")
            if name:
                neighborhood = str(name).strip() or None
                break
    lvl2 = geo_by_level.get("2")
    if isinstance(lvl2, dict):
        municipality = lvl2.get("name")
    return neighborhood, municipality


def photo_urls_from_images(images: list) -> list[str]:
    urls = []
    for img in images:
        if not isinstance(img, dict):
            continue
        url = img.get("large") or img.get("medium") or img.get("xlarge")
        if url:
            urls.append(url)
    return urls


def clean_year(val: Any) -> int | None:
    if val is None:
        return None
    try:
        y = int(str(val).strip()[:4])
        return y if 1800 < y < 2100 else None
    except (ValueError, TypeError):
        return None


def parse_spitogatos_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        if "T" in s or s.endswith("Z") or len(s) > 10:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def clean_float(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def atomic_save(listings: dict[str, dict[str, Any]], path: Path | None = None) -> None:
    path = path or cfg.LISTINGS_JSON
    os.makedirs(path.parent, exist_ok=True)
    rows = sorted(listings.values(), key=lambda r: str(r.get("url", "")))
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_listings_url_map(path: Path | None = None) -> dict[str, dict[str, Any]]:
    path = path or cfg.LISTINGS_JSON
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load %s (%s); starting empty store.", path, exc)
        return {}
    if not isinstance(data, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in data:
        if isinstance(item, dict):
            u = item.get("url")
            if u:
                out[str(u).strip()] = item
    return out
