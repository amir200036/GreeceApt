"""Spitogatos API URLs, search payload, and concurrency (no I/O)."""

from __future__ import annotations

import asyncio
import os

from greeceapt.db_helpers.paths import SPITOGATOS_COOKIES_JSON, SPITOGATOS_LISTINGS_JSON

LISTINGS_JSON = SPITOGATOS_LISTINGS_JSON
COOKIES_JSON = SPITOGATOS_COOKIES_JSON

SEARCH_URL = (
    "https://www.spitogatos.gr/en/for_sale-homes/athens-center/"
    "with_photo/last_update_6months/maxprice-70000"
)
SEARCH_API = "https://www.spitogatos.gr/n_api/v1/properties/search-results"
DETAIL_API = "https://www.spitogatos.gr/n_api/v1/properties/{id}"

SEARCH_PAYLOAD: dict = {
    "listingType": "sale",
    "category": "residential",
    "priceLow": 30000,
    "priceHigh": 70000,
    "areaIDs": [100],
    "withPhotos": True,
    "lastUpdateMonths": 6,
    "sortBy": "rankingscore",
    "sortOrder": "desc",
}

DETAIL_SEM = asyncio.Semaphore(max(1, int(os.getenv("SPITOGATOS_DETAIL_CONCURRENCY", "12"))))
BATCH_SAVE_SIZE = int(os.getenv("SPITOGATOS_BATCH_SAVE", "50"))
ROTATE_EVERY = int(os.getenv("SPITOGATOS_ROTATE_EVERY", "100"))
# Set SPITOGATOS_MAX_OFFSET=30 to fetch only 1 page (offset 0) for testing.
MAX_OFFSET = int(os.getenv("SPITOGATOS_MAX_OFFSET", "10000"))
