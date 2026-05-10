"""JSON ingestion helpers for the pipeline package."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_listings_json_list(path: Path) -> list[dict[str, Any]]:
    """
    Read ``path`` as UTF-8 JSON and return a list of listing dicts.

    Raises ``FileNotFoundError`` or ``ValueError`` on invalid input.
    """
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("JSON must contain a list")
    return data
