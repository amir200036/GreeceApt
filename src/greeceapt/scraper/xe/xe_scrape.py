"""XE.gr discovery, detail fetch, JSON store, and scrape cycle orchestration."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from typing import Any
from urllib.parse import parse_qs, urlsplit

from curl_cffi import requests

from greeceapt.db_helpers.util import normalize_listing_url
from . import xe_config as cfg
from . import xe_parse as parse
from . import xe_session as sess
from greeceapt.scraper.util import build_xe_url, upsert_listing_in_url_store

logger = logging.getLogger(__name__)


async def _api_get_json(
    runtime: "ScraperRuntime",
    url: str,
    params: dict[str, Any],
    *,
    max_retries: int,
    tag: str,
) -> Any | None:
    """Shared curl GET + JSON parse with the same retry / refresh policy as discovery and detail."""
    already_refreshed = False
    for attempt in range(1, max_retries + 1):
        try:
            response = await asyncio.to_thread(runtime.session.get, url, params=params, timeout=30)
        except Exception as exc:
            if sess.is_dns_or_connection_error(exc):
                logger.warning("%s network err attempt=%s: %s — sleep 60s + refresh.", tag, attempt, exc)
                await asyncio.sleep(60)
                if not already_refreshed:
                    await runtime.refresh_session_cookies(force=True)
                    already_refreshed = True
                if attempt < max_retries:
                    continue
                return None
            logger.warning("%s exception attempt=%s: %s", tag, attempt, exc)
            if attempt < max_retries:
                await asyncio.sleep(cfg.DETAIL_RETRY_BASE_DELAY * attempt)
                continue
            return None

        if response.status_code in (403, 405, 429):
            logger.warning("%s blocked status=%s attempt=%s", tag, response.status_code, attempt)
            if not already_refreshed:
                await runtime.refresh_session_cookies(force=True)
                already_refreshed = True
            if attempt < max_retries:
                backoff = 45.0 * attempt if response.status_code == 405 else cfg.DETAIL_RETRY_BASE_DELAY * attempt
                await asyncio.sleep(backoff)
                continue
            return None

        if response.status_code != 200:
            logger.error("%s HTTP %s", tag, response.status_code)
            return None
        try:
            return response.json()
        except ValueError:
            logger.error("%s JSON parse failed", tag)
            return None
    return None


async def discover_items(
    runtime: "ScraperRuntime",
    page: int,
    min_price: int,
    max_price: int,
    geo_place_id: str,
    *,
    has_photos: bool = True,
) -> list[dict[str, Any]]:
    """Raw map_search rows for one SERP page (used for IDs and optional preview fast path)."""
    search_url = build_xe_url(
        min_price=min_price,
        max_price=max_price,
        geo_place_id=geo_place_id,
        page=page,
        has_photos=has_photos,
    )
    parsed_params = parse_qs(urlsplit(search_url).query, keep_blank_values=True)
    params: dict[str, Any] = {
        key: values if len(values) > 1 else values[0]
        for key, values in parsed_params.items()
    }
    tag = f"Discovery page={page}"
    payload = await _api_get_json(
        runtime, cfg.MAP_SEARCH_URL, params, max_retries=cfg.DISCOVERY_MAX_RETRIES, tag=tag,
    )
    if not isinstance(payload, dict):
        if payload is not None:
            logger.warning("%s unexpected payload type: %s", tag, type(payload).__name__)
        return []
    items = parse.extract_discovery_items(payload)
    if not items:
        logger.warning("%s empty items; keys=%s", tag, sorted(payload.keys()))
        return []
    return [item for item in items if isinstance(item, dict)]


def load_local_listings() -> dict[str, dict[str, Any]]:
    if not cfg.LISTINGS_JSON.exists():
        return {}
    try:
        with cfg.LISTINGS_JSON.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            logger.warning("Existing listings file is not a list. Starting with empty store.")
            return {}
        deduped: dict[str, dict[str, Any]] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            canonical_url = normalize_listing_url(item.get("url"))
            if canonical_url:
                deduped[canonical_url] = item
        return deduped
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Failed reading %s: %s", cfg.LISTINGS_JSON, exc)
        return {}


def save_local_listings(listings_store: dict[str, dict[str, Any]]) -> None:
    cfg.LISTINGS_JSON.parent.mkdir(parents=True, exist_ok=True)
    rows = list(listings_store.values())
    rows.sort(key=lambda row: str(row.get("url", "")))
    with cfg.LISTINGS_JSON.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)


class ScraperRuntime:
    def __init__(self, session: requests.Session, concurrency: int) -> None:
        self.session = session
        self.sem = asyncio.Semaphore(concurrency)
        self.cookie_refresh_lock = asyncio.Lock()
        self._refresh_gen = 0
        self.ad_group_cache: dict[str, list[dict[str, Any]]] = {}

    async def refresh_session_cookies(self, force: bool) -> None:
        gen_before = self._refresh_gen
        async with self.cookie_refresh_lock:
            # If another task already refreshed while we were waiting, skip.
            if self._refresh_gen > gen_before:
                return
            cookies = await sess.get_valid_cookies(force=force)
            if force:
                # Full rebuild: new TLS connection pool + new cookies — avoids reusing a flagged session.
                self.session = sess.build_impersonated_session(cookies)
                self.ad_group_cache.clear()
                logger.info("Session fully rebuilt after forced cookie refresh.")
            else:
                cookie_dict = sess._cookie_dict_for_xe_session(cookies)
                self.session.cookies.clear()
                self.session.cookies.update({k: v for k, v in cookie_dict.items() if v is not None})
                self.session.headers["x-csrf-token"] = str(cookie_dict.get("csrf_token", "") or "")
            self._refresh_gen += 1


def _extract_unit_photo_urls(unit: dict[str, Any]) -> list[str]:
    """Extract photo URLs from a unit dict using the same logic as _extract_photo_urls plus extra keys."""
    # Reuse the main extractor (handles photos + image_gallery with nested branches)
    urls: list[str] = parse._extract_photo_urls(unit)

    # Also check extra keys that units sometimes use
    for key in ("media", "images", "gallery", "pictures", "media_gallery"):
        collection = unit.get(key)
        if not isinstance(collection, list):
            continue
        for item in collection:
            if isinstance(item, str) and item:
                urls.append(item)
            elif isinstance(item, dict):
                url = parse._first_present(item, "url", "src", "image_url", "large", "original", "media_url", "jpeg", "webp")
                if url:
                    urls.append(str(url))
    return list(dict.fromkeys(urls))


async def _fetch_ad_group_units_cached(
    runtime: "ScraperRuntime", ad_group_id: str,
) -> list[dict[str, Any]]:
    """Deduplicate ``unique_properties`` when many ``single_result`` rows share the same ``ad_group_id``."""
    cache = runtime.ad_group_cache
    if ad_group_id in cache:
        return cache[ad_group_id]
    units = await _fetch_ad_group_units(runtime, ad_group_id)
    cache[ad_group_id] = units
    return units


def _units_from_unique_properties_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [u for u in payload if isinstance(u, dict)]
    if isinstance(payload, dict):
        for key in ("results", "items", "units", "properties", "ads"):
            value = payload.get(key)
            if isinstance(value, list):
                return [u for u in value if isinstance(u, dict)]
    return []


async def _fetch_ad_group_units(runtime: "ScraperRuntime", ad_group_id: str) -> list[dict[str, Any]]:
    """Call unique_properties and return unit dicts."""
    params = {"ad_group_id": ad_group_id, "transaction_name": "buy", "item_type": "re_residence"}
    tag = f"unique_properties ad_group={ad_group_id}"
    payload = await _api_get_json(runtime, cfg.AD_GROUP_URL, params, max_retries=2, tag=tag)
    return _units_from_unique_properties_payload(payload)


def _build_hybrid_detail_node(
    base_node: dict[str, Any],
    units: list[dict[str, Any]],
    ad_group_id: str,
) -> tuple[dict[str, Any], Any]:
    """
    Synthesise one best record from all units in an ad group:
      • price, area_sqm, floor, url  ← cheapest unit (price > 0)
      • photos                        ← unit with the most photos

    Returns (merged_detail_node, raw_floor_value_for_chars_override).
    raw_floor_value is None when no cheapest unit was found.
    """
    if not units:
        return base_node, None

    def _unit_price(u: dict[str, Any]) -> float:
        raw = parse._first_present(u, "price", "price_eur", "price_value", "final_price")
        p = parse.clean_price_eur(raw)
        return p if p and p > 0 else float("inf")

    cheapest = min(units, key=_unit_price)
    cheapest_price = _unit_price(cheapest)
    has_valid_price = cheapest_price < float("inf")

    photos_by_unit = {u: _extract_unit_photo_urls(u) for u in units}
    most_photos = max(units, key=lambda u: len(photos_by_unit[u]))
    best_photos = photos_by_unit[most_photos]

    merged = dict(base_node)
    raw_floor = None

    if has_valid_price:
        raw_price = parse._first_present(cheapest, "price", "price_eur", "price_value", "final_price")
        if raw_price is not None:
            merged["price"] = raw_price

        raw_size = parse._first_present(cheapest, "size_with_square_meter", "size", "area_sqm", "sqm", "area")
        if raw_size is not None:
            merged["size_with_square_meter"] = raw_size

        raw_floor = parse._first_present(cheapest, "floor")

        unit_url = parse._first_present(cheapest, "url", "seo_url", "link")
        if unit_url:
            unit_url_str = str(unit_url)
            if unit_url_str.startswith("/"):
                unit_url_str = "https://www.xe.gr" + unit_url_str
            merged["url"] = unit_url_str
            merged["seo_url"] = unit_url_str

    if best_photos:
        merged["photos"] = best_photos
        merged["image_gallery"] = []  # prevent _extract_photo_urls from mixing in stale gallery

    logger.info(
        "Ad group %s: %s units — cheapest=%.0f€  best_photos=%s",
        ad_group_id,
        len(units),
        cheapest_price if has_valid_price else 0,
        len(best_photos),
    )
    if not best_photos and units:
        logger.warning(
            "Ad group %s: photos still 0 after extraction. Sample unit top-level keys: %s",
            ad_group_id,
            sorted(units[0].keys()),
        )
    return merged, raw_floor


async def fetch_listing_detail(runtime: ScraperRuntime, result_id: str) -> dict[str, Any] | None:
    params = {
        "result_id": result_id,
        "item_type": "re_residence",
        "transaction_name": "buy",
    }

    if cfg.XE_DETAIL_JITTER_MAX > 0:
        await asyncio.sleep(random.uniform(0.0, cfg.XE_DETAIL_JITTER_MAX))
    async with runtime.sem:
        tag = f"single_result id={result_id}"
        payload = await _api_get_json(
            runtime, cfg.SINGLE_RESULT_URL, params, max_retries=cfg.DETAIL_MAX_RETRIES, tag=tag,
        )
        if not isinstance(payload, dict):
            return None

        detail_node = payload.get("result")
        if not isinstance(detail_node, dict):
            detail_node = payload.get("t")
        if not isinstance(detail_node, dict):
            logger.warning("single_result missing detail node id=%s keys=%s", result_id, sorted(payload.keys()))
            return None

        ad_group_id = parse._first_present(detail_node, "ad_group_id")
        if ad_group_id:
            logger.info("id=%s belongs to ad_group=%s — fetching all units.", result_id, ad_group_id)
            units = await _fetch_ad_group_units_cached(runtime, str(ad_group_id))
            if units:
                detail_node, raw_floor = _build_hybrid_detail_node(detail_node, units, str(ad_group_id))
                chars = parse.flatten_characteristics(parse.find_characteristics_container(detail_node))
                if raw_floor is not None:
                    chars["floor"] = raw_floor
            else:
                logger.warning("Ad group %s returned no units — using base listing.", ad_group_id)
                chars = parse.flatten_characteristics(parse.find_characteristics_container(detail_node))
        else:
            chars = parse.flatten_characteristics(parse.find_characteristics_container(detail_node))

        listing = parse.get_listing_payload(detail_node, chars)
        if not listing:
            logger.warning("Listing payload missing valid URL for id=%s", result_id)
            return None
        logger.info("Scraped id=%-10s  %s", result_id, listing.get("url", "—"))
        return listing


async def run_scrape_cycle(
    max_pages: int = cfg.DEFAULT_MAX_PAGES,
    min_price: int = cfg.DEFAULT_MIN_PRICE,
    max_price: int = cfg.DEFAULT_MAX_PRICE,
    geo_place_id: str = cfg.ATHENS_CENTER_ID,
) -> None:
    session = await sess.get_impersonated_session()
    cookies_dict = dict(session.cookies)
    has_rodeo = bool(cookies_dict.get("_rodeo_session") or cookies_dict.get("rodeo_session"))
    has_csrf = bool(cookies_dict.get("csrf_token") or (session.headers.get("x-csrf-token") or "").strip())
    if not has_rodeo and not has_csrf:
        logger.warning(
            "XE: No _rodeo_session/rodeo_session and no csrf_token — cannot call APIs. "
            "Cookie keys present: %s — skipping scrape cycle.",
            sorted(cookies_dict.keys()),
        )
        return
    if not has_rodeo:
        logger.warning(
            "XE: _rodeo_session missing (keys: %s). Proceeding with csrf — if discovery returns 403, "
            "re-capture cookies or check WAF.",
            sorted(cookies_dict.keys()),
        )
    runtime = ScraperRuntime(session=session, concurrency=cfg.XE_DETAIL_CONCURRENCY)

    listings_store = load_local_listings()
    if os.getenv("XE_FAST", "").lower() in ("1", "true", "yes"):
        logger.info(
            "XE_FAST=1: concurrency=%s jitter=%s discovery_sleep=%s..%s map_preview=%s",
            cfg.XE_DETAIL_CONCURRENCY,
            cfg.XE_DETAIL_JITTER_MAX,
            cfg.XE_DISCOVERY_SLEEP_MIN,
            cfg.XE_DISCOVERY_SLEEP_MAX,
            cfg.XE_MAPSEARCH_PREVIEW,
        )
    elif getattr(cfg, "XE_STEALTH_MODE", False):
        logger.info(
            "XE_STEALTH: concurrency=%s jitter=%s discovery_sleep=%s..%s cooldown_every=%s_pages pause=%ss map_preview=%s",
            cfg.XE_DETAIL_CONCURRENCY,
            cfg.XE_DETAIL_JITTER_MAX,
            cfg.XE_DISCOVERY_SLEEP_MIN,
            cfg.XE_DISCOVERY_SLEEP_MAX,
            cfg.XE_COOLDOWN_EVERY_N_PAGES,
            cfg.XE_COOLDOWN_SECONDS,
            cfg.XE_MAPSEARCH_PREVIEW,
        )
    logger.info("Loaded %s existing listings from JSON store.", len(listings_store))

    processed_since_save = 0
    total_updated = 0
    total_reseen_date_only = 0
    consecutive_empty = 0
    total_refreshes = 0

    d_lo, d_hi = sorted((cfg.XE_DISCOVERY_SLEEP_MIN, cfg.XE_DISCOVERY_SLEEP_MAX))
    d_lo = max(0.0, d_lo)
    if d_hi > 0:
        _disc_sleep_lo, _disc_sleep_hi = d_lo, d_hi
    else:
        _disc_sleep_hi = _disc_sleep_lo = 0.0

    for page in range(1, max_pages + 1):
        if _disc_sleep_hi > 0:
            await asyncio.sleep(random.uniform(_disc_sleep_lo, _disc_sleep_hi))
        logger.info("Stage 1 Discovery: page=%s", page)
        items = await discover_items(
            runtime, page=page, min_price=min_price, max_price=max_price, geo_place_id=geo_place_id,
        )
        logger.info("Discovery page=%s returned %s items.", page, len(items))
        if not items:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                total_refreshes += 1
                if total_refreshes > 2:
                    logger.error(
                        "XE: Blocked after %s session refreshes — giving up.",
                        total_refreshes,
                    )
                    break
                logger.warning(
                    "%s consecutive empty discovery pages — likely blocked. Rebuilding session and sleeping %.0fs.",
                    consecutive_empty,
                    cfg.XE_EMPTY_PAGE_BACKOFF_SEC,
                )
                await runtime.refresh_session_cookies(force=True)
                if cfg.XE_EMPTY_PAGE_BACKOFF_SEC > 0:
                    await asyncio.sleep(cfg.XE_EMPTY_PAGE_BACKOFF_SEC)
                consecutive_empty = 0
            continue
        consecutive_empty = 0

        async def _enrich_row(mcard: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
            if cfg.XE_MAPSEARCH_PREVIEW:
                prev = parse.listing_from_mapsearch_preview(mcard)
                if prev:
                    return True, prev
            rid = parse.extract_property_id(mcard)
            if not rid:
                return False, None
            return False, await fetch_listing_detail(runtime, rid)

        logger.info(
            "Stage 2 Enrichment: %s rows, concurrency=%s, map_preview=%s",
            len(items), cfg.XE_DETAIL_CONCURRENCY, cfg.XE_MAPSEARCH_PREVIEW,
        )
        row_results = await asyncio.gather(
            *(_enrich_row(it) for it in items), return_exceptions=True,
        )

        preview_n = 0
        total_on_page = len(items)
        for idx, (mcard, packed) in enumerate(zip(items, row_results), 1):
            listing_id = parse.extract_property_id(mcard) or "—"
            if isinstance(packed, Exception):
                logger.exception(
                    "[%s/%s] Unexpected enrichment error for id=%s: %s",
                    idx, total_on_page, listing_id, packed,
                )
                continue
            from_preview, result = packed
            if from_preview:
                preview_n += 1
            if not result:
                logger.warning("[%s/%s] No data for id=%s", idx, total_on_page, listing_id)
                continue

            canonical_url = normalize_listing_url(result.get("url"))
            if not canonical_url:
                continue
            action = upsert_listing_in_url_store(listings_store, canonical_url, result)
            if action == "touched":
                total_reseen_date_only += 1
                logger.info(
                    "[%s/%s page=%s] URL already in store — updated_at only: %s",
                    idx, total_on_page, page, canonical_url,
                )
            else:
                total_updated += 1
                logger.info("[%s/%s page=%s] Stored #%s: %s", idx, total_on_page, page, total_updated, canonical_url)
            processed_since_save += 1

            if processed_since_save >= cfg.BATCH_COMMIT_SIZE:
                save_local_listings(listings_store)
                logger.info("Batch saved %s updates (store size=%s).", processed_since_save, len(listings_store))
                processed_since_save = 0

        if preview_n:
            logger.info(
                "Page %s: %s / %s rows from map_search preview (skipped single_result).",
                page, preview_n, total_on_page,
            )

        n_cool = cfg.XE_COOLDOWN_EVERY_N_PAGES
        sec_cool = cfg.XE_COOLDOWN_SECONDS
        if n_cool > 0 and sec_cool > 0 and page % n_cool == 0:
            logger.info("Cooldown after page %s: sleeping %.0fs.", page, sec_cool)
            await asyncio.sleep(sec_cool)

    if processed_since_save > 0:
        save_local_listings(listings_store)

    logger.info(
        "Scrape finished. New/updated rows=%s, date-only re-seen=%s, total in store=%s, output=%s",
        total_updated,
        total_reseen_date_only,
        len(listings_store),
        cfg.LISTINGS_JSON,
    )
