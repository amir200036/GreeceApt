"""Pure helpers for listing URLs, neighborhoods, SQLite rows, and JSON columns."""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_COMPOUND_PREFIXES = frozenset({"ano", "kato", "nea", "neo", "palaio"})


def normalize_listing_url(url: str | None) -> str | None:
    """Strip query/fragment to keep a stable canonical URL."""
    if not url:
        return None
    try:
        parts = urlsplit(str(url))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return None


def resolve_neighborhood(
    municipality: str | None, area_raw: str | None
) -> str | None:
    """
    Return the full neighborhood name, combining split API fields when needed.
    Examples:
      ("Nea Smyrni", None)   → "Nea Smyrni"
      ("Nea", "Smyrni")      → "Nea Smyrni"
      ("Athens", "Pagkrati") → "Pagkrati"
      ("Pagkrati", "Gouva")  → "Pagkrati"
    """
    mun = (municipality or "").strip() or None
    ar = (area_raw or "").strip() or None
    if not mun and not ar:
        return None
    if mun and mun.lower() == "athens":
        return ar or None
    if mun:
        mun_parts = mun.split()
        if len(mun_parts) == 1 and mun_parts[0].lower() in _COMPOUND_PREFIXES and ar:
            return mun + " " + ar
        return mun
    return ar


# ── SQLite row / TEXT column coercion (used by scoring, ingest, layers) ─────


def row_get(row: sqlite3.Row, key: str) -> Any:
    """Safe ``row[key]`` for pipeline rows (TEXT columns, optional keys)."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def coerce_sql_float(value: Any) -> float | None:
    """Parse a DB cell to float; returns None on failure (TEXT REAL columns)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def coerce_sql_int(value: Any) -> int | None:
    """Parse a DB cell to int; returns None on failure."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_photo_urls_json(photo_urls_json: str | None) -> list[str]:
    """Decode ``listings.photo_urls_json`` to a list of URL strings."""
    if not photo_urls_json:
        return []
    try:
        urls = json.loads(photo_urls_json)
    except (ValueError, TypeError):
        return []
    return urls if isinstance(urls, list) else []
