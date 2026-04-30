import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from curl_cffi import requests
from playwright.async_api import async_playwright

from greeceapt.cookies.cookie_manager import load_cookies
from greeceapt.utils.helpers import normalize_listing_url, resolve_neighborhood
from greeceapt.utils.url_builder import ATHENS_CENTER_ID, BASE_URL_XE, build_xe_url

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = PROJECT_ROOT / "data"
LISTINGS_JSON = DATA_DIR / "scraper_listings.json"
COOKIES_PATH = DATA_DIR / "cookies.json"

MAP_SEARCH_URL = "https://www.xe.gr/en/property/results/map_search"
SINGLE_RESULT_URL = "https://www.xe.gr/en/property/results/single_result"
AD_GROUP_URL = "https://www.xe.gr/en/property/unique_properties"
RESULTS_REFERER = BASE_URL_XE

BATCH_COMMIT_SIZE = int(os.getenv("BATCH_COMMIT_SIZE", "25"))
DETAIL_MAX_RETRIES = int(os.getenv("DETAIL_MAX_RETRIES", "3"))
DETAIL_RETRY_BASE_DELAY = float(os.getenv("DETAIL_RETRY_BASE_DELAY", "1.5"))
DISCOVERY_MAX_RETRIES = int(os.getenv("DISCOVERY_MAX_RETRIES", "3"))

DEFAULT_MIN_PRICE = 30_000
DEFAULT_MAX_PRICE = 70_000
DEFAULT_MAX_PAGES = 50
DISCOVERY_ITEM_KEYS = ("items", "result_items", "results", "ads")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _deep_first(node: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(node, dict):
        for key in keys:
            if key in node and node[key] not in (None, ""):
                return node[key]
        for value in node.values():
            found = _deep_first(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(node, list):
        for item in node:
            found = _deep_first(item, keys)
            if found not in (None, ""):
                return found
    return None


def clean_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


_FLOOR_TEXT_MAP: dict[str, float] = {
    "basement":               -1.0,
    "lower ground":           -1.0,
    "semi-basement":          -0.5,
    "semi basement":          -0.5,
    "ground floor":            0.0,
    "ground":                  0.0,
    "elevated ground floor":   0.5,
    "elevated ground":         0.5,
    "raised ground floor":     0.5,
    "mezzanine":               0.5,
}
_ORDINAL_RE = re.compile(r"(\d+)\s*(?:st|nd|rd|th)\b", re.IGNORECASE)


def clean_floor(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    # Plain integer string ("3") or negative ("-1")
    try:
        return float(int(text))
    except ValueError:
        pass
    # Ordinal: "1st", "2nd floor", "3rd", "4th floor"
    m = _ORDINAL_RE.search(text)
    if m:
        return float(int(m.group(1)))
    # Text keyword match (longest key first to avoid "ground" shadowing "ground floor")
    lower = text.lower()
    for key in sorted(_FLOOR_TEXT_MAP, key=len, reverse=True):
        if key in lower:
            return _FLOOR_TEXT_MAP[key]
    return None


def clean_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    token_match = re.search(r"\d[\d.,]*", str(value).strip())
    if not token_match:
        return None
    token = token_match.group(0)

    if "." in token and "," in token:
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif "," in token:
        parts = token.split(",")
        token = "".join(parts) if len(parts[-1]) == 3 else token.replace(",", ".")
    elif "." in token:
        parts = token.split(".")
        if len(parts[-1]) == 3 and len(parts) > 1:
            token = "".join(parts)

    try:
        return float(token)
    except ValueError:
        return None


def clean_price_eur(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not digits:
        return None
    try:
        return float(int(digits))
    except ValueError:
        return None


def normalize_publication_date(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def flatten_characteristics(node: dict[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    characteristics = node.get("characteristics_list")
    if not isinstance(characteristics, list):
        return flattened
    for entry in characteristics:
        if not isinstance(entry, dict):
            continue
        label_raw = _first_present(entry, "text", "label", "name", "title")
        if label_raw is None:
            continue
        label = str(label_raw).strip().lower()
        if not label:
            continue
        value = _first_present(entry, "value", "display_value", "text_value", "description")
        flattened[label] = value
    return flattened


def find_characteristics_container(node: dict[str, Any]) -> dict[str, Any]:
    if isinstance(node.get("characteristics_list"), list):
        return node
    for key in ("result", "t", "listing", "property", "details"):
        child = node.get(key)
        if isinstance(child, dict) and isinstance(child.get("characteristics_list"), list):
            return child
    return node


def _extract_photo_urls(node: dict[str, Any]) -> list[str]:
    photo_urls: list[str] = []
    photos = node.get("photos")
    if isinstance(photos, list):
        for photo in photos:
            if isinstance(photo, str) and photo:
                photo_urls.append(photo)
            elif isinstance(photo, dict):
                url = _first_present(photo, "url", "src", "image_url", "large", "original")
                if url:
                    photo_urls.append(str(url))

    image_gallery = node.get("image_gallery")
    if isinstance(image_gallery, list):
        for image in image_gallery:
            if isinstance(image, str) and image:
                photo_urls.append(image)
                continue
            if not isinstance(image, dict):
                continue
            for branch in ("fullscreen", "big", "medium", "small", "thumbnail"):
                branch_value = image.get(branch)
                if isinstance(branch_value, dict):
                    url = _first_present(branch_value, "jpeg", "webp", "jpg", "url")
                    if url:
                        photo_urls.append(str(url))
                        break
    return list(dict.fromkeys(photo_urls))


def extract_discovery_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in DISCOVERY_ITEM_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            if key != "items":
                logger.info("Discovery items extracted from response key '%s'.", key)
            return [item for item in value if isinstance(item, dict)]
    return []


def extract_property_id(item: dict[str, Any]) -> str | None:
    raw_id = _first_present(item, "id", "result_id", "ad_id", "listing_id")
    if raw_id is None:
        return None
    text = str(raw_id).strip()
    return text or None


def extract_address_parts(node: dict[str, Any]) -> tuple[str | None, str | None]:
    separated = node.get("address_separated")
    if isinstance(separated, list):
        municipality = str(separated[0]).strip() if len(separated) > 0 and separated[0] else None
        area_raw = str(separated[1]).strip() if len(separated) > 1 and separated[1] else None
        return municipality, area_raw
    if isinstance(separated, dict):
        municipality = _first_present(separated, "municipality", "city", "region")
        area_raw = _first_present(separated, "area", "neighborhood", "district")
        return (
            str(municipality).strip() if municipality else None,
            str(area_raw).strip() if area_raw else None,
        )
    return None, None


def _cookies_have_valid_entry(cookies: list[dict[str, Any]]) -> bool:
    if not cookies:
        return False
    now = time.time()
    for cookie in cookies:
        expires = cookie.get("expires")
        if not isinstance(expires, (int, float)):
            return True
        if expires <= 0 or expires > now:
            return True
    return False


def _is_dns_or_connection_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "could not resolve host" in msg
        or "name or service not known" in msg
        or "temporary failure in name resolution" in msg
        or "connection" in msg
        or "failed to perform" in msg
    )


async def _ensure_cookies_with_optional_force(force: bool) -> list[dict[str, Any]]:
    if force and COOKIES_PATH.exists():
        COOKIES_PATH.unlink()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            page = await context.new_page()

            for attempt in range(1, 4):
                try:
                    await page.goto(RESULTS_REFERER, wait_until="domcontentloaded", timeout=45000)
                except Exception as exc:
                    logger.warning("Cookie bootstrap navigation failed (attempt=%s): %s", attempt, exc)

                await page.wait_for_timeout(8000 + attempt * 2000)
                title = (await page.title()).strip().lower()
                content_sample = (await page.content())[:8000].lower()
                blocked = "human verification" in title or "awswafcookiedomainlist" in content_sample
                cookies = await context.cookies()
                if cookies and not blocked:
                    COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
                    with COOKIES_PATH.open("w", encoding="utf-8") as fh:
                        json.dump(cookies, fh, ensure_ascii=False, indent=2)
                    logger.info("Auto-captured %s cookies via Playwright.", len(cookies))
                    return cookies
                logger.warning("Cookie bootstrap still blocked (attempt=%s). Retrying...", attempt)

            cookies = await context.cookies()
            if cookies:
                COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
                with COOKIES_PATH.open("w", encoding="utf-8") as fh:
                    json.dump(cookies, fh, ensure_ascii=False, indent=2)
                logger.info("Saved %s fallback cookies after blocked bootstrap.", len(cookies))
                return cookies

            raise RuntimeError("Auto cookie bootstrap failed: no cookies captured.")
        finally:
            await browser.close()


async def get_valid_cookies(force: bool = False) -> list[dict[str, Any]]:
    if force:
        logger.info("Forced cookie refresh requested.")
        return await _ensure_cookies_with_optional_force(force=True)

    try:
        cookies = load_cookies(COOKIES_PATH)
    except FileNotFoundError:
        logger.info("cookies.json missing. Capturing fresh cookies via Playwright.")
        return await _ensure_cookies_with_optional_force(force=False)
    except Exception as exc:
        logger.warning("cookies.json unreadable (%s). Re-capturing cookies.", exc)
        return await _ensure_cookies_with_optional_force(force=False)

    if _cookies_have_valid_entry(cookies):
        logger.info("Reusing existing cookies from %s", COOKIES_PATH)
        return cookies

    # Session hygiene: keep using existing cookie jar until blocked errors explicitly trigger force refresh.
    logger.warning("cookies.json appears expired; reusing until blocked response triggers forced refresh.")
    return cookies


def build_impersonated_session(cookies: list[dict[str, Any]]) -> requests.Session:
    session = requests.Session(impersonate="chrome124")
    cookie_dict = {c.get("name"): c.get("value") for c in cookies if c.get("name")}
    session.cookies.update({k: v for k, v in cookie_dict.items() if v is not None})
    csrf_token = str(cookie_dict.get("csrf_token", "") or "")
    session.headers.update(
        {
            "accept": "application/json, text/javascript, */*; q=0.01",
            "referer": RESULTS_REFERER,
            "x-requested-with": "XMLHttpRequest",
            "x-csrf-token": csrf_token,
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }
    )
    return session


async def get_impersonated_session(force_cookies: bool = False) -> requests.Session:
    cookies = await get_valid_cookies(force=force_cookies)
    return build_impersonated_session(cookies)


async def discover_ids(
    runtime: "ScraperRuntime",
    page: int,
    min_price: int,
    max_price: int,
    geo_place_id: str,
) -> list[str]:
    search_url = build_xe_url(
        min_price=min_price,
        max_price=max_price,
        geo_place_id=geo_place_id,
        page=page,
    )
    parsed_params = parse_qs(urlsplit(search_url).query, keep_blank_values=True)
    params: dict[str, Any] = {
        key: values if len(values) > 1 else values[0]
        for key, values in parsed_params.items()
    }
    for attempt in range(1, DISCOVERY_MAX_RETRIES + 1):
        try:
            response = await asyncio.to_thread(runtime.session.get, MAP_SEARCH_URL, params=params, timeout=30)
        except Exception as exc:
            if _is_dns_or_connection_error(exc):
                logger.warning(
                    "Discovery network failure page=%s attempt=%s err=%s. Cooling 60s + cookie refresh.",
                    page,
                    attempt,
                    exc,
                )
                await asyncio.sleep(60)
                await runtime.refresh_session_cookies(force=True)
                if attempt < DISCOVERY_MAX_RETRIES:
                    continue
                return []
            logger.warning("Discovery request exception page=%s attempt=%s err=%s", page, attempt, exc)
            if attempt < DISCOVERY_MAX_RETRIES:
                await asyncio.sleep(DETAIL_RETRY_BASE_DELAY * attempt)
                continue
            return []

        if response.status_code in (403, 405, 429):
            logger.warning("Discovery blocked page=%s status=%s attempt=%s", page, response.status_code, attempt)
            await runtime.refresh_session_cookies(force=True)
            if attempt < DISCOVERY_MAX_RETRIES:
                # 405 means the server flagged the session; use a much longer back-off.
                backoff = 45.0 * attempt if response.status_code == 405 else DETAIL_RETRY_BASE_DELAY * attempt
                logger.info("Discovery back-off %.0fs before retry (page=%s attempt=%s).", backoff, page, attempt)
                await asyncio.sleep(backoff)
                continue
            return []

        if response.status_code != 200:
            logger.error("Discovery failed on page %s with status=%s", page, response.status_code)
            return []
        try:
            payload = response.json()
        except ValueError:
            logger.error("Discovery JSON parse failed on page %s.", page)
            return []

        items = extract_discovery_items(payload) if isinstance(payload, dict) else []
        if not items:
            if isinstance(payload, dict):
                logger.warning("Discovery returned no items on page %s. Response keys: %s", page, sorted(payload.keys()))
            else:
                logger.warning("Discovery returned unexpected payload type: %s", type(payload).__name__)
            return []
        return [listing_id for item in items if (listing_id := extract_property_id(item))]
    return []


def load_local_listings() -> dict[str, dict[str, Any]]:
    if not LISTINGS_JSON.exists():
        return {}
    try:
        with LISTINGS_JSON.open("r", encoding="utf-8") as fh:
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
        logger.error("Failed reading %s: %s", LISTINGS_JSON, exc)
        return {}


def save_local_listings(listings_store: dict[str, dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rows = list(listings_store.values())
    rows.sort(key=lambda row: str(row.get("url", "")))
    with LISTINGS_JSON.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)


def get_listing_payload(detail_node: dict[str, Any], chars: dict[str, Any]) -> dict[str, Any] | None:
    url = _first_present(detail_node, "url", "seo_url")
    canonical_url = normalize_listing_url(url)
    if not canonical_url:
        return None

    municipality, area_raw = extract_address_parts(detail_node)
    neighborhood = resolve_neighborhood(municipality, area_raw)
    photo_urls = _extract_photo_urls(detail_node)

    listing = {
        "url": str(url).strip(),
        "Headline": _first_present(detail_node, "title", "headline")
        or _deep_first(detail_node, ("title", "headline")),
        "price_eur": clean_price_eur(
            _first_present(detail_node, "price", "price_value") or _deep_first(detail_node, ("price", "price_value"))
        ),
        "price_per_sqm": clean_float(
            _first_present(detail_node, "price_per_sqm", "price_sqm", "price_per_square_meter", "price_per_unit_area")
            or _deep_first(detail_node, ("price_per_sqm", "price_sqm", "price_per_square_meter", "price_per_unit_area"))
        ),
        "area_sqm": clean_float(
            _first_present(detail_node, "size_with_square_meter", "size", "sqm")
            or _deep_first(detail_node, ("size_with_square_meter", "size", "sqm"))
        ),
        "municipality": municipality,
        "neighborhood": neighborhood,
        "address_raw": _first_present(detail_node, "address", "address_full")
        or _deep_first(detail_node, ("address", "address_full")),
        "bedrooms": clean_int(
            _first_present(detail_node, "bedrooms", "bedrooms_count")
            or _deep_first(detail_node, ("bedrooms", "bedrooms_count"))
        ),
        "bathrooms": clean_int(
            _first_present(detail_node, "bathrooms", "bathrooms_count")
            or _deep_first(detail_node, ("bathrooms", "bathrooms_count"))
        ),
        "floor": clean_floor(_first_present(chars, "floor")),
        "year_built": clean_int(
            _first_present(chars, "year built", "year of construction", "construction year")
            or _first_present(detail_node, "construction_year")
            or _deep_first(detail_node, ("construction_year",))
        ),
        "renovation_year": clean_int(_first_present(chars, "renovation year")),
        "energy_class": _first_present(chars, "energy class"),
        "photos_count": clean_int(_first_present(detail_node, "photos_count") or _deep_first(detail_node, ("photos_count",)))
        or len(photo_urls),
        "photo_urls": photo_urls,
        "publication_date": normalize_publication_date(
            _first_present(detail_node, "published_at", "publication_date", "publication_start_date", "date")
            or _deep_first(detail_node, ("published_at", "publication_date", "publication_start_date", "date"))
        ),
        "latitude": clean_float(
            _first_present(detail_node, "geo_lat", "latitude") or _deep_first(detail_node, ("geo_lat", "latitude"))
        ),
        "longitude": clean_float(
            _first_present(detail_node, "geo_lng", "longitude") or _deep_first(detail_node, ("geo_lng", "longitude"))
        ),
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }
    return listing


class ScraperRuntime:
    def __init__(self, session: requests.Session, concurrency: int) -> None:
        self.session = session
        self.sem = asyncio.Semaphore(concurrency)
        self.cookie_refresh_lock = asyncio.Lock()

    async def refresh_session_cookies(self, force: bool) -> None:
        async with self.cookie_refresh_lock:
            cookies = await get_valid_cookies(force=force)
            if force:
                # Full rebuild: new TLS connection pool + new cookies — avoids reusing a flagged session.
                self.session = build_impersonated_session(cookies)
                logger.info("Session fully rebuilt after forced cookie refresh.")
            else:
                cookie_dict = {c.get("name"): c.get("value") for c in cookies if c.get("name")}
                self.session.cookies.clear()
                self.session.cookies.update({k: v for k, v in cookie_dict.items() if v is not None})
                self.session.headers["x-csrf-token"] = str(cookie_dict.get("csrf_token", "") or "")


def _extract_unit_photo_urls(unit: dict[str, Any]) -> list[str]:
    """Extract photo URLs from a unit dict using the same logic as _extract_photo_urls plus extra keys."""
    # Reuse the main extractor (handles photos + image_gallery with nested branches)
    urls: list[str] = _extract_photo_urls(unit)

    # Also check extra keys that units sometimes use
    for key in ("media", "images", "gallery", "pictures", "media_gallery"):
        collection = unit.get(key)
        if not isinstance(collection, list):
            continue
        for item in collection:
            if isinstance(item, str) and item:
                urls.append(item)
            elif isinstance(item, dict):
                url = _first_present(item, "url", "src", "image_url", "large", "original", "media_url", "jpeg", "webp")
                if url:
                    urls.append(str(url))
    return list(dict.fromkeys(urls))


async def _fetch_ad_group_units(runtime: "ScraperRuntime", ad_group_id: str) -> list[dict[str, Any]]:
    """Call unique_properties and return the list of unit dicts (one HTTP call, no sub-URL navigation)."""
    params = {
        "ad_group_id": ad_group_id,
        "transaction_name": "buy",
        "item_type": "re_residence",
    }
    try:
        response = await asyncio.to_thread(runtime.session.get, AD_GROUP_URL, params=params, timeout=30)
    except Exception as exc:
        logger.warning("unique_properties fetch failed ad_group_id=%s: %s", ad_group_id, exc)
        return []
    if response.status_code != 200:
        logger.warning("unique_properties status=%s ad_group_id=%s", response.status_code, ad_group_id)
        return []
    try:
        payload = response.json()
    except ValueError:
        logger.warning("unique_properties JSON parse failed ad_group_id=%s", ad_group_id)
        return []
    if isinstance(payload, list):
        return [u for u in payload if isinstance(u, dict)]
    if isinstance(payload, dict):
        for key in ("results", "items", "units", "properties", "ads"):
            value = payload.get(key)
            if isinstance(value, list):
                return [u for u in value if isinstance(u, dict)]
    return []


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
        raw = _first_present(u, "price", "price_eur", "price_value", "final_price")
        p = clean_price_eur(raw)
        return p if p and p > 0 else float("inf")

    cheapest = min(units, key=_unit_price)
    cheapest_price = _unit_price(cheapest)
    has_valid_price = cheapest_price < float("inf")

    most_photos = max(units, key=lambda u: len(_extract_unit_photo_urls(u)))
    best_photos = _extract_unit_photo_urls(most_photos)

    merged = dict(base_node)
    raw_floor = None

    if has_valid_price:
        raw_price = _first_present(cheapest, "price", "price_eur", "price_value", "final_price")
        if raw_price is not None:
            merged["price"] = raw_price

        raw_size = _first_present(cheapest, "size_with_square_meter", "size", "area_sqm", "sqm", "area")
        if raw_size is not None:
            merged["size_with_square_meter"] = raw_size

        raw_floor = _first_present(cheapest, "floor")

        unit_url = _first_present(cheapest, "url", "seo_url", "link")
        if unit_url:
            merged["url"] = unit_url
            merged["seo_url"] = unit_url

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

    async with runtime.sem:
        await asyncio.sleep(random.uniform(5.0, 10.0))
        for attempt in range(1, DETAIL_MAX_RETRIES + 1):
            try:
                response = await asyncio.to_thread(runtime.session.get, SINGLE_RESULT_URL, params=params, timeout=30)
            except Exception as exc:
                if _is_dns_or_connection_error(exc):
                    logger.warning(
                        "single_result DNS/connection error for id=%s attempt=%s err=%s. Cooling 60s + cookie refresh.",
                        result_id,
                        attempt,
                        exc,
                    )
                    await asyncio.sleep(60)
                    await runtime.refresh_session_cookies(force=True)
                    if attempt < DETAIL_MAX_RETRIES:
                        continue
                    return None
                logger.warning("single_result request exception for id=%s attempt=%s err=%s", result_id, attempt, exc)
                if attempt < DETAIL_MAX_RETRIES:
                    await asyncio.sleep(DETAIL_RETRY_BASE_DELAY * attempt)
                    continue
                return None

            if response.status_code in (403, 405, 429):
                logger.warning("single_result throttled id=%s status=%s attempt=%s", result_id, response.status_code, attempt)
                await runtime.refresh_session_cookies(force=True)
                if attempt < DETAIL_MAX_RETRIES:
                    backoff = 45.0 * attempt if response.status_code == 405 else DETAIL_RETRY_BASE_DELAY * attempt
                    logger.info("single_result back-off %.0fs id=%s.", backoff, result_id)
                    await asyncio.sleep(backoff)
                    continue
                return None

            if response.status_code != 200:
                logger.warning("single_result failed for id=%s status=%s", result_id, response.status_code)
                return None

            try:
                payload = response.json()
            except ValueError:
                logger.warning("single_result JSON parse failed for id=%s", result_id)
                return None

            if not isinstance(payload, dict):
                logger.warning("single_result payload is not dict for id=%s", result_id)
                return None

            detail_node = payload.get("result")
            if not isinstance(detail_node, dict):
                detail_node = payload.get("t")
            if not isinstance(detail_node, dict):
                logger.warning("single_result missing usable detail node for id=%s keys=%s", result_id, sorted(payload.keys()))
                return None

            # ── Ad Group: hybrid selection (cheapest price + most photos) ──
            ad_group_id = _first_present(detail_node, "ad_group_id")
            if ad_group_id:
                logger.info("id=%s belongs to ad_group=%s — fetching all units.", result_id, ad_group_id)
                units = await _fetch_ad_group_units(runtime, str(ad_group_id))
                if units:
                    detail_node, raw_floor = _build_hybrid_detail_node(detail_node, units, str(ad_group_id))
                    chars = flatten_characteristics(find_characteristics_container(detail_node))
                    if raw_floor is not None:
                        chars["floor"] = raw_floor
                else:
                    logger.warning("Ad group %s returned no units — using base listing.", ad_group_id)
                    chars = flatten_characteristics(find_characteristics_container(detail_node))
            else:
                chars = flatten_characteristics(find_characteristics_container(detail_node))
            # ────────────────────────────────────────────────────────────────

            listing = get_listing_payload(detail_node, chars)
            if not listing:
                logger.warning("Listing payload missing valid URL for id=%s", result_id)
                return None
            logger.info("Scraped id=%-10s  %s", result_id, listing.get("url", "—"))
            return listing
    return None


async def run_scrape_cycle(
    max_pages: int = DEFAULT_MAX_PAGES,
    min_price: int = DEFAULT_MIN_PRICE,
    max_price: int = DEFAULT_MAX_PRICE,
    geo_place_id: str = ATHENS_CENTER_ID,
) -> None:
    session = await get_impersonated_session()
    runtime = ScraperRuntime(session=session, concurrency=3)

    listings_store = load_local_listings()
    logger.info("Loaded %s existing listings from JSON store.", len(listings_store))

    processed_since_save = 0
    total_updated = 0
    consecutive_empty = 0

    for page in range(1, max_pages + 1):
        logger.info("Stage 1 Discovery: page=%s", page)
        ids = await discover_ids(runtime, page=page, min_price=min_price, max_price=max_price, geo_place_id=geo_place_id)
        logger.info("Discovery page=%s returned %s IDs.", page, len(ids))
        if not ids:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                logger.warning(
                    "%s consecutive empty discovery pages — likely blocked. Rebuilding session and sleeping 120s.",
                    consecutive_empty,
                )
                await runtime.refresh_session_cookies(force=True)
                await asyncio.sleep(120)
                consecutive_empty = 0
            continue
        consecutive_empty = 0

        logger.info("Stage 2 Enrichment: running %s IDs with concurrency=%s", len(ids), 3)
        tasks = [fetch_listing_detail(runtime, listing_id) for listing_id in ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        total_on_page = len(ids)
        for idx, (listing_id, result) in enumerate(zip(ids, results), 1):
            if isinstance(result, Exception):
                logger.exception("[%s/%s] Unexpected enrichment error for id=%s: %s", idx, total_on_page, listing_id, result)
                continue
            if not result:
                logger.warning("[%s/%s] No data for id=%s", idx, total_on_page, listing_id)
                continue

            canonical_url = normalize_listing_url(result.get("url"))
            if not canonical_url:
                continue
            listings_store[canonical_url] = result
            total_updated += 1
            processed_since_save += 1
            logger.info("[%s/%s page=%s] Stored #%s: %s", idx, total_on_page, page, total_updated, canonical_url)

            if processed_since_save >= BATCH_COMMIT_SIZE:
                save_local_listings(listings_store)
                logger.info("Batch saved %s updates (store size=%s).", processed_since_save, len(listings_store))
                processed_since_save = 0

        save_local_listings(listings_store)
        logger.info("Saved after page=%s (store size=%s).", page, len(listings_store))
        if page % 3 == 0:
            logger.info("Cooldown after page %s: sleeping 120 seconds.", page)
            await asyncio.sleep(120)

    if processed_since_save > 0:
        save_local_listings(listings_store)

    logger.info(
        "Scrape finished. Updated listings=%s, total deduplicated records=%s, output=%s",
        total_updated,
        len(listings_store),
        LISTINGS_JSON,
    )


if __name__ == "__main__":
    asyncio.run(run_scrape_cycle())