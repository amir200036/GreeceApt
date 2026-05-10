"""Date and parsing helpers shared by AI pipeline layers."""

from __future__ import annotations

from datetime import date, datetime


def parse_yyyy_mm_dd_prefix(value) -> date | None:
    """Parse a DB or API date string; only the first 10 chars ``YYYY-MM-DD`` are used."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
