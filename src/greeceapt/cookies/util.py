"""Cookie JSON parsing and expiry inspection (no Playwright)."""

from __future__ import annotations

import time
from typing import Any


def parse_cookie_json_root(data: Any) -> list[dict[str, Any]]:
    """
    Normalise cookies.json payload to a list of cookie dicts.

    Accepts either a bare list or ``{\"cookies\": [...]}``.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        return data["cookies"]
    raise ValueError("cookies.json format not recognized (expected list or {'cookies': [...]}).")


def count_expired_cookies(cookies: list[dict[str, Any]], now: float | None = None) -> int:
    """How many cookies have a positive ``expires`` in the past."""
    t = time.time() if now is None else now
    n = 0
    for c in cookies:
        exp = c.get("expires")
        if isinstance(exp, (int, float)) and exp > 0 and exp < t:
            n += 1
    return n
