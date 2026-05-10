"""Spitogatos Phase A (search) + Phase B (detail) scrape cycle."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from typing import Any

import json as _json_lib

from curl_cffi import requests as cffi_requests

from greeceapt.scraper.util import touch_listing_updated_at

from . import config as cfg
from . import parse as P

logger = logging.getLogger(__name__)

_IMPERSONATE = "chrome131"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _load_spito_cookies() -> dict[str, str]:
    """Load spito_cookies.json (reese84 bot-detection token) if it exists."""
    p = cfg.COOKIES_JSON
    if not p.exists():
        return {}
    try:
        data = _json_lib.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    items = data if isinstance(data, list) else data.get("cookies", [])
    cookies: dict[str, str] = {}
    for c in items:
        name = c.get("name")
        value = c.get("value")
        if name and value is not None:
            cookies[str(name)] = str(value)
    if cookies:
        logger.info("Loaded %s Spitogatos cookie(s) from %s", len(cookies), p.name)
    return cookies


def _make_session() -> cffi_requests.Session:
    s = cffi_requests.Session(impersonate=_IMPERSONATE)
    s.headers.update({
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "x-locale": "en",
        "x-mdraw": "1",
        "x-requested-with": "XMLHttpRequest",
        "referer": cfg.SEARCH_URL,
        "content-type": "application/json",
        "user-agent": _USER_AGENT,
    })
    spito_cookies = _load_spito_cookies()
    if spito_cookies:
        s.cookies.update(spito_cookies)
    return s


async def _post_json(session: cffi_requests.Session, url: str, body: dict, *, tag: str) -> Any | None:
    """POST JSON; return parsed root or None on transport / HTTP / JSON failure."""
    try:
        resp = await asyncio.to_thread(session.post, url, data=json.dumps(body), timeout=30)
    except Exception as exc:
        logger.error("%s: %s", tag, exc)
        return None
    if not resp.ok:
        if resp.status_code in (403, 429):
            logger.error(
                "%s status=%s — Spitogatos bot-detection cookie may be expired. "
                "Re-capture: open https://www.spitogatos.gr in a browser, export cookies "
                "via a browser extension, save as data/spito_cookies.json, then re-run.",
                tag, resp.status_code,
            )
        else:
            logger.warning("%s status=%s", tag, resp.status_code)
        return None
    try:
        return resp.json()
    except Exception:
        logger.error("%s JSON parse error", tag)
        return None


async def _get_json(session: cffi_requests.Session, url: str, *, tag: str) -> Any | None:
    """GET; return parsed root or None."""
    try:
        resp = await asyncio.to_thread(session.get, url, timeout=30)
    except Exception as exc:
        logger.warning("%s: %s", tag, exc)
        return None
    if not resp.ok:
        logger.warning("%s status=%s", tag, resp.status_code)
        return None
    try:
        return resp.json()
    except Exception:
        return None


def _props_from_search_root(data: Any) -> list:
    if not isinstance(data, dict):
        return []
    return data.get("data") or data.get("properties") or data.get("results") or []


async def _collect_listing_ids(
    session: cffi_requests.Session,
    existing_store: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, str], int]:
    pending: dict[str, dict[str, Any]] = {}
    pid_map: dict[str, str] = {}
    consecutive_errors = 0
    reseen_touched = 0

    for offset in range(0, cfg.MAX_OFFSET, 30):
        payload = {**cfg.SEARCH_PAYLOAD, "offset": offset}
        logger.info("Phase A — offset=%s  new=%s  reseen_touched_total=%s", offset, len(pending), reseen_touched)
        data = await _post_json(session, cfg.SEARCH_API, payload, tag=f"Phase A offset={offset}")
        if data is None:
            consecutive_errors += 1
            if consecutive_errors >= 5:
                logger.error("Phase A: aborting after %s consecutive errors.", consecutive_errors)
                break
            await asyncio.sleep(random.uniform(3.0, 6.0))
            continue

        consecutive_errors = 0
        props = _props_from_search_root(data)
        if not props:
            logger.info("Phase A — empty page at offset=%s, done.", offset)
            break

        for prop in props:
            pid = prop.get("id")
            if not pid:
                continue
            image_ids = prop.get("imageIds") or []
            pid_str = str(pid)
            url = f"https://www.spitogatos.gr/en/property/11{pid_str}"
            if url in existing_store:
                touch_listing_updated_at(existing_store[url])
                reseen_touched += 1
                continue
            if url in pending:
                continue

            geo = prop.get("geography")
            neighborhood, municipality = P.neighborhood_from_geo(geo if isinstance(geo, dict) else {})
            geo_str = prop.get("geography") or ""
            if isinstance(geo_str, str) and not neighborhood:
                neighborhood = geo_str.split("(")[0].strip() or None

            raw_date = prop.get("modified") or prop.get("uploaded") or prop.get("firstPublishDate")
            raw_floor = prop.get("floorNumber")
            photo_urls = [
                f"https://m3.spitogatos.gr/{iid}_300x220.jpg?v=20130730"
                for iid in image_ids
            ]
            now_iso = datetime.now(timezone.utc).isoformat()
            pending[url] = {
                "url": url,
                "title": None,
                "description": None,
                "price_eur": prop.get("price"),
                "price_per_sqm": None,
                "area_sqm": prop.get("sq_meters"),
                "municipality": municipality,
                "neighborhood": neighborhood,
                "address_raw": None,
                "bedrooms": prop.get("rooms"),
                "bathrooms": prop.get("no_of_bathrooms"),
                "floor": float(raw_floor) if raw_floor is not None else None,
                "year_built": None,
                "renovation_year": None,
                "heating_type": None,
                "energy_class": None,
                "photo_urls": photo_urls,
                "photos_count": len(photo_urls),
                "publication_date": (
                    str(raw_date)[:10] if raw_date and len(str(raw_date)) >= 10 else None
                ),
                "latitude": P.clean_float(prop.get("latitude")),
                "longitude": P.clean_float(prop.get("longitude")),
                "scraped_at": now_iso,
                "updated_at": now_iso,
            }
            pid_map[url] = pid_str

        await asyncio.sleep(random.uniform(1.5, 3.0))

    return pending, pid_map, reseen_touched


async def _enrich_listing(session: cffi_requests.Session, pid: str) -> dict[str, Any]:
    async with cfg.DETAIL_SEM:
        await asyncio.sleep(random.uniform(0.3, 1.0))
        root = await _get_json(session, cfg.DETAIL_API.format(id=pid), tag=f"detail pid={pid}")
        if not isinstance(root, dict):
            return {}
        d = root.get("data") or {}
        if not isinstance(d, dict):
            return {}

        neighborhood, municipality = P.neighborhood_from_geo(d.get("geographiesByLevel"))
        images = d.get("images") or []
        photo_urls = P.photo_urls_from_images(images)
        result: dict[str, Any] = {}

        title = d.get("title") or d.get("name") or d.get("heading")
        if isinstance(title, str) and title.strip():
            result["title"] = title.strip()

        desc = d.get("description")
        if isinstance(desc, str) and desc.strip():
            result["description"] = desc.strip()

        if municipality:
            result["municipality"] = municipality
        if neighborhood:
            result["neighborhood"] = neighborhood

        addr = d.get("streetAddress")
        if isinstance(addr, str) and addr.strip():
            result["address_raw"] = addr.strip()

        ppsm = P.clean_float(d.get("pricePerSqMeters"))
        if ppsm:
            result["price_per_sqm"] = ppsm

        yb = P.clean_year(d.get("year_of_construction"))
        if yb:
            result["year_built"] = yb

        ry = P.clean_year(d.get("renovationYear"))
        if ry:
            result["renovation_year"] = ry

        ec = P.decode_energy_class(d.get("energyClass"))
        if ec:
            result["energy_class"] = ec

        ht = P.decode_heating(d.get("heatingController"), d.get("heatingMedium"))
        if ht:
            result["heating_type"] = ht

        if photo_urls:
            result["photo_urls"] = photo_urls
            result["photos_count"] = len(photo_urls)

        lat = P.clean_float(d.get("latitude"))
        lng = P.clean_float(d.get("longitude"))
        if lat:
            result["latitude"] = lat
        if lng:
            result["longitude"] = lng

        best_dt = None
        for key in ("modified", "uploaded", "firstPublishDate", "updatedAt"):
            dt = P.parse_spitogatos_datetime(d.get(key))
            if dt is not None and (best_dt is None or dt > best_dt):
                best_dt = dt
        if best_dt:
            result["publication_date"] = best_dt.date().isoformat()

        logger.info(
            "Enriched %-12s  year=%s  energy=%s  heat=%s  desc=%s",
            pid,
            result.get("year_built", "—"),
            result.get("energy_class", "—"),
            result.get("heating_type", "—")[:20] if result.get("heating_type") else "—",
            "yes" if result.get("description") else "no",
        )
        return result


async def run() -> None:
    session = _make_session()
    logger.info("=== Spitogatos — API-only scraper (no browser needed) ===")

    store = P.load_listings_url_map(cfg.LISTINGS_JSON)
    logger.info("Loaded %s existing listings from %s", len(store), cfg.LISTINGS_JSON.name)

    pending, pid_map, reseen_touched = await _collect_listing_ids(session, store)
    if not pending and reseen_touched == 0:
        logger.warning("Phase A returned no listings and no re-seen URLs.")
        return

    logger.info(
        "Phase A complete — %s new to enrich, %s URLs touched (updated_at only).",
        len(pending), reseen_touched,
    )
    store.update(pending)
    P.atomic_save(store)

    urls = list(pending.keys())
    total = len(urls)
    if total == 0:
        logger.info("Phase B skipped — nothing new to enrich.")
        P.atomic_save(store)
        logger.info("Done. %s rows in %s", len(store), cfg.LISTINGS_JSON)
        return

    logger.info("Phase B — enriching %s new listings via detail API …", total)
    enriched = 0
    saved_since_last = 0

    for batch_start in range(0, total, cfg.ROTATE_EVERY):
        batch_urls = urls[batch_start: batch_start + cfg.ROTATE_EVERY]
        batch_session = _make_session()
        logger.info(
            "Phase B batch %s–%s / %s (fresh session)",
            batch_start + 1, batch_start + len(batch_urls), total,
        )

        tasks = [_enrich_listing(batch_session, pid_map[url]) for url in batch_urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for url, result in zip(batch_urls, results):
            if isinstance(result, Exception):
                logger.warning("Enrich error url=%s: %s", url, result)
                continue
            if not result:
                continue
            store[url].update(result)
            touch_listing_updated_at(store[url])
            enriched += 1
            saved_since_last += 1
            if saved_since_last >= cfg.BATCH_SAVE_SIZE:
                P.atomic_save(store)
                logger.info("Saved — %s / %s enriched.", enriched, total)
                saved_since_last = 0

    P.atomic_save(store)
    logger.info("Done. %s rows in store (%s enriched this run) → %s", len(store), enriched, cfg.LISTINGS_JSON)
