"""
Scraper-wide helpers: XE search URLs, merge/dedupe math, and listing image hashing.

Used by ``run_all`` (merge), ``scraper/xe/``, and ``scraper/spitogatos/``. Keeps orchestration files thinner.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import imagehash
from PIL import Image

from greeceapt.db_helpers import paths
from greeceapt.scraper.fingerprints import PhotoFingerprint, fingerprint_image

# ── XE.gr search URLs ─────────────────────────────────────────────────────────

BASE_URL_XE = "https://www.xe.gr/en/property/results"
ATHENS_CENTER_ID = "ChIJ8UNwBh-9oRQR3Y1mdkU1Nic"


def build_xe_url(
    min_price: int | None = None,
    max_price: int | None = None,
    geo_place_id: str = ATHENS_CENTER_ID,
    building_type: str | None = None,
    country: str | None = "GR",
    has_photos: bool = False,
    page: int | None = None,
) -> str:
    params: dict[str, object] = {
        "transaction_name": "buy",
        "item_type": "re_residence",
        "geo_place_ids[]": [geo_place_id],
    }
    if country:
        params["country"] = country
    if building_type:
        params["building_type_options[]"] = [building_type]
    if min_price is not None:
        params["minimum_price"] = min_price
    if max_price is not None:
        params["maximum_price"] = max_price
    if has_photos:
        params["has_photos"] = "true"
    if page is not None:
        params["page"] = page
    query_str = urllib.parse.urlencode(params, doseq=True)
    return f"{BASE_URL_XE}?{query_str}"


# ── Incremental scrape JSON stores (XE + Spitogatos) ─────────────────────────


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def touch_listing_updated_at(row: dict[str, Any]) -> None:
    """Set ``updated_at`` to current UTC ISO on the dict already held in the store."""
    row["updated_at"] = utc_now_iso()


def upsert_listing_in_url_store(
    store: dict[str, dict[str, Any]],
    canonical_url: str,
    fresh_row: dict[str, Any],
) -> str:
    """
    If ``canonical_url`` is already in ``store``, only ``updated_at`` is refreshed.
    Otherwise ``fresh_row`` is copied in and gets ``updated_at`` (and ``scraped_at`` if missing).
    Returns ``"touched"`` or ``"inserted"``.
    """
    if canonical_url in store:
        touch_listing_updated_at(store[canonical_url])
        return "touched"
    row = dict(fresh_row)
    now = utc_now_iso()
    row.setdefault("updated_at", now)
    row.setdefault("scraped_at", now)
    store[canonical_url] = row
    return "inserted"


# ── Scrape output filters (XE + Spitogatos) ───────────────────────────────────


def parse_publication_date_string(value: Any) -> datetime | None:
    """Parse ``publication_date`` text (``YYYY-MM-DD`` or ISO). Returns None if unknown."""
    if value is None:
        return None
    s = str(value).strip()
    if len(s) < 10:
        return None
    if s[4] == "-" and s[7] == "-":
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ── Merge / dedupe (run_all) ─────────────────────────────────────────────────

HASH_DH_THRESHOLD = 5
IMAGE_CONCURRENCY = 10
STALE_DAYS = 180

FILL_FROM_OTHER_ON_MERGE = (
    "title", "description", "address_raw", "area_sqm", "bedrooms", "bathrooms",
    "floor", "year_built", "renovation_year", "energy_class", "heating_type",
    "neighborhood", "municipality", "latitude", "longitude", "price_per_sqm",
)

_stale_cutoff_cache: dict[int, datetime] = {}


def clear_publication_stale_cutoff_cache() -> None:
    """Reset cached cutoff (tests or multiple merge runs in one process)."""
    _stale_cutoff_cache.clear()


def publication_stale_cutoff_utc(days: int = STALE_DAYS) -> datetime:
    if days not in _stale_cutoff_cache:
        _stale_cutoff_cache[days] = datetime.now(timezone.utc) - timedelta(days=days)
    return _stale_cutoff_cache[days]


def parse_listing_publication_utc(listing: dict) -> datetime | None:
    return parse_publication_date_string(listing.get("publication_date"))


def is_listing_stale_by_publication(listing: dict, days: int = STALE_DAYS) -> bool:
    dt = parse_listing_publication_utc(listing)
    return dt is not None and dt < publication_stale_cutoff_utc(days)


def listing_has_photos_and_recent_publication(
    publication_date: Any,
    photo_urls: Any,
    *,
    days: int = STALE_DAYS,
) -> bool:
    """
    Strict gate for scrape JSON: at least one photo URL, a parseable publication/update
    date, and that date must be **on or after** the rolling cutoff (``days`` ago). Anything
    else is excluded so it never appears in ``xe_listings.json`` / ``spitogatos_listings.json``.
    """
    urls = photo_urls if isinstance(photo_urls, list) else []
    if not urls:
        return False
    dt = parse_publication_date_string(publication_date)
    if dt is None:
        return False
    return dt >= publication_stale_cutoff_utc(days)


def load_standalone_phash_cache(cache_json: Path | None = None) -> dict[str, PhotoFingerprint]:
    """Load legacy strings and versioned URL→fingerprint cache records."""
    path = cache_json if cache_json is not None else paths.PHASH_CACHE_JSON
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    cache: dict[str, PhotoFingerprint] = {}
    for url, value in data.items():
        fingerprint = PhotoFingerprint.from_json(value)
        if url and fingerprint is not None:
            cache[str(url)] = fingerprint
    return cache


def save_standalone_phash_cache(
    cache: dict[str, PhotoFingerprint | str],
    cache_json: Path | None = None,
) -> None:
    """Persist versioned records atomically so a wiped listing store costs no re-downloads."""
    path = cache_json if cache_json is not None else paths.PHASH_CACHE_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    serialized = {}
    for url, value in cache.items():
        fingerprint = value if isinstance(value, PhotoFingerprint) else PhotoFingerprint.from_json(value)
        if fingerprint is not None:
            serialized[url] = fingerprint.to_json()
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(serialized, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_url_hash_cache_from_listings_json(output_json: Path) -> dict[str, PhotoFingerprint]:
    """
    Build ``{photo_url: phash hex}`` for the merge.

    Seeded from the standalone cache (which outlives ``listings.json``), then
    overlaid with ``photo_url_hashes`` from an existing ``listings.json`` when present.
    """
    cache = load_standalone_phash_cache()
    if not output_json.exists():
        return cache
    try:
        with output_json.open(encoding="utf-8") as f:
            data = json.load(f)
        for listing in data:
            for url, h in (listing.get("photo_url_hashes") or {}).items():
                fingerprint = PhotoFingerprint.from_json(h)
                if fingerprint is not None:
                    cache[url] = fingerprint
            for url, value in (listing.get("photo_fingerprints") or {}).items():
                fingerprint = PhotoFingerprint.from_json(value)
                if fingerprint is not None:
                    cache[url] = fingerprint
    except Exception:
        return cache
    return cache


async def compute_listing_photo_fingerprints(
    photo_urls: list[str],
    url_cache: dict[str, PhotoFingerprint],
    sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
) -> dict[str, PhotoFingerprint]:
    """Return complete v2 fingerprints, lazily upgrading full-only legacy entries."""
    result: dict[str, PhotoFingerprint] = {}
    to_download: list[str] = []
    for url in photo_urls:
        cached = url_cache.get(url)
        if cached is not None and cached.is_complete_v2:
            result[url] = cached
        else:
            to_download.append(url)

    async def _one(url: str) -> tuple[str, PhotoFingerprint | None]:
        async with sem:
            try:
                resp = await client.get(url, timeout=15.0)
                resp.raise_for_status()
                with Image.open(io.BytesIO(resp.content)) as image:
                    return url, fingerprint_image(image)
            except Exception:
                return url, None

    if to_download:
        downloads = await asyncio.gather(*(_one(u) for u in to_download))
        for url, fingerprint in downloads:
            if fingerprint is not None:
                result[url] = fingerprint
                url_cache[url] = fingerprint
    return result


async def compute_listing_photo_hashes(
    photo_urls: list[str],
    url_cache: dict[str, PhotoFingerprint],
    sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
) -> dict[str, str]:
    """Compatibility adapter returning only full hashes until pipeline Task 7 migrates."""
    fingerprints = await compute_listing_photo_fingerprints(photo_urls, url_cache, sem, client)
    return {
        url: f"{fingerprint.full:016x}"
        for url, fingerprint in fingerprints.items()
        if fingerprint.full is not None
    }


def phash_duplicate_groups(
    objs_a: list[imagehash.ImageHash],
    objs_b: list[imagehash.ImageHash],
    dh_threshold: int = HASH_DH_THRESHOLD,
) -> bool:
    """True if any pair has Hamming distance strictly less than ``dh_threshold``."""
    return any((h1 - h2) < dh_threshold for h1 in objs_a for h2 in objs_b)


def phash_duplicate_metrics(
    objs_a: list[imagehash.ImageHash],
    objs_b: list[imagehash.ImageHash],
    dh_threshold: int = HASH_DH_THRESHOLD,
) -> tuple[bool, float, int]:
    """
    Same duplicate predicate as ``phash_duplicate_groups``, plus observability metrics.

    Returns:
        (is_duplicate, similarity_0_to_1, min_hamming_distance)
    where similarity = 1 - min_d / n_bits (n_bits from the first hash's bit length).
    """
    if not objs_a or not objs_b:
        return False, 0.0, 0
    n_bits = max(1, int(objs_a[0].hash.size))
    min_d = min((h1 - h2) for h1 in objs_a for h2 in objs_b)
    sim = 1.0 - (min_d / float(n_bits))
    is_dup = min_d < dh_threshold
    return is_dup, sim, int(min_d)


def quad_lock_metadata_match(a: dict, b: dict) -> bool:
    """Metadata agreement for duplicate confirmation (area / floor / year / energy)."""
    a_area, b_area = a.get("area_sqm"), b.get("area_sqm")
    if a_area is not None and b_area is not None:
        denom = max(float(a_area), float(b_area))
        if denom > 0 and abs(float(a_area) - float(b_area)) / denom > 0.01:
            return False
    a_floor, b_floor = a.get("floor"), b.get("floor")
    if a_floor is not None and b_floor is not None and a_floor != b_floor:
        return False
    a_year, b_year = a.get("year_built"), b.get("year_built")
    if a_year is not None and b_year is not None and a_year != b_year:
        return False
    a_ec = (a.get("energy_class") or "").strip().upper()
    b_ec = (b.get("energy_class") or "").strip().upper()
    if a_ec and b_ec and a_ec != b_ec:
        return False
    return True


def merge_duplicate_listing_into_kept(
    existing: dict,
    incoming: dict,
    *,
    stale_days: int = STALE_DAYS,
) -> None:
    """Merge ``incoming`` into ``existing`` in-place (Quad-Lock survivor rules)."""
    cutoff = publication_stale_cutoff_utc(stale_days)
    existing_dt = parse_listing_publication_utc(existing)
    incoming_dt = parse_listing_publication_utc(incoming)
    existing_stale = existing_dt is not None and existing_dt < cutoff
    incoming_stale = incoming_dt is not None and incoming_dt < cutoff

    if existing_stale and not incoming_stale:
        existing["price_eur"] = incoming.get("price_eur")
        existing["publication_date"] = incoming.get("publication_date")
    elif not existing_stale and not incoming_stale:
        e_price = existing.get("price_eur")
        i_price = incoming.get("price_eur")
        if i_price is not None and (e_price is None or i_price < e_price):
            existing["price_eur"] = i_price
            existing["publication_date"] = incoming.get("publication_date")

    seen: set[str] = set(existing.get("source_urls") or [])
    seen.update(incoming.get("source_urls") or [])
    existing["source_urls"] = sorted(seen)

    e_src = existing.get("source", "")
    i_src = incoming.get("source", "")
    if e_src != i_src and e_src and i_src:
        existing["source"] = "xe & spitogatos"

    for field in FILL_FROM_OTHER_ON_MERGE:
        if existing.get(field) is None and incoming.get(field) is not None:
            existing[field] = incoming[field]

    existing_photos = existing.get("photo_urls") or []
    incoming_photos = incoming.get("photo_urls") or []
    if len(incoming_photos) > len(existing_photos):
        existing["photo_urls"] = incoming_photos
        existing["photos_count"] = len(incoming_photos)

    existing["photo_url_hashes"] = {
        **(existing.get("photo_url_hashes") or {}),
        **(incoming.get("photo_url_hashes") or {}),
    }
    all_hashes: set[str] = set(existing.get("image_hashes") or [])
    all_hashes.update(incoming.get("image_hashes") or [])
    existing["image_hashes"] = sorted(all_hashes)
