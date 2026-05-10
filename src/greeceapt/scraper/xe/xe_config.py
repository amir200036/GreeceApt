"""XE.gr scraper: URLs, pacing knobs, and data paths (no network logic)."""

from __future__ import annotations

import os

from greeceapt.db_helpers.paths import COOKIES_JSON, XE_LISTINGS_JSON
from greeceapt.scraper.util import ATHENS_CENTER_ID, BASE_URL_XE, build_xe_url

LISTINGS_JSON = XE_LISTINGS_JSON
COOKIES_PATH = COOKIES_JSON

# First Obscura ``serve --port`` for XE cookie bootstrap. 9224 often exits non-zero on macOS;
# fallbacks (9244, 9264, …) still apply via obscura_helper. Override: GREECEAPT_XE_OBSCURA_PORT.
try:
    OBSCURA_BOOTSTRAP_PORT = int(os.getenv("GREECEAPT_XE_OBSCURA_PORT", "9244"))
except ValueError:
    OBSCURA_BOOTSTRAP_PORT = 9244
OBSCURA_BOOTSTRAP_PORT = max(1024, min(65535, OBSCURA_BOOTSTRAP_PORT))

MAP_SEARCH_URL = "https://www.xe.gr/en/property/results/map_search"
SINGLE_RESULT_URL = "https://www.xe.gr/en/property/results/single_result"
AD_GROUP_URL = "https://www.xe.gr/en/property/unique_properties"
RESULTS_REFERER = BASE_URL_XE

_xe_fast = os.getenv("XE_FAST", "").lower() in ("1", "true", "yes")
# Stealth pacing is the default (lower concurrency, sleeps, periodic cooldown). Disable with
# ``XE_STEALTH=0``. Ignored when ``XE_FAST=1``. Individual ``XE_*`` env vars still win if set beforehand.
_xe_stealth_env = os.getenv("XE_STEALTH", "1").strip().lower()
_xe_stealth_on = _xe_stealth_env not in ("0", "false", "no", "off")

if _xe_fast:
    for _k, _v in (
        ("XE_DETAIL_CONCURRENCY", "64"),
        ("XE_DETAIL_JITTER_MAX", "0"),
        ("XE_DISCOVERY_SLEEP_MIN", "0"),
        ("XE_DISCOVERY_SLEEP_MAX", "0"),
        ("XE_EMPTY_PAGE_BACKOFF_SEC", "20"),
        ("XE_MAPSEARCH_PREVIEW", "1"),
    ):
        if _k not in os.environ:
            os.environ[_k] = _v
elif _xe_stealth_on:
    for _k, _v in (
        ("XE_DETAIL_CONCURRENCY", "8"),
        ("XE_DETAIL_JITTER_MAX", "1.0"),
        ("XE_DISCOVERY_SLEEP_MIN", "1.0"),
        ("XE_DISCOVERY_SLEEP_MAX", "2.5"),
        ("XE_COOLDOWN_EVERY_N_PAGES", "3"),
        ("XE_COOLDOWN_SECONDS", "45"),
        ("XE_EMPTY_PAGE_BACKOFF_SEC", "90"),
        # Preview skipped in stealth: map_search cards lack characteristics_list,
        # so floor/energy_class/heating_type/renovation_year would always be null.
        ("XE_MAPSEARCH_PREVIEW", "0"),
    ):
        if _k not in os.environ:
            os.environ[_k] = _v

XE_STEALTH_MODE = (not _xe_fast) and _xe_stealth_on

BATCH_COMMIT_SIZE = int(os.getenv("BATCH_COMMIT_SIZE", "25"))
DETAIL_MAX_RETRIES = int(os.getenv("DETAIL_MAX_RETRIES", "3"))
DETAIL_RETRY_BASE_DELAY = float(os.getenv("DETAIL_RETRY_BASE_DELAY", "1.5"))
DISCOVERY_MAX_RETRIES = int(os.getenv("DISCOVERY_MAX_RETRIES", "3"))

XE_COOLDOWN_EVERY_N_PAGES = int(os.getenv("XE_COOLDOWN_EVERY_N_PAGES", "0"))
XE_COOLDOWN_SECONDS = float(os.getenv("XE_COOLDOWN_SECONDS", "0"))
XE_DISCOVERY_SLEEP_MIN = float(os.getenv("XE_DISCOVERY_SLEEP_MIN", "0.15"))
XE_DISCOVERY_SLEEP_MAX = float(os.getenv("XE_DISCOVERY_SLEEP_MAX", "0.45"))
XE_DETAIL_JITTER_MAX = float(os.getenv("XE_DETAIL_JITTER_MAX", "0.35"))
XE_EMPTY_PAGE_BACKOFF_SEC = float(os.getenv("XE_EMPTY_PAGE_BACKOFF_SEC", "45"))
XE_DETAIL_CONCURRENCY = max(1, int(os.getenv("XE_DETAIL_CONCURRENCY", "40")))
XE_MAPSEARCH_PREVIEW = os.getenv("XE_MAPSEARCH_PREVIEW", "").lower() in ("1", "true", "yes")

DEFAULT_MIN_PRICE = 40_000
DEFAULT_MAX_PRICE = 70_000
DEFAULT_MAX_PAGES = 50
DISCOVERY_ITEM_KEYS = ("items", "result_items", "results", "ads")

BOOTSTRAP_SEARCH_URL = build_xe_url(
    min_price=DEFAULT_MIN_PRICE,
    max_price=DEFAULT_MAX_PRICE,
    geo_place_id=ATHENS_CENTER_ID,
    has_photos=True,
)
